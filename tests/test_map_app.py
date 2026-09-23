"""map-app: the choose_resolution()/rows_to_playback_payload() pure
functions, the JSON endpoints against an injected fake connection (fast, no
Docker), and one integration-marked case proving grafana_ro's grants
(db/init/50_roles.sh) actually cover every table/view this service queries."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from meshdb_common.envelope import (
    DecodedPacketEnvelope,
    FieldValue,
    NodeIdentityUpdate,
    PositionFix,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_map_app_main():
    path = REPO_ROOT / "services" / "map-app" / "main.py"
    spec = importlib.util.spec_from_file_location("map_app_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCursor:
    """One canned row-set per execute() call — a test presets the queue of
    responses in the order the endpoint under test issues its queries."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, responses):
        self._responses = list(responses)

    def cursor(self):
        return _FakeCursor(self._responses.pop(0))


def _client(responses):
    # Entered directly (not via `with`) so the helper can return a client
    # to each test rather than yielding one — fine here since lifespan
    # teardown on a _FakeConn has no real resource to release. Using
    # TestClient as a context manager (one way or another) is required:
    # it's what actually runs the app's lifespan, which is what sets
    # app.state.conn/lock in the first place.
    map_app_main = _load_map_app_main()
    app = map_app_main.create_app(conn=_FakeConn(responses))
    return TestClient(app).__enter__(), map_app_main


# --- pure functions, no DB involved -----------------------------------


def test_choose_resolution_at_and_just_past_the_48h_boundary():
    map_app_main = _load_map_app_main()
    assert map_app_main.choose_resolution(0, 48 * 3600) == "raw"
    assert map_app_main.choose_resolution(0, 48 * 3600 + 1) == "hourly"


def test_choose_resolution_at_and_just_past_the_14d_boundary():
    map_app_main = _load_map_app_main()
    assert map_app_main.choose_resolution(0, 14 * 24 * 3600) == "hourly"
    assert map_app_main.choose_resolution(0, 14 * 24 * 3600 + 1) == "daily"


def test_rows_to_playback_payload_shapes_positions_and_values_per_node():
    map_app_main = _load_map_app_main()
    payload = map_app_main.rows_to_playback_payload(
        region="MY_919",
        metric="environment_metrics.temperature",
        resolution="raw",
        start=1000,
        end=2000,
        position_rows=[(111, 1000, 3.1, 101.6, 10.0), (222, 1100, 3.2, 101.7, None)],
        value_rows=[(111, 1000, 27.5)],
    )
    assert payload["nodes"]["111"] == {"positions": [[1000, 3.1, 101.6, 10.0]], "values": [[1000, 27.5]]}
    # A node with positions but no values in range still appears, with an
    # empty values array rather than being dropped.
    assert payload["nodes"]["222"] == {"positions": [[1100, 3.2, 101.7, None]], "values": []}


def test_rows_to_playback_payload_handles_a_node_with_no_positions():
    map_app_main = _load_map_app_main()
    payload = map_app_main.rows_to_playback_payload(
        region="MY_919", metric="m", resolution="raw", start=0, end=1, position_rows=[], value_rows=[(111, 0, 1.0)]
    )
    assert payload["nodes"]["111"] == {"positions": [], "values": [[0, 1.0]]}


# --- endpoints against a fake connection -------------------------------


def test_healthz():
    client, _ = _client(responses=[])
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_regions_endpoint_shape():
    client, _ = _client(responses=[[("MY_919", 5, 3)]])
    response = client.get("/api/regions")
    assert response.status_code == 200
    assert response.json() == {"regions": [{"region": "MY_919", "node_count": 5, "with_position_count": 3}]}


def test_nodes_endpoint_reports_null_position_when_a_node_has_none():
    now = datetime.now(tz=timezone.utc)
    rows = [
        (111, "Node One", "N1", "TBEAM", now, 3.1, 101.6, 10.0, now),
        (222, "Node Two", "N2", "HELTEC", None, None, None, None, None),
    ]
    client, _ = _client(responses=[rows])
    response = client.get("/api/nodes", params={"region": "MY_919"})
    assert response.status_code == 200
    body = response.json()
    assert body["nodes"][0]["position"]["lat"] == 3.1
    assert body["nodes"][1]["position"] is None
    assert body["nodes"][1]["last_heard"] is None


def test_metrics_endpoint_excludes_denylisted_names():
    map_app_main = _load_map_app_main()
    names = ["environment_metrics.temperature", *map_app_main.METRIC_DENYLIST]
    app = map_app_main.create_app(conn=_FakeConn([[(name,) for name in names]]))
    client = TestClient(app).__enter__()
    response = client.get("/api/metrics", params={"region": "MY_919"})
    assert response.json() == {"metrics": ["environment_metrics.temperature"]}


def test_metrics_endpoint_falls_back_to_raw_when_aggregates_are_empty():
    # First query (metric_daily) returns nothing; second (raw metric scan)
    # returns the real answer -- covers a fresh deployment where the
    # continuous aggregates haven't populated yet (they start WITH NO DATA).
    client, _ = _client(responses=[[], [("device_metrics.battery_level",)]])
    response = client.get("/api/metrics", params={"region": "MY_919"})
    assert response.json() == {"metrics": ["device_metrics.battery_level"]}


def test_playback_endpoint_rejects_a_non_positive_range():
    client, _ = _client(responses=[])
    response = client.get("/api/playback", params={"region": "MY_919", "metric": "m", "start": 100, "end": 100})
    assert response.status_code == 400


def test_playback_endpoint_rejects_a_non_integer_timestamp():
    client, _ = _client(responses=[])
    response = client.get("/api/playback", params={"region": "MY_919", "metric": "m", "start": "not-a-number", "end": 100})
    assert response.status_code == 422


def test_playback_endpoint_returns_shaped_payload_at_raw_resolution():
    positions = [(111, 1000, 3.1, 101.6, 10.0)]
    values = [(111, 1000, 27.5)]
    client, _ = _client(responses=[positions, values])
    response = client.get("/api/playback", params={"region": "MY_919", "metric": "m", "start": 0, "end": 3600})
    body = response.json()
    assert body["resolution"] == "raw"
    assert body["nodes"]["111"]["values"] == [[1000, 27.5]]


def test_root_serves_the_static_frontend():
    client, _ = _client(responses=[])
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# --- integration: real grafana_ro grants --------------------------------


@pytest.mark.integration
def test_grafana_ro_can_read_everything_map_app_queries(ingest_dsn, grafana_ro_dsn):
    write_conn = psycopg.connect(ingest_dsn)
    node_id = 0xC0FFEE
    region = "MAP_APP_TEST"
    from meshdb_common.db import write_envelopes

    write_envelopes(
        write_conn,
        [
            DecodedPacketEnvelope(
                time=datetime.now(tz=timezone.utc),
                node_id=node_id,
                region=region,
                source="serial",
                packet_type="Position",
                portnum=3,
                packet_id=1,
                position=PositionFix(latitude=3.14, longitude=101.6, altitude=10.0, location_source=None, ground_speed=None, ground_track=None),
            ),
            DecodedPacketEnvelope(
                time=datetime.now(tz=timezone.utc),
                node_id=node_id,
                region=region,
                source="serial",
                packet_type="Telemetry",
                portnum=67,
                packet_id=2,
                fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=91.0),),
            ),
            # /api/nodes joins from node_identity, which only gets a row from
            # a User/NodeInfo packet -- without this, a node that has only
            # ever sent Position/Telemetry wouldn't show up in that endpoint.
            DecodedPacketEnvelope(
                time=datetime.now(tz=timezone.utc),
                node_id=node_id,
                region=region,
                source="serial",
                packet_type="User",
                portnum=4,
                packet_id=3,
                identity=NodeIdentityUpdate(long_name="Test Node", short_name="TEST"),
            ),
        ],
    )

    read_conn = psycopg.connect(grafana_ro_dsn)
    map_app_main = _load_map_app_main()
    app = map_app_main.create_app(conn=read_conn)

    with TestClient(app) as client:
        nodes_response = client.get("/api/nodes", params={"region": region})
        assert nodes_response.status_code == 200
        assert nodes_response.json()["nodes"][0]["node_id"] == node_id

        now = int(datetime.now(tz=timezone.utc).timestamp())
        playback_response = client.get(
            "/api/playback",
            params={"region": region, "metric": "device_metrics.battery_level", "start": now - 3600, "end": now + 3600},
        )
        assert playback_response.status_code == 200
        assert playback_response.json()["nodes"][str(node_id)]["values"][0][1] == 91.0
