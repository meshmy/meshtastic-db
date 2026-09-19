"""Unit tests for meshdb_common.limits — no Docker needed."""

from __future__ import annotations

import pytest
from meshdb_common import limits


@pytest.mark.parametrize(
    ("metric_name", "value", "expected"),
    [
        # battery_level: firmware uses >100 to mean "powered", so the upper
        # bound is 101, not 100.
        ("device_metrics.battery_level", 0.0, True),
        ("device_metrics.battery_level", 87.0, True),
        ("device_metrics.battery_level", 101.0, True),
        ("device_metrics.battery_level", 101.1, False),
        ("device_metrics.battery_level", -1.0, False),
        # percentage-like fields, 0-100
        ("environment_metrics.relative_humidity", 0.0, True),
        ("environment_metrics.relative_humidity", 100.0, True),
        ("environment_metrics.relative_humidity", 100.1, False),
        ("air_quality_metrics.co2_humidity", 50.0, True),
        ("device_metrics.channel_utilization", 12.5, True),
        ("device_metrics.channel_utilization", -0.1, False),
        ("local_stats.air_util_tx", 100.0, True),
        ("environment_metrics.soil_moisture", 100.0, True),
        ("health_metrics.spO2", 98.0, True),
        ("health_metrics.spO2", 101.0, False),
        # temperature, -100 to 100
        ("environment_metrics.temperature", -100.0, True),
        ("environment_metrics.temperature", 100.0, True),
        ("environment_metrics.temperature", 100.01, False),
        ("environment_metrics.temperature", -100.01, False),
        ("health_metrics.temperature", 36.8, True),
        ("environment_metrics.one_wire_temperature[0]", 19.0, True),
        ("environment_metrics.one_wire_temperature_ch0", 1000.0, False),
        # voltage, -80 to 80
        ("device_metrics.voltage", 4.01, True),
        ("environment_metrics.adc_voltage_ch0", -80.0, True),
        ("environment_metrics.adc_voltage_ch0", -80.1, False),
        ("power_metrics.ch1_voltage", 80.0, True),
        ("power_metrics.ch8_voltage", 80.1, False),
        # heart rate, 0-300
        ("health_metrics.heart_bpm", 72.0, True),
        ("health_metrics.heart_bpm", 301.0, False),
        # pH, 0-14 — exact match only (see phosphorus guard below)
        ("soil_water_metrics.soil_ph", 6.5, True),
        ("soil_water_metrics.ph", 14.0, True),
        ("soil_water_metrics.ph", 14.1, False),
        # wind direction, 0-360
        ("environment_metrics.wind_direction", 180.0, True),
        ("environment_metrics.wind_direction", 360.0, True),
        ("environment_metrics.wind_direction", 360.1, False),
        # barometric pressure, widened below zero for negative-pressure rooms
        ("environment_metrics.barometric_pressure", 1013.2, True),
        ("environment_metrics.barometric_pressure", -50.0, True),
        ("environment_metrics.barometric_pressure", -50.1, False),
        ("environment_metrics.barometric_pressure", 1100.0, True),
        ("environment_metrics.barometric_pressure", 1100.1, False),
    ],
)
def test_value_within_limits(metric_name: str, value: float, expected: bool) -> None:
    assert limits.value_within_limits(metric_name, value) is expected


def test_phosphorus_is_not_mistaken_for_a_ph_field() -> None:
    """soil_water_metrics.phosphorus contains the substring "ph" — must not
    be caught by a pH pattern rule (ph/soil_ph are exact-match for exactly
    this reason)."""
    assert limits.bounds_for("soil_water_metrics.phosphorus") is None
    assert limits.value_within_limits("soil_water_metrics.phosphorus", 9999.0) is True


def test_unmatched_metric_name_passes_through_unchecked() -> None:
    assert limits.bounds_for("local_stats.num_packets_tx") is None
    assert limits.value_within_limits("local_stats.num_packets_tx", 1_000_000.0) is True


def test_leaf_name_strips_prefix_and_index_suffix() -> None:
    assert limits.leaf_name("environment_metrics.one_wire_temperature[3]") == "one_wire_temperature"
    assert limits.leaf_name("device_metrics.battery_level") == "battery_level"
    assert limits.leaf_name("route[0]") == "route"
