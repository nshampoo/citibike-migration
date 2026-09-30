"""Pack raw station_status snapshots into small hourly files the website loads.

Runs every 5 minutes. Rebuilds the current UTC hour, plus the previous hour during the first 10
minutes (so it picks up that hour's last snapshots). Writes to the site bucket:

  data/live/hours/YYYY-MM-DDTHH.json  {"hour", "times": [epoch s], "bikes": {station_id: [bikes | null]}}
  data/live/stations.json             {station_id: [name, lat, lon]}, every station ever seen
  data/live/index.json                {"hours": [...], "updated": epoch s}
  data/trucks/YYYY-MM-DD.json         overnight van stops for that night (midnight-6 AM New York time)
  data/trucks/index.json              {"nights": [...]}

"bikes" counts physical bikes (available + disabled), so a station flagging bikes as disabled
doesn't look like bikes leaving. null means the station was missing from that snapshot.

Backfill after a deploy: invoke with {"backfill_hours": 6} to rebuild the last 6 hours, and/or
{"truck_nights": ["2026-09-30"]} to recompute those nights' van stops.
Local test (reads the real raw bucket, writes files to a folder instead of the site bucket):
  RAW_BUCKET=<raw> SITE_DIR=/tmp/site python handler.py 3
"""
import gzip
import json
import os
import pathlib
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3

s3 = boto3.client("s3")
RAW = os.environ["RAW_BUCKET"]
SITE = os.environ.get("SITE_BUCKET")
SITE_DIR = os.environ.get("SITE_DIR")  # local testing only
INFO_URL = "https://gbfs.lyft.com/gbfs/2.3/bkn/en/station_information.json"
LIVE = "data/live"
TRUCKS = "data/trucks"
NIGHT_MINUTES = 360   # midnight to 6 AM: almost nobody rides, so big swings are vans
WINDOW = 10           # minutes
MIN_SWING = 8         # bikes gained or lost within one window to count as a van stop


# ---- storage helpers: the site bucket, or a local folder when testing ----
def site_get(key):
    try:
        if SITE_DIR:
            return json.loads(pathlib.Path(SITE_DIR, key).read_text())
        return json.loads(s3.get_object(Bucket=SITE, Key=key)["Body"].read())
    except (FileNotFoundError, s3.exceptions.NoSuchKey):
        return None


def site_put(key, obj, max_age=60):
    body = json.dumps(obj, separators=(",", ":"))
    if SITE_DIR:
        path = pathlib.Path(SITE_DIR, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return
    s3.put_object(Bucket=SITE, Key=key, Body=body.encode(), ContentType="application/json",
                  CacheControl=f"public, max-age={max_age}")


def site_list(prefix):
    if SITE_DIR:
        root = pathlib.Path(SITE_DIR, prefix)
        return sorted(str(p.relative_to(SITE_DIR)) for p in root.glob("*.json")) if root.exists() else []
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=SITE, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return sorted(keys)


def raw_list(prefix):
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=RAW, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return sorted(keys)


def raw_get(key):
    return json.loads(gzip.decompress(s3.get_object(Bucket=RAW, Key=key)["Body"].read()))


# ---- the work ----
def build_hour(hour):
    """One UTC hour of snapshots -> {"times", "bikes"}; None if there are no snapshots yet."""
    keys = raw_list(f"raw/station_status/dt={hour:%Y-%m-%d}/station_status_{hour:%Y%m%dT%H}")
    if not keys:
        return None
    with ThreadPoolExecutor(16) as pool:
        snaps = sorted(pool.map(raw_get, keys), key=lambda s: s["last_updated"])
    bikes = {}
    for t, snap in enumerate(snaps):
        for st in snap["data"]["stations"]:
            row = bikes.setdefault(st["station_id"], [None] * len(snaps))
            row[t] = st["num_bikes_available"] + st.get("num_bikes_disabled", 0)
    return {"hour": f"{hour:%Y-%m-%dT%H}", "times": [s["last_updated"] for s in snaps], "bikes": bikes}


def update_stations():
    """Merge the newest station list into stations.json, keeping stations that have since closed."""
    stations = site_get(f"{LIVE}/stations.json") or {}
    keys = raw_list("raw/station_information/")
    info = raw_get(keys[-1]) if keys else json.loads(urllib.request.urlopen(INFO_URL, timeout=20).read())
    for st in info["data"]["stations"]:
        stations[st["station_id"]] = [st["name"], round(st["lat"], 5), round(st["lon"], 5)]
    site_put(f"{LIVE}/stations.json", stations, max_age=300)
    return stations


def update_index():
    hours = [pathlib.PurePosixPath(k).stem for k in site_list(f"{LIVE}/hours/")]
    site_put(f"{LIVE}/index.json", {"hours": hours, "updated": int(datetime.now(timezone.utc).timestamp())})
    return len(hours)


# ---- overnight van stops ----
def ny_offset_hours(utc):
    """New York's UTC offset: -4 in daylight time (2nd Sunday of March to 1st Sunday of November, 2 AM
    local), else -5. Computed here so the Lambda doesn't depend on the OS time zone database."""
    march = datetime(utc.year, 3, 8, 7, tzinfo=timezone.utc)
    nov = datetime(utc.year, 11, 1, 6, tzinfo=timezone.utc)
    start = march + timedelta(days=(6 - march.weekday()) % 7)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if start <= utc < end else -5


def night_start_utc(night):
    """UTC instant of midnight at the start of `night` (a New York date, "YYYY-MM-DD")."""
    guess = datetime.fromisoformat(night).replace(tzinfo=timezone.utc) + timedelta(hours=5)
    return datetime.fromisoformat(night).replace(tzinfo=timezone.utc) - timedelta(hours=ny_offset_hours(guess))


def detect_night(night, stations):
    """Van stops for one night: a station whose bike count moves by MIN_SWING+ within a WINDOW-minute
    window. Back-to-back windows moving the same way at the same station are one stop."""
    start = night_start_utc(night)
    t0 = start.timestamp()
    series = {}  # station_id -> {window: [first value, last value]}
    last_time = None
    for h in range(NIGHT_MINUTES // 60):
        doc = site_get(f"{LIVE}/hours/{start + timedelta(hours=h):%Y-%m-%dT%H}.json")
        if not doc:
            continue
        for t, ts in enumerate(doc["times"]):
            w = int((ts - t0) // 60 // WINDOW)
            if not 0 <= w < NIGHT_MINUTES // WINDOW:
                continue
            last_time = ts
            for sid, row in doc["bikes"].items():
                v = row[t]
                if v is None:
                    continue
                win = series.setdefault(sid, {}).setdefault(w, [v, v])
                win[1] = v
    events = []
    for sid, wins in series.items():
        run = None
        for w in sorted(wins):
            a, b = wins[w]
            d = b - a
            if abs(d) >= MIN_SWING and run and run["w1"] == w and (d > 0) == (run["delta"] > 0):
                run.update(w1=w + 1, after=b, delta=b - run["before"])
                continue
            if run:
                events.append(run)
            run = {"sid": sid, "w0": w, "w1": w + 1, "before": a, "after": b, "delta": d} if abs(d) >= MIN_SWING else None
        if run:
            events.append(run)
    out = []
    for e in sorted(events, key=lambda e: (e["w0"], e["sid"])):
        name, lat, lon = stations.get(e["sid"], ["Unknown station", None, None])
        if lat is None:
            continue
        out.append({"name": name, "lat": lat, "lng": lon, "m0": e["w0"] * WINDOW, "m1": e["w1"] * WINDOW,
                    "before": e["before"], "after": e["after"], "delta": e["delta"]})
    done = last_time is not None and last_time >= t0 + (NIGHT_MINUTES - 2) * 60
    return {"night": night, "complete": done, "through": last_time, "events": out}


def update_trucks(now, nights, stations):
    for night in nights:
        site_put(f"{TRUCKS}/{night}.json", detect_night(night, stations), max_age=300)
    keys = [pathlib.PurePosixPath(k).stem for k in site_list(f"{TRUCKS}/")]
    site_put(f"{TRUCKS}/index.json", {"nights": sorted((k for k in keys if k != "index"), reverse=True)}, max_age=60)


def nights_due(now):
    """Tonight's file while the night is under way (and shortly after), so it can be watched live."""
    local = now + timedelta(hours=ny_offset_hours(now))
    return [f"{local:%Y-%m-%d}"] if local.hour < 7 else []


def handler(event=None, _context=None):
    now = datetime.now(timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    back = int((event or {}).get("backfill_hours", 2 if now.minute < 10 else 1))
    built = []
    for h in (hour - timedelta(hours=i) for i in range(back)):
        doc = build_hour(h)
        if doc:
            site_put(f"{LIVE}/hours/{doc['hour']}.json", doc)
            built.append(f"{doc['hour']} ({len(doc['times'])} snapshots)")
    stations = update_stations()
    nights = list((event or {}).get("truck_nights") or nights_due(now))
    if nights:
        update_trucks(now, nights, stations)
    return {"built": built, "hours_available": update_index(), "truck_nights": nights}


if __name__ == "__main__":
    # python handler.py [backfill_hours] [truck night ...]
    print(handler({"backfill_hours": int(sys.argv[1]) if len(sys.argv) > 1 else 2, "truck_nights": sys.argv[2:]}))
