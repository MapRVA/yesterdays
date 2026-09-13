/**
 * PointPicker - a small map for choosing a single coordinate.
 *
 * Click the map (or drag the marker) to place a point; the bound latitude and
 * longitude inputs update, and typing into either input moves the marker back.
 * Everything else — the base layer switcher, the region-aware initial view,
 * and place-name search — comes from the shared map components, so a caller
 * only supplies a container and two inputs.
 *
 * Usage:
 *   import { initPointPicker } from "../components/point_picker";
 *
 *   const picker = initPointPicker({
 *     mapId: "inViewPickerMap",
 *     latInput: document.getElementById("lat") as HTMLInputElement,
 *     lonInput: document.getElementById("lon") as HTMLInputElement,
 *     onChange: (lng, lat) => search(lat, lng),
 *   });
 *
 * The map is created against whatever size its container has at the time, so
 * build the picker once its container is visible — a container hidden behind
 * `display: none` has no size. Later size changes are handled: MapLibre
 * watches the container, and the picker nudges it once more after creation for
 * the case where the container is revealed in the same frame.
 */

import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./map_display/pmtiles_protocol";
import { initialMapStyle, LayerControl } from "./layer_control";
import { dangerColor } from "./map_display/colors";
import { addResponsiveGeocoder } from "./responsive_geocoder";
import { initialMapView } from "../constants/map";

// Close enough to read a street, far enough to recognise the block.
const PLACED_POINT_ZOOM = 16;

// How many decimals the inputs show. ~0.1 m at these latitudes, which is
// finer than any georeference is honest about, and short enough to read.
const COORDINATE_PRECISION = 6;

export interface PointPickerOptions {
  // id of the (already visible) container element for the map
  mapId: string;
  latInput: HTMLInputElement;
  lonInput: HTMLInputElement;
  // Starting point as [lng, lat], matching MapLibre's coordinate order
  initial?: [number, number];
  // Called whenever the *user* moves the point, by click, drag, or typing.
  // Not called for `initial` or for setPoint(), where the caller already knows.
  onChange?: (lng: number, lat: number) => void;
  // Run after a base-layer change, which discards every source and layer a
  // caller added to the map. The marker is a DOM element and survives.
  onStyleSwap?: () => void | Promise<void>;
  // Layers that must stay above tile overlays the LayerControl inserts.
  overlayLayerIds?: string[];
}

export interface PointPicker {
  setPoint(lng: number, lat: number): void;
  getPoint(): [number, number] | null;
  // The underlying map, for callers that need to add their own layers
  map: MapLibreMap;
}

// The chosen point, drawn to match the "current image" marker on an image
// detail page: an 8px-radius circle with a 2px white ring. Red rather than the
// primary colour every *other* point on a map uses, so the spot being asked
// about never reads as one of the answers.
//
// A MapLibre circle layer can't be dragged, so this is a Marker element with
// the same geometry drawn as an SVG data URI — no image asset needed.
const MARKER_RADIUS = 8;
const MARKER_STROKE = 2;

function createMarkerElement(): HTMLElement {
  const encodedColor = encodeURIComponent(dangerColor);
  // The stroke straddles the circle's edge, so half of it falls outside.
  const size = (MARKER_RADIUS + MARKER_STROKE / 2) * 2;
  const centre = size / 2;

  const el = document.createElement("div");
  el.style.width = `${size}px`;
  el.style.height = `${size}px`;
  el.style.backgroundImage = `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 ${size} ${size}'%3E%3Ccircle cx='${centre}' cy='${centre}' r='${MARKER_RADIUS}' fill='${encodedColor}' stroke='%23fff' stroke-width='${MARKER_STROKE}'/%3E%3C/svg%3E")`;
  el.style.backgroundSize = "contain";
  el.style.cursor = "grab";
  return el;
}

export function initPointPicker(options: PointPickerOptions): PointPicker {
  const {
    mapId,
    latInput,
    lonInput,
    initial,
    onChange,
    onStyleSwap,
    overlayLayerIds = [],
  } = options;

  window.setupPMTilesProtocol();

  const map = new maplibregl.Map({
    container: mapId,
    style: initialMapStyle(),
    ...(initial
      ? { center: initial, zoom: PLACED_POINT_ZOOM }
      : initialMapView()),
  });

  map.addControl(
    new LayerControl({ overlayLayerIds, onStyleSwap: onStyleSwap ?? null }),
    "top-right",
  );
  map.addControl(new maplibregl.NavigationControl());
  map.getCanvas().style.cursor = "crosshair";

  const marker = new maplibregl.Marker({
    element: createMarkerElement(),
    draggable: true,
  });
  let point: [number, number] | null = null;

  // `writeInputs: false` is for a point that came *from* the inputs, so a
  // half-typed coordinate isn't reformatted under the cursor. `notify: false`
  // is for programmatic moves, where the caller is the one who asked.
  const place = (
    lng: number,
    lat: number,
    { writeInputs = true, notify = true } = {},
  ): void => {
    point = [lng, lat];
    marker.setLngLat(point).addTo(map);
    if (writeInputs) {
      latInput.value = lat.toFixed(COORDINATE_PRECISION);
      lonInput.value = lng.toFixed(COORDINATE_PRECISION);
    }
    if (notify) onChange?.(lng, lat);
  };

  map.on("click", (e) => {
    place(e.lngLat.lng, e.lngLat.lat);
  });

  marker.on("dragend", () => {
    const { lng, lat } = marker.getLngLat();
    place(lng, lat);
  });

  // Typing a coordinate moves the marker, but only once both halves parse:
  // a longitude alone doesn't describe a place.
  const readInputs = (): void => {
    const lat = parseFloat(latInput.value);
    const lng = parseFloat(lonInput.value);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return;

    place(lng, lat, { writeInputs: false });
    map.easeTo({ center: [lng, lat] });
  };
  latInput.addEventListener("change", readInputs);
  lonInput.addEventListener("change", readInputs);

  map.on("load", () => {
    addResponsiveGeocoder(map);
    // The container may have been revealed in the same frame the map was
    // built, before its own observer had a size to react to.
    map.resize();
  });

  if (initial) {
    place(initial[0], initial[1], { notify: false });
  }

  return {
    setPoint(lng: number, lat: number) {
      place(lng, lat, { notify: false });
      map.easeTo({ center: [lng, lat] });
    },
    getPoint() {
      return point;
    },
    map,
  };
}
