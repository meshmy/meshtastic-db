"""ingest-api: bearer-token auth, region opt-in defense-in-depth, and the
write path itself. Most cases use FastAPI's TestClient against an injected
fake connection (fast, no Docker); one integration-marked case exercises
`create_app()` against a real, disposable TimescaleDB via the `ingest_dsn`
fixture, proving the whole HTTP -> write_envelopes() -> Postgres path
works end to end, not just that the two pieces are wired together."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from meshdb_common.config import MqttConfig, RegionsConfig
from meshdb_common.serialization import envelope_to_dict

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-ingest-api-token"


def _load_ingest_api_main():
    path = REPO_ROOT / "services" / "ingest-api" / "main.py"
    spec = importlib.util.spec_from_file_location("ingest_api_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg(allowed_regions: tuple[str, ...]) -> RegionsConfig:
    return RegionsConfig(
        allowed_regions=allowed_regions,
        mqtt=MqttConfig(topic_template="msh/{region}/2/{version}/#", brokers=(), channels=()),
        tcp_nodes=(),
        gateway_agent=None,
    )


class _FakeConn:
    """Stands in for a psycopg.Connection: write_envelopes is monkeypatched
    out for the non-integration tests, so this only needs to be a distinct
    object write_envelopes() can be asserted against — no real DB calls
    happen through it."""


def _envelope(*, region: str, node_id: int = 0x1234, packet_id: int = 1, battery_level: float = 55.0) -> dict:
    from datetime import datetime, timezone

    from meshdb_common.envelope import DecodedPacketEnvelope, FieldValue

    return envelope_to_dict(
        DecodedPacketEnvelope(
            time=datetime.now(tz=timezone.utc),
            node_id=node_id,
            region=region,
            source="serial",
            packet_type="Telemetry",
            portnum=67,
            packet_id=packet_id,
            fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=battery_level),),
        )
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INGEST_API_TOKEN", TOKEN)
    ingest_api_main = _load_ingest_api_main()
    written_batches = []
    monkeypatch.setattr(ingest_api_main, "write_envelopes", lambda conn, envelopes: written_batches.append(envelopes))
    app = ingest_api_main.create_app(conn=_FakeConn(), cfg=_cfg(("MY_919",)))
    with TestClient(app) as test_client:
        test_client.written_batches = written_batches
        yield test_client


def test_rejects_requests_with_no_bearer_token(client):
    response = client.post("/v1/ingest", json=[_envelope(region="MY_919")])
    assert response.status_code == 401


def test_rejects_requests_with_the_wrong_bearer_token(client):
    response = client.post("/v1/ingest", json=[_envelope(region="MY_919")], headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_writes_envelopes_for_an_allowed_region(client):
    response = client.post("/v1/ingest", json=[_envelope(region="MY_919")], headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"received": 1, "written": 1}
    assert len(client.written_batches) == 1
    assert len(client.written_batches[0]) == 1


def test_drops_envelopes_for_a_disallowed_region_but_still_succeeds(client):
    response = client.post(
        "/v1/ingest",
        json=[_envelope(region="MY_919"), _envelope(region="SOME_OTHER_REGION")],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"received": 2, "written": 1}
    assert len(client.written_batches[0]) == 1


def test_rejects_a_malformed_envelope_body(client):
    response = client.post("/v1/ingest", json=[{"not": "an envelope"}], headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 400


@pytest.mark.integration
def test_ingest_api_writes_through_to_postgres(ingest_dsn, monkeypatch):
    monkeypatch.setenv("INGEST_API_TOKEN", TOKEN)
    ingest_api_main = _load_ingest_api_main()
    conn = psycopg.connect(ingest_dsn)
    app = ingest_api_main.create_app(conn=conn, cfg=_cfg(("MY_919",)))

    node_id = 0xFEED
    with TestClient(app) as client:
        response = client.post(
            "/v1/ingest",
            json=[_envelope(region="MY_919", node_id=node_id, packet_id=99, battery_level=81.0)],
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200

    with conn.cursor() as cur:
        cur.execute(
            "SELECT value_numeric FROM metric WHERE node_id = %s AND metric_name = 'device_metrics.battery_level'",
            (node_id,),
        )
        rows = cur.fetchall()
    assert rows == [(81.0,)]
