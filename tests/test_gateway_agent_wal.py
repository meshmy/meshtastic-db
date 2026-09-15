"""Pure unit coverage for gateway-agent's WAL spillover file — no network,
no Docker. Append/replay/clear semantics only; IngestApiSender's use of it
(replay-before-send ordering, spillover on a failed POST) is covered
separately in test_gateway_agent_http_sender.py against a real local HTTP
server."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gateway_agent_main():
    path = REPO_ROOT / "services" / "gateway-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("gateway_agent_main", path)
    module = importlib.util.module_from_spec(spec)
    # Python's dataclass processing looks the defining module up in
    # sys.modules; a module loaded via exec_module without being registered
    # there first breaks dataclasses defined *in this module* (unlike
    # tcp-poller/mqtt-ingest's main.py, which only use dataclasses imported
    # from meshdb_common, already registered under their own module name).
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


def test_wal_starts_empty(tmp_path):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")
    assert not wal.has_pending()
    assert wal.replay() == []


def test_wal_append_and_replay_roundtrips_envelopes(tmp_path):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")
    envelopes = [_envelope(1), _envelope(2)]

    wal.append(envelopes)

    assert wal.has_pending()
    assert wal.replay() == envelopes


def test_wal_replay_does_not_clear_the_file(tmp_path):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")
    wal.append([_envelope(1)])

    wal.replay()

    assert wal.has_pending()


def test_wal_clear_empties_the_file(tmp_path):
    gw = _load_gateway_agent_main()
    wal = gw.Wal(tmp_path / "wal.jsonl")
    wal.append([_envelope(1)])

    wal.clear()

    assert not wal.has_pending()
    assert wal.replay() == []


def test_wal_skips_a_torn_last_line_from_a_crash_mid_append(tmp_path):
    gw = _load_gateway_agent_main()
    path = tmp_path / "wal.jsonl"
    wal = gw.Wal(path)
    envelope = _envelope(1)
    wal.append([envelope])
    with path.open("a") as f:
        f.write('{"time": "2026-01-01T00:00:00+00:00", "node_id": 2, "region')  # torn, no trailing newline

    assert wal.replay() == [envelope]
