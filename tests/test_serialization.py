"""Round-trip coverage for meshdb_common.serialization — the JSON wire
format gateway-agent uses to POST already-decoded envelopes to ingest-api
(and to spill them to its local WAL), for each of the four envelope shapes
decode.py can produce (fields, position, identity, UNDECRYPTABLE)."""

from __future__ import annotations

from datetime import datetime, timezone

from meshdb_common.envelope import (
    DecodedPacketEnvelope,
    FieldValue,
    NodeIdentityUpdate,
    PositionFix,
)
from meshdb_common.serialization import envelope_from_dict, envelope_to_dict

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _roundtrip(env: DecodedPacketEnvelope) -> DecodedPacketEnvelope:
    return envelope_from_dict(envelope_to_dict(env))


def test_roundtrips_a_metric_fields_envelope():
    env = DecodedPacketEnvelope(
        time=NOW,
        node_id=0x1234,
        region="MY_919",
        source="ble",
        packet_type="Telemetry",
        portnum=67,
        gateway_node_id=0xABCD,
        packet_id=42,
        snr=5.5,
        rssi=-90,
        hop_limit=3,
        hop_start=5,
        channel=0,
        fields=(
            FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=77.0),
            FieldValue(metric_name="device_metrics.uptime_seconds", value_type="numeric", value_numeric=123.0),
        ),
    )
    assert _roundtrip(env) == env


def test_roundtrips_a_position_envelope():
    env = DecodedPacketEnvelope(
        time=NOW,
        node_id=0x1234,
        region="MY_919",
        source="serial",
        packet_type="Position",
        portnum=3,
        position=PositionFix(latitude=1.23, longitude=4.56, altitude=10.0, location_source="LOC_INTERNAL"),
    )
    assert _roundtrip(env) == env


def test_roundtrips_an_identity_envelope():
    env = DecodedPacketEnvelope(
        time=NOW,
        node_id=0x1234,
        region="MY_919",
        source="serial",
        packet_type="User",
        portnum=4,
        identity=NodeIdentityUpdate(long_name="Node One", short_name="N1", hw_model="TBEAM", role="CLIENT", is_licensed=False),
    )
    assert _roundtrip(env) == env


def test_roundtrips_an_undecryptable_envelope_with_no_payload():
    env = DecodedPacketEnvelope(
        time=NOW,
        node_id=0x1234,
        region="MY_919",
        source="ble",
        packet_type="UNDECRYPTABLE",
        portnum=None,
    )
    assert _roundtrip(env) == env
