"""Gateway agent (§5 of the design): runs on a host with physical BLE/serial
access to one radio, decodes its traffic via meshdb_common (the same
reflection-based decode every other ingestion source uses), and POSTs
batches of decoded envelopes to a remote ingest-api instead of writing to
Postgres directly.

**Why this doesn't depend on the `meshtastic` PyPI package** (the plan this
repo was built from flagged this as needing a "transport-interception
spike" — this is that spike's outcome, not an assumption): Meshtastic's
serial and TCP local APIs share the exact same wire framing and protobuf
messages (tcp-poller already proves this for TCP — see its own module
docstring), so the serial connection here reuses meshdb_common's own
`stream_framing`/`mesh_pb2` exactly like tcp-poller does, via `pyserial`
for the byte source. BLE uses Meshtastic's documented GATT service/
characteristic UUIDs directly (via `bleak`) instead of the library's
`BLEInterface`. Avoiding the `meshtastic` package entirely also sidesteps a
real Python import collision it would otherwise cause: that package's own
bundled protobuf classes live at the same import path
(`meshtastic.mesh_pb2`, etc.) that `meshdb_common/__init__.py` puts on
`sys.path` for *this repo's own* generated classes, and a plain top-level
package (the pip one) always wins that name over a namespace-package
portion (this repo's `generated/meshtastic/`, which has no `__init__.py`)
regardless of sys.path order — so having both installed in one interpreter
would silently make every decode in this process use the pip package's
(possibly stale) schema instead of this repo's freshly-regenerated one.
Every other property upstream cared about still holds: the transport layer
here only ever produces raw bytes off the wire; meshdb_common's own
regenerated classes always do the field-level decode.

BLE has not been validated against real hardware (flagged in AGENTS.md,
same as tcp-poller's own hedge on hardware-dependent paths) — the GATT
UUIDs are Meshtastic's documented ones, but a live BLE radio is needed to
confirm the read/notify sequence against real firmware.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml
from meshdb_common.config import resolve_secret
from meshdb_common.decode import decode_mesh_packet
from meshdb_common.envelope import DecodedPacketEnvelope
from meshdb_common.serialization import envelope_from_dict, envelope_to_dict
from meshdb_common.stream_framing import FrameReader, encode_frame
from meshtastic import mesh_pb2

logger = logging.getLogger("gateway-agent")

DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_INTERVAL_SECONDS = 2.0
DEFAULT_RECONNECT_DELAY_SECONDS = 5.0
SERIAL_BAUD_RATE = 115200
RECV_CHUNK_SIZE = 4096

# Meshtastic's documented BLE GATT service/characteristic UUIDs (stable
# across firmware versions — part of the wire protocol, not an
# implementation detail of any one client).
BLE_FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
BLE_TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
BLE_FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"


# --- config -----------------------------------------------------------------


@dataclass(frozen=True)
class SerialConnection:
    port: str


@dataclass(frozen=True)
class BleConnection:
    address: str


@dataclass(frozen=True)
class AgentConfig:
    region: str
    connection: SerialConnection | BleConnection
    ingest_api_url: str
    batch_size: int
    batch_interval: float
    wal_path: Path


def load_agent_config(path: str | Path) -> AgentConfig:
    raw = yaml.safe_load(Path(path).read_text())
    conn_raw = raw["connection"]
    conn_type = conn_raw["type"]
    if conn_type == "serial":
        connection: SerialConnection | BleConnection = SerialConnection(port=conn_raw["port"])
    elif conn_type == "ble":
        connection = BleConnection(address=conn_raw["ble_address"])
    else:
        raise ValueError(f"unknown connection type {conn_type!r}")

    batch_raw = raw.get("batch") or {}
    return AgentConfig(
        region=raw["region"],
        connection=connection,
        ingest_api_url=raw["ingest_api"]["url"],
        batch_size=batch_raw.get("size", DEFAULT_BATCH_SIZE),
        batch_interval=batch_raw.get("interval_seconds", DEFAULT_BATCH_INTERVAL_SECONDS),
        wal_path=Path((raw.get("wal") or {}).get("path", "/var/lib/gateway-agent/wal.jsonl")),
    )


# --- WAL spillover ------------------------------------------------------------


class Wal:
    """Append-only local spillover for envelope batches that couldn't be
    POSTed to ingest-api — replayed on the next send attempt instead of
    being dropped. One JSON envelope per line (meshdb_common.serialization's
    wire format) so a partially-written last line from a crash mid-append
    is simply skippable, not corrupting.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def has_pending(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def append(self, envelopes: list[DecodedPacketEnvelope]) -> None:
        with self._path.open("a") as f:
            for env in envelopes:
                f.write(json.dumps(envelope_to_dict(env)))
                f.write("\n")

    def replay(self) -> list[DecodedPacketEnvelope]:
        """Returns every spilled envelope without clearing the file — only
        call clear() after a caller confirms it forwarded them
        successfully, so a crash mid-replay doesn't lose anything."""
        if not self._path.exists():
            return []
        envelopes = []
        with self._path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelopes.append(envelope_from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning("skipping unparseable WAL line (likely a torn write from a prior crash)")
        return envelopes

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


class IngestApiSender:
    """POSTs batches of decoded envelopes to ingest-api. Any pending WAL
    backlog is always retried before a new batch, and in the same relative
    order it was recorded in, so a prolonged outage doesn't reorder data;
    on failure the new batch joins the backlog rather than being sent out
    of turn."""

    def __init__(self, url: str, token: str | None, wal: Wal, *, timeout: float = 10.0) -> None:
        self._url = url
        self._token = token
        self._wal = wal
        self._timeout = timeout

    def _post(self, envelopes: list[DecodedPacketEnvelope]) -> bool:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        body = [envelope_to_dict(env) for env in envelopes]
        try:
            response = requests.post(self._url, json=body, headers=headers, timeout=self._timeout)
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("ingest-api POST failed: %s", exc)
            return False

    def send(self, envelopes: list[DecodedPacketEnvelope]) -> None:
        if not envelopes:
            return
        if self._wal.has_pending():
            pending = self._wal.replay()
            if not self._post(pending):
                self._wal.append(envelopes)
                return
            self._wal.clear()
        if not self._post(envelopes):
            self._wal.append(envelopes)


# --- batching -----------------------------------------------------------------


class HttpEnvelopeBatcher:
    """Buffers decoded envelopes and flushes them to IngestApiSender once
    `batch_size` is reached or `batch_interval` seconds have passed since
    the oldest buffered envelope — the HTTP-forwarding analogue of
    meshdb_common.batching.EnvelopeBatcher, which flushes to Postgres
    directly instead (not shared with it: the flush target and failure
    handling differ enough that reuse would need its own seam)."""

    def __init__(self, sender: IngestApiSender, *, batch_size: int, batch_interval: float) -> None:
        self._sender = sender
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
            self._sender.send(batch)


def handle_from_radio_bytes(payload: bytes, *, region: str, source: str, batcher: HttpEnvelopeBatcher) -> None:
    """Parses one raw serialized FromRadio message (already off the wire,
    regardless of transport) via this repo's own generated classes and
    decodes any wrapped MeshPacket. Local-API traffic is already decrypted
    by the node itself, so channel_id=None — only a (not currently
    configured) wildcard PSK could ever apply to the rare packet the node
    still forwards encrypted."""
    from_radio = mesh_pb2.FromRadio()
    try:
        from_radio.ParseFromString(payload)
    except Exception:
        logger.exception("failed to parse FromRadio frame")
        return
    if from_radio.WhichOneof("payload_variant") != "packet":
        return
    envelope = decode_mesh_packet(from_radio.packet, region=region, source=source, channel_psks={}, channel_id=None)
    batcher.add(envelope)


# --- serial transport ---------------------------------------------------------


def _read_serial_frames(port: str, *, stop_event: threading.Event):
    """Yields raw FromRadio payload bytes off a serial local-API connection
    — same START1/START2 length-prefixed framing as the TCP local API (see
    module docstring), just with pyserial as the byte source instead of a
    socket. Raises on any serial error so the caller's reconnect loop can
    retry."""
    import serial

    with serial.Serial(port, SERIAL_BAUD_RATE, timeout=1) as ser:
        want_config_id = random.randint(1, 0xFFFFFFFF)
        to_radio = mesh_pb2.ToRadio(want_config_id=want_config_id)
        ser.write(encode_frame(to_radio.SerializeToString()))
        logger.info("connected to serial port %s", port)
        reader = FrameReader()
        while not stop_event.is_set():
            chunk = ser.read(RECV_CHUNK_SIZE)
            if not chunk:
                continue
            yield from reader.feed(chunk)


# --- BLE transport --------------------------------------------------------


def _read_ble_frames(address: str, *, stop_event: threading.Event):
    """Yields raw FromRadio payload bytes off a BLE local-API connection,
    via Meshtastic's documented GATT characteristics rather than the
    meshtastic package's own BLEInterface (see module docstring). Not
    hardware-validated — see AGENTS.md."""
    import asyncio
    import queue

    from bleak import BleakClient

    out: queue.Queue = queue.Queue()

    async def drain(client: BleakClient) -> None:
        while True:
            payload = await client.read_gatt_char(BLE_FROMRADIO_UUID)
            if not payload:
                return
            out.put(payload)

    async def run_ble() -> None:
        async with BleakClient(address) as client:

            async def on_from_num(_, __) -> None:
                await drain(client)

            await client.start_notify(BLE_FROMNUM_UUID, on_from_num)
            logger.info("connected to BLE device %s", address)
            await drain(client)  # anything already queued before we subscribed
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
            await client.stop_notify(BLE_FROMNUM_UUID)

    def runner() -> None:
        asyncio.run(run_ble())

    ble_thread = threading.Thread(target=runner, daemon=True, name="gateway-agent-ble-loop")
    ble_thread.start()
    while not stop_event.is_set() and ble_thread.is_alive():
        try:
            yield out.get(timeout=0.5)
        except queue.Empty:
            continue


# --- orchestration --------------------------------------------------------


def run(
    config_path: str | Path,
    *,
    ingest_api_url: str | None = None,
    token: str | None = None,
    wal_path: str | Path | None = None,
    batch_size: int | None = None,
    batch_interval: float | None = None,
    reconnect_delay: float | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or threading.Event()
    cfg = load_agent_config(config_path)
    token = token if token is not None else resolve_secret("INGEST_API_TOKEN")
    reconnect_delay = (
        reconnect_delay
        if reconnect_delay is not None
        else float(os.environ.get("GATEWAY_AGENT_RECONNECT_DELAY_SECONDS", DEFAULT_RECONNECT_DELAY_SECONDS))
    )

    wal = Wal(Path(wal_path) if wal_path is not None else cfg.wal_path)
    sender = IngestApiSender(ingest_api_url or cfg.ingest_api_url, token, wal)
    batcher = HttpEnvelopeBatcher(
        sender,
        batch_size=batch_size if batch_size is not None else cfg.batch_size,
        batch_interval=batch_interval if batch_interval is not None else cfg.batch_interval,
    )

    if isinstance(cfg.connection, SerialConnection):
        source = "serial"

        def frames():
            return _read_serial_frames(cfg.connection.port, stop_event=stop_event)
    elif isinstance(cfg.connection, BleConnection):
        source = "ble"

        def frames():
            return _read_ble_frames(cfg.connection.address, stop_event=stop_event)
    else:
        raise TypeError(f"unsupported connection {cfg.connection!r}")

    def reader_loop() -> None:
        while not stop_event.is_set():
            try:
                for payload in frames():
                    if stop_event.is_set():
                        break
                    handle_from_radio_bytes(payload, region=cfg.region, source=source, batcher=batcher)
            except Exception:
                logger.exception("%s connection error — reconnecting in %.0fs", source, reconnect_delay)
                stop_event.wait(reconnect_delay)

    reader_thread = threading.Thread(target=reader_loop, daemon=True, name="gateway-agent-reader")
    reader_thread.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
            batcher.flush_if_due()
    finally:
        stop_event.set()
        reader_thread.join(timeout=5)
        batcher.flush()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(os.environ.get("GATEWAY_AGENT_CONFIG_PATH", "/config/config.yaml"))
