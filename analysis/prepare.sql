-- Build the tables every chart reads from. Run: duckdb data/trips.duckdb < analysis/prepare.sql
-- Expects the unzipped monthly CSVs in data/raw/ (NYC and JC- files), data/nta2020.geojson (NYC neighborhoods)
-- and data/nj_place/ (Census place boundaries for New Jersey, for Jersey City and Hoboken).
-- Stop at the first error. By default the duckdb CLI prints the error and runs the next statement,
-- which silently leaves stale tables behind.
.bail on
INSTALL spatial; LOAD spatial;

-- Station IDs must be read as text. DuckDB infers types per file, and in a file where every
-- ID happens to look numeric it reads a DOUBLE, turning '5997.10' into '5997.1' (~1,100 trips/day
-- lost their destination in August 2026 before this was fixed).
CREATE OR REPLACE TABLE t AS
SELECT * FROM read_csv('data/raw/*.csv', types = {'start_station_id': 'VARCHAR', 'end_station_id': 'VARCHAR'});

-- One location per station: median of reported coordinates across trip starts and ends,
-- so stations that only appear as a destination still get a location.
CREATE OR REPLACE TABLE stations AS
SELECT sid, any_value(name) AS name, median(lat) AS lat, median(lng) AS lng,
       count(*) FILTER (WHERE is_start) AS trips_started
FROM (
  SELECT start_station_id AS sid, start_station_name AS name, start_lat AS lat, start_lng AS lng, true AS is_start FROM t
  UNION ALL
  SELECT end_station_id, end_station_name, end_lat, end_lng, false FROM t
) WHERE sid IS NOT NULL GROUP BY sid;

-- Areas: NYC neighborhoods, plus Jersey City and Hoboken (the New Jersey side of Citi Bike) as
-- one area each, grouped as borough 'New Jersey'. The two sources use different coordinate systems
-- (WGS84 vs NAD83, about 1 m apart here), which DuckDB refuses to mix; ::GEOMETRY drops the tag.
CREATE OR REPLACE TABLE nta AS
SELECT ntaname AS area, boroname AS borough, geom::GEOMETRY AS geom FROM ST_Read('data/nta2020.geojson')
UNION ALL
SELECT NAME, 'New Jersey', geom::GEOMETRY FROM ST_Read('data/nj_place/cb_2024_34_place_500k.shp')
WHERE NAME IN ('Jersey City', 'Hoboken');

-- A station outside every area (none as of August 2026) is kept, labeled 'Other'.
CREATE OR REPLACE TABLE station_area AS
SELECT s.*, coalesce(n.area, 'Other') AS area, coalesce(n.borough, 'Other') AS borough
FROM stations s LEFT JOIN nta n ON ST_Contains(n.geom, ST_Point(s.lng, s.lat));

-- Every bike arrival (+1) and departure (-1) at a station.
CREATE OR REPLACE TABLE events AS
SELECT start_station_id AS sid, started_at AS ts, -1 AS d, rideable_type, member_casual FROM t WHERE start_station_id IS NOT NULL
UNION ALL
SELECT end_station_id, ended_at, 1, rideable_type, member_casual FROM t WHERE end_station_id IS NOT NULL;

-- Core migration table: net bikes delivered by riders per area per hour.
CREATE OR REPLACE TABLE hourly_net_flow AS
SELECT date_trunc('hour', e.ts) AS hour, a.area, a.borough,
       sum(e.d) AS trip_net, sum((e.d = 1)::int) AS arrivals, sum((e.d = -1)::int) AS departures
FROM events e JOIN station_area a USING (sid)
WHERE e.ts >= '2026-08-01' AND e.ts < '2026-09-01'
GROUP BY ALL;
