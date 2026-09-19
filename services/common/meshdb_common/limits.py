"""Per-metric numeric sanity bounds, enforced at write time (`db.py`) and by
the one-off historical trim script (`services/archive-job/trim_invalid_metrics.py`).
Both share this module so a rule change takes effect for new data and any
future cleanup pass identically.

Matching is against the *leaf* field name — the metric_name's final dotted
segment, with any trailing `[N]` repeated-field index stripped, lowercased —
not the full dotted path, so one rule covers a field regardless of which
Telemetry oneof variant it appears under (e.g. one `"voltage"` pattern
covers `device_metrics.voltage`, `environment_metrics.adc_voltage_ch0`, and
`power_metrics.ch1_voltage` alike).

Extend by adding one entry to `_EXACT` (a precise leaf name) or `_PATTERNS`
(a substring match, tried in order after `_EXACT`) — no other code needs to
change. `bounds_for` returning `None` means no rule is configured, so the
value passes through unchecked (fail-open, same as before this module
existed).
"""

from __future__ import annotations

import re

Bounds = tuple[float, float]

# ph/soil_ph are exact-match, not a "ph" substring pattern, because
# soil_water_metrics.phosphorus also contains "ph" — a substring rule would
# wrongly bound it to the pH scale.
_EXACT: dict[str, Bounds] = {
    "battery_level": (0.0, 101.0),  # >100 signals "powered", not a charge %
    "spo2": (0.0, 100.0),
    "channel_utilization": (0.0, 100.0),
    "air_util_tx": (0.0, 100.0),
    "soil_moisture": (0.0, 100.0),
    "soil_ph": (0.0, 14.0),
    "ph": (0.0, 14.0),
    "heart_bpm": (0.0, 300.0),
    "wind_direction": (0.0, 360.0),
}

_PATTERNS: tuple[tuple[re.Pattern[str], Bounds], ...] = (
    (re.compile("voltage"), (-80.0, 80.0)),
    (re.compile("temperature"), (-100.0, 100.0)),
    (re.compile("humidity"), (0.0, 100.0)),
    # Widened below zero (beyond standard ~300-1100 hPa atmospheric range)
    # to also allow differential/negative-pressure-room deployments.
    (re.compile("pressure"), (-50.0, 1100.0)),
)

_INDEX_SUFFIX = re.compile(r"\[\d+\]$")


def leaf_name(metric_name: str) -> str:
    tail = metric_name.rsplit(".", 1)[-1]
    return _INDEX_SUFFIX.sub("", tail).lower()


def bounds_for(metric_name: str) -> Bounds | None:
    name = leaf_name(metric_name)
    bounds = _EXACT.get(name)
    if bounds is not None:
        return bounds
    for pattern, b in _PATTERNS:
        if pattern.search(name):
            return b
    return None


def value_within_limits(metric_name: str, value: float) -> bool:
    bounds = bounds_for(metric_name)
    if bounds is None:
        return True
    lo, hi = bounds
    return lo <= value <= hi
