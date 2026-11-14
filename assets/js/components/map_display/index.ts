// Shared map viewer component. Templates include
// templates/images/partials/map_display.html, which loads this bundle and
// calls window.initializeMap() with a MapDisplayConfig.
import maplibregl from "maplibre-gl";
import { initialMapView } from "../../constants/map";
import "maplibre-gl/dist/maplibre-gl.css";
import "@maplibre/maplibre-gl-geocoder/dist/maplibre-gl-geocoder.css";
import "../../../styles/components/map-display.css";
import "./pmtiles_protocol";
import { initialMapStyle, LayerControl } from "../layer_control";
import { addResponsiveGeocoder } from "../responsive_geocoder";
import { addCornerAttribution, mountExternalAttribution } from "./attribution";
import { boundsFromAerialGeometry } from "./bounds";
import { primaryColor, dangerColor } from "./colors";
import { setupMapDataLayers } from "./data_layers";
import { watchInitialFeatures } from "./features";
import { registerImageInteractions } from "./interactions";
import { LAYER_IDS, OVERLAY_LAYER_IDS } from "./layer_ids";
import { installToggleOtherImages } from "./other_images_toggle";
import { buildScaleHelpers } from "./scale_visibility";
import {
  DEFAULT_HISTOGRAM_SOURCE,
  DEFAULT_TIME_SLIDER_TARGETS,
  POINTS_HISTOGRAM_SOURCE,
  TimeSliderControl,
} from "./time_slider_control";
import type { ImageDisplayStyle } from "../layer_control/types";

// The histogram counts the source behind the visible image layers: the
// clustered one for the heatmap style, the per-image one for the simple style.
function histogramSourceFor(style: ImageDisplayStyle) {
  return style === "simple" ? POINTS_HISTOGRAM_SOURCE : DEFAULT_HISTOGRAM_SOURCE;
}
import type {
  MapDisplayConfig,
  MapDisplayContext,
  ResolvedMapDisplayConfig,
} from "./types";

// Main initialization function
export function initializeMap(config: MapDisplayConfig): maplibregl.Map {
  const {
    mapId,
    // Left undefined, initialMapView() picks the selected region's view or
    // the whole world. The template resolves all three itself.
    center,
    zoom,
    bounds,
    hash = false,
    vectorTilesUrl = null,
    allImagesUrl = null,
    showOtherImages = false,
    imageId = null,
    enableScaleVisibility = false,
    includeGeocoder = false,
    unboundedSearch = false,
    geolocate = false,
    aerialGeoreference = null,
    zoomToContents = false,
    directionImageUrl = null,
    currentImageDirection = null,
    attributionContainerId = null,
    timeSliderCollapsed = true,
  } = config;

  const resolved: ResolvedMapDisplayConfig = {
    mapId,
    center,
    zoom,
    bounds,
    hash,
    vectorTilesUrl,
    allImagesUrl,
    showOtherImages,
    imageId,
    enableScaleVisibility,
    includeGeocoder,
    unboundedSearch,
    geolocate,
    aerialGeoreference,
    zoomToContents,
    directionImageUrl,
    currentImageDirection,
    attributionContainerId,
    timeSliderCollapsed,
  };

  // Setup PMTiles protocol
  window.setupPMTilesProtocol();

  // Initialize the map
  const map = new maplibregl.Map({
    container: mapId,
    style: initialMapStyle(),
    hash: hash,
    // Added below: outside the map when the page provides a place for it
    attributionControl: false,
    ...initialMapView({ center, zoom, bounds }),
  });

  // Setup toggle for other images if enabled
  if (showOtherImages && imageId) {
    installToggleOtherImages(map);
  }

  const ctx: MapDisplayContext = {
    map,
    config: resolved,
    colors: { primary: primaryColor, danger: dangerColor },
    scaleHelpers: buildScaleHelpers(enableScaleVisibility),
  };

  // Add controls
  const layerControl = new LayerControl({
    showImageLayerToggle: true,
    overlayLayerIds: OVERLAY_LAYER_IDS,
    beforeLayerId: LAYER_IDS.imageHeatmap,
    onStyleSwap: async () => {
      await setupMapDataLayers(ctx);
      // Recreated layers come back with their base filters; re-apply the
      // slider's date range
      ctx.timeSlider?.applyFilter();
    },
    // The date slider only filters the image layers, so it goes with them
    // (keeping its range for when they come back)
    onImageLayersToggle: (visible) => ctx.timeSlider?.setVisible(visible),
    onImageStyleChange: (style) =>
      ctx.timeSlider?.setHistogramSource(histogramSourceFor(style)),
  });
  ctx.layerControl = layerControl;
  const navControl = new maplibregl.NavigationControl();
  const fullscreenControl = new maplibregl.FullscreenControl();

  map.addControl(layerControl, "top-right");

  map.addControl(navControl, "top-right");
  map.addControl(fullscreenControl, "top-right");

  if (geolocate) {
    const geolocateControl = new maplibregl.GeolocateControl({
      positionOptions: { enableHighAccuracy: true },
      trackUserLocation: true,
    });
    map.addControl(geolocateControl, "top-right");
  }

  const attributionContainer = attributionContainerId
    ? document.getElementById(attributionContainerId)
    : null;
  if (attributionContainer) {
    mountExternalAttribution(map, attributionContainer, fullscreenControl);
  } else {
    addCornerAttribution(map);
  }

  // Map load handler
  map.on("load", async function () {
    // Add geocoder if requested
    if (includeGeocoder) {
      addResponsiveGeocoder(map, unboundedSearch ? { searchBbox: null } : {});
    }

    await setupMapDataLayers(ctx);

    // Fit bounds to aerial polygon (only on initial load). Only include
    // `center` in the bounds when it marks the current image point
    // (point+polygon case). In the polygon-only case there is no
    // current-image marker and `center` is just the region centerpoint (or
    // 0,0 with no region), so extending to it would drag the view off the
    // polygon.
    if (aerialGeoreference) {
      try {
        const bounds = boundsFromAerialGeometry(
          aerialGeoreference,
          showOtherImages && imageId ? center : null,
        );
        if (bounds) {
          map.fitBounds(bounds, {
            padding: 50,
            maxZoom: 16,
          });
        }
      } catch (error) {
        console.error("Error fitting bounds to aerial polygon:", error);
      }
    }

    // Date-range filter over a fixed span (not on single-image maps)
    if (!imageId) {
      const timeSlider = new TimeSliderControl(
        mapId,
        DEFAULT_TIME_SLIDER_TARGETS,
        histogramSourceFor(layerControl.displayStyle),
        { collapsed: timeSliderCollapsed },
      );
      map.addControl(timeSlider, "bottom-right");
      ctx.timeSlider = timeSlider;
    }

    watchInitialFeatures(ctx);

    registerImageInteractions(ctx);
  });

  return map;
}

// Expose initializeMap globally for template usage
window.initializeMap = initializeMap;
