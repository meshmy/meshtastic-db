// Per-metric color ramps, shared by the field overlay's blended color and
// the legend. Domains are fixed, not auto-scaled from whatever range
// happens to be fetched, so the legend doesn't jump every time the user
// changes the time window. Temperature is the one metric with a physically
// meaningful zero, so it gets a diverging ramp; everything else here is a
// sequential "badness gradient" with no meaningful center.
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
// — the field overlay blends this position (not the raw value) between
// nearby nodes, and 0 for a missing/stale value keeps it from pulling the
// blend toward an arbitrary in-range default.
export function normalize(value, metricName, observedDomain) {
  if (value === null || value === undefined) return 0;
  const config = configFor(metricName, observedDomain);
  const lo = config.domain[0];
  const hi = config.domain[config.domain.length - 1];
  if (hi <= lo) return 0;
  return Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
}

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

// A precomputed table of `steps` [r, g, b] triples spanning t in [0, 1] —
// the field overlay looks up a color per pixel every redraw and can't
// afford to redo the hex-parsing/interpolation work that often, so this is
// computed once per metric/theme change instead (see field.js).
export function buildRampLUT(colors, steps = 256) {
  const stops = colors.map(hexToRgb);
  const lut = new Array(steps);
  for (let i = 0; i < steps; i++) {
    const scaled = (i / (steps - 1)) * (stops.length - 1);
    const idx = Math.min(stops.length - 2, Math.floor(scaled));
    const localT = scaled - idx;
    const c0 = stops[idx];
    const c1 = stops[idx + 1];
    lut[i] = [lerp(c0[0], c1[0], localT), lerp(c0[1], c1[1], localT), lerp(c0[2], c1[2], localT)];
  }
  return lut;
}
