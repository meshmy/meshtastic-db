"""Grafana provisioning tests (§7/§8 of the design). Two layers:

- Fast, Docker-free structural checks on the provisioning files themselves,
  including a regression guard that decodes real Telemetry variants through
  `walk_message()` and asserts the dashboard's `metric_name LIKE
  'device_metrics.%'`-style panel filters actually match what decode.py
  emits — the schema's own doc comment (`db/init/10_schema.sql`) shows an
  illustrative `telemetry.environment_metrics.temperature` example, but
  `decode_data()` calls `walk_message()` with no `telemetry.` prefix, so a
  dashboard written against the doc comment instead of the real decode
  output would silently show empty panels.
- An `integration`-marked pass (needs Docker — see AGENTS.md) that actually
  runs Grafana against this repo's own grafana.ini/provisioning/ files, the
  same TimescaleDB container every other integration test module shares,
  and a real archive-job export, then drives Grafana's HTTP API to confirm
  both datasources and the dashboard are wired up for real — the phase's
  own stated acceptance criteria ("dashboard renders against seeded test
  data; DuckDB datasource queries an archived Parquet file")."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
import requests
import yaml
from meshdb_common.db import write_envelopes
from meshdb_common.decode import walk_message
from meshdb_common.envelope import DecodedPacketEnvelope, FieldValue, NodeIdentityUpdate
from meshtastic import telemetry_pb2

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAFANA_DIR = REPO_ROOT / "grafana"
DASHBOARD_PATH = GRAFANA_DIR / "provisioning" / "dashboards" / "meshtastic-overview.json"

# One representative field per Telemetry oneof variant the demo dashboard
# gives its own row (§7) — deliberately just these five, matching the
# design's explicit list; newer upstream variants (health_metrics,
# host_metrics, soil_water_metrics) have no dashboard row yet, a documented
# visualization-layer gap (see AGENTS.md), not something this test enforces.
# Each message sets exactly REPRESENTATIVE_FIELD's field for that variant —
# used below as a stand-in for "some real field this variant reports",
# chosen for being a value every node in a variant reports and directly
# comparable across nodes (a percentage, a temperature, a voltage), unlike
# e.g. uptime_seconds; the panel itself repeats over *every* field found in
# the data, not just this one (see test_*_field_variable_* below).
TELEMETRY_VARIANT_MESSAGES = {
    "device_metrics": telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=88)),
    "environment_metrics": telemetry_pb2.Telemetry(environment_metrics=telemetry_pb2.EnvironmentMetrics(temperature=21.5)),
    "air_quality_metrics": telemetry_pb2.Telemetry(air_quality_metrics=telemetry_pb2.AirQualityMetrics(pm25_standard=5)),
    "power_metrics": telemetry_pb2.Telemetry(power_metrics=telemetry_pb2.PowerMetrics(ch1_voltage=12.1)),
    "local_stats": telemetry_pb2.Telemetry(local_stats=telemetry_pb2.LocalStats(channel_utilization=45.0)),
}

REPRESENTATIVE_FIELD = {
    "device_metrics": "battery_level",
    "environment_metrics": "temperature",
    "air_quality_metrics": "pm25_standard",
    "power_metrics": "ch1_voltage",
    "local_stats": "channel_utilization",
}

# Each row's one panel repeats over "$<variant>_field" (a hidden,
# multi-value, region-scoped variable listing every metric_name actually
# seen for that variant) rather than a hardcoded field list — a new
# protobuf field showing up in decoded data becomes a new panel with no
# dashboard-JSON change, the same "no hardcoded field list" property the
# decode pipeline itself has (§3.3). The panel's own (unrepeated, as saved
# in this file) title is exactly "$<variant>_field".
FIELD_VARIABLES = {
    "device_metrics": "device_metrics_field",
    "environment_metrics": "environment_metrics_field",
    "air_quality_metrics": "air_quality_metrics_field",
    "power_metrics": "power_metrics_field",
    "local_stats": "local_stats_field",
}


def _load_dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text())


def _iter_panels(panels: list[dict]):
    """Depth-1 flatten: the five telemetry rows are collapsed
    (`collapsed: true`), which moves their child panels into the row
    panel's own `panels` list instead of the dashboard's top-level list —
    Node Map and Node Detail stay expanded, their panels already top-level
    siblings."""
    for panel in panels:
        if panel["type"] == "row":
            yield from panel.get("panels", [])
        else:
            yield panel


def _panel_sql_by_title(dashboard: dict) -> dict[str, str]:
    return {panel["title"]: panel["targets"][0]["rawSql"] for panel in _iter_panels(dashboard["panels"])}


def test_datasources_yaml_provisions_timescaledb_and_duckdb():
    doc = yaml.safe_load((GRAFANA_DIR / "provisioning" / "datasources" / "datasources.yaml").read_text())
    by_uid = {ds["uid"]: ds for ds in doc["datasources"]}

    pg = by_uid["timescaledb"]
    assert pg["type"] == "postgres"
    assert pg["url"] == "timescaledb:5432"
    assert pg["user"] == "grafana_ro"
    assert pg["secureJsonData"]["password"] == "${GRAFANA_DB_PASSWORD}"

    duckdb = by_uid["archive-duckdb"]
    assert duckdb["type"] == "motherduck-duckdb-datasource"


def test_dashboards_yaml_points_at_the_provisioning_dashboards_dir():
    doc = yaml.safe_load((GRAFANA_DIR / "provisioning" / "dashboards" / "dashboards.yaml").read_text())
    provider = doc["providers"][0]
    assert provider["type"] == "file"
    assert provider["options"]["path"] == "/etc/grafana/provisioning/dashboards"


def test_dashboard_json_is_valid_and_has_region_and_node_id_template_vars():
    dashboard = _load_dashboard()
    assert dashboard["uid"] == "meshtastic-overview"
    var_names = {v["name"] for v in dashboard["templating"]["list"]}
    assert {"region", "node_id", *FIELD_VARIABLES.values()} == var_names


def test_node_id_variable_has_a_wildcard_all_option():
    """The dropdown's "All" option must resolve $node_id to a sentinel no
    real node_id will ever equal (-1; node_id is always a non-negative
    uint32 from the wire), not Grafana's default regex-based "All" value —
    every panel's WHERE clause compares $node_id with plain `=`/`OR`, which
    a regex value wouldn't satisfy."""
    node_id_var = next(v for v in _load_dashboard()["templating"]["list"] if v["name"] == "node_id")
    assert node_id_var["includeAll"] is True
    assert node_id_var["allValue"] == "-1"
    assert node_id_var["multi"] is False


@pytest.mark.parametrize("variant", sorted(FIELD_VARIABLES))
def test_field_variable_is_hidden_multi_value_and_region_scoped(variant):
    """Each row's field-repeat variable must be multi-value with "All"
    selected by default (`$__all` — a repeating panel needs a multi-value
    variable to repeat over, and this one is never user-facing so it must
    resolve to every option on its own) and hidden from the dashboard
    toolbar (`hide: 2`) — it exists purely to drive the panel repeat, not
    for a person to interact with."""
    field_var = next(v for v in _load_dashboard()["templating"]["list"] if v["name"] == FIELD_VARIABLES[variant])
    assert field_var["multi"] is True
    assert field_var["includeAll"] is True
    assert field_var["current"]["value"] == "$__all"
    assert field_var["hide"] == 2
    assert f"metric_name LIKE '{variant}.%'" in field_var["query"]
    assert "region = '$region'" in field_var["query"]


@pytest.mark.parametrize("variant", sorted(TELEMETRY_VARIANT_MESSAGES))
def test_telemetry_panel_repeats_over_its_field_variable(variant):
    """Regression guard: each variant's one panel repeats over
    "$<variant>_field" — every protobuf field that variant has, one graph
    each — filtering the exact metric_name the repeat currently resolved
    to. The field-list variable's own `metric_name LIKE '<variant>.%'`
    query must match what walk_message() actually emits for that variant,
    not the schema doc comment's illustrative `telemetry.<variant>.*`
    example (see REPRESENTATIVE_FIELD, a stand-in real field checked
    against it)."""
    telemetry = TELEMETRY_VARIANT_MESSAGES[variant]
    metric_names = [fv.metric_name for fv in walk_message(telemetry)]
    assert any(name.startswith(f"{variant}.") for name in metric_names), metric_names

    headline_metric_name = f"{variant}.{REPRESENTATIVE_FIELD[variant]}"
    assert headline_metric_name in metric_names

    field_var_name = FIELD_VARIABLES[variant]
    panel = next(p for p in _iter_panels(_load_dashboard()["panels"]) if p["title"] == f"${field_var_name}")
    assert panel["repeat"] == field_var_name
    assert panel["repeatDirection"] == "v"

    sql = panel["targets"][0]["rawSql"]
    assert f"metric_name = '${field_var_name}'" in sql
    assert "$node_id::bigint = -1 OR m.node_id = $node_id::bigint" in sql


def test_node_map_and_overview_panels_reference_expected_tables():
    sql_by_title = _panel_sql_by_title(_load_dashboard())
    assert "FROM metric" in sql_by_title["Current node positions ($region)"]
    assert "FROM node_position_history" in sql_by_title["$node_id track (selected time range)"]
    assert "FROM node_identity" in sql_by_title["$node_id identity"]


def test_every_node_id_reference_is_cast_to_bigint():
    """Regression guard for a real bug: node_id is a full uint32 off the
    wire (up to ~4.29 billion), but a bare `$node_id` in a WHERE clause got
    bound by Grafana's Postgres datasource as an `integer` (int4, max
    ~2.15 billion) parameter — any node whose id exceeded that (confirmed
    against a real one, 2769232366, "Bukit Cermin Selangor MY_919") broke
    every panel with `ERROR: value "<node_id>" is out of range for type
    integer`, reproduced directly against a real Postgres connection by
    binding the same value as `::int4`. Casting every `$node_id` occurrence
    to `::bigint` forces Postgres to negotiate the parameter as bigint
    instead, confirmed to resolve it the same way. Every panel using
    `$node_id` as a *value* (not the plain `$node_id` in a title string)
    must carry that cast."""
    dashboard = _load_dashboard()
    all_sql = " ".join(
        target["rawSql"] for panel in _iter_panels(dashboard["panels"]) for target in panel.get("targets", [])
    )
    bare_node_id_as_value = re.findall(r"(?<!\w)\$node_id(?!::bigint)\b", all_sql)
    assert bare_node_id_as_value == []


# --------------------------------------------------------------------------
# Integration: a real Grafana container against a real TimescaleDB + a real
# archive-job export.
# --------------------------------------------------------------------------


def _load_export_parquet():
    path = REPO_ROOT / "services" / "archive-job" / "export_parquet.py"
    spec = importlib.util.spec_from_file_location("export_parquet", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ds_query(base_url: str, auth: tuple[str, str], uid: str, raw_sql: str, *, query_format: str | int = "table") -> dict:
    resp = requests.post(
        f"{base_url}/api/ds/query",
        auth=auth,
        json={
            "queries": [{"refId": "A", "datasource": {"uid": uid}, "rawSql": raw_sql, "format": query_format}],
            "from": "now-1h",
            "to": "now",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise AssertionError(f"{resp.status_code}: {resp.text}")
    return resp.json()


def _table_rows(ds_query_response: dict) -> list[list]:
    frame = ds_query_response["results"]["A"]["frames"][0]["data"]
    columns = frame["values"]
    return list(zip(*columns)) if columns else []


@pytest.mark.integration
def test_grafana_provisions_datasources_and_dashboard(grafana_base_url, grafana_auth):
    resp = requests.get(f"{grafana_base_url}/api/datasources", auth=grafana_auth, timeout=10)
    resp.raise_for_status()
    by_uid = {ds["uid"]: ds for ds in resp.json()}
    # Grafana 11's API reports the built-in Postgres plugin's canonical id
    # (`grafana-postgresql-datasource`), not the `type: postgres` alias our
    # provisioning YAML uses — both are accepted on the way in; only the
    # canonical id comes back out.
    assert by_uid["timescaledb"]["type"] == "grafana-postgresql-datasource"
    assert by_uid["archive-duckdb"]["type"] == "motherduck-duckdb-datasource"

    resp = requests.get(f"{grafana_base_url}/api/dashboards/uid/meshtastic-overview", auth=grafana_auth, timeout=10)
    resp.raise_for_status()
    assert resp.json()["dashboard"]["title"] == "Meshtastic Overview"


def _panel_sql_for_field(variant: str, *, region: str, node_id: int, metric_name: str) -> str:
    """A variant's panel query, loaded straight from the file (not a
    hand-copied duplicate, so this can't drift from what's actually
    provisioned), with $region/$node_id/the field-repeat variable
    substituted literally the way Grafana would resolve them for one
    repeated instance — $__timeFilter dropped since the seeded rows are
    already within the fixed -1h/now window _ds_query sends, and the
    macro's own expansion isn't what these tests are about."""
    field_var_name = FIELD_VARIABLES[variant]
    panel = next(p for p in _iter_panels(_load_dashboard()["panels"]) if p["title"] == f"${field_var_name}")
    sql = panel["targets"][0]["rawSql"]
    sql = sql.replace("$__timeFilter(m.time)", "TRUE")
    sql = sql.replace(f"${field_var_name}", metric_name)
    sql = sql.replace("$region", region)
    sql = sql.replace("$node_id", str(node_id))
    return sql


def _seed_two_nodes_temperature(ingest_dsn: str, region: str, node_a: int, node_b: int) -> None:
    now = datetime.now(tz=timezone.utc)
    with psycopg.connect(ingest_dsn) as conn:
        write_envelopes(
            conn,
            [
                DecodedPacketEnvelope(
                    time=now,
                    node_id=node_a,
                    region=region,
                    source="mqtt",
                    packet_type="Telemetry",
                    portnum=67,
                    packet_id=201,
                    fields=(FieldValue(metric_name="environment_metrics.temperature", value_type="numeric", value_numeric=21.5),),
                    position=None,
                    identity=NodeIdentityUpdate(long_name="Node A", short_name="NodA", hw_model=None, role=None, is_licensed=False),
                ),
                DecodedPacketEnvelope(
                    time=now,
                    node_id=node_b,
                    region=region,
                    source="mqtt",
                    packet_type="Telemetry",
                    portnum=67,
                    packet_id=202,
                    fields=(FieldValue(metric_name="environment_metrics.temperature", value_type="numeric", value_numeric=30.1),),
                    position=None,
                    identity=NodeIdentityUpdate(long_name="Node B", short_name="NodB", hw_model=None, role=None, is_licensed=False),
                ),
            ],
        )
        conn.commit()


@pytest.mark.integration
def test_grafana_dashboard_panel_renders_against_seeded_data(grafana_base_url, grafana_auth, ingest_dsn):
    node_id = 0xC3C3
    now = datetime.now(tz=timezone.utc)
    with psycopg.connect(ingest_dsn) as conn:
        write_envelopes(
            conn,
            [
                DecodedPacketEnvelope(
                    time=now,
                    node_id=node_id,
                    region="MY_919",
                    source="mqtt",
                    packet_type="Telemetry",
                    portnum=67,
                    packet_id=101,
                    fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=76.0),),
                    position=None,
                    identity=None,
                )
            ],
        )
        conn.commit()

    sql = _panel_sql_for_field("device_metrics", region="MY_919", node_id=node_id, metric_name="device_metrics.battery_level")
    result = _ds_query(grafana_base_url, grafana_auth, "timescaledb", sql)
    rows = _table_rows(result)
    assert any(row[2] == 76.0 for row in rows), result


@pytest.mark.integration
def test_grafana_wildcard_node_id_plots_multiple_nodes_on_one_graph(grafana_base_url, grafana_auth, ingest_dsn):
    """The concrete feature request this dashboard exists to serve: compare
    the same field (temperature) across multiple nodes on one graph without
    picking one $node_id at a time. Seeds two nodes' identities and
    temperature readings, then runs the Environment Metrics panel's own
    query exactly as Grafana would substitute it for the dropdown's "All"
    option ($node_id = -1) — asserting both nodes come back as distinct,
    correctly-labeled series in one query."""
    region = "MY_919"
    node_a, node_b = 0xE5E5, 0xE6E6
    _seed_two_nodes_temperature(ingest_dsn, region, node_a, node_b)

    sql = _panel_sql_for_field("environment_metrics", region=region, node_id=-1, metric_name="environment_metrics.temperature")
    result = _ds_query(grafana_base_url, grafana_auth, "timescaledb", sql)
    rows = _table_rows(result)
    series_by_name = {row[1]: row[2] for row in rows if row[1] in ("NodA", "NodB")}
    assert series_by_name == {"NodA": 21.5, "NodB": 30.1}, rows


@pytest.mark.integration
def test_grafana_specific_node_id_excludes_other_nodes(grafana_base_url, grafana_auth, ingest_dsn):
    """The other half of the wildcard behavior: picking one real node_id
    (not -1) must filter out every other node's data, not just add to it —
    same panel query as the wildcard test above, $node_id substituted with
    one of the two seeded nodes instead of -1."""
    region = "MY_919"
    node_a, node_b = 0xE7E7, 0xE8E8
    _seed_two_nodes_temperature(ingest_dsn, region, node_a, node_b)

    sql = _panel_sql_for_field("environment_metrics", region=region, node_id=node_a, metric_name="environment_metrics.temperature")
    result = _ds_query(grafana_base_url, grafana_auth, "timescaledb", sql)
    rows = _table_rows(result)
    series_names = {row[1] for row in rows}
    assert series_names == {"NodA"}, rows


@pytest.mark.integration
def test_grafana_panel_query_works_for_a_node_id_above_int32_max(grafana_base_url, grafana_auth, ingest_dsn):
    """Regression test for a real bug (see test_every_node_id_reference_is_cast_to_bigint):
    node_id is a full uint32 off the wire (up to ~4.29 billion), and a real
    node — 2769232366, "Bukit Cermin Selangor MY_919" — broke every panel
    once picked, because $node_id got bound as `integer` (int4, max
    ~2.15 billion) rather than `bigint`. Uses that exact node_id, run
    through the fixed (`::bigint`-cast) panel query via Grafana's real
    API — the earlier bug reproduced as a 400 from the datasource at this
    exact step."""
    region = "MY_919"
    node_id = 2769232366  # real value from the field report, > 2**31 - 1
    now = datetime.now(tz=timezone.utc)
    with psycopg.connect(ingest_dsn) as conn:
        write_envelopes(
            conn,
            [
                DecodedPacketEnvelope(
                    time=now,
                    node_id=node_id,
                    region=region,
                    source="mqtt",
                    packet_type="Telemetry",
                    portnum=67,
                    packet_id=204,
                    fields=(FieldValue(metric_name="environment_metrics.temperature", value_type="numeric", value_numeric=26.0),),
                    position=None,
                    identity=NodeIdentityUpdate(long_name="Bukit Cermin Selangor MY_919", short_name="BCPH", hw_model=None, role=None, is_licensed=False),
                )
            ],
        )
        conn.commit()

    sql = _panel_sql_for_field("environment_metrics", region=region, node_id=node_id, metric_name="environment_metrics.temperature")
    result = _ds_query(grafana_base_url, grafana_auth, "timescaledb", sql)
    rows = _table_rows(result)
    assert any(row[1] == "BCPH" and row[2] == 26.0 for row in rows), result


@pytest.mark.integration
def test_grafana_field_variable_lists_only_metric_names_actually_present(grafana_base_url, grafana_auth, ingest_dsn):
    """What actually drives "one graph per metric, with no hardcoded field
    list": the device_metrics_field variable's own query, run for real,
    must come back with exactly the metric_names seeded — proof a new
    protobuf field showing up in decoded data becomes a new repeated panel
    with zero dashboard-JSON changes, not just that the panel's filter
    clause happens to reference the right variable name."""
    region = "MY_919"
    node_id = 0xE9E9
    now = datetime.now(tz=timezone.utc)
    with psycopg.connect(ingest_dsn) as conn:
        write_envelopes(
            conn,
            [
                DecodedPacketEnvelope(
                    time=now,
                    node_id=node_id,
                    region=region,
                    source="mqtt",
                    packet_type="Telemetry",
                    portnum=67,
                    packet_id=203,
                    fields=(
                        FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=61.0),
                        FieldValue(metric_name="device_metrics.voltage", value_type="numeric", value_numeric=3.9),
                    ),
                    position=None,
                    identity=None,
                )
            ],
        )
        conn.commit()

    field_var = next(v for v in _load_dashboard()["templating"]["list"] if v["name"] == "device_metrics_field")
    sql = field_var["query"].replace("$region", region)
    result = _ds_query(grafana_base_url, grafana_auth, "timescaledb", sql)
    rows = _table_rows(result)
    metric_names = {row[0] for row in rows}
    assert {"device_metrics.battery_level", "device_metrics.voltage"} <= metric_names, rows


@pytest.mark.integration
def test_grafana_duckdb_datasource_queries_an_archived_parquet_file(
    grafana_base_url, grafana_auth, ingest_dsn, archive_dsn, archive_root
):
    export_parquet = _load_export_parquet()
    node_id = 0xD4D4
    old_time = datetime(2019, 1, 1, tzinfo=timezone.utc)
    with psycopg.connect(ingest_dsn) as ingest_conn:
        write_envelopes(
            ingest_conn,
            [
                DecodedPacketEnvelope(
                    time=old_time,
                    node_id=node_id,
                    region="MY_919",
                    source="mqtt",
                    packet_type="Telemetry",
                    portnum=67,
                    packet_id=102,
                    fields=(FieldValue(metric_name="device_metrics.battery_level", value_type="numeric", value_numeric=55.0),),
                    position=None,
                    identity=None,
                )
            ],
        )
        ingest_conn.commit()

    with psycopg.connect(archive_dsn) as archive_conn:
        duckdb_con = export_parquet.build_duckdb_connection(archive_dsn)
        try:
            archived = export_parquet.run_once(
                archive_conn,
                duckdb_con,
                archive_root=archive_root,
                retention_interval="0 seconds",
                backend="local",
            )
        finally:
            duckdb_con.close()
    assert archived >= 1

    result = _ds_query(
        grafana_base_url,
        grafana_auth,
        "archive-duckdb",
        "SELECT count(*) AS n FROM read_parquet('/archive/parquet/metric/**/*.parquet')",
        query_format=1,
    )
    rows = _table_rows(result)
    assert rows and rows[0][0] >= 1, result
