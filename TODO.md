# Pi Cable Tester - TODO

## 0. Groundwork (do first, the rest depends on it)
- [ ] Make netcore take an interface argument instead of the global `IFACE`,
      so every test can run on eth0 OR wlan0.
- [ ] Rearrange the UI into submenus. Proposed home screen:
      WIRED | WI-FI | DISCOVERY | RADIO (BT / Zigbee) | RESULTS | SYSTEM
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
      - [ ] SSDP/UPnP is already written standalone in ssdp_probe.py
            (M-SEARCH + passive NOTIFY listen + parallel description
            fetch, stdlib only). To merge into netcore.py: drop its
            iface_ip() in favor of netcore's own ip_info(), fold
            ssdp_discover() in as another scan step, and surface its
            sections on the device list screen above.
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
