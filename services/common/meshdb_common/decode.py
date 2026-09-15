"""Reflection-based decode of Meshtastic packets.

`walk_message` is what makes a brand-new upstream protobuf field become a
new `metric_name` with zero code change here: it walks whatever fields are
actually populated on a decoded message, generically, rather than switching
on a fixed field list. The only hardcoded knowledge in this module is
`PORTNUM_MESSAGE_MAP` (which top-level message class a given PortNum's
payload bytes decode as — protobuf has no built-in link from a PortNum
integer to a message class) and the Position/User special cases (they route
to `node_position_history`/`node_identity` instead of generic `metric` rows).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import DecodeError, Message
from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2

from .envelope import DecodedPacketEnvelope, FieldValue, NodeIdentityUpdate, PositionFix

PortNum = portnums_pb2.PortNum

# A config/regions.yaml channel entry named this is a fallback PSK tried for
# any channel_id with no specific entry of its own — see decode_service_envelope.
WILDCARD_CHANNEL = "*"

# The one hardcoded portnum -> payload-message-class dispatch table: a new
# *field* inside one of these messages needs no change here (walk_message
# picks it up automatically); only an entirely new top-level portnum does.
PORTNUM_MESSAGE_MAP: dict[int, type[Message]] = {
    PortNum.POSITION_APP: mesh_pb2.Position,
    PortNum.NODEINFO_APP: mesh_pb2.User,
    PortNum.TELEMETRY_APP: telemetry_pb2.Telemetry,
    PortNum.ROUTING_APP: mesh_pb2.Routing,
    PortNum.NEIGHBORINFO_APP: mesh_pb2.NeighborInfo,
    PortNum.TRACEROUTE_APP: mesh_pb2.RouteDiscovery,
    PortNum.MAP_REPORT_APP: mqtt_pb2.MapReport,
}

# Every other currently-known portnum: deliberately unhandled (no metric
# rows emitted for it), as opposed to simply unmapped. The portnum-coverage
# test asserts every value in portnums_pb2.PortNum is in exactly one of
# these two collections, so a submodule bump introducing a new portnum fails
# the test instead of silently dropping data.
KNOWN_UNHANDLED_PORTNUMS: set[int] = {
    PortNum.UNKNOWN_APP,
    PortNum.TEXT_MESSAGE_APP,
    PortNum.REMOTE_HARDWARE_APP,
    PortNum.ADMIN_APP,
    PortNum.TEXT_MESSAGE_COMPRESSED_APP,
    PortNum.WAYPOINT_APP,
    PortNum.AUDIO_APP,
    PortNum.DETECTION_SENSOR_APP,
    PortNum.ALERT_APP,
    PortNum.KEY_VERIFICATION_APP,
    PortNum.REMOTE_SHELL_APP,
    PortNum.REPLY_APP,
    PortNum.IP_TUNNEL_APP,
    PortNum.PAXCOUNTER_APP,
    PortNum.STORE_FORWARD_PLUSPLUS_APP,
    PortNum.NODE_STATUS_APP,
    PortNum.MESH_BEACON_APP,
    PortNum.PAGING_APP,
    PortNum.SERIAL_APP,
    PortNum.STORE_FORWARD_APP,
    PortNum.RANGE_TEST_APP,
    PortNum.ZPS_APP,
    PortNum.SIMULATOR_APP,
    PortNum.ATAK_PLUGIN,
    PortNum.POWERSTRESS_APP,
    PortNum.LORAWAN_BRIDGE,
    PortNum.RETICULUM_TUNNEL_APP,
    PortNum.CAYENNE_APP,
    PortNum.ATAK_PLUGIN_V2,
    PortNum.LORA_OTA_APP,
    PortNum.GROUPALARM_APP,
    PortNum.PRIVATE_APP,
    PortNum.ATAK_FORWARDER,
    PortNum.MAX,
}

_PACKET_TYPE_BY_PORTNUM: dict[int, str] = {
    PortNum.POSITION_APP: "Position",
    PortNum.NODEINFO_APP: "User",
    PortNum.TELEMETRY_APP: "Telemetry",
    PortNum.ROUTING_APP: "Routing",
    PortNum.NEIGHBORINFO_APP: "NeighborInfo",
    PortNum.TRACEROUTE_APP: "Traceroute",
    PortNum.MAP_REPORT_APP: "MapReport",
}

_NUMERIC_FIELD_TYPES = {
    FieldDescriptor.TYPE_DOUBLE,
    FieldDescriptor.TYPE_FLOAT,
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_INT32,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_FIXED32,
    FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_SFIXED32,
    FieldDescriptor.TYPE_SFIXED64,
    FieldDescriptor.TYPE_SINT32,
    FieldDescriptor.TYPE_SINT64,
}


def _leaf(fd: FieldDescriptor, value, path: str) -> FieldValue:
    if fd.type == FieldDescriptor.TYPE_ENUM:
        enum_value = fd.enum_type.values_by_number.get(value)
        label = enum_value.name if enum_value is not None else str(value)
        return FieldValue(metric_name=path, value_type="enum", value_numeric=float(value), value_text=label)
    if fd.type == FieldDescriptor.TYPE_BOOL:
        return FieldValue(metric_name=path, value_type="bool", value_bool=bool(value))
    if fd.type == FieldDescriptor.TYPE_STRING:
        return FieldValue(metric_name=path, value_type="text", value_text=value)
    if fd.type == FieldDescriptor.TYPE_BYTES:
        return FieldValue(metric_name=path, value_type="text", value_text=value.hex())
    if fd.type in _NUMERIC_FIELD_TYPES:
        return FieldValue(metric_name=path, value_type="numeric", value_numeric=float(value))
    raise ValueError(f"unhandled protobuf field type {fd.type} at {path!r}")


def walk_message(msg: Message, path_prefix: str = "") -> Iterator[FieldValue]:
    """Recursively walk a decoded protobuf message, yielding leaf scalar
    fields. Operates on the inner decoded app payload (Telemetry,
    NeighborInfo, ...), never on the outer MeshPacket/Data wrapper."""
    for fd, value in msg.ListFields():
        full_path = f"{path_prefix}.{fd.name}" if path_prefix else fd.name
        if fd.is_repeated:
            if fd.type == FieldDescriptor.TYPE_MESSAGE:
                for i, item in enumerate(value):
                    yield from walk_message(item, f"{full_path}[{i}]")
            else:
                for i, item in enumerate(value):
                    yield _leaf(fd, item, f"{full_path}[{i}]")
        elif fd.type == FieldDescriptor.TYPE_MESSAGE:
            yield from walk_message(value, full_path)
        else:
            yield _leaf(fd, value, full_path)


def _enum_name(msg: Message, field_name: str) -> str:
    fd = msg.DESCRIPTOR.fields_by_name[field_name]
    value = getattr(msg, field_name)
    enum_value = fd.enum_type.values_by_number.get(value)
    return enum_value.name if enum_value is not None else str(value)


def parse_node_id(value: str) -> int | None:
    """Parse Meshtastic's '!<hex>' string node-ID form (used for
    ServiceEnvelope.gateway_id and User.id) into a plain int, as stored in
    every node_id/gateway_node_id column."""
    if not value:
        return None
    return int(value.lstrip("!"), 16)


def _extract_position(position: mesh_pb2.Position) -> PositionFix | None:
    if not position.HasField("latitude_i") or not position.HasField("longitude_i"):
        return None
    return PositionFix(
        # Wire format is int32 degrees * 1e7, not float degrees.
        latitude=position.latitude_i / 1e7,
        longitude=position.longitude_i / 1e7,
        altitude=float(position.altitude) if position.HasField("altitude") else None,
        location_source=_enum_name(position, "location_source"),
        ground_speed=float(position.ground_speed) if position.HasField("ground_speed") else None,
        ground_track=float(position.ground_track) if position.HasField("ground_track") else None,
    )


def _extract_identity(user: mesh_pb2.User) -> NodeIdentityUpdate:
    return NodeIdentityUpdate(
        long_name=user.long_name or None,
        short_name=user.short_name or None,
        hw_model=_enum_name(user, "hw_model"),
        role=_enum_name(user, "role"),
        is_licensed=user.is_licensed,
    )


# The fixed AES-128 key Meshtastic firmware substitutes whenever a channel's
# configured PSK is the single-byte "simple key" sentinel 0x01 (base64
# "AQ=="), used by the default LongFast channel.
_DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")


def resolve_psk(psk_base64: str) -> bytes:
    """Expand a configured channel PSK to raw key bytes. AES-128 vs AES-256
    is selected by the resulting key length (16 vs 32 bytes), not fixed."""
    raw = base64.b64decode(psk_base64)
    if raw == b"\x01":
        return _DEFAULT_PSK
    return raw


def _ctr_nonce(packet_id: int, from_node: int) -> bytes:
    return packet_id.to_bytes(8, "little") + from_node.to_bytes(4, "little") + bytes(4)


def decrypt_payload(encrypted: bytes, packet_id: int, from_node: int, psk: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(psk), modes.CTR(_ctr_nonce(packet_id, from_node)))
    decryptor = cipher.decryptor()
    return decryptor.update(encrypted) + decryptor.finalize()


def decode_data(data: mesh_pb2.Data, **common_kwargs) -> DecodedPacketEnvelope:
    """Dispatch one already-decrypted Data payload by its portnum."""
    portnum = data.portnum
    packet_type = _PACKET_TYPE_BY_PORTNUM.get(portnum, "Unknown")

    if portnum == PortNum.NODEINFO_APP:
        user = mesh_pb2.User()
        user.ParseFromString(data.payload)
        return DecodedPacketEnvelope(packet_type=packet_type, portnum=portnum, identity=_extract_identity(user), **common_kwargs)

    if portnum == PortNum.POSITION_APP:
        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        return DecodedPacketEnvelope(packet_type=packet_type, portnum=portnum, position=_extract_position(position), **common_kwargs)

    message_cls = PORTNUM_MESSAGE_MAP.get(portnum)
    if message_cls is None:
        return DecodedPacketEnvelope(packet_type=packet_type, portnum=portnum, **common_kwargs)

    msg = message_cls()
    msg.ParseFromString(data.payload)
    return DecodedPacketEnvelope(packet_type=packet_type, portnum=portnum, fields=tuple(walk_message(msg)), **common_kwargs)


def decode_service_envelope(
    raw: bytes,
    *,
    region: str,
    source: str,
    channel_psks: dict[str, bytes],
) -> DecodedPacketEnvelope:
    """Parse a raw ServiceEnvelope, decrypt its payload if needed, and
    dispatch by portnum. Returns an envelope with packet_type
    'UNDECRYPTABLE' (transport metadata only, no fields/position/identity)
    when the payload is encrypted and no matching channel PSK is
    configured, or decryption/parsing fails."""
    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.ParseFromString(raw)
    packet = envelope.packet

    from_node = getattr(packet, "from")
    rx_time = (
        datetime.fromtimestamp(packet.rx_time, tz=timezone.utc)
        if packet.rx_time
        else datetime.now(tz=timezone.utc)
    )
    common_kwargs = {
        "time": rx_time,
        "node_id": from_node,
        "region": region,
        "source": source,
        "gateway_node_id": parse_node_id(envelope.gateway_id),
        "packet_id": packet.id,
        "snr": packet.rx_snr,
        "rssi": packet.rx_rssi if packet.HasField("rx_rssi") else None,
        "hop_limit": packet.hop_limit,
        "hop_start": packet.hop_start,
        "channel": packet.channel,
    }

    if packet.WhichOneof("payload_variant") == "encrypted":
        # A channel keyed by name takes priority; "*" (WILDCARD_CHANNEL) is
        # a fallback tried for any channel_id with no specific entry — the
        # default PSK ("AQ==") is a single fixed key independent of channel
        # name (Meshtastic's default-preset channels — LongFast, ShortFast,
        # MediumSlow, etc. — and any custom-named channel left on the
        # default key all use it), so one wildcard entry decrypts all of
        # them without enumerating every possible name. A channel actually
        # using a different, unconfigured custom PSK will still attempt
        # decryption against the wildcard key and most likely fail to
        # parse as a valid Data message (caught below as UNDECRYPTABLE),
        # since there's no way to tell "uses the default key" and "uses an
        # unknown custom key" apart from channel_id alone.
        psk = channel_psks.get(envelope.channel_id, channel_psks.get(WILDCARD_CHANNEL))
        if psk is None:
            return DecodedPacketEnvelope(packet_type="UNDECRYPTABLE", portnum=None, **common_kwargs)
        try:
            plaintext = decrypt_payload(packet.encrypted, packet.id, from_node, psk)
            data = mesh_pb2.Data()
            data.ParseFromString(plaintext)
        except DecodeError:
            return DecodedPacketEnvelope(packet_type="UNDECRYPTABLE", portnum=None, **common_kwargs)
    else:
        data = packet.decoded

    return decode_data(data, **common_kwargs)
