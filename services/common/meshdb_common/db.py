"""The one shared write path (§2.2/§2.4/§5): every ingestion source hands a
batch of DecodedPacketEnvelopes to `write_envelopes` instead of writing to
the database itself. Per envelope, exactly one of three things happens —
Position packets append to `node_position_history`, User/NodeInfo packets
upsert `node_identity`, everything else's fields insert into `metric` with
an as-of position LATERAL join applied automatically — so no ingestion
source ever queries, caches, or reasons about a node's position or identity
on its own.

Position rows are written before the `metric` insert in the same
transaction so a Position packet earlier in a batch is already visible to
the LATERAL join for a later packet in that same batch, from any node.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import psycopg

from .envelope import DecodedPacketEnvelope, NodeIdentityUpdate

_INSERT_POSITIONS_SQL = """
INSERT INTO node_position_history (
    time, node_id, region, latitude, longitude, altitude,
    location_source, ground_speed, ground_track
)
SELECT * FROM unnest(
    %(time)s::timestamptz[], %(node_id)s::bigint[], %(region)s::text[],
    %(latitude)s::double precision[], %(longitude)s::double precision[],
    %(altitude)s::real[], %(location_source)s::text[],
    %(ground_speed)s::real[], %(ground_track)s::real[]
)
ON CONFLICT (time, node_id) DO NOTHING;
"""

# Multiple source rows targeting the same conflict key make Postgres reject
# ON CONFLICT ... DO UPDATE ("cannot affect row a second time") within one
# statement, so callers must first collapse a batch to one row per node_id
# (see _dedup_identities) — DO NOTHING inserts (positions, metric) have no
# such restriction and are left free to carry same-key duplicates.
_UPSERT_IDENTITIES_SQL = """
INSERT INTO node_identity (
    node_id, long_name, short_name, hw_model, role, is_licensed, region, last_heard, updated_at
)
SELECT node_id, long_name, short_name, hw_model, role, is_licensed, region, last_heard, now()
FROM unnest(
    %(node_id)s::bigint[], %(long_name)s::text[], %(short_name)s::text[],
    %(hw_model)s::text[], %(role)s::text[], %(is_licensed)s::boolean[],
    %(region)s::text[], %(last_heard)s::timestamptz[]
) AS t(node_id, long_name, short_name, hw_model, role, is_licensed, region, last_heard)
ON CONFLICT (node_id) DO UPDATE SET
    long_name = EXCLUDED.long_name,
    short_name = EXCLUDED.short_name,
    hw_model = EXCLUDED.hw_model,
    role = EXCLUDED.role,
    is_licensed = EXCLUDED.is_licensed,
    region = EXCLUDED.region,
    last_heard = EXCLUDED.last_heard,
    updated_at = now();
"""

_INSERT_METRICS_SQL = """
INSERT INTO metric (
    time, node_id, region, gateway_node_id, packet_type, portnum,
    metric_name, value_type, value_numeric, value_text, value_bool,
    snr, rssi, hop_limit, hop_start, channel, source, packet_id,
    latitude, longitude, altitude, position_time
)
SELECT
    r.time, r.node_id, r.region, r.gateway_node_id, r.packet_type, r.portnum,
    r.metric_name, r.value_type, r.value_numeric, r.value_text, r.value_bool,
    r.snr, r.rssi, r.hop_limit, r.hop_start, r.channel, r.source, r.packet_id,
    p.latitude, p.longitude, p.altitude, p.time
FROM unnest(
    %(time)s::timestamptz[], %(node_id)s::bigint[], %(region)s::text[],
    %(gateway_node_id)s::bigint[], %(packet_type)s::text[], %(portnum)s::smallint[],
    %(metric_name)s::text[], %(value_type)s::text[], %(value_numeric)s::double precision[],
    %(value_text)s::text[], %(value_bool)s::boolean[],
    %(snr)s::real[], %(rssi)s::smallint[], %(hop_limit)s::smallint[],
    %(hop_start)s::smallint[], %(channel)s::smallint[], %(source)s::text[], %(packet_id)s::bigint[]
) AS r(
    time, node_id, region, gateway_node_id, packet_type, portnum,
    metric_name, value_type, value_numeric, value_text, value_bool,
    snr, rssi, hop_limit, hop_start, channel, source, packet_id
)
LEFT JOIN LATERAL (
    SELECT latitude, longitude, altitude, time
    FROM node_position_history
    WHERE node_id = r.node_id AND time <= r.time
    ORDER BY time DESC LIMIT 1
) p ON true
ON CONFLICT (time, node_id, metric_name, packet_id) DO NOTHING;
"""


def _dedup_identities(
    envelopes: Sequence[DecodedPacketEnvelope],
) -> dict[int, tuple[datetime, str, NodeIdentityUpdate]]:
    """Collapse a batch to at most one identity row per node_id, keeping the
    one with the latest packet time — required by the ON CONFLICT ... DO
    UPDATE restriction noted above."""
    latest: dict[int, tuple[datetime, str, NodeIdentityUpdate]] = {}
    for env in envelopes:
        if env.identity is None:
            continue
        current = latest.get(env.node_id)
        if current is None or env.time > current[0]:
            latest[env.node_id] = (env.time, env.region, env.identity)
    return latest


def _write_positions(cur: psycopg.Cursor, envelopes: Sequence[DecodedPacketEnvelope]) -> None:
    rows = [env for env in envelopes if env.position is not None]
    if not rows:
        return
    cur.execute(
        _INSERT_POSITIONS_SQL,
        {
            "time": [env.time for env in rows],
            "node_id": [env.node_id for env in rows],
            "region": [env.region for env in rows],
            "latitude": [env.position.latitude for env in rows],
            "longitude": [env.position.longitude for env in rows],
            "altitude": [env.position.altitude for env in rows],
            "location_source": [env.position.location_source for env in rows],
            "ground_speed": [env.position.ground_speed for env in rows],
            "ground_track": [env.position.ground_track for env in rows],
        },
    )


def _write_identities(cur: psycopg.Cursor, envelopes: Sequence[DecodedPacketEnvelope]) -> None:
    latest = _dedup_identities(envelopes)
    if not latest:
        return
    rows = list(latest.items())
    cur.execute(
        _UPSERT_IDENTITIES_SQL,
        {
            "node_id": [node_id for node_id, _ in rows],
            "long_name": [identity.long_name for _, (_, _, identity) in rows],
            "short_name": [identity.short_name for _, (_, _, identity) in rows],
            "hw_model": [identity.hw_model for _, (_, _, identity) in rows],
            "role": [identity.role for _, (_, _, identity) in rows],
            "is_licensed": [identity.is_licensed for _, (_, _, identity) in rows],
            "region": [region for _, (_, region, _) in rows],
            "last_heard": [time for _, (time, _, _) in rows],
        },
    )


def _write_metrics(cur: psycopg.Cursor, envelopes: Sequence[DecodedPacketEnvelope]) -> None:
    rows = [(env, fv) for env in envelopes for fv in env.fields]
    if not rows:
        return
    cur.execute(
        _INSERT_METRICS_SQL,
        {
            "time": [env.time for env, _ in rows],
            "node_id": [env.node_id for env, _ in rows],
            "region": [env.region for env, _ in rows],
            "gateway_node_id": [env.gateway_node_id for env, _ in rows],
            "packet_type": [env.packet_type for env, _ in rows],
            "portnum": [env.portnum for env, _ in rows],
            "metric_name": [fv.metric_name for _, fv in rows],
            "value_type": [fv.value_type for _, fv in rows],
            "value_numeric": [fv.value_numeric for _, fv in rows],
            "value_text": [fv.value_text for _, fv in rows],
            "value_bool": [fv.value_bool for _, fv in rows],
            "snr": [env.snr for env, _ in rows],
            "rssi": [env.rssi for env, _ in rows],
            "hop_limit": [env.hop_limit for env, _ in rows],
            "hop_start": [env.hop_start for env, _ in rows],
            "channel": [env.channel for env, _ in rows],
            "source": [env.source for env, _ in rows],
            "packet_id": [env.packet_id for env, _ in rows],
        },
    )


def write_envelopes(conn: psycopg.Connection, envelopes: Sequence[DecodedPacketEnvelope]) -> None:
    """Write one batch of decoded envelopes in a single transaction. Safe to
    call with envelopes from multiple nodes/sources/packet types mixed
    together — routing per envelope is entirely by its populated
    position/identity/fields attribute, not by any argument the caller
    passes."""
    with conn.transaction(), conn.cursor() as cur:
        _write_positions(cur, envelopes)
        _write_identities(cur, envelopes)
        _write_metrics(cur, envelopes)
