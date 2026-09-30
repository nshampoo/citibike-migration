# Citi Bike Migration — Design (draft)

**Question:** Where do Citi Bikes go over a day or a week? Which neighborhoods empty out
and which fill up, when does that happen, and how much of it does Lyft move back by truck?

**Deliverable:** charts and maps that fit on a slide or in a blog post, plus reproducible
code in GitHub.

---

## 1. Data sources (checked 2026-09-29)

| Source | What it gives | Granularity | Limits |
| --- | --- | --- | --- |
| **Monthly trip files** — `s3://tripdata` (public; e.g. `202608-citibike-tripdata.zip`, ~1 GB/month) | Every ride: start/end station, start/end time, bike type, member/casual | One row per trip | Released about a month late. Only covers bikes moved by riders. |
| **Live GBFS** — `gbfs.lyft.com/gbfs/2.3/bkn/en/station_status.json` (no key, same feed Park It uses) | Bikes, e-bikes and docks at each station right now | ~2,500 stations, refreshed every 60 s | Current state only, with no history. **We can't backfill it**: a snapshot we don't take is gone. |
| `station_information.json` | Station name, lat/lon, capacity, region | Static-ish | — |
| `free_bike_status.json` | Would list individual bikes | — | **Empty for NYC**, so we can't follow a single bike. "Migration" has to be measured at station and neighborhood level. |

**Why we want both sources:**

- **Trips** show where riders took bikes.
- **Snapshots** show where bikes actually were.
- **Snapshot change − trip change = rebalancing**, i.e. bikes Lyft moved by van or truck. That hidden
  flow is the most original story here.

## 2. Plan in phases

### Phase 0: historical analysis (no AWS, starts today)
Download 1–3 months of trip files and analyze them locally with DuckDB and Python. This
produces the first charts within days and shows which visuals are worth automating.

### Phase 1: live collector (AWS, starts today because it can't be backfilled)

```
EventBridge Scheduler (interval: see "Snapshot interval" below)
      └─> Lambda (Python 3.13, ARM, 256 MB, ~2 s)
            ├─ GET station_status.json
            └─ PUT s3://<bucket>/raw/station_status/dt=YYYY-MM-DD/station_status_YYYYMMDDTHHMMSSZ.json.gz  (UTC)
```
- Raw gzipped JSON is about 76 KB per snapshot.
- A daily compaction job (a second Lambda, or run on a laptop at first) turns each day into
  one Parquet file in `curated/`. Athena or DuckDB queries that directly.
- `station_information` is captured once a day, since stations move and get added.
- Estimated cost: **under $1/month** at any interval (table below). **Nothing gets deployed until you approve it.**
- Infrastructure is defined as code with **AWS CDK (TypeScript)** in `infra/`, deployed from a
  laptop with the `personal` profile for now. Deploying through GitHub Actions (OIDC, no stored keys) is a later option.

We considered a GitHub Actions cron as a free collector, but it has a 5-minute minimum and is
often delayed or skipped. It's fine as a fallback, but not for the main data.

#### Snapshot interval (loose math, 2026-09-29)

| Interval | Snapshots/mo | Raw GB/mo | Compute + PUTs/mo |
| --- | --- | --- | --- |
| 1 min | 43,200 | 3.3 | ~$0.40 (compute in free tier) |
| 2 min | 21,600 | 1.6 | ~$0.20 |
| 5 min | 8,640 | 0.7 | ~$0.08 |
| 10 min | 4,320 | 0.3 | ~$0.04 |
| 60 min | 720 | 0.05 | ~$0.01 |

Cost doesn't decide this. What each interval gives up, from simulating August 2026 trips:

- **Hourly net flow per area: exact at every interval ≤ 60 min.** Net change adds up, so a
  snapshot on each hour boundary is enough.
- **Movement visible at one station:** 85% at 1 min, 62% at 5, 51% at 10, 27% at 60. The rest
  cancels out between snapshots, e.g. one bike in and one bike out. Trip data covers gross flow, so this
  matters mainly for rebalancing.
- **Tide shape:** the morning ramp takes about 2 hours, so 60-min snapshots give only ~2 points on it; 5–10 min gives a smooth curve.
- **Rush-hour turnover:** a typical station sees 1 event per 5 min (p90 4, p99 9). A truck
  dropping 10–20 bikes stands out clearly at 5–10 min.
- **Empty/full stretches: still unknown.** A 1-minute local poll (`collector/poll_local.py`) runs through
  the 9/30 morning rush, and we'll thin it to 5 and 10 min to measure what they miss.

### Phase 2: aggregation
Derived tables, recomputed each day:

**`station_snapshot`** (curated, one row per station per snapshot)
| ts | station_id | bikes | ebikes | docks | capacity |
| --- | --- | --- | --- | --- | --- |
| 2026-10-06 08:02 | 66de1516… | 12 | 1 | 18 | 32 |

**`hourly_net_flow`** (the core "migration" table)
| date | hour | area | bikes_start_of_hour | net_change | trip_net | rebalance_net |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-06 | 08 | Upper West Side | 1,240 | −310 | −355 | +45 |
| 2026-10-06 | 08 | Midtown East | 980 | +402 | +430 | −28 |

`area` = neighborhood (NYC NTA boundaries), so the story reads at a human scale instead of
across 2,500 dots.

**`station_stress`**: minutes per day each station spent empty or full.

### Phase 3: visuals for slides and a blog post
Each one is a single static image with one clear message:

0. **Citi Bike Tides site** ✅ (https://d2g10dtmnepqv0.cloudfront.net): animated station heatmaps for
   Live (1-minute snapshots), Trucks (overnight van stops, proof of concept) and Average weekday (trips).
1. **Tide chart** ✅ (`output/tide_2026-08.png`): cumulative net bikes per zone through the day,
   weekday vs. weekend. The zones are Manhattan areas that fill on weekday mornings, those that empty, and the outer boroughs.
2. **Hourly net-flow map:** neighborhoods shaded red (emptying) or blue (filling) at 8am and at 6pm,
   shown side by side.
3. **Flow lines:** the top 20 neighborhood-to-neighborhood corridors from trip data.
4. **"The invisible fleet":** trip-driven vs. truck-driven movement per borough. Started: the Trucks tab
   detects overnight van stops. First night (Sep 30): +398 bikes dropped at 26 stops, mostly Midtown
   transit hubs; −264 picked up at 21 stops, mostly Brooklyn nightlife areas. The full daytime split
   needs September trip data (early October) to compare the same days.
5. **Empty/full heatmap:** stations × hour of week.
6. An animated GIF/MP4 of a day of bikes moving, for the blog post.

Charts are generated by scripts in `analysis/` and exported as PNG/SVG. The site (above) runs on
CloudFront rather than GitHub Pages, because it reads data the builder writes to S3 every 5 minutes.

## 3. Repo layout (see docs/WALKTHROUGH.md for a file-by-file tour)
```
infra/        CDK (TypeScript): CitibikeCollector and CitibikeSite stacks, with tests
collector/    snapshot Lambda (every minute) + builder Lambda (hourly files, van stops)
site/         the website: index.html, base map, trip-month data
analysis/     DuckDB SQL + Python scripts → tables, charts, site data
output/       charts for slides and the blog, with their CSVs
docs/         WALKTHROUGH.md: how the code works and why
```

## 4. Open decisions
- Budget alarm ($5): need an alert email; check whether the account already has one
- How long to collect before the write-up (≥ 3 weeks gives weekday/weekend comparisons)
- Trucks detection: tune the 8-bikes-in-10-minutes rule once a week of nights is in, and check
  whether the same stations are restocked every night

Decided:
- ~~Snapshot interval~~: **1 minute** (2026-09-29). Matches the feed's 60 s refresh; coarser views can
  be derived later, finer ones can't. About $0.25–0.45/month.
- ~~Scope~~: **NYC + Jersey City + Hoboken**, one system in one feed. Only ~19 trips/day cross the Hudson.
- ~~AWS region and account~~: `us-east-1`, account 404933715334, profile `personal`
- ~~Site hosting~~: public CloudFront URL, no custom domain, live data refreshed every 5 minutes
