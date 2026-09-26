"""storage.py - save/load results as CSV in ~/scan_results."""
import csv
import os
import re
import socket
import time
from pathlib import Path

RESULTS_DIR = Path(os.environ.get("PITESTER_RESULTS", str(Path.home() / "scan_results")))
ORDER = ["scan", "cable", "gig"]  # fixed section order within a saved file, regardless
                                  # of the order the tests happened to run in


def _sanitize(s):
    # filenames end up like {switch}_{port}_{timestamp}.csv - strip anything
    # that isn't filesystem-safe out of the switch/port names first
    s = re.sub(r"[^A-Za-z0-9.-]+", "-", str(s or "NA")).strip("-.")
    return (s or "NA")[:40]


def save(ident, results):
    """ident: {'switch','port'}; results: {'scan'|'cable'|'gig': sections}."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d%H%M%S")
    sw, port = ident.get("switch") or "NA", ident.get("port") or "NA"
    path = RESULTS_DIR / f"{_sanitize(sw)}_{_sanitize(port)}_{ts}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "section",
            "field",
            "value",
            "status",
        ])
        for k, v in (("Timestamp", ts), ("Switch", sw), ("Port", port),
                     ("Tester", socket.gethostname())):
            w.writerow([
                "SAVED RESULT",
                k,
                v,
                "",
            ])
        for key in ORDER:
            for title, rows in results.get(key) or []:
                for field, value, status in rows:
                    w.writerow([
                        title,
                        field,
                        value,
                        status,
                    ])
    return path


def _mtime(path):
    return path.stat().st_mtime


def list_results():
    if not RESULTS_DIR.is_dir():
        return []
    return sorted(RESULTS_DIR.glob("*.csv"), key=_mtime, reverse=True)


def load(path):
    # flatten-then-regroup: the CSV is flat rows, but the UI wants them back
    # as sections; idx remembers which section index each title first
    # appeared at, so rows land in the same section even if they're not
    # contiguous in the file
    sections, idx = [], {}
    with open(path, newline="") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 3:
                continue
            sec, field, value = row[:3]
            status = row[3] if len(row) > 3 else ""
            if sec not in idx:
                idx[sec] = len(sections)
                sections.append((sec, []))

            sections[idx[sec]][1].append((field, value, status))
    return sections


def delete(path):
    Path(path).unlink(missing_ok=True)


def describe(path):
    """('switch / port', 'YYYY-MM-DD HH:MM:SS') from a result filename."""
    parts = Path(path).stem.split("_")
    ts = parts[-1]
    name = " / ".join(parts[:-1]) or Path(path).stem
    if len(ts) == 12 and ts.isdigit():  # yymmddhhmmss -> readable date/time
        ts = f"20{ts[0:2]}-{ts[2:4]}-{ts[4:6]} {ts[6:8]}:{ts[8:10]}:{ts[10:12]}"
    return name, ts
