// Display modes for the already-georeferenced context images: hidden, ghost
// (visible but de-emphasized and inert), or clickable (popups on click,
// pointer cursor). The layers share their look with the sitewide maps via
// map_display/image_point_layers, but live under their own ids so
// LayerControl's image-panel state never touches them.
import maplibregl from "maplibre-gl";
import type { ExpressionSpecification, MapLayerMouseEvent } from "maplibre-gl";
import {
  buildPopupWrapper,
  deduplicateFeatures,
} from "../../components/map_popup";
import {
  primaryColor,
  secondaryColor,
} from "../../components/map_display/colors";
import {
  detailColorRamp,
  detailOpacityRamp,
} from "../../components/map_display/image_point_layers";
import type { TimeSliderTarget } from "../../components/map_display/time_slider_control";
import type {
  ContextDisplayMode,
  ContextImagesController,
  CurrentImageConfig,
  GeoreferenceContext,
} from "./types";

// Stacking order: directions (bottom), circles (top). Keep map_layers.ts
// OVERLAY_LAYER_IDS and the shared layer_control/layer_classification.ts
// lists in sync with these. The sitewide maps' density heatmap layer has no
// counterpart here: context images only appear from CONTEXT_MIN_ZOOM, where
// individual points are legible without a density overview.
export const CONTEXT_LAYER_IDS = {
  directions: "context-image-directions",
  circles: "context-image-circles",
} as const;

// Context images stay hidden until you are zoomed in far enough to be placing
// a pin, so the map reads clean while you pan around looking for the spot.
export const CONTEXT_MIN_ZOOM = 16;

const GHOST_OPACITY = 0.6;

export interface ContextModeAppearance {
  color: string;
  opacity: number;
  visible: boolean;
}

export function contextModeAppearance(
  mode: ContextDisplayMode,
): ContextModeAppearance {
  switch (mode) {
    case "hidden":
      return { color: secondaryColor, opacity: GHOST_OPACITY, visible: false };
    case "ghost":
      return { color: secondaryColor, opacity: GHOST_OPACITY, visible: true };
    case "clickable":
      return { color: primaryColor, opacity: 1, visible: true };
  }
}

// When correcting an existing georeference, the current image is already in
// the tiles; its old point would duplicate the location hint, so exclude it.
export function contextImageExtraFilter(
  image: CurrentImageConfig,
): ExpressionSpecification | null {
  return image.isGeoreferenced ? ["!=", ["get", "id"], image.id] : null;
}

// The context layers the date slider filters, preserving each layer's base
// filter.
export function contextTimeSliderTargets(
  image: CurrentImageConfig,
): TimeSliderTarget[] {
  const extraFilter = contextImageExtraFilter(image);
  return [
    { layerId: CONTEXT_LAYER_IDS.circles, extraFilter },
    {
      layerId: CONTEXT_LAYER_IDS.directions,
      requiresDirection: true,
      extraFilter,
    },
  ];
}

export function createContextImagesController(
  ctx: GeoreferenceContext,
): ContextImagesController {
  const { map, state } = ctx;

  // Pin placement is suppressed separately: the map click handler in index.ts
  // queries the circles layer at the click point before placing the pin.
  const onClick = (e: MapLayerMouseEvent): void => {
    // Only show popup if in clickable mode
    if (state.contextDisplayMode !== "clickable") return;

    const features = deduplicateFeatures(e.features ?? []);
    if (features.length === 0) return;

    // Close any existing popup
    if (state.activePopup) {
      state.activePopup.remove();
    }

    // Create and track new popup
    const popup = new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setDOMContent(buildPopupWrapper(features))
      .addTo(map);
    state.activePopup = popup;

    // Clear the popup reference when it's closed
    popup.on("close", () => {
      state.activePopup = null;
      // Reset cursor to crosshairs when popup closes
      if (!state.isHoveringContextImage) {
        map.getCanvas().style.cursor = "crosshair";
      }
    });
  };

  const onMouseEnter = (): void => {
    if (state.contextDisplayMode === "clickable") {
      map.getCanvas().style.cursor = "pointer";
      state.isHoveringContextImage = true;
    }
  };

  const onMouseLeave = (): void => {
    if (state.contextDisplayMode === "clickable") {
      map.getCanvas().style.cursor = "crosshair";
      state.isHoveringContextImage = false;
    }
  };

  function detachHandlers(): void {
    map.off("click", CONTEXT_LAYER_IDS.circles, onClick);
    map.off("mouseenter", CONTEXT_LAYER_IDS.circles, onMouseEnter);
    map.off("mouseleave", CONTEXT_LAYER_IDS.circles, onMouseLeave);
  }

  function closeActivePopup(): void {
    if (state.activePopup) {
      state.activePopup.remove();
      state.activePopup = null;
    }
  }

  function updateDisplay(): void {
    if (!map.getLayer(CONTEXT_LAYER_IDS.circles)) return;

    const appearance = contextModeAppearance(state.contextDisplayMode);
    const visibility = appearance.visible ? "visible" : "none";
    for (const layerId of Object.values(CONTEXT_LAYER_IDS)) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", visibility);
      }
    }

    // The date slider only filters these layers, so hide it with them
    ctx.timeSlider?.setVisible(appearance.visible);

    // Re-apply the mode-dependent paint ramps (the zoom-graduated blur and
    // radius ramps are mode-independent and set at layer creation)
    map.setPaintProperty(
      CONTEXT_LAYER_IDS.circles,
      "circle-color",
      detailColorRamp(appearance.color),
    );
    map.setPaintProperty(
      CONTEXT_LAYER_IDS.circles,
      "circle-opacity",
      detailOpacityRamp(appearance.opacity),
    );
    map.setPaintProperty(
      CONTEXT_LAYER_IDS.circles,
      "circle-stroke-opacity",
      appearance.opacity,
    );
    if (map.getLayer(CONTEXT_LAYER_IDS.directions)) {
      map.setPaintProperty(
        CONTEXT_LAYER_IDS.directions,
        "icon-opacity",
        appearance.opacity,
      );
    }

    // Detach first so re-entering clickable mode never stacks handlers
    detachHandlers();
    if (state.contextDisplayMode === "clickable") {
      map.on("click", CONTEXT_LAYER_IDS.circles, onClick);
      map.on("mouseenter", CONTEXT_LAYER_IDS.circles, onMouseEnter);
      map.on("mouseleave", CONTEXT_LAYER_IDS.circles, onMouseLeave);
    } else {
      closeActivePopup();
      state.isHoveringContextImage = false;
      map.getCanvas().style.cursor = "crosshair";
    }
  }

  return { updateDisplay };
}

// Wire the display-mode radios and read the initial mode from the checked one
// (pre-checked by the template from the user's saved preference).
export function initContextDisplayControls(
  ctx: GeoreferenceContext,
  controller: ContextImagesController,
): void {
  const { state } = ctx;
  const contextDisplayRadios = document.querySelectorAll<HTMLInputElement>(
    'input[name="contextDisplay"]',
  );

  const checkedRadio = document.querySelector<HTMLInputElement>(
    'input[name="contextDisplay"]:checked',
  );
  if (checkedRadio) {
    state.contextDisplayMode = checkedRadio.value as ContextDisplayMode;
  }

  contextDisplayRadios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) {
        state.contextDisplayMode = radio.value as ContextDisplayMode;
        controller.updateDisplay();
      }
    });
  });
}
