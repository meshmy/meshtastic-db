"""Regression check for the core requirement: walk_message() must handle a
message shape it has no hardcoded knowledge of, with zero code change.

This exercises the property using a Telemetry variant (HealthMetrics) that
decode.py never names anywhere — standing in for "upstream adds a new field
to an existing variant, or a new variant inside the Telemetry oneof". The
literal end-to-end version (edit vendor/protobufs, `make proto-gen`, decode
with no other changes) is a manual/periodic check, since it means mutating
the vendored submodule's checkout — see the design plan's §10.5."""

from meshdb_common.decode import walk_message
from meshtastic import telemetry_pb2


def test_walk_message_handles_a_telemetry_variant_it_has_no_special_case_for():
    telemetry = telemetry_pb2.Telemetry(
        health_metrics=telemetry_pb2.HealthMetrics(heart_bpm=72, spO2=98, temperature=36.8),
    )

    names = {fv.metric_name for fv in walk_message(telemetry)}

    assert "health_metrics.heart_bpm" in names
    assert "health_metrics.spO2" in names
    assert "health_metrics.temperature" in names
