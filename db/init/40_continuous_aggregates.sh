#!/bin/bash
# Three-tier hierarchical continuous aggregates (raw -> hourly -> daily).
# Uses HOURLY_COMPRESS_AFTER / DAILY_COMPRESS_AFTER from the container
# environment — see 30_compression_retention.sh for why this is a .sh
# wrapper rather than a plain .sql file.
#
# Each aggregate stores sum/count rather than a pre-computed average so a
# coarser rollup can compute an exact average instead of an
# average-of-averages. Neither metric_hourly nor metric_daily ever gets an
# add_retention_policy() — they're kept forever; only compression is applied
# to aged chunks.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
CREATE MATERIALIZED VIEW metric_hourly
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 hour', time) AS hour,
  node_id, region, packet_type, metric_name, value_type,
  min(value_numeric) FILTER (WHERE value_type = 'numeric')        AS min_value,
  max(value_numeric) FILTER (WHERE value_type = 'numeric')        AS max_value,
  sum(value_numeric) FILTER (WHERE value_type = 'numeric')        AS sum_value,
  count(*)           FILTER (WHERE value_type = 'numeric')        AS count_numeric,
  last(value_numeric, time) FILTER (WHERE value_type = 'numeric') AS last_value_numeric,
  last(value_text, time)                                          AS last_value_text,
  last(value_bool, time)                                          AS last_value_bool,
  count(*)                                                        AS sample_count,
  avg(snr)                                                        AS avg_snr,
  avg(rssi)                                                       AS avg_rssi
FROM metric
GROUP BY hour, node_id, region, packet_type, metric_name, value_type
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metric_hourly',
  start_offset => INTERVAL '3 hours', end_offset => INTERVAL '10 minutes', schedule_interval => INTERVAL '30 minutes');

ALTER MATERIALIZED VIEW metric_hourly SET (timescaledb.compress = true);
SELECT add_compression_policy('metric_hourly', INTERVAL '${HOURLY_COMPRESS_AFTER}');

CREATE MATERIALIZED VIEW metric_daily
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 day', hour) AS day,
  node_id, region, packet_type, metric_name, value_type,
  min(min_value)                AS min_value,
  max(max_value)                AS max_value,
  sum(sum_value)                AS sum_value,
  sum(count_numeric)            AS count_numeric,
  last(last_value_numeric, hour) AS last_value_numeric,
  last(last_value_text, hour)    AS last_value_text,
  sum(sample_count)             AS sample_count,
  avg(avg_snr)                  AS avg_snr,
  avg(avg_rssi)                 AS avg_rssi
FROM metric_hourly
GROUP BY day, node_id, region, packet_type, metric_name, value_type
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metric_daily',
  start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour');

ALTER MATERIALIZED VIEW metric_daily SET (timescaledb.compress = true);
SELECT add_compression_policy('metric_daily', INTERVAL '${DAILY_COMPRESS_AFTER}');

CREATE VIEW metric_daily_v AS
SELECT *, CASE WHEN count_numeric > 0 THEN sum_value / count_numeric END AS avg_value
FROM metric_daily;
EOSQL
