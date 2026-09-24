const SOURCE_ID = "map-app-nodes";
const CIRCLE_LAYER_ID = "map-app-nodes-circle";
const LABEL_LAYER_ID = "map-app-nodes-label";

function emptyCollection() {
  return { type: "FeatureCollection", features: [] };
}

// Node markers + labels as one GeoJSONSource updated via setData() per
// scrub frame — the standard MapLibre animation pattern (also how
// meshatlas animates its own live node layer).
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
    // brightness; marker fill color itself comes from the telemetry value
    // (set per-feature in app.js), not from theme.
    const strokeColor = this.theme.isLight() ? "#ffffff" : "#0d1117";
    const textColor = this.theme.isLight() ? "#0d1117" : "#e6edf3";

    if (!this.map.getLayer(CIRCLE_LAYER_ID)) {
      this.map.addLayer({
        id: CIRCLE_LAYER_ID,
        type: "circle",
        source: SOURCE_ID,
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 3, 12, 8],
          "circle-color": ["get", "color"],
          "circle-opacity": ["get", "opacity"],
          "circle-stroke-color": strokeColor,
          "circle-stroke-width": 1.5,
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
          // pipeline, silently hiding the circle layer sharing it too.
          "text-font": ["Noto Sans Regular"],
          "text-field": ["get", "label"],
          "text-size": 11,
          "text-offset": [0, 1.2],
          "text-anchor": "top",
          "text-optional": true,
        },
        paint: {
          "text-color": textColor,
          "text-halo-color": strokeColor,
          "text-halo-width": 1,
        },
      });
    }
  }

  // A basemap style swap (map.setStyle()) wipes every programmatically
  // added source/layer — call this after the new style has finished
  // loading to re-add them with theme-appropriate colors, then
  // repopulate from whatever data the caller re-renders next.
  reattach(theme) {
    this.theme = theme;
    if (this.map.getLayer(LABEL_LAYER_ID)) this.map.removeLayer(LABEL_LAYER_ID);
    if (this.map.getLayer(CIRCLE_LAYER_ID)) this.map.removeLayer(CIRCLE_LAYER_ID);
    if (this.map.getSource(SOURCE_ID)) this.map.removeSource(SOURCE_ID);
    this._attach();
  }

  setData(featureCollection) {
    const source = this.map.getSource(SOURCE_ID);
    if (source) source.setData(featureCollection);
  }
}
