"""Export the static data behind the Citi Bike Tides site (site/index.html).

Writes into site/:
  basemap.json               simplified NYC neighborhood outlines (no Staten Island: no Citi Bike there)
  data/trips/YYYY-MM.json    average weekday for the month, 15-min frames: net bikes riders moved in or
                             out of each station since midnight (from trip data; needs data/trips.duckdb)
  data/trips/index.json      the months available

Live data isn't exported here: the builder Lambda (collector/builder) writes it to the site bucket.
Usage: .venv/bin/python analysis/heatmap_export.py [--month 2026-08]
"""
import argparse
import datetime
import json
import pathlib

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "site"


def write(name: str, obj) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
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


def trips_weekday(con, month: str) -> None:
    first = datetime.date.fromisoformat(f"{month}-01")
    after = (first + datetime.timedelta(days=32)).replace(day=1)
    between = f"ts >= '{first}' AND ts < '{after}'"
    stations = con.sql("""
        SELECT sid, name, round(lat, 5) AS lat, round(lng, 5) AS lng FROM station_area
        WHERE borough <> 'NJ' ORDER BY sid
    """).fetchall()
    index = {s[0]: i for i, s in enumerate(stations)}
    # Average weekday cumulative net per station per 15-min slot. Days with no events at a station
    # in a slot still count (as 0), so divide by the number of weekdays rather than averaging rows.
    rows = con.sql(f"""
        WITH days AS (SELECT count(DISTINCT ts::date) AS n FROM events
                      WHERE {between} AND dayofweek(ts) BETWEEN 1 AND 5),
        q AS (SELECT sid, (hour(ts) * 60 + minute(ts)) // 15 AS slot, sum(d) AS n FROM events
              WHERE {between} AND dayofweek(ts) BETWEEN 1 AND 5
              GROUP BY ALL)
        SELECT sid, slot, sum(n) OVER (PARTITION BY sid ORDER BY slot) / (SELECT n FROM days) AS v FROM q
    """).fetchall()
    frames = [[0.0] * len(stations) for _ in range(96)]
    last = {}
    for sid, slot, v in sorted(rows, key=lambda r: (r[0], r[1])):
        if sid not in index:
            continue
        i = index[sid]
        prev_slot, prev_v = last.get(sid, (-1, 0.0))
        for s in range(prev_slot + 1, slot):  # carry the running total through empty slots
            frames[s][i] = prev_v
        frames[slot][i] = v
        last[sid] = (slot, v)
    for sid, (slot, v) in last.items():
        for s in range(slot + 1, 96):
            frames[s][index[sid]] = v
    label = first.strftime("%B %Y")
    trips = con.sql(f"SELECT count(*) FROM t WHERE {between.replace('ts', 'started_at')}").fetchone()[0]
    write(f"data/trips/{month}.json", {
        "kind": "change",
        "title": f"Average weekday, {label}",
        "note": f"Net bikes riders moved in or out of each station since midnight, averaged over "
                f"{label} weekdays ({trips / 1e6:.1f}M trips).",
        "times": [f"{(s + 1) * 15 // 60 % 24:02d}:{(s + 1) * 15 % 60:02d}" for s in range(96)],
        "stations": [[s[1], s[2], s[3]] for s in stations],
        "frames": [[round(v, 1) for v in f] for f in frames],
    })


def trips_index(month: str) -> None:
    path = OUT / "data" / "trips" / "index.json"
    months = set(json.loads(path.read_text())["months"]) if path.exists() else set()
    write("data/trips/index.json", {"months": sorted(months | {month}, reverse=True)})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--month", default="2026-08", help="YYYY-MM loaded in data/trips.duckdb")
    args = p.parse_args()
    con = duckdb.connect(str(ROOT / "data" / "trips.duckdb"), read_only=True)
    con.sql("INSTALL spatial; LOAD spatial;")
    basemap(con)
    trips_weekday(con, args.month)
    trips_index(args.month)


if __name__ == "__main__":
    main()
