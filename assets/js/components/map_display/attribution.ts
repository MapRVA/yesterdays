import maplibregl, { type AttributionControlOptions } from "maplibre-gl";

/**
 * MapLibre's default attribution control, minus the "MapLibre" credit it adds
 * on its own. Pass as `attributionControl` to every `new maplibregl.Map` that
 * doesn't mount its attribution through one of the helpers below.
 */
export const ATTRIBUTION_CONTROL_OPTIONS: AttributionControlOptions = {
  compact: true,
};

/**
 * Show the map's attribution (MapLibre's AttributionControl, so it still
 * tracks layer and source changes) outside the map, in `container`: a caption
 * directly under the map, or the /map/ page's footer. OSMF's attribution
 * guidelines accept credit "adjacent to the map", and this keeps it off the
 * map's corners, which the date-range slider uses.
 *
 * Fullscreen hides everything but the map, so while it's active the credit
 * moves into the map's bottom-left corner, returning on exit.
 */
export function mountExternalAttribution(
  map: maplibregl.Map,
  container: HTMLElement,
  fullscreenControl: maplibregl.FullscreenControl,
): void {
  const control = new maplibregl.AttributionControl({ compact: false });
  const element = control.onAdd(map);
  container.appendChild(element);

  // The credit's text arrives with the style, after the map has sized
  // itself. Where the map shares a flex layout with it (the /map/ footer on
  // phones, where it wraps onto its own line), that shrinks the map's
  // container during load, a change MapLibre's resize tracking can miss
  // (it skips its observer's first report), leaving the canvas drawn past
  // the container. Resize whenever the credit's container changes size.
  new ResizeObserver(() => map.resize()).observe(container);

  const corner = map
    .getContainer()
    .querySelector<HTMLElement>(".maplibregl-ctrl-bottom-left");
  if (!corner) return;
  fullscreenControl.on("fullscreenstart", () => corner.appendChild(element));
  fullscreenControl.on("fullscreenend", () => container.appendChild(element));
}

/**
 * The in-map fallback (e.g. the external iframe embed, which has no room
 * below the map): MapLibre's usual collapsible credit, in the bottom-left
 * corner so it stays clear of the slider in the bottom-right.
 */
export function addCornerAttribution(map: maplibregl.Map): void {
  // An options object, even empty: with none, MapLibre adds its own credit
  map.addControl(new maplibregl.AttributionControl({}), "bottom-left");
}

/**
 * For the tiny map in the homepage hero, it doesn't make sense to
 * start with an expanded attribution
 */
export function addCollapsedAttribution(map: maplibregl.Map): void {
  const control = new maplibregl.AttributionControl({ compact: true });
  map.addControl(control);
  control._updateCompactMinimize();
}
