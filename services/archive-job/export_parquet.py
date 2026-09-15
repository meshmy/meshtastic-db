"""Scheduled chunk export + manifest-gated drop_chunks (§6 of the design):
the only path by which raw rows ever leave `metric`. No add_retention_policy()
exists on `metric` — a blind age-based drop has no "only drop if archived"
hook, so this job drives drop_chunks() explicitly instead, and only after a
Parquet export has been written to disk (or uploaded) and its row count
verified against the source chunk.

Sequencing per chunk, matching the design's own ordering (must not drop
unarchived data):
  1. export to a temp file
  2. verify row count (source chunk vs. read_parquet(tmp)), compute sha256
  3. atomically rename into place (and upload to S3, if configured)
  4. insert the archive_manifest row — only after verification succeeds
  5. only then call drop_chunks(), scoped to exactly that chunk
  6. mark archive_manifest.dropped_at

A chunk with a manifest row but no dropped_at (job crashed between steps 4
and 6 on a previous run) resumes at step 5 on the next run, rather than
re-exporting — see archive_chunk(). A chunk that fails before step 4 (no
manifest row yet) is simply left in the hot table and retried next run.

Geometry: `metric.geom` arrives through DuckDB's postgres scanner as a
VARCHAR of hex-encoded EWKB (Postgres's own text output format for a
geography value) since DuckDB has no native concept of the PostGIS
geography type; ST_GeomFromHEXEWKB (from the `spatial` extension) parses
that directly into a DuckDB GEOMETRY, which is written back out as
standard GeoParquet — this is what keeps the archive tier as
GIS-consumable as the live database (§2.7).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
import psycopg
from meshdb_common.connect import build_archive_dsn, connect_with_retry

logger = logging.getLogger("archive-job")

DEFAULT_ARCHIVE_ROOT = "/archive"
DEFAULT_SCHEDULE_CRON = "0 2 * * *"


@dataclass(frozen=True)
class ChunkInfo:
    chunk_schema: str
    chunk_name: str
    range_start: datetime
    range_end: datetime


@dataclass(frozen=True)
class ManifestRow:
    parquet_path: str
    row_count: int
    sha256: str
    dropped_at: datetime | None


def find_aged_chunks(pg_conn: psycopg.Connection, retention_interval: str) -> list[ChunkInfo]:
    """Every `metric` chunk whose range has fully aged past retention_interval
    (e.g. "1 year"), regardless of whether it's already been archived —
    archive_chunk() itself decides whether that means a fresh export or just
    resuming at the drop_chunks step."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT chunk_schema, chunk_name, range_start, range_end
            FROM timescaledb_information.chunks
            WHERE hypertable_name = 'metric'
              AND range_end <= now() - %(retention)s::interval
            ORDER BY range_start
            """,
            {"retention": retention_interval},
        )
        return [ChunkInfo(*row) for row in cur.fetchall()]


def _existing_manifest_row(pg_conn: psycopg.Connection, chunk_name: str) -> ManifestRow | None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT parquet_path, row_count, sha256, dropped_at FROM archive_manifest WHERE chunk_name = %s",
            (chunk_name,),
        )
        row = cur.fetchone()
        return ManifestRow(*row) if row else None


def _parquet_path(archive_root: Path, chunk: ChunkInfo) -> Path:
    dt = chunk.range_start.date().isoformat()
    return archive_root / "parquet" / "metric" / f"dt={dt}" / f"metric_{chunk.chunk_name}.parquet"


def export_and_verify(duckdb_con: duckdb.DuckDBPyConnection, chunk: ChunkInfo, final_path: Path) -> tuple[int, str]:
    """Write chunk.chunk_name to a temp file next to final_path, verify its
    row count against the source, then atomically rename into place. Returns
    (row_count, sha256). Raises on any mismatch — caller leaves the chunk
    unarchived and retries next run rather than trusting a partial export."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_suffix(".parquet.tmp")

    source = f'pg."{chunk.chunk_schema}"."{chunk.chunk_name}"'
    duckdb_con.execute(
        f"""
        COPY (
            SELECT * EXCLUDE (geom), ST_GeomFromHEXEWKB(geom) AS geom
            FROM {source}
        ) TO '{tmp_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    (source_count,) = duckdb_con.execute(f"SELECT count(*) FROM {source}").fetchone()
    (parquet_count,) = duckdb_con.execute(f"SELECT count(*) FROM read_parquet('{tmp_path}')").fetchone()
    if source_count != parquet_count:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"row count mismatch for {chunk.chunk_name}: source={source_count} parquet={parquet_count}"
        )

    sha256 = hashlib.sha256()
    with tmp_path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(block)

    os.replace(tmp_path, final_path)
    return source_count, sha256.hexdigest()


def upload_to_s3(local_path: Path, archive_root: Path, *, bucket: str, endpoint: str | None) -> str:
    """Uploads the verified local file to S3/MinIO and removes the local
    copy — local and S3 are alternative backends (§4.1), not additive
    storage, so keeping both would double the on-disk footprint §9 sizes
    against."""
    import boto3

    key = str(local_path.relative_to(archive_root))
    client = boto3.client("s3", endpoint_url=endpoint or None)
    client.upload_file(str(local_path), bucket, key)
    local_path.unlink()
    return f"s3://{bucket}/{key}"


def _insert_manifest_row(pg_conn: psycopg.Connection, chunk: ChunkInfo, *, parquet_path: str, row_count: int, sha256: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive_manifest (chunk_name, range_start, range_end, parquet_path, row_count, sha256)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (chunk.chunk_name, chunk.range_start, chunk.range_end, parquet_path, row_count, sha256),
        )
    pg_conn.commit()


def _drop_chunk(pg_conn: psycopg.Connection, chunk: ChunkInfo) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT drop_chunks('metric', older_than => %(newer)s, newer_than => %(older)s)",
            {"newer": chunk.range_end, "older": chunk.range_start},
        )
        cur.execute("UPDATE archive_manifest SET dropped_at = now() WHERE chunk_name = %s", (chunk.chunk_name,))
    pg_conn.commit()


def archive_chunk(
    pg_conn: psycopg.Connection,
    duckdb_con: duckdb.DuckDBPyConnection,
    chunk: ChunkInfo,
    *,
    archive_root: Path,
    backend: str,
    s3_bucket: str | None,
    s3_endpoint: str | None,
) -> None:
    manifest = _existing_manifest_row(pg_conn, chunk.chunk_name)
    if manifest is not None and manifest.dropped_at is not None:
        return  # already fully archived and dropped

    if manifest is None:
        final_path = _parquet_path(archive_root, chunk)
        row_count, sha256 = export_and_verify(duckdb_con, chunk, final_path)
        parquet_path = str(final_path)
        if backend == "s3":
            if not s3_bucket:
                raise RuntimeError("ARCHIVE_BACKEND=s3 requires ARCHIVE_S3_BUCKET")
            parquet_path = upload_to_s3(final_path, archive_root, bucket=s3_bucket, endpoint=s3_endpoint)
        _insert_manifest_row(pg_conn, chunk, parquet_path=parquet_path, row_count=row_count, sha256=sha256)
        logger.info("archived %s -> %s (%d rows)", chunk.chunk_name, parquet_path, row_count)

    _drop_chunk(pg_conn, chunk)
    logger.info("dropped chunk %s from metric", chunk.chunk_name)


def build_duckdb_connection(pg_dsn: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"ATTACH '{pg_dsn}' AS pg (TYPE postgres);")
    return con


def run_once(
    pg_conn: psycopg.Connection,
    duckdb_con: duckdb.DuckDBPyConnection,
    *,
    archive_root: Path,
    retention_interval: str,
    backend: str,
    s3_bucket: str | None = None,
    s3_endpoint: str | None = None,
) -> int:
    """Archives every aged, not-yet-dropped `metric` chunk. A failure on one
    chunk is logged and skipped rather than aborting the run — it's simply
    left in the hot table (or, if already manifested, still pending its
    drop_chunks step) and retried next run. Returns the count archived
    (export completed or drop completed) this run."""
    chunks = find_aged_chunks(pg_conn, retention_interval)
    archived = 0
    for chunk in chunks:
        try:
            archive_chunk(
                pg_conn,
                duckdb_con,
                chunk,
                archive_root=archive_root,
                backend=backend,
                s3_bucket=s3_bucket,
                s3_endpoint=s3_endpoint,
            )
            archived += 1
        except Exception:
            logger.exception("failed to archive chunk %s — will retry next run", chunk.chunk_name)
    return archived


@dataclass(frozen=True)
class EnvConfig:
    archive_root: Path
    retention_interval: str
    backend: str
    s3_bucket: str | None
    s3_endpoint: str | None
    cron: str


def _env_config() -> EnvConfig:
    return EnvConfig(
        archive_root=Path(os.environ.get("ARCHIVE_LOCAL_PATH", DEFAULT_ARCHIVE_ROOT)),
        retention_interval=os.environ.get("RAW_RETENTION_INTERVAL", "1 year"),
        backend=os.environ.get("ARCHIVE_BACKEND", "local"),
        s3_bucket=os.environ.get("ARCHIVE_S3_BUCKET") or None,
        s3_endpoint=os.environ.get("ARCHIVE_S3_ENDPOINT") or None,
        cron=os.environ.get("ARCHIVE_SCHEDULE_CRON", DEFAULT_SCHEDULE_CRON),
    )


def _run_once_from_env() -> None:
    """`python export_parquet.py --once` — a manual, immediate run for
    operational use (e.g. right after deliberately shortening
    RAW_RETENTION_INTERVAL, or to verify the pipeline against a real
    deployment) instead of waiting for the next scheduled tick."""
    cfg = _env_config()
    pg_conn = connect_with_retry(build_archive_dsn())
    duckdb_con = build_duckdb_connection(build_archive_dsn())
    archived = run_once(
        pg_conn,
        duckdb_con,
        archive_root=cfg.archive_root,
        retention_interval=cfg.retention_interval,
        backend=cfg.backend,
        s3_bucket=cfg.s3_bucket,
        s3_endpoint=cfg.s3_endpoint,
    )
    logger.info("archive run complete: %d chunk(s) archived/dropped", archived)


def _run_scheduled() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    cfg = _env_config()
    pg_conn = connect_with_retry(build_archive_dsn())
    duckdb_con = build_duckdb_connection(build_archive_dsn())

    def tick() -> None:
        archived = run_once(
            pg_conn,
            duckdb_con,
            archive_root=cfg.archive_root,
            retention_interval=cfg.retention_interval,
            backend=cfg.backend,
            s3_bucket=cfg.s3_bucket,
            s3_endpoint=cfg.s3_endpoint,
        )
        logger.info("archive run complete: %d chunk(s) archived/dropped", archived)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(tick, CronTrigger.from_crontab(cfg.cron, timezone="UTC"))
    logger.info("archive-job scheduled: %s (UTC)", cfg.cron)
    scheduler.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--once" in sys.argv[1:]:
        _run_once_from_env()
    else:
        _run_scheduled()
