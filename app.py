#!/usr/bin/env python3
"""
Pi Cable Tester - fullscreen touch UI for a 640x480 display.

Exit codes (acted on by launch.sh):
   0 -> power off
  10 -> reboot (requested from the menu)
  20 -> another copy is already running (launcher does nothing)
  30 -> exit to desktop (EXIT button)
  anything else -> crash -> reboot
Run windowed for development:  PITESTER_WINDOWED=1 python3 app.py
"""
import fcntl
import functools
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import netcore as nc  # noqa: E402
import preflight  # noqa: E402
import storage  # noqa: E402

EXIT_SHUTDOWN, EXIT_REBOOT, EXIT_ALREADY_RUNNING, EXIT_CRASH, EXIT_DESKTOP = 0, 10, 20, 1, 30
WINDOWED = os.environ.get("PITESTER_WINDOWED") == "1"
log = logging.getLogger("pitester")

BG, PANEL, PANEL2 = "#0f1419", "#1b232c", "#26323e"
FG, DIM, ACCENT = "#e6edf3", "#8b98a5", "#2f81f7"
GREEN, RED, AMBER = "#2ea043", "#da3633", "#c98a14"
STATUS_COLOR = {nc.PASS: GREEN, nc.FAIL: RED, nc.WARN: AMBER}
VERDICT = {
    nc.PASS: "PASS",
    nc.WARN: "WARNING",
    nc.FAIL: "FAIL",
    "": "DONE",
}

F_SMALL = ("DejaVu Sans", 11)
F_BODY = ("DejaVu Sans", 13)
F_BOLD = ("DejaVu Sans", 13, "bold")
F_BTN = ("DejaVu Sans", 16, "bold")
F_TITLE = ("DejaVu Sans", 15, "bold")
F_BIG = ("DejaVu Sans", 22, "bold")


# ================================================================ widgets
def mkbtn(
    parent,
    text,
    cmd,
    bg=PANEL2,
    font=F_BTN,
):
    return tk.Button(
        parent,
        text=text,
        command=cmd,
        bg=bg,
        fg=FG,
        activebackground=ACCENT,
        activeforeground=FG,
        font=font,
        bd=0,
        relief="flat",
        highlightthickness=0,
    )


class confirm_button(tk.Button):
    """First tap arms it (turns red), second tap within 3 s runs it."""

    def __init__(
        self,
        parent,
        text,
        cmd,
        bg=PANEL2,
        font=F_BTN,
    ):
        self._text, self._cmd, self._bg = text, cmd, bg
        self._armed, self._job = False, None
        super().__init__(
            parent,
            text=text,
            command=self._tap,
            bg=bg,
            fg=FG,
            activebackground=RED,
            activeforeground=FG,
            font=font,
            bd=0,
            relief="flat",
            highlightthickness=0,
        )

    def set_text(self, text):
        self._text = text
        if not self._armed:
            self.config(text=text)

    def _tap(self):
        if self._armed:
            self._disarm()
            self._cmd()
        else:
            self._armed = True
            self.config(text="TAP AGAIN", bg=RED)
            self._job = self.after(3000, self._disarm)

    def _disarm(self):
        self._armed = False
        if self._job:
            self.after_cancel(self._job)
            self._job = None

        self.config(text=self._text, bg=self._bg)


class touch_scroll(tk.Frame):
    """Canvas-backed frame you scroll by dragging a finger.

    A press only counts as a drag once it moves more than 10px, so a plain
    tap still reaches whatever's underneath (see load_screen's row click,
    which checks self.dragged before opening a file).
    """

    DRAG_THRESHOLD_PX = 10

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.canvas = tk.Canvas(
            self,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window(
            0,
            0,
            window=self.inner,
            anchor="nw",
        )
        self.inner.bind("<Configure>", self._region)
        self.canvas.bind("<Configure>", self._resize)
        self.dragged, self._y0 = False, 0
        self._bind(self.canvas)
        self._bind(self.inner)

    def _resize(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._region()

    def _region(self, _e=None):
        h = max(self.inner.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), h))

    def _bind(self, w):
        w.bind("<ButtonPress-1>", self._press, add="+")
        w.bind("<B1-Motion>", self._drag, add="+")
        w.bind("<Button-4>", self._wheel_up, add="+")
        w.bind("<Button-5>", self._wheel_down, add="+")

    def _wheel_up(self, _e=None):
        self.canvas.yview_scroll(-3, "units")

    def _wheel_down(self, _e=None):
        self.canvas.yview_scroll(3, "units")

    def bind_children(self, w=None):
        for c in (w or self.inner).winfo_children():
            self._bind(c)
            self.bind_children(c)

    def _press(self, e):
        self._y0, self.dragged = e.y_root, False
        self.canvas.scan_mark(0, e.y_root)

    def _drag(self, e):
        if abs(e.y_root - self._y0) > self.DRAG_THRESHOLD_PX:
            self.dragged = True
        if self.dragged:
            self.canvas.scan_dragto(0, e.y_root, gain=1)

    def clear(self, reset=False):
        for c in self.inner.winfo_children():
            c.destroy()
        if reset:
            self.canvas.yview_moveto(0)


def render_sections(scroll, sections, reset=False):
    """Draw a list of (title, [(field, value, status), ...]) as stacked panels.

    Each section gets an accent-colored heading, then a panel with three
    grid columns: field name (dim, fixed width), value (wraps), and a
    PASS/WARN/FAIL badge on the right if a status was given.
    """
    scroll.clear(reset)
    for title, rows in sections:
        title_lbl = tk.Label(
            scroll.inner,
            text=title,
            font=F_BOLD,
            fg=ACCENT,
            bg=BG,
            anchor="w",
        )
        title_lbl.pack(fill="x", padx=8, pady=(8, 2))

        box = tk.Frame(scroll.inner, bg=PANEL)
        box.pack(fill="x", padx=6)
        box.columnconfigure(1, weight=1)
        for i, (field, value, status) in enumerate(rows):
            field_lbl = tk.Label(
                box,
                text=field,
                font=F_SMALL,
                fg=DIM,
                bg=PANEL,
                anchor="nw",
                justify="left",
                width=14,
            )
            field_lbl.grid(
                row=i,
                column=0,
                sticky="nw",
                padx=(6, 4),
                pady=3,
            )
            value_lbl = tk.Label(
                box,
                text=str(value),
                font=F_BODY,
                fg=FG,
                bg=PANEL,
                anchor="w",
                justify="left",
                wraplength=400,
            )
            value_lbl.grid(
                row=i,
                column=1,
                sticky="w",
                pady=3,
            )
            if status:
                status_lbl = tk.Label(
                    box,
                    text=status,
                    font=F_BOLD,
                    bg=PANEL,
                    fg=STATUS_COLOR.get(status, DIM),
                )
                status_lbl.grid(
                    row=i,
                    column=2,
                    sticky="ne",
                    padx=6,
                    pady=3,
                )

    tk.Frame(scroll.inner, bg=BG, height=12).pack()  # bottom spacer so the last row isn't flush
    scroll.bind_children()


# ================================================================ screens
class screen(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app

    def on_show(self):
        pass

    def on_msg(self, kw):
        pass

    def on_done(self):
        pass

    def _go_home(self):
        """Shared BACK target: every screen except LOAD's viewer returns to home."""
        self.app.show("home")


class report_screen(screen):
    """Base layout for every screen that shows a scrollable list of results:
    title + info text up top, a row of buttons pinned to the bottom, and the
    scrollable report body in between. Banner and progress bar start hidden
    and are shown on demand via set_banner()/set_progress().
    """

    def __init__(self, parent, app, title):
        super().__init__(parent, app)

        # ---- header: title on the left, status text on the right ----
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", side="top")
        self.title_lbl = tk.Label(
            head,
            text=title,
            font=F_TITLE,
            fg=FG,
            bg=BG,
        )
        self.title_lbl.pack(side="left", padx=8, pady=(4, 2))
        self.info_lbl = tk.Label(
            head,
            text="",
            font=F_SMALL,
            fg=DIM,
            bg=BG,
        )
        self.info_lbl.pack(side="right", padx=8)

        # ---- button bar, pinned to the bottom; buttons added via add_btn() ----
        self.bar = tk.Frame(self, bg=BG)
        self.bar.pack(
            fill="x",
            side="bottom",
            padx=3,
            pady=3,
        )

        # ---- scrollable report body, fills whatever space is left ----
        self.scroll = touch_scroll(self)
        self.scroll.pack(fill="both", expand=True, side="top")

        # ---- banner + progress bar: created now, packed in later on demand ----
        self.banner = tk.Label(
            self,
            font=F_BIG,
            fg="white",
            bg=PANEL2,
            pady=4,
        )
        self.progress = tk.Canvas(
            self,
            height=10,
            bg=PANEL,
            highlightthickness=0,
        )

    def add_btn(
        self,
        text,
        cmd,
        bg=PANEL2,
        confirm=False,
        font=F_BTN,
    ):
        cls = confirm_button if confirm else mkbtn
        b = cls(
            self.bar,
            text,
            cmd,
            bg=bg,
            font=font,
        )
        b.pack(
            side="left",
            fill="both",
            expand=True,
            padx=3,
            ipady=10,
        )
        return b

    def set_sections(self, sections, reset=False):
        render_sections(self.scroll, sections, reset)

    def set_banner(self, text, status=""):
        if not text:
            self.banner.pack_forget()
            return

        self.banner.config(text=text, bg=STATUS_COLOR.get(status, PANEL2))
        self.banner.pack(
            fill="x",
            padx=6,
            pady=(0, 4),
            before=self.scroll,
        )

    def set_progress(self, frac):
        if frac is None:
            self.progress.pack_forget()
            return

        self.progress.pack(
            fill="x",
            padx=6,
            pady=(0, 4),
            before=self.scroll,
        )
        self.progress.update_idletasks()
        w = self.progress.winfo_width()
        self.progress.delete("all")
        self.progress.create_rectangle(
            0,
            0,
            int(w * max(0, min(1, frac))),
            10,
            fill=ACCENT,
            width=0,
        )


class home_screen(screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)

        # ---- summary card: switch / port / vlan / ip from the last scan ----
        card = tk.Frame(self, bg=PANEL)
        card.pack(fill="x", padx=6, pady=(6, 4))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)
        self.vals = {}
        summary_fields = [
            ("switch", "SWITCH"),
            ("port", "PORT"),
            ("vlan", "VLAN"),
            ("ip", "IP"),
        ]
        for i, (key, label) in enumerate(summary_fields):
            r, c = divmod(i, 2)
            label_lbl = tk.Label(card, text=label, font=F_SMALL, fg=DIM, bg=PANEL)
            label_lbl.grid(
                row=r,
                column=c * 2,
                sticky="w",
                padx=(8, 4),
                pady=4,
            )
            v = tk.Label(
                card,
                text=nc.NA,
                font=F_BOLD,
                fg=FG,
                bg=PANEL,
                anchor="w",
            )
            v.grid(
                row=r,
                column=c * 2 + 1,
                sticky="we",
                pady=4,
            )
            self.vals[key] = v

        # ---- one-line status text ("plug in a cable", "scanning...", etc) ----
        self.state_lbl = tk.Label(
            card,
            text="Plug in a cable to start",
            font=F_SMALL,
            fg=DIM,
            bg=PANEL,
            anchor="w",
        )
        self.state_lbl.grid(
            row=2,
            column=0,
            columnspan=4,
            sticky="we",
            padx=8,
            pady=(0, 4),
        )

        # ---- main 3x2 action grid ----
        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="both", expand=True, padx=4)
        buttons = [
            ("SCAN", app.cmd_scan, ACCENT),
            ("CABLE\nTEST", self._go_cable, PANEL2),
            ("1000BASE-T\nTEST", self._go_gig, PANEL2),
            ("BLINK\nPORT", self._go_blink, PANEL2),
            ("SAVE", app.cmd_save, PANEL2),
            ("LOAD", self._go_load, PANEL2),
        ]
        for i, (t, cmd, bg) in enumerate(buttons):
            r, c = divmod(i, 3)
            btn = mkbtn(
                grid,
                t,
                cmd,
                bg=bg,
            )
            btn.grid(
                row=r,
                column=c,
                sticky="nsew",
                padx=3,
                pady=3,
            )
        grid.columnconfigure(0, weight=1, uniform="c")
        grid.columnconfigure(1, weight=1, uniform="c")
        grid.columnconfigure(2, weight=1, uniform="c")
        grid.rowconfigure(0, weight=1, uniform="r")
        grid.rowconfigure(1, weight=1, uniform="r")

        # ---- system menu entry point (reboot / power off / exit / AP toggle) ----
        sys_btn = mkbtn(
            self,
            "SYSTEM",
            self._go_system,
            font=F_BOLD,
        )
        sys_btn.pack(
            fill="x",
            padx=7,
            pady=(0, 6),
            ipady=6,
        )

    # ---- button targets (named instead of inline lambdas, so they show up
    # by name in tracebacks and can be set as breakpoints) ----
    def _go_cable(self):
        self.app.show("cable")

    def _go_gig(self):
        self.app.show("gig")

    def _go_blink(self):
        self.app.show("blink")

    def _go_load(self):
        self.app.show("load")

    def _go_system(self):
        self.app.show("system")

    def refresh(self):
        ident = self.app.ident
        for k, lbl in self.vals.items():
            t = str(ident.get(k) or nc.NA)
            lbl.config(text=t if len(t) <= 20 else t[:19] + "...")
        self.state_lbl.config(text=ident.get("state") or "")

    def on_show(self):
        self.refresh()


class scan_screen(report_screen):
    def __init__(self, parent, app):
        super().__init__(parent, app, "NETWORK SCAN")
        self.add_btn("BACK", self._go_home)
        self.add_btn("RESCAN", app.cmd_scan, bg=ACCENT)
        self.add_btn("SAVE", app.cmd_save)
        self.set_sections([("SCAN", [("Status", "Plug in a cable or tap RESCAN", "")])])

    def on_msg(self, kw):
        self.set_sections(kw["sections"])
        self.info_lbl.config(text=kw["ident"].get("state", ""))

    def on_done(self):
        self.info_lbl.config(text=f"{self.app.ident.get('state', '')}  "
                                  f"({time.strftime('%H:%M:%S')})")


class cable_screen(report_screen):
    INTRO = [("CABLE TEST", [
        ("Link check", "Always runs: reads pair health from the negotiated speed", ""),
        ("TDR", "Uses ethtool --cable-test if the Pi's PHY supports it: per-pair "
                "open/short + distance. The link drops for a few seconds while it runs.", ""),
        ("Tip", "Leave the far end unplugged to measure cable length and find breaks.", "")])]

    def __init__(self, parent, app):
        super().__init__(parent, app, "CABLE TEST")
        self.add_btn("BACK", self._go_home)
        self.run_btn = self.add_btn("RUN", app.cmd_cable, bg=ACCENT)
        self.set_sections(self.INTRO)

    def on_show(self):
        if self.app.busy != "cable":
            res = self.app.results.get("cable")
            self.set_sections(res or self.INTRO, reset=True)
            self.set_banner(VERDICT[nc.worst(res)] if res else "", nc.worst(res) if res else "")

    def running(self):
        self.set_banner("TESTING...")
        self.run_btn.config(state="disabled")

    def on_msg(self, kw):
        self.set_sections(kw["sections"])
        if kw.get("final"):
            self.app.results["cable"] = kw["sections"]
            w = nc.worst(kw["sections"])
            self.set_banner(VERDICT[w], w)

    def on_done(self):
        self.run_btn.config(state="normal")


class gig_screen(report_screen):
    DURS = [10, 30, 60]
    INTRO = [("1000BASE-T TEST", [
        ("Checks", "Link negotiates 1000/Full, holds it for the test time with no drops, "
                   "no CRC/frame errors, and passes 1400-byte traffic to the gateway.", ""),
        ("Note", "Qualifies the link for gigabit. Not a TIA Cat5e/6 certification "
                 "(no NEXT / return-loss measurement).", "")])]

    def __init__(self, parent, app):
        super().__init__(parent, app, "1000BASE-T TEST")
        self.duration, self.is_running = 30, False
        self.add_btn("BACK", self._go_home)
        self.dur_btn = self.add_btn("30 s", self._cycle)
        self.run_btn = self.add_btn("RUN", self._run, bg=ACCENT)
        self.set_sections(self.INTRO)

    def on_show(self):
        if not self.is_running:
            res = self.app.results.get("gig")
            self.set_sections(res or self.INTRO, reset=True)
            self.set_banner(VERDICT[nc.worst(res)] if res else "", nc.worst(res) if res else "")

    def _cycle(self):
        if not self.is_running:
            self.duration = self.DURS[(self.DURS.index(self.duration) + 1) % len(self.DURS)]
            self.dur_btn.config(text=f"{self.duration} s")

    def _run(self):
        if self.is_running:
            self.app.stop_task("gig")
            return

        if self.app.busy:
            self.app.toast(f"Busy: {self.app.busy}", AMBER)
            return

        self.is_running = True
        self.run_btn.config(text="STOP", bg=RED)
        self.set_banner("TESTING...")
        self.set_progress(0)
        self.app.cmd_gig(self.duration)

    def on_msg(self, kw):
        if "sections" in kw:
            self.set_sections(kw["sections"])
        if "progress" in kw:
            el, d = kw["progress"]
            self.set_progress(el / d)
            self.info_lbl.config(text=f"{int(el)} / {d} s")
        if kw.get("final"):
            self.app.results["gig"] = kw["sections"]
            w = nc.worst(kw["sections"])
            self.set_banner(VERDICT[w] + (" - GIGABIT OK" if w == nc.PASS else ""), w)

    def on_done(self):
        self.is_running = False
        self.run_btn.config(text="RUN", bg=ACCENT)
        self.set_progress(None)
        self.info_lbl.config(text="")


class blink_screen(report_screen):
    MODES = {"traffic": "Bursts of traffic to the gateway: the port's activity LED "
                        "flickers hard for 1 s on / 1 s off.",
             "link": "Restarts auto-negotiation every few seconds: the port LED goes "
                     "dark and comes back. Most visible, but the link drops briefly."}

    def __init__(self, parent, app):
        super().__init__(parent, app, "BLINK PORT")
        self.mode, self.is_running, self.status = "traffic", False, "Idle"
        self.add_btn("BACK", self._go_home)
        self.mode_btn = self.add_btn("MODE: TRAFFIC", self._cycle, font=F_BOLD)
        self.run_btn = self.add_btn("START", self._toggle, bg=ACCENT)
        self._draw()

    def _draw(self):
        self.set_sections([("BLINK PORT", [("Mode", self.MODES[self.mode], ""),
                                           ("Status", self.status, ""),
                                           ("Duration", "30 s (tap STOP to end early)", "")])])

    def _cycle(self):
        if not self.is_running:
            self.mode = "link" if self.mode == "traffic" else "traffic"
            self.mode_btn.config(text=f"MODE: {self.mode.upper()}")
            self._draw()

    def _toggle(self):
        if self.is_running:
            self.app.stop_task("blink")
            return

        if self.app.busy:
            self.app.toast(f"Busy: {self.app.busy}", AMBER)
            return

        self.is_running = True
        self.run_btn.config(text="STOP", bg=RED)
        self.app.cmd_blink(self.mode)

    def on_msg(self, kw):
        if "status" in kw:
            self.status = kw["status"]
            self._draw()

    def on_done(self):
        self.is_running = False
        self.run_btn.config(text="START", bg=ACCENT)


class load_screen(screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)

        # ---- header: title on the left, file count on the right ----
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x")
        title_lbl = tk.Label(
            head,
            text="SAVED RESULTS",
            font=F_TITLE,
            fg=FG,
            bg=BG,
        )
        title_lbl.pack(side="left", padx=8, pady=(4, 2))
        self.count = tk.Label(
            head,
            text="",
            font=F_SMALL,
            fg=DIM,
            bg=BG,
        )
        self.count.pack(side="right", padx=8)

        # ---- back button, pinned to the bottom ----
        bar = tk.Frame(self, bg=BG)
        bar.pack(
            fill="x",
            side="bottom",
            padx=3,
            pady=3,
        )
        back_btn = mkbtn(bar, "BACK", self._go_home)
        back_btn.pack(
            fill="both",
            expand=True,
            padx=3,
            ipady=10,
        )

        # ---- scrollable list of saved result files, populated in on_show ----
        self.scroll = touch_scroll(self)
        self.scroll.pack(fill="both", expand=True)

    def on_show(self):
        self.scroll.clear(reset=True)
        files = storage.list_results()
        self.count.config(text=f"{len(files)} file(s)")
        if not files:
            empty_lbl = tk.Label(
                self.scroll.inner,
                text=f"Nothing saved yet.\n{storage.RESULTS_DIR}",
                font=F_BODY,
                fg=DIM,
                bg=BG,
            )
            empty_lbl.pack(pady=40)

        for p in files[:300]:
            name, ts = storage.describe(p)
            row = tk.Frame(self.scroll.inner, bg=PANEL)
            row.pack(fill="x", padx=6, pady=3)
            name_lbl = tk.Label(
                row,
                text=name,
                font=F_BOLD,
                fg=FG,
                bg=PANEL,
                anchor="w",
            )
            name_lbl.pack(fill="x", padx=8, pady=(6, 0))
            ts_lbl = tk.Label(
                row,
                text=ts,
                font=F_SMALL,
                fg=DIM,
                bg=PANEL,
                anchor="w",
            )
            ts_lbl.pack(fill="x", padx=8, pady=(0, 6))
            # tapping the row or either label opens that file; partial binds
            # the file's path per-row (a plain lambda here would need the
            # same p=p default-arg trick to dodge late-binding in the loop)
            for w in (row, *row.winfo_children()):
                w.bind("<ButtonRelease-1>", functools.partial(self._open, p))
        self.scroll.bind_children()

    def _open(self, path, _e=None):
        if self.scroll.dragged:
            return
        self.app.screens["viewer"].open(path)
        self.app.show("viewer")


class viewer_screen(report_screen):
    def __init__(self, parent, app):
        super().__init__(parent, app, "RESULT")
        self.path = None
        self.add_btn("BACK", self._go_load)
        self.add_btn("DELETE", self._delete, confirm=True)

    def _go_load(self):
        self.app.show("load")

    def open(self, path):
        self.path = Path(path)
        name, ts = storage.describe(path)
        self.title_lbl.config(text=name[:32])
        self.info_lbl.config(text=ts)
        try:
            self.set_sections(storage.load(path), reset=True)
        except (OSError, ValueError) as e:
            self.set_sections([("ERROR", [("File", str(e), nc.FAIL)])], reset=True)

    def _delete(self):
        if self.path:
            storage.delete(self.path)
            self.app.toast(f"Deleted {self.path.name}")

        self.app.show("load")


class system_screen(report_screen):
    def __init__(self, parent, app):
        super().__init__(parent, app, "SYSTEM")
        self.ap_mode = None
        self.add_btn("BACK", self._go_home, font=F_BOLD)
        self.add_btn("SELF TEST", self._go_selftest, font=F_BOLD)
        self.ap_btn = self.add_btn(
            "AP: ...",
            self._toggle_ap,
            confirm=True,
            font=F_BOLD,
        )
        self.add_btn(
            "EXIT",
            self._exit_to_desktop,
            confirm=True,
            font=F_BOLD,
        )
        self.add_btn(
            "REBOOT",
            self._reboot,
            confirm=True,
            font=F_BOLD,
        )
        self.add_btn(
            "POWER OFF",
            self._power_off,
            bg="#5a1f1f",
            confirm=True,
            font=F_BOLD,
        )

    def _go_selftest(self):
        self.app.show("selftest")

    def _exit_to_desktop(self):
        self.app.quit_with(EXIT_DESKTOP)

    def _reboot(self):
        self.app.quit_with(EXIT_REBOOT)

    def _power_off(self):
        self.app.quit_with(EXIT_SHUTDOWN)

    def on_show(self):
        self.info_lbl.config(text="loading...")

        def fn(emit, stop):
            emit(result=nc.system_info())
        self.app.run_task("system", fn)

    def _toggle_ap(self):
        target = "open" if self.ap_mode == "isolated" else "isolate"
        self.info_lbl.config(text="changing AP...")

        def fn(emit, stop):
            ok, msg = nc.set_ap(target)
            emit(toast=("AP forwarding " + ("opened" if target == "open" else "blocked"))
                 if ok else f"AP change failed: {msg}", ok=ok)
            emit(result=nc.system_info())
        self.app.run_task("system", fn)

    def on_msg(self, kw):
        if "toast" in kw:
            self.app.toast(kw["toast"], GREEN if kw.get("ok") else RED)
        if "result" in kw:
            secs, ap = kw["result"]
            self.set_sections(secs)
            self.ap_mode = ap.get("mode")
            self.ap_btn.set_text("OPEN AP" if self.ap_mode == "isolated" else "ISOLATE AP")
            self.info_lbl.config(text=time.strftime("%H:%M:%S"))


class self_test_screen(report_screen):
    INTRO = [("SELF TEST", [
        ("Safe checks", "Run automatically whenever this screen opens: imports, "
                        "project files, required tools, adapter presence, and a live "
                        "call to every read-only netcore function. No side effects.", ""),
        ("Active checks", "Tap ACTIVE TESTS to actually exercise the link: drops the "
                          "cable briefly, sends real gigabit traffic, blinks the port, "
                          "and toggles AP forwarding on and back off.", "")])]

    def __init__(self, parent, app):
        super().__init__(parent, app, "SELF TEST")
        self.add_btn("BACK", self._go_system)
        self.add_btn("SAFE\nCHECKS", self._run_safe, bg=ACCENT)
        self.add_btn("ACTIVE\nTESTS", self._run_active, bg="#5a1f1f", confirm=True)
        self.set_sections(self.INTRO)

    def _go_system(self):
        self.app.show("system")

    def on_show(self):
        if self.app.busy not in ("selftest_safe", "selftest_active"):
            self._run_safe()

    def _run_safe(self):
        if self.app.busy:
            self.app.toast(f"Busy: {self.app.busy}", AMBER)
            return

        self.info_lbl.config(text="running safe checks...")

        def fn(emit, stop):
            emit(sections=preflight.run_safe_checks(), final=True)
        self.app.run_task("selftest_safe", fn)

    def _run_active(self):
        if self.app.busy:
            self.app.toast(f"Busy: {self.app.busy}", AMBER)
            return

        self.info_lbl.config(text="running active checks...")
        self.set_banner("TESTING...")

        def fn(emit, stop):
            secs = preflight.run_active_checks(emit, stop)
            emit(sections=secs, final=True)
        self.app.run_task("selftest_active", fn, exclusive=True)

    def on_msg(self, kw):
        self.set_sections(kw["sections"])
        if kw.get("final"):
            w = nc.worst(kw["sections"])
            self.set_banner(VERDICT[w], w)

    def on_done(self):
        self.info_lbl.config(text=time.strftime("%H:%M:%S"))


# ================================================================ app
class app(tk.Tk):
    TASK_SCREEN = {
        "scan": "scan",
        "cable": "cable",
        "gig": "gig",
        "blink": "blink",
        "system": "system",
        "selftest_safe": "selftest",
        "selftest_active": "selftest",
    }
    LINK_DISRUPTING = {"cable", "blink", "selftest_active"}

    def __init__(self):
        super().__init__()

        # ---- window chrome ----
        self.exit_code = EXIT_CRASH  # anything unexpected ending mainloop = crash
        self.title("Pi Cable Tester")
        self.configure(bg=BG)
        if WINDOWED:
            self.geometry("640x480")
        else:
            self.attributes("-fullscreen", True)
            self.config(cursor="none")

        self.protocol("WM_DELETE_WINDOW", self._ignore_close)

        # ---- shared state ----
        self.q = queue.Queue()
        self.ident = {
            "switch": nc.NA,
            "port": nc.NA,
            "vlan": nc.NA,
            "ip": nc.NA,
            "state": "",
        }
        self.results = {}
        self.busy = None
        self.suppress_until = 0.0
        self.tokens, self.stops = {}, {}
        self.stop_all = threading.Event()
        self._toast_job = None
        self.current = None

        # ---- status bar + screens ----
        self._build_statusbar()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        self.screens = {}
        screen_classes = [
            ("home", home_screen),
            ("scan", scan_screen),
            ("cable", cable_screen),
            ("gig", gig_screen),
            ("blink", blink_screen),
            ("load", load_screen),
            ("viewer", viewer_screen),
            ("system", system_screen),
            ("selftest", self_test_screen),
        ]
        for name, cls in screen_classes:
            s = cls(body, self)
            s.place(
                relx=0,
                rely=0,
                relwidth=1,
                relheight=1,
            )
            self.screens[name] = s
        self.toast_lbl = tk.Label(
            self,
            font=F_BOLD,
            bg=ACCENT,
            fg=FG,
            padx=14,
            pady=8,
            wraplength=560,
        )
        self.show("home")

        # ---- background plumbing: link monitor thread, queue pump, clock ----
        threading.Thread(target=self._monitor, daemon=True).start()
        self.after(100, self._pump)
        self._tick_clock()

    def _ignore_close(self):
        """Window manager's close button does nothing; use SYSTEM > EXIT instead."""
        pass

    # ---------- chrome
    def _build_statusbar(self):
        bar = tk.Frame(self, bg=PANEL, height=34)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        self.sb_link = tk.Label(
            bar,
            text="...",
            font=F_BOLD,
            bg=PANEL,
            fg=DIM,
            padx=8,
        )
        self.sb_link.pack(side="left")
        self.sb_ip = tk.Label(
            bar,
            text="",
            font=F_SMALL,
            bg=PANEL,
            fg=FG,
        )
        self.sb_ip.pack(side="left")
        self.sb_clock = tk.Label(
            bar,
            text="",
            font=F_BOLD,
            bg=PANEL,
            fg=FG,
            padx=8,
        )
        self.sb_clock.pack(side="right")
        self.sb_ap = tk.Label(
            bar,
            text="AP",
            font=F_SMALL,
            bg=PANEL,
            fg=DIM,
            padx=6,
        )
        self.sb_ap.pack(side="right")

    def _update_status(self, s):
        if not s["up"]:
            text, color = "NO LINK", RED
        else:
            sp, dx = s.get("speed"), (s.get("duplex") or "?")
            text = f"{sp or '?'}/{dx.upper()}"
            color = GREEN if sp == 1000 and dx == "full" else AMBER

        self.sb_link.config(text=text, fg=color)
        self.sb_ip.config(text=s.get("ipv4") or ("no IP" if s["up"] else ""))
        ap = s.get("ap")
        self.sb_ap.config(text="AP UP" if ap == "up" else "AP DOWN" if ap else "AP ?",
                          fg=GREEN if ap == "up" else RED if ap else DIM)

    def _tick_clock(self):
        self.sb_clock.config(text=time.strftime("%H:%M"))
        self.after(5000, self._tick_clock)

    def show(self, name):
        self.current = name
        self.screens[name].tkraise()
        self.screens[name].on_show()

    def toast(self, text, color=ACCENT, ms=2800):
        self.toast_lbl.config(text=text, bg=color)
        self.toast_lbl.place(relx=0.5, rely=0.82, anchor="s")
        self.toast_lbl.lift()
        if self._toast_job:
            self.after_cancel(self._toast_job)
        self._toast_job = self.after(ms, self.toast_lbl.place_forget)

    def report_callback_exception(self, exc, val, tb):
        log.error("UI callback error", exc_info=(exc, val, tb))
        try:
            self.toast(f"UI error: {val}", RED)
        except tk.TclError:
            pass

    # ---------- background plumbing
    def _monitor(self):
        """Runs in its own thread: watches carrier state and polls status
        every ~2s, pushing both onto the queue for _pump to hand to the UI.
        """
        last, n = None, 0
        while not self.stop_all.is_set():
            try:
                up = nc.carrier()
                if up != last:
                    self.q.put(("carrier", None, up))
                    last = up
                if n % 4 == 0:
                    self.q.put(("status", None, nc.status_snapshot()))
            except Exception:
                log.exception("monitor")
            n += 1
            self.stop_all.wait(0.5)

    def run_task(self, name, fn, exclusive=False):
        """Runs fn(emit, stop) in a background thread. `name` identifies the
        task so a newer run of the same name invalidates a stale one still
        in flight (see the token check in _handle).
        """
        tok = self.tokens.get(name, 0) + 1
        self.tokens[name] = tok
        if name in self.stops:
            self.stops[name].set()
        stop = threading.Event()
        self.stops[name] = stop
        if exclusive:
            self.busy = name

        def emit(**kw):
            self.q.put((name, tok, kw))

        def worker():
            try:
                fn(emit, stop)
            except Exception as e:
                log.exception("task %s", name)
                self.q.put(("error", None, f"{name}: {e}"))
            finally:
                self.q.put(("done", tok, name))
        threading.Thread(target=worker, daemon=True).start()

    def stop_task(self, name):
        if name in self.stops:
            self.stops[name].set()

    def _pump(self):
        """Drains the queue on the UI thread every 100ms; this is the only
        place background-thread results are allowed to touch widgets.
        """
        try:
            while True:
                msg = self.q.get_nowait()
                try:
                    self._handle(*msg)
                except Exception:
                    log.exception("handling %s", msg[0])
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _handle(self, kind, token, payload):
        # one big if/elif dispatch on message kind - deliberately not split
        # up or blank-line-separated, since the branches are one cohesive
        # decision rather than independent blocks
        if kind == "carrier":
            self._on_carrier(payload)
        elif kind == "status":
            self._update_status(payload)
        elif kind == "error":
            self.toast(f"Error: {payload}", RED, ms=5000)
        elif kind == "done":
            name = payload
            if token != self.tokens.get(name):
                return
            if self.busy == name:
                self.busy = None
                if name in self.LINK_DISRUPTING:
                    self.suppress_until = time.time() + 8
                    self.after(8500, self._rescan_if_idle)
            self.screens[self.TASK_SCREEN[name]].on_done()
        elif kind in self.TASK_SCREEN:
            if token != self.tokens.get(kind):
                return  # stale message from a cancelled run
            if kind == "scan":
                self.results["scan"] = payload["sections"]
                self.ident = payload["ident"]
                self.screens["home"].refresh()
            self.screens[self.TASK_SCREEN[kind]].on_msg(payload)

    def _on_carrier(self, up):
        if self.busy or time.time() < self.suppress_until:
            return
        if up:
            self.results.pop("cable", None)
            self.results.pop("gig", None)
            self.start_scan(fresh=True)
            if self.current == "home":
                self.show("scan")
            self.toast("Cable connected - scanning")
        else:
            self.start_scan(fresh=False)

    def _rescan_if_idle(self):
        if not self.busy and nc.carrier():
            self.start_scan(fresh=False)

    # ---------- commands
    def start_scan(self, fresh):
        def fn(emit, stop):
            def report(secs, ident):
                emit(sections=secs, ident=ident)
            nc.scan(report, stop, fresh=fresh)
        self.run_task("scan", fn)
        self.screens["scan"].info_lbl.config(text="scanning...")

    def cmd_scan(self):
        if self.busy:
            self.toast(f"Busy: {self.busy}", AMBER)
            return

        self.start_scan(fresh=False)
        self.show("scan")

    def cmd_cable(self):
        if self.busy:
            self.toast(f"Busy: {self.busy}", AMBER)
            return

        self.screens["cable"].running()

        def fn(emit, stop):
            li = nc.link_info()
            emit(sections=[nc.link_inference(li), ("CABLE: TDR", [("TDR", "running...", "")])])
            secs = nc.cable_sections(nc.test_cable(), li)
            emit(sections=secs, final=True)
        self.run_task("cable", fn, exclusive=True)

    def cmd_gig(self, duration):
        def fn(emit, stop):
            secs = nc.qualify_gig(duration, emit, stop)
            emit(sections=secs, final=True)
        self.run_task("gig", fn, exclusive=True)

    def cmd_blink(self, mode):
        def fn(emit, stop):
            nc.blink(mode, emit, stop)
        self.run_task("blink", fn, exclusive=True)

    def cmd_save(self):
        if not self.results:
            self.toast("Nothing to save yet - run a scan first", AMBER)
            return

        try:
            p = storage.save(self.ident, self.results)
            self.toast(f"Saved {p.name}", GREEN)
        except OSError as e:
            self.toast(f"Save failed: {e}", RED)

    def quit_with(self, code):
        log.info("quit requested, code %s", code)
        self.exit_code = code
        self.stop_all.set()
        for e in self.stops.values():
            e.set()
        self.destroy()


# ================================================================ main
def setup_logging():
    d = APP_DIR / "logs"
    d.mkdir(exist_ok=True)
    h = logging.handlers.RotatingFileHandler(d / "app.log", maxBytes=1_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def main():
    lock = open("/tmp/pitester.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(EXIT_ALREADY_RUNNING)
    setup_logging()
    log.info("starting")
    code = EXIT_CRASH
    try:
        tester = app()
        tester.mainloop()
        code = tester.exit_code
    except Exception:
        log.exception("fatal")
        code = EXIT_CRASH
    log.info("exiting with %s", code)
    sys.exit(code)


if __name__ == "__main__":
    main()
