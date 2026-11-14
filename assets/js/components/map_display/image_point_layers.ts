// Shared layer definitions for georeferenced-image points rendered from the
// image_points vector tiles. data_layers.ts instantiates them under LAYER_IDS
// for the sitewide maps; the georeference interface instantiates them under
// its own context-image-* ids so LayerControl's image-panel state (which
// targets LAYER_IDS) never fights that page's display-mode radios.
import type {
  CircleLayerSpecification,
  ExpressionSpecification,
  FilterSpecification,
  HeatmapLayerSpecification,
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

// Tiles up to settings.MAP_TILE_CLUSTER_MAX_ZOOM hold clusters (one feature
// per tile-grid cell and fuzzy dates, counted in `n`) rather than images, so
// only the heatmap may read them; the detail circles start a zoom above.
const DETAIL_CIRCLE_MIN_ZOOM = Math.max(
  8,
  (window.MAP_TILE_CLUSTER_MAX_ZOOM ?? 8) + 1,
);

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

// "#rrggbb" or "#rgb" → "rgba(r,g,b,alpha)". The heatmap ramp needs the marker
// color at several alphas, which a hex string can't express. Colors come from
// Bootstrap's --bs-* properties (always hex); anything else is passed through
// unchanged, so it renders opaque rather than failing.
function withAlpha(color: string, alpha: number): string {
  const hex = color.replace(/^#/, "");
  const full =
    hex.length === 3 ? [...hex].map((c) => c + c).join("") : hex;
  if (!/^[0-9a-f]{6}$/i.test(full)) return color;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

export function detailOpacityRamp(opacity: number): ExpressionSpecification {
  return ["interpolate", ["linear"], ["zoom"], 9, 0, 11, opacity];
}

export function detailColorRamp(color: string): ExpressionSpecification {
  return ["interpolate", ["linear"], ["zoom"], 14, "#fff", 15, color];
}

/**
 * Density heatmap drawn under the detail circles. The color ramp tops out at
 * half opacity, so the base map stays legible beneath dense areas.
 */
export function heatmapLayer(
  id: string,
  options: ImagePointLayerOptions,
): HeatmapLayerSpecification {
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
    type: "heatmap",
    source,
    ...sourceLayerSpec(sourceLayer),
    filter: circleFilter(extraFilter),
    layout: { visibility },
    paint: {
      // Weight per image: a cluster of n images counts n times, which the
      // heatmap's additive density makes identical to drawing each image.
      "heatmap-weight": ["*", 0.35, ["coalesce", ["get", "n"], 1]],
      "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 4, 6, 10, 20],
      "heatmap-color": [
        "interpolate",
        ["exponential", 0.05],
        ["heatmap-density"],
        0,
        withAlpha(color, 0),
        0.05,
        withAlpha(color, 0.4),
        0.5,
        withAlpha(color, 0.5),
      ],
      "heatmap-opacity": opacity,
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
    minzoom = DETAIL_CIRCLE_MIN_ZOOM,
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
      "circle-stroke-opacity": detailOpacityRamp(opacity),
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
      "icon-ignore-placement": true,
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
      "icon-ignore-placement": true,
      "icon-size": 1,
      "icon-rotate": ["to-number", ["get", "direction"]],
      "icon-rotation-alignment": "map",
      "icon-pitch-alignment": "map",
      visibility,
    },
    paint: { "icon-opacity": opacity },
  };
}
