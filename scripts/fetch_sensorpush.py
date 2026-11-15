#!/usr/bin/env python3
"""Pull current readings + 24h history from the SensorPush Cloud API.

Writes a compact JSON file the static dashboard (home/index.html) reads.
Standard library only, so the GitHub Action needs no pip install.

Usage:
  SENSORPUSH_EMAIL=... SENSORPUSH_PASSWORD=... python3 scripts/fetch_sensorpush.py out/latest.json
  python3 scripts/fetch_sensorpush.py --mock out/latest.json   # fake data for local preview

API reference: https://www.sensorpush.com/gateway-cloud-api
"""
import json
import math
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.sensorpush.com/api/v1"
HISTORY_HOURS = 24
BUCKET_MIN = 10  # chart resolution; keeps the JSON small
MEASURES = ["temperature", "humidity", "dewpoint", "barometric_pressure"]


def post(path, body, token=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "home-dashboard/1.0",
            **({"Authorization": token} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"POST {path} -> HTTP {e.code}: {detail}") from None


def login(email, password):
    auth = post("/oauth/authorize", {"email": email, "password": password})["authorization"]
    return post("/oauth/accesstoken", {"authorization": auth})["accesstoken"]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def r1(v):
    return None if v is None else round(float(v), 1)


def summarize(sensor_meta, samples, now):
    """samples: list of dicts with 'observed' + measures, any order."""
    pts = sorted(
        ((parse_ts(s["observed"]), s) for s in samples if s.get("observed")),
        key=lambda x: x[0],
    )
    latest = pts[-1][1] if pts else None

    # Average into fixed buckets for the chart.
    buckets = {}
    for ts, s in pts:
        key = int(ts.timestamp() // (BUCKET_MIN * 60))
        b = buckets.setdefault(key, {"t": [], "h": []})
        if s.get("temperature") is not None:
            b["t"].append(s["temperature"])
        if s.get("humidity") is not None:
            b["h"].append(s["humidity"])
    series = []
    for key in sorted(buckets):
        b = buckets[key]
        series.append([
            key * BUCKET_MIN * 60,
            r1(sum(b["t"]) / len(b["t"])) if b["t"] else None,
            r1(sum(b["h"]) / len(b["h"])) if b["h"] else None,
        ])

    temps = [s["temperature"] for _, s in pts if s.get("temperature") is not None]
    hums = [s["humidity"] for _, s in pts if s.get("humidity") is not None]

    # Trend: latest temp vs ~1 hour earlier.
    trend = None
    if latest and latest.get("temperature") is not None:
        target = pts[-1][0] - timedelta(hours=1)
        earlier = [s for ts, s in pts if ts <= target and s.get("temperature") is not None]
        if earlier:
            trend = r1(latest["temperature"] - earlier[-1]["temperature"])

    return {
        "id": sensor_meta.get("id"),
        "name": sensor_meta.get("name") or sensor_meta.get("id"),
        "type": sensor_meta.get("type"),
        "active": sensor_meta.get("active", True),
        "battery_voltage": sensor_meta.get("battery_voltage"),
        "rssi": sensor_meta.get("rssi"),
        "latest": None if not latest else {
            "observed": latest["observed"],
            "temperature": r1(latest.get("temperature")),
            "humidity": r1(latest.get("humidity")),
            "dewpoint": r1(latest.get("dewpoint")),
            "barometric_pressure": r1(latest.get("barometric_pressure")),
        },
        "trend_1h": trend,
        "stats_24h": {
            "temp_min": r1(min(temps)) if temps else None,
            "temp_max": r1(max(temps)) if temps else None,
            "hum_min": r1(min(hums)) if hums else None,
            "hum_max": r1(max(hums)) if hums else None,
        },
        "series": series,
    }


def fetch_live():
    email = os.environ.get("SENSORPUSH_EMAIL")
    password = os.environ.get("SENSORPUSH_PASSWORD")
    if not email or not password:
        sys.exit("Set SENSORPUSH_EMAIL and SENSORPUSH_PASSWORD")
    token = login(email, password)

    sensors = post("/devices/sensors", {}, token)
    gateways = post("/devices/gateways", {}, token)
    active_ids = [sid for sid, s in sensors.items() if s.get("active", True)]

    now = datetime.now(timezone.utc)
    body = {
        "sensors": active_ids,
        "startTime": iso(now - timedelta(hours=HISTORY_HOURS)),
        "endTime": iso(now),
        "limit": HISTORY_HOURS * 60 + 60,  # per sensor; ~1 reading/min
        "measures": MEASURES,
    }
    try:
        samples = post("/samples", body, token)
    except RuntimeError as e:
        # Some accounts reject measures they have no hardware for; fall back to defaults.
        print(f"samples with measures failed ({e}); retrying with defaults", file=sys.stderr)
        body.pop("measures")
        samples = post("/samples", body, token)

    by_sensor = samples.get("sensors", {})
    out_sensors = []
    for sid in active_ids:
        meta = dict(sensors[sid])
        meta.setdefault("id", sid)
        out_sensors.append(summarize(meta, by_sensor.get(sid, []), now))

    out_gateways = [
        {
            "name": g.get("name"),
            "last_seen": g.get("last_seen"),
            "paired": g.get("paired"),
            "message": g.get("message"),
        }
        for g in gateways.values()
    ]
    return now, out_sensors, out_gateways, bool(samples.get("truncated"))


def fetch_mock():
    now = datetime.now(timezone.utc)
    rooms = [("Living Room", 70, 42), ("Bedroom", 67, 45), ("Basement", 61, 58),
             ("Garage", 48, 63), ("Outside", 41, 71), ("Wine Fridge", 55, 60)]
    out = []
    for i, (name, base_t, base_h) in enumerate(rooms):
        samples = []
        for m in range(0, HISTORY_HOURS * 60, 1):
            ts = now - timedelta(minutes=m)
            phase = (ts.hour + ts.minute / 60) / 24 * 2 * math.pi
            amp = 8 if name in ("Outside", "Garage") else 2
            t = base_t + amp * math.sin(phase - 2) + random.uniform(-0.3, 0.3)
            h = base_h - amp * 0.8 * math.sin(phase - 2) + random.uniform(-0.8, 0.8)
            dp = t - (100 - h) / 5 * 1.8
            samples.append({"observed": iso(ts), "temperature": t, "humidity": h, "dewpoint": dp})
        if name == "Wine Fridge":  # simulate a sensor that stopped reporting
            samples = samples[90:]
        meta = {"id": f"mock{i}", "name": name, "active": True,
                "battery_voltage": 2.4 if name == "Garage" else 2.95, "rssi": -70 - i * 3}
        out.append(summarize(meta, samples, now))
    gws = [{"name": "Home Gateway", "last_seen": iso(now - timedelta(minutes=1)), "paired": True}]
    return now, out, gws, False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_path = args[0] if args else "latest.json"
    now, sensors, gateways, truncated = fetch_mock() if "--mock" in sys.argv else fetch_live()
    data = {
        "generated_at": iso(now),
        "units": {"temperature": "F", "humidity": "%", "barometric_pressure": "inHg"},
        "bucket_minutes": BUCKET_MIN,
        "truncated": truncated,
        "sensors": sorted(sensors, key=lambda s: (s["name"] or "").lower()),
        "gateways": gateways,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {out_path}: {len(sensors)} sensors, {os.path.getsize(out_path)} bytes")


if __name__ == "__main__":
    main()
