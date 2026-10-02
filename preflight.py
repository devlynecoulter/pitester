#!/usr/bin/env python3
"""
preflight.py - self-diagnostic checks for the Pi cable tester.

Two tiers:
  run_safe_checks()   read-only, no side effects, safe to call any time
                       (including automatically whenever the self-test
                       screen opens): imports, project files, required
                       binaries, adapter presence, and a live call to every
                       read-only netcore function.
  run_active_checks() actually exercises the link and AP: drops/renegotiates
                       the port, sends real gigabit traffic, blinks the port
                       LED, and flips AP forwarding on and back off. Real
                       physical side effects - only ever call this from an
                       explicit, confirmed user action, never automatically.

Same conventions as netcore.py: returns "sections":
    [(section_title, [(field, value, status), ...]), ...]

Run directly for a console report: python3 preflight.py [--active]
"""
import argparse
import functools
import importlib
import os
import shutil
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import netcore as nc  # noqa: E402
import storage  # noqa: E402

PASS, FAIL, WARN, NA = nc.PASS, nc.FAIL, nc.WARN, nc.NA
_VERDICT_TEXT = {
    PASS: "PASS",
    WARN: "WARNING",
    FAIL: "FAIL",
    "": "DONE",
}

REQUIRED_MODULES = [
    "tkinter",
    "subprocess",
    "threading",
    "queue",
    "socket",
    "json",
    "csv",
    "re",
    "struct",
    "random",
    "urllib.request",
    "urllib.parse",
    "concurrent.futures",
    "fcntl",
    "logging",
    "logging.handlers",
    "ipaddress",
]

REQUIRED_FILES = [
    "app.py",
    "netcore.py",
    "storage.py",
    "launch.sh",
    "install.sh",
    "pitester-ap",
]

REQUIRED_BINARIES = [
    "ethtool",
    "ip",
    "nmcli",
    "lldpcli",
    "systemctl",
    "iw",
    "ping",
    "sudo",
]


def _discard(**kw):
    """A no-op emit(), for running an emit-shaped function without a live UI."""
    pass


# --------------------------------------------------------------- safe tier
def _check_imports():
    rows = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
            rows.append((name, "importable", PASS))
        except ImportError as e:
            rows.append((name, str(e), FAIL))
    return ("IMPORTS", rows)


def _check_files():
    rows = []
    for name in REQUIRED_FILES:
        path = APP_DIR / name
        if not path.exists():
            rows.append((name, "missing", FAIL))
            continue

        executable_expected = name.endswith(".sh") or name == "pitester-ap"
        if executable_expected:
            ok = os.access(path, os.X_OK)
            rows.append((name, "present, executable" if ok else "present, NOT executable",
                         PASS if ok else FAIL))
        else:
            rows.append((name, "present", PASS))
    return ("PROJECT FILES", rows)


def _check_dirs():
    for_dirs = (("Results dir", storage.RESULTS_DIR), ("Logs dir", APP_DIR / "logs"))
    rows = []
    for label, path in for_dirs:
        try:
            path.mkdir(parents=True, exist_ok=True)
            writable = os.access(path, os.W_OK)
            rows.append((label, str(path), PASS if writable else FAIL))
        except OSError as e:
            rows.append((label, str(e), FAIL))
    return ("DIRECTORIES", rows)


def _check_binaries():
    rows = []
    for name in REQUIRED_BINARIES:
        found = shutil.which(name)
        rows.append((name, found or "not on PATH", PASS if found else FAIL))
    return ("REQUIRED TOOLS", rows)


def _check_adapters(iface):
    rows = []
    try:
        ifaces_present = os.listdir("/sys/class/net")
    except OSError:
        ifaces_present = []

    if iface in ifaces_present:
        rc, out, err = nc.run(["ethtool", iface])
        rows.append((iface, "present, ethtool exit 0" if rc == 0
                     else f"present, ethtool exit {rc}: {err.strip()}", PASS if rc == 0 else WARN))
    else:
        rows.append((iface, "not present in /sys/class/net", FAIL))

    # matched by MAC, not name, since USB wifi adapters don't reliably keep
    # the same wlanN name across reboots (see netcore.ap_iface)
    ap_if = nc.ap_iface()
    if not ap_if:
        rows.append(("AP adapter", "not found (check /etc/pitester/ap.conf)", FAIL))
    else:
        rc, out, err = nc.run([
            "iw",
            "dev",
            ap_if,
            "info",
        ])
        rows.append((f"AP adapter ({ap_if})", "present, iw exit 0" if rc == 0
                     else f"present, iw exit {rc}: {err.strip()}", PASS if rc == 0 else WARN))

    return ("NETWORK ADAPTERS", rows)


def _check_safe_functions(iface):
    """Actually call the read-only netcore functions and confirm they don't raise."""
    checks = [
        ("link_info", functools.partial(nc.link_info, iface)),
        ("ip_info", functools.partial(nc.ip_info, iface)),
        ("ap_status", nc.ap_status),
        ("system_info", nc.system_info),
        ("lldp_neighbors", functools.partial(nc.lldp_neighbors, iface)),
        ("wifi_iface", nc.wifi_iface),
        ("saved_wifi", nc.saved_wifi),
        ("read_adapter", functools.partial(nc.read_adapter, "eth", iface)),
    ]
    rows = []
    for label, fn in checks:
        try:
            fn()
            rows.append((label, "ran OK", PASS))
        except Exception as e:
            rows.append((label, f"raised {type(e).__name__}: {e}", FAIL))
    return ("SAFE FUNCTION CHECKS (read-only)", rows)


def run_safe_checks(iface=None):
    """Read-only diagnostics. No side effects - safe to call any time."""
    iface = iface or nc.IFACE
    return [
        _check_imports(),
        _check_files(),
        _check_dirs(),
        _check_binaries(),
        _check_adapters(iface),
        _check_safe_functions(iface),
    ]


# --------------------------------------------------------------- active tier
def run_active_checks(
    emit,
    stop,
    iface=None,
    gig_seconds=10,
):
    """Exercises the link and AP for real: drops/renegotiates the port, sends
    real gigabit traffic, blinks the port LED, and flips AP forwarding on and
    back off. Only ever call this from an explicit, confirmed user action.
    """
    iface = iface or nc.IFACE
    sections = []

    def push(title, rows):
        sections.append((title, rows))
        emit(sections=list(sections))

    li = nc.link_info(iface)
    for title, rows in nc.cable_sections(nc.test_cable(iface), li):
        push(title, rows)

    if stop.is_set():
        return sections

    for title, rows in nc.qualify_gig(gig_seconds, _discard, stop):
        push(title, rows)

    if stop.is_set():
        return sections

    try:
        nc.blink("traffic", _discard, stop, duration=3)
        push("PORT BLINK", [("Blink", "ran for 3s without error", PASS)])
    except Exception as e:
        push("PORT BLINK", [("Blink", f"raised {type(e).__name__}: {e}", FAIL)])

    if stop.is_set():
        return sections

    ap_before = nc.ap_status()
    mode_before = ap_before.get("mode")
    target = "open" if mode_before == "isolated" else "isolate"
    ok1, msg1 = nc.set_ap(target)
    ok2, msg2 = nc.set_ap("isolate" if target == "open" else "open")
    ap_after = nc.ap_status()
    restored = ap_after.get("mode") == mode_before
    push("AP TOGGLE", [
        ("Toggle to " + target, "ok" if ok1 else msg1, PASS if ok1 else FAIL),
        ("Toggle back", "ok" if ok2 else msg2, PASS if ok2 else FAIL),
        ("Restored original mode", "yes" if restored else "NO - check AP mode manually",
         PASS if restored else FAIL),
    ])

    return sections


# --------------------------------------------------------------- CLI
def _print_sections(sections):
    for title, rows in sections:
        print(f"\n== {title} ==")
        for field, value, status in rows:
            tag = f"[{status}]" if status else ""
            print(f"  {field}: {value} {tag}".rstrip())


def main():
    parser = argparse.ArgumentParser(description="Pi Cable Tester self-diagnostic")
    parser.add_argument("--active", action="store_true",
                         help="also run the active tier (drops the link briefly, sends "
                              "real gigabit traffic, blinks the port, toggles the AP)")
    args = parser.parse_args()

    safe_sections = run_safe_checks()
    _print_sections(safe_sections)
    safe_verdict = nc.worst(safe_sections)
    print(f"\nSAFE CHECKS: {_VERDICT_TEXT.get(safe_verdict, 'DONE')}")

    all_sections = list(safe_sections)
    if args.active:
        print("\nRunning active checks (this will briefly disrupt the link)...")
        active_sections = run_active_checks(_discard, threading.Event())
        _print_sections(active_sections)
        print(f"\nACTIVE CHECKS: {_VERDICT_TEXT.get(nc.worst(active_sections), 'DONE')}")
        all_sections += active_sections

    sys.exit(0 if nc.worst(all_sections) != FAIL else 1)


if __name__ == "__main__":
    main()
