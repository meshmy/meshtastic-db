"""JSON (de)serialization of DecodedPacketEnvelope — the wire format between
gateway-agent (which decodes locally, on the host with radio access) and
ingest-api, which calls the same write_envelopes() every other ingestion
source uses rather than re-decoding anything itself."""

from __future__ import annotations

from datetime import datetime

from .envelope import DecodedPacketEnvelope, FieldValue, NodeIdentityUpdate, PositionFix


def envelope_to_dict(env: DecodedPacketEnvelope) -> dict:
    return {
        "time": env.time.isoformat(),
        "node_id": env.node_id,
        "region": env.region,
        "source": env.source,
        "packet_type": env.packet_type,
        "portnum": env.portnum,
        "gateway_node_id": env.gateway_node_id,
        "packet_id": env.packet_id,
        "snr": env.snr,
        "rssi": env.rssi,
        "hop_limit": env.hop_limit,
        "hop_start": env.hop_start,
        "channel": env.channel,
        "fields": [
            {
                "metric_name": f.metric_name,
                "value_type": f.value_type,
                "value_numeric": f.value_numeric,
                "value_text": f.value_text,
                "value_bool": f.value_bool,
            }
            for f in env.fields
        ],
        "position": None
        if env.position is None
        else {
            "latitude": env.position.latitude,
            "longitude": env.position.longitude,
            "altitude": env.position.altitude,
            "location_source": env.position.location_source,
            "ground_speed": env.position.ground_speed,
            "ground_track": env.position.ground_track,
        },
        "identity": None
        if env.identity is None
        else {
            "long_name": env.identity.long_name,
            "short_name": env.identity.short_name,
            "hw_model": env.identity.hw_model,
            "role": env.identity.role,
            "is_licensed": env.identity.is_licensed,
        },
    }


def envelope_from_dict(data: dict) -> DecodedPacketEnvelope:
    position = data.get("position")
    identity = data.get("identity")
    return DecodedPacketEnvelope(
        time=datetime.fromisoformat(data["time"]),
        node_id=data["node_id"],
        region=data["region"],
        source=data["source"],
        packet_type=data["packet_type"],
        portnum=data.get("portnum"),
        gateway_node_id=data.get("gateway_node_id"),
        packet_id=data.get("packet_id"),
        snr=data.get("snr"),
        rssi=data.get("rssi"),
        hop_limit=data.get("hop_limit"),
        hop_start=data.get("hop_start"),
        channel=data.get("channel"),
        fields=tuple(
            FieldValue(
                metric_name=f["metric_name"],
                value_type=f["value_type"],
                value_numeric=f.get("value_numeric"),
                value_text=f.get("value_text"),
                value_bool=f.get("value_bool"),
            )
            for f in data.get("fields") or ()
        ),
        position=None
        if position is None
        else PositionFix(
            latitude=position["latitude"],
            longitude=position["longitude"],
            altitude=position.get("altitude"),
            location_source=position.get("location_source"),
            ground_speed=position.get("ground_speed"),
            ground_track=position.get("ground_track"),
        ),
        identity=None
        if identity is None
        else NodeIdentityUpdate(
            long_name=identity.get("long_name"),
            short_name=identity.get("short_name"),
            hw_model=identity.get("hw_model"),
            role=identity.get("role"),
            is_licensed=identity.get("is_licensed"),
        ),
    )
