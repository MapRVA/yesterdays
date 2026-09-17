import type {
  Map as MapLibreMap,
  MapGeoJSONFeature,
  ErrorEvent,
} from "maplibre-gl";
import type { LayerCollectionData, OverlayLayerConfig, OverlayTilesData } from "./types";

export type OverlayLoadState = "loading" | "ready" | "error";

/** Keep the selection available even when the viewport no longer contains it. */
export function withActiveOverlay(
  collections: LayerCollectionData[],
  active: OverlayLayerConfig | null,
): LayerCollectionData[] {
  if (
    !active || collections.some((collection) =>
      collection.layers.some((layer) => layer.id === active.layer.id),
    )
  ) {
    return collections;
  }
  const result = collections.map((collection) => ({
    ...collection,
    layers: [...collection.layers],
  }));
  const collection = result.find((item) => item.id === active.collection.id);
  if (collection) collection.layers.push(active.layer);
  else result.push({ ...active.collection, layers: [active.layer] });
  return result;
}

// These are discovery layers, never user-selectable raster overlays. Keep them
// queryable when switching basemaps; opacity zero still permits feature queries.
const SOURCE_ID = "layer-discovery";
// The fill layer alone finds every polygon touching the viewport. The circle
// layer only exists for the points kept for polygons too small for the tile
// grid; unfiltered, it would also emit a circle at every polygon vertex.
export const DISCOVERY_LAYER_IDS = [
  "layer-discovery-fill", "layer-discovery-point",
] as const;

/** Deduplicate tile fragments/world copies and apply minzoom at camera zoom. */
export function collectionsFromFeatures(
  features: Pick<MapGeoJSONFeature, "id" | "properties">[],
  zoom: number,
): LayerCollectionData[] {
  const unique = new Map<number, (typeof features)[number]>();
  for (const feature of features) {
    if (typeof feature.id !== "number" || feature.properties.min_zoom > zoom) continue;
    unique.set(feature.id, feature);
  }
  const ordered = [...unique.values()].sort((a, b) => {
    const left = a.properties;
    const right = b.properties;
    return left.collection_order - right.collection_order
      || left.collection_name.localeCompare(right.collection_name)
      || left.collection_id - right.collection_id
      || left.layer_order - right.layer_order
      || left.name.localeCompare(right.name)
      || Number(a.id) - Number(b.id);
  });
  const collections = new Map<number, LayerCollectionData>();
  for (const { id, properties: p } of ordered) {
    let collection = collections.get(p.collection_id);
    if (!collection) {
      collection = { id: p.collection_id, name: p.collection_name, layers: [] };
      collections.set(collection.id, collection);
    }
    collection.layers.push({
      id: Number(id), name: p.name, type: p.type, url: p.url, attribution: p.attribution,
    });
  }
  return [...collections.values()];
}

/** MapLibre owns tile requests/cache/overzoom; viewport filtering stays local. */
export class TileOverlays {
  private failed = false;
  private lastResult = "";

  // Discovery runs once the camera settles and again whenever the map goes
  // idle, which covers finished tile loads, failures, and resizes.
  constructor(
    private readonly map: MapLibreMap,
    private readonly tiles: OverlayTilesData,
    private readonly onUpdate: (
      collections: LayerCollectionData[],
      state: OverlayLoadState,
    ) => void,
  ) {
    map.on("style.load", this.setup);
    map.on("moveend", this.refresh);
    map.on("idle", this.refresh);
    map.on("error", this.onError);
    this.setup();
  }

  private setup = (): void => {
    this.failed = false;
    if (!this.map.getSource(SOURCE_ID)) {
      this.map.addSource(SOURCE_ID, {
        type: "vector",
        tiles: [new URL(this.tiles.url, window.location.origin).href
          .replaceAll("%7B", "{").replaceAll("%7D", "}")],
        minzoom: 0,
        maxzoom: this.tiles.maxzoom,
      });
    }
    if (!this.map.getLayer(DISCOVERY_LAYER_IDS[0])) {
      const common = { source: SOURCE_ID, "source-layer": "layer_extents" };
      this.map.addLayer({
        ...common, id: DISCOVERY_LAYER_IDS[0], type: "fill",
        paint: { "fill-opacity": 0 },
      });
      this.map.addLayer({
        ...common, id: DISCOVERY_LAYER_IDS[1], type: "circle",
        filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-opacity": 0, "circle-radius": 1 },
      });
    }
  };

  private onError = (event: ErrorEvent & { sourceId?: string }): void => {
    // Registering any "error" listener disables MapLibre's default logging,
    // so log every error here, not just the ones this class handles.
    if (event.sourceId !== SOURCE_ID) {
      console.error(event.error);
      return;
    }
    console.error("Error fetching layer discovery tiles:", event.error);
    this.failed = true;
    // Failed requests don't always cause a render, so request one to make
    // the map reach idle and refresh the menu.
    this.map.triggerRepaint();
  };

  private refresh = (): void => {
    if (!this.map.getLayer(DISCOVERY_LAYER_IDS[0])) return;
    const collections = collectionsFromFeatures(
      this.map.queryRenderedFeatures({ layers: [...DISCOVERY_LAYER_IDS] }),
      this.map.getZoom(),
    );
    const state: OverlayLoadState = this.failed ? "error"
      : this.map.isSourceLoaded(SOURCE_ID) ? "ready" : "loading";
    const result = JSON.stringify([collections, state]);
    if (result !== this.lastResult) {
      this.lastResult = result;
      this.onUpdate(collections, state);
    }
  };

  destroy(): void {
    this.map.off("style.load", this.setup);
    this.map.off("moveend", this.refresh);
    this.map.off("idle", this.refresh);
    this.map.off("error", this.onError);
    for (const id of DISCOVERY_LAYER_IDS) {
      if (this.map.getLayer(id)) this.map.removeLayer(id);
    }
    if (this.map.getSource(SOURCE_ID)) this.map.removeSource(SOURCE_ID);
  }
}
