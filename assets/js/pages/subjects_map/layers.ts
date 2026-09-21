import type {
  ExpressionSpecification,
  FilterSpecification,
  Map as MapLibreMap,
} from "maplibre-gl";
import { DIRECTION_SPRITE_ID as DIRECTION_ICON } from "../../components/map_display/layer_ids";
import { DIRECTION_SPRITE_URL as DIRECTION_ICON_URL } from "../../components/map_display/direction_sprite";
import type { SubjectsMapUrls } from "./types";

// Subject geometry layers, ordered so the topmost queryable feature is also
// the visually topmost one: large polygons < small polygons < lines < points.
export const SUBJECT_LAYER_IDS = [
  "osm-elements-points",
  "osm-elements-lines",
  "osm-elements-polygons-small-fill",
  "osm-elements-polygons-small-stroke",
  "osm-elements-polygons-large-fill",
  "osm-elements-polygons-large-stroke",
];

export const IMAGE_CIRCLES_LAYER = "image-circles";
export const IMAGE_DIRECTIONS_LAYER = "image-directions";

const SUBJECT_COLOR = "#ff6b35";

// Polygons below this area (in square degrees) are drawn on top of, and more
// opaquely than, the larger ones, so a small building inside a park stays
// visible and clickable.
const SMALL_POLYGON_AREA = 3e-6;

const isPolygon: ExpressionSpecification = [
  "in",
  ["get", "geom_type"],
  ["literal", ["ST_Polygon", "ST_MultiPolygon"]],
];

const LARGE_POLYGONS: FilterSpecification = [
  "all",
  isPolygon,
  [">=", ["get", "geometry_area"], SMALL_POLYGON_AREA],
];

const SMALL_POLYGONS: FilterSpecification = [
  "all",
  isPolygon,
  ["<", ["get", "geometry_area"], SMALL_POLYGON_AREA],
];

// Adds every source and layer this page owns. Called on initial load and
// again after a theme-driven setStyle(), which wipes the style clean.
export function addCustomLayers(
  map: MapLibreMap,
  urls: SubjectsMapUrls,
  primaryColor: string,
): void {
  map.addSource("osm-elements", {
    type: "vector",
    tiles: [urls.osmElementTiles],
    minzoom: urls.osmElementMinZoom,
    maxzoom: 14,
    scheme: "xyz",
    attribution: "Subject geometries © OpenStreetMap Contributors",
  });

  map.addSource("images", {
    type: "vector",
    tiles: [urls.imageTiles],
    minzoom: 0,
    maxzoom: 14,
  });

  // Image points stay invisible until a subject is hovered or pinned, at
  // which point interactions.ts reveals just that subject's images.
  map.addLayer({
    id: IMAGE_CIRCLES_LAYER,
    type: "circle",
    source: "images",
    "source-layer": "image_points",
    paint: {
      "circle-radius": 8,
      "circle-color": primaryColor,
      "circle-stroke-color": "#fff",
      "circle-stroke-width": 2,
      "circle-opacity": 0,
      "circle-stroke-opacity": 0,
    },
  });

  void addDirectionLayer(map);

  // Large polygons (background)
  map.addLayer({
    id: "osm-elements-polygons-large-fill",
    type: "fill",
    source: "osm-elements",
    "source-layer": "osm_elements",
    filter: LARGE_POLYGONS,
    paint: {
      "fill-color": SUBJECT_COLOR,
      "fill-opacity": 0.3,
    },
  });

  map.addLayer({
    id: "osm-elements-polygons-large-stroke",
    type: "line",
    source: "osm-elements",
    "source-layer": "osm_elements",
    filter: LARGE_POLYGONS,
    paint: {
      "line-color": SUBJECT_COLOR,
      "line-width": 2,
      "line-opacity": 0.5,
    },
  });

  // Small polygons (on top)
  map.addLayer({
    id: "osm-elements-polygons-small-fill",
    type: "fill",
    source: "osm-elements",
    "source-layer": "osm_elements",
    filter: SMALL_POLYGONS,
    paint: {
      "fill-color": SUBJECT_COLOR,
      "fill-opacity": 0.6,
    },
  });

  map.addLayer({
    id: "osm-elements-polygons-small-stroke",
    type: "line",
    source: "osm-elements",
    "source-layer": "osm_elements",
    filter: SMALL_POLYGONS,
    paint: {
      "line-color": SUBJECT_COLOR,
      "line-width": 2,
      "line-opacity": 0.8,
    },
  });

  // Lines (above polygons)
  map.addLayer({
    id: "osm-elements-lines",
    type: "line",
    source: "osm-elements",
    "source-layer": "osm_elements",
    filter: [
      "in",
      ["get", "geom_type"],
      ["literal", ["ST_LineString", "ST_MultiLineString"]],
    ],
    paint: {
      "line-color": SUBJECT_COLOR,
      "line-width": 4,
      "line-opacity": 0.7,
    },
  });

  // Points (on top of everything)
  map.addLayer({
    id: "osm-elements-points",
    type: "circle",
    source: "osm-elements",
    "source-layer": "osm_elements",
    filter: ["==", ["get", "geom_type"], "ST_Point"],
    paint: {
      "circle-radius": 6,
      "circle-color": SUBJECT_COLOR,
      "circle-opacity": 0.7,
      "circle-stroke-width": 2,
      "circle-stroke-color": "#fff",
    },
  });
}

// The direction arrows need an image loaded over the network, so they land
// after the rest. A theme swap can retire the style mid-flight, hence the
// guards before touching the map again.
async function addDirectionLayer(map: MapLibreMap): Promise<void> {
  try {
    const image = await map.loadImage(DIRECTION_ICON_URL);
    if (!map.getSource("images") || map.getLayer(IMAGE_DIRECTIONS_LAYER)) return;
    if (!map.hasImage(DIRECTION_ICON)) map.addImage(DIRECTION_ICON, image.data);

    map.addLayer(
      {
        id: IMAGE_DIRECTIONS_LAYER,
        type: "symbol",
        source: "images",
        "source-layer": "image_points",
        filter: ["has", "direction"],
        layout: {
          "icon-image": DIRECTION_ICON,
          "icon-overlap": "always",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 5, 0.3, 15, 1],
          "icon-rotate": ["to-number", ["get", "direction"]],
          "icon-rotation-alignment": "map",
          "icon-pitch-alignment": "map",
        },
        paint: {
          "icon-opacity": 0,
        },
      },
      map.getLayer(IMAGE_CIRCLES_LAYER) ? IMAGE_CIRCLES_LAYER : undefined,
    );
  } catch (error) {
    console.warn("Could not load direction arrow image:", error);
  }
}
