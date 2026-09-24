import { buildRampLUT } from "./colorScale.js";

const EARTH_RADIUS_KM = 6371;
const KM_PER_DEGREE_LAT = (Math.PI / 180) * EARTH_RADIUS_KM;

function kmPerDegreeLon(latDeg) {
  return (Math.PI / 180) * EARTH_RADIUS_KM * Math.cos((latDeg * Math.PI) / 180);
}

// A continuous "field" of the selected telemetry value, in the style of a
// weather-map overlay (zoom.earth/earth.nullschool) — each node's value
// blends smoothly into its neighbors within a fixed real-world radius,
// rather than MapLibre's native `heatmap` layer, which sums point density
// and can't represent "this node's own value" once points are close
// together. Rendered as a small offscreen canvas, georeferenced onto the
// map via a `canvas` source (MapLibre re-samples it as a raster layer),
// redrawn whenever the camera moves or the data changes.
export class FieldOverlay {
  constructor(map, { radiusKm = 3, resolution = 96, maxOpacity = 0.75 } = {}) {
    this.map = map;
    this.radiusKm = radiusKm;
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
    const centerLat = (north + south) / 2;

    // Real-world radius converted to this frame's canvas-pixel space, so
    // the blob covers the same ground area at any zoom level rather than
    // a fixed screen size.
    const radiusPxX = (this.radiusKm / kmPerDegreeLon(centerLat)) * (width / lonSpan);
    const radiusPxY = (this.radiusKm / KM_PER_DEGREE_LAT) * (height / latSpan);
    const radiusPx = (radiusPxX + radiusPxY) / 2;

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
          // Smooth falloff to exactly 0 at the radius edge, rather than a
          // hard cutoff, so blobs fade out instead of clipping.
          const w = (1 - dist / radiusPx) ** 2;
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
