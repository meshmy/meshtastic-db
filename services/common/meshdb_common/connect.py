"""Bootstrapping an `ingest_rw` Postgres connection — the same
INGEST_DB_HOST/PORT/NAME/PASSWORD env-var convention every ingestion
service uses (matching the values docker-compose.yml passes into each
service's container)."""

from __future__ import annotations

import os
import time

import psycopg

from .config import resolve_secret


def build_ingest_dsn() -> str:
    host = os.environ.get("INGEST_DB_HOST", "timescaledb")
    port = os.environ.get("INGEST_DB_PORT", "5432")
    dbname = os.environ.get("INGEST_DB_NAME", "meshtastic")
    password = resolve_secret("INGEST_DB_PASSWORD")
    return f"host={host} port={port} dbname={dbname} user=ingest_rw password={password}"


def build_archive_dsn() -> str:
    """Same INGEST_DB_HOST/PORT/NAME as build_ingest_dsn — only the role and
    password differ, since archive_rw is a separate, more-privileged role
    (drop_chunks() needs hypertable-owner privileges; see db/init/50_roles.sh)."""
    host = os.environ.get("INGEST_DB_HOST", "timescaledb")
    port = os.environ.get("INGEST_DB_PORT", "5432")
    dbname = os.environ.get("INGEST_DB_NAME", "meshtastic")
    password = resolve_secret("ARCHIVE_DB_PASSWORD")
    return f"host={host} port={port} dbname={dbname} user=archive_rw password={password}"


def build_grafana_ro_dsn() -> str:
    """Same INGEST_DB_HOST/PORT/NAME as build_ingest_dsn — grafana_ro is the
    read-only role Grafana's own datasource already connects as (see
    db/init/50_roles.sh); map-app is the first Python code in this repo to
    connect as it too."""
    host = os.environ.get("INGEST_DB_HOST", "timescaledb")
    port = os.environ.get("INGEST_DB_PORT", "5432")
    dbname = os.environ.get("INGEST_DB_NAME", "meshtastic")
    password = resolve_secret("GRAFANA_DB_PASSWORD")
    return f"host={host} port={port} dbname={dbname} user=grafana_ro password={password}"


def connect_with_retry(dsn: str, *, timeout: float = 60.0) -> psycopg.Connection:
    """Container start order isn't the same as "ready to accept
    connections" — retry rather than crash-loop while Postgres finishes
    initializing."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return psycopg.connect(dsn)
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"could not connect to {dsn!r} within {timeout}s") from last_error
