# Citi Bike Migration

Where do Citi Bikes go over a day? Station-level "migration" across NYC, from public trip data
and live snapshots of the Citi Bike feed. Plan and open decisions: [DESIGN.md](DESIGN.md).
How the code works and why: [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

## Live site

**https://d2g10dtmnepqv0.cloudfront.net** (open straight to live data: [/#live](https://d2g10dtmnepqv0.cloudfront.net/#live))

An animated heatmap of NYC, Jersey City and Hoboken. *Average weekday* plays a month of trip data;
*Live* plays today's 1-minute snapshots (bikes docked, or change over the last hour) and updates
every 5 minutes.

## What's running in AWS

Account `404933715334`, `us-east-1`, CLI profile `personal`. Both stacks are CDK code in [`infra/`](infra/).

| Stack | What it does |
|---|---|
| `CitibikeCollector` | Saves the Citi Bike feed every minute to S3. The raw bucket is kept even if the stack is deleted. |
| `CitibikeSite` | The site: CloudFront, its bucket, and a builder that packs new snapshots into hourly files every 5 minutes. |

Update the site after editing `site/` (from `infra/`): `npx cdk deploy CitibikeSite`.
Add a trip month: load it (below), run `.venv/bin/python analysis/heatmap_export.py --month YYYY-MM`, then deploy.

![Weekday tide](output/tide_2026-08.png)

## Reproduce the August 2026 charts

```sh
brew install duckdb
python3 -m venv .venv && .venv/bin/pip install duckdb pandas matplotlib pyarrow
mkdir -p data/raw && curl -o data/raw/202608.zip https://s3.amazonaws.com/tripdata/202608-citibike-tripdata.zip
(cd data/raw && unzip 202608.zip)
curl -L -o data/nta2020.geojson "https://data.cityofnewyork.us/api/geospatial/9nt8-h7nd?method=export&format=GeoJSON"
curl -o data/raw/JC-202608.zip https://s3.amazonaws.com/tripdata/JC-202608-citibike-tripdata.csv.zip && (cd data/raw && unzip JC-202608.zip)
curl -o data/nj_place.zip https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_34_place_500k.zip && unzip data/nj_place.zip -d data/nj_place
duckdb data/trips.duckdb < analysis/prepare.sql
.venv/bin/python analysis/tide_chart.py
```

`data/` is gitignored (about 2 GB). Charts and their CSVs go to `output/`.
