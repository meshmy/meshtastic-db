import { API_BASE } from "./config.js";

async function getJson(path) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${path} failed: ${response.status} ${body}`);
  }
  return response.json();
}

export function fetchRegions() {
  return getJson("/regions");
}

export function fetchNodes(region) {
  return getJson(`/nodes?region=${encodeURIComponent(region)}`);
}

export function fetchMetrics(region) {
  return getJson(`/metrics?region=${encodeURIComponent(region)}`);
}

export function fetchPlayback(region, metric, start, end) {
  const params = new URLSearchParams({ region, metric, start, end });
  return getJson(`/playback?${params}`);
}
