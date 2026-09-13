// Drawing a single maps.models.MapLayer over the Protomaps basemap. Shared by
// the public layer detail page (pages/map_detail.ts) and the staff edit page
// (pages/layer_edit.ts).
import type { Map as MapLibreMap, StyleSpecification } from "maplibre-gl";
import { protomapsStyleUrl } from "./basemap";

export const OVERLAY_ID = "overlay-layer";

// Draw the previewed layer on top of the basemap. Only tile layers get here —
// a "style" layer is the map's whole style and is handled at construction.
export function addOverlayLayer(map: MapLibreMap, layer: PreviewMapLayer): void {
  if (map.getSource(OVERLAY_ID)) return;

  const tiles =
    layer.type === "pmtiles"
      ? [`pmtiles://${layer.url}/{z}/{x}/{y}`]
      : [layer.url];

  map.addSource(OVERLAY_ID, {
    type: "raster",
    tiles,
    tileSize: 256,
    attribution: layer.attribution,
  });
  map.addLayer({ id: OVERLAY_ID, type: "raster", source: OVERLAY_ID });
}

export function removeOverlayLayer(map: MapLibreMap): void {
  if (map.getLayer(OVERLAY_ID)) map.removeLayer(OVERLAY_ID);
  if (map.getSource(OVERLAY_ID)) map.removeSource(OVERLAY_ID);
}

// A colour-scheme flip means a different Protomaps style. setStyle() would
// discard the preview layer along with the old basemap, so fetch the new style
// and copy its paint properties onto the layers already on the map instead.
export async function retintBaseMap(map: MapLibreMap): Promise<void> {
  try {
    const response = await fetch(protomapsStyleUrl());
    const style: StyleSpecification = await response.json();

    for (const layer of style.layers) {
      if (!map.getLayer(layer.id)) continue;
      const paint = ("paint" in layer ? layer.paint : undefined) as
        | Record<string, unknown>
        | undefined;
      if (!paint) continue;

      for (const [property, value] of Object.entries(paint)) {
        try {
          map.setPaintProperty(layer.id, property, value);
        } catch {
          // Not every paint property can be re-set on a live layer.
        }
      }
    }
  } catch (e) {
    console.warn("Could not update base map theme:", e);
  }
}
