"""
netcore.py - all network probing for the Pi cable tester.

No Tk code in here on purpose: the future headless web version can import
this module unchanged. Every test produces "sections":
    [(section_title, [(field, value, status), ...]), ...]
which the UI renders and storage.py writes to CSV.
"""
import ipaddress
import json
import os
import random
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

IFACE = os.environ.get("PITESTER_IFACE", "eth0")
AP_CONF = "/etc/pitester/ap.conf"
AP_HELPER = "/usr/local/sbin/pitester-ap"

PASS, FAIL, WARN, NA = "PASS", "FAIL", "WARN", "NA"
_RANK = {
    "": 0,
    NA: 0,
    PASS: 1,
    WARN: 2,
    FAIL: 3,
}

PAIRS = [
    ("A", "1/2"),
    ("B", "3/6"),
    ("C", "4/5"),
    ("D", "7/8"),
]

# counters that point at the cable / PHY rather than at host load
CABLE_ERR = {"rx_crc_errors", "rx_frame_errors", "rx_length_errors",
             "tx_carrier_errors", "collisions"}
ERR_COUNTERS = [
    "rx_errors",
    "tx_errors",
    "rx_crc_errors",
    "rx_frame_errors",
    "rx_length_errors",
    "rx_over_errors",
    "rx_fifo_errors",
    "rx_missed_errors",
    "tx_carrier_errors",
    "tx_fifo_errors",
    "collisions",
]


###################################### helpers##########################
def na(v):
    return NA if v in (None, "", []) else v


def worst(sections):
    w = ""
    for _, rows in sections:
        for _, _, s in rows:
            if _RANK.get(s, 0) > _RANK.get(w, 0):
                w = s
    return w


def run(cmd, timeout=10, sudo=False):
    cmd = list(cmd)
    if sudo:
        cmd = ["sudo", "-n"] + cmd
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except UnicodeDecodeError as e:
        # a tool emitted bytes that aren't valid in the system locale
        return 1, "", f"non-text output: {e}"
    except OSError as e:
        # e.g. PermissionError (not executable) or a corrupt binary
        return 126, "", str(e)


def sysfs(name, iface=IFACE):
    try:
        with open(f"/sys/class/net/{iface}/{name}") as f:
            return f.read().strip()
    except OSError:
        return None


def carrier(iface=IFACE):
    return sysfs("carrier", iface) == "1"


# --------------------------------------------------------------- link
def quick_link(iface=IFACE):
    up = carrier(iface)
    speed = duplex = None
    if up:
        try:
            speed = int(sysfs("speed", iface) or -1)
        except ValueError:
            speed = -1
        speed = speed if speed > 0 else None
        duplex = sysfs("duplex", iface)

    return {"up": up, "speed": speed, "duplex": duplex}


def _parse_ethtool(text):
    # "Key: value" lines, but a long value (e.g. advertised modes) wraps
    # onto following lines with no ":" - those get appended to the last key
    d, key = {}, None
    for line in text.splitlines():
        if not line.strip() or line.startswith("Settings for"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            d[key] = v.strip()
        elif key:
            d[key] = (d[key] + " " + line.strip()).strip()
    return d


def link_info(iface=IFACE):
    info = quick_link(iface)
    rc, out, _ = run(["ethtool", iface])
    d = _parse_ethtool(out) if rc == 0 else {}
    info["autoneg"] = d.get("Auto-negotiation")
    pm = d.get("Link partner advertised link modes", "")
    if not info["up"] or not pm or "not reported" in pm.lower():
        info["partner_gig"] = None
    else:
        info["partner_gig"] = "1000baseT/Full" in pm
    info["master_slave"] = d.get("master-slave status")
    return info


def link_inference(li):
    """What the negotiated link tells us about the pairs."""
    # one cohesive decision tree (link speed -> what it implies about the
    # pairs), left as a single if/elif chain rather than split up
    rows = []
    if not li.get("up"):
        rows.append(("Link", "No link", FAIL))
        rows.append(("Meaning", "Far end unplugged/unpowered, or a fault on pins 1/2 or 3/6", ""))
    elif li.get("speed") == 1000:
        rows.append(("Link", "1000 Mb/s", PASS))
        rows.append(("Pairs", "All 4 pairs carrying signal (1000BASE-T needs all four)", PASS))
    elif li.get("speed") == 100:
        rows.append(("Link", "100 Mb/s", WARN))
        pg = li.get("partner_gig")
        if pg:
            rows.append(("Pairs", "Far end supports gig but link downshifted: 1/2 + 3/6 OK, "
                                  "suspect 4/5 or 7/8 (open, split, or poor termination)", FAIL))
        elif pg is False:
            rows.append(("Pairs", "Far end is 100 Mb only: 1/2 + 3/6 OK, 4/5 + 7/8 unverified", WARN))
        else:
            rows.append(("Pairs", "1/2 + 3/6 OK, 4/5 + 7/8 unverified", WARN))
    else:
        rows.append(("Link", f"{li.get('speed') or '?'} Mb/s", WARN))
        rows.append(("Pairs", "Very low speed: severe cable fault or legacy device", WARN))
    return ("CABLE: LINK CHECK", rows)


# --------------------------------------------------------------- IP layer
def ip_info(iface=IFACE):
    res = {
        "mac": sysfs("address", iface),
        "ipv4": [],
        "gateway": None,
        "dns": [],
        "domain": None,
        "dhcp_server": None,
        "mtu": sysfs("mtu", iface),
    }
    rc, out, _ = run(["ip", "-j", "addr", "show", "dev", iface])
    try:
        for a in json.loads(out)[0].get("addr_info", []):
            if a.get("family") == "inet":
                res["ipv4"].append(f"{a['local']}/{a['prefixlen']}")
    except (ValueError, IndexError, KeyError):
        pass
    rc, out, _ = run(["ip", "-j", "route", "show", "default", "dev", iface])
    try:
        routes = json.loads(out) if out.strip() else []
        if routes:
            res["gateway"] = routes[0].get("gateway")
    except ValueError:
        pass
    rc, out, _ = run(["nmcli", "-t", "-f", "IP4.DNS,IP4.DOMAIN,DHCP4", "device", "show", iface])
    for line in out.splitlines():
        # one cohesive parse: which nmcli field is this line, and what do we
        # do with it - left as a single if/elif chain
        k, _, v = line.partition(":")
        v = v.replace("\\:", ":").strip()
        if k.startswith("IP4.DNS"):
            res["dns"].append(v)
        elif k.startswith("IP4.DOMAIN") and v:
            res["domain"] = v
        elif k.startswith("DHCP4.OPTION"):
            name, _, val = v.partition(" = ")
            name = name.strip()
            if name == "dhcp_server_identifier":
                res["dhcp_server"] = val
            elif name == "domain_name" and not res["domain"]:
                res["domain"] = val
    return res


def neighbor_mac(ip, iface=IFACE):
    rc, out, _ = run(["ip", "-j", "neigh", "show", "to", ip, "dev", iface])
    try:
        for n in json.loads(out) if out.strip() else []:
            if n.get("lladdr"):
                return n["lladdr"]
    except ValueError:
        pass
    return None


_OUI = None


def vendor(mac):
    # bit 1 of the first octet marks a locally-administered MAC (randomized
    # or virtual) - those were never assigned an OUI, so don't bother looking
    global _OUI
    if not mac:
        return None
    try:
        if int(mac.split(":")[0], 16) & 2:
            return "Locally administered (random/virtual MAC)"
    except ValueError:
        return None
    if _OUI is None:  # load the ~30k-line OUI table once, lazily, and cache it
        _OUI = {}
        try:
            with open("/usr/share/ieee-data/oui.txt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "(hex)" in line:
                        pre, _, name = line.partition("(hex)")
                        _OUI[pre.strip().replace("-", ":").upper()] = name.strip()
        except OSError:
            pass

    return _OUI.get(mac.upper()[:8])


def parse_ping(txt):
    r = {
        "sent": None,
        "recv": None,
        "loss": None,
        "min": None,
        "avg": None,
        "max": None,
    }
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?([\d.]+)% packet loss",
                  txt, re.S)
    if m:
        r.update(sent=int(m[1]), recv=int(m[2]), loss=float(m[3]))
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/[\d.]+ ms", txt)
    if m:
        r.update(min=float(m[1]), avg=float(m[2]), max=float(m[3]))
    return r


def ping(
    host,
    count=3,
    interval=None,
    iface=IFACE,
):
    cmd = [
        "ping",
        "-n",
        "-q",
        "-I",
        iface,
        "-W",
        "1",
        "-c",
        str(count),
    ]
    if interval:
        cmd += ["-i", str(interval)]
    rc, out, err = run(cmd + [host], timeout=count * 2 + 5)
    return parse_ping(out + err)


def fmt_ping(r):
    if r.get("loss") is None:
        return NA, FAIL
    s = f"{r['loss']:g}% loss"
    if r.get("avg") is not None:
        s += f", avg {r['avg']:.1f} ms"
    st = PASS if r["loss"] == 0 else FAIL if r["loss"] >= 100 else WARN
    return s, st


def query_dns(
    server,
    name="google.com",
    src=None,
    timeout=2,
):
    """Tiny raw DNS A query so we test the DNS server handed out on eth0."""
    if ":" in server:
        return {"server": server, "ok": False, "detail": "IPv6 DNS server - not tested"}
    try:
        tid = random.randint(0, 0xFFFF)
        q = struct.pack(
            ">HHHHHH",
            tid,
            0x0100,
            1,
            0,
            0,
            0,
        )
        q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
        q += struct.pack(">HH", 1, 1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            if src:
                s.bind((src, 0))
            t = time.monotonic()
            s.sendto(q, (server, 53))
            deadline = t + timeout
            while True:
                # re-arm the timeout to whatever's left of the deadline each pass,
                # so a stream of non-matching packets can't keep this alive past
                # `timeout` (unlike a single settimeout() call up front)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("no matching reply before deadline")
                s.settimeout(remaining)
                data, _ = s.recvfrom(1500)
                if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == tid:
                    break
            ms = (time.monotonic() - t) * 1000
        rcode = data[3] & 0x0F
        an = struct.unpack(">H", data[6:8])[0]
        ok = rcode == 0 and an > 0
        detail = f"{name}: {an} answer(s) in {ms:.0f} ms" if ok else f"error code {rcode}"
        return {"server": server, "ok": ok, "detail": detail}
    except OSError as e:
        return {"server": server, "ok": False, "detail": f"no answer ({e})"}


# --------------------------------------------------------------- LLDP / CDP (via lldpd)
# lldpd's JSON is inconsistently shaped depending on version/backend: a
# field can come back as a bare value, a single {"value": ...} dict, or a
# list of either. These three helpers normalize that before _parse_neighbor
# touches anything.
def _as_list(x):
    return x if isinstance(x, list) else ([] if x is None else [x])


def _first_value(x):
    # one cohesive decision (what counts as this field's value), left as a
    # single if/elif chain
    for i in _as_list(x):
        if isinstance(i, dict):
            if "value" in i:
                return i["value"]
        elif i not in (None, ""):
            return str(i)
    return None


def _all_values(x):
    out = []
    for i in _as_list(x):
        v = i.get("value") if isinstance(i, dict) else i
        if v not in (None, ""):
            out.append(str(v))
    return out


_CHASSIS_KEYS = {"id", "name", "descr", "mgmt-ip", "mgmt-iface", "capability"}


def _parse_neighbor(itf):
    n = {"protocol": itf.get("via")}
    ch = _as_list(itf.get("chassis"))
    c = ch[0] if ch else {}
    # plain-json form: {"chassis": {"SwitchName": {...}}}
    if isinstance(c, dict) and len(c) == 1 and not (set(c) & _CHASSIS_KEYS):
        n["name"], c = next(iter(c.items()))
    if isinstance(c, dict):
        n["name"] = n.get("name") or _first_value(c.get("name"))
        cid = _as_list(c.get("id"))
        if cid and isinstance(cid[0], dict):
            n["chassis_id"], n["chassis_id_type"] = cid[0].get("value"), cid[0].get("type")
        n["descr"] = _first_value(c.get("descr"))
        n["mgmt_ip"] = ", ".join(_all_values(c.get("mgmt-ip"))) or None
        caps = [x.get("type") for x in _as_list(c.get("capability"))
                if isinstance(x, dict) and x.get("enabled")]
        n["caps"] = ", ".join(caps) or None

    p = (_as_list(itf.get("port")) or [{}])[0]
    if isinstance(p, dict):
        pid = _as_list(p.get("id"))
        if pid and isinstance(pid[0], dict):
            n["port_id"], n["port_id_type"] = pid[0].get("value"), pid[0].get("type")
        n["port_descr"] = _first_value(p.get("descr"))
        n["ttl"] = _first_value(p.get("ttl")) or _first_value(itf.get("ttl"))

    vl = []
    for v in _as_list(itf.get("vlan")):
        if isinstance(v, dict):
            s = str(v.get("vlan-id", "?"))
            if v.get("pvid"):
                s += " (native)"
            if v.get("value") and str(v.get("value")) != str(v.get("vlan-id")):
                s += f" {v['value']}"
            vl.append(s)
    n["vlans"] = ", ".join(vl) or None
    return n


def lldp_neighbors(iface=IFACE):
    cmd = [
        "lldpcli",
        "-f",
        "json0",
        "show",
        "neighbors",
        "details",
    ]
    rc, out, err = run(cmd, timeout=5)
    if rc != 0:  # not in _lldpd group yet -> try sudo
        rc, out, err = run(cmd, timeout=5, sudo=True)
    if rc != 0:
        return None, (err or out).strip() or f"lldpcli exit {rc}"
    try:
        data = json.loads(out)
    except ValueError:
        return None, "unreadable lldpcli output"
    found = []
    for block in _as_list(data.get("lldp")):
        if not isinstance(block, dict):
            continue
        for itf in _as_list(block.get("interface")):
            if not isinstance(itf, dict):
                continue
            name = itf.get("name")
            if name is None and len(itf) == 1:
                name, itf = next(iter(itf.items()))
            if name == iface and isinstance(itf, dict):
                found.append(_parse_neighbor(itf))
    return found, None


def port_label(n):
    # a raw MAC or an internal ifIndex number isn't a useful label on its
    # own - prefer the human-readable port description when either shows up
    pid, typ, desc = n.get("port_id"), n.get("port_id_type"), n.get("port_descr")
    if not pid:
        return desc or NA
    if typ == "mac" or (typ == "local" and str(pid).isdigit()):
        return desc or f"port {pid}"
    return pid


# --------------------------------------------------------------- scan
def _state_text(st):
    ph = st.get("phase")
    if ph == "done":
        if not (st.get("link") or {}).get("up"):
            return "No link"
        return "Switch found via " + st["lldp"][0].get("protocol", "LLDP") if st.get("lldp") \
            else "No LLDP/CDP heard"

    phase_text = {
        "link": "Checking link...",
        "dhcp": "Waiting for DHCP...",
        "tests": "Testing gateway / internet...",
        "lldp": f"Listening for LLDP/CDP... {st.get('lldp_left', '')}",
    }
    return phase_text.get(ph, "")


def scan_ident(st):
    nb = (st.get("lldp") or [None])[0]
    ip = st.get("ip") or {}
    return {
        "switch": (nb.get("name") or nb.get("chassis_id") or NA) if nb else NA,
        "port": port_label(nb) if nb else NA,
        "vlan": (nb.get("vlans") if nb else None) or NA,
        "ip": (ip.get("ipv4") or [NA])[0],
        "state": _state_text(st),
    }


def scan_sections(st):
    secs = []
    li = st.get("link") or {}
    rows = []
    if not li.get("up"):
        rows.append(("Link", "NO LINK", FAIL))
    else:
        sp, dx = li.get("speed"), li.get("duplex")
        rows.append(("Link", f"{sp or '?'} Mb/s, {dx or '?'} duplex",
                     PASS if sp == 1000 and dx == "full" else WARN))
        rows.append(("Auto-neg", na(li.get("autoneg")), ""))
        pg = li.get("partner_gig")
        rows.append(("Partner 1G", {True: "advertised", False: "NOT advertised"}.get(pg, NA),
                     WARN if pg is False else ""))
        if li.get("master_slave"):
            rows.append(("Master/slave", li["master_slave"], ""))

    secs.append(("LINK", rows))

    rows = []
    nb = st.get("lldp") or []
    if nb:
        n = nb[0]
        rows += [
            ("Protocol", na(n.get("protocol")), PASS),
            ("Switch", na(n.get("name")), ""),
            ("Port", port_label(n), ""),
            ("Port desc", na(n.get("port_descr")), ""),
            ("VLAN", na(n.get("vlans")), ""),
            ("Mgmt IP", na(n.get("mgmt_ip")), ""),
            ("Chassis ID", f"{n.get('chassis_id') or NA} ({n.get('chassis_id_type') or '?'})", ""),
            ("Chassis vendor", na(vendor(n.get("chassis_id"))
                                  if n.get("chassis_id_type") == "mac" else None), ""),
            ("Description", na(n.get("descr")), ""),
            ("Capabilities", na(n.get("caps")), ""),
            ("TTL", na(n.get("ttl")), ""),
        ]
        if len(nb) > 1:
            rows.append(("Other neighbors", ", ".join(
                x.get("name") or x.get("chassis_id") or "?" for x in nb[1:]), WARN))
    else:
        rows += [("Switch", NA, ""), ("Port", NA, "")]
        # one cohesive decision (why no neighbor showed up), left as a
        # single if/elif chain
        if st.get("phase") == "done" and li.get("up"):
            rows.append(("Discovery", "No LLDP/CDP heard (unmanaged switch, LLDP disabled, "
                                      "or plugged straight into a device)", WARN))
            if st.get("lldp_err"):
                rows.append(("lldpd", st["lldp_err"], WARN))
        elif li.get("up"):
            rows.append(("Discovery", "listening...", ""))

    secs.append(("SWITCH / PORT", rows))

    ip = st.get("ip")
    if ip is not None:
        v4 = ip.get("ipv4") or []
        # one cohesive decision (what to show for our own IP), left as a
        # single if/elif chain
        if v4:
            apipa = all(a.startswith("169.254.") for a in v4)
            ipv = ", ".join(v4) + (" (self-assigned, no DHCP)" if apipa else "")
            ipst = WARN if apipa else PASS
        elif not li.get("up"):
            ipv, ipst = NA, ""
        elif st.get("phase") == "dhcp":
            ipv, ipst = "waiting for DHCP...", ""
        else:
            ipv, ipst = "none (no DHCP reply)", FAIL
        secs.append(("THIS TESTER", [
            ("MAC", na(ip.get("mac")), ""),
            ("IPv4", ipv, ipst),
            ("Gateway", na(ip.get("gateway")), ""),
            ("DNS", ", ".join(ip.get("dns") or []) or NA, ""),
            ("DHCP server", na(ip.get("dhcp_server")), ""),
            ("Domain", na(ip.get("domain")), ""),
            ("MTU", na(ip.get("mtu")), ""),
        ]))

    if st.get("gw"):
        g = st["gw"]
        secs.append(("GATEWAY", [("IP", g["ip"], ""), ("MAC", na(g.get("mac")), ""),
                                 ("Vendor", na(g.get("vendor")), "")]))

    rows = []
    # sibling checks that all build the same CONNECTIVITY section - kept
    # together rather than blank-line-separated
    if "gw_ping" in st:
        rows.append(("Ping gateway", *fmt_ping(st["gw_ping"])))
    if "inet_ping" in st:
        rows.append(("Ping 1.1.1.1", *fmt_ping(st["inet_ping"])))
    if "dns" in st:
        d = st["dns"]
        rows.append(("DNS lookup", f"{d['server']} - {d['detail']}", PASS if d["ok"] else FAIL))
    if rows:
        secs.append(("CONNECTIVITY", rows))
    return secs


def scan(emit, stop, fresh=False, lldp_wait=35, dhcp_wait=20):
    """emit(sections, ident) is called as results arrive."""
    st = {"phase": "link"}
    t0 = time.time()

    def push():
        if not stop.is_set():
            emit(scan_sections(st), scan_ident(st))

    def poll_lldp():
        nb, err = lldp_neighbors()
        if nb:
            st["lldp"] = nb
        st["lldp_err"] = err

    if fresh:  # clear neighbors cached from the previous cable
        run(["systemctl", "restart", "lldpd"], sudo=True, timeout=15)
    st["link"] = link_info()
    st["ip"] = ip_info()
    push()
    if not st["link"]["up"]:
        st["phase"] = "done"
        push()
        return st

    st["phase"] = "dhcp"
    while not stop.is_set():
        st["ip"] = ip_info()
        poll_lldp()
        if (st["ip"]["ipv4"] and st["ip"]["gateway"]) or time.time() - t0 > dhcp_wait \
                or not carrier():
            break
        push()
        stop.wait(1)

    st["phase"] = "tests"
    st["link"] = link_info()
    push()
    gw = st["ip"].get("gateway")
    if gw and not stop.is_set():
        st["gw_ping"] = ping(gw, count=4, interval=0.2)
        mac = neighbor_mac(gw)
        st["gw"] = {"ip": gw, "mac": mac, "vendor": vendor(mac)}
        push()
        st["inet_ping"] = ping("1.1.1.1", count=3, interval=0.2)
        dns = (st["ip"].get("dns") or [None])[0]
        if dns:
            src = st["ip"]["ipv4"][0].split("/")[0] if st["ip"]["ipv4"] else None
            st["dns"] = query_dns(dns, src=src)
        push()

    st["phase"] = "lldp"
    deadline = t0 + lldp_wait
    while not stop.is_set():
        poll_lldp()
        if st.get("lldp") or time.time() >= deadline or not carrier():
            break
        st["lldp_left"] = f"{int(deadline - time.time())}s"
        push()
        stop.wait(1)
    st["phase"] = "done"
    push()
    return st


# --------------------------------------------------------------- continuity / TDR
def test_cable(iface=IFACE):
    rc, out, err = run(["ethtool", "--cable-test", iface], sudo=True, timeout=40)
    txt = (out + "\n" + err).strip()
    low = txt.lower()
    if "password is required" in low:
        return {"supported": None, "error": "sudo rule missing - rerun install.sh"}
    if rc != 0:
        if any(s in low for s in ("not supported", "unrecognized option", "eopnotsupp")):
            return {"supported": False, "error": txt}
        return {"supported": None, "error": txt or f"ethtool exit {rc}"}

    pairs = {}
    for m in re.finditer(r"Pair ([A-D]) code (.+)", txt):
        pairs.setdefault(m[1], {})["code"] = m[2].strip()
    for m in re.finditer(r"Pair ([A-D]), fault length:\s*([\d.]+)\s*m", txt):
        pairs.setdefault(m[1], {})["length"] = float(m[2])
    if not pairs:
        return {"supported": None, "error": "no pair results: " + txt}
    return {"supported": True, "pairs": pairs}


def cable_sections(res, li):
    # one cohesive decision (what the TDR rows say), left as a single
    # if/elif/else rather than split up
    secs = [link_inference(li)]
    rows = []
    sup = res.get("supported")
    if sup:
        pairs = res["pairs"]
        codes = [pairs.get(p, {}).get("code", "") for p, _ in PAIRS]
        all_open = all(c.lower().startswith("open") for c in codes)
        lens = {p: pairs[p]["length"] for p, _ in PAIRS if pairs.get(p, {}).get("length") is not None}
        longest = max(lens.values()) if lens else None
        for p, pins in PAIRS:
            info = pairs.get(p)
            if not info:
                rows.append((f"Pair {p} ({pins})", "no result", WARN))
                continue

            code, length = info.get("code", "?"), info.get("length")
            val = code + (f" at {length:.1f} m" if length is not None else "")
            if code.upper() == "OK":
                st = PASS
            elif all_open and length is not None and longest - length <= 2.0:
                st = ""  # every pair open at the same distance = far end unplugged
            else:
                st = FAIL
            rows.append((f"Pair {p} ({pins})", val, st))
        if all_open and lens:
            if not li.get("up") and max(lens.values()) - min(lens.values()) <= 2.0:
                # far end deliberately unplugged: no link is expected, not a fault
                secs[0] = ("CABLE: LINK CHECK", [("Link", "No link (far end open - expected)", "")])
            rows.append(("Cable length", f"~{longest:.1f} m (far end open)", ""))
            rows.append(("Note", "All pairs open = far end unplugged. A pair open noticeably "
                                 "shorter than the others is a break at that distance.", ""))

        rows.append(("Accuracy", "TDR distance is roughly +/-1-2 m. With a live device on the "
                                 "far end some PHYs report 'Unspecified'.", ""))
    elif sup is False:
        rows.append(("TDR", "Not supported by this Pi's PHY driver - the link check above "
                            "is the result", ""))
    else:
        rows.append(("TDR", f"Could not run: {res.get('error')}", WARN))

    secs.append(("CABLE: TDR", rows))
    return secs


# --------------------------------------------------------------- 1000BASE-T qualification
def counters(iface=IFACE):
    d = {}
    for c in ERR_COUNTERS:
        v = sysfs(f"statistics/{c}", iface)
        if v and v.isdigit():
            d[c] = int(v)
    v = sysfs("carrier_changes", iface)
    d["carrier_changes"] = int(v) if v and v.isdigit() else 0
    return d


def phy_counters(iface=IFACE):
    # not every PHY driver implements --phy-statistics - None here just
    # means "unavailable," and callers show that instead of treating it as a failure
    rc, out, _ = run(["ethtool", "--phy-statistics", iface], sudo=True, timeout=5)
    if rc != 0:
        return None
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        v = v.strip()
        if v.isdigit() and any(s in k.lower() for s in ("err", "nok", "fail", "false")):
            d[k.strip()] = int(v)
    return d or None


def qualify_gig(duration, emit, stop):
    """emit(sections=..., progress=(elapsed, total)). Returns final sections."""
    title = "1000BASE-T QUALIFICATION"
    rows = []

    def secs():
        return [(title, list(rows))]

    def add(f, v, s=""):
        rows.append((f, v, s))
        emit(sections=secs())

    li = link_info()
    if not li["up"]:
        add("Link", "No link - check cable / far end", FAIL)
        return secs()

    sp, dx, pg = li["speed"], li["duplex"], li["partner_gig"]
    add("Negotiated speed", f"{sp} Mb/s" if sp else "unknown", PASS if sp == 1000 else FAIL)
    add("Duplex", (dx or "unknown").capitalize(), PASS if dx == "full" else FAIL)
    add("Auto-negotiation", na(li["autoneg"]), PASS if li["autoneg"] == "on" else WARN)
    if pg is True:
        add("Far end", "advertises 1000BASE-T/Full", PASS)
    elif pg is False:
        add("Far end", "does NOT advertise 1000BASE-T - can't qualify gig on this port", FAIL)
    else:
        add("Far end", "abilities not reported", "")
    if li.get("master_slave"):
        add("Master/slave", li["master_slave"])
    if sp != 1000:
        if pg:
            add("Diagnosis", "Far end supports gig but the link came up slower: likely a bad "
                             "4/5 or 7/8 pair, split pair, or poor termination", FAIL)
        return secs()

    gw = ip_info().get("gateway")
    c0, p0 = counters(), phy_counters()
    proc = None
    if gw:
        try:
            proc = subprocess.Popen(
                ["ping", "-n", "-q", "-I", IFACE, "-i", "0.05", "-s", "1400", "-M", "do",
                 "-W", "1", "-w", str(duration), gw],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError:
            proc = None

    t0 = time.time()
    drops, was_up, slow = 0, True, set()
    while not stop.is_set():
        el = time.time() - t0
        if el >= duration:
            break
        q = quick_link()
        if not q["up"]:
            drops += 1 if was_up else 0
            was_up = False
        else:
            was_up = True
            if q["speed"] and q["speed"] != 1000:
                slow.add(q["speed"])

        emit(progress=(el, duration))
        stop.wait(0.25)

    if stop.is_set():
        if proc:
            proc.kill()
        add("Test", "Cancelled", WARN)
        return secs()

    ping_txt = ""
    if proc:
        try:
            ping_txt, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            ping_txt, _ = proc.communicate()
    c1, p1 = counters(), phy_counters()

    flaps = max(drops, (c1["carrier_changes"] - c0["carrier_changes"] + 1) // 2)
    detail = f"{duration} s, {flaps} drop(s)"
    if slow:
        detail += f", fell to {'/'.join(map(str, sorted(slow)))} Mb/s"
    add("Link stability", detail, PASS if flaps == 0 and not slow else FAIL)

    bad = False
    for k in ERR_COUNTERS:
        d = c1.get(k, 0) - c0.get(k, 0)
        if d > 0:
            bad = True
            add(k.replace("_", " "), f"+{d} during test", FAIL if k in CABLE_ERR else WARN)
    if not bad:
        add("Interface errors", "none during test", PASS)

    if p0 is None or p1 is None:
        add("PHY error counters", "not exposed by this PHY", "")
    else:
        pbad = False
        for k in sorted(p1):
            d = p1[k] - p0.get(k, 0)
            if d > 0:
                pbad = True
                add(f"PHY {k}", f"+{d} during test", FAIL)
        if not pbad:
            add("PHY error counters", "none during test", PASS)

    if gw:
        r = parse_ping(ping_txt)
        if r["loss"] is None:
            add("Traffic test", "no result", WARN)
        else:
            loss = r["loss"]
            v = f"{r['recv']}/{r['sent']} 1400-byte pings to gateway, {loss:g}% loss"
            if r["avg"] is not None:
                v += f", avg {r['avg']:.1f} / max {r['max']:.1f} ms"
            if 0 < loss <= 2:
                v += " (gateway may be rate-limiting ICMP)"
            add("Traffic test", v, PASS if loss == 0 else WARN if loss <= 2 else FAIL)
    else:
        add("Traffic test", "skipped - no gateway (no DHCP?). Link checks still valid.", WARN)

    return secs()


# --------------------------------------------------------------- blink port
def blink(mode, emit, stop, duration=30):
    gw = ip_info().get("gateway")
    if mode == "traffic" and not gw:
        emit(status="No gateway to send traffic to - using link-renegotiate mode")
        mode = "link"

    t0, n = time.time(), 0
    while not stop.is_set() and time.time() - t0 < duration:
        n += 1
        if mode == "traffic":
            emit(status=f"Burst {n}: watch for a fast-flickering activity LED")
            run(
                ["ping", "-n", "-q", "-I", IFACE, "-i", "0.005", "-s", "1400", "-w", "1", gw],
                timeout=4,
            )
            stop.wait(1.0)
        else:
            emit(status=f"Renegotiation {n}: watch for the port LED going dark")
            run(["ethtool", "-r", IFACE], sudo=True, timeout=5)
            stop.wait(4.0)
    emit(status="Stopped" if stop.is_set() else "Done")


# --------------------------------------------------------------- AP / system
def ap_iface():
    # install.sh records the AP adapter's MAC, not its interface name - USB
    # wifi dongles don't reliably keep the same wlanN name across reboots
    mac = None
    try:
        with open(AP_CONF) as f:
            for line in f:
                if line.startswith("AP_MAC="):
                    mac = line.split("=", 1)[1].strip().strip('"').lower()
    except OSError:
        return None
    if not mac:
        return None
    try:
        ifaces = os.listdir("/sys/class/net")
    except OSError:
        return None
    for n in ifaces:
        if (sysfs("address", n) or "").lower() == mac:
            return n
    return None


def ap_status():
    rc, out, err = run([AP_HELPER, "status"], sudo=True, timeout=8)
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k.strip():
            d[k.strip()] = v.strip()
    if rc != 0 and not d:
        d["error"] = (err or out).strip() or f"exit {rc}"
    return d


def set_ap(mode):
    rc, out, err = run([AP_HELPER, "isolate" if mode == "isolate" else "open"],
                       sudo=True, timeout=45)
    return rc == 0, (out + err).strip()


def status_snapshot():
    s = quick_link()
    s["ipv4"] = None
    rc, out, _ = run(
        ["ip", "-j", "-4", "addr", "show", "dev", IFACE],
        timeout=3,
    )
    try:
        for a in json.loads(out)[0].get("addr_info", []):
            s["ipv4"] = a.get("local")
            break
    except (ValueError, IndexError, KeyError):
        pass
    ai = ap_iface()
    if not ai:
        s["ap"] = None
    else:  # only "up" if the adapter is really in AP (hotspot) mode
        rc, out, _ = run(
            ["iw", "dev", ai, "info"],
            timeout=3,
        )
        s["ap"] = "up" if "type AP" in out else "down"

    return s


def system_info():
    rows = [("Hostname", socket.gethostname(), "")]
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        rows.append(("Uptime", f"{int(up // 3600)}h {int(up % 3600 // 60)}m", ""))
    except OSError:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            t = int(f.read()) / 1000
        rows.append(("CPU temp", f"{t:.1f} C", WARN if t > 75 else ""))
    except (OSError, ValueError):
        pass
    rc, out, _ = run(["systemctl", "is-active", "lldpd"])
    rows.append(("lldpd", out.strip() or "unknown", PASS if out.strip() == "active" else FAIL))
    secs = [("SYSTEM", rows)]

    ap = ap_status()
    ap_if = ap.get("iface")
    rows = []
    rc, out, _ = run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
    )
    for line in out.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 4 or parts[1] != "wifi" or parts[0] == ap_if:
            continue
        dev, conn = parts[0], parts[3].replace("\\:", ":")
        rc2, o2, _ = run(
            ["nmcli", "-g", "IP4.ADDRESS", "device", "show", dev],
        )
        addr = o2.strip().split("|")[0].strip() if o2.strip() else "no IP"
        rows.append((dev, f"{conn or 'not connected'} - {addr}", ""))
    secs.append(("WI-FI CLIENT", rows or [("Wi-Fi", "none", "")]))

    mode = ap.get("mode")
    eth_mac = (sysfs("address") or "").replace(":", "")
    rows = [
        ("SSID", na(ap.get("ssid")), ""),
        ("Password", f"{eth_mac} (eth0 MAC) unless changed" if eth_mac else NA, ""),
        ("Adapter", na(ap_if), ""),
        ("Address", na(ap.get("ip")), ""),
        ("State", na(ap.get("state")), ""),
        ("DHCP leases", na(ap.get("clients")), ""),
        ("Forwarding", "BLOCKED - SSH/SFTP to the Pi only" if mode == "isolated"
         else "OPEN - AP clients can reach other networks" if mode == "open" else NA,
         "" if mode == "isolated" else WARN if mode == "open" else ""),
    ]
    if ap.get("error"):
        rows.append(("Error", ap["error"], WARN))
    secs.append(("TESTER ACCESS POINT", rows))
    return secs, ap


# --------------------------------------------------------------- connections (NetworkManager)
# eth0 settings live in the tester's own profile, so the stock "Wired
# connection 1" is never touched; it's made on the first APPLY
ETH_CON = "pitester-eth"
ADAPTER_DEFAULTS = {
    "method": "auto",
    "address": "",
    "gateway": "",
    "dns": "",
    "link": "auto",
    "mtu": "auto",
}


def nmcli(args, timeout=20):
    """nmcli, retried through sudo if polkit says no (e.g. run over SSH
    instead of from the desktop session)."""
    rc, out, err = run(["nmcli"] + args, timeout=timeout)
    if rc != 0 and re.search(r"not authorized|insufficient privileges|permission denied", err, re.I):
        rc, out, err = run(["nmcli"] + args, timeout=timeout, sudo=True)
    return rc, out, err


def wifi_iface():
    """The built-in Wi-Fi radio: any Wi-Fi device that isn't the AP adapter."""
    ap = ap_iface()
    rc, out, _ = run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    for line in out.splitlines():
        dev, _, typ = line.partition(":")
        if typ == "wifi" and dev != ap:
            return dev
    return None


def _wifi_sort_key(n):
    return (not n["in_use"], -n["signal"])


def wifi_scan(iface):
    """Visible networks, one entry per SSID (strongest AP wins), connected
    one first. Hidden networks (no SSID) are left out. Returns (nets, error)."""
    rc, out, err = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN",
                        "device", "wifi", "list", "ifname", iface, "--rescan", "yes"], timeout=25)
    if rc != 0:
        return None, (err or out).strip() or f"nmcli exit {rc}"
    nets = {}
    for line in out.splitlines():
        parts = [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]
        if len(parts) < 5 or not parts[1]:
            continue
        n = {
            "in_use": parts[0] == "*",
            "ssid": parts[1],
            "signal": int(parts[2]) if parts[2].isdigit() else 0,
            "security": "" if parts[3] in ("", "--") else parts[3],
            "chan": parts[4],
        }
        old = nets.get(n["ssid"])
        if not old or n["in_use"] or (not old["in_use"] and n["signal"] > old["signal"]):
            nets[n["ssid"]] = n
    return sorted(nets.values(), key=_wifi_sort_key), None


def saved_wifi():
    """Names of saved Wi-Fi profiles (nmcli names them after the SSID)."""
    rc, out, _ = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    names = set()
    for line in out.splitlines():
        parts = [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]
        if len(parts) == 2 and parts[1] == "802-11-wireless":
            names.add(parts[0])
    return names


def wifi_connect(iface, ssid, password=None):
    """Join a network. A saved profile is reused (its password updated if a
    new one was typed) rather than deleted - that keeps the home connection
    as it was. Returns (ok, message)."""
    if ssid in saved_wifi():
        if password:
            rc, out, err = nmcli(["connection", "modify", "id", ssid, "wifi-sec.psk", password])
            if rc != 0:
                return False, (err or out).strip()
        rc, out, err = nmcli(["connection", "up", "id", ssid, "ifname", iface], timeout=45)
    else:
        args = ["device", "wifi", "connect", ssid, "ifname", iface]
        if password:
            args += ["password", password]
        rc, out, err = nmcli(args, timeout=45)
    return rc == 0, "connected" if rc == 0 else (err or out).strip() or f"nmcli exit {rc}"


def wifi_disconnect(iface):
    rc, out, err = nmcli(["device", "disconnect", iface])
    return rc == 0, "disconnected" if rc == 0 else (err or out).strip()


def clean_ipv4(field, text):
    """Tidy a typed address / gateway / DNS entry. Returns (value, error).
    An address with no /prefix gets /24."""
    text = text.strip()
    if not text:
        return "", None
    try:
        if field == "address":
            return str(ipaddress.IPv4Interface(text if "/" in text else text + "/24")), None
        if field == "dns":
            return ",".join(str(ipaddress.IPv4Address(x)) for x in re.split(r"[,\s]+", text) if x), None
        return str(ipaddress.IPv4Address(text)), None
    except ValueError as e:
        return None, str(e)


def adapter_connection(kind, iface):
    """The profile the settings screen edits: pitester-eth for eth0, or
    whichever network the Wi-Fi radio is connected to (None if none)."""
    if kind == "eth":
        return ETH_CON
    rc, out, _ = run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface])
    return out.strip() if out.strip() not in ("", "--") else None


def read_adapter(kind, iface):
    """Returns (profile name, settings) in the ADAPTER_DEFAULTS shape. A
    missing pitester-eth reads as the defaults, which is what eth0 does
    without it (DHCP, auto-negotiate)."""
    s = dict(ADAPTER_DEFAULTS)
    con = adapter_connection(kind, iface)
    if not con:
        return None, s
    eth = "802-3-ethernet"
    mtu_key = (eth if kind == "eth" else "802-11-wireless") + ".mtu"
    fields = ["ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns", mtu_key]
    if kind == "eth":
        fields += [eth + ".speed", eth + ".duplex"]
    rc, out, _ = run(["nmcli", "-t", "-f", ",".join(fields), "connection", "show", "id", con])
    if rc != 0:
        return con, s
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        v = v.replace("\\:", ":").strip()
        d[k] = "" if v == "--" else v
    s["method"] = d.get("ipv4.method") or "auto"
    s["address"] = d.get("ipv4.addresses", "").split(",")[0].strip()
    s["gateway"] = d.get("ipv4.gateway", "")
    s["dns"] = d.get("ipv4.dns", "")
    s["mtu"] = "auto" if d.get(mtu_key, "") in ("", "0", "auto") else d[mtu_key]
    speed = d.get(eth + ".speed", "")
    if kind == "eth" and speed not in ("", "0"):
        s["link"] = f"{speed}/{d.get(eth + '.duplex') or 'full'}"
    return con, s


def apply_adapter(kind, iface, s):
    """Write settings into the profile and bring it up. Returns (ok, message)."""
    con = adapter_connection(kind, iface)
    if not con:
        return False, "not connected to a Wi-Fi network"
    if s["method"] == "manual" and not s["address"]:
        return False, "STATIC needs an address"
    eth = "802-3-ethernet"
    if kind == "eth" and run(["nmcli", "connection", "show", "id", con])[0] != 0:
        # priority 50 beats the stock profile, so this one comes up on every cable
        rc, out, err = nmcli(["connection", "add", "type", "ethernet", "ifname", iface,
                              "con-name", con, "autoconnect", "yes",
                              "connection.autoconnect-priority", "50"])
        if rc != 0:
            return False, (err or out).strip()

    static = s["method"] == "manual"
    args = [
        "connection", "modify", "id", con,
        "ipv4.method", s["method"],
        "ipv4.addresses", s["address"] if static else "",
        "ipv4.gateway", s["gateway"] if static else "",
        "ipv4.dns", s["dns"] if static else "",
        (eth if kind == "eth" else "802-11-wireless") + ".mtu", "0" if s["mtu"] == "auto" else s["mtu"],
    ]
    if kind == "eth":
        # auto-negotiation stays on even for a fixed speed: NM then advertises
        # only that one mode, so the switch follows along instead of dropping
        # to half duplex the way it does against a hard-forced port
        speed, _, duplex = s["link"].partition("/")
        args += [
            eth + ".auto-negotiation", "yes",
            eth + ".speed", "0" if s["link"] == "auto" else speed,
            eth + ".duplex", "" if s["link"] == "auto" else duplex,
        ]
    rc, out, err = nmcli(args)
    if rc != 0:
        return False, (err or out).strip()
    if kind == "eth" and not carrier(iface):
        return True, "saved - takes effect when a cable is plugged in"
    rc, out, err = nmcli(["connection", "up", "id", con, "ifname", iface], timeout=45)
    return rc == 0, "applied" if rc == 0 else (err or out).strip()


# --------------------------------------------------------------- SSDP / UPnP discovery
# Merged from the standalone ssdp_probe.py prototype. Same conventions as
# the rest of this file: blocking, worker-thread friendly, emit(sections=...)
# as results arrive, returns the same [(title, [(field, value, status), ...])]
# shape. iface_ip() from the prototype is gone in favor of ip_info(), which
# already does the same lookup.
MCAST = ("239.255.255.250", 1900)
MAX_DESC_BYTES = 65536
DESC_WORKERS = 16


def _parse_ssdp_headers(raw):
    """SSDP replies are HTTP-ish: first line, then Key: value."""
    h = {}
    for line in raw.decode("utf-8", "replace").split("\r\n")[1:]:
        k, sep, v = line.partition(":")
        if sep:
            h[k.strip().upper()] = v.strip()
    return h


def _record_device(devices, ip, h):
    d = devices.setdefault(ip, {"ip": ip, "types": set(), "info": {}})
    d["server"] = h.get("SERVER") or d.get("server")
    d["location"] = h.get("LOCATION") or d.get("location")
    d["usn"] = h.get("USN") or d.get("usn")
    t = h.get("ST") or h.get("NT")
    if t:
        d["types"].add(t)
    return d


def search_ssdp(
    stop,
    src_ip=None,
    timeout=4,
    mx=2,
    st="ssdp:all",
    devices=None,
):
    """Send M-SEARCH and collect replies until timeout.

    src_ip pins the search to one interface, so it can't leak out of the
    hotspot when both NICs are up.
    """
    devices = devices if devices is not None else {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if src_ip:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                         socket.inet_aton(src_ip))
            s.bind((src_ip, 0))

        s.settimeout(0.5)

        pkt = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {MCAST[0]}:{MCAST[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {mx}\r\n"
            f"ST: {st}\r\n"
            "\r\n"
        ).encode()
        for _ in range(3):  # UDP: send a few, they get dropped
            if stop.is_set():
                return devices
            s.sendto(pkt, MCAST)
            stop.wait(0.1)

        end = time.time() + timeout
        while not stop.is_set() and time.time() < end:
            try:
                raw, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            _record_device(devices, addr[0], _parse_ssdp_headers(raw))
    finally:
        s.close()
    return devices


def listen_ssdp(stop, src_ip=None, seconds=30, devices=None):
    """Passive: devices announce themselves (NOTIFY) without being asked.
    Catches gear that ignores M-SEARCH."""
    devices = devices if devices is not None else {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", MCAST[1]))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(MCAST[0]) + socket.inet_aton(src_ip or "0.0.0.0"))
        s.settimeout(1)
        end = time.time() + seconds
        while not stop.is_set() and time.time() < end:
            try:
                raw, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            _record_device(devices, addr[0], _parse_ssdp_headers(raw))
    finally:
        s.close()
    return devices


def fetch_description(location, expect_ip=None, timeout=3):
    """GET the LOCATION XML and pull the names out of it.

    The URL comes from the device, so treat it as hostile: http(s) only, host
    must be the IP that answered, and the read is capped.
    """
    try:
        u = urllib.parse.urlparse(location)
        if u.scheme not in ("http", "https"):
            return {"error": "bad scheme"}
        if expect_ip and u.hostname != expect_ip:
            return {"error": f"points at {u.hostname}, not {expect_ip}"}
        req = urllib.request.Request(location, headers={"User-Agent": "pitester"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            xml = r.read(MAX_DESC_BYTES).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - any failure here just means "no info"
        return {"error": str(e)}

    out = {}
    for tag in ("friendlyName", "manufacturer", "modelName", "modelNumber",
                "serialNumber", "UDN"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S | re.I)
        if m:
            out[tag] = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
    return out


def fetch_all_descriptions(devices, stop, workers=DESC_WORKERS):
    """Sequential HTTP would be ~3 s per device; do them in parallel."""
    todo = [(ip, d["location"]) for ip, d in devices.items()
            if d.get("location") and not d["info"]]
    if not todo or stop.is_set():
        return devices

    def one(item):
        ip, loc = item
        return ip, ({} if stop.is_set() else fetch_description(loc, expect_ip=ip))

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
        for ip, info in ex.map(one, todo):
            devices[ip]["info"] = info
    return devices


def device_name(d):
    info = d.get("info") or {}
    return (info.get("friendlyName") or info.get("modelName")
            or (d.get("server") or "").split(" ")[-1] or NA)


def _ip_sort_key(ip):
    return tuple(int(p) for p in ip.split(".") if p.isdigit()) or (0,)


def ssdp_sections(devices, note=None):
    rows = []
    for ip in sorted(devices, key=_ip_sort_key):
        d = devices[ip]
        info = d.get("info") or {}
        bits = [device_name(d)]
        maker = " ".join(x for x in (info.get("manufacturer"), info.get("modelName")) if x)
        if maker and maker.lower() not in bits[0].lower():
            bits.append(maker)
        if d.get("types"):
            bits.append(f"{len(d['types'])} service type(s)")
        rows.append((ip, " - ".join(bits), ""))
    if not rows:
        rows.append(("Devices", "none answered - multicast may be filtered "
                                "(IGMP snooping with no querier), or nothing here "
                                "speaks UPnP", WARN))
    if note:
        rows.insert(0, ("Status", note, ""))
    return [(f"UPNP / SSDP ({len(devices)} device(s))", rows)]


def discover_ssdp(emit, stop, iface=IFACE, timeout=4, listen=0):
    """Top-level test, netcore-style. Returns sections."""
    ipv4 = ip_info(iface).get("ipv4") or []
    src = ipv4[0].split("/")[0] if ipv4 else None
    devices = {}

    emit(sections=ssdp_sections(devices, f"searching from {src or iface}..."))
    search_ssdp(stop, src_ip=src, timeout=timeout, devices=devices)
    if listen and not stop.is_set():
        emit(sections=ssdp_sections(devices, f"listening {listen}s for announcements..."))
        listen_ssdp(stop, src_ip=src, seconds=listen, devices=devices)

    emit(sections=ssdp_sections(devices, "fetching device details..."))
    fetch_all_descriptions(devices, stop)
    sections = ssdp_sections(devices)
    emit(sections=sections)
    return sections


# --------------------------------------------------------------- ARP sweep (arp-scan)
# Every host on the subnet has to answer ARP to talk at all, so this finds
# gear that ignores ping, LLDP and SSDP. arp-scan needs raw sockets, so it
# runs through sudo (rule added by install.sh).
MAX_SWEEP_PREFIX = 22  # /22 = 1024 addresses; anything bigger needs a second tap


def _sweep_sections(net, devices, own, gw, note=None, status=""):
    head = [("Subnet", f"{net} ({net.num_addresses} addresses)" if net else NA, "")]
    if note:
        head.append(("Status", note, status))
    rows = []
    if own:
        rows.append((own["ip"], f"{own['mac'] or NA}\nThis tester", ""))
    for ip in sorted(devices, key=_ip_sort_key):
        d = devices[ip]
        macs = sorted(d["macs"])
        text = f"{macs[0]}\n{d['vendor'] or 'unknown vendor'}"
        st = ""
        if ip == gw:
            text += " (gateway)"
        if len(macs) > 1:
            text += f"\nIP CONFLICT - also answered from {', '.join(macs[1:])}"
            st = WARN
        rows.append((ip, text, st))
    return [("ARP SWEEP", head), (f"DEVICES ({len(devices)})", rows)]


def _kill_on_stop(proc, stop, finished):
    while not finished.is_set():
        if stop.wait(0.5):
            proc.kill()
            return


def arp_sweep(emit, stop, iface=IFACE, allow_big=False):
    """emit(sections=...) as hosts answer. A subnet bigger than /22 isn't
    swept unless allow_big - instead it emits big=<subnet> so the UI can ask.
    Returns final sections."""
    ip = ip_info(iface)
    v4 = [a for a in ip.get("ipv4") or [] if not a.startswith("169.254.")]
    if not v4:
        secs = _sweep_sections(None, {}, None, None,
                               f"No IPv4 address on {iface} - plug in and wait for DHCP", FAIL)
        emit(sections=secs)
        return secs

    me = ipaddress.IPv4Interface(v4[0])
    net = me.network
    own = {"ip": str(me.ip), "mac": ip.get("mac")}
    gw = ip.get("gateway")
    devices = {}
    if net.prefixlen < MAX_SWEEP_PREFIX and not allow_big:
        secs = _sweep_sections(net, devices, own, gw,
                               f"Bigger than /{MAX_SWEEP_PREFIX} - tap SWEEP ALL to sweep "
                               f"anyway (slow, and noisy on a big network)", WARN)
        emit(sections=secs, big=str(net))
        return secs

    note = f"sweeping {net.num_addresses} addresses on {iface}..."
    emit(sections=_sweep_sections(net, devices, own, gw, note))
    cmd = ["sudo", "-n", "arp-scan", "--interface", iface, "--plain", "--retry", "2", str(net)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
    except OSError as e:
        secs = _sweep_sections(net, devices, own, gw, f"Could not start arp-scan: {e}", FAIL)
        emit(sections=secs)
        return secs

    finished = threading.Event()
    threading.Thread(target=_kill_on_stop, args=(proc, stop, finished), daemon=True).start()
    last_emit = 0.0
    for line in proc.stdout:
        # --plain lines are "IP<tab>MAC<tab>vendor"; a host that answers twice
        # gets a second line with "(DUP: n)" on the end
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        host, mac = parts[0], parts[1].lower()
        name = re.sub(r"\s*\(DUP: \d+\)\s*$", "", parts[2] if len(parts) > 2 else "").strip()
        if name in ("", "(Unknown)") or name.startswith("(Unknown:"):
            name = vendor(mac)
        d = devices.setdefault(host, {"macs": set(), "vendor": None})
        d["macs"].add(mac)
        d["vendor"] = d["vendor"] or name
        if time.time() - last_emit > 0.5:  # a burst of replies -> one redraw
            emit(sections=_sweep_sections(net, devices, own, gw, f"{note} {len(devices)} found"))
            last_emit = time.time()
    err = proc.stderr.read()
    rc = proc.wait()
    finished.set()

    # one cohesive decision (how the sweep ended), left as a single if/elif chain
    if stop.is_set():
        note, st = f"Stopped - {len(devices)} found before stopping", WARN
    elif "password is required" in err:
        note, st = "sudo rule missing - rerun install.sh", FAIL
    elif "command not found" in err:
        note, st = "arp-scan not installed - rerun install.sh", FAIL
    elif rc != 0:
        note, st = f"arp-scan exit {rc}: {err.strip() or 'no error text'}", FAIL
    elif not devices:
        note, st = "Nothing answered (isolated port, or nothing else on this subnet)", WARN
    else:
        note, st = f"Done - {len(devices)} device(s) answered", PASS
    secs = _sweep_sections(net, devices, own, gw, note, st)
    emit(sections=secs)
    return secs
