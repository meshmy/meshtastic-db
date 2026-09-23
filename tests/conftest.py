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
import requests
from paho.mqtt.client import CallbackAPIVersion, Client
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

REPO_ROOT = Path(__file__).resolve().parent.parent
INGEST_DB_PASSWORD = "test-ingest-password"
ARCHIVE_DB_PASSWORD = "test-archive-password"
GRAFANA_DB_PASSWORD = "test-grafana-password"
GRAFANA_ADMIN_PASSWORD = "test-admin-password"
DUCKDB_PLUGIN_URL = (
    "https://github.com/motherduckdb/grafana-duckdb-datasource/releases/"
    "download/v0.4.5/motherduck-duckdb-datasource-0.4.5.zip;motherduck-duckdb-datasource"
)


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
def _test_network():
    """A custom Docker network shared by every container-to-container test
    (currently just Grafana -> TimescaleDB) — testcontainers' default bridge
    network gives no hostname resolution between sibling containers, and the
    Grafana provisioning YAML under test (grafana/provisioning/) hardcodes
    `timescaledb:5432`, the same hostname docker-compose.yml's own network
    gives it in production. Aliasing the test container to that same name
    means the provisioning file needs no test-only override."""
    with Network() as network:
        yield network


@pytest.fixture(scope="session")
def _timescaledb_host_port(_test_network):
    """One disposable TimescaleDB container running the actual db/init/*
    scripts, shared by every fixture below that needs a role DSN against it
    — spinning up a second container per role would be pure overhead. Also
    joined to `_test_network` under the alias `timescaledb` so a sibling
    container (Grafana) can reach it by that hostname, same as production."""
    container = (
        DockerContainer("timescale/timescaledb-ha:pg16")
        .with_env("POSTGRES_PASSWORD", "test-superuser-password")
        .with_env("POSTGRES_DB", "meshtastic")
        .with_env("RAW_COMPRESS_AFTER", "10 days")
        .with_env("HOURLY_COMPRESS_AFTER", "30 days")
        .with_env("DAILY_COMPRESS_AFTER", "90 days")
        .with_env("INGEST_DB_PASSWORD", INGEST_DB_PASSWORD)
        .with_env("GRAFANA_DB_PASSWORD", GRAFANA_DB_PASSWORD)
        .with_env("ARCHIVE_DB_PASSWORD", ARCHIVE_DB_PASSWORD)
        .with_volume_mapping(str(REPO_ROOT / "db" / "init"), "/docker-entrypoint-initdb.d", "ro")
        .with_exposed_ports(5432)
        .with_network(_test_network)
        .with_network_aliases("timescaledb")
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


@pytest.fixture(scope="session")
def grafana_ro_dsn(_timescaledb_host_port):
    """A DSN against the shared test container that connects as
    `grafana_ro` — the same read-only role Grafana's own datasource and
    map-app both use, so a test using this fixture also exercises
    50_roles.sh's actual grants rather than just the schema SQL."""
    host, port = _timescaledb_host_port
    return f"host={host} port={port} dbname=meshtastic user=grafana_ro password={GRAFANA_DB_PASSWORD}"


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


@pytest.fixture(scope="session")
def archive_root():
    """Bind-mount source for the Grafana test container's /archive — under
    the repo tree, not the system tempdir, for the same colima-visibility
    reason as `mosquitto_broker`'s conf dir above. Starts empty; a test
    populates it with a real archive-job export after the container is
    already running (a live bind mount, so newly written files are visible
    inside the container with no restart needed).

    tempfile.mkdtemp() always creates its directory 0700 (owner-only) — the
    Grafana container runs as a non-root uid (472) that doesn't own this
    host directory, so without loosening permissions it can't even traverse
    into it to see files the archive-job export later writes, surfacing as
    a spurious "no files match" from its DuckDB datasource instead of a
    permission error."""
    root = Path(tempfile.mkdtemp(prefix=".grafana-archive-test-", dir=REPO_ROOT))
    root.chmod(0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _wait_until_grafana_ready(base_url: str, timeout: float = 180.0) -> None:
    # Generous timeout: GF_INSTALL_PLUGINS fetches the unsigned DuckDB
    # plugin's zip from GitHub on every container start (not baked into the
    # image — see docker-compose.yml), so this also covers that download.
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/health", timeout=5)
            if resp.status_code == 200:
                return
            last_error = RuntimeError(f"status {resp.status_code}: {resp.text}")
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(2)
    raise TimeoutError(f"grafana test container never became ready: {last_error}")


@pytest.fixture(scope="session")
def grafana_auth():
    return ("admin", GRAFANA_ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def grafana_base_url(_timescaledb_host_port, _test_network, archive_root):
    """A disposable Grafana container running this repo's actual
    grafana.ini/provisioning/ files, joined to `_test_network` so its
    provisioned TimescaleDB datasource (hostname `timescaledb`, matching
    production) can actually reach `_timescaledb_host_port`. Session-scoped
    like the Postgres container it depends on — one Grafana instance, not
    one per test."""
    container = (
        DockerContainer("grafana/grafana:11.3.0-ubuntu")
        .with_env("GF_SECURITY_ADMIN_PASSWORD", GRAFANA_ADMIN_PASSWORD)
        .with_env("GRAFANA_DB_PASSWORD", GRAFANA_DB_PASSWORD)
        .with_env("GF_INSTALL_PLUGINS", DUCKDB_PLUGIN_URL)
        .with_volume_mapping(str(REPO_ROOT / "grafana" / "grafana.ini"), "/etc/grafana/grafana.ini", "ro")
        .with_volume_mapping(str(REPO_ROOT / "grafana" / "provisioning"), "/etc/grafana/provisioning", "ro")
        .with_volume_mapping(str(archive_root), "/archive", "ro")
        .with_exposed_ports(3000)
        .with_network(_test_network)
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(3000)
        base_url = f"http://{host}:{port}"
        _wait_until_grafana_ready(base_url)
        yield base_url
