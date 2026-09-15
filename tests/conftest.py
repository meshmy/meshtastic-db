"""Shared fixtures for the Docker-backed integration tests (`-m integration`,
see AGENTS.md / `make test-integration`)."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import meshdb_common  # noqa: F401  side effect: puts generated/ on sys.path so `import meshtastic.*_pb2` resolves
import psycopg
import pytest
from paho.mqtt.client import CallbackAPIVersion, Client
from testcontainers.core.container import DockerContainer

REPO_ROOT = Path(__file__).resolve().parent.parent
INGEST_DB_PASSWORD = "test-ingest-password"
ARCHIVE_DB_PASSWORD = "test-archive-password"


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


@pytest.fixture(scope="session")
def _timescaledb_host_port():
    """One disposable TimescaleDB container running the actual db/init/*
    scripts, shared by every fixture below that needs a role DSN against it
    — spinning up a second container per role would be pure overhead."""
    container = (
        DockerContainer("timescale/timescaledb-ha:pg16")
        .with_env("POSTGRES_PASSWORD", "test-superuser-password")
        .with_env("POSTGRES_DB", "meshtastic")
        .with_env("RAW_COMPRESS_AFTER", "10 days")
        .with_env("HOURLY_COMPRESS_AFTER", "30 days")
        .with_env("DAILY_COMPRESS_AFTER", "90 days")
        .with_env("INGEST_DB_PASSWORD", INGEST_DB_PASSWORD)
        .with_env("GRAFANA_DB_PASSWORD", "test-grafana-password")
        .with_env("ARCHIVE_DB_PASSWORD", ARCHIVE_DB_PASSWORD)
        .with_volume_mapping(str(REPO_ROOT / "db" / "init"), "/docker-entrypoint-initdb.d", "ro")
        .with_exposed_ports(5432)
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        superuser_dsn = f"host={host} port={port} dbname=meshtastic user=postgres password=test-superuser-password"
        _wait_until_ready(superuser_dsn)
        yield host, port


@pytest.fixture(scope="session")
def ingest_dsn(_timescaledb_host_port):
    """A DSN against the shared test container that connects as `ingest_rw`
    (not the superuser) — so any test using this fixture also exercises the
    grants db/init/50_roles.sh actually creates, not just the schema SQL."""
    host, port = _timescaledb_host_port
    return f"host={host} port={port} dbname=meshtastic user=ingest_rw password={INGEST_DB_PASSWORD}"


@pytest.fixture(scope="session")
def archive_dsn(_timescaledb_host_port):
    """A DSN against the shared test container that connects as
    `archive_rw` — the archive-job role, distinct from ingest_rw because
    drop_chunks() needs hypertable-owner privileges (see
    db/init/50_roles.sh)."""
    host, port = _timescaledb_host_port
    return f"host={host} port={port} dbname=meshtastic user=archive_rw password={ARCHIVE_DB_PASSWORD}"


def _wait_until_mqtt_ready(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        client = Client(CallbackAPIVersion.VERSION2)
        try:
            client.connect(host, port, keepalive=5)
            client.disconnect()
            return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"mosquitto test container never became ready: {last_error}")


@pytest.fixture(scope="session")
def mosquitto_broker():
    """A disposable eclipse-mosquitto broker, anonymous access enabled, for
    the MQTT-ingest corpus-replay test — real MQTT wire traffic, not a
    mocked client.

    The bind-mounted conf dir is created under the repo tree, not the
    system tempdir: on macOS via colima, Docker only sees paths under the
    colima VM's mounted directories (the user's home directory by default),
    and the system tempdir (/tmp, /var/folders/...) isn't one of them —
    mosquitto would otherwise start against an empty/missing config."""
    conf_dir = Path(tempfile.mkdtemp(prefix=".mqtt-ingest-test-", dir=REPO_ROOT))
    try:
        (conf_dir / "mosquitto.conf").write_text("listener 1883\nallow_anonymous true\n")
        container = (
            DockerContainer("eclipse-mosquitto:2")
            .with_volume_mapping(str(conf_dir), "/mosquitto/config", "ro")
            .with_exposed_ports(1883)
        )
        with container:
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(1883))
            _wait_until_mqtt_ready(host, port)
            yield host, port
    finally:
        shutil.rmtree(conf_dir, ignore_errors=True)
