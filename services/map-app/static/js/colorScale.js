// Per-metric color ramps, shared by the heatmap layer's color scale and the
// legend. Domains are fixed, not auto-scaled from whatever range happens to
// be fetched, so the legend doesn't jump every time the user changes the
// time window. Temperature is the one metric with a physically meaningful
// zero, so it gets a diverging ramp; everything else here is a sequential
// "badness gradient" with no meaningful center.
const METRIC_COLOR_CONFIG = {
  "environment_metrics.temperature": {
    type: "diverging",
    domain: [-10, 0, 40],
    colors: ["#2166ac", "#f7f7f7", "#b2182b"],
  },
  "device_metrics.battery_level": {
    type: "sequential",
    domain: [0, 50, 100],
    colors: ["#b2182b", "#f7f7f7", "#1a9850"],
  },
  "environment_metrics.relative_humidity": {
    type: "sequential",
    domain: [0, 50, 100],
    colors: ["#dfc27d", "#f5f5f5", "#01665e"],
  },
  "device_metrics.channel_utilization": {
    type: "sequential",
    domain: [0, 50, 100],
    colors: ["#1a9850", "#f7f7f7", "#b2182b"],
  },
};

const DEFAULT_COLORS = ["#33608c", "#4fa39a", "#f2c14e"];

// `observedDomain` (a [min, max] pair computed from the currently fetched
// playback payload) only matters for a metric with no configured ramp.
export function configFor(metricName, observedDomain) {
  const config = METRIC_COLOR_CONFIG[metricName];
  if (config) return config;
  const hasRange = observedDomain && observedDomain[0] < observedDomain[1];
  const [lo, hi] = hasRange ? observedDomain : [0, 1];
  return { type: "sequential", domain: [lo, (lo + hi) / 2, hi], colors: DEFAULT_COLORS };
}

// Maps a value onto [0, 1] against the metric's configured/observed domain
// for use as a heatmap layer's per-point weight — 0 (no contribution) for
// a missing/stale value rather than an arbitrary in-range default.
export function normalize(value, metricName, observedDomain) {
  if (value === null || value === undefined) return 0;
  const config = configFor(metricName, observedDomain);
  const lo = config.domain[0];
  const hi = config.domain[config.domain.length - 1];
  if (hi <= lo) return 0;
  return Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
}
