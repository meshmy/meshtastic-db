import { STALE_AFTER_SECONDS } from "./config.js";

// `series` is an array of [t, ...] sorted ascending by t (matches the
// backend's ORDER BY node_id, time) — binary search for the last entry at
// or before `t`, i.e. the "as-of" reading a node would have been showing
// at that instant.
function asOf(series, t) {
  let lo = 0;
  let hi = series.length - 1;
  let result = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (series[mid][0] <= t) {
      result = series[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}

export class Playback {
  constructor(payload) {
    this.payload = payload;
    this.start = payload.range.start;
    this.end = payload.range.end;
    this.current = this.end;
    this.playing = false;
    this._rafId = null;
    this._lastFrameMs = null;
  }

  // Positions are [t, lat, lon, alt]; GeoJSON coordinates are [lon, lat].
  frameAt(t) {
    const features = [];
    for (const [nodeIdStr, series] of Object.entries(this.payload.nodes)) {
      const position = asOf(series.positions, t);
      if (!position) continue;
      const value = asOf(series.values, t);
      const stale = !value || t - value[0] > STALE_AFTER_SECONDS;
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [position[2], position[1]] },
        properties: {
          node_id: Number(nodeIdStr),
          value: stale ? null : value[1],
        },
      });
    }
    return { type: "FeatureCollection", features };
  }

  seek(t) {
    this.current = Math.min(this.end, Math.max(this.start, t));
  }

  play(onFrame, durationMs) {
    this.playing = true;
    this._lastFrameMs = null;
    // Starting playback while parked at `end` (the default view) would
    // otherwise overflow past `end` on the very first advancing frame and
    // wrap to `start` almost immediately, reading as a jarring reset
    // rather than a smooth play — jump to `start` synchronously instead so
    // the loop always has room to advance forward from here.
    if (this.current >= this.end) {
      this.current = this.start;
      onFrame(this.current);
    }
    const step = (nowMs) => {
      if (!this.playing) return;
      if (this._lastFrameMs !== null) {
        const deltaMs = nowMs - this._lastFrameMs;
        const range = this.end - this.start;
        this.current += (deltaMs / durationMs) * range;
        if (this.current >= this.end) this.current = this.start;
      }
      this._lastFrameMs = nowMs;
      onFrame(this.current);
      this._rafId = requestAnimationFrame(step);
    };
    this._rafId = requestAnimationFrame(step);
  }

  pause() {
    this.playing = false;
    if (this._rafId) cancelAnimationFrame(this._rafId);
  }
}
