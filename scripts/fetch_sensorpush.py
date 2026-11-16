#!/usr/bin/env python3
"""Pull current readings + 24h history from the SensorPush Cloud API.

Writes a compact JSON file the static dashboard (index.html) reads.
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
import urllib.parse
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


# ---------------------------------------------------------------- Nest (optional)
# Google Smart Device Management API. Only current state is available, so the
# script keeps its own rolling history by carrying forward the previous file.
NEST_HISTORY_HOURS = 24
HVAC_CODE = {"OFF": 0, "HEATING": 1, "COOLING": 2}


def c_to_f(c):
    return None if c is None else round(c * 9 / 5 + 32, 1)


def http_json(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", "User-Agent": "home-dashboard/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method or 'GET'} {url.split('?')[0]} -> HTTP {e.code}: {detail}") from None


def nest_thermostats(project_id, client_id, client_secret, refresh_token):
    tok = http_json("https://oauth2.googleapis.com/token", method="POST",
                    data=urllib.parse.urlencode({
                        "client_id": client_id, "client_secret": client_secret,
                        "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})["access_token"]
    devices = http_json(
        f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{project_id}/devices",
        headers={"Authorization": f"Bearer {tok}"}).get("devices", [])
    out = []
    for d in devices:
        if d.get("type") != "sdm.devices.types.THERMOSTAT":
            continue
        t = d.get("traits", {})
        g = lambda trait, key: t.get(f"sdm.devices.traits.{trait}", {}).get(key)
        rooms = [r.get("displayName") for r in d.get("parentRelations", []) if r.get("displayName")]
        eco = g("ThermostatEco", "mode")
        in_eco = eco == "MANUAL_ECO"
        out.append({
            "id": d["name"].rsplit("/", 1)[-1],
            "name": g("Info", "customName") or (rooms[0] if rooms else "Thermostat"),
            "online": g("Connectivity", "status") == "ONLINE",
            "ambient": c_to_f(g("Temperature", "ambientTemperatureCelsius")),
            "humidity": g("Humidity", "ambientHumidityPercent"),
            "mode": g("ThermostatMode", "mode"),          # HEAT / COOL / HEATCOOL / OFF
            "eco": in_eco,
            "hvac": g("ThermostatHvac", "status"),        # HEATING / COOLING / OFF
            "heat_to": c_to_f(g("ThermostatEco", "heatCelsius") if in_eco else g("ThermostatTemperatureSetpoint", "heatCelsius")),
            "cool_to": c_to_f(g("ThermostatEco", "coolCelsius") if in_eco else g("ThermostatTemperatureSetpoint", "coolCelsius")),
        })
    return out


def add_nest(data, now, prev_path):
    """Attach Nest thermostats + rolling history to data. Never raises."""
    prev = {}
    if prev_path and os.path.exists(prev_path):
        try:
            with open(prev_path) as f:
                prev = {n["id"]: n for n in json.load(f).get("nest", [])}
        except Exception as e:  # noqa: BLE001
            print(f"nest: could not read previous file ({e})", file=sys.stderr)
    env = [os.environ.get(k) for k in ("NEST_PROJECT_ID", "NEST_CLIENT_ID", "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN")]
    if not all(env):
        if prev:
            data["nest"] = list(prev.values())
        return
    try:
        current = nest_thermostats(*env)
        data.pop("nest_error", None)
    except Exception as e:  # noqa: BLE001
        print(f"nest: fetch failed, keeping previous data ({e})", file=sys.stderr)
        data["nest_error"] = str(e)[:200]
        data["nest"] = list(prev.values())
        return
    cutoff = now.timestamp() - NEST_HISTORY_HOURS * 3600
    out = []
    for n in current:
        hist = [h for h in prev.get(n["id"], {}).get("history", []) if h[0] >= cutoff]
        hist.append([int(now.timestamp()), n["ambient"], HVAC_CODE.get(n["hvac"], 0), n["heat_to"], n["cool_to"]])
        out.append({**n, "observed": iso(now), "history": hist})
    data["nest"] = out
    print(f"nest: {len(out)} thermostat(s)")


# ------------------------------------------------- Heat-call check + alerts
# During a heat call, a working furnace pushes the supply vent well above the
# return (this house: +35-42F). If the supply hasn't been at least
# MIN_DELTA_F above the return at any point in the last WINDOW_MIN minutes of
# a call that has lasted WINDOW_MIN or longer, raise an alert.
SUPPLY_NAME = os.environ.get("SUPPLY_SENSOR", "Living Room Supply")
RETURN_NAME = os.environ.get("RETURN_SENSOR", "Living Room Return")
MIN_DELTA_F = float(os.environ.get("HEAT_MIN_DELTA_F", "15"))
WINDOW_MIN = int(os.environ.get("HEAT_WINDOW_MIN", "20"))


def heat_check(data, now, prev_heat):
    nest = (data.get("nest") or [None])[0]
    if not nest or not nest.get("history"):
        return None
    hist = nest["history"]
    calling = hist[-1][2] == 1
    out = {"calling": calling, "supply_sensor": SUPPLY_NAME, "return_sensor": RETURN_NAME,
           "min_delta_f": MIN_DELTA_F, "window_min": WINDOW_MIN, "alert": False}
    if calling:
        i = len(hist) - 1
        while i > 0 and hist[i - 1][2] == 1:
            i -= 1
        out["since"] = hist[i][0]
        out["since_is_lower_bound"] = i == 0    # call began before our history did
        out["minutes"] = int((now.timestamp() - hist[i][0]) // 60)
    by_name = {s["name"]: s for s in data.get("sensors", [])}
    sup, ret = by_name.get(SUPPLY_NAME), by_name.get(RETURN_NAME)
    if sup and ret and sup.get("latest") and ret.get("latest"):
        out["supply_f"] = sup["latest"]["temperature"]
        out["return_f"] = ret["latest"]["temperature"]
        if out["supply_f"] is not None and out["return_f"] is not None:
            out["delta_f"] = round(out["supply_f"] - out["return_f"], 1)
        r = {p[0]: p[1] for p in ret.get("series", []) if p[1] is not None}
        cutoff = now.timestamp() - WINDOW_MIN * 60
        deltas = [p[1] - r[p[0]] for p in sup.get("series", []) if p[0] >= cutoff - BUCKET_MIN * 60
                  and p[1] is not None and p[0] in r]
        if out.get("delta_f") is not None:
            deltas.append(out["delta_f"])
        out["max_delta_recent_f"] = round(max(deltas), 1) if deltas else None
        if calling and out.get("minutes", 0) >= WINDOW_MIN and out["max_delta_recent_f"] is not None \
                and out["max_delta_recent_f"] < MIN_DELTA_F:
            out["alert"] = True
    # Carry alert bookkeeping so a push goes out once per heat call.
    prev_heat = prev_heat or {}
    out["notified_for"] = prev_heat.get("notified_for")
    return out


def notify(heat, now):
    topic = os.environ.get("NTFY_TOPIC")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    key = heat.get("since")
    if heat["alert"] and heat.get("notified_for") != key:
        mins = heat.get("minutes", 0)
        title = "Heat on but no warm air"
        msg = (f"The thermostat has called for heat for {'at least ' if heat.get('since_is_lower_bound') else ''}"
               f"{mins} min, but {heat['supply_sensor']} is {heat.get('supply_f')}F, only "
               f"{heat.get('max_delta_recent_f')}F above {heat['return_sensor']} "
               f"(a running furnace is usually 30F+ above). Check the furnace.")
        prio = "high"
        new_mark = key
    elif not heat["alert"] and heat.get("notified_for") and (not heat["calling"] or (heat.get("max_delta_recent_f") or 0) >= MIN_DELTA_F):
        title, prio, new_mark = "Heat is working again", "default", None
        msg = f"{heat['supply_sensor']} is {heat.get('supply_f')}F" + (
            f", {heat.get('delta_f')}F above {heat['return_sensor']}." if heat.get("delta_f") is not None else ".")
        if not heat["calling"]:
            title, msg = "Heat call ended", "The thermostat stopped calling for heat. Earlier no-heat alert cleared."
    else:
        return
    if not topic:
        print(f"alert ({title}) not pushed: NTFY_TOPIC not set", file=sys.stderr)
        heat["notified_for"] = new_mark
        return
    try:
        req = urllib.request.Request(f"{server}/{topic}", data=msg.encode(), method="POST", headers={
            "Title": title, "Priority": prio, "Tags": "warning" if prio == "high" else "white_check_mark",
            "Click": "https://home.esemay.com", "User-Agent": "home-dashboard/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
        heat["notified_for"] = new_mark
        print(f"pushed alert: {title}")
    except Exception as e:  # noqa: BLE001  (retry next run)
        print(f"push failed: {e}", file=sys.stderr)


def mock_nest(now):
    hist = []
    for m in range(NEST_HISTORY_HOURS * 60, -1, -15):
        ts = now - timedelta(minutes=m)
        heating = (ts.minute // 15) % 3 == 0 and 5 <= ts.hour <= 22
        hist.append([int(ts.timestamp()), round(67 + math.sin(m / 90) * 1.2, 1), 1 if heating else 0, 68.0, None])
    return [{"id": "mock-nest", "name": "Hallway", "online": True, "ambient": hist[-1][1], "humidity": 44,
             "mode": "HEAT", "eco": False, "hvac": "HEATING" if hist[-1][2] else "OFF",
             "heat_to": 68.0, "cool_to": None, "observed": iso(now), "history": hist}]


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
    if "--mock" in sys.argv:
        data["nest"] = mock_nest(now)
    else:
        add_nest(data, now, os.environ.get("PREV_JSON"))
    prev_heat = None
    pj = os.environ.get("PREV_JSON")
    if pj and os.path.exists(pj):
        try:
            with open(pj) as f:
                prev_heat = json.load(f).get("heat")
        except Exception:  # noqa: BLE001
            pass
    heat = heat_check(data, now, prev_heat)
    if heat:
        if "--mock" not in sys.argv:
            notify(heat, now)
        data["heat"] = heat
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {out_path}: {len(sensors)} sensors, {os.path.getsize(out_path)} bytes")


if __name__ == "__main__":
    main()
