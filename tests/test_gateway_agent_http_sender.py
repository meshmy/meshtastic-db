"""gateway-agent's IngestApiSender against a real local HTTP server (no
mocking of the `requests` library) — covers both stated acceptance criteria
for this phase: forwarding a batch over HTTP, and WAL spillover surviving
an ingest-api restart mid-stream (proven here with an actual server
shutdown/rebind, not just a toggled failure flag)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gateway_agent_main():
    path = REPO_ROOT / "services" / "gateway-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("gateway_agent_main", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _envelope(node_id: int):
    from meshdb_common.envelope import DecodedPacketEnvelope, FieldValue

    return DecodedPacketEnvelope(
        time=datetime.now(tz=timezone.utc),
        node_id=node_id,
        region="MY_919",
        source="serial",
        packet_type="Telemetry",
        portnum=67,
        packet_id=1,
        fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=50.0),),
    )


class _CapturingServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.received_batches: list[list[dict]] = []
        self.received_auth_headers: list[str | None] = []
        self.fail = False


class _CapturingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        if self.server.fail:
            self.send_response(500)
            self.end_headers()
            return
        self.server.received_batches.append(body)
        self.server.received_auth_headers.append(self.headers.get("Authorization"))
        payload = json.dumps({"received": len(body), "written": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence default request logging to stderr
        pass


def _start(server: _CapturingServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def fake_ingest_api():
    server = _CapturingServer(("127.0.0.1", 0), _CapturingHandler)
    thread = _start(server)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sender_forwards_a_batch_to_a_reachable_ingest_api(tmp_path, fake_ingest_api):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")
    sender = gw.IngestApiSender(f"http://127.0.0.1:{fake_ingest_api.server_port}/v1/ingest", "test-token", wal)

    sender.send([_envelope(1)])

    assert len(fake_ingest_api.received_batches) == 1
    assert fake_ingest_api.received_batches[0][0]["node_id"] == 1
    assert fake_ingest_api.received_auth_headers == ["Bearer test-token"]
    assert not wal.has_pending()


def test_sender_spills_to_wal_on_failure_then_replays_it_ahead_of_the_next_batch(tmp_path, fake_ingest_api):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")
    sender = gw.IngestApiSender(f"http://127.0.0.1:{fake_ingest_api.server_port}/v1/ingest", None, wal)

    fake_ingest_api.fail = True
    sender.send([_envelope(1)])
    assert wal.has_pending()
    assert fake_ingest_api.received_batches == []

    fake_ingest_api.fail = False
    sender.send([_envelope(2)])

    assert not wal.has_pending()
    assert [batch[0]["node_id"] for batch in fake_ingest_api.received_batches] == [1, 2]


def test_wal_spillover_survives_an_actual_ingest_api_restart_mid_stream(tmp_path):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")

    server1 = _CapturingServer(("127.0.0.1", 0), _CapturingHandler)
    port = server1.server_port
    thread1 = _start(server1)
    sender = gw.IngestApiSender(f"http://127.0.0.1:{port}/v1/ingest", None, wal)

    sender.send([_envelope(10)])
    assert server1.received_batches[0][0]["node_id"] == 10

    server1.shutdown()
    server1.server_close()
    thread1.join(timeout=5)

    sender.send([_envelope(11)])  # ingest-api is down: connection refused, not just a 500
    assert wal.has_pending()

    server2 = _CapturingServer(("127.0.0.1", port), _CapturingHandler)
    thread2 = _start(server2)
    try:
        sender.send([_envelope(12)])

        assert not wal.has_pending()
        assert [batch[0]["node_id"] for batch in server2.received_batches] == [11, 12]
    finally:
        server2.shutdown()
        server2.server_close()
        thread2.join(timeout=5)
