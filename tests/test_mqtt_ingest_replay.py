"""MQTT corpus replay (§10.3): synthesize ServiceEnvelope byte blobs and
publish them over a real MQTT broker, run mqtt-ingest's actual `run()`
against a real, disposable TimescaleDB, and assert the expected rows land —
end to end, not a mock of either the broker or the database.

Needs Docker (mosquitto + timescaledb containers, see AGENTS.md /
`make test-integration`), so it's marked `integration` like the other
Docker-backed tests."""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import psycopg
import pytest
from meshdb_common.decode import decrypt_payload, resolve_psk
from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2
from paho.mqtt.client import CallbackAPIVersion, Client

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
REGION = "MY_919"
DEFAULT_PSK = resolve_psk("AQ==")


def _load_mqtt_ingest_main():
    path = REPO_ROOT / "services" / "mqtt-ingest" / "main.py"
    spec = importlib.util.spec_from_file_location("mqtt_ingest_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _service_envelope(*, from_node: int, packet_id: int, battery_level: int, encrypt: bool) -> bytes:
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=battery_level))
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TELEMETRY_APP, payload=telemetry.SerializeToString())

    packet = mesh_pb2.MeshPacket(id=packet_id)
    setattr(packet, "from", from_node)
    if encrypt:
        packet.encrypted = decrypt_payload(data.SerializeToString(), packet_id, from_node, DEFAULT_PSK)
    else:
        packet.decoded.CopyFrom(data)
    envelope = mqtt_pb2.ServiceEnvelope(packet=packet, channel_id="LongFast", gateway_id="!0000abcd")
    return envelope.SerializeToString()


@pytest.fixture
def regions_yaml(tmp_path, mosquitto_broker) -> Path:
    host, port = mosquitto_broker
    path = tmp_path / "regions.yaml"
    path.write_text(f"""\
allowed_regions: [{REGION}]
mqtt:
  topic_template: "msh/{{region}}/2/{{version}}/#"
  brokers:
    - host: {host}
      port: {port}
      tls: false
  channels:
    - name: LongFast
      psk_base64: "AQ=="
tcp_nodes: []
gateway_agent:
  region: null
  connection: {{type: serial, port: /dev/ttyUSB0}}
""")
    return path


def test_mqtt_ingest_decodes_and_writes_published_packets(ingest_dsn, mosquitto_broker, regions_yaml):
    mqtt_ingest_main = _load_mqtt_ingest_main()
    broker_host, broker_port = mosquitto_broker
    plaintext_node, encrypted_node = 0x7777, 0x8888

    conn = psycopg.connect(ingest_dsn)
    stop_event = threading.Event()
    ingest_thread = threading.Thread(
        target=mqtt_ingest_main.run,
        kwargs={"config_path": regions_yaml, "conn": conn, "batch_size": 1, "batch_interval": 0.2, "stop_event": stop_event},
        daemon=True,
    )
    ingest_thread.start()
    try:
        publisher = Client(CallbackAPIVersion.VERSION2)
        publisher.connect(broker_host, broker_port)
        publisher.loop_start()
        time.sleep(1)  # let mqtt-ingest's subscription land before publishing

        publisher.publish(
            f"msh/{REGION}/2/e/LongFast/!00007777",
            _service_envelope(from_node=plaintext_node, packet_id=1, battery_level=55, encrypt=False),
        )
        publisher.publish(
            f"msh/{REGION}/2/e/LongFast/!00008888",
            _service_envelope(from_node=encrypted_node, packet_id=2, battery_level=66, encrypt=True),
        )

        deadline = time.monotonic() + 30
        rows: dict[int, float] = {}
        while time.monotonic() < deadline and len(rows) < 2:
            time.sleep(0.5)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT node_id, value_numeric FROM metric "
                    "WHERE node_id IN (%s, %s) AND metric_name = 'device_metrics.battery_level'",
                    (plaintext_node, encrypted_node),
                )
                rows = dict(cur.fetchall())

        publisher.loop_stop()
        publisher.disconnect()
    finally:
        stop_event.set()
        ingest_thread.join(timeout=10)

    assert rows == {plaintext_node: 55.0, encrypted_node: 66.0}
