import maplibregl from "maplibre-gl";
import { LAYER_IDS, SOURCE_IDS } from "./layer_ids";
import type { MapDisplayContext } from "./types";

interface SourceFeaturesWatch {
  sourceId: string;
  sourceLayer: string;
  // Layer to query rendered features from when the source query returns none
  fallbackLayerId?: string;
}

// Invokes onFeatures once, with the first non-empty batch of features from the
// source. Tile loading can race the sourcedata event, hence the retry.
function watchFirstSourceFeatures(
  map: maplibregl.Map,
  watch: SourceFeaturesWatch,
  onFeatures: (features: maplibregl.GeoJSONFeature[]) => void,
): void {
  let loaded = false;
  map.on("sourcedata", (e) => {
    if (e.sourceId !== watch.sourceId || !e.isSourceLoaded || loaded) return;

    let features = map.querySourceFeatures(watch.sourceId, {
      sourceLayer: watch.sourceLayer,
    });

    if (
      features.length === 0 &&
      watch.fallbackLayerId &&
      map.getLayer(watch.fallbackLayerId)
    ) {
      features = map.queryRenderedFeatures({
        layers: [watch.fallbackLayerId],
      });
    }

    if (features.length > 0) {
      loaded = true;
      onFeatures(features);
    } else {
      setTimeout(() => {
        if (loaded) return;
        const retryFeatures = map.querySourceFeatures(watch.sourceId, {
          sourceLayer: watch.sourceLayer,
        });

        if (retryFeatures.length > 0) {
          loaded = true;
          onFeatures(retryFeatures);
        }
      }, 1000);
    }
  });
}

// With zoomToContents, watches for the first load of whichever image source
// the display style uses (clustered for the heatmap, per image for the simple
// style) and fits the map to its features. Clusters sit on their images'
// tile-grid cell, so they bound the contents just as well.
export function watchInitialFeatures(ctx: MapDisplayContext): void {
  const { map, config } = ctx;
  if (!config.zoomToContents) return;

  let fitted = false;
  const watches: SourceFeaturesWatch[] = [
    {
      sourceId: SOURCE_IDS.images,
      sourceLayer: "image_points",
      fallbackLayerId: LAYER_IDS.imageCircles,
    },
    {
      sourceId: SOURCE_IDS.imagePoints,
      sourceLayer: "image_points",
      fallbackLayerId: LAYER_IDS.imageCirclesSimple,
    },
  ];
  for (const watch of watches) {
    watchFirstSourceFeatures(map, watch, (features) => {
      if (fitted) return;
      fitted = true;
      if (features.length > 0) {
        const bounds = new maplibregl.LngLatBounds();
        features.forEach(function (feature) {
          if (feature.geometry.type !== "Point") return;
          bounds.extend(feature.geometry.coordinates as [number, number]);
        });
        map.fitBounds(bounds, {
          padding: 50,
          maxZoom: 15,
        });
      }
    });
  }
}
