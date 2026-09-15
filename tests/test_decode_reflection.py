"""Reflection-walker unit tests: construct in-memory protobuf messages and
assert walk_message() yields the expected FieldValues — the property that
makes a brand-new upstream field become a new metric_name automatically."""

import pytest
from meshdb_common.decode import walk_message
from meshtastic import config_pb2, mesh_pb2, telemetry_pb2


def test_walk_message_scalar_and_enum_fields():
    telemetry = telemetry_pb2.Telemetry(
        time=1700000000,
        device_metrics=telemetry_pb2.DeviceMetrics(
            battery_level=87,
            voltage=4.01,
        ),
    )

    values = {fv.metric_name: fv for fv in walk_message(telemetry)}

    assert values["time"].value_type == "numeric"
    assert values["time"].value_numeric == 1700000000

    battery = values["device_metrics.battery_level"]
    assert battery.value_type == "numeric"
    assert battery.value_numeric == 87

    voltage = values["device_metrics.voltage"]
    assert voltage.value_type == "numeric"
    assert voltage.value_numeric == pytest.approx(4.01, rel=1e-4)


def test_walk_message_repeated_submessages():
    neighbor_info = mesh_pb2.NeighborInfo(
        node_id=0x1234,
        neighbors=[
            mesh_pb2.Neighbor(node_id=0x1111, snr=5.5),
            mesh_pb2.Neighbor(node_id=0x2222, snr=-3.25),
        ],
    )

    values = {fv.metric_name: fv for fv in walk_message(neighbor_info)}

    assert values["node_id"].value_numeric == 0x1234
    assert values["neighbors[0].node_id"].value_numeric == 0x1111
    assert values["neighbors[0].snr"].value_numeric == pytest.approx(5.5, rel=1e-4)
    assert values["neighbors[1].node_id"].value_numeric == 0x2222
    assert values["neighbors[1].snr"].value_numeric == pytest.approx(-3.25, rel=1e-4)


def test_walk_message_enum_field_carries_label_and_code():
    user = mesh_pb2.User(long_name="Test Node", role=config_pb2.Config.DeviceConfig.Role.ROUTER)

    values = {fv.metric_name: fv for fv in walk_message(user)}

    role = values["role"]
    assert role.value_type == "enum"
    assert role.value_numeric == config_pb2.Config.DeviceConfig.Role.ROUTER
    assert role.value_text == "ROUTER"

    name = values["long_name"]
    assert name.value_type == "text"
    assert name.value_text == "Test Node"


def test_walk_message_bool_field():
    user = mesh_pb2.User(is_licensed=True)
    values = {fv.metric_name: fv for fv in walk_message(user)}
    assert values["is_licensed"].value_type == "bool"
    assert values["is_licensed"].value_bool is True


def test_walk_message_only_yields_populated_fields():
    # ListFields() only returns populated fields — an unset optional field
    # produces no row at all, rather than a row with a default/zero value.
    metrics = telemetry_pb2.DeviceMetrics(battery_level=50)
    values = list(walk_message(metrics))
    assert [fv.metric_name for fv in values] == ["battery_level"]
