/**
 * ImagePointOverlay - draws a known list of georeferenced images on a map as
 * points with direction cones.
 *
 * The sitewide maps read their points from the image_points vector tiles,
 * which is the right thing when the answer is "every image in this viewport".
 * A search result set is not that: it is a specific handful of images the
 * server already picked, so this draws them from a GeoJSON source instead —
 * reusing the same layer definitions the tiled maps use, so a result looks
 * exactly like the same image looks anywhere else on the site.
 *
 * Usage:
 *   const overlay = createImagePointOverlay(map, { spriteUrl });
 *   overlay.setPoints([{ id: 1, lat: 37.5, lng: -77.4, direction: 90 }]);
 *   overlay.addPoints(morePoints);   // "Load More"
 *   overlay.redraw();                // after a base-layer style swap
 */

import type { Map as MapLibreMap, GeoJSONSource } from "maplibre-gl";
import type { FeatureCollection, Point } from "geojson";
import { primaryColor } from "./map_display/colors";
import { ensureDirectionSprite } from "./map_display/direction_sprite";
import {
  simpleCircleLayer,
  simpleDirectionSymbolLayer,
} from "./map_display/image_point_layers";

export interface ImagePointDatum {
  id: number;
  lat: number;
  lng: number;
  direction: number | null;
}

export interface ImagePointOverlayOptions {
  /**
   * URL of the direction-arrow sprite. Pass the page's own {% static %} URL
   * so the browser isn't fetching an icon cross-origin that the site ships.
   */
  spriteUrl?: string;
  color?: string;
  /** Distinguishes this overlay's source and layers from any others. */
  idPrefix?: string;
}

export const DEFAULT_OVERLAY_PREFIX = "result-points";

/** The source and layer ids an overlay with this prefix will own. */
export function imagePointOverlayIds(idPrefix: string = DEFAULT_OVERLAY_PREFIX) {
  return {
    source: idPrefix,
    circles: `${idPrefix}-circles`,
    directions: `${idPrefix}-directions`,
  };
}

/**
 * Just the layer ids, cones first.
 *
 * Available before the overlay itself, which needs a map to exist: a caller
 * building the map has to tell its LayerControl which layers to keep tile
 * overlays underneath, and that is decided at construction time.
 */
export function imagePointOverlayLayerIds(
  idPrefix: string = DEFAULT_OVERLAY_PREFIX,
): string[] {
  const ids = imagePointOverlayIds(idPrefix);
  return [ids.directions, ids.circles];
}

export interface ImagePointOverlay {
  setPoints(points: ImagePointDatum[]): void;
  addPoints(points: ImagePointDatum[]): void;
  clear(): void;
  /** Re-add source and layers, which a MapLibre style swap discards. */
  redraw(): void;
  /** Layer ids, for LayerControl to insert tile overlays underneath. */
  readonly layerIds: string[];
}

function toFeatureCollection(
  points: ImagePointDatum[],
): FeatureCollection<Point> {
  return {
    type: "FeatureCollection",
    features: points.map((point) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [point.lng, point.lat] },
      // `direction` is omitted rather than set to null when unrecorded: the
      // direction layer filters on ["has", "direction"], and a null value
      // still counts as present.
      properties: {
        id: point.id,
        ...(point.direction === null ? {} : { direction: point.direction }),
      },
    })),
  };
}

export function createImagePointOverlay(
  map: MapLibreMap,
  options: ImagePointOverlayOptions = {},
): ImagePointOverlay {
  const {
    spriteUrl,
    color = primaryColor,
    idPrefix = DEFAULT_OVERLAY_PREFIX,
  } = options;

  const ids = imagePointOverlayIds(idPrefix);
  const layerIds = imagePointOverlayLayerIds(idPrefix);

  let points: ImagePointDatum[] = [];

  const draw = async (): Promise<void> => {
    // Cones are drawn from a sprite the map may not have yet; without it the
    // symbol layer would render nothing at all.
    await ensureDirectionSprite(map, spriteUrl);

    if (!map.getSource(ids.source)) {
      map.addSource(ids.source, {
        type: "geojson",
        data: toFeatureCollection(points),
      });
    }

    const layerOptions = { source: ids.source, sourceLayer: null, color };
    // Cones underneath, so a circle is never hidden by its neighbour's cone.
    if (!map.getLayer(ids.directions)) {
      map.addLayer(simpleDirectionSymbolLayer(ids.directions, layerOptions));
    }
    if (!map.getLayer(ids.circles)) {
      map.addLayer(simpleCircleLayer(ids.circles, layerOptions));
    }

    map.getSource<GeoJSONSource>(ids.source)?.setData(toFeatureCollection(points));
  };

  // A map still loading its style has nowhere to put a source yet.
  const drawWhenReady = (): void => {
    if (map.isStyleLoaded()) {
      void draw();
      return;
    }
    // Deliberately not `load`: that fires once in a map's lifetime, so a
    // redraw arriving during a later base-layer swap would wait forever.
    // `styledata` fires for every style, and re-checking on each one costs
    // nothing when the first is already the right one.
    map.once("styledata", () => drawWhenReady());
  };

  return {
    setPoints(next) {
      points = [...next];
      drawWhenReady();
    },
    addPoints(next) {
      points = [...points, ...next];
      drawWhenReady();
    },
    clear() {
      points = [];
      drawWhenReady();
    },
    redraw() {
      drawWhenReady();
    },
    layerIds,
  };
}
