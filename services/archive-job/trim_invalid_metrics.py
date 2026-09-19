"""One-off maintenance pass: delete existing `metric` rows that fail the
bounds in meshdb_common.limits — rows written before that module existed,
or by any future bug bypassing the write-time filter in meshdb_common.db.
Not scheduled; run manually via `docker compose run --rm archive-job python
trim_invalid_metrics.py` (see docs/archive-and-retention.md).

Reuses the archive_rw role (same as export_parquet.py) since ingest_rw has
no DELETE grant on `metric` (db/init/50_roles.sh).

Defaults to a dry run (report only); pass --execute to actually delete.
"""

from __future__ import annotations

import logging
import sys

import psycopg
from meshdb_common import limits
from meshdb_common.connect import build_archive_dsn, connect_with_retry

logger = logging.getLogger("trim-invalid-metrics")


def distinct_numeric_metric_names(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT metric_name FROM metric WHERE value_type = 'numeric'")
        return [row[0] for row in cur.fetchall()]


def trim(conn: psycopg.Connection, *, execute: bool) -> dict[str, int]:
    """Returns {metric_name: row count deleted (or, in a dry run, that
    would be deleted)} for every metric_name with at least one out-of-range
    row. Only metric_names with a configured limits.py rule are considered
    — an unmatched metric_name is left alone, same as at write time."""
    results: dict[str, int] = {}
    for metric_name in distinct_numeric_metric_names(conn):
        bounds = limits.bounds_for(metric_name)
        if bounds is None:
            continue
        lo, hi = bounds
        with conn.cursor() as cur:
            if execute:
                cur.execute(
                    "DELETE FROM metric WHERE metric_name = %s AND value_type = 'numeric' "
                    "AND (value_numeric < %s OR value_numeric > %s)",
                    (metric_name, lo, hi),
                )
                count = cur.rowcount
            else:
                cur.execute(
                    "SELECT count(*) FROM metric WHERE metric_name = %s AND value_type = 'numeric' "
                    "AND (value_numeric < %s OR value_numeric > %s)",
                    (metric_name, lo, hi),
                )
                (count,) = cur.fetchone()
        if count:
            results[metric_name] = count
    if execute:
        conn.commit()
    return results


def _run_from_env() -> None:
    execute = "--execute" in sys.argv[1:]
    conn = connect_with_retry(build_archive_dsn())
    results = trim(conn, execute=execute)

    verb = "deleted" if execute else "would delete"
    for metric_name, count in sorted(results.items()):
        logger.info("%s %d row(s) for %s", verb, count, metric_name)
    total = sum(results.values())
    logger.info("%s %d row(s) total across %d metric_name(s)", verb, total, len(results))
    if not execute and total:
        logger.info("dry run only — re-run with --execute to delete these rows")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _run_from_env()
