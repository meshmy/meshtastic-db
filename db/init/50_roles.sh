#!/bin/bash
# Role creation needs INGEST_DB_PASSWORD / GRAFANA_DB_PASSWORD /
# ARCHIVE_DB_PASSWORD from the container environment — see
# 30_compression_retention.sh for why this is a .sh wrapper. Runs last (50_)
# since grafana_ro's grants reference metric_hourly/metric_daily/metric_daily_v,
# created in 40_.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
CREATE ROLE ingest_rw LOGIN PASSWORD '${INGEST_DB_PASSWORD}';
GRANT INSERT, SELECT, UPDATE ON metric, node_position_history, node_identity, archive_manifest TO ingest_rw;

CREATE ROLE grafana_ro LOGIN PASSWORD '${GRAFANA_DB_PASSWORD}';
GRANT SELECT ON metric, metric_hourly, metric_daily, metric_daily_v, node_identity, node_position_history TO grafana_ro;

-- archive-job: reads raw chunks for export and writes archive_manifest.
-- drop_chunks() requires hypertable-owner privileges (a plain SELECT/DELETE
-- grant isn't enough), so archive_rw is made a member of the bootstrap
-- superuser role instead of transferring metric's ownership away from it.
-- This scopes the elevated privilege to the archive-job container alone,
-- which isn't network-facing, rather than widening ingest_rw.
CREATE ROLE archive_rw LOGIN PASSWORD '${ARCHIVE_DB_PASSWORD}';
GRANT SELECT ON metric TO archive_rw;
GRANT SELECT, INSERT, UPDATE ON archive_manifest TO archive_rw;
GRANT "${POSTGRES_USER}" TO archive_rw;
EOSQL
