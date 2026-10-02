# Pi Cable Tester - TODO

## Recently done
- Full style/robustness pass on app.py, netcore.py, storage.py (readable
  formatting, no lambdas, verb-first naming, snake_case classes).
- SSDP/UPnP discovery merged into netcore.py (was standalone ssdp_probe.py,
  now archived in old/). Not yet wired into the scan flow or a device list
  screen - see item 1 below.
- preflight.py: two-tier self-diagnostic (safe read-only checks that run
  automatically on the new SYSTEM > SELF TEST screen, plus a confirm-gated
  active tier that exercises the link/AP for real). run_safe_checks() also
  hardened run() and query_dns() against a couple of real edge cases found
  along the way (non-UTF8 tool output, an unbounded receive loop).
- Fixed report-screen flicker during live scans (was rebuilding every
  widget on every 1s tick even when nothing changed).
- Repo is live at github.com/devlynecoulter/pitester (public).
- UI overhaul (on unstable-sts): home is now CONNECTIONS | TESTS | SCANS |
  LOGS, each opening a submenu; unbuilt features show greyed out with
  "(soon)". BACK returns to the parent menu, SYSTEM moved to a gear icon in
  the status bar, switch/port/VLAN shown in the status bar, clock removed.

## 0. Groundwork (do first, the rest depends on it)
- [ ] Shake out the UI overhaul. A rewrite this size is guaranteed to have
      a pile of small issues that only show up on the real 640x480 touch
      screen. Run through every menu and screen on the Pi and log what's
      off here, e.g.:
  - [ ] Button text fits at 640x480 (3-column submenus, long labels like
        "RESET\nCONNECTION")
  - [ ] Status bar: switch/port/VLAN text truncates cleanly, gear is easy
        to hit with a finger
  - [ ] BACK lands on the right menu from every screen (incl. after a
        cable plug-in auto-jumps to the scan screen)
  - [ ] Scan status ("scanning...", "waiting for DHCP") is visible enough
        now that the home card is gone
  - [ ] Merge unstable-sts into master once it's solid
- [ ] Put git on the Pi: clone the repo there so updates are a `git pull`
      (and branch switches are a `git checkout`) instead of copying files
      over by hand. Needs a GitHub login or deploy key on the Pi.
- [ ] Make netcore take an interface argument instead of the global `IFACE`,
      so every test can run on eth0 OR wlan0.
- [ ] On-screen touch keyboard in Tk (needed for Wi-Fi passwords, file names).
- [ ] Captures/results can fill or wear out the SD card: size limits, and
      optional save to a USB stick.

## 1. Aggressive device discovery (TOP PRIORITY)
- [ ] ARP sweep of the whole subnet (arp-scan, needs root via sudoers).
      Cap at /22 (1024 addresses); ask before sweeping anything bigger.
- [ ] TCP port probe on hosts that answered only. Editable port list, e.g.
      22, 23, 80, 443, 554 (RTSP cameras), 8080, 1400 (Sonos),
      8008/8009 (Chromecast) + AV/control ports from the job's gear.
- [ ] Name/ID layer: MAC vendor, reverse DNS, mDNS/Bonjour, SSDP/UPnP,
      NetBIOS, HTTP page title.
      - [x] SSDP/UPnP: discover_ssdp() lives in netcore.py now (see
            "Recently done"). Still need: a scan step or button that calls
            it, and somewhere in the UI to show what it finds.
- [ ] Results: device list screen (IP, MAC, vendor, name, open ports),
      saved to CSV like the other tests.
- [ ] Also from earlier list:
  - [ ] IGMP querier / multicast check (Dante, NDI, AV-over-IP dropouts)
  - [ ] Rogue DHCP detection (list every DHCP server that answers)
  - [ ] IP conflict check (arping on the tester's own address)
  - [ ] VLAN probe (tagged traffic on common VLAN IDs)
- Note: only run on networks you're authorised to service.

## 2. Raw packet capture (save only, no UI viewer)
- [ ] Start/stop button; tcpdump to ~/captures, named like results
      (SWITCH_PORT_yymmddhhmmss.pcap).
- [ ] Ring buffer (tcpdump -C/-W) so it can't fill the card.
- [ ] Choose interface: eth0 / wlan0.
- [ ] Viewing/analysis lives in the future web app.

## 3. AP sniffer (find hidden APs)
- [ ] Scan with the built-in radio (does 2.4 + 5 GHz and scans while
      connected; the PAU06 is busy being the hotspot and is 2.4 GHz only).
- [ ] Show top 5 by signal: SSID (or <hidden>), dBm, channel, band,
      BSSID + vendor. Refresh every few seconds.
- [ ] "Track" mode: pick one BSSID, show a big dBm number that rises as
      you walk toward it.

## 4. Wi-Fi as an alternative to Ethernet (built-in NIC)
- [ ] Scan list, tap an SSID, enter password on the touch keyboard, connect.
- [ ] Run the normal scan/discovery tests over wlan0.
- [ ] The built-in radio is free to use for testing: SSH/SFTP goes over the
      PAU06 hotspot. Keep the saved home connection as-is (don't delete it);
      Wi-Fi test mode just switches the built-in radio to the chosen network.

## 5. Bluetooth / BLE sniffing (Adafruit dongle)
- [ ] Confirm which Adafruit dongle (e.g. Bluefruit LE Sniffer = Nordic
      sniffer firmware, works with Wireshark).
- [ ] Basic BLE advertising scan (name, MAC, RSSI) could also use the
      Pi 4's built-in Bluetooth.
- [ ] Captures saved to file for the web app.

## 6. Zigbee diagnostics (need to buy a dongle)
- [ ] Pick a dongle that supports sniffer firmware (research before buying).
- [ ] Channel energy scan, channels 11-26 (Zigbee overlaps 2.4 GHz Wi-Fi,
      so show which Wi-Fi channels collide).
- [ ] List Zigbee networks (PAN IDs, channel, signal).
- [ ] Packet capture to file for the web app.

## Later
- [ ] Headless web app version (reuses netcore.py).
- [ ] Battery HAT support: battery level in the status bar, safe shutdown
      when low.
