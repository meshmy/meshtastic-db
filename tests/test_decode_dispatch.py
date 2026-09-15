"""PortNum dispatch, the Position/User special cases, and MQTT
decrypt — exercised via synthetic messages built in-process (no real
device/broker traffic; that's the corpus-replay test once mqtt-ingest and a
live database exist)."""

from datetime import datetime, timezone

from meshdb_common.decode import decode_data, decode_service_envelope, resolve_psk
from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2

COMMON_KWARGS = {
    "time": datetime(2026, 1, 1, tzinfo=timezone.utc),
    "node_id": 0x1234,
    "region": "MY_919",
    "source": "mqtt",
    "gateway_node_id": None,
    "packet_id": 1,
    "snr": None,
    "rssi": None,
    "hop_limit": None,
    "hop_start": None,
    "channel": None,
}


def _data(portnum, payload_msg) -> mesh_pb2.Data:
    return mesh_pb2.Data(portnum=portnum, payload=payload_msg.SerializeToString())


def test_decode_data_telemetry_routes_to_generic_fields():
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=99))
    envelope = decode_data(_data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry), **COMMON_KWARGS)

    assert envelope.packet_type == "Telemetry"
    assert envelope.position is None
    assert envelope.identity is None
    names = {fv.metric_name for fv in envelope.fields}
    assert "device_metrics.battery_level" in names


def test_decode_data_position_converts_wire_degrees_and_routes_to_position():
    position = mesh_pb2.Position(latitude_i=37_000_000, longitude_i=-122_000_000, altitude=15)
    envelope = decode_data(_data(portnums_pb2.PortNum.POSITION_APP, position), **COMMON_KWARGS)

    assert envelope.packet_type == "Position"
    assert envelope.fields == ()
    assert envelope.position.latitude == 3.7
    assert envelope.position.longitude == -12.2
    assert envelope.position.altitude == 15


def test_decode_data_position_without_fix_yields_no_position():
    envelope = decode_data(_data(portnums_pb2.PortNum.POSITION_APP, mesh_pb2.Position()), **COMMON_KWARGS)
    assert envelope.packet_type == "Position"
    assert envelope.position is None


def test_decode_data_nodeinfo_routes_to_identity():
    user = mesh_pb2.User(long_name="Ridgeline Relay", short_name="RDG1", is_licensed=False)
    envelope = decode_data(_data(portnums_pb2.PortNum.NODEINFO_APP, user), **COMMON_KWARGS)

    assert envelope.packet_type == "User"
    assert envelope.fields == ()
    assert envelope.identity.long_name == "Ridgeline Relay"
    assert envelope.identity.short_name == "RDG1"
    assert envelope.identity.is_licensed is False


def test_decode_data_unhandled_portnum_yields_no_fields():
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.ADMIN_APP, payload=b"")
    envelope = decode_data(data, **COMMON_KWARGS)
    assert envelope.fields == ()
    assert envelope.position is None
    assert envelope.identity is None


def _service_envelope(
    data: mesh_pb2.Data,
    *,
    from_node: int,
    packet_id: int,
    encrypt_with: bytes | None = None,
    channel_id: str = "LongFast",
) -> bytes:
    packet = mesh_pb2.MeshPacket(id=packet_id)
    setattr(packet, "from", from_node)
    if encrypt_with is None:
        packet.decoded.CopyFrom(data)
    else:
        from meshdb_common.decode import decrypt_payload

        packet.encrypted = decrypt_payload(data.SerializeToString(), packet_id, from_node, encrypt_with)
    envelope = mqtt_pb2.ServiceEnvelope(packet=packet, channel_id=channel_id, gateway_id="!0000abcd")
    return envelope.SerializeToString()


def test_decode_service_envelope_plaintext_round_trip():
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=42))
    raw = _service_envelope(_data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry), from_node=0x1234, packet_id=7)

    envelope = decode_service_envelope(raw, region="MY_919", source="mqtt", channel_psks={})

    assert envelope.packet_type == "Telemetry"
    assert envelope.node_id == 0x1234
    assert envelope.gateway_node_id == 0xABCD
    assert any(fv.metric_name == "device_metrics.battery_level" for fv in envelope.fields)


def test_decode_service_envelope_encrypted_round_trip_with_matching_psk():
    psk = resolve_psk("AQ==")
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=13))
    raw = _service_envelope(
        _data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry),
        from_node=0x5555,
        packet_id=99,
        encrypt_with=psk,
    )

    envelope = decode_service_envelope(raw, region="MY_919", source="mqtt", channel_psks={"LongFast": psk})

    assert envelope.packet_type == "Telemetry"
    assert any(fv.metric_name == "device_metrics.battery_level" for fv in envelope.fields)


def test_decode_service_envelope_encrypted_without_matching_psk_is_undecryptable():
    psk = resolve_psk("AQ==")
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=13))
    raw = _service_envelope(
        _data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry),
        from_node=0x5555,
        packet_id=99,
        encrypt_with=psk,
    )

    envelope = decode_service_envelope(raw, region="MY_919", source="mqtt", channel_psks={})

    assert envelope.packet_type == "UNDECRYPTABLE"
    assert envelope.portnum is None
    assert envelope.fields == ()
    assert envelope.node_id == 0x5555


def test_decode_service_envelope_falls_back_to_wildcard_channel_psk():
    psk = resolve_psk("AQ==")
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=21))
    raw = _service_envelope(
        _data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry),
        from_node=0x6666,
        packet_id=100,
        encrypt_with=psk,
        channel_id="ShortFast",  # a default-modem-preset channel, not explicitly configured
    )

    envelope = decode_service_envelope(raw, region="MY_919", source="mqtt", channel_psks={"*": psk})

    assert envelope.packet_type == "Telemetry"
    assert any(fv.metric_name == "device_metrics.battery_level" for fv in envelope.fields)


def test_decode_service_envelope_prefers_named_channel_psk_over_wildcard():
    named_psk = resolve_psk("AQ==")
    wrong_wildcard_psk = bytes(range(16))
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=34))
    raw = _service_envelope(
        _data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry),
        from_node=0x7777,
        packet_id=101,
        encrypt_with=named_psk,
        channel_id="LongFast",
    )

    envelope = decode_service_envelope(
        raw, region="MY_919", source="mqtt", channel_psks={"LongFast": named_psk, "*": wrong_wildcard_psk}
    )

    assert envelope.packet_type == "Telemetry"
    assert any(fv.metric_name == "device_metrics.battery_level" for fv in envelope.fields)
