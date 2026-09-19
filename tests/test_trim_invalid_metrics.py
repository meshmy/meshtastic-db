"""Integration test for the one-off historical cleanup script
(services/archive-job/trim_invalid_metrics.py). Needs Docker (see
AGENTS.md / `make test-integration`).

Rows are seeded via raw SQL INSERT rather than write_envelopes(), since
write_envelopes() now applies the same limits.py filter at write time
(meshdb_common/db.py) — this simulates historical data written before that
filter existed, which is exactly what the trim script exists to clean up.

trim() intentionally scans the whole `metric` table (that's the point of a
historical cleanup pass), and every integration test in this suite shares
one session-scoped database container (see conftest.py) — some of which
insert real device_metrics.battery_level etc. rows of their own. Each test
below therefore gives its metric_name a test-unique prefix segment (the
leaf — the part limits.py actually matches on — is unaffected), so a
result dict here reflects only rows this test inserted, not the whole
table.

Uses `archive_dsn` (the `archive_rw` role), same as test_archive_job.py,
since ingest_rw has no DELETE grant on `metric` (db/init/50_roles.sh)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_trim_invalid_metrics():
    # services/archive-job isn't an importable package name (hyphen) — same
    # importlib pattern test_archive_job.py uses for export_parquet.py.
    path = REPO_ROOT / "services" / "archive-job" / "trim_invalid_metrics.py"
    spec = importlib.util.spec_from_file_location("trim_invalid_metrics", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trim_invalid_metrics = _load_trim_invalid_metrics()
trim = trim_invalid_metrics.trim

PACKET_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def archive_conn(archive_dsn):
    with psycopg.connect(archive_dsn) as conn:
        yield conn


def _insert_metric(conn, *, node_id: int, packet_id: int, metric_name: str, value_numeric: float) -> None:
    conn.execute(
        """
        INSERT INTO metric (
            time, node_id, region, packet_type, portnum,
            metric_name, value_type, value_numeric, source, packet_id
        ) VALUES (%s, %s, 'MY_919', 'Telemetry', 67, %s, 'numeric', %s, 'mqtt', %s)
        """,
        (PACKET_TIME, node_id, metric_name, value_numeric, packet_id),
    )
    conn.commit()


def test_dry_run_reports_without_deleting(archive_conn):
    node_id = 0xB1B1
    metric_name = "test_dry_run.device_metrics.battery_level"
    _insert_metric(archive_conn, node_id=node_id, packet_id=1, metric_name=metric_name, value_numeric=87.0)
    _insert_metric(archive_conn, node_id=node_id, packet_id=2, metric_name=metric_name, value_numeric=9000.0)

    results = trim(archive_conn, execute=False)

    assert results.get(metric_name) == 1
    count = archive_conn.execute("SELECT count(*) FROM metric WHERE node_id = %s", (node_id,)).fetchone()[0]
    assert count == 2  # dry run: nothing actually deleted


def test_execute_deletes_only_out_of_range_rows(archive_conn):
    node_id = 0xB2B2
    temperature = "test_execute.environment_metrics.temperature"
    voltage = "test_execute.device_metrics.voltage"
    unbounded = "test_execute.local_stats.num_packets_tx"  # no limits.py rule
    _insert_metric(archive_conn, node_id=node_id, packet_id=1, metric_name=temperature, value_numeric=21.5)
    _insert_metric(archive_conn, node_id=node_id, packet_id=2, metric_name=temperature, value_numeric=3000.0)
    _insert_metric(archive_conn, node_id=node_id, packet_id=3, metric_name=voltage, value_numeric=-500.0)
    _insert_metric(archive_conn, node_id=node_id, packet_id=4, metric_name=unbounded, value_numeric=1_000_000.0)

    results = trim(archive_conn, execute=True)

    assert results.get(temperature) == 1
    assert results.get(voltage) == 1
    assert unbounded not in results
    rows = archive_conn.execute(
        "SELECT metric_name, value_numeric FROM metric WHERE node_id = %s ORDER BY packet_id", (node_id,)
    ).fetchall()
    assert rows == [
        (temperature, 21.5),
        (unbounded, 1_000_000.0),
    ]
