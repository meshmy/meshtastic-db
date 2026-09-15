#!/bin/bash
# Compression settings for `metric`. Uses RAW_COMPRESS_AFTER from the
# container environment (see docker-compose.yml / .env.sample) — a plain
# .sql file can't be envsubst'd here since docker-entrypoint-initdb.d is
# mounted read-only, so this wrapper interpolates via shell heredoc instead.
#
# No add_retention_policy() on `metric` — see AGENTS.md. A blind age-based
# drop has no "only drop if archived" hook, so the archive job drives
# drop_chunks explicitly instead.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
ALTER TABLE metric SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'node_id, metric_name, packet_type',
  timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('metric', INTERVAL '${RAW_COMPRESS_AFTER}');
EOSQL
