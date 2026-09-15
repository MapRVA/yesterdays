import type { Map as MapLibreMap } from "maplibre-gl";
import type {
  MapLayersData,
  MapLayerType,
} from "../js/components/layer_control/types";
import type { MapDisplayConfig } from "../js/components/map_display/types";
import type { MapBounds } from "../js/constants/map";
import type { GeoreferenceConfig } from "../js/pages/georeference_interface/types";
import type { SubjectBrowserConfig } from "../js/pages/browse_subjects";

declare global {
  // Minimal shape of the Bootstrap bundle assets/index.js puts on window. The
  // npm package ships no type declarations, so only the pieces TypeScript
  // modules actually use are described here.
  interface BootstrapOffcanvas {
    show(): void;
    hide(): void;
    dispose(): void;
  }

  interface BootstrapModal {
    show(): void;
    hide(): void;
  }

  interface BootstrapNamespace {
    Offcanvas: new (
      element: Element,
      options?: Record<string, unknown>,
    ) => BootstrapOffcanvas;
    Modal: {
      new (
        element: Element,
        options?: Record<string, unknown>,
      ): BootstrapModal;
      getInstance(element: Element): BootstrapModal | null;
    };
  }

  // The single layer previewed on the map layer detail page, serialized by
  // templates/maps/map_detail.html from a maps.models.MapLayer.
  interface PreviewMapLayer {
    name: string;
    type: MapLayerType;
    url: string;
    attribution: string;
  }

  // Initial state for the staff map layer edit page, serialized by
  // templates/maps/layer_form.html.
  interface LayerEditConfig {
    protomapsApiKey: string;
    name: string;
    type: MapLayerType;
    url: string;
    attribution: string;
    collection: string;
  }

  interface Window {
    // Django-provided configuration, set by inline scripts in templates/base.html
    DEFAULT_MAP_CENTER?: [number, number];
    DEFAULT_MAP_ZOOM?: number;
    REGION_MAP_CENTER?: [number, number] | null;
    REGION_MAP_BOUNDS?: MapBounds | null;

    // Geocoder configuration: the search bounding box is
    // [west, south, east, north]; ADMIN_EMAIL is sent to Nominatim as a
    // contact address and is null when unset.
    DEFAULT_SEARCH_BBOX?: [number, number, number, number];
    SEARCH_BBOX?: MapBounds;
    ADMIN_EMAIL?: string | null;

    MAP_LAYERS_DATA?: MapLayersData;
    MAP_LAYER?: PreviewMapLayer;
    layerEditConfig?: LayerEditConfig;

    // Django-serialized configuration for the point georeference interface,
    // set inline by templates/images/georeference_interface.html
    georeferenceConfig?: GeoreferenceConfig;

    // Django-serialized configuration for the subject browser, set inline by
    // templates/subjects/browse_subjects.html.
    subjectBrowserConfig?: SubjectBrowserConfig;

    // Protomaps basemap key, set by pages that build their own basemap style
    PROTOMAPS_API_KEY?: string;

    // Alpine.js, assigned globally in assets/index.js
    Alpine: typeof import("alpinejs").default;

    bootstrap: BootstrapNamespace;

    // Toast-style alert helper, defined in js/components/notifications.js and
    // loaded sitewide by assets/index.js
    showAlert: (
      type: "success" | "danger" | "warning" | "info" | "primary" | "secondary",
      message: string,
      duration?: number,
    ) => void;

    // PMTiles protocol registration, shared with still-JS page bundles
    pmtilesProtocolSetup: boolean;
    setupPMTilesProtocol: () => boolean;

    // Map viewer entry points called from Django templates and page bundles
    initializeMap: (config: MapDisplayConfig) => MapLibreMap;
    toggleOtherImages?: (showAll: boolean, onLoadCallback?: () => void) => void;
  }
}

export {};
