#!/bin/bash
# Pi Cable Tester installer. From the pitester folder:
#   sudo bash install.sh            check: report what's installed / missing, change nothing
#   sudo bash install.sh --update   fix only what's missing or out of date (hotspot left alone)
#   sudo bash install.sh --force    fresh install: redo every step, rebuild the hotspot
# Does NOT touch your existing Wi-Fi client connection on the built-in adapter.
set -euo pipefail

CHECK=1; FORCE=0
case "${1:-}" in
    "") ;;
    --update) CHECK=0 ;;
    --force) CHECK=0; FORCE=1 ;;
    *) echo "usage: sudo bash install.sh [--update | --force]"; exit 1 ;;
esac

AP_SSID="scanner"
# Hotspot password: defaults to this Pi's eth0 MAC, lowercase, no colons
# (e.g. dca632a1b2c3). Override with:  sudo AP_PASS=something bash install.sh --update
# (with --update, AP_PASS changes the existing hotspot's password in place)
AP_PASS_GIVEN="${AP_PASS:+1}"
AP_PASS="${AP_PASS:-$(tr -d ':' < /sys/class/net/eth0/address)}"
AP_ADDR="192.168.4.1/24"
AP_CON="scanner-ap"

# ANSI colours (auto-off when output isn't a terminal, or with NO_COLOR=1)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\e[1m'; D=$'\e[2m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; N=$'\e[0m'
else
    B=""; D=""; R=""; G=""; Y=""; C=""; N=""
fi
say()  { echo -e "\n${B}${C}==>${N} ${B}$*${N}"; }
ok()   { echo "   ${G}OK${N}  $*"; }
warn() { echo "   ${Y}!!  $*${N}"; }
info() { echo "   ${D}$*${N}"; }
die()  { echo -e "\n${R}${B}XX  $*${N}"; exit 1; }
cmd()  { echo "     ${C}$*${N}"; }
# each step: check, then fine "..." or need "...", then `if redo; then <fix>; fi`.
# redo is true when the check failed (--update) or always (--force), never
# in check mode
TODO=0; STALE=0
fine() { STALE=0; if [ "$FORCE" = 1 ]; then ok "$* (redoing anyway, --force)"; else ok "$*"; fi; }
need() {
    STALE=1; TODO=$((TODO + 1))
    if [ "$CHECK" = 1 ]; then warn "$* - needs fixing"; else warn "$* - fixing"; fi
}
redo() { [ "$CHECK" = 0 ] && { [ "$STALE" = 1 ] || [ "$FORCE" = 1 ]; }; }
fixing() { [ "$CHECK" = 0 ]; }

if [ "$EUID" -ne 0 ]; then die "Run with sudo:  sudo bash install.sh"; fi
if [ ${#AP_PASS} -lt 8 ]; then die "Hotspot password must be at least 8 characters"; fi
USER_NAME="${SUDO_USER:-}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
    die "Run via sudo from your normal login user, not as root."
fi
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$CHECK" = 1 ]; then
    echo "${B}Check only - nothing will be changed.${N} ${D}(--update to fix, --force for a fresh install)${N}"
elif [ "$FORCE" = 1 ]; then
    echo "${B}Fresh install (--force): every step is redone, the hotspot is rebuilt.${N}"
fi

if fixing; then
    say "Normalising line endings / permissions"
    sed -i 's/\r$//' "$APP_DIR"/*.py "$APP_DIR"/*.sh "$APP_DIR"/pitester-ap
    chmod +x "$APP_DIR/launch.sh" "$APP_DIR/app.py" "$APP_DIR/pitester-ap"
fi

say "Packages"
PKGS=(lldpd ethtool ieee-data python3-tk iw nftables iputils-ping dnsmasq-base arp-scan)
MISSING=()
for p in "${PKGS[@]}"; do
    if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
        ok "$p"
    else
        MISSING+=("$p")
    fi
done
if [ ${#MISSING[@]} -gt 0 ]; then need "not installed: ${MISSING[*]}"; else fine "all packages installed"; fi
if redo; then
    # apt can't install offline, and a failed install would stop this script
    # halfway (set -e) - so check the internet first and stop cleanly instead.
    # deb.debian.org is where Pi OS gets its packages; pinging it by name
    # tests DNS as well as the connection
    if ! ping -c 2 -W 3 deb.debian.org >/dev/null 2>&1; then
        die "No internet (can't ping deb.debian.org) - needed to install: ${MISSING[*]:-${PKGS[*]}}
    Connect eth0 or the built-in Wi-Fi to a network with internet, then run this again.
    No system changes made yet."
    fi
    apt-get update
    if [ "$FORCE" = 1 ]; then INSTALL=("${PKGS[@]}"); else INSTALL=("${MISSING[@]}"); fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${INSTALL[@]}"
fi

say "lldpd (LLDP + CDP, eth0 only)"
LLDP_CONF="configure system interface pattern eth0"
LLDP_CHANGED=0
if [ "$(cat /etc/lldpd.d/pitester.conf 2>/dev/null)" = "$LLDP_CONF" ]; then
    fine "listens on eth0 only"
else
    need "eth0-only config"
fi
if redo; then
    LLDP_CHANGED=1
    mkdir -p /etc/lldpd.d
    echo "$LLDP_CONF" > /etc/lldpd.d/pitester.conf
fi
if grep -qx 'DAEMON_ARGS="-c"' /etc/default/lldpd 2>/dev/null; then
    fine "CDP enabled"
else
    need "CDP (DAEMON_ARGS=\"-c\")"
fi
if redo; then
    LLDP_CHANGED=1
    if grep -q '^DAEMON_ARGS=' /etc/default/lldpd 2>/dev/null; then
        sed -i 's/^DAEMON_ARGS=.*/DAEMON_ARGS="-c"/' /etc/default/lldpd
    else
        echo 'DAEMON_ARGS="-c"' >> /etc/default/lldpd
    fi
fi
if ! getent group _lldpd >/dev/null; then
    info "no _lldpd group (lldpcli falls back to sudo)"
else
    if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx _lldpd; then
        fine "$USER_NAME is in _lldpd"
    else
        need "$USER_NAME not in the _lldpd group"
    fi
    if redo; then usermod -aG _lldpd "$USER_NAME"; fi
fi
if systemctl is-enabled --quiet lldpd 2>/dev/null; then
    fine "starts on boot"
else
    need "lldpd not enabled on boot"
fi
if redo; then systemctl enable lldpd; fi
if systemctl is-active --quiet lldpd; then fine "running"; else need "lldpd not running"; fi
# a config change only takes effect on restart
if redo || { fixing && [ "$LLDP_CHANGED" = 1 ]; }; then systemctl restart lldpd; fi

say "Hotspot '$AP_SSID'"
if nmcli -t -f NAME connection show | grep -qx "$AP_CON" && [ -f /etc/pitester/ap.conf ]; then
    fine "already set up"
else
    need "hotspot not set up"
fi
if redo; then
    if [ "$FORCE" = 1 ]; then warn "rebuilding: anyone on the hotspot (SSH included) drops off"; fi
    AP_IF=""; DRV=""
    for d in /sys/class/net/*; do
        [ -e "$d/phy80211" ] || continue
        DRV="$(basename "$(readlink -f "$d/device/driver")")"
        if [ "$DRV" != "brcmfmac" ]; then AP_IF="$(basename "$d")"; break; fi
    done
    if [ -z "$AP_IF" ]; then die "USB Wi-Fi adapter not found - is the PAU06 plugged in?"; fi
    AP_MAC="$(cat "/sys/class/net/$AP_IF/address")"
    PHY="$(basename "$(readlink -f "/sys/class/net/$AP_IF/phy80211")")"
    ok "$AP_IF  mac $AP_MAC  driver $DRV  ($PHY)"
    if ! iw phy "$PHY" info | grep -qE '^\s+\* AP$'; then
        warn "this adapter doesn't list AP mode - the hotspot may not start"
    fi

    mkdir -p /etc/pitester
    printf 'AP_MAC="%s"\nAP_CON="%s"\n' "$AP_MAC" "$AP_CON" > /etc/pitester/ap.conf
    chmod 644 /etc/pitester/ap.conf

    nmcli connection delete "$AP_CON" >/dev/null 2>&1 || true
    nmcli connection add type wifi ifname '*' con-name "$AP_CON" autoconnect yes \
        connection.autoconnect-priority 100 \
        802-11-wireless.mode ap 802-11-wireless.ssid "$AP_SSID" \
        802-11-wireless.band bg 802-11-wireless.channel 6 \
        802-11-wireless.mac-address "$AP_MAC" 802-11-wireless.powersave 2 \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PASS" \
        wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
        ipv4.method shared ipv4.addresses "$AP_ADDR" ipv6.method disabled
    if nmcli connection up "$AP_CON" >/dev/null; then ok "hotspot up"; else warn "hotspot didn't start now - it will retry on next boot"; fi
elif fixing && [ -n "$AP_PASS_GIVEN" ]; then
    warn "changing the password: anyone on the hotspot (SSH included) drops off"
    nmcli connection modify "$AP_CON" wifi-sec.psk "$AP_PASS"
    if nmcli connection up "$AP_CON" >/dev/null; then ok "new password active"; else warn "saved - takes effect when the hotspot next starts"; fi
fi

say "AP isolation helper + boot service"
if cmp -s "$APP_DIR/pitester-ap" /usr/local/sbin/pitester-ap; then
    fine "helper up to date"
else
    need "helper missing or out of date"
fi
if redo; then install -m 755 "$APP_DIR/pitester-ap" /usr/local/sbin/pitester-ap; fi
UNIT=/etc/systemd/system/pitester-ap-isolate.service
UNIT_TEXT='[Unit]
Description=Pi tester: block forwarding through the Wi-Fi hotspot
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/pitester-ap isolate

[Install]
WantedBy=multi-user.target'
UNIT_CHANGED=0
if [ "$(cat "$UNIT" 2>/dev/null)" = "$UNIT_TEXT" ]; then
    fine "service file up to date"
else
    need "service file missing or out of date"
fi
if redo; then
    UNIT_CHANGED=1
    echo "$UNIT_TEXT" > "$UNIT"
    systemctl daemon-reload
fi
if systemctl is-enabled --quiet pitester-ap-isolate.service 2>/dev/null; then
    fine "isolation runs on boot"
else
    need "isolation service not enabled"
fi
if redo; then systemctl enable pitester-ap-isolate.service; fi
if systemctl is-active --quiet pitester-ap-isolate.service; then
    fine "isolation active"
else
    need "isolation not active"
fi
# restarting it re-isolates the hotspot, so only when needed - with --update,
# an AP opened from the SYSTEM screen stays open
if redo || { fixing && [ "$UNIT_CHANGED" = 1 ]; }; then
    if systemctl restart pitester-ap-isolate.service; then ok "hotspot isolated (SSH/SFTP only)"; else warn "isolation will apply on next boot"; fi
elif fixing; then
    info "forwarding setting left as-is"
fi

say "sudo rule for the root commands the tester needs"
paths() { for d in /usr/sbin /usr/bin /sbin /bin; do [ -x "$d/$1" ] && echo "$d/$1"; done; return 0; }
CMDS=()
for p in $(paths ethtool); do CMDS+=("$p"); done
for p in $(paths lldpcli); do CMDS+=("$p"); done
for p in $(paths arp-scan); do CMDS+=("$p"); done   # SCANS > NETWORK SCAN
for p in $(paths nmcli); do CMDS+=("$p"); done   # CONNECTIONS screens, if polkit says no
for p in $(paths systemctl); do CMDS+=("$p poweroff" "$p reboot" "$p restart lldpd"); done
CMDS+=("/usr/local/sbin/pitester-ap")
LINE="$USER_NAME ALL=(root) NOPASSWD: $(printf '%s, ' "${CMDS[@]}")"
LINE="${LINE%, }"
if [ "$(cat /etc/sudoers.d/pitester 2>/dev/null)" = "$LINE" ]; then
    fine "sudoers rule up to date"
else
    need "sudoers rule missing or out of date"
fi
if redo; then
    TMP="$(mktemp)"
    echo "$LINE" > "$TMP"
    if visudo -cf "$TMP" >/dev/null; then
        install -m 440 "$TMP" /etc/sudoers.d/pitester
        ok "sudoers rule installed"
    else
        warn "sudoers rule failed validation - not installed"; cat "$TMP"
    fi
    rm -f "$TMP"
fi
if [ "$CHECK" = 1 ] && [ ${#MISSING[@]} -gt 0 ]; then
    info "(the rule picks up newly installed tools once the packages above are in)"
fi

say "Autostart on login + results folder"
DESKTOP="$USER_HOME/.config/autostart/pitester.desktop"
DESKTOP_TEXT="[Desktop Entry]
Type=Application
Name=Pi Cable Tester
Exec=$APP_DIR/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true"
if [ "$(cat "$DESKTOP" 2>/dev/null)" = "$DESKTOP_TEXT" ]; then
    fine "autostart entry up to date"
else
    need "autostart entry missing or out of date"
fi
if redo; then
    sudo -u "$USER_NAME" mkdir -p "$USER_HOME/.config/autostart"
    echo "$DESKTOP_TEXT" > "$DESKTOP"
    chown "$USER_NAME:$USER_NAME" "$DESKTOP"
fi
if [ -d "$USER_HOME/scan_results" ]; then
    fine "results folder exists"
else
    need "no results folder"
fi
if redo; then sudo -u "$USER_NAME" mkdir -p "$USER_HOME/scan_results"; fi
if fixing; then chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"; fi

if fixing; then
    say "Disabling screen blanking"
    if raspi-config nonint do_blanking 1 2>/dev/null; then ok "screen blanking off"; else warn "couldn't set - do it in raspi-config"; fi
fi

if [ "$CHECK" = 1 ]; then
    if [ "$TODO" = 0 ]; then
        echo -e "\n${G}${B}==> Everything is installed and up to date.${N}"
    else
        echo -e "\n${Y}${B}==> $TODO thing(s) need fixing.${N} Run:  ${C}sudo bash install.sh --update${N}"
    fi
    exit 0
fi

# with --update the hotspot may have been left alone, so show the password it really has
SHOWN_PASS="$(nmcli -s -g 802-11-wireless-security.psk connection show "$AP_CON" 2>/dev/null || true)"
echo -e "\n${G}${B}==> Done.${N}"
echo
echo "   ${B}Hotspot:${N}  SSID ${B}${G}$AP_SSID${N}   password ${B}${G}${SHOWN_PASS:-$AP_PASS}${N}"
echo "   ${B}SSH/SFTP:${N} ${B}${G}${AP_ADDR%/*}${N}"
echo
echo "   ${B}Change the hotspot password${N} ${D}(connected clients drop off; rejoin with the new one):${N}"
cmd "sudo AP_PASS=NEWPASSWORD bash install.sh --update"
echo "   ${B}Back to the default${N} ${D}(eth0 MAC, no colons):${N}"
cmd "sudo AP_PASS=\"\$(tr -d ':' < /sys/class/net/eth0/address)\" bash install.sh --update"
info "(min 8 characters; --update without AP_PASS keeps the current password, --force resets it)"
echo
echo "   ${B}Results:${N}      $USER_HOME/scan_results"
echo "   ${B}Maintenance:${N}  ${C}touch $APP_DIR/MAINTENANCE${N} ${D}(exits go to desktop, no power off/reboot)${N}"
echo
if [ "$FORCE" = 0 ] && [ "$TODO" = 0 ]; then
    echo "   ${D}Nothing needed changing - no reboot needed.${N}"
else
    echo "   ${Y}${B}Reboot now:${N}  ${C}sudo reboot${N}"
fi
