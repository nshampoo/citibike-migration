-- Build the tables every chart reads from. Run: duckdb data/trips.duckdb < analysis/prepare.sql
-- Expects table t (raw trips) and data/nta2020.geojson (NYC neighborhood boundaries).
INSTALL spatial; LOAD spatial;

-- One location per station: median of reported trip start coordinates.
CREATE OR REPLACE TABLE stations AS
SELECT start_station_id AS sid, any_value(start_station_name) AS name,
       median(start_lat) AS lat, median(start_lng) AS lng, count(*) AS trips_started
FROM t WHERE start_station_id IS NOT NULL GROUP BY 1;

CREATE OR REPLACE TABLE nta AS
SELECT ntaname AS area, boroname AS borough, geom FROM ST_Read('data/nta2020.geojson');

-- Stations outside NYC (Jersey City/Hoboken) get borough 'NJ'.
CREATE OR REPLACE TABLE station_area AS
SELECT s.*, coalesce(n.area, 'Jersey City / Hoboken') AS area, coalesce(n.borough, 'NJ') AS borough
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
