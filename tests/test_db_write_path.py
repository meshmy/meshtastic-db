"""Integration test for meshdb_common.db.write_envelopes against a real,
disposable TimescaleDB container running the actual db/init/* schema —
needs Docker (see AGENTS.md / `make test-integration`), so it's marked
`integration` and excluded from the default `make test` run.

Connects as `ingest_rw` (not the superuser) so the test also exercises the
grants that db/init/50_roles.sh actually creates, not just the SQL."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from meshdb_common.db import write_envelopes
from meshdb_common.envelope import (
    DecodedPacketEnvelope,
    FieldValue,
    NodeIdentityUpdate,
    PositionFix,
)
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
INGEST_DB_PASSWORD = "test-ingest-password"


def _wait_until_ready(dsn: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1 FROM node_identity")
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"timescaledb test container never became ready: {last_error}")


@pytest.fixture(scope="module")
def ingest_dsn():
    container = (
        DockerContainer("timescale/timescaledb-ha:pg16")
        .with_env("POSTGRES_PASSWORD", "test-superuser-password")
        .with_env("POSTGRES_DB", "meshtastic")
        .with_env("RAW_COMPRESS_AFTER", "10 days")
        .with_env("HOURLY_COMPRESS_AFTER", "30 days")
        .with_env("DAILY_COMPRESS_AFTER", "90 days")
        .with_env("INGEST_DB_PASSWORD", INGEST_DB_PASSWORD)
        .with_env("GRAFANA_DB_PASSWORD", "test-grafana-password")
        .with_volume_mapping(str(REPO_ROOT / "db" / "init"), "/docker-entrypoint-initdb.d", "ro")
        .with_exposed_ports(5432)
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        superuser_dsn = f"host={host} port={port} dbname=meshtastic user=postgres password=test-superuser-password"
        _wait_until_ready(superuser_dsn)
        yield f"host={host} port={port} dbname=meshtastic user=ingest_rw password={INGEST_DB_PASSWORD}"


@pytest.fixture
def conn(ingest_dsn):
    # No per-test cleanup: ingest_rw has no TRUNCATE/DELETE grant (matches
    # its production grants, §2.6 — INSERT/SELECT/UPDATE only), and each
    # test below uses its own node_id so tests stay isolated without it.
    with psycopg.connect(ingest_dsn) as connection:
        yield connection


def _envelope(
    *,
    time: datetime,
    node_id: int,
    packet_type: str = "Telemetry",
    portnum: int = 3,
    region: str = "MY_919",
    source: str = "mqtt",
    packet_id: int = 1,
    fields: tuple[FieldValue, ...] = (),
    position: PositionFix | None = None,
    identity: NodeIdentityUpdate | None = None,
) -> DecodedPacketEnvelope:
    return DecodedPacketEnvelope(
        time=time,
        node_id=node_id,
        region=region,
        source=source,
        packet_type=packet_type,
        portnum=portnum,
        packet_id=packet_id,
        fields=fields,
        position=position,
        identity=identity,
    )


def test_metric_row_lands_with_null_position_when_node_never_reported_one(conn):
    env = _envelope(
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        node_id=0x1111,
        fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=88.0),),
    )
    write_envelopes(conn, [env])

    row = conn.execute(
        "SELECT metric_name, value_numeric, latitude, position_time FROM metric WHERE node_id = %s", (0x1111,)
    ).fetchone()
    assert row == ("device_metrics.battery_level", 88.0, None, None)


def test_position_written_before_metric_resolves_in_same_batch(conn):
    node_id = 0x2222
    position_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    metric_time = position_time + timedelta(minutes=5)
    envelopes = [
        _envelope(
            time=position_time,
            node_id=node_id,
            packet_type="Position",
            portnum=1,
            packet_id=1,
            position=PositionFix(latitude=3.7, longitude=-12.2, altitude=15.0),
        ),
        _envelope(
            time=metric_time,
            node_id=node_id,
            packet_id=2,
            fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=70.0),),
        ),
    ]

    write_envelopes(conn, envelopes)

    row = conn.execute(
        "SELECT latitude, longitude, altitude, position_time FROM metric WHERE node_id = %s AND metric_name = 'device_metrics.battery_level'",
        (node_id,),
    ).fetchone()
    assert row == (3.7, -12.2, 15.0, position_time)

    position_rows = conn.execute(
        "SELECT latitude, longitude FROM node_position_history WHERE node_id = %s", (node_id,)
    ).fetchall()
    assert position_rows == [(3.7, -12.2)]


def test_position_as_of_join_picks_earlier_position_not_later(conn):
    node_id = 0x3333
    earlier = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    later = datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)
    between = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    write_envelopes(
        conn,
        [
            _envelope(time=earlier, node_id=node_id, packet_type="Position", portnum=1, packet_id=1,
                      position=PositionFix(latitude=1.0, longitude=1.0)),
            _envelope(time=later, node_id=node_id, packet_type="Position", portnum=1, packet_id=2,
                      position=PositionFix(latitude=2.0, longitude=2.0)),
        ],
    )
    write_envelopes(
        conn,
        [
            _envelope(
                time=between,
                node_id=node_id,
                packet_id=3,
                fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=50.0),),
            )
        ],
    )

    row = conn.execute(
        "SELECT latitude, longitude, position_time FROM metric WHERE node_id = %s AND metric_name = 'device_metrics.battery_level'",
        (node_id,),
    ).fetchone()
    assert row == (1.0, 1.0, earlier)


def test_identity_upsert_within_one_batch_keeps_latest_by_time(conn):
    node_id = 0x4444
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = older + timedelta(hours=1)

    write_envelopes(
        conn,
        [
            _envelope(time=older, node_id=node_id, packet_type="User", portnum=4, packet_id=1,
                      identity=NodeIdentityUpdate(long_name="Old Name", short_name="OLD1")),
            _envelope(time=newer, node_id=node_id, packet_type="User", portnum=4, packet_id=2,
                      identity=NodeIdentityUpdate(long_name="New Name", short_name="NEW1")),
        ],
    )

    row = conn.execute(
        "SELECT long_name, short_name, last_heard FROM node_identity WHERE node_id = %s", (node_id,)
    ).fetchone()
    assert row == ("New Name", "NEW1", newer)


def test_identity_upsert_across_batches_latest_wins(conn):
    node_id = 0x5555
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second = first + timedelta(hours=1)

    write_envelopes(conn, [_envelope(time=first, node_id=node_id, packet_type="User", portnum=4, packet_id=1,
                                      identity=NodeIdentityUpdate(long_name="First"))])
    write_envelopes(conn, [_envelope(time=second, node_id=node_id, packet_type="User", portnum=4, packet_id=2,
                                      identity=NodeIdentityUpdate(long_name="Second"))])

    row = conn.execute("SELECT long_name FROM node_identity WHERE node_id = %s", (node_id,)).fetchone()
    assert row == ("Second",)


def test_metric_dedup_on_conflict_when_same_packet_relayed_twice(conn):
    node_id = 0x6666
    packet_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    field = FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=42.0)

    # Same packet_id/time/node_id/metric_name relayed by two MQTT gateways,
    # inside one batch and again in a second batch.
    write_envelopes(
        conn,
        [
            _envelope(time=packet_time, node_id=node_id, packet_id=7, fields=(field,)),
            _envelope(time=packet_time, node_id=node_id, packet_id=7, fields=(field,)),
        ],
    )
    write_envelopes(conn, [_envelope(time=packet_time, node_id=node_id, packet_id=7, fields=(field,))])

    count = conn.execute("SELECT count(*) FROM metric WHERE node_id = %s", (node_id,)).fetchone()[0]
    assert count == 1
