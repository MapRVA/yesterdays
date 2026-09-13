// Shared layer definitions for georeferenced-image points rendered from the
// image_points vector tiles. data_layers.ts instantiates them under LAYER_IDS
// for the sitewide maps; the georeference interface instantiates them under
// its own context-image-* ids so LayerControl's image-panel state (which
// targets LAYER_IDS) never fights that page's display-mode radios.
import type {
  CircleLayerSpecification,
  ExpressionSpecification,
  FilterSpecification,
  SymbolLayerSpecification,
} from "maplibre-gl";
import { DIRECTION_SPRITE_ID } from "./layer_ids";

const SOURCE_LAYER = "image_points";

// Layers normally read the image_points source-layer out of the vector tiles.
// A caller with its own GeoJSON source (in-view search draws its results that
// way) passes `sourceLayer: null`: MapLibre rejects a layer that names a
// source-layer when the source is GeoJSON, so the key has to be absent, not
// empty.
function sourceLayerSpec(
  sourceLayer: string | null | undefined,
): { "source-layer"?: string } {
  return sourceLayer === null ? {} : { "source-layer": sourceLayer ?? SOURCE_LAYER };
}

export interface ImagePointLayerOptions {
  /** Source id the layer reads from. */
  source: string;
  /**
   * Source-layer within a vector source. Defaults to `image_points`; pass
   * `null` when `source` is a GeoJSON source, which has no source-layers.
   */
  sourceLayer?: string | null;
  /** Marker color — the emphasis knob between normal and ghost styling. */
  color: string;
  /** Multiplier applied to every opacity ramp (ghost mode dims). */
  opacity?: number;
  /** Extra filter ANDed into the layer filter (e.g. exclude current image). */
  extraFilter?: ExpressionSpecification | null;
  visibility?: "visible" | "none";
  /**
   * Overrides the layer's default zoom floor. The georeference interface
   * raises it so context images stay out of the way until you are zoomed in
   * far enough to place a pin (see context_images.ts CONTEXT_MIN_ZOOM).
   */
  minzoom?: number;
}

function circleFilter(
  extraFilter: ExpressionSpecification | null,
): FilterSpecification {
  return extraFilter ?? ["literal", true];
}

function directionFilter(
  extraFilter: ExpressionSpecification | null,
): FilterSpecification {
  return extraFilter
    ? ["all", ["has", "direction"], extraFilter]
    : ["has", "direction"];
}

// The mode-dependent detail ramps are exported separately so the georeference
// interface can re-apply them via setPaintProperty when its display mode
// changes, without recreating the layers.

function heatmapOpacityRamp(opacity: number): ExpressionSpecification {
  return ["interpolate", ["exponential", 5], ["zoom"], 14, opacity, 17, 0];
}

export function detailOpacityRamp(opacity: number): ExpressionSpecification {
  return ["interpolate", ["exponential", 1.5], ["zoom"], 7, 0, 10, opacity];
}

export function detailColorRamp(color: string): ExpressionSpecification {
  return ["interpolate", ["linear"], ["zoom"], 14, "#fff", 15, color];
}

/** Blurred background circles: a heatmap effect that fades out at high zoom. */
export function heatmapCircleLayer(
  id: string,
  options: ImagePointLayerOptions,
): CircleLayerSpecification {
  const {
    source,
    sourceLayer,
    color,
    opacity = 1,
    extraFilter = null,
    visibility = "visible",
  } = options;
  return {
    id,
    type: "circle",
    source,
    ...sourceLayerSpec(sourceLayer),
    filter: circleFilter(extraFilter),
    maxzoom: 17,
    layout: { visibility },
    paint: {
      "circle-blur": ["interpolate", ["linear"], ["zoom"], 14.5, 1.5, 16, 3],
      "circle-opacity": heatmapOpacityRamp(opacity),
      "circle-radius": [
        "interpolate",
        ["exponential", 2],
        ["zoom"],
        10,
        25,
        20,
        100,
      ],
      "circle-color": color,
      "circle-pitch-alignment": "map",
      "circle-pitch-scale": "map",
    },
  };
}

/** Detail circles: blur transitions from blurry to sharp as zoom increases. */
export function detailCircleLayer(
  id: string,
  options: ImagePointLayerOptions,
): CircleLayerSpecification {
  const {
    source,
    sourceLayer,
    color,
    opacity = 1,
    extraFilter = null,
    visibility = "visible",
    minzoom = 7,
  } = options;
  return {
    id,
    type: "circle",
    source,
    ...sourceLayerSpec(sourceLayer),
    minzoom,
    filter: circleFilter(extraFilter),
    layout: { visibility },
    paint: {
      "circle-blur": [
        "interpolate",
        ["exponential", 1.5],
        ["zoom"],
        9,
        5,
        13,
        1,
        15,
        0,
      ],
      "circle-opacity": detailOpacityRamp(opacity),
      "circle-radius": [
        "interpolate",
        ["exponential", 0.6],
        ["zoom"],
        7,
        1,
        15,
        3,
        20,
        9,
      ],
      "circle-color": detailColorRamp(color),
      "circle-stroke-color": "#fff",
      "circle-stroke-opacity": opacity,
      "circle-stroke-width": [
        "interpolate",
        ["linear"],
        ["zoom"],
        13.5,
        0,
        15,
        1,
        20,
        2,
      ],
      "circle-pitch-alignment": "map",
      "circle-pitch-scale": "map",
    },
  };
}

/** Direction arrows that fade in at high zoom, rendered under the circles. */
export function directionSymbolLayer(
  id: string,
  options: ImagePointLayerOptions,
): SymbolLayerSpecification {
  const {
    source,
    sourceLayer,
    opacity = 1,
    extraFilter = null,
    visibility = "visible",
    minzoom = 15,
  } = options;
  return {
    id,
    type: "symbol",
    source,
    ...sourceLayerSpec(sourceLayer),
    minzoom,
    filter: directionFilter(extraFilter),
    layout: {
      "icon-image": DIRECTION_SPRITE_ID,
      "icon-overlap": "always",
      "icon-size": [
        "interpolate",
        ["exponential", 0.7],
        ["zoom"],
        15,
        0.3,
        20,
        1,
      ],
      "icon-rotate": ["to-number", ["get", "direction"]],
      "icon-rotation-alignment": "map",
      "icon-pitch-alignment": "map",
      visibility,
    },
    paint: { "icon-opacity": opacity },
  };
}

/** Flat circles for the "simple" display style: constant size at all zooms. */
export function simpleCircleLayer(
  id: string,
  options: ImagePointLayerOptions,
): CircleLayerSpecification {
  const {
    source,
    sourceLayer,
    color,
    opacity = 1,
    extraFilter = null,
    visibility = "visible",
  } = options;
  return {
    id,
    type: "circle",
    source,
    ...sourceLayerSpec(sourceLayer),
    filter: circleFilter(extraFilter),
    layout: { visibility },
    paint: {
      "circle-radius": 8,
      "circle-color": color,
      "circle-opacity": opacity,
      "circle-stroke-color": "#fff",
      "circle-stroke-opacity": opacity,
      "circle-stroke-width": 2,
      "circle-pitch-alignment": "map",
      "circle-pitch-scale": "map",
    },
  };
}

/** Direction arrows for the "simple" display style. */
export function simpleDirectionSymbolLayer(
  id: string,
  options: ImagePointLayerOptions,
): SymbolLayerSpecification {
  const {
    source,
    sourceLayer,
    opacity = 1,
    extraFilter = null,
    visibility = "visible",
  } = options;
  return {
    id,
    type: "symbol",
    source,
    ...sourceLayerSpec(sourceLayer),
    filter: directionFilter(extraFilter),
    layout: {
      "icon-image": DIRECTION_SPRITE_ID,
      "icon-overlap": "always",
      "icon-size": 1,
      "icon-rotate": ["to-number", ["get", "direction"]],
      "icon-rotation-alignment": "map",
      "icon-pitch-alignment": "map",
      visibility,
    },
    paint: { "icon-opacity": opacity },
  };
}
