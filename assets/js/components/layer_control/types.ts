import type { Map as MapLibreMap } from "maplibre-gl";

// Layer kinds from maps.models.MapLayer.TYPE_CHOICES.
export type MapLayerType = "pmtiles" | "xyz" | "style";

// Shape of window.MAP_LAYERS_DATA, serialized into templates/base.html by
// images/context_processors.py::_build_map_layers_data(), and the collection
// metadata fetched from its overlay_tiles as the viewport changes.
export interface PrimaryLayerData {
  slug: string;
  name: string;
  type: MapLayerType;
  url: string;
  is_default: boolean;
  attribution?: string;
}

export interface OverlayLayerData {
  id: number;
  name: string;
  type: MapLayerType;
  url: string;
  attribution?: string;
  description?: string;
}

export interface LayerCollectionData {
  id: number;
  name: string;
  layers: OverlayLayerData[];
}

export interface OverlayTilesData {
  url: string;
  maxzoom: number;
}

export interface MapLayersData {
  primary_layers?: PrimaryLayerData[];
  overlay_tiles?: OverlayTilesData;
}

export interface LayerControlOptions {
  // Show the "Georeferenced Images" toggle and its display-style controls
  showImageLayerToggle?: boolean;
  // Layer IDs the host page treats as overlays, so they stay above tile layers
  overlayLayerIds?: string[];
  // Insert overlay tile layers before this layer ID
  beforeLayerId?: string | null;
  onBaseLayerChange?: ((layerKey: string) => void) | null;
  // Called after a style swap, before the overlay tile layer is restored
  onStyleSwap?: ((map: MapLibreMap) => void | Promise<void>) | null;
}

// LayerControlOptions after defaults are applied in the constructor.
export interface ResolvedLayerControlOptions {
  showImageLayerToggle: boolean;
  overlayLayerIds: string[];
  beforeLayerId: string | null;
  onBaseLayerChange: ((layerKey: string) => void) | null;
  onStyleSwap: ((map: MapLibreMap) => void | Promise<void>) | null;
}

interface BaseLayerCommon {
  name: string;
  isDefault: boolean;
  // Adds whatever sources/layers the base layer needs; re-run after a style swap
  setupLayer: () => void;
  activate: () => void;
  deactivate: () => void;
}

export interface StyleBaseLayer extends BaseLayerCommon {
  type: "style";
  url: string;
}

export interface RasterBaseLayer extends BaseLayerCommon {
  type: "pmtiles" | "xyz";
  sourceId: string;
  layerId: string;
}

export type BaseLayer = StyleBaseLayer | RasterBaseLayer;

// A user-selectable tile overlay, held in memory so it can be re-added after a
// style swap destroys the map's sources and layers.
export interface OverlayLayerConfig {
  layer: OverlayLayerData;
  collection: Pick<LayerCollectionData, "id" | "name">;
  layerId: string;
  tileUrl: string;
  title: string;
  tileType: MapLayerType;
  attribution: string;
}

export type ImageDisplayStyle = "heatmap" | "simple";
