const SOURCE_ID = "map-app-nodes";
const HEATMAP_LAYER_ID = "map-app-nodes-heat";
const CIRCLE_LAYER_ID = "map-app-nodes-circle";
const LABEL_LAYER_ID = "map-app-nodes-label";

// Transparent until app.js's first updateLegend() call installs the real
// per-metric ramp via setHeatmapColors() — avoids a flash of an unrelated
// color scheme between layer creation and the first render.
const PLACEHOLDER_HEATMAP_COLOR = ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(0,0,0,0)", 1, "rgba(0,0,0,0)"];

function emptyCollection() {
  return { type: "FeatureCollection", features: [] };
}

// One GeoJSONSource shared by three layers, all updated via setData() per
// scrub frame (the standard MapLibre animation pattern): a heatmap driven
// by the selected telemetry field's value (heatWeight, normalized 0-1),
// with a small neutral marker + label on top for node identity/location —
// node color no longer encodes the metric's value, the heatmap does.
export class NodeLayers {
  constructor(map, theme) {
    this.map = map;
    this.theme = theme;
    this.hoveredId = null;
    this.onSelect = null;
    this.onHover = null;
    this._attach();

    map.on("click", CIRCLE_LAYER_ID, (event) => {
      const feature = event.features && event.features[0];
      if (feature && this.onSelect) this.onSelect(feature.properties);
    });
    map.on("mouseenter", CIRCLE_LAYER_ID, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", CIRCLE_LAYER_ID, () => {
      map.getCanvas().style.cursor = "";
      if (this.hoveredId !== null) {
        this.hoveredId = null;
        if (this.onHover) this.onHover(null);
      }
    });
    // A layer's own mouseenter/mouseleave fire once for the whole layer,
    // not per feature — per-marker hover-change needs mousemove compared
    // against the last-hovered id instead.
    map.on("mousemove", CIRCLE_LAYER_ID, (event) => {
      const feature = event.features && event.features[0];
      const id = feature ? feature.properties.node_id : null;
      if (id === this.hoveredId) return;
      this.hoveredId = id;
      if (this.onHover) this.onHover(feature ? feature.properties : null);
    });
  }

  _attach() {
    if (!this.map.getSource(SOURCE_ID)) {
      this.map.addSource(SOURCE_ID, { type: "geojson", data: emptyCollection() });
    }
    // Stroke/label colors flip per theme for contrast against the basemap
    // brightness. The marker fill is now a fixed neutral color too (theme
    // -matched, not value-driven) — only its opacity still varies, as a
    // freshness cue (dim when the node has no value at the current scrub
    // instant).
    const strokeColor = this.theme.isLight() ? "#ffffff" : "#0d1117";
    const markerColor = this.theme.isLight() ? "#0d1117" : "#e6edf3";

    // Added first (bottom of the paint order) so the heat layer renders
    // beneath the node markers/labels rather than covering them.
    if (!this.map.getLayer(HEATMAP_LAYER_ID)) {
      this.map.addLayer({
        id: HEATMAP_LAYER_ID,
        type: "heatmap",
        source: SOURCE_ID,
        paint: {
          "heatmap-weight": ["get", "heatWeight"],
          "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 0, 1, 9, 3],
          // "Reasonable radius per node": grows with zoom so a blob
          // doesn't shrink to a speck when zoomed in, in pixels.
          "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 15, 9, 25, 16, 45],
          "heatmap-opacity": 0.75,
          "heatmap-color": PLACEHOLDER_HEATMAP_COLOR,
        },
      });
    }
    if (!this.map.getLayer(CIRCLE_LAYER_ID)) {
      this.map.addLayer({
        id: CIRCLE_LAYER_ID,
        type: "circle",
        source: SOURCE_ID,
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 2, 12, 6],
          "circle-color": markerColor,
          "circle-opacity": ["get", "opacity"],
          "circle-stroke-color": strokeColor,
          "circle-stroke-width": 1,
        },
      });
    }
    if (!this.map.getLayer(LABEL_LAYER_ID)) {
      this.map.addLayer({
        id: LABEL_LAYER_ID,
        type: "symbol",
        source: SOURCE_ID,
        layout: {
          // OpenFreeMap's styles only actually serve the "Noto Sans ..."
          // font stacks (verified against its style JSON's own glyphs
          // usage) — the spec's default text-font ("Open Sans Regular,
          // Arial Unicode MS Regular") 404s against its glyphs endpoint,
          // and that failure was found to stall this whole source's tile
          // pipeline, silently hiding every other layer sharing it too.
          "text-font": ["Noto Sans Regular"],
          "text-field": ["get", "label"],
          "text-size": 11,
          "text-offset": [0, 1.2],
          "text-anchor": "top",
          "text-optional": true,
        },
        paint: {
          "text-color": markerColor,
          "text-halo-color": strokeColor,
          "text-halo-width": 1,
        },
      });
    }
  }

  // A basemap style swap (map.setStyle()) wipes every programmatically
  // added source/layer — call this after the new style has finished
  // loading to re-add them with theme-appropriate colors. The heatmap
  // layer comes back on PLACEHOLDER_HEATMAP_COLOR until the caller also
  // re-applies setHeatmapColors() and re-renders the current frame.
  reattach(theme) {
    this.theme = theme;
    if (this.map.getLayer(LABEL_LAYER_ID)) this.map.removeLayer(LABEL_LAYER_ID);
    if (this.map.getLayer(CIRCLE_LAYER_ID)) this.map.removeLayer(CIRCLE_LAYER_ID);
    if (this.map.getLayer(HEATMAP_LAYER_ID)) this.map.removeLayer(HEATMAP_LAYER_ID);
    if (this.map.getSource(SOURCE_ID)) this.map.removeSource(SOURCE_ID);
    this._attach();
  }

  // `colors` is the current metric's [low, mid, high] ramp (colorScale.js's
  // configFor().colors) — reused here so the heatmap and the legend always
  // agree, rather than maintaining a second color scheme just for heat.
  setHeatmapColors(colors) {
    const [low, mid, high] = colors;
    this.map.setPaintProperty(HEATMAP_LAYER_ID, "heatmap-color", [
      "interpolate",
      ["linear"],
      ["heatmap-density"],
      0,
      "rgba(0,0,0,0)",
      0.2,
      low,
      0.6,
      mid,
      1,
      high,
    ]);
  }

  setData(featureCollection) {
    const source = this.map.getSource(SOURCE_ID);
    if (source) source.setData(featureCollection);
  }
}
