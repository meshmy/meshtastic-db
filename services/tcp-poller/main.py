"""Central TCP poller (§1/§5 of the design): connects directly to each
configured node's local TCP API port (no message broker in between, same
reasoning as mqtt-ingest — §8), decodes via meshdb_common, and writes
through the same shared db.py write path. One connection thread per
configured `tcp_nodes` entry; independent of every other node.

Local API traffic is already decrypted by the node itself (it has no
channel_id like MQTT's ServiceEnvelope does), so `decode_mesh_packet` is
called with `channel_id=None` — only a configured wildcard ("*") PSK could
ever apply to the rare packet the node forwards still encrypted.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
from pathlib import Path

import psycopg
from meshdb_common.batching import EnvelopeBatcher
from meshdb_common.config import RegionsConfig, TcpNode, load_regions_config
from meshdb_common.connect import build_ingest_dsn, connect_with_retry
from meshdb_common.decode import decode_mesh_packet
from meshdb_common.regions import build_channel_psks, is_region_allowed
from meshdb_common.stream_framing import FrameReader, encode_frame
from meshtastic import mesh_pb2

logger = logging.getLogger("tcp-poller")

DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_INTERVAL_SECONDS = 2.0
DEFAULT_PORT = 4403
DEFAULT_RECONNECT_DELAY_SECONDS = 5.0
RECV_CHUNK_SIZE = 4096


def handle_from_radio(
    from_radio: mesh_pb2.FromRadio,
    *,
    node: TcpNode,
    channel_psks: dict[str, bytes],
    cfg: RegionsConfig,
    batcher: EnvelopeBatcher,
) -> None:
    if from_radio.WhichOneof("payload_variant") != "packet":
        return  # config/nodeinfo-db/channel dump etc. — only mesh traffic is ingested
    if not is_region_allowed(node.region, cfg):
        # Defense in depth (§4): a misconfigured tcp_nodes entry shouldn't
        # silently ingest an unwanted region.
        logger.warning("dropping packet for disallowed region %r (node %s)", node.region, node.host)
        return
    envelope = decode_mesh_packet(
        from_radio.packet,
        region=node.region,
        source="tcp",
        channel_psks=channel_psks,
        channel_id=None,
    )
    batcher.add(envelope)


def _connect_and_request_config(host: str, port: int, *, socket_timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=socket_timeout)
    want_config_id = random.randint(1, 0xFFFFFFFF)
    to_radio = mesh_pb2.ToRadio(want_config_id=want_config_id)
    sock.sendall(encode_frame(to_radio.SerializeToString()))
    return sock


def poll_node(
    node: TcpNode,
    *,
    channel_psks: dict[str, bytes],
    cfg: RegionsConfig,
    batcher: EnvelopeBatcher,
    stop_event: threading.Event,
    port: int,
    reconnect_delay: float,
) -> None:
    """Runs until `stop_event` is set, reconnecting on any socket error."""
    while not stop_event.is_set():
        try:
            sock = _connect_and_request_config(node.host, port, socket_timeout=1.0)
        except OSError as exc:
            logger.warning("tcp connect to %s:%d failed: %s — retrying in %.0fs", node.host, port, exc, reconnect_delay)
            stop_event.wait(reconnect_delay)
            continue

        logger.info("connected to %s:%d (region %s)", node.host, port, node.region)
        reader = FrameReader()
        try:
            with sock:
                while not stop_event.is_set():
                    try:
                        chunk = sock.recv(RECV_CHUNK_SIZE)
                    except TimeoutError:
                        continue
                    if not chunk:
                        raise ConnectionError("connection closed by remote node")
                    for payload in reader.feed(chunk):
                        from_radio = mesh_pb2.FromRadio()
                        try:
                            from_radio.ParseFromString(payload)
                        except Exception:
                            logger.exception("failed to parse FromRadio frame from %s", node.host)
                            continue
                        handle_from_radio(from_radio, node=node, channel_psks=channel_psks, cfg=cfg, batcher=batcher)
        except OSError as exc:
            if not stop_event.is_set():
                logger.warning("tcp connection to %s:%d lost: %s — reconnecting in %.0fs", node.host, port, exc, reconnect_delay)
                stop_event.wait(reconnect_delay)


def run(
    config_path: str | Path,
    *,
    conn: psycopg.Connection | None = None,
    batch_size: int | None = None,
    batch_interval: float | None = None,
    port: int | None = None,
    reconnect_delay: float | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    cfg = load_regions_config(config_path)
    if not cfg.tcp_nodes:
        logger.warning("tcp_nodes is empty — nothing to poll")

    channel_psks = build_channel_psks(cfg)
    port = port if port is not None else int(os.environ.get("TCP_POLLER_PORT", DEFAULT_PORT))
    reconnect_delay = (
        reconnect_delay
        if reconnect_delay is not None
        else float(os.environ.get("TCP_POLLER_RECONNECT_DELAY_SECONDS", DEFAULT_RECONNECT_DELAY_SECONDS))
    )

    owns_conn = conn is None
    conn = conn or connect_with_retry(build_ingest_dsn())
    batcher = EnvelopeBatcher(
        conn,
        batch_size=batch_size if batch_size is not None else int(os.environ.get("TCP_POLLER_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
        batch_interval=batch_interval
        if batch_interval is not None
        else float(os.environ.get("TCP_POLLER_BATCH_INTERVAL_SECONDS", DEFAULT_BATCH_INTERVAL_SECONDS)),
    )

    stop_event = stop_event or threading.Event()
    threads = [
        threading.Thread(
            target=poll_node,
            kwargs={
                "node": node,
                "channel_psks": channel_psks,
                "cfg": cfg,
                "batcher": batcher,
                "stop_event": stop_event,
                "port": port,
                "reconnect_delay": reconnect_delay,
            },
            daemon=True,
            name=f"tcp-poller-{node.host}",
        )
        for node in cfg.tcp_nodes
    ]
    for thread in threads:
        thread.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
            batcher.flush_if_due()
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)
        batcher.flush()
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(os.environ.get("REGIONS_CONFIG_PATH", "/config/regions.yaml"))
