# Pi Cable Tester

Files (copy the whole folder to /home/<you>/pitester):
- app.py        Tk fullscreen touch UI (640x480)
- netcore.py    all network logic (no Tk - reused by the future web version)
- storage.py    CSV save/load -> ~/scan_results/SWITCH_PORT_yymmddhhmmss.csv
- preflight.py  self-diagnostic checks (see "Self test" below)
- launch.sh     starts the UI; exit 0 = power off, 10 = reboot, other = crash -> reboot
- pitester-ap   hotspot isolate/open/status helper (installed to /usr/local/sbin)
- install.sh    setup and updates (see "install.sh modes" below)

Install:
    cd ~/pitester
    sed -i 's/\r$//' install.sh      # only needed if WinSCP added Windows line endings
    sudo bash install.sh --force     # first install
    sudo reboot

install.sh modes:
    sudo bash install.sh            check only: reports what's installed /
                                    missing, changes nothing
    sudo bash install.sh --update   fixes only what's missing or out of date;
                                    an existing hotspot is left alone (use
                                    this after pulling changes)
    sudo bash install.sh --force    fresh install: redoes every step and
                                    rebuilds the hotspot (password resets to
                                    the default unless AP_PASS is given)

Hotspot: SSID "scanner", Pi at 192.168.4.1 (SSH/SFTP).
Password defaults to the Pi's eth0 MAC, lowercase, no colons (e.g. dca632a1b2c3).
It is shown on the System screen and printed at the end of install.
Custom password:  sudo AP_PASS=yourpassword bash install.sh --update
Built-in Wi-Fi client connection is left untouched.

Maintenance: `touch ~/pitester/MAINTENANCE` -> exiting the app (e.g. POWER OFF,
or `pkill -f app.py`) drops to the desktop instead of powering off/rebooting.
Delete the file to go back to normal.

Crash-loop guard: 3 crashes within 10 minutes -> launcher stops rebooting.

Logs: ~/pitester/logs/ (app.log, launcher.log, stdout.log)
Dev run in a window: PITESTER_WINDOWED=1 python3 app.py

Self test: two tiers, both defined in preflight.py.
- Safe checks (no side effects): confirms every stdlib import used by app.py/
  netcore.py/storage.py, that the project files are present and the shell
  scripts executable, that ethtool/ip/nmcli/lldpcli/systemctl/iw/ping/sudo
  are on PATH, that eth0 and the AP adapter show up and respond, and that
  every read-only netcore function actually runs without raising. Runs
  automatically whenever the SYSTEM > SELF TEST screen opens, and from the
  command line: `python3 preflight.py`.
- Active checks (real side effects - only runs when you tap it): drops the
  cable briefly for a TDR test, sends ~10s of real gigabit traffic, blinks
  the port LED for a few seconds, and flips AP forwarding on and back off.
  Triggered from the SELF TEST screen's ACTIVE TESTS button (tap-to-arm,
  tap again to confirm - same pattern as REBOOT/POWER OFF), or from the
  command line: `python3 preflight.py --active`.

Unit tests for the pure parsing/decision functions (link_inference,
parse_ping, scan_sections, etc.) are a separate, external thing - not part
of what runs on the device itself.
