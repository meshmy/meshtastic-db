"""Integration test for the archive job's export/verify/manifest/drop_chunks
sequencing (§6 of the design; §10 item — "manually age a chunk, run the job,
confirm Parquet file + manifest row + chunk drop, confirm DuckDB reads the
file"). Needs Docker (see AGENTS.md / `make test-integration`).

Uses `archive_dsn` (the `archive_rw` role) rather than `ingest_dsn` so this
test also exercises db/init/50_roles.sh's drop_chunks grant, not just the
export logic — a plain SELECT/DELETE grant isn't enough for drop_chunks(),
so a failure here would mean the role setup is wrong, not just the code."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from meshdb_common.db import write_envelopes
from meshdb_common.envelope import DecodedPacketEnvelope, FieldValue, PositionFix

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_export_parquet():
    # services/archive-job isn't an importable package name (hyphen) — same
    # importlib pattern test_tcp_poller_replay.py/test_mqtt_ingest_replay.py
    # use for their services' main.py.
    path = REPO_ROOT / "services" / "archive-job" / "export_parquet.py"
    spec = importlib.util.spec_from_file_location("export_parquet", path)
    module = importlib.util.module_from_spec(spec)
    # export_parquet.py defines its own @dataclass classes (ChunkInfo,
    # ManifestRow) — dataclass processing looks the defining module up in
    # sys.modules, so it must be registered before exec_module runs (same
    # workaround test_gateway_agent_wal.py needs, for the same reason).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


export_parquet = _load_export_parquet()
build_duckdb_connection = export_parquet.build_duckdb_connection
find_aged_chunks = export_parquet.find_aged_chunks
run_once = export_parquet.run_once

# Distinct, far-past dates per test — `archive_conn`/`duckdb_con` share one
# session-scoped container with every other integration test module, some
# of which also insert historical (already-aged-by-now) rows, so a chunk
# is identified by its own date rather than by a global count or ordering
# assumption, and each test uses a date no other test touches.
OLD_TIME_1 = datetime(2020, 6, 15, 12, 0, tzinfo=timezone.utc)
OLD_TIME_2 = datetime(2020, 3, 10, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def ingest_conn(ingest_dsn):
    with psycopg.connect(ingest_dsn) as conn:
        yield conn


@pytest.fixture
def archive_conn(archive_dsn):
    with psycopg.connect(archive_dsn) as conn:
        yield conn


@pytest.fixture
def duckdb_con(archive_dsn):
    con = build_duckdb_connection(archive_dsn)
    try:
        yield con
    finally:
        con.close()


def test_run_once_exports_verifies_and_drops_an_aged_chunk(ingest_conn, archive_conn, duckdb_con, tmp_path: Path):
    node_id = 0xA1A1
    write_envelopes(
        ingest_conn,
        [
            DecodedPacketEnvelope(
                time=OLD_TIME_1,
                node_id=node_id,
                region="MY_919",
                source="mqtt",
                packet_type="Position",
                portnum=1,
                packet_id=1,
                fields=(),
                position=PositionFix(latitude=1.35, longitude=103.87, altitude=10.0),
                identity=None,
            ),
            DecodedPacketEnvelope(
                time=OLD_TIME_1,
                node_id=node_id,
                region="MY_919",
                source="mqtt",
                packet_type="Telemetry",
                portnum=67,
                packet_id=2,
                fields=(FieldValue(metric_name="environment_metrics.temperature", value_type="numeric", value_numeric=27.5),),
                position=None,
                identity=None,
            ),
        ],
    )
    ingest_conn.commit()

    aged = find_aged_chunks(archive_conn, "0 seconds")
    chunk = next(c for c in aged if c.range_start.date() == OLD_TIME_1.date())

    archived = run_once(
        archive_conn,
        duckdb_con,
        archive_root=tmp_path,
        retention_interval="0 seconds",
        backend="local",
    )
    assert archived >= 1  # this run may also sweep up aged chunks from other test modules sharing the container

    manifest_row = archive_conn.execute(
        "SELECT parquet_path, row_count, sha256, dropped_at FROM archive_manifest WHERE chunk_name = %s",
        (chunk.chunk_name,),
    ).fetchone()
    parquet_path, row_count, sha256, dropped_at = manifest_row
    # Only the Telemetry envelope lands a `metric` row — a Position envelope
    # has no `fields` of its own, it only feeds node_position_history (which
    # this job doesn't export) and the as-of join the Telemetry row picks up.
    assert row_count == 1
    assert sha256
    assert dropped_at is not None
    assert Path(parquet_path).exists()

    remaining = archive_conn.execute(
        "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'metric' AND chunk_name = %s",
        (chunk.chunk_name,),
    ).fetchone()[0]
    assert remaining == 0

    rows = duckdb_con.execute(f"SELECT node_id, metric_name, ST_AsText(geom) FROM read_parquet('{parquet_path}')").fetchall()
    assert rows == [(node_id, "environment_metrics.temperature", "POINT (103.87 1.35)")]


def test_run_once_is_idempotent_on_a_chunk_already_fully_archived(ingest_conn, archive_conn, duckdb_con, tmp_path: Path):
    node_id = 0xB2B2
    write_envelopes(
        ingest_conn,
        [
            DecodedPacketEnvelope(
                time=OLD_TIME_2,
                node_id=node_id,
                region="MY_919",
                source="mqtt",
                packet_type="Telemetry",
                portnum=67,
                packet_id=3,
                fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=99.0),),
                position=None,
                identity=None,
            )
        ],
    )
    ingest_conn.commit()

    kwargs = {"archive_root": tmp_path, "retention_interval": "0 seconds", "backend": "local"}
    run_once(archive_conn, duckdb_con, **kwargs)

    def manifest_for_my_chunk():
        return archive_conn.execute(
            "SELECT chunk_name, row_count, dropped_at FROM archive_manifest WHERE range_start = %s",
            (OLD_TIME_2.date(),),
        ).fetchone()

    first_manifest = manifest_for_my_chunk()
    assert first_manifest is not None
    _chunk_name, row_count, dropped_at = first_manifest
    assert row_count == 1
    assert dropped_at is not None

    run_once(archive_conn, duckdb_con, **kwargs)  # nothing new to do for this chunk — must not re-export or error

    second_manifest = manifest_for_my_chunk()
    assert second_manifest == first_manifest
