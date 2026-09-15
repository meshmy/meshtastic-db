"""Dataclasses describing a decoded Meshtastic packet, independent of any
particular transport (MQTT/TCP/BLE/serial) or database representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class FieldValue:
    """One leaf scalar field extracted from a decoded protobuf payload,
    shaped to map directly onto a `metric` row."""

    metric_name: str
    value_type: str  # 'numeric' | 'bool' | 'text' | 'enum'
    value_numeric: float | None = None
    value_text: str | None = None
    value_bool: bool | None = None


@dataclass(frozen=True)
class PositionFix:
    """One Position-packet reading, destined for `node_position_history`.
    Latitude/longitude are already converted from the wire format's
    int32-degrees-times-1e7 encoding to plain signed-degree floats."""

    latitude: float
    longitude: float
    altitude: float | None = None
    location_source: str | None = None
    ground_speed: float | None = None
    ground_track: float | None = None


@dataclass(frozen=True)
class NodeIdentityUpdate:
    """One User/NodeInfo-packet reading, destined for `node_identity`."""

    long_name: str | None = None
    short_name: str | None = None
    hw_model: str | None = None
    role: str | None = None
    is_licensed: bool | None = None


@dataclass(frozen=True)
class DecodedPacketEnvelope:
    """Everything decode.py knows about one MeshPacket, ready to hand to
    meshdb_common.db's shared write path. Exactly one of `fields`,
    `position`, `identity` is populated, per packet_type."""

    time: datetime
    node_id: int
    region: str
    source: str  # 'mqtt' | 'ble' | 'serial' | 'tcp'
    packet_type: str  # 'Telemetry', 'Position', 'User', ..., 'UNDECRYPTABLE'
    portnum: int | None
    gateway_node_id: int | None = None
    packet_id: int | None = None
    snr: float | None = None
    rssi: int | None = None
    hop_limit: int | None = None
    hop_start: int | None = None
    channel: int | None = None
    fields: tuple[FieldValue, ...] = field(default_factory=tuple)
    position: PositionFix | None = None
    identity: NodeIdentityUpdate | None = None
