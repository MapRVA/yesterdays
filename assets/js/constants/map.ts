import type { MapOptions } from "maplibre-gl";

// Map configuration constants. There is no sitewide default place: a map
// opens on the region selected in the navbar, else on the whole world.
export type MapBounds = [number, number, number, number];

// The whole world, to Web Mercator's latitude limits. Must match the literal
// in templates/images/partials/map_display.html.
export const WORLD_BOUNDS: MapBounds = [-180, -85.0511, 180, 85.0511];

// Zoom for a selected region that has no map bounds, shown around its
// centerpoint. Set from regions.context_processors.REGION_MAP_ZOOM.
export const REGION_MAP_ZOOM = window.REGION_MAP_ZOOM ?? 10;

export const REGION_MAP_CENTER = window.REGION_MAP_CENTER ?? null;
export const REGION_MAP_BOUNDS = window.REGION_MAP_BOUNDS ?? null;

export interface InitialMapViewOptions {
  center?: [number, number];
  // How close to show a center. Applies to an explicit center or the
  // selected region's centerpoint; with no region the map fits the world.
  zoom?: number;
  // An explicit viewport; null rules out the selected region's bounds.
  bounds?: MapBounds | null;
  padding?: number;
  maxZoom?: number;
}

type InitialMapView = Pick<
  MapOptions,
  "bounds" | "center" | "zoom" | "fitBoundsOptions"
>;

// The opening camera for a map, in precedence order: explicit bounds, an
// explicit center, the selected region's bounds, then its centerpoint, then
// the whole world. Explicit bounds outrank an explicit center because the
// shared map template passes a region's centerpoint alongside its bounds,
// as a fallback for regions that have none.
export function initialMapView(
  options: InitialMapViewOptions = {},
): Partial<InitialMapView> {
  const { center, zoom, padding = 48, maxZoom = 14 } = options;
  const bounds =
    options.bounds === undefined && !center
      ? REGION_MAP_BOUNDS
      : (options.bounds ?? null);

  const fit = ([west, south, east, north]: MapBounds) => ({
    bounds: [
      [west, south],
      [east, north],
    ] as [[number, number], [number, number]],
    fitBoundsOptions: { padding, maxZoom },
  });

  if (bounds) return fit(bounds);
  if (center) return { center, zoom: zoom ?? REGION_MAP_ZOOM };
  if (REGION_MAP_CENTER) {
    return { center: REGION_MAP_CENTER, zoom: zoom ?? REGION_MAP_ZOOM };
  }
  return fit(WORLD_BOUNDS);
}
