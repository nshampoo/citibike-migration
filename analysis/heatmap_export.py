"""Export the data behind the animated heatmap (output/heatmap/index.html).

Writes three JSON files next to the page:
  basemap.json        simplified NYC neighborhood outlines (no Staten Island: no Citi Bike there)
  trips_weekday.json  average August 2026 weekday, 15-min frames: net bikes riders moved in/out of
                      each station since midnight (from trip data; needs data/trips.duckdb)
  live.json           1-minute frames from collector snapshots: bikes docked at each station
                      (needs data/live.duckdb, built by analysis/snapshots.sql)

Only NYC stations are kept (the base map has no New Jersey land to draw them on).
Usage: .venv/bin/python analysis/heatmap_export.py [--live-hours N]
"""
import argparse
import json
import pathlib

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "heatmap"


def write(name: str, obj) -> None:
    path = OUT / name
    path.write_text(json.dumps(obj, separators=(",", ":")))
    print(f"{name}: {path.stat().st_size / 1e3:,.0f} KB")


def basemap(con) -> None:
    rows = con.sql("""
        SELECT area, borough, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0002)) AS g
        FROM nta WHERE borough <> 'Staten Island'
    """).fetchall()

    def rnd(c):  # 4 decimals of a degree is ~10 m, plenty for a city-scale map
        return [rnd(x) for x in c] if isinstance(c[0], list) else [round(c[0], 4), round(c[1], 4)]

    write("basemap.json", [{"area": a, "borough": b, "coordinates": rnd(json.loads(g)["coordinates"]),
                            "type": json.loads(g)["type"]} for a, b, g in rows])


def trips_weekday(con) -> None:
    stations = con.sql("""
        SELECT sid, name, round(lat, 5) AS lat, round(lng, 5) AS lng FROM station_area
        WHERE borough <> 'NJ' ORDER BY sid
    """).fetchall()
    index = {s[0]: i for i, s in enumerate(stations)}
    # Average weekday cumulative net per station per 15-min slot. Days with no events at a station
    # in a slot still count (as 0), so divide by the number of weekdays rather than averaging rows.
    rows = con.sql("""
        WITH days AS (SELECT count(DISTINCT ts::date) AS n FROM events
                      WHERE ts >= '2026-08-01' AND ts < '2026-09-01' AND dayofweek(ts) BETWEEN 1 AND 5),
        q AS (SELECT sid, (hour(ts) * 60 + minute(ts)) // 15 AS slot, sum(d) AS n FROM events
              WHERE ts >= '2026-08-01' AND ts < '2026-09-01' AND dayofweek(ts) BETWEEN 1 AND 5
              GROUP BY ALL)
        SELECT sid, slot, sum(n) OVER (PARTITION BY sid ORDER BY slot) / (SELECT n FROM days) AS v FROM q
    """).fetchall()
    frames = [[0.0] * len(stations) for _ in range(96)]
    last = {}
    for sid, slot, v in sorted(rows, key=lambda r: (r[0], r[1])):
        if sid not in index:
            continue
        i = index[sid]
        start = last.get(sid, (-1, 0.0))
        for s in range(start[0] + 1, slot):  # carry the running total through empty slots
            frames[s][i] = start[1]
        frames[slot][i] = v
        last[sid] = (slot, v)
    for sid, (slot, v) in last.items():
        for s in range(slot + 1, 96):
            frames[s][index[sid]] = v
    write("trips_weekday.json", {
        "kind": "change",
        "title": "Average weekday, August 2026",
        "note": "Net bikes riders moved in or out of each station since midnight. From 5.2M trips.",
        "times": [f"{(s + 1) * 15 // 60 % 24:02d}:{(s + 1) * 15 % 60:02d}" for s in range(96)],
        "stations": [[s[1], s[2], s[3]] for s in stations],
        "frames": [[round(v, 1) for v in f] for f in frames],
    })


def live(con, hours: float) -> None:
    con.sql("INSTALL spatial; LOAD spatial;")
    stations = con.sql("""
        SELECT i.station_id, i.name, round(i.lat, 5), round(i.lon, 5)
        FROM station_info i JOIN trips.nta n ON ST_Contains(n.geom, ST_Point(i.lon, i.lat))
        ORDER BY i.station_id
    """).fetchall()
    index = {s[0]: i for i, s in enumerate(stations)}
    times = [t for (t,) in con.sql(f"""
        SELECT DISTINCT ts FROM station_snapshot
        WHERE ts >= (SELECT max(ts) FROM station_snapshot) - INTERVAL {hours * 60:.0f} MINUTE ORDER BY ts
    """).fetchall()]
    tindex = {t: i for i, t in enumerate(times)}
    frames = [[None] * len(stations) for _ in times]
    for ts, sid, bikes in con.sql(f"""
        SELECT ts, station_id, bikes + bikes_disabled FROM station_snapshot
        WHERE ts >= '{times[0]}'
    """).fetchall():
        if sid in index and ts in tindex:
            frames[tindex[ts]][index[sid]] = bikes
    for f in range(1, len(frames)):  # a station missing from one snapshot keeps its last count
        for i, v in enumerate(frames[f]):
            if v is None:
                frames[f][i] = frames[f - 1][i]
    frames = [[v or 0 for v in f] for f in frames]
    gaps = sum(1 for a, b in zip(times, times[1:]) if (b - a).total_seconds() > 90)
    write("live.json", {
        "kind": "level",
        "title": f"Live, {times[0]:%b %-d %-I:%M %p} to {times[-1]:%-I:%M %p}",
        "note": f"Bikes docked at each station, one frame per snapshot. {len(times)} snapshots"
                + (f", {gaps} gaps where the source laptop slept." if gaps else ", no gaps."),
        "times": [f"{t:%H:%M}" for t in times],
        "stations": [[s[1], s[2], s[3]] for s in stations],
        "frames": frames,
    })


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--live-hours", type=float, default=24)
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(ROOT / "data" / "trips.duckdb"), read_only=True)
    con.sql("INSTALL spatial; LOAD spatial;")
    basemap(con)
    trips_weekday(con)
    lcon = duckdb.connect(str(ROOT / "data" / "live.duckdb"), read_only=True)
    lcon.sql(f"ATTACH '{ROOT / 'data' / 'trips.duckdb'}' AS trips (READ_ONLY)")
    live(lcon, args.live_hours)


if __name__ == "__main__":
    main()
