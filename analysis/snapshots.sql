-- Turn raw station_status snapshots into one tidy table: one row per station per snapshot.
-- Run: duckdb data/live.duckdb < analysis/snapshots.sql
-- Reads every snapshot under data/live/ (local poller) and data/s3/ (synced from the collector bucket:
--   aws s3 sync s3://<bucket>/raw/ data/s3/ --profile personal).
-- Needs data/station_information.json for names, locations, capacity and the trip-data station IDs.

CREATE OR REPLACE TABLE station_snapshot AS
WITH raw AS (
  SELECT to_timestamp(last_updated) AS ts_utc, unnest(data.stations) AS s
  FROM read_json(['data/live/*.json.gz', 'data/s3/station_status/*/*.json.gz'], union_by_name = true)
)
SELECT DISTINCT ON (ts_utc, s.station_id)  -- local and S3 can hold the same minute
  timezone('America/New_York', ts_utc) AS ts,  -- local time, to line up with trip data
  s.station_id,
  s.num_bikes_available  AS bikes,
  s.num_ebikes_available AS ebikes,
  s.num_docks_available  AS docks,
  s.num_bikes_disabled   AS bikes_disabled,
  s.num_docks_disabled   AS docks_disabled,
  s.is_renting = 1 AND s.is_installed = 1 AS active
FROM raw;

CREATE OR REPLACE TABLE station_info AS
SELECT unnest(data.stations, recursive := true)
FROM read_json('data/station_information.json');

-- Per station per minute: how many bikes physically appeared or vanished since the previous snapshot.
-- Rides move 1-3 bikes per station per minute (9/29 evening: 99.6% of nonzero changes). Physical
-- jumps >= 5 are rare (max 7 that evening), so trucks likely load over several minutes and need a
-- multi-minute detector, not a one-minute threshold. Unverified until we have morning data.
-- Use physical bikes (available + disabled): stations sometimes flip bikes between the two
-- (e.g. Clinton St & Joralemon St, 9/29: 21 available -> 6 available + 25 disabled in one minute),
-- which changes `bikes` by 15 with no bike moving.
CREATE OR REPLACE TABLE station_change AS
SELECT *,
  bikes + bikes_disabled AS physical_bikes,
  (bikes + bikes_disabled) - lag(bikes + bikes_disabled) OVER w AS d_bikes,
  epoch(ts - lag(ts) OVER w) / 60 AS minutes_since_prev
FROM station_snapshot
WINDOW w AS (PARTITION BY station_id ORDER BY ts);
