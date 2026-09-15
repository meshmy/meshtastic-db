"""Central MQTT subscriber (§1/§5 of the design): region-scoped topic
subscriptions, decrypt+decode via meshdb_common, batched write straight to
Postgres — no broker process sits between mqtt-ingest and the database (§8:
worst-case network-wide load is on the order of tens of rows/sec, trivially
handled by batched inserts).

Region opt-in is enforced twice: subscriptions are only ever made for
`allowed_regions` (never a wildcard-then-filter), and `handle_message` checks
the tagged region against the allow-list again as defense in depth.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path

import psycopg
from meshdb_common.config import (
    MqttBroker,
    RegionsConfig,
    load_regions_config,
    resolve_secret,
)
from meshdb_common.db import write_envelopes
from meshdb_common.decode import decode_service_envelope
from meshdb_common.envelope import DecodedPacketEnvelope
from meshdb_common.regions import (
    build_channel_psks,
    build_subscribe_topic_filters,
    is_region_allowed,
)
from paho.mqtt.client import CallbackAPIVersion, Client, MQTTMessage, topic_matches_sub

logger = logging.getLogger("mqtt-ingest")

DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_INTERVAL_SECONDS = 2.0


def resolve_region_for_topic(topic: str, topic_filters: list[tuple[str, str]]) -> str | None:
    """The allowed region an incoming topic matches, from the same (filter,
    region) pairs actually subscribed."""
    for topic_filter, region in topic_filters:
        if topic_matches_sub(topic_filter, topic):
            return region
    return None


class EnvelopeBatcher:
    """Buffers decoded envelopes and flushes them to Postgres via
    write_envelopes() once `batch_size` is reached or `batch_interval`
    seconds have passed since the oldest buffered envelope — bounds both
    memory and worst-case write latency. Thread-safe: `add()` runs on
    paho-mqtt's network thread, `flush_if_due()` is polled from the main
    thread."""

    def __init__(self, conn: psycopg.Connection, *, batch_size: int, batch_interval: float) -> None:
        self._conn = conn
        self._batch_size = batch_size
        self._batch_interval = batch_interval
        self._lock = threading.Lock()
        self._buffer: list[DecodedPacketEnvelope] = []
        self._oldest_at: float | None = None

    def add(self, envelope: DecodedPacketEnvelope) -> None:
        with self._lock:
            self._buffer.append(envelope)
            if self._oldest_at is None:
                self._oldest_at = time.monotonic()
            should_flush = len(self._buffer) >= self._batch_size
        if should_flush:
            self.flush()

    def flush_if_due(self) -> None:
        with self._lock:
            due = self._oldest_at is not None and (time.monotonic() - self._oldest_at) >= self._batch_interval
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
            self._oldest_at = None
        if batch:
            write_envelopes(self._conn, batch)
            by_packet_type = dict(Counter(env.packet_type for env in batch))
            logger.info("wrote batch of %d envelope(s): %s", len(batch), by_packet_type)


def handle_message(
    msg: MQTTMessage,
    *,
    topic_filters: list[tuple[str, str]],
    channel_psks: dict[str, bytes],
    cfg: RegionsConfig,
    batcher: EnvelopeBatcher,
) -> None:
    region = resolve_region_for_topic(msg.topic, topic_filters)
    if region is None or not is_region_allowed(region, cfg):
        # Shouldn't happen given subscriptions are already scoped to
        # allowed_regions, but never trust the network (§4).
        logger.warning("dropping message on unrecognized/disallowed topic %r", msg.topic)
        return
    try:
        envelope = decode_service_envelope(msg.payload, region=region, source="mqtt", channel_psks=channel_psks)
    except Exception:
        logger.exception("failed to decode message on topic %r", msg.topic)
        return
    batcher.add(envelope)


def build_ingest_dsn() -> str:
    host = os.environ.get("INGEST_DB_HOST", "timescaledb")
    port = os.environ.get("INGEST_DB_PORT", "5432")
    dbname = os.environ.get("INGEST_DB_NAME", "meshtastic")
    password = resolve_secret("INGEST_DB_PASSWORD")
    return f"host={host} port={port} dbname={dbname} user=ingest_rw password={password}"


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


def build_mqtt_client(broker: MqttBroker, *, client_id: str, on_message, on_connect) -> Client:
    client = Client(CallbackAPIVersion.VERSION2, client_id=client_id)
    username = resolve_secret("MQTT_USERNAME")
    password = resolve_secret("MQTT_PASSWORD")
    if username:
        client.username_pw_set(username, password)
    if broker.tls:
        client.tls_set()
    client.on_message = on_message
    client.on_connect = on_connect
    client.connect(broker.host, broker.port)
    return client


def run(
    config_path: str | Path,
    *,
    conn: psycopg.Connection | None = None,
    batch_size: int | None = None,
    batch_interval: float | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    cfg = load_regions_config(config_path)
    if not cfg.allowed_regions:
        logger.warning("allowed_regions is empty — no MQTT subscriptions will be made")

    topic_filters = build_subscribe_topic_filters(cfg)
    channel_psks = build_channel_psks(cfg)

    owns_conn = conn is None
    conn = conn or connect_with_retry(build_ingest_dsn())
    batcher = EnvelopeBatcher(
        conn,
        batch_size=batch_size if batch_size is not None else int(os.environ.get("MQTT_INGEST_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
        batch_interval=batch_interval
        if batch_interval is not None
        else float(os.environ.get("MQTT_INGEST_BATCH_INTERVAL_SECONDS", DEFAULT_BATCH_INTERVAL_SECONDS)),
    )

    def on_message(client, userdata, msg: MQTTMessage) -> None:
        handle_message(msg, topic_filters=topic_filters, channel_psks=channel_psks, cfg=cfg, batcher=batcher)

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        for topic_filter, _ in topic_filters:
            client.subscribe(topic_filter)
        logger.info("connected, subscribed to %d topic filter(s)", len(topic_filters))

    stop_event = stop_event or threading.Event()
    clients = [
        build_mqtt_client(broker, client_id=f"mqtt-ingest-{i}", on_message=on_message, on_connect=on_connect)
        for i, broker in enumerate(cfg.mqtt.brokers)
    ]
    for client in clients:
        client.loop_start()

    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
            batcher.flush_if_due()
    finally:
        for client in clients:
            client.loop_stop()
            client.disconnect()
        batcher.flush()
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(os.environ.get("REGIONS_CONFIG_PATH", "/config/regions.yaml"))
