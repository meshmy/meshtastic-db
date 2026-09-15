"""tcp-poller corpus replay (§10.3's TCP analogue — connects to a
mocked/simulated TCP endpoint and writes rows): run tcp-poller's actual
`run()` against a real, disposable TimescaleDB and a
lightweight in-process fake radio TCP server speaking the real wire framing
(meshdb_common.stream_framing) — not a mock of tcp-poller's own code, just a
stand-in for hardware neither CI nor a dev machine has.

Needs Docker for `ingest_dsn` (see AGENTS.md / `make test-integration`), so
it's marked `integration` like the other Docker-backed tests. The fake radio
server itself is a plain local TCP socket — no Docker needed for it."""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import psycopg
import pytest
from meshdb_common.stream_framing import encode_frame
from meshtastic import mesh_pb2, portnums_pb2, telemetry_pb2

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
REGION = "MY_919"


def _load_tcp_poller_main():
    path = REPO_ROOT / "services" / "tcp-poller" / "main.py"
    spec = importlib.util.spec_from_file_location("tcp_poller_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mesh_packet(*, from_node: int, packet_id: int, battery_level: int) -> mesh_pb2.MeshPacket:
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=battery_level))
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TELEMETRY_APP, payload=telemetry.SerializeToString())
    packet = mesh_pb2.MeshPacket(id=packet_id)
    setattr(packet, "from", from_node)
    packet.decoded.CopyFrom(data)
    return packet


class FakeRadioServer:
    """Stand-in for one Meshtastic node's local TCP API: accepts a single
    connection, drains the client's initial ToRadio{want_config_id} frame,
    then lets the test push FromRadio frames on demand."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._conn: socket.socket | None = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        conn, _ = self._sock.accept()
        conn.recv(4096)  # drain the initial ToRadio{want_config_id} frame
        self._conn = conn

    def wait_for_client(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self._conn is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self._conn is not None, "tcp-poller never connected"

    def send_packet(self, packet: mesh_pb2.MeshPacket) -> None:
        from_radio = mesh_pb2.FromRadio(packet=packet)
        assert self._conn is not None
        self._conn.sendall(encode_frame(from_radio.SerializeToString()))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._sock.close()


@pytest.fixture
def fake_radio_server():
    server = FakeRadioServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def regions_yaml(tmp_path, fake_radio_server) -> Path:
    path = tmp_path / "regions.yaml"
    path.write_text(f"""\
allowed_regions: [{REGION}]
mqtt:
  topic_template: "msh/{{region}}/2/{{version}}/#"
  brokers: []
  channels: []
tcp_nodes:
  - host: {fake_radio_server.host}
    region: {REGION}
gateway_agent:
  region: null
  connection: {{type: serial, port: /dev/ttyUSB0}}
""")
    return path


def test_tcp_poller_decodes_and_writes_packets_from_a_simulated_node(ingest_dsn, fake_radio_server, regions_yaml):
    tcp_poller_main = _load_tcp_poller_main()
    node_id = 0x9999

    conn = psycopg.connect(ingest_dsn)
    stop_event = threading.Event()
    poller_thread = threading.Thread(
        target=tcp_poller_main.run,
        kwargs={
            "config_path": regions_yaml,
            "conn": conn,
            "batch_size": 1,
            "batch_interval": 0.2,
            "port": fake_radio_server.port,
            "reconnect_delay": 1.0,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    poller_thread.start()
    try:
        fake_radio_server.wait_for_client()
        fake_radio_server.send_packet(_mesh_packet(from_node=node_id, packet_id=1, battery_level=77))

        deadline = time.monotonic() + 30
        rows: dict[int, float] = {}
        while time.monotonic() < deadline and not rows:
            time.sleep(0.5)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT node_id, value_numeric FROM metric "
                    "WHERE node_id = %s AND metric_name = 'device_metrics.battery_level'",
                    (node_id,),
                )
                rows = dict(cur.fetchall())
    finally:
        stop_event.set()
        poller_thread.join(timeout=10)

    assert rows == {node_id: 77.0}
