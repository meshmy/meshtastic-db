"""Metric-name stability check — what makes the vendor/protobufs submodule
bump automerge-eligible (renovate.json). Decodes a fixed corpus of synthetic
messages (tests/golden_metrics.py) through the currently-vendored classes
and asserts every (portnum, metric_name, value_type) tuple in the checked-in
golden fixture is still produced. A pure addition (a new field) passes with
no change here; `make update-golden-metrics` folds it into the baseline. A
removal or rename of an existing field is what this exists to catch: it
would otherwise silently fork a time series into a new, uncorrelated
metric_name with no error anywhere else in the pipeline."""

from golden_metrics import build_metric_tuples, load_golden


def test_golden_metric_names_are_a_subset_of_current_output():
    golden = load_golden()
    current = build_metric_tuples()
    missing = golden - current
    assert not missing, f"metric names removed or renamed: {sorted(missing)}"
