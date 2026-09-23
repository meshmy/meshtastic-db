// Per-metric color ramps. Domains are fixed, not auto-scaled from whatever
// range happens to be fetched, so the legend doesn't jump every time the
// user changes the time window. Temperature is the one metric with a
// physically meaningful zero, so it gets a diverging ramp; everything else
// here is a sequential "badness gradient" with no meaningful center.
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

export const STALE_COLOR = "#6b7684";

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex([r, g, b]) {
  return (
    "#" +
    [r, g, b]
      .map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0"))
      .join("")
  );
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function interpolateStops(value, domain, colors) {
  if (value <= domain[0]) return colors[0];
  if (value >= domain[domain.length - 1]) return colors[colors.length - 1];
  for (let i = 0; i < domain.length - 1; i++) {
    if (value >= domain[i] && value <= domain[i + 1]) {
      const t = (value - domain[i]) / (domain[i + 1] - domain[i]);
      const c0 = hexToRgb(colors[i]);
      const c1 = hexToRgb(colors[i + 1]);
      return rgbToHex(c0.map((v, idx) => lerp(v, c1[idx], t)));
    }
  }
  return colors[colors.length - 1];
}

// `observedDomain` (a [min, max] pair computed from the currently fetched
// playback payload) only matters for a metric with no configured ramp.
export function configFor(metricName, observedDomain) {
  const config = METRIC_COLOR_CONFIG[metricName];
  if (config) return config;
  const hasRange = observedDomain && observedDomain[0] < observedDomain[1];
  const [lo, hi] = hasRange ? observedDomain : [0, 1];
  return { type: "sequential", domain: [lo, (lo + hi) / 2, hi], colors: DEFAULT_COLORS };
}

export function colorFor(metricName, value, observedDomain) {
  if (value === null || value === undefined) return STALE_COLOR;
  const config = configFor(metricName, observedDomain);
  return interpolateStops(value, config.domain, config.colors);
}
