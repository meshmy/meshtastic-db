"""gateway-agent's own decode/batch wiring (handle_from_radio_bytes,
HttpEnvelopeBatcher) in isolation from any real transport — the transport
layer itself (serial/BLE connection handling) is not fully mockable in CI
per the design's own §10.7 hedge, but the part downstream of "raw
FromRadio bytes off the wire" is exactly what mqtt-ingest/tcp-poller's own
decode paths already prove works via meshdb_common, so this only needs to
confirm gateway-agent wires it up the same way."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from meshtastic import mesh_pb2, portnums_pb2, telemetry_pb2

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gateway_agent_main():
    path = REPO_ROOT / "services" / "gateway-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("gateway_agent_main", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeSender:
    def __init__(self) -> None:
        self.sent_batches = []

    def send(self, envelopes) -> None:
        self.sent_batches.append(envelopes)


def _from_radio_with_packet(*, from_node: int, packet_id: int, battery_level: int) -> bytes:
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=battery_level))
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TELEMETRY_APP, payload=telemetry.SerializeToString())
    packet = mesh_pb2.MeshPacket(id=packet_id)
    setattr(packet, "from", from_node)
    packet.decoded.CopyFrom(data)
    return mesh_pb2.FromRadio(packet=packet).SerializeToString()


def test_handle_from_radio_bytes_decodes_a_packet_variant_and_batches_it():
    gw = _load_gateway_agent_main()
    sender = _FakeSender()
    batcher = gw.HttpEnvelopeBatcher(sender, batch_size=1, batch_interval=100.0)

    payload = _from_radio_with_packet(from_node=0x55, packet_id=7, battery_level=42)
    gw.handle_from_radio_bytes(payload, region="MY_919", source="serial", batcher=batcher)

    assert len(sender.sent_batches) == 1
    [envelope] = sender.sent_batches[0]
    assert envelope.node_id == 0x55
    assert envelope.region == "MY_919"
    assert envelope.source == "serial"
    assert envelope.packet_type == "Telemetry"
    assert envelope.fields[0].metric_name == "device_metrics.battery_level"
    assert envelope.fields[0].value_numeric == 42.0


def test_handle_from_radio_bytes_ignores_non_packet_payload_variants():
    gw = _load_gateway_agent_main()
    sender = _FakeSender()
    batcher = gw.HttpEnvelopeBatcher(sender, batch_size=1, batch_interval=100.0)

    from_radio = mesh_pb2.FromRadio(config_complete_id=1)
    gw.handle_from_radio_bytes(from_radio.SerializeToString(), region="MY_919", source="serial", batcher=batcher)

    assert sender.sent_batches == []


def test_handle_from_radio_bytes_ignores_unparseable_bytes():
    gw = _load_gateway_agent_main()
    sender = _FakeSender()
    batcher = gw.HttpEnvelopeBatcher(sender, batch_size=1, batch_interval=100.0)

    gw.handle_from_radio_bytes(b"not a valid protobuf message at all \xff\xfe", region="MY_919", source="serial", batcher=batcher)

    assert sender.sent_batches == []


def test_batcher_flushes_once_batch_size_is_reached_not_before():
    gw = _load_gateway_agent_main()
    sender = _FakeSender()
    batcher = gw.HttpEnvelopeBatcher(sender, batch_size=2, batch_interval=100.0)

    gw.handle_from_radio_bytes(
        _from_radio_with_packet(from_node=1, packet_id=1, battery_level=1), region="MY_919", source="serial", batcher=batcher
    )
    assert sender.sent_batches == []

    gw.handle_from_radio_bytes(
        _from_radio_with_packet(from_node=2, packet_id=2, battery_level=2), region="MY_919", source="serial", batcher=batcher
    )
    assert len(sender.sent_batches) == 1
    assert len(sender.sent_batches[0]) == 2
