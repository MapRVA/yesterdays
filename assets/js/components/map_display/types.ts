import type { Map as MapLibreMap } from "maplibre-gl";
import type { MultiPolygon, Polygon } from "geojson";
import type { MapBounds } from "../../constants/map";
import type { LayerControl } from "../layer_control";
import type { TimeSliderControl } from "./time_slider_control";

// Zoom thresholds for a map scale (1-5): pinpointZoom is where points become
// visible, fullDetailZoom is where they become interactive.
export interface ScaleVisibilityZooms {
  pinpointZoom: number;
  fullDetailZoom: number;
}

export type ScaleVisibilityConfig = Record<number, ScaleVisibilityZooms>;

export interface ScaleHelpers {
  isFullDetail(scale: number, zoom: number): boolean;
}

// Config object built by templates/images/partials/map_display.html and passed
// to window.initializeMap().
export interface MapDisplayConfig {
  mapId: string;
  center?: [number, number];
  zoom?: number;
  bounds?: MapBounds | null;
  hash?: boolean;
  vectorTilesUrl?: string | null;
  allImagesUrl?: string | null;
  showOtherImages?: boolean;
  imageId?: string | null;
  enableScaleVisibility?: boolean;
  includeGeocoder?: boolean;
  // Search the whole world instead of the region/sitewide search box
  unboundedSearch?: boolean;
  geolocate?: boolean;
  aerialGeoreference?: Polygon | MultiPolygon | null;
  zoomToContents?: boolean;
  directionImageUrl?: string;
  currentImageDirection?: number | null;
}

// MapDisplayConfig after defaults are applied in initializeMap().
export interface ResolvedMapDisplayConfig {
  mapId: string;
  center: [number, number];
  zoom: number;
  bounds: MapBounds | null;
  hash: boolean;
  vectorTilesUrl: string | null;
  allImagesUrl: string | null;
  showOtherImages: boolean;
  imageId: string | null;
  enableScaleVisibility: boolean;
  includeGeocoder: boolean;
  unboundedSearch: boolean;
  geolocate: boolean;
  aerialGeoreference: Polygon | MultiPolygon | null;
  zoomToContents: boolean;
  directionImageUrl: string | null;
  currentImageDirection: number | null;
}

// Shared state threaded through the map_display modules for one map instance.
// layerControl is assigned right after construction (its onStyleSwap callback
// closes over the context, so the context must exist first); timeSlider is
// assigned once the initial features reveal a filterable date range.
export interface MapDisplayContext {
  map: MapLibreMap;
  config: ResolvedMapDisplayConfig;
  colors: { primary: string; danger: string };
  scaleHelpers: ScaleHelpers;
  layerControl?: LayerControl;
  timeSlider?: TimeSliderControl;
}
