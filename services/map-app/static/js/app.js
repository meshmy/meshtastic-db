import { fetchRegions, fetchNodes, fetchMetrics, fetchPlayback } from "./api.js";
import { colorFor, configFor } from "./colorScale.js";
import { ThemeController } from "./theme.js";
import { NodeLayers } from "./layers.js";
import { Playback } from "./playback.js";
import { MAP_STYLES, DEFAULT_RANGE_SECONDS, PLAYBACK_DURATION_MS } from "./config.js";

const regionSelect = document.getElementById("region-select");
const metricSelect = document.getElementById("metric-select");
const playPauseButton = document.getElementById("play-pause");
const scrubRange = document.getElementById("scrub-range");
const scrubTime = document.getElementById("scrub-time");
const themeToggleButton = document.getElementById("theme-toggle");
const legendGradient = document.getElementById("legend-gradient");
const legendMin = document.getElementById("legend-min");
const legendMax = document.getElementById("legend-max");
const popup = document.getElementById("node-popup");
const popupName = document.getElementById("popup-name");
const popupMeta = document.getElementById("popup-meta");

let nodesById = new Map();
let layers = null;
let playback = null;
let observedDomain = [0, 1];
let currentMetric = null;

// Constructing ThemeController before the map only sets the `data-theme`
// dataset attribute (used for the initial style pick below) — its
// onChange callback isn't invoked until a later cycle()/OS-preference
// change, by which point `map`/`layers` are already assigned.
const theme = new ThemeController((resolved) => {
  map.setStyle(MAP_STYLES[resolved]);
  map.once("idle", () => layers && layers.reattach(theme));
});

const map = new maplibregl.Map({
  container: "map",
  style: MAP_STYLES[theme.resolved()],
  center: [0, 20],
  zoom: 1.5,
});

function themeIcon() {
  if (theme.mode === "system") return "🖥";
  return theme.mode === "dark" ? "🌙" : "☀";
}

function updateThemeButton() {
  themeToggleButton.textContent = themeIcon();
  themeToggleButton.title = `Theme: ${theme.mode}`;
}

themeToggleButton.addEventListener("click", () => {
  theme.cycle();
  updateThemeButton();
});
updateThemeButton();

function showPopup(properties) {
  if (!properties) {
    popup.hidden = true;
    return;
  }
  const node = nodesById.get(properties.node_id);
  popupName.textContent = (node && (node.short_name || node.long_name)) || `!${properties.node_id.toString(16)}`;
  popupMeta.textContent =
    node && node.last_heard ? `Last heard ${new Date(node.last_heard * 1000).toLocaleString()}` : "Never heard";
  popup.hidden = false;
}

function render(t) {
  if (!playback || !layers) return;
  const frame = playback.frameAt(t);
  for (const feature of frame.features) {
    const node = nodesById.get(feature.properties.node_id);
    feature.properties.label = (node && node.short_name) || `!${feature.properties.node_id.toString(16)}`;
    feature.properties.color = colorFor(currentMetric, feature.properties.value, observedDomain);
    feature.properties.opacity = feature.properties.value === null ? 0.4 : 0.9;
  }
  layers.setData(frame);
  scrubRange.value = String(Math.round(t));
  scrubTime.textContent = new Date(t * 1000).toLocaleString();
}

function updateLegend() {
  const config = configFor(currentMetric, observedDomain);
  legendGradient.style.background = `linear-gradient(to right, ${config.colors.join(",")})`;
  legendMin.textContent = config.domain[0].toFixed(1);
  legendMax.textContent = config.domain[config.domain.length - 1].toFixed(1);
}

function computeObservedDomain(payload) {
  let min = Infinity;
  let max = -Infinity;
  for (const series of Object.values(payload.nodes)) {
    for (const [, value] of series.values) {
      if (value < min) min = value;
      if (value > max) max = value;
    }
  }
  return min <= max ? [min, max] : [0, 1];
}

async function loadPlayback(region, metric) {
  const end = Math.floor(Date.now() / 1000);
  const start = end - DEFAULT_RANGE_SECONDS;
  const payload = await fetchPlayback(region, metric, start, end);
  observedDomain = computeObservedDomain(payload);
  playback = new Playback(payload);
  scrubRange.min = String(start);
  scrubRange.max = String(end);
  updateLegend();
  render(end);
}

function fitToNodes(nodes) {
  const withPosition = nodes.filter((n) => n.position);
  if (withPosition.length === 0) return;
  const bounds = new maplibregl.LngLatBounds();
  for (const node of withPosition) {
    bounds.extend([node.position.lon, node.position.lat]);
  }
  map.fitBounds(bounds, { padding: 80, maxZoom: 12, duration: 500 });
}

async function loadRegion(region) {
  if (playback) playback.pause();
  playPauseButton.textContent = "▶";
  const [nodesPayload, metricsPayload] = await Promise.all([fetchNodes(region), fetchMetrics(region)]);
  nodesById = new Map(nodesPayload.nodes.map((n) => [n.node_id, n]));
  fitToNodes(nodesPayload.nodes);

  metricSelect.innerHTML = "";
  for (const name of metricsPayload.metrics) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    metricSelect.appendChild(option);
  }
  currentMetric = metricsPayload.metrics[0] || null;
  if (currentMetric) {
    metricSelect.value = currentMetric;
    await loadPlayback(region, currentMetric);
  } else if (layers) {
    layers.setData({ type: "FeatureCollection", features: [] });
  }
}

regionSelect.addEventListener("change", () => loadRegion(regionSelect.value));
metricSelect.addEventListener("change", () => {
  currentMetric = metricSelect.value;
  loadPlayback(regionSelect.value, currentMetric);
});

playPauseButton.addEventListener("click", () => {
  if (!playback) return;
  if (playback.playing) {
    playback.pause();
    playPauseButton.textContent = "▶";
  } else {
    playPauseButton.textContent = "⏸";
    playback.play(render, PLAYBACK_DURATION_MS);
  }
});

scrubRange.addEventListener("input", () => {
  if (!playback) return;
  playback.pause();
  playPauseButton.textContent = "▶";
  playback.seek(Number(scrubRange.value));
  render(playback.current);
});

map.on("load", async () => {
  layers = new NodeLayers(map, theme);
  layers.onHover = showPopup;
  layers.onSelect = showPopup;

  const { regions } = await fetchRegions();
  for (const r of regions) {
    const option = document.createElement("option");
    option.value = r.region;
    option.textContent = `${r.region} (${r.with_position_count}/${r.node_count})`;
    option.disabled = r.with_position_count === 0;
    regionSelect.appendChild(option);
  }
  const firstUsable = regions.find((r) => r.with_position_count > 0) || regions[0];
  if (firstUsable) {
    regionSelect.value = firstUsable.region;
    await loadRegion(firstUsable.region);
  }
});
