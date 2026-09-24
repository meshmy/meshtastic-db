export const API_BASE = "/api";

// Same OpenFreeMap style slugs meshatlas already uses in production —
// verified working rather than guessed.
export const MAP_STYLES = {
  dark: "https://tiles.openfreemap.org/styles/dark",
  light: "https://tiles.openfreemap.org/styles/positron",
};

// Beyond this age, a node's last-known value is treated as stale rather
// than shown as if it were current.
export const STALE_AFTER_SECONDS = 3 * 3600;

export const DEFAULT_RANGE_SECONDS = 24 * 3600;

// Wall-clock time to animate through one full selected range when playing.
export const PLAYBACK_DURATION_MS = 30000;

// How far a single node's reading is treated as locally representative for
// the field overlay, in real-world km (roughly typical urban LoRa spacing)
// — kept in real-world distance rather than screen pixels so the blob
// covers the same ground area regardless of zoom, the way an actual sensor
// coverage radius would.
export const FIELD_RADIUS_KM = 3;

