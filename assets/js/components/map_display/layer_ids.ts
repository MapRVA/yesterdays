// Source, layer, and sprite ids owned by the map_display component.
// layer_control/ imports these to decide layer stacking and visibility.
export const SOURCE_IDS = {
  images: "images",
  // The same tiles requested with `points=1`: one feature per image at every
  // zoom, for the simple style (popups need ids; arrows show at all zooms).
  imagePoints: "image-points",
  currentImage: "current-image",
  aerialPolygon: "aerial-polygon",
} as const;

export const LAYER_IDS = {
  imageHeatmap: "image-heatmap",
  imageCircles: "image-circles",
  imageDirections: "image-directions",
  imageCirclesSimple: "image-circles-simple",
  imageDirectionsSimple: "image-directions-simple",
  currentImageCircle: "current-image-circle",
  currentImageDirection: "current-image-direction",
  aerialPolygonFill: "aerial-polygon-fill",
  aerialPolygonOutline: "aerial-polygon-outline",
} as const;

// Name of the direction-arrow sprite registered via map.addImage()
export const DIRECTION_SPRITE_ID = "image-direction";

// The image layers LayerControl treats as toggleable overlays
export const OVERLAY_LAYER_IDS: string[] = [
  LAYER_IDS.imageHeatmap,
  LAYER_IDS.imageCircles,
  LAYER_IDS.imageDirections,
  LAYER_IDS.imageCirclesSimple,
  LAYER_IDS.imageDirectionsSimple,
];
