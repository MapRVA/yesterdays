import type { ExpressionSpecification } from "maplibre-gl";
import {
  detailCircleLayer,
  directionSymbolLayer,
  heatmapLayer,
  simpleCircleLayer,
  simpleDirectionSymbolLayer,
  type ImagePointLayerOptions,
} from "./image_point_layers";
import { DIRECTION_SPRITE_ID, LAYER_IDS, SOURCE_IDS } from "./layer_ids";
import type { MapDisplayContext } from "./types";

// Reusable function to add all image/overlay sources and layers to the map.
// Called on initial load and after style swaps to restore layers.
export async function setupMapDataLayers(ctx: MapDisplayContext): Promise<void> {
  const { map, config, colors } = ctx;
  const {
    showOtherImages,
    imageId,
    vectorTilesUrl,
    allImagesUrl,
    center,
    currentImageDirection,
    aerialGeoreference,
    directionImageUrl,
  } = config;

  // Load direction arrow image
  if (directionImageUrl && !map.hasImage(DIRECTION_SPRITE_ID)) {
    try {
      const image = await map.loadImage(directionImageUrl);
      map.addImage(DIRECTION_SPRITE_ID, image.data);
    } catch (error) {
      console.warn("Could not load direction arrow image:", error);
    }
  }

  // Add vector tiles source for other images
  const tilesUrl =
    showOtherImages && allImagesUrl ? allImagesUrl : vectorTilesUrl;
  if (tilesUrl && !map.getSource(SOURCE_IDS.images)) {
    map.addSource(SOURCE_IDS.images, {
      type: "vector",
      tiles: [tilesUrl],
      minzoom: 0,
      maxzoom: 14,
    });

    // Start hidden if in showOtherImages mode (toggle controls visibility)
    const initialVisibility = showOtherImages ? "none" : "visible";

    // Build filter to exclude current image if in showOtherImages mode
    // Convert imageId to number since vector tile properties are integers
    const excludeCurrentFilter: ExpressionSpecification | null =
      showOtherImages && imageId
        ? ["!=", ["get", "id"], parseInt(imageId, 10)]
        : null;

    const layerOptions: ImagePointLayerOptions = {
      source: SOURCE_IDS.images,
      color: colors.primary,
      extraFilter: excludeCurrentFilter,
      visibility: initialVisibility,
    };

    // Add density heatmap (underneath the direction markers and detail circles)
    map.addLayer(heatmapLayer(LAYER_IDS.imageHeatmap, layerOptions));

    // Add direction markers (fade in at higher zoom, rendered under circles)
    if (map.hasImage(DIRECTION_SPRITE_ID)) {
      map.addLayer(
        directionSymbolLayer(LAYER_IDS.imageDirections, layerOptions),
      );
    }

    // Add detail circles (blur transitions from blurry to sharp, on top of directions)
    map.addLayer(detailCircleLayer(LAYER_IDS.imageCircles, layerOptions));

    // === Simple style layers (hidden by default) ===
    const simpleLayerOptions = { ...layerOptions, visibility: "none" as const };

    // Add simple direction markers (rendered under simple circles)
    if (map.hasImage(DIRECTION_SPRITE_ID)) {
      map.addLayer(
        simpleDirectionSymbolLayer(
          LAYER_IDS.imageDirectionsSimple,
          simpleLayerOptions,
        ),
      );
    }

    // Add simple circle layer (always visible at all zooms, on top of simple directions)
    map.addLayer(
      simpleCircleLayer(LAYER_IDS.imageCirclesSimple, simpleLayerOptions),
    );
  }

  // Add current image as GeoJSON layer (always visible, distinct color, on top)
  if (
    showOtherImages &&
    imageId &&
    center &&
    !map.getSource(SOURCE_IDS.currentImage)
  ) {
    map.addSource(SOURCE_IDS.currentImage, {
      type: "geojson",
      data: {
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: center,
        },
        properties: {
          id: imageId,
          direction: currentImageDirection,
        },
      },
    });

    // Add direction marker for current image
    if (map.hasImage(DIRECTION_SPRITE_ID) && currentImageDirection !== null) {
      map.addLayer({
        id: LAYER_IDS.currentImageDirection,
        type: "symbol",
        source: SOURCE_IDS.currentImage,
        layout: {
          "icon-image": DIRECTION_SPRITE_ID,
          "icon-overlap": "always",
          "icon-ignore-placement": true,
          "icon-size": 1,
          "icon-rotate": currentImageDirection,
          "icon-rotation-alignment": "map",
          "icon-pitch-alignment": "map",
        },
        paint: {
          "icon-opacity": 1,
        },
      });
    }

    // Add circle for current image
    map.addLayer({
      id: LAYER_IDS.currentImageCircle,
      type: "circle",
      source: SOURCE_IDS.currentImage,
      paint: {
        "circle-radius": 8,
        "circle-color": colors.danger,
        "circle-stroke-color": "#fff",
        "circle-stroke-width": 2,
        "circle-pitch-alignment": "map",
        "circle-pitch-scale": "map",
      },
    });
  }

  // Add aerial georeference if provided
  if (aerialGeoreference && !map.getSource(SOURCE_IDS.aerialPolygon)) {
    map.addSource(SOURCE_IDS.aerialPolygon, {
      type: "geojson",
      data: {
        type: "Feature",
        geometry: aerialGeoreference,
        properties: null,
      },
    });

    map.addLayer({
      id: LAYER_IDS.aerialPolygonFill,
      type: "fill",
      source: SOURCE_IDS.aerialPolygon,
      paint: {
        "fill-color": colors.primary,
        "fill-opacity": 0.2,
      },
    });

    map.addLayer({
      id: LAYER_IDS.aerialPolygonOutline,
      type: "line",
      source: SOURCE_IDS.aerialPolygon,
      paint: {
        "line-color": colors.primary,
        "line-width": 2.5,
      },
    });
  }

  // Restore visibility, display style, and point size from LayerControl state
  if (ctx.layerControl) {
    ctx.layerControl.applyImageLayerState();
  }

  // When "Show Other Images" toggle controls visibility, re-hide layers
  // unless the toggle checkbox is currently checked (applyImageLayerState
  // above doesn't know about the separate toggle and would make them visible).
  if (showOtherImages && imageId) {
    const toggle = document.getElementById(
      "show-other-images-toggle",
    ) as HTMLInputElement | null;
    if (!toggle || !toggle.checked) {
      window.toggleOtherImages?.(false);
    }
  }
}
