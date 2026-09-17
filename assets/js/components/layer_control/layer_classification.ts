// Decides which layers count as overlays (they must stay above user-selected
// tile layers) and where a newly added tile layer belongs in the stack.
import type { Map as MapLibreMap } from "maplibre-gl";
import { DISCOVERY_LAYER_IDS } from "./tile_overlays";
import { LAYER_IDS } from "../map_display/layer_ids";

// Overlay layers owned by other pages: the georeference interfaces (pins,
// context images — see pages/georeference_interface/context_images.ts) and
// the map/subject detail maps (polygons).
const PAGE_OVERLAY_LAYER_IDS = [
  "pin-circle",
  "pin-symbol",
  "context-image-directions",
  "context-image-circles",
  "polygon-fill",
  "polygon-outline",
] as const;

const ALWAYS_OVERLAY_LAYER_IDS: readonly string[] = [
  ...Object.values(LAYER_IDS),
  ...DISCOVERY_LAYER_IDS,
  ...PAGE_OVERLAY_LAYER_IDS,
];

// Dynamically named overlays: user-selected tile layers ("overlay-N") and
// Geoman's drawing layers ("gm_...").
const OVERLAY_LAYER_PREFIXES = ["overlay-", "gm_"];

export function isOverlayLayerId(
  layerId: string,
  configuredOverlayLayerIds: readonly string[],
): boolean {
  if (configuredOverlayLayerIds.includes(layerId)) return true;
  if (ALWAYS_OVERLAY_LAYER_IDS.includes(layerId)) return true;
  return OVERLAY_LAYER_PREFIXES.some((prefix) => layerId.startsWith(prefix));
}

// Fallback insertion points, ordered from the bottom of the overlay stack up.
// A tile overlay is inserted before the first of these that exists, so it ends
// up beneath every overlay on the page. The order must match the actual layer
// stacking:
// 1. subject hints (bottom of overlays)
// 2. location hints
// 3. context images (other georeferenced images)
// 4. pin layers (the user's placement marker — always on top)
// 5. image point layers
// 6. aerial georeference polygon (the only overlay present on aerial-only
//    image detail pages, where image points are disabled)
const BEFORE_LAYER_CANDIDATES: readonly string[] = [
  "subject-hints-pulse",
  "subject-hints-label",
  "location-hint-pulse",
  "location-hint-label",
  "context-image-directions",
  "context-image-circles",
  "pin-circle",
  "pin-symbol",
  LAYER_IDS.imageHeatmap,
  LAYER_IDS.imageCircles,
  LAYER_IDS.imageDirections,
  LAYER_IDS.imageCirclesSimple,
  LAYER_IDS.imageDirectionsSimple,
  LAYER_IDS.aerialPolygonFill,
  LAYER_IDS.aerialPolygonOutline,
];

/**
 * The layer ID to insert overlay tile layers before, so user-selected overlays
 * render below the page's own layers.
 */
export function findBeforeLayerId(
  map: MapLibreMap,
  configuredBeforeLayerId: string | null,
): string | undefined {
  if (configuredBeforeLayerId && map.getLayer(configuredBeforeLayerId)) {
    return configuredBeforeLayerId;
  }

  // Geoman layers are created dynamically after load and must stay on top
  const geomanLayer = map
    .getStyle()
    .layers.find((layer) => layer.id.startsWith("gm_"));
  if (geomanLayer) return geomanLayer.id;

  return BEFORE_LAYER_CANDIDATES.find((layerId) =>
    Boolean(map.getLayer(layerId)),
  );
}
