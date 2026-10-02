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

# near-black, dark grey, slate grey
BG, PANEL, PANEL2 = "#0f1419", "#1b232c", "#26323e"
# off-white, light grey, blue
FG, DIM, ACCENT = "#e6edf3", "#8b98a5", "#2f81f7"
# green, red, amber/orange
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


class touch_keyboard(tk.Frame):
    """Full-screen on-screen keyboard, laid over everything (status bar too).

    ask() shows it; OK hands the text to on_ok, CANCEL just hides it.
    mode "ip" is a big number pad for addresses (digits . / ,).
    """

    LETTERS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm-_."]
    SYMBOLS = ["1234567890", "!@#$%^&*()", "-_=+[]{}\\|", ";:'\",.<>/?`~"]
    NUMPAD = ["123", "456", "789", ".0/,"]

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.mode, self.layout, self.shift = "text", "letters", False
        self.text, self.on_ok = "", None
        self.title_lbl = tk.Label(
            self,
            font=F_SMALL,
            fg=DIM,
            bg=BG,
            anchor="w",
        )
        self.title_lbl.pack(fill="x", padx=8, pady=(4, 0))
        self.entry_lbl = tk.Label(
            self,
            font=F_BIG,
            fg=FG,
            bg=PANEL,
            anchor="w",
            padx=8,
        )
        self.entry_lbl.pack(fill="x", padx=6, pady=(0, 4))
        self.keys = tk.Frame(self, bg=BG)
        self.keys.pack(fill="both", expand=True, padx=3, pady=(0, 3))

    def ask(self, title, initial, on_ok, mode="text"):
        self.mode, self.layout, self.shift = mode, "letters", False
        self.text, self.on_ok = initial, on_ok
        self.title_lbl.config(text=title)
        self._draw_keys()
        self._draw_text()
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lift()

    def hide(self):
        self.place_forget()

    def _draw_text(self):
        # only the tail is shown so the cursor end of a long password stays visible
        shown = self.text if len(self.text) <= 30 else "..." + self.text[-29:]
        self.entry_lbl.config(text=shown + "_")

    def _draw_keys(self):
        for c in self.keys.winfo_children():
            c.destroy()
        if self.mode == "ip":
            rows = self.NUMPAD
        elif self.layout == "symbols":
            rows = self.SYMBOLS
        else:
            rows = [r.upper() for r in self.LETTERS] if self.shift else self.LETTERS
        for chars in rows:
            row = tk.Frame(self.keys, bg=BG)
            row.pack(fill="both", expand=True)
            for ch in chars:
                key_btn = mkbtn(row, ch, functools.partial(self._type, ch))
                key_btn.pack(side="left", fill="both", expand=True, padx=2, pady=2)

        bar = tk.Frame(self.keys, bg=BG)
        bar.pack(fill="both", expand=True)
        actions = [("CANCEL", self.hide, PANEL2)]
        if self.mode != "ip":
            actions += [
                ("SHIFT", self._toggle_shift, ACCENT if self.shift else PANEL2),
                ("abc" if self.layout == "symbols" else "#+=", self._toggle_symbols, PANEL2),
                ("SPACE", self._space, PANEL2),
            ]
        actions += [("DEL", self._delete, PANEL2), ("OK", self._ok, GREEN)]
        for text, cmd, bg in actions:
            action_btn = mkbtn(bar, text, cmd, bg=bg, font=F_BOLD)
            action_btn.pack(side="left", fill="both", expand=True, padx=2, pady=2)

    def _type(self, ch):
        self.text += ch
        self._draw_text()

    def _space(self):
        self._type(" ")

    def _delete(self):
        self.text = self.text[:-1]
        self._draw_text()

    def _toggle_shift(self):
        self.shift = not self.shift
        self.layout = "letters"
        self._draw_keys()

    def _toggle_symbols(self):
        self.layout = "letters" if self.layout == "symbols" else "symbols"
        self._draw_keys()

    def _ok(self):
        self.hide()
        if self.on_ok:
            self.on_ok(self.text)


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

    def _go_back(self):
        """Shared BACK target: returns to the menu this screen lives under."""
        self.app.show(self.app.PARENT.get(self.app.current, "home"))


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
        self._last_sections = None

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
        # during a live scan, most 1s ticks arrive with identical content
        # (still "waiting for DHCP...", still "listening..."); rebuilding
        # every widget in the scroll area for no visual change is what was
        # causing the flicker - skip the rebuild when nothing changed
        if not reset and sections == self._last_sections:
            return
        self._last_sections = sections
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


class menu_screen(screen):
    """Full-screen grid of big buttons under the header bar.

    `cells` is row-major; each entry is one of:
      (text, cmd)   a normal button
      (text, None)  a feature that isn't built yet: greyed out, tapping it
                    just shows a "coming soon" toast
      None          an empty cell (keeps the grid evenly sized)
    BACK is supplied by the caller so the top-level menu can leave it out.
    """

    def __init__(self, parent, app, cells, cols):
        super().__init__(parent, app)
        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="both", expand=True, padx=4, pady=4)
        for i, cell in enumerate(cells):
            r, c = divmod(i, cols)
            if cell is None:
                w = tk.Frame(grid, bg=BG)
            elif cell[1] is None:
                w = mkbtn(grid, cell[0] + "\n(soon)", functools.partial(self._soon, cell[0]),
                          bg=PANEL)
                w.config(fg=DIM, activebackground=PANEL, activeforeground=DIM)
            else:
                w = mkbtn(grid, cell[0], cell[1])
            w.grid(row=r, column=c, sticky="nsew", padx=3, pady=3)
        rows = (len(cells) + cols - 1) // cols
        for c in range(cols):
            grid.columnconfigure(c, weight=1, uniform="c")
        for r in range(rows):
            grid.rowconfigure(r, weight=1, uniform="r")

    def _soon(self, text):
        self.app.toast(f"{text.replace(chr(10), ' ')}: coming soon", AMBER)


def build_menus(parent, app):
    """The main menu and its four submenus, laid out to match the sketch:
    each submenu sits in the same corner as its button on the main menu.
    """
    def go(name):
        return functools.partial(app.show, name)

    back = ("BACK", go("home"))
    return {
        "home": menu_screen(parent, app, [
            ("CONNECTIONS", go("connections")),
            ("TESTS", go("tests")),
            ("SCANS", go("scans")),
            ("LOGS", go("logs")),
        ], cols=2),
        "connections": menu_screen(parent, app, [
            ("WI-FI\nCONFIG", go("wifi")),
            ("ETH\nCONFIG", go("ethcfg")),
            None,
            None,
            None,
            back,
        ], cols=3),
        "tests": menu_screen(parent, app, [
            ("BLINK\nPORT", go("blink")),
            ("SPEED\nTEST", go("gig")),
            ("RESET\nCONNECTION", None),
            ("CABLE\nTEST", go("cable")),
            None,
            back,
        ], cols=3),
        "scans": menu_screen(parent, app, [
            ("LLDP /\nSSDP SCAN", app.cmd_scan),
            ("NETWORK\nSCAN", None),
            ("VLAN /\nSUBNET", None),
            None,
            ("NETWORK\nMAP", None),
            back,
        ], cols=3),
        "logs": menu_screen(parent, app, [
            ("SAVE\nCURRENT", app.cmd_save),
            ("EXPORT\nTO USB", None),
            ("LOAD\nLOG", go("load")),
            back,
        ], cols=2),
    }


class scan_screen(report_screen):
    def __init__(self, parent, app):
        super().__init__(parent, app, "NETWORK SCAN")
        self.add_btn("BACK", self._go_back)
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
        self.add_btn("BACK", self._go_back)
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
        self.add_btn("BACK", self._go_back)
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
        self.add_btn("BACK", self._go_back)
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


class wifi_screen(report_screen):
    """Networks seen by the built-in radio; tap one to join it. A secured
    network asks for its password unless it's already saved, or the last
    try with the saved one failed (then it asks again)."""

    def __init__(self, parent, app):
        super().__init__(parent, app, "WI-FI")
        self.iface, self.saved, self.retry = None, set(), set()
        self.add_btn("BACK", self._go_back)
        self.add_btn("RESCAN", self._rescan, bg=ACCENT)
        self.add_btn("DISCONNECT", self._disconnect, confirm=True, font=F_BOLD)
        self.add_btn("SETTINGS", self._go_settings, font=F_BOLD)
        self.set_sections([("WI-FI", [("Status", "scanning...", "")])])

    def on_show(self):
        self._rescan()

    def _scan_and_emit(self, emit):
        iface = nc.wifi_iface()
        nets, err = nc.wifi_scan(iface) if iface else (None, "no built-in Wi-Fi adapter found")
        emit(iface=iface, nets=nets or [], err=err, saved=nc.saved_wifi())

    def _rescan(self):
        self.info_lbl.config(text="scanning...")

        def fn(emit, stop):
            self._scan_and_emit(emit)
        self.app.run_task("wifi", fn)

    def _tap(self, net, _e=None):
        if self.scroll.dragged:
            return
        ssid = net["ssid"]
        if net["security"] and (ssid not in self.saved or ssid in self.retry):
            self.app.keyboard.ask(f"Password for {ssid}", "", functools.partial(self._connect, ssid))
        else:
            self._connect(ssid, None)

    def _connect(self, ssid, password):
        if not self.iface:
            return
        iface = self.iface
        self.info_lbl.config(text=f"connecting to {ssid}...")

        def fn(emit, stop):
            ok, msg = nc.wifi_connect(iface, ssid, password)
            emit(toast=f"{ssid}: {msg}", ok=ok, ssid=ssid)
            self._scan_and_emit(emit)
        self.app.run_task("wifi", fn)

    def _disconnect(self):
        if not self.iface:
            return
        iface = self.iface
        self.info_lbl.config(text="disconnecting...")

        def fn(emit, stop):
            ok, msg = nc.wifi_disconnect(iface)
            emit(toast=msg, ok=ok)
            self._scan_and_emit(emit)
        self.app.run_task("wifi", fn)

    def _go_settings(self):
        self.app.show("wificfg")

    def on_msg(self, kw):
        if "toast" in kw:
            self.app.toast(kw["toast"], GREEN if kw["ok"] else RED, ms=4000)
            if "ssid" in kw:
                if kw["ok"]:
                    self.retry.discard(kw["ssid"])
                else:
                    self.retry.add(kw["ssid"])
        if "nets" in kw:
            self.iface, self.saved = kw["iface"], kw["saved"]
            self.info_lbl.config(text=f"{self.iface or 'no adapter'}  {time.strftime('%H:%M:%S')}")
            self._draw(kw["nets"], kw["err"])

    def _draw(self, nets, err):
        self.scroll.clear()
        self._last_sections = None
        if err:
            err_lbl = tk.Label(
                self.scroll.inner,
                text=err,
                font=F_BODY,
                fg=RED,
                bg=BG,
                wraplength=600,
            )
            err_lbl.pack(pady=30)
        elif not nets:
            empty_lbl = tk.Label(
                self.scroll.inner,
                text="No networks found - tap RESCAN",
                font=F_BODY,
                fg=DIM,
                bg=BG,
            )
            empty_lbl.pack(pady=40)

        for n in nets:
            row = tk.Frame(self.scroll.inner, bg=PANEL)
            row.pack(fill="x", padx=6, pady=3)
            name_lbl = tk.Label(
                row,
                text=n["ssid"] + ("   CONNECTED" if n["in_use"] else ""),
                font=F_BOLD,
                fg=GREEN if n["in_use"] else FG,
                bg=PANEL,
                anchor="w",
            )
            name_lbl.pack(fill="x", padx=8, pady=(6, 0))
            detail = f"{n['signal']}%   ch {n['chan']}   {n['security'] or 'open'}"
            if n["ssid"] in self.saved:
                detail += "   saved"
            detail_lbl = tk.Label(
                row,
                text=detail,
                font=F_SMALL,
                fg=DIM,
                bg=PANEL,
                anchor="w",
            )
            detail_lbl.pack(fill="x", padx=8, pady=(0, 6))
            for w in (row, *row.winfo_children()):
                w.bind("<ButtonRelease-1>", functools.partial(self._tap, n))
        self.scroll.bind_children()


class adapter_screen(report_screen):
    """The most-used adapter settings, as tap-to-change rows: IPv4 mode,
    static address / gateway / DNS, link speed (Ethernet only) and MTU.
    kind "eth" edits eth0's pitester-eth profile; kind "wifi" edits the
    network the built-in radio is on. Nothing changes until APPLY; DEFAULTS
    only refills the form (DHCP, auto link, auto MTU) for you to APPLY.
    """

    LINKS = ["auto", "1000/full", "100/full", "100/half", "10/full", "10/half"]
    MTUS = ["auto", "1500", "1492", "1400", "9000"]
    STATIC_FIELDS = ("address", "gateway", "dns")

    def __init__(self, parent, app, name, kind, title):
        super().__init__(parent, app, title)
        self.name, self.kind = name, kind
        self.iface, self.con = None, None
        self.s = dict(nc.ADAPTER_DEFAULTS)
        self.add_btn("BACK", self._go_back)
        self.add_btn("DEFAULTS", self._defaults)
        self.apply_btn = self.add_btn("APPLY", self._apply, bg=ACCENT)

    def on_show(self):
        self.info_lbl.config(text="reading...")
        self.set_banner("")
        kind = self.kind

        def fn(emit, stop):
            iface = nc.IFACE if kind == "eth" else nc.wifi_iface()
            con, s = nc.read_adapter(kind, iface) if iface else (None, dict(nc.ADAPTER_DEFAULTS))
            emit(iface=iface, con=con, s=s)
        self.app.run_task(self.name, fn)

    def _rows(self):
        rows = [("IPv4", "method"), ("Address", "address"), ("Gateway", "gateway"), ("DNS", "dns")]
        if self.kind == "eth":
            rows.append(("Link", "link"))
        rows.append(("MTU", "mtu"))
        return rows

    def _value_text(self, key):
        v = self.s[key]
        if key == "method":
            return {"auto": "DHCP", "manual": "STATIC"}.get(v, v.upper())
        if key in self.STATIC_FIELDS:
            return v or "(none)"
        return v.replace("/", " ").upper()

    def _draw(self):
        self.scroll.clear()
        self._last_sections = None
        if not self.con:
            msg = "No adapter found" if not self.iface else "Not connected - join a network first"
            none_lbl = tk.Label(self.scroll.inner, text=msg, font=F_BODY, fg=DIM, bg=BG)
            none_lbl.pack(pady=40)
            self.apply_btn.config(state="disabled")
            return

        self.apply_btn.config(state="normal")
        for label, key in self._rows():
            locked = key in self.STATIC_FIELDS and self.s["method"] != "manual"
            row = tk.Frame(self.scroll.inner, bg=PANEL)
            row.pack(fill="x", padx=6, pady=3)
            field_lbl = tk.Label(
                row,
                text=label,
                font=F_BODY,
                fg=DIM,
                bg=PANEL,
                anchor="w",
                width=10,
            )
            field_lbl.pack(side="left", padx=8, pady=6)
            value_lbl = tk.Label(
                row,
                text="(from DHCP)" if locked else self._value_text(key),
                font=F_BOLD,
                fg=DIM if locked else FG,
                bg=PANEL,
                anchor="w",
            )
            value_lbl.pack(side="left", fill="x", expand=True)
            if not locked:
                for w in (row, field_lbl, value_lbl):
                    w.bind("<ButtonRelease-1>", functools.partial(self._edit, key))
        self.scroll.bind_children()

    def _edit(self, key, _e=None):
        if self.scroll.dragged:
            return
        if key == "method":
            self.s["method"] = "manual" if self.s["method"] == "auto" else "auto"
        elif key == "link":
            self.s["link"] = self.LINKS[(self.LINKS.index(self.s["link"]) + 1) % len(self.LINKS)] \
                if self.s["link"] in self.LINKS else "auto"
        elif key == "mtu":
            self.s["mtu"] = self.MTUS[(self.MTUS.index(self.s["mtu"]) + 1) % len(self.MTUS)] \
                if self.s["mtu"] in self.MTUS else "auto"
        else:
            title = {"address": "Address (e.g. 192.168.1.50/24)", "gateway": "Gateway",
                     "dns": "DNS servers (comma between them)"}[key]
            self.app.keyboard.ask(title, self.s[key], functools.partial(self._set_text, key), mode="ip")
            return
        self._changed()

    def _set_text(self, key, text):
        value, err = nc.clean_ipv4(key, text)
        if err:
            self.app.toast(f"Not a valid address: {err}", RED, ms=4000)
            return
        self.s[key] = value
        self._changed()

    def _changed(self):
        self.set_banner("NOT APPLIED YET", nc.WARN)
        self._draw()

    def _defaults(self):
        self.s = dict(nc.ADAPTER_DEFAULTS)
        self._changed()

    def _apply(self):
        if not self.con:
            return
        self.info_lbl.config(text="applying...")
        self.apply_btn.config(state="disabled")
        kind, iface, s = self.kind, self.iface, dict(self.s)

        def fn(emit, stop):
            ok, msg = nc.apply_adapter(kind, iface, s)
            emit(toast=msg, ok=ok)
            if ok:  # on failure keep the form as typed so it can be fixed
                con, s2 = nc.read_adapter(kind, iface)
                emit(iface=iface, con=con, s=s2)
        self.app.run_task(self.name, fn)

    def on_msg(self, kw):
        if "toast" in kw:
            self.app.toast(kw["toast"], GREEN if kw["ok"] else RED, ms=4000)
            if kw["ok"]:
                self.set_banner("")
                if self.kind == "eth":
                    self.app.after(4000, self.app._rescan_if_idle)
        if "s" in kw:
            self.iface, self.con, self.s = kw["iface"], kw["con"], kw["s"]
            self.info_lbl.config(text=f"{self.iface or '?'}  {self.con or ''}")
            self._draw()

    def on_done(self):
        if self.con:
            self.apply_btn.config(state="normal")


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
        back_btn = mkbtn(bar, "BACK", self._go_back)
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
        self.add_btn("BACK", self._go_back, font=F_BOLD)
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
            bg="#5a1f1f",  # dark red / maroon
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
        self.add_btn("ACTIVE\nTESTS", self._run_active, bg="#5a1f1f", confirm=True)  # dark red / maroon
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
        "wifi": "wifi",
        "ethcfg": "ethcfg",
        "wificfg": "wificfg",
    }
    LINK_DISRUPTING = {"cable", "blink", "selftest_active"}
    # where BACK goes from each screen; anything not listed goes to home
    PARENT = {
        "scan": "scans",
        "cable": "tests",
        "gig": "tests",
        "blink": "tests",
        "load": "logs",
        "viewer": "load",
        "selftest": "system",
        "wifi": "connections",
        "ethcfg": "connections",
        "wificfg": "wifi",
    }

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
        self.screens = build_menus(body, self)
        for s in self.screens.values():
            s.place(
                relx=0,
                rely=0,
                relwidth=1,
                relheight=1,
            )
        screen_classes = [
            ("scan", scan_screen),
            ("cable", cable_screen),
            ("gig", gig_screen),
            ("blink", blink_screen),
            ("load", load_screen),
            ("viewer", viewer_screen),
            ("system", system_screen),
            ("selftest", self_test_screen),
            ("wifi", wifi_screen),
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
        adapter_screens = [
            ("ethcfg", "eth", "ETHERNET SETTINGS"),
            ("wificfg", "wifi", "WI-FI SETTINGS"),
        ]
        for name, kind, title in adapter_screens:
            s = adapter_screen(body, self, name, kind, title)
            s.place(
                relx=0,
                rely=0,
                relwidth=1,
                relheight=1,
            )
            self.screens[name] = s
        self.keyboard = touch_keyboard(self)
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

        # ---- background plumbing: link monitor thread, queue pump ----
        threading.Thread(target=self._monitor, daemon=True).start()
        self.after(100, self._pump)

    def _ignore_close(self):
        """Window manager's close button does nothing; use SYSTEM > EXIT instead."""
        pass

    # ---------- chrome
    def _build_statusbar(self):
        """Header on every screen: the current connection (link, IP, and the
        switch/port/VLAN from the last scan) on the left, gear -> SYSTEM on
        the right.
        """
        bar = tk.Frame(self, bg=PANEL, height=44)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        gear_btn = mkbtn(
            bar,
            "\u2699",
            self._go_system,
            bg=PANEL,
            font=("DejaVu Sans", 22),
        )
        gear_btn.config(width=2)
        gear_btn.pack(side="right", fill="y")
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
        self.sb_conn = tk.Label(
            bar,
            text="",
            font=F_SMALL,
            bg=PANEL,
            fg=DIM,
            padx=8,
            anchor="w",
        )
        self.sb_ap = tk.Label(
            bar,
            text="AP",
            font=F_SMALL,
            bg=PANEL,
            fg=DIM,
            padx=6,
        )
        self.sb_ap.pack(side="right")
        # packed last so it only takes what the fixed-width labels leave over
        self.sb_conn.pack(side="left", fill="x", expand=True)

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

    def _update_ident(self):
        """Switch / port / VLAN from the last scan, shown in the header."""
        ident = self.ident
        parts = [
            ident.get("switch"),
            ident.get("port"),
            f"VLAN {ident['vlan']}" if ident.get("vlan") not in (None, nc.NA) else None,
        ]
        t = "  ".join(str(p) for p in parts if p and p != nc.NA)
        self.sb_conn.config(text=t if len(t) <= 34 else t[:33] + "...")

    def _go_system(self):
        self.show("system")

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
                self._update_ident()
            self.screens[self.TASK_SCREEN[kind]].on_msg(payload)

    def _on_carrier(self, up):
        if self.busy or time.time() < self.suppress_until:
            return
        if up:
            self.results.pop("cable", None)
            self.results.pop("gig", None)
            self.start_scan(fresh=True)
            if self.current in ("home", "scans"):
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
