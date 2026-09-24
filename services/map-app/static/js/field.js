import { buildRampLUT } from "./colorScale.js";

// [zoom, screen-pixel radius] control points, linearly interpolated and
// clamped at the ends. A fixed real-world radius (e.g. "3km") looks right
// zoomed into a city block but is imperceptible at a whole-country view —
// this instead targets a radius that stays clearly visible at any zoom the
// app actually shows, growing at low zoom rather than shrinking with the
// geography, and growing further at high zoom for a more sensor-local feel.
const DEFAULT_RADIUS_STOPS = [
  [0, 80],
  [4, 120],
  [6, 160],
  [9, 200],
  [12, 240],
  [16, 300],
];

function interpolateStops(x, stops) {
  if (x <= stops[0][0]) return stops[0][1];
  const last = stops[stops.length - 1];
  if (x >= last[0]) return last[1];
  for (let i = 0; i < stops.length - 1; i++) {
    const [x0, y0] = stops[i];
    const [x1, y1] = stops[i + 1];
    if (x >= x0 && x <= x1) return y0 + ((y1 - y0) * (x - x0)) / (x1 - x0);
  }
  return last[1];
}

// A continuous "field" of the selected telemetry value, in the style of a
// weather-map overlay (zoom.earth/earth.nullschool) — each node's value
// blends smoothly into its neighbors within a radius, rather than
// MapLibre's native `heatmap` layer, which sums point density and can't
// represent "this node's own value" once points are close together.
// Rendered as a small offscreen canvas, georeferenced onto the map via a
// `canvas` source (MapLibre re-samples it as a raster layer), redrawn
// whenever the camera moves or the data changes.
export class FieldOverlay {
  constructor(map, { radiusStops = DEFAULT_RADIUS_STOPS, resolution = 96, maxOpacity = 0.85 } = {}) {
    this.map = map;
    this.radiusStops = radiusStops;
    this.maxOpacity = maxOpacity;
    this.sourceId = "map-app-field";
    this.layerId = "map-app-field-layer";
    this.points = [];
    this.lut = null;
    this._rafId = null;

    this.canvas = document.createElement("canvas");
    this.canvas.width = resolution;
    this.canvas.height = resolution;
    this.ctx = this.canvas.getContext("2d", { willReadFrequently: false });

    this._attach();
    this._onMove = () => {
      this._updateCoordinates();
      this._scheduleRedraw();
    };
    map.on("move", this._onMove);
  }

  _currentCorners() {
    const bounds = this.map.getBounds();
    return [
      [bounds.getWest(), bounds.getNorth()],
      [bounds.getEast(), bounds.getNorth()],
      [bounds.getEast(), bounds.getSouth()],
      [bounds.getWest(), bounds.getSouth()],
    ];
  }

  _attach() {
    if (!this.map.getSource(this.sourceId)) {
      this.map.addSource(this.sourceId, {
        type: "canvas",
        canvas: this.canvas,
        coordinates: this._currentCorners(),
        animate: true,
      });
    }
    if (!this.map.getLayer(this.layerId)) {
      this.map.addLayer({
        id: this.layerId,
        type: "raster",
        source: this.sourceId,
        // No cross-fade on texture updates -- this canvas is repainted
        // every redraw, a fade would just blur each transition.
        paint: { "raster-opacity": 1, "raster-fade-duration": 0 },
      });
    }
  }

  // A basemap style swap wipes every programmatically added source/layer,
  // same as NodeLayers — re-add them, then the caller re-applies colors
  // and the current frame's points.
  reattach() {
    if (this.map.getLayer(this.layerId)) this.map.removeLayer(this.layerId);
    if (this.map.getSource(this.sourceId)) this.map.removeSource(this.sourceId);
    this._attach();
    this._scheduleRedraw();
  }

  _updateCoordinates() {
    const source = this.map.getSource(this.sourceId);
    if (source && source.setCoordinates) source.setCoordinates(this._currentCorners());
  }

  setColors(colors) {
    this.lut = buildRampLUT(colors);
    this._scheduleRedraw();
  }

  // `points`: [{lon, lat, t}], t already normalized to [0, 1] via
  // colorScale.js's normalize() — a point with no current value is left
  // out entirely rather than passed as t=0, so it doesn't pull the blend
  // toward the cold end of the ramp.
  setFeatures(points) {
    this.points = points;
    this._scheduleRedraw();
  }

  _scheduleRedraw() {
    if (this._rafId !== null) return;
    this._rafId = requestAnimationFrame(() => {
      this._rafId = null;
      this._draw();
    });
  }

  _draw() {
    if (!this.lut) return;
    const { width, height } = this.canvas;
    const bounds = this.map.getBounds();
    const west = bounds.getWest();
    const east = bounds.getEast();
    const north = bounds.getNorth();
    const south = bounds.getSouth();
    const lonSpan = east - west || 1e-9;
    const latSpan = north - south || 1e-9;

    // The radius is chosen in on-screen CSS pixels (see DEFAULT_RADIUS_STOPS)
    // then converted into this canvas's own (smaller) pixel space, since
    // this canvas is stretched by MapLibre to fill the actual map container.
    const container = this.map.getContainer();
    const screenRadiusPx = interpolateStops(this.map.getZoom(), this.radiusStops);
    const scaleX = width / (container.clientWidth || width);
    const scaleY = height / (container.clientHeight || height);
    const radiusPx = screenRadiusPx * ((scaleX + scaleY) / 2);

    const points = this.points.map((p) => ({
      t: p.t,
      px: ((p.lon - west) / lonSpan) * width,
      py: ((north - p.lat) / latSpan) * height,
    }));

    const image = this.ctx.createImageData(width, height);
    const data = image.data;
    const lut = this.lut;

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        let weightSum = 0;
        let tSum = 0;
        for (const p of points) {
          const dx = x - p.px;
          const dy = y - p.py;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist >= radiusPx) continue;
          // Linear falloff to exactly 0 at the radius edge, rather than a
          // hard cutoff (so blobs fade out instead of clipping) or a
          // steeper curve (which would leave most of the radius faint,
          // with only a small bright core near each point).
          const w = 1 - dist / radiusPx;
          weightSum += w;
          tSum += w * p.t;
        }
        const idx = (y * width + x) * 4;
        if (weightSum <= 0) {
          data[idx + 3] = 0;
          continue;
        }
        const [r, g, b] = lut[Math.round((tSum / weightSum) * (lut.length - 1))];
        data[idx] = r;
        data[idx + 1] = g;
        data[idx + 2] = b;
        data[idx + 3] = Math.min(1, weightSum) * this.maxOpacity * 255;
      }
    }
    this.ctx.putImageData(image, 0, 0);
  }
}
