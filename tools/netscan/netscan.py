#!/usr/bin/env python3
"""Home network inventory: find every device on the LAN and work out what it
needs for a vendor-independent dashboard.

Run it on a machine INSIDE your home network (laptop, Pi, NAS). Standard
library only, no root needed.

    python3 netscan.py                      # scan the /24 you're on
    python3 netscan.py --cidr 192.168.1.0/24
    python3 netscan.py --out ~/netscan-output --oui   # also look up MAC vendors

What it does, in order:
  1. TCP sweep of the subnet (a "connection refused" counts as alive)
  2. Reads the OS ARP table for MAC addresses
  3. mDNS / Bonjour service discovery (what devices advertise about themselves)
  4. SSDP / UPnP discovery + device description XML (manufacturer, model)
  5. Port probe of each live host, HTTP title/server grab
  6. A few safe, unauthenticated vendor probes (Shelly, Hue, Roku, Cast)
  7. Classifies each device and lists its local-control path and what you
     need to supply (credentials, pairing, local keys)

Writes inventory.json and inventory.md to --out. Nothing is sent anywhere
except the optional one-time IEEE OUI download (--oui).

THE OUTPUT DESCRIBES YOUR NETWORK. Do not commit it to a public repo.
"""
import argparse
import concurrent.futures as cf
import csv
import ipaddress
import json
import os
import platform
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SWEEP_PORTS = [80, 443, 22, 8080, 554, 62078, 7000, 8008, 9100, 445, 53]
PROBE_PORTS = {
    21: "ftp", 22: "ssh", 23: "telnet", 53: "dns", 80: "http", 139: "netbios",
    443: "https", 445: "smb", 502: "modbus", 548: "afp", 554: "rtsp",
    631: "ipp", 1400: "sonos", 1883: "mqtt", 1900: "upnp-http", 2020: "onvif-alt",
    3000: "http-alt", 3689: "daap", 4070: "spotify-connect", 5000: "http-5000",
    5001: "https-5001", 5683: "coap", 6053: "esphome-api", 6466: "androidtv-remote",
    6467: "androidtv-pair", 6668: "tuya", 7000: "airplay", 8000: "http-8000",
    8001: "samsung-tv", 8002: "samsung-tv-tls", 8008: "cast", 8009: "cast-tls",
    8060: "roku-ecp", 8080: "http-8080", 8081: "http-8081", 8089: "channels-dvr", 8123: "home-assistant",
    8443: "https-8443", 8883: "mqtt-tls", 8899: "onvif-alt2", 9000: "http-9000",
    9100: "jetdirect", 9999: "kasa", 10001: "ubiquiti", 32400: "plex",
    49152: "upnp", 55443: "yeelight", 62078: "apple-lockdown",
}
HTTP_PORTS = [80, 8080, 8000, 8081, 8089, 5000, 3000, 9000]
HTTPS_PORTS = [443, 8443, 5001]

# Local LAN devices use self-signed certs. This context is ONLY used to read
# page titles from devices on your own subnet. No credentials are ever sent.
LAN_TLS = ssl.create_default_context()
LAN_TLS.check_hostname = False
LAN_TLS.verify_mode = ssl.CERT_NONE


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- network basics

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
        return s.getsockname()[0]
    finally:
        s.close()


def tcp_state(ip, port, timeout):
    """'open', 'closed' (host answered RST: it exists), or None (no answer)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        rc = s.connect_ex((ip, port))
    except OSError:
        return None
    finally:
        s.close()
    if rc == 0:
        return "open"
    if rc in (111, 61, 10061):  # ECONNREFUSED on Linux / macOS / Windows
        return "closed"
    return None


def sweep(hosts, timeout, workers):
    alive = set()

    def check(ip):
        for p in SWEEP_PORTS:
            if tcp_state(ip, p, timeout):
                return ip
        return None

    with cf.ThreadPoolExecutor(workers) as ex:
        for r in ex.map(check, hosts):
            if r:
                alive.add(r)
    return alive


def read_arp():
    """ip -> mac from the OS neighbour cache (populated by the sweep)."""
    table = {}
    if os.path.exists("/proc/net/arp"):
        with open("/proc/net/arp") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    table[parts[0]] = parts[3].lower()
        return table
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})", out):
        mac = ":".join(b.zfill(2) for b in re.split("[:-]", m.group(2))).lower()
        if mac not in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            table[m.group(1)] = mac
    return table


def mac_is_random(mac):
    """Locally administered bit set = randomized/private MAC (phones, laptops)."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def load_oui(cache_dir):
    path = os.path.join(cache_dir, "oui.csv")
    if not os.path.exists(path):
        log("downloading IEEE OUI list (one time, ~3 MB)...")
        req = urllib.request.Request("https://standards-oui.ieee.org/oui/oui.csv",
                                     headers={"User-Agent": "home-netscan/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    vendors = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and len(row[1]) == 6:
                vendors[row[1].upper()] = row[2].strip()
    return vendors


# ---------------------------------------------------------------- mDNS

MDNS_ADDR = ("224.0.0.251", 5353)
T_A, T_PTR, T_TXT, T_AAAA, T_SRV = 1, 12, 16, 28, 33
KNOWN_SERVICES = [
    "_http._tcp", "_hap._tcp", "_matter._tcp", "_matterc._udp", "_googlecast._tcp",
    "_airplay._tcp", "_raop._tcp", "_spotify-connect._tcp", "_sonos._tcp",
    "_hue._tcp", "_esphomelib._tcp", "_shelly._tcp", "_ipp._tcp", "_ipps._tcp",
    "_printer._tcp", "_pdl-datastream._tcp", "_scanner._tcp", "_uscan._tcp",
    "_smb._tcp", "_afpovertcp._tcp", "_device-info._tcp", "_companion-link._tcp",
    "_homekit._tcp", "_mqtt._tcp", "_home-assistant._tcp", "_amzn-wplay._tcp",
    "_androidtvremote2._tcp", "_roku._tcp", "_elg._tcp", "_wled._tcp",
    "_nanoleafapi._tcp", "_ewelink._tcp", "_miio._udp", "_meshcop._udp",
    "_trel._udp", "_sleep-proxy._udp", "_workstation._tcp", "_ssh._tcp",
    "_rtsp._tcp", "_daap._tcp", "_touch-able._tcp", "_axis-video._tcp",
]


def dns_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        out += bytes([len(label)]) + label.encode()
    return out + b"\x00"


def dns_query(names, qtype=T_PTR):
    pkt = struct.pack("!HHHHHH", 0, 0, len(names), 0, 0, 0)
    for n in names:
        pkt += dns_name(n) + struct.pack("!HH", qtype, 0x8001)  # QU bit: ask for unicast reply
    return pkt


def read_name(buf, off, depth=0):
    labels = []
    jumped_to = None
    while True:
        if off >= len(buf) or depth > 20:
            raise ValueError("bad name")
        ln = buf[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", buf[off:off + 2])[0] & 0x3FFF
            if jumped_to is None:
                jumped_to = off + 2
            off = ptr
            depth += 1
            continue
        labels.append(buf[off + 1:off + 1 + ln].decode("utf-8", "replace"))
        off += 1 + ln
    return ".".join(labels), (jumped_to if jumped_to is not None else off)


def parse_dns(buf):
    """Return list of (name, type, rdata) for every record in the packet."""
    qd, an, ns, ar = struct.unpack("!HHHH", buf[4:12])
    off = 12
    for _ in range(qd):
        _, off = read_name(buf, off)
        off += 4
    recs = []
    for _ in range(an + ns + ar):
        name, off = read_name(buf, off)
        rtype, _, _, rdlen = struct.unpack("!HHIH", buf[off:off + 10])
        off += 10
        rd = buf[off:off + rdlen]
        val = None
        try:
            if rtype == T_A and rdlen == 4:
                val = socket.inet_ntoa(rd)
            elif rtype == T_AAAA and rdlen == 16:
                val = socket.inet_ntop(socket.AF_INET6, rd)
            elif rtype == T_PTR:
                val = read_name(buf, off)[0]
            elif rtype == T_SRV:
                _, _, port = struct.unpack("!HHH", rd[:6])
                val = (read_name(buf, off + 6)[0], port)
            elif rtype == T_TXT:
                kv, i = {}, 0
                while i < len(rd):
                    ln = rd[i]
                    s = rd[i + 1:i + 1 + ln].decode("utf-8", "replace")
                    i += 1 + ln
                    if s:
                        k, _, v = s.partition("=")
                        kv[k] = v
                val = kv
        except (ValueError, struct.error, OSError):
            val = None
        recs.append((name, rtype, val))
        off += rdlen
    return recs


def mdns_round(questions, listen):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.settimeout(0.3)
    sock.bind(("", 0))
    for i in range(0, len(questions), 12):  # keep packets small
        try:
            sock.sendto(dns_query(questions[i:i + 12]), MDNS_ADDR)
        except OSError as e:
            log(f"mDNS send failed: {e}")
    packets = []
    end = time.time() + listen
    while time.time() < end:
        try:
            data, (src, _) = sock.recvfrom(9000)
            packets.append((src, data))
        except socket.timeout:
            continue
        except OSError:
            break
    sock.close()
    return packets


def mdns_discover(listen=3.0):
    """ip -> {'services': {type: {instance, port, txt}}, 'hostnames': set}"""
    found = {}
    types = set(KNOWN_SERVICES)
    for _, data in mdns_round(["_services._dns-sd._udp.local"], listen):
        try:
            for name, rtype, val in parse_dns(data):
                if rtype == T_PTR and val and name.startswith("_services._dns-sd"):
                    types.add(val.replace(".local", ""))
        except (ValueError, struct.error):
            pass
    packets = mdns_round([t + ".local" for t in sorted(types)], listen)
    for src, data in packets:
        try:
            recs = parse_dns(data)
        except (ValueError, struct.error):
            continue
        dev = found.setdefault(src, {"services": {}, "hostnames": set()})
        srv, txt = {}, {}
        for name, rtype, val in recs:
            if rtype == T_SRV and val:
                srv[name] = val
                dev["hostnames"].add(val[0].replace(".local", ""))
            elif rtype == T_TXT and isinstance(val, dict):
                txt[name] = val
            elif rtype == T_A and name.endswith(".local"):
                dev["hostnames"].add(name.replace(".local", ""))
        for name, rtype, val in recs:
            if rtype == T_PTR and val and not name.startswith("_services"):
                stype = name.replace(".local", "")
                instance = val.replace("." + name, "") if val.endswith(name) else val
                dev["services"][stype] = {
                    "instance": instance,
                    "port": srv.get(val, (None, None))[1],
                    "txt": txt.get(val, {}),
                }
    return found


# ---------------------------------------------------------------- SSDP / UPnP

def ssdp_discover(listen=3.0):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n').encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.3)
    try:
        for _ in range(2):
            sock.sendto(msg, ("239.255.255.250", 1900))
    except OSError as e:
        log(f"SSDP send failed: {e}")
        return {}
    found = {}
    end = time.time() + listen
    while time.time() < end:
        try:
            data, (src, _) = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        hdrs = {}
        for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
            k, _, v = line.partition(":")
            if v:
                hdrs[k.strip().lower()] = v.strip()
        d = found.setdefault(src, {"locations": set(), "servers": set(), "st": set()})
        if hdrs.get("location"):
            d["locations"].add(hdrs["location"])
        if hdrs.get("server"):
            d["servers"].add(hdrs["server"])
        if hdrs.get("st"):
            d["st"].add(hdrs["st"])
    sock.close()
    return found


def upnp_description(url):
    try:
        body = http_get(url, timeout=3, limit=200_000)
        root = ET.fromstring(body)
    except Exception:
        return None
    out = {}
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("friendlyName", "manufacturer", "modelName", "modelNumber",
                   "modelDescription", "deviceType") and tag not in out and el.text:
            out[tag] = el.text.strip()
    return out or None


# ---------------------------------------------------------------- HTTP probes

def http_get(url, timeout=3, limit=65536, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "home-netscan/1.0", **(headers or {})})
    ctx = LAN_TLS if url.startswith("https") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(limit)


def http_head_info(ip, port, https=False):
    url = f"{'https' if https else 'http'}://{ip}:{port}/"
    req = urllib.request.Request(url, headers={"User-Agent": "home-netscan/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3, context=LAN_TLS if https else None) as r:
            server, body, status = r.headers.get("Server"), r.read(32768), r.status
    except urllib.error.HTTPError as e:
        server, body, status = e.headers.get("Server"), b"", e.code
        auth = e.headers.get("WWW-Authenticate")
        if auth:
            server = (server or "") + f" [auth: {auth.split()[0]}]"
    except Exception:
        return None
    m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = m.group(1).decode("utf-8", "replace").strip()[:80] if m else None
    return {"port": port, "status": status, "server": server, "title": title}


def json_probe(url):
    try:
        return json.loads(http_get(url, timeout=3))
    except Exception:
        return None


def vendor_probes(ip, open_ports):
    """Safe, unauthenticated, read-only identity endpoints."""
    hits = {}
    if 80 in open_ports:
        j = json_probe(f"http://{ip}/shelly")
        if isinstance(j, dict) and ("type" in j or "gen" in j or "model" in j):
            hits["shelly"] = {k: j.get(k) for k in ("type", "model", "gen", "name", "fw", "fw_id", "auth", "auth_en") if k in j}
        j = json_probe(f"http://{ip}/api/config")
        if isinstance(j, dict) and "bridgeid" in j:
            hits["hue"] = {k: j.get(k) for k in ("name", "modelid", "swversion", "apiversion")}
        j = json_probe(f"http://{ip}/json/info")
        if isinstance(j, dict) and "ver" in j and "leds" in j:
            hits["wled"] = {"name": j.get("name"), "ver": j.get("ver")}
    if 8060 in open_ports:
        try:
            x = ET.fromstring(http_get(f"http://{ip}:8060/query/device-info", timeout=3))
            hits["roku"] = {k: (x.findtext(k) or "") for k in ("vendor-name", "model-name", "friendly-device-name", "software-version")}
        except Exception:
            pass
    if 8008 in open_ports:
        j = json_probe(f"http://{ip}:8008/setup/eureka_info?params=name,build_info,device_info")
        if isinstance(j, dict):
            hits["cast"] = {"name": j.get("name"), "model": (j.get("device_info") or {}).get("model_name")}
    return hits


def probe_host(ip, timeout):
    ports = {}
    for p in PROBE_PORTS:
        if tcp_state(ip, p, timeout) == "open":
            ports[p] = PROBE_PORTS[p]
    http = []
    for p in HTTP_PORTS:
        if p in ports:
            info = http_head_info(ip, p)
            if info:
                http.append(info)
    for p in HTTPS_PORTS:
        if p in ports:
            info = http_head_info(ip, p, https=True)
            if info:
                http.append(info)
    try:
        rdns = socket.gethostbyaddr(ip)[0]
    except OSError:
        rdns = None
    return {"ports": ports, "http": http, "rdns": rdns, "vendor_probe": vendor_probes(ip, ports)}


# ---------------------------------------------------------------- classification

# Each rule: (test(device) -> bool, category, local_path, local_control, needs)
# local_control: "full" = documented/stable local API, "partial" = local but
# limited or reverse-engineered, "cloud" = no practical local path.
def _svc(d, *names):
    return any(n in d["mdns"].get("services", {}) for n in names)


def _txt(d, field):
    return " ".join(str(s.get("txt", {}).get(field, "")) for s in d["mdns"].get("services", {}).values())


def _text(d):
    bits = [d.get("vendor") or "", d.get("rdns") or "", " ".join(d["mdns"].get("hostnames", []))]
    for u in d["upnp"]:
        bits += [u.get("manufacturer", ""), u.get("modelName", ""), u.get("friendlyName", "")]
    bits += [h.get("server") or "" for h in d["http"]] + [h.get("title") or "" for h in d["http"]]
    bits += list(d.get("ssdp_servers", []))
    bits += [s.get("instance", "") for s in d["mdns"].get("services", {}).values()]
    return " ".join(bits).lower()


def _has(d, *words):
    t = _text(d)
    return any(w in t for w in words)


RULES = [
    (lambda d: "home-assistant" in d["ports"].values() or _svc(d, "_home-assistant._tcp"),
     "Home hub", "Home Assistant REST/WebSocket API (port 8123)", "full",
     ["long-lived access token (Profile > Security)"]),
    (lambda d: "hue" in d["vendor_probe"] or _svc(d, "_hue._tcp"),
     "Lighting bridge", "Hue local REST API v2 (https://bridge/clip/v2) + event stream", "full",
     ["press bridge link button once to mint an app key"]),
    (lambda d: "shelly" in d["vendor_probe"] or _svc(d, "_shelly._tcp"),
     "Relay / power meter", "Shelly local HTTP RPC + optional MQTT", "full",
     ["nothing if auth is off; device password if auth is on"]),
    (lambda d: _svc(d, "_esphomelib._tcp") or "esphome-api" in d["ports"].values(),
     "ESPHome device", "ESPHome native API (port 6053) or its web server", "full",
     ["API encryption key from the device's YAML"]),
    (lambda d: "wled" in d["vendor_probe"] or _svc(d, "_wled._tcp"),
     "LED controller", "WLED JSON API (/json/state)", "full", ["nothing"]),
    (lambda d: "roku" in d["vendor_probe"] or "roku-ecp" in d["ports"].values(),
     "TV / streamer", "Roku ECP on :8060 (state, apps, remote keys)", "full",
     ["enable 'Control by mobile apps' in Roku settings"]),
    (lambda d: "cast" in d["vendor_probe"] or _svc(d, "_googlecast._tcp"),
     "Cast speaker / TV", "Google Cast protocol (pychromecast) for now-playing + volume", "partial",
     ["nothing for read-only media state"]),
    (lambda d: _svc(d, "_sonos._tcp") or "sonos" in d["ports"].values() or _has(d, "sonos"),
     "Speaker", "Sonos local UPnP/SOAP on :1400 (now-playing, volume, groups)", "full", ["nothing"]),
    (lambda d: "rtsp" in d["ports"].values() or _svc(d, "_rtsp._tcp", "_axis-video._tcp")
     or "onvif-alt" in d["ports"].values() or "onvif-alt2" in d["ports"].values(),
     "Camera", "RTSP stream (re-served via go2rtc for browsers) + ONVIF for events", "full",
     ["camera username/password", "RTSP path for the stream", "ONVIF enabled in camera settings"]),
    (lambda d: _svc(d, "_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp")
     or "ipp" in d["ports"].values() or "jetdirect" in d["ports"].values(),
     "Printer", "IPP get-printer-attributes (ink/toner, state) or SNMP Printer-MIB", "full",
     ["nothing for read-only status"]),
    (lambda d: "kasa" in d["ports"].values() or _has(d, "tp-link", "kasa", "tapo"),
     "Plug / switch (TP-Link)", "Kasa local protocol on :9999; newer Tapo uses KLAP", "partial",
     ["Kasa: nothing", "Tapo/KLAP firmware: your TP-Link cloud email + password (used locally)"]),
    (lambda d: "tuya" in d["ports"].values(),
     "Tuya-based device", "Tuya local protocol (tinytuya)", "partial",
     ["device ID + local key, pulled once from Tuya IoT developer account"]),
    (lambda d: "yeelight" in d["ports"].values(),
     "Light (Yeelight)", "Yeelight LAN control on :55443", "full", ["enable LAN Control in Yeelight app"]),
    (lambda d: _svc(d, "_nanoleafapi._tcp"),
     "Light panels", "Nanoleaf OpenAPI on :16021", "full", ["hold power button to mint an auth token"]),
    (lambda d: _svc(d, "_elg._tcp"),
     "Light (Elgato)", "Elgato Key Light REST on :9123", "full", ["nothing"]),
    (lambda d: _has(d, "synology") or "http-5000" in d["ports"].values() and _has(d, "diskstation"),
     "NAS", "Synology DSM API or SNMP (disks, volumes, temps)", "full",
     ["read-only DSM user, or SNMP community"]),
    (lambda d: "channels-dvr" in d["ports"].values() or _has(d, "channels dvr"),
     "DVR server (Channels)", "Channels DVR HTTP API on :8089 (recordings, guide, jobs, disk)", "full",
     ["nothing on the LAN; remote access must be enabled to reach it from outside"]),
    (lambda d: "plex" in d["ports"].values(),
     "Media server", "Plex API on :32400 (sessions, libraries)", "full", ["X-Plex-Token"]),
    (lambda d: "mqtt" in d["ports"].values() or _svc(d, "_mqtt._tcp"),
     "MQTT broker", "Subscribe to topics; becomes the backbone for everything else", "full",
     ["broker username/password if set"]),
    (lambda d: _has(d, "ubiquiti", "unifi") or "ubiquiti" in d["ports"].values(),
     "Network gear (UniFi)", "UniFi Network API (clients, throughput) or SNMP", "full",
     ["local read-only admin account or API key"]),
    (lambda d: d.get("is_gateway"),
     "Router / gateway", "UPnP IGD (WAN throughput), SNMP if supported, or router's own API", "partial",
     ["router admin login; enable SNMP if it has it"]),
    (lambda d: _svc(d, "_matter._tcp", "_matterc._udp"),
     "Matter device", "Matter over IP via python-matter-server or Home Assistant", "full",
     ["Matter pairing code (from device or existing controller's 'share' option)"]),
    (lambda d: _svc(d, "_hap._tcp") and not _has(d, "apple"),
     "HomeKit accessory", "HomeKit Accessory Protocol (aiohomekit), fully local", "full",
     ["8-digit HomeKit setup code; must be removed from Apple Home first, or use Matter if supported"]),
    (lambda d: _svc(d, "_amzn-wplay._tcp") or _has(d, "amazon", "echo", "fire tv"),
     "Amazon Echo / Fire TV", "No practical local API for Echo; Fire TV via ADB", "cloud",
     ["Fire TV: enable ADB debugging"]),
    (lambda d: _svc(d, "_androidtvremote2._tcp") or "androidtv-remote" in d["ports"].values(),
     "Android / Google TV", "Android TV Remote protocol v2", "partial", ["one-time on-screen pairing code"]),
    (lambda d: "samsung-tv" in d["ports"].values(),
     "Samsung TV", "Samsung Tizen WebSocket API on :8001/8002", "partial", ["accept pairing prompt on TV"]),
    (lambda d: _svc(d, "_airplay._tcp", "_raop._tcp", "_companion-link._tcp") or "apple-lockdown" in d["ports"].values() or _has(d, "apple"),
     "Apple device", "Apple TV: pyatv (now-playing). iPhone/Mac: presence only", "partial",
     ["Apple TV: on-screen pairing PIN"]),
    (lambda d: _has(d, "ecobee", "nest", "ring", "wyze", "myq", "chamberlain", "rachio", "sensorpush"),
     "Cloud-tethered device", "Vendor cloud only, or a local workaround (see notes)", "cloud",
     ["vendor account credentials or API key"]),
    (lambda d: _svc(d, "_smb._tcp", "_afpovertcp._tcp") or "smb" in d["ports"].values(),
     "Computer / file share", "Presence + SNMP/node_exporter if you want metrics", "partial",
     ["install an agent (node_exporter, Glances) to get real metrics"]),
    (lambda d: "ssh" in d["ports"].values(),
     "Linux / server", "node_exporter or Glances API for CPU, disk, temps", "full",
     ["install node_exporter or Glances"]),
]


def classify(d):
    matches = []
    for test, cat, path, level, needs in RULES:
        try:
            if test(d):
                matches.append({"category": cat, "local_path": path, "local_control": level, "needs": needs})
        except Exception:
            continue
    if not matches:
        if d.get("mac_random"):
            matches.append({"category": "Phone / laptop (private MAC)", "local_path": "Presence only (seen / not seen)",
                            "local_control": "partial", "needs": ["give it a DHCP reservation or turn off private MAC for this network to track it"]})
        else:
            matches.append({"category": "Unknown", "local_path": "Identify first: check router's client list, power-cycle test",
                            "local_control": "unknown", "needs": ["a name"]})
    return matches


SECURITY_FLAGS = {
    "telnet": "Telnet open: plaintext login. Disable if the device allows it.",
    "ftp": "FTP open: plaintext. Check whether you need it.",
    "upnp-http": "UPnP service exposed. Make sure UPnP is off on the router's WAN side.",
    "tuya": "Tuya device: phones home to Tuya cloud. Candidate for a VLAN or local firmware.",
    "modbus": "Modbus open: no authentication by design.",
}


def security_notes(d):
    notes = [SECURITY_FLAGS[v] for v in d["ports"].values() if v in SECURITY_FLAGS]
    for h in d["http"]:
        if h["port"] in (80, 8080) and h.get("status") == 200 and not any(x["port"] in HTTPS_PORTS for x in d["http"]):
            notes.append(f"Admin page on plain HTTP :{h['port']} with no HTTPS alternative.")
            break
    sh = d["vendor_probe"].get("shelly", {})
    if sh and not (sh.get("auth") or sh.get("auth_en")):
        notes.append("Shelly with no password: anyone on the LAN can switch it.")
    return notes


# ---------------------------------------------------------------- report

def best_name(d):
    for u in d["upnp"]:
        if u.get("friendlyName"):
            return u["friendlyName"]
    for key in ("roku", "cast", "shelly", "hue", "wled"):
        v = d["vendor_probe"].get(key, {})
        n = v.get("friendly-device-name") or v.get("name")
        if n:
            return n
    for s in d["mdns"].get("services", {}).values():
        if s.get("instance") and not s["instance"].startswith("_"):
            return s["instance"]
    if d["mdns"].get("hostnames"):
        return sorted(d["mdns"]["hostnames"])[0]
    return d.get("rdns") or d["ip"]


def best_model(d):
    for u in d["upnp"]:
        if u.get("modelName"):
            return " ".join(x for x in (u.get("manufacturer"), u.get("modelName"), u.get("modelNumber")) if x)
    for key, fields in (("roku", ("vendor-name", "model-name")), ("cast", ("model",)),
                        ("shelly", ("model", "type")), ("hue", ("modelid",))):
        v = d["vendor_probe"].get(key)
        if v:
            parts = [str(v[f]) for f in fields if v.get(f)]
            if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
                parts = parts[1:]  # "Roku" + "Roku Ultra" -> "Roku Ultra"
            return " ".join(parts)
    md = _txt(d, "md").strip() or _txt(d, "model").strip()
    if md:
        return md
    return d.get("vendor") or ""


def write_markdown(path, inv):
    L = [f"# Home network inventory", "",
         f"Scanned {inv['scanned_at']} from {inv['scanner_ip']} on {inv['cidr']}. "
         f"{len(inv['devices'])} devices found.", "",
         "**Private.** This file maps your network. Keep it off public repos.", "",
         "## Summary", "", "| IP | Name | Model / vendor | Category | Local control |", "|---|---|---|---|---|"]
    for d in inv["devices"]:
        c = d["classification"][0]
        L.append(f"| {d['ip']} | {d['name']} | {d['model'] or '-'} | {c['category']} | {c['local_control']} |")
    counts = {}
    for d in inv["devices"]:
        lvl = d["classification"][0]["local_control"]
        counts[lvl] = counts.get(lvl, 0) + 1
    L += ["", "Local control: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), ""]
    flagged = [d for d in inv["devices"] if d["security"]]
    if flagged:
        L += ["## Security flags", ""]
        for d in flagged:
            for n in d["security"]:
                L.append(f"- **{d['name']}** ({d['ip']}): {n}")
        L.append("")
    L += ["## Devices", ""]
    for d in inv["devices"]:
        L += [f"### {d['name']} ({d['ip']})", ""]
        L.append(f"- MAC: `{d['mac'] or 'unknown'}`" + (" (randomized)" if d["mac_random"] else "")
                 + (f", vendor: {d['vendor']}" if d["vendor"] else ""))
        if d["model"]:
            L.append(f"- Model: {d['model']}")
        if d["ports"]:
            L.append("- Open ports: " + ", ".join(f"{p}/{n}" for p, n in sorted(d["ports"].items(), key=lambda x: int(x[0]))))
        if d["mdns"].get("services"):
            L.append("- Advertises: " + ", ".join(sorted(d["mdns"]["services"])))
        for h in d["http"]:
            L.append(f"- Web :{h['port']} -> {h['status']} {h.get('title') or ''} {('(' + h['server'] + ')') if h.get('server') else ''}".rstrip())
        for c in d["classification"]:
            L.append(f"- **{c['category']}** [{c['local_control']}]: {c['local_path']}")
            for n in c["needs"]:
                L.append(f"  - needs: {n}")
        L.append("")
    with open(path, "w") as f:
        f.write("\n".join(L))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cidr", help="subnet to scan, default: the /24 this machine is on")
    ap.add_argument("--out", default="netscan-output", help="output directory (default ./netscan-output)")
    ap.add_argument("--oui", action="store_true", help="download IEEE MAC vendor list once and use it")
    ap.add_argument("--timeout", type=float, default=0.6, help="TCP connect timeout seconds")
    ap.add_argument("--workers", type=int, default=128)
    ap.add_argument("--listen", type=float, default=3.0, help="seconds to listen for mDNS/SSDP replies")
    a = ap.parse_args()

    me = local_ip()
    net = ipaddress.ip_network(a.cidr or f"{me}/24", strict=False)
    if net.num_addresses > 1024:
        sys.exit(f"{net} is {net.num_addresses} addresses. Pass a /22 or smaller.")
    os.makedirs(a.out, exist_ok=True)
    hosts = [str(h) for h in net.hosts()]
    log(f"scanner {me}, scanning {net} ({len(hosts)} addresses) on {platform.system()}")

    with cf.ThreadPoolExecutor(3) as ex:
        f_mdns = ex.submit(mdns_discover, a.listen)
        f_ssdp = ex.submit(ssdp_discover, a.listen)
        alive = sweep(hosts, a.timeout, a.workers)
        mdns, ssdp = f_mdns.result(), f_ssdp.result()
    log(f"sweep: {len(alive)} answered TCP; mDNS: {len(mdns)} responders; SSDP: {len(ssdp)} responders")

    time.sleep(0.5)
    arp = {ip: mac for ip, mac in read_arp().items() if ipaddress.ip_address(ip) in net}
    candidates = (alive | set(arp) | {ip for ip in mdns if _in(ip, net)} | {ip for ip in ssdp if _in(ip, net)}) - {me}
    log(f"{len(candidates)} devices total, probing ports...")

    oui = {}
    if a.oui:
        try:
            oui = load_oui(a.out)
        except Exception as e:
            log(f"OUI lookup unavailable: {e}")

    gateway_guess = str(next(net.hosts()))
    with cf.ThreadPoolExecutor(min(32, max(1, len(candidates)))) as ex:
        probes = dict(zip(sorted(candidates), ex.map(lambda ip: probe_host(ip, a.timeout), sorted(candidates))))

    upnp_cache = {}
    devices = []
    for ip in sorted(candidates, key=lambda x: ipaddress.ip_address(x)):
        mac = arp.get(ip)
        s = ssdp.get(ip, {})
        upnp = []
        for loc in sorted(s.get("locations", [])):
            if loc not in upnp_cache:
                upnp_cache[loc] = upnp_description(loc)
            if upnp_cache[loc]:
                upnp.append(upnp_cache[loc])
        m = mdns.get(ip, {"services": {}, "hostnames": set()})
        p = probes[ip]
        d = {
            "ip": ip, "mac": mac, "mac_random": bool(mac and mac_is_random(mac)),
            "vendor": oui.get(mac.replace(":", "")[:6].upper()) if mac and oui else None,
            "rdns": p["rdns"], "ports": p["ports"], "http": p["http"], "vendor_probe": p["vendor_probe"],
            "mdns": {"services": m["services"], "hostnames": sorted(m["hostnames"])},
            "upnp": upnp, "ssdp_servers": sorted(s.get("servers", [])), "is_gateway": ip == gateway_guess,
        }
        d["name"] = best_name(d)
        d["model"] = best_model(d)
        d["classification"] = classify(d)
        d["security"] = security_notes(d)
        devices.append(d)

    inv = {"scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "scanner_ip": me, "cidr": str(net), "devices": devices}
    jp, mp = os.path.join(a.out, "inventory.json"), os.path.join(a.out, "inventory.md")
    with open(jp, "w") as f:
        json.dump(inv, f, indent=1, default=list)
    write_markdown(mp, inv)
    log(f"wrote {jp} and {mp}")


def _in(ip, net):
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


if __name__ == "__main__":
    main()
