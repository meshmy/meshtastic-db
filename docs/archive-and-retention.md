# Archive & retention operational notes

## The three tiers

| Tier | Table/view | Retention | Notes |
|---|---|---|---|
| Raw | `metric` (hypertable) | `RAW_RETENTION_INTERVAL` (default 1 year) hot, then archived + dropped | Compressed after `RAW_COMPRESS_AFTER` (default 10 days) |
| Hourly | `metric_hourly` (continuous aggregate) | forever | Built from raw; compressed after `HOURLY_COMPRESS_AFTER` (default 30 days) |
| Daily | `metric_daily` / `metric_daily_v` | forever | Built from `metric_hourly`, not raw (hierarchical) — unaffected once raw is dropped |

Because the hourly/daily aggregates are continuously materialized and never
have a drop policy, dropping an aged raw chunk has no effect on rollup
queries — they stay queryable indefinitely. The archive exists for
point-in-time raw-row durability and offline access beyond the 1-year hot
window, not because the hot table would otherwise grow unbounded.

## Two different "retention" knobs — don't confuse them

- **Compression** (`RAW_COMPRESS_AFTER`, `HOURLY_COMPRESS_AFTER`,
  `DAILY_COMPRESS_AFTER`): a TimescaleDB compression policy, applied once at
  first database init from `.env` (`db/init/30_compression_retention.sh`,
  `40_continuous_aggregates.sh`). Compresses aged chunks in place; doesn't
  delete anything.
- **Raw retention / archival** (`RAW_RETENTION_INTERVAL`): read live by
  `archive-job` on every scheduled run (default `ARCHIVE_SCHEDULE_CRON=0 2
  * * *`, i.e. daily at 02:00 UTC) — this one *is* effectively
  live-reloadable by editing `.env` and restarting the `archive-job`
  container, unlike the compression settings.

**`docker-entrypoint-initdb.d` only runs once, against an empty volume.**
Editing `RAW_COMPRESS_AFTER`/`HOURLY_COMPRESS_AFTER`/`DAILY_COMPRESS_AFTER`
in `.env` after the database has already initialized has **no effect** on a
running deployment — those became live TimescaleDB background jobs the
first time the container started. Retuning them later needs `make
retune-retention`, which — as of this writing — is still an unimplemented
stub (`make retune-retention` exits non-zero with a message saying so). The
schema it would act on already exists; changing a compression policy on a
live deployment currently means running `remove_retention_policy()`/
`add_retention_policy()` (or `alter_job()`) by hand via `psql` against the
relevant policy, following TimescaleDB's own documentation for altering an
existing job's config, until that Makefile target is built out.

## How the archive job works

Runs on a schedule inside the always-on `archive-job` container (no
`profiles:`, starts with a plain `docker compose up`). Per aged chunk (any
`metric` chunk whose `range_end` is older than `now() -
RAW_RETENTION_INTERVAL` and has no `archive_manifest` row yet):

1. Export via DuckDB's `postgres` scanner directly against `archive_rw`
   (reads the chunk table itself, including already-compressed chunks —
   verified, no special-casing needed) to a temp Parquet file, with
   `spatial`-extension conversion so the `geom` column round-trips as
   standard GeoParquet, not an opaque blob.
2. Verify: row count in the Parquet file matches the source chunk's row
   count; compute a sha256.
3. Atomic rename into place under `<ARCHIVE_LOCAL_PATH>/parquet/metric/dt=<date>/`.
4. Insert the `archive_manifest` row (`chunk_name`, range, path, row count,
   sha256) — only after verification succeeds.
5. **Only then** call `drop_chunks()` scoped to exactly that chunk, and mark
   `dropped_at`.

If export/verification fails, the job logs and retries on the next
scheduled run — the chunk is simply left in the hot table, never
half-dropped. If the process dies between steps 4 and 5 (manifest row
exists, `dropped_at` still null), the next run resumes at the `drop_chunks`
step for that chunk rather than re-exporting it.

This ordering — verify before drop, manifest before drop — is why `metric`
has no `add_retention_policy()`/age-based drop policy of its own: a blind
age-based drop has no hook for "only drop if archived first."

## Running an archive pass manually

Outside the cron schedule — e.g. right after lowering
`RAW_RETENTION_INTERVAL` to test the pipeline against real data without
waiting for 02:00 UTC:

```bash
docker compose run --rm archive-job python export_parquet.py --once
```

## Storage backend

`ARCHIVE_BACKEND=local` (default) keeps Parquet files under the
`./archive` bind mount (`ARCHIVE_LOCAL_PATH=/archive` inside the
container). `ARCHIVE_BACKEND=s3` uploads the same verified file via `boto3`
to `ARCHIVE_S3_BUCKET`/`ARCHIVE_S3_ENDPOINT` (works against AWS S3 or a
MinIO-compatible endpoint) and removes the local copy — the two backends
are alternatives, not additive storage. `services/archive-job` needs
outbound network access the first time it runs, regardless of backend:
DuckDB's `postgres` and `spatial` extensions are `INSTALL`/`LOAD`ed at
runtime, not baked into the image.

## Querying the archive

No running database needed for archived data — query Parquet files
directly via DuckDB:

```sql
INSTALL spatial; LOAD spatial;
SELECT * FROM read_parquet('/archive/parquet/metric/dt=2027-*/*.parquet');
```

or through Grafana's provisioned `archive-duckdb` datasource (the unsigned
`motherduck-duckdb-datasource` plugin — see the main
[README](../README.md) for why Grafana needs the Ubuntu image variant for
this). Every ad hoc query against it needs its own `read_parquet(...)` glob
naming the date range of interest; the datasource itself doesn't point at
one fixed file.

## Checking archival health

```sql
-- Chunks archived so far, and whether each has actually been dropped yet:
SELECT chunk_name, range_start, range_end, row_count, exported_at, dropped_at
FROM archive_manifest ORDER BY range_start DESC;

-- Chunks old enough to be archived but with no manifest row yet (expected
-- to be empty most of the time; a persistently non-empty result means the
-- job is failing before it gets to the manifest insert -- check its logs):
SELECT c.chunk_name, c.range_start, c.range_end
FROM timescaledb_information.chunks c
LEFT JOIN archive_manifest m ON m.chunk_name = c.chunk_name
WHERE c.hypertable_name = 'metric'
  AND c.range_end <= now() - interval '1 year'  -- match RAW_RETENTION_INTERVAL
  AND m.chunk_name IS NULL;
```

A manifest row with `dropped_at` still null after more than one scheduled
run's worth of time has passed is worth investigating — it means either the
job has been failing at the `drop_chunks` step specifically (check
`archive-job` logs) or it's a very recent export that just hasn't hit its
next scheduled tick yet.
