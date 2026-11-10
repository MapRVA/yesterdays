// Entry point for the point georeference interface
// (templates/images/georeference_interface.html). Handles map interaction,
// georeferencing functionality, and UI interactions.
import "../../../styles/pages/georeference-interface.css";
import "../../../styles/components/georeference-joystick.css";
import "../../../styles/components/map-display.css";
import "../../../styles/components/image-viewer.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap, MapOptions } from "maplibre-gl";

import "../../components/map_display/pmtiles_protocol";
import {
  initialMapStyle,
  LayerControl,
} from "../../components/layer_control";
import { initImageViewer } from "../../components/image_viewer";
import {
  computeYearRange,
  watchFirstSourceFeatures,
} from "../../components/map_display/features";
import {
  insertTimeSlider,
  TimeSliderControl,
} from "../../components/map_display/time_slider_control";
import { addResponsiveGeocoder } from "../../components/responsive_geocoder";
import { initialMapView } from "../../constants/map";
import { initSubjectEditor } from "../../components/subject_editor.js";
import {
  CONTEXT_LAYER_IDS,
  contextTimeSliderTargets,
  createContextImagesController,
  initContextDisplayControls,
} from "./context_images";
import { ensureCsrfInput } from "./csrf";
import { initializeDifficultyToggles } from "./difficulty_filter";
import { initDifficultyMarking } from "./difficulty_marking";
import { initConfidenceRadios } from "./form";
import { pinFeatureCollection } from "./geojson";
import {
  addMapSourcesAndLayers,
  OVERLAY_LAYER_IDS,
  restoreOverlayState,
} from "./map_layers";
import { initOsdCenterline } from "./osd_centerline";
import {
  initBearingLineToggle,
  initCoordinateInputs,
  initJoystick,
  setJoystickFromDirection,
  updateCoordinates,
  updateDirection,
} from "./pin_direction";
import {
  initSkipButton,
  initSubmitFlow,
  initWillNotGeoreference,
} from "./submission";
import type {
  CurrentImageConfig,
  GeoreferenceConfig,
  GeoreferenceContext,
  GeoreferenceState,
  ImagePageUrls,
} from "./types";

function updateMapSwapLink(map: MapLibreMap): void {
  const mapswapLink = document.getElementById(
    "mapswap-link",
  ) as HTMLAnchorElement | null;
  if (!mapswapLink) return;

  const center = map.getCenter();
  const zoom = map.getZoom().toFixed(2);
  const lat = center.lat.toFixed(6);
  const lng = center.lng.toFixed(6);
  mapswapLink.href = `https://mapswap.trailsta.sh/swap/#type=m&url=geo:${lat},${lng};z=${zoom}`;
}

/**
 * Initialize the main georeference interface
 */
function initializeGeoreferenceInterface(
  config: GeoreferenceConfig,
  image: CurrentImageConfig,
): void {
  // The per-image URLs are always serialized when a current image is; narrow
  // them once so the rest of the page can rely on them.
  const { georeferenceImage, skipImage, markDifficulty, markWillNotGeoref, imageDetail } =
    config.urls;
  if (
    !georeferenceImage ||
    !skipImage ||
    !markDifficulty ||
    !markWillNotGeoref ||
    !imageDetail
  ) {
    console.error("Georeference URLs missing from configuration");
    return;
  }
  const urls: ImagePageUrls = {
    georeferenceImage,
    skipImage,
    markDifficulty,
    markWillNotGeoref,
    imageDetail,
    vectorTiles: config.urls.vectorTiles,
  };

  // Get DOM elements
  const submitButton = document.getElementById(
    "submitButton",
  ) as HTMLButtonElement | null;
  const latitudeInput = document.getElementById(
    "latitude-input",
  ) as HTMLInputElement | null;
  const longitudeInput = document.getElementById(
    "longitude-input",
  ) as HTMLInputElement | null;
  const directionInput = document.getElementById(
    "direction-input",
  ) as HTMLInputElement | null;
  const joystickContainer = document.getElementById("joystick-container");
  const joystickHandle = document.getElementById("joystick-handle");
  const confidenceNotes = document.getElementById(
    "confidence-notes",
  ) as HTMLTextAreaElement | null;

  if (
    !submitButton ||
    !latitudeInput ||
    !longitudeInput ||
    !directionInput ||
    !joystickContainer ||
    !joystickHandle ||
    !confidenceNotes
  ) {
    console.error("Georeference interface elements not found in DOM");
    return;
  }

  const notesRequiredIndicator = document.getElementById(
    "notes-required-indicator",
  );
  const notesHelpText = document.getElementById("notes-help-text");
  const confidenceHighRadio = document.getElementById(
    "confidence-high",
  ) as HTMLInputElement | null;
  if (!notesRequiredIndicator || !notesHelpText || !confidenceHighRadio) {
    console.error("Confidence elements not found in DOM");
  }

  window.setupPMTilesProtocol();

  // Determine initial map position based on all hints
  const allHintCoords: [number, number][] = [];
  if (config.locationHint) {
    allHintCoords.push([config.locationHint.lng, config.locationHint.lat]);
  }
  (config.subjectHints || []).forEach((hint) => {
    allHintCoords.push([hint.lng, hint.lat]);
  });

  const mapOptions: MapOptions = {
    container: "mymap",
    style: initialMapStyle(),
  };

  if (allHintCoords.length > 1) {
    // Multiple hints: fit bounds to show them all
    const bounds = new maplibregl.LngLatBounds();
    allHintCoords.forEach((coord) => bounds.extend(coord));
    mapOptions.bounds = bounds;
    mapOptions.fitBoundsOptions = { padding: 100, maxZoom: 17 };
  } else if (allHintCoords[0]) {
    // Single hint: center on it
    mapOptions.center = allHintCoords[0];
    mapOptions.zoom = 17;
  } else {
    // No hints: the selected region, else the whole world
    Object.assign(mapOptions, initialMapView());
  }

  const map = new maplibregl.Map(mapOptions);

  const state: GeoreferenceState = {
    pinPlaced: false,
    currentDirection: null,
    isJoystickDragging: false,
    bearingLineEnabled: false,
    osdCenterlineOverlay: null,
    contextDisplayMode: "ghost", // Default to ghost mode
    isHoveringContextImage: false,
    activePopup: null,
  };

  const ctx: GeoreferenceContext = {
    map,
    config,
    image,
    urls,
    els: {
      submitButton,
      latitudeInput,
      longitudeInput,
      directionInput,
      joystickContainer,
      joystickHandle,
      confidenceNotes,
      confidenceRadios: Array.from(
        document.querySelectorAll<HTMLInputElement>('input[name="confidence"]'),
      ),
      notesRequiredIndicator,
      notesHelpText,
      confidenceHighRadio,
    },
    state,
  };
  ctx.contextImages = createContextImagesController(ctx);

  // Note: overlayLayerIds lists all layers that should stay on top of
  // secondary tile layers (like Sanborn maps), so the LayerControl inserts
  // those below the bottommost overlay layer.
  map.addControl(
    new LayerControl({
      overlayLayerIds: OVERLAY_LAYER_IDS,
      onStyleSwap: async () => {
        await addMapSourcesAndLayers(ctx);
        restoreOverlayState(ctx);
      },
    }),
    "top-right",
  );
  map.addControl(new maplibregl.NavigationControl());
  map.addControl(new maplibregl.FullscreenControl());

  map.getCanvas().style.cursor = "crosshair";

  map.on("load", async () => {
    await addMapSourcesAndLayers(ctx);

    // Add address search control powered by Nominatim
    addResponsiveGeocoder(map);

    // Initialize MapSwap link and update on map move
    updateMapSwapLink(map);
    map.on("moveend", () => updateMapSwapLink(map));

    restoreOverlayState(ctx);

    // Add the date slider once the first context images load and reveal a
    // filterable date range (mirrors the sitewide maps)
    watchFirstSourceFeatures(
      map,
      {
        sourceId: "context-images",
        sourceLayer: "image_points",
        fallbackLayerId: CONTEXT_LAYER_IDS.circles,
      },
      (features) => {
        const range = computeYearRange(features);
        if (!range || range.minYear >= range.maxYear) return;
        const timeSlider = new TimeSliderControl(
          range.minYear,
          range.maxYear,
          map.getContainer().id,
          contextTimeSliderTargets(image),
        );
        insertTimeSlider(map, timeSlider);
        // The slider may arrive while context images are hidden
        timeSlider.setVisible(state.contextDisplayMode !== "hidden");
        ctx.timeSlider = timeSlider;
      },
    );
  });

  map.on("click", (e) => {
    // If there's an open popup, close it and don't place pin
    if (state.activePopup) {
      state.activePopup.remove();
      state.activePopup = null;
      return;
    }

    // Don't place pin if clicking on a context image in clickable mode (the
    // layer's own click handler opens a popup instead). Queried rather than
    // tracked via hover state so it also works on touch devices.
    if (
      state.contextDisplayMode === "clickable" &&
      map.getLayer(CONTEXT_LAYER_IDS.circles) &&
      map.queryRenderedFeatures(e.point, {
        layers: [CONTEXT_LAYER_IDS.circles],
      }).length > 0
    ) {
      return;
    }

    const { lng, lat } = e.lngLat;
    map
      .getSource<GeoJSONSource>("pin")
      ?.setData(pinFeatureCollection(lng, lat, null));
    state.pinPlaced = true;
    updateCoordinates(ctx, lng, lat);

    // Maintain current direction when placing new pin
    if (state.currentDirection !== null) {
      updateDirection(ctx, state.currentDirection);
      setJoystickFromDirection(ctx, state.currentDirection);
    }
  });

  initJoystick(ctx);
  initCoordinateInputs(ctx);
  initConfidenceRadios(ctx);
  initSubmitFlow(ctx);
  initSkipButton(ctx);
  initDifficultyMarking(ctx);
  initWillNotGeoreference(ctx);
  initBearingLineToggle(ctx);
  initOsdCenterline(ctx);
  initContextDisplayControls(ctx, ctx.contextImages);
  ensureCsrfInput(config);
}

document.addEventListener("DOMContentLoaded", () => {
  const config = window.georeferenceConfig;
  if (!config) {
    console.error("Georeference configuration not found");
    return;
  }

  // Initialize difficulty filter toggles if they exist in the DOM
  // (shown when not viewing a specific image via URL parameter)
  if (document.getElementById("difficulty-filter-group")) {
    initializeDifficultyToggles(config);
  }

  initImageViewer();

  // Initialize main functionality if we have a current image
  if (config.currentImage) {
    initializeGeoreferenceInterface(config, config.currentImage);
  }

  initSubjectEditor();
});
