"""Builds the fixed corpus behind the metric-name stability check
(test_metric_name_stability.py) and (re)writes its golden fixture.

Not a test module itself — run directly (`python tests/golden_metrics.py`,
or `make update-golden-metrics`) after a deliberate vendor/protobufs bump
that adds fields on purpose, to fold the additions into the baseline.

Scope: only the portnums decode_data() routes through walk_message()
(Telemetry, Routing, NeighborInfo, Traceroute, MapReport) — every leaf field
on one populated message per oneof variant, so a silent upstream rename of
any of them changes the emitted metric_name and fails the check. Position
and NodeInfo are deliberately excluded: they're extracted by
_extract_position/_extract_identity, which reference field names directly
and so already fail loudly (AttributeError) on a rename rather than
silently forking a metric_name.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import meshdb_common  # noqa: F401  (side effect: puts generated/ on sys.path)
from meshdb_common.decode import decode_data
from meshtastic import config_pb2, mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2

PortNum = portnums_pb2.PortNum
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "known_metrics_golden.json"

_COMMON_KWARGS = {
    "time": datetime(2024, 1, 1, tzinfo=timezone.utc),
    "node_id": 1,
    "region": "TEST",
    "source": "test",
}


def _data(portnum: int, payload_msg) -> mesh_pb2.Data:
    return mesh_pb2.Data(portnum=portnum, payload=payload_msg.SerializeToString())


def build_corpus() -> list[mesh_pb2.Data]:
    corpus: list[mesh_pb2.Data] = []

    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        device_metrics=telemetry_pb2.DeviceMetrics(
            battery_level=87, voltage=4.01, channel_utilization=12.5,
            air_util_tx=3.2, uptime_seconds=123456,
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        environment_metrics=telemetry_pb2.EnvironmentMetrics(
            temperature=21.5, relative_humidity=55.0, barometric_pressure=1013.2,
            gas_resistance=100.0, voltage=3.3, current=10.0, iaq=42,
            distance=5.0, lux=100.0, white_lux=90.0, ir_lux=10.0, uv_lux=1.0,
            wind_direction=180, wind_speed=5.0, weight=10.0, wind_gust=8.0,
            wind_lull=2.0, radiation=0.1, rainfall_1h=0.5, rainfall_24h=2.0,
            soil_moisture=30, soil_temperature=18.0, one_wire_temperature=[19.0, 20.0],
            adc_voltage_ch0=1.1, adc_voltage_ch1=1.2, adc_voltage_ch2=1.3,
            adc_voltage_ch3=1.4, adc_voltage_ch4=1.5, adc_voltage_ch5=1.6,
            adc_voltage_ch6=1.7, adc_voltage_ch7=1.8,
            one_wire_temperature_ch0=1.0, one_wire_temperature_ch1=2.0,
            one_wire_temperature_ch2=3.0, one_wire_temperature_ch3=4.0,
            one_wire_temperature_ch4=5.0, one_wire_temperature_ch5=6.0,
            one_wire_temperature_ch6=7.0, one_wire_temperature_ch7=8.0,
            lightning_strike_count_1h=1, lightning_distance_km=3.0,
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        air_quality_metrics=telemetry_pb2.AirQualityMetrics(
            pm10_standard=1, pm25_standard=2, pm100_standard=3,
            pm10_environmental=4, pm25_environmental=5, pm100_environmental=6,
            particles_03um=7, particles_05um=8, particles_10um=9,
            particles_25um=10, particles_50um=11, particles_100um=12,
            co2=400, co2_temperature=22.0, co2_humidity=45.0,
            form_formaldehyde=1.0, form_humidity=50.0, form_temperature=23.0,
            pm40_standard=13, particles_40um=14, pm_temperature=24.0,
            pm_humidity=46.0, pm_voc_idx=100, pm_nox_idx=1, particles_tps=15,
            pm_status_flags=1,
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        power_metrics=telemetry_pb2.PowerMetrics(
            ch1_voltage=1.0, ch1_current=0.1, ch2_voltage=2.0, ch2_current=0.2,
            ch3_voltage=3.0, ch3_current=0.3, ch4_voltage=4.0, ch4_current=0.4,
            ch5_voltage=5.0, ch5_current=0.5, ch6_voltage=6.0, ch6_current=0.6,
            ch7_voltage=7.0, ch7_current=0.7, ch8_voltage=8.0, ch8_current=0.8,
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        local_stats=telemetry_pb2.LocalStats(
            uptime_seconds=1, channel_utilization=2.0, air_util_tx=3.0,
            num_packets_tx=4, num_packets_rx=5, num_packets_rx_bad=6,
            num_online_nodes=7, num_total_nodes=8, num_rx_dupe=9,
            num_tx_relay=10, num_tx_relay_canceled=11, heap_total_bytes=12,
            heap_free_bytes=13, num_tx_dropped=14, noise_floor=-90,
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        health_metrics=telemetry_pb2.HealthMetrics(heart_bpm=72, spO2=98, temperature=36.8),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        host_metrics=telemetry_pb2.HostMetrics(
            uptime_seconds=1, freemem_bytes=2, diskfree1_bytes=3,
            diskfree2_bytes=4, diskfree3_bytes=5, load1=1, load5=2,
            load15=3, user_string="host",
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        traffic_management_stats=telemetry_pb2.TrafficManagementStats(
            packets_inspected=1, position_dedup_drops=2, nodeinfo_cache_hits=3,
            rate_limit_drops=4, unknown_packet_drops=5, hop_exhausted_packets=6,
            router_hops_preserved=7,
        ),
    )))
    corpus.append(_data(PortNum.TELEMETRY_APP, telemetry_pb2.Telemetry(
        soil_water_metrics=telemetry_pb2.SoilWaterMetrics(
            soil_ph=6.5, ph=7.0, electrical_conductivity=1.0, salinity=2.0,
            nitrogen=3.0, phosphorus=4.0, potassium=5.0, dissolved_oxygen=6.0,
            orp=7.0, chemical_oxygen_demand=8.0, turbidity=9.0, nitrate=10.0,
            ammonium=11.0, biochemical_oxygen_demand=12.0, solar_irradiance=13.0,
        ),
    )))

    corpus.append(_data(PortNum.ROUTING_APP, mesh_pb2.Routing(
        route_request=mesh_pb2.RouteDiscovery(route=[1, 2, 3], snr_towards=[10, 9, 8]),
    )))
    corpus.append(_data(PortNum.ROUTING_APP, mesh_pb2.Routing(
        error_reason=mesh_pb2.Routing.Error.NO_ROUTE,
    )))

    corpus.append(_data(PortNum.NEIGHBORINFO_APP, mesh_pb2.NeighborInfo(
        node_id=0x1234, last_sent_by_id=0x5678, node_broadcast_interval_secs=900,
        neighbors=[
            mesh_pb2.Neighbor(node_id=0x1111, snr=5.5, last_rx_time=1700000000, node_broadcast_interval_secs=900),
        ],
    )))

    corpus.append(_data(PortNum.TRACEROUTE_APP, mesh_pb2.RouteDiscovery(
        route=[1, 2, 3], snr_towards=[10, 9, 8], route_back=[3, 2, 1], snr_back=[8, 9, 10],
    )))

    corpus.append(_data(PortNum.MAP_REPORT_APP, mqtt_pb2.MapReport(
        long_name="Test Node", short_name="TST", role=config_pb2.Config.DeviceConfig.Role.CLIENT,
        hw_model=mesh_pb2.HardwareModel.HELTEC_V3, firmware_version="2.5.0",
        region=config_pb2.Config.LoRaConfig.RegionCode.US, modem_preset=config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST,
        has_default_channel=True, latitude_i=123456789, longitude_i=-123456789,
        altitude=100, position_precision=16, num_online_local_nodes=5,
        has_opted_report_location=True,
    )))

    return corpus


def build_metric_tuples() -> set[tuple[int, str, str]]:
    tuples: set[tuple[int, str, str]] = set()
    for data in build_corpus():
        envelope = decode_data(data, **_COMMON_KWARGS)
        for fv in envelope.fields:
            tuples.add((data.portnum, fv.metric_name, fv.value_type))
    return tuples


def load_golden() -> set[tuple[int, str, str]]:
    raw = json.loads(GOLDEN_PATH.read_text())
    return {tuple(entry) for entry in raw}


def write_golden(tuples: set[tuple[int, str, str]]) -> None:
    ordered = sorted(tuples)
    GOLDEN_PATH.write_text(json.dumps(ordered, indent=2) + "\n")


if __name__ == "__main__":
    write_golden(build_metric_tuples())
    print(f"wrote {GOLDEN_PATH}")
