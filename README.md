# Citi Bike Migration

Where do Citi Bikes go over a day? Station-level "migration" across NYC, from public trip data
and live snapshots of the Citi Bike feed. Plan and open decisions: [DESIGN.md](DESIGN.md).
How the code works and why: [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

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
