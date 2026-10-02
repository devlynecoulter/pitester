#!/bin/bash
# Pi Cable Tester installer. From the pitester folder:  sudo ./install.sh
# Does NOT touch your existing Wi-Fi client connection on the built-in adapter.
set -euo pipefail

AP_SSID="scanner"
# Hotspot password: defaults to this Pi's eth0 MAC, lowercase, no colons
# (e.g. dca632a1b2c3). Override with:  sudo AP_PASS=something bash install.sh
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

if [ "$EUID" -ne 0 ]; then die "Run with sudo:  sudo bash install.sh"; fi
if [ ${#AP_PASS} -lt 8 ]; then die "Hotspot password must be at least 8 characters"; fi
USER_NAME="${SUDO_USER:-}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
    die "Run via sudo from your normal login user, not as root."
fi
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

say "Normalising line endings / permissions"
sed -i 's/\r$//' "$APP_DIR"/*.py "$APP_DIR"/*.sh "$APP_DIR"/pitester-ap
chmod +x "$APP_DIR/launch.sh" "$APP_DIR/app.py" "$APP_DIR/pitester-ap"

say "Installing packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    lldpd ethtool ieee-data python3-tk iw nftables iputils-ping dnsmasq-base

say "Configuring lldpd (LLDP + CDP, eth0 only)"
mkdir -p /etc/lldpd.d
echo "configure system interface pattern eth0" > /etc/lldpd.d/pitester.conf
if grep -q '^DAEMON_ARGS=' /etc/default/lldpd 2>/dev/null; then
    sed -i 's/^DAEMON_ARGS=.*/DAEMON_ARGS="-c"/' /etc/default/lldpd
else
    echo 'DAEMON_ARGS="-c"' >> /etc/default/lldpd
fi
getent group _lldpd >/dev/null && usermod -aG _lldpd "$USER_NAME"
systemctl enable lldpd
systemctl restart lldpd

say "Finding the USB Wi-Fi adapter (PAU06)"
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

say "Creating hotspot '$AP_SSID' (bound to $AP_MAC only)"
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

say "Installing AP isolation helper + boot service"
install -m 755 "$APP_DIR/pitester-ap" /usr/local/sbin/pitester-ap
cat > /etc/systemd/system/pitester-ap-isolate.service <<'EOF'
[Unit]
Description=Pi tester: block forwarding through the Wi-Fi hotspot
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/pitester-ap isolate

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable pitester-ap-isolate.service
if systemctl restart pitester-ap-isolate.service; then ok "hotspot isolated (SSH/SFTP only)"; else warn "isolation will apply on next boot"; fi

say "Granting the tester the specific root commands it needs"
paths() { for d in /usr/sbin /usr/bin /sbin /bin; do [ -x "$d/$1" ] && echo "$d/$1"; done; return 0; }
CMDS=()
for p in $(paths ethtool); do CMDS+=("$p"); done
for p in $(paths lldpcli); do CMDS+=("$p"); done
for p in $(paths nmcli); do CMDS+=("$p"); done   # CONNECTIONS screens, if polkit says no
for p in $(paths systemctl); do CMDS+=("$p poweroff" "$p reboot" "$p restart lldpd"); done
CMDS+=("/usr/local/sbin/pitester-ap")
LINE="$USER_NAME ALL=(root) NOPASSWD: $(printf '%s, ' "${CMDS[@]}")"
TMP="$(mktemp)"
echo "${LINE%, }" > "$TMP"
if visudo -cf "$TMP" >/dev/null; then
    install -m 440 "$TMP" /etc/sudoers.d/pitester
    ok "sudoers rule installed"
else
    warn "sudoers rule failed validation - not installed"; cat "$TMP"
fi
rm -f "$TMP"

say "Autostart on login + results folder"
sudo -u "$USER_NAME" mkdir -p "$USER_HOME/.config/autostart" "$USER_HOME/scan_results"
cat > "$USER_HOME/.config/autostart/pitester.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Pi Cable Tester
Exec=$APP_DIR/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
chown "$USER_NAME:$USER_NAME" "$USER_HOME/.config/autostart/pitester.desktop"
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"

say "Disabling screen blanking"
if raspi-config nonint do_blanking 1 2>/dev/null; then ok "screen blanking off"; else warn "couldn't set - do it in raspi-config"; fi

echo -e "\n${G}${B}==> Done.${N}"
echo
echo "   ${B}Hotspot:${N}  SSID ${B}${G}$AP_SSID${N}   password ${B}${G}$AP_PASS${N}"
echo "   ${B}SSH/SFTP:${N} ${B}${G}${AP_ADDR%/*}${N}"
echo
echo "   ${B}Change the hotspot password${N} ${D}(connected clients drop off; rejoin with the new one):${N}"
cmd "sudo nmcli connection modify $AP_CON wifi-sec.psk \"NEWPASSWORD\""
cmd "sudo nmcli connection up $AP_CON"
echo "   ${B}Back to the default${N} ${D}(eth0 MAC, no colons):${N}"
cmd "sudo nmcli connection modify $AP_CON wifi-sec.psk \"\$(tr -d ':' < /sys/class/net/eth0/address)\""
cmd "sudo nmcli connection up $AP_CON"
echo "   ${B}Or set it at install time:${N}"
cmd "sudo AP_PASS=NEWPASSWORD bash install.sh"
info "(min 8 characters; rerunning install.sh without AP_PASS resets it to the MAC)"
echo
echo "   ${B}Results:${N}      $USER_HOME/scan_results"
echo "   ${B}Maintenance:${N}  ${C}touch $APP_DIR/MAINTENANCE${N} ${D}(exits go to desktop, no power off/reboot)${N}"
echo
echo "   ${Y}${B}Reboot now:${N}  ${C}sudo reboot${N}"
