// Entry point for the staff region metadata editor.
import "../../styles/pages/region-edit.css";
import "maplibre-gl/dist/maplibre-gl.css";

import type { Polygon } from "geojson";
import maplibregl from "maplibre-gl";
import type { Map as MapLibreMap, Marker } from "maplibre-gl";
import {
  RegionBoundsEditor,
  type RegionBounds,
} from "../components/region_bounds_editor";
import {
  onColorSchemeChange,
  protomapsStyleUrl,
} from "../components/map_display/basemap";
import { retintBaseMap } from "../components/map_display/layer_preview";
import { DEFAULT_MAP_CENTER, DEFAULT_MAP_ZOOM } from "../constants/map";

type Coordinate = [number, number];
type BoundsKind = "map" | "geocoder";

interface RegionEditorConfig {
  protomapsApiKey: string;
  effectiveCenter: Coordinate | null;
  wikidataCenter: Coordinate | null;
}

interface RegionEditor {
  mapBounds: RegionBounds | null;
  geocoderBounds: RegionBounds | null;
  customCenter: boolean;
  hasWikidataCenter: boolean;
  center: Coordinate | null;
  mapError: string;
  geocoderError: string;
  readonly centerLabel: string;
  readonly mapBoundsLabel: string;
  readonly geocoderBoundsLabel: string;
  init(): void;
  resetCenter(): void;
  prepareSubmit(event: SubmitEvent): void;
  destroy(): void;
  setCustomCenter(coordinate: Coordinate): void;
  setBounds(kind: BoundsKind, bounds: RegionBounds | null): void;
}

const config = JSON.parse(
  document.getElementById("region-editor-config")?.textContent ?? "{}",
) as RegionEditorConfig;

function input(name: string): HTMLInputElement {
  const element = document.querySelector<HTMLInputElement>(`input[name="${name}"]`);
  if (!element) throw new Error(`Missing region form field: ${name}`);
  return element;
}

function fieldNumber(name: string): number | null {
  const value = input(name).value.trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function readCenter(): Coordinate | null {
  const latitude = fieldNumber("center_latitude");
  const longitude = fieldNumber("center_longitude");
  return latitude !== null && longitude !== null ? [longitude, latitude] : null;
}

function readBounds(kind: BoundsKind): RegionBounds | null {
  const prefix = kind === "map" ? "map" : "geocoder";
  const values = ["west", "south", "east", "north"].map((edge) =>
    fieldNumber(`${prefix}_${edge}`),
  );
  if (values.some((value) => value === null)) return null;
  const [west, south, east, north] = values as RegionBounds;
  return west < east && south < north ? [west, south, east, north] : null;
}

function writeCenter(center: Coordinate | null): void {
  input("center_longitude").value = center ? String(center[0]) : "";
  input("center_latitude").value = center ? String(center[1]) : "";
}

function writeBounds(kind: BoundsKind, bounds: RegionBounds | null): void {
  const prefix = kind === "map" ? "map" : "geocoder";
  const edges = ["west", "south", "east", "north"];
  for (let index = 0; index < edges.length; index += 1) {
    input(`${prefix}_${edges[index]}`).value = bounds ? String(bounds[index]) : "";
  }
}

function boundsPolygon(bounds: RegionBounds | null): Polygon | null {
  if (!bounds) return null;
  const [west, south, east, north] = bounds;
  return {
    type: "Polygon",
    coordinates: [[
      [west, south],
      [east, south],
      [east, north],
      [west, north],
      [west, south],
    ]],
  };
}

function formatCoordinate(coordinate: Coordinate | null): string {
  if (!coordinate) return "Not set";
  return `${coordinate[1].toFixed(5)}, ${coordinate[0].toFixed(5)}`;
}

function formatBounds(bounds: RegionBounds | null): string {
  if (!bounds) return "Not set";
  return bounds.map((value) => value.toFixed(5)).join(", ");
}

function buildMap(
  container: string,
  bounds: RegionBounds | null,
  center: Coordinate,
): MapLibreMap {
  const map = new maplibregl.Map({
    container,
    style: protomapsStyleUrl(),
    ...(bounds
      ? {
          bounds: [[bounds[0], bounds[1]], [bounds[2], bounds[3]]],
          fitBoundsOptions: { padding: 48, maxZoom: 14 },
        }
      : { center, zoom: DEFAULT_MAP_ZOOM }),
  });
  map.addControl(new maplibregl.NavigationControl());
  map.addControl(new maplibregl.FullscreenControl());
  return map;
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("regionEditor", function (): RegionEditor {
    let viewportMap: MapLibreMap | null = null;
    let geocoderMap: MapLibreMap | null = null;
    let centerMarker: Marker | null = null;
    let viewportEditor: RegionBoundsEditor | null = null;
    let geocoderEditor: RegionBoundsEditor | null = null;

    return {
      mapBounds: readBounds("map"),
      geocoderBounds: readBounds("geocoder"),
      customCenter: readCenter() !== null,
      hasWikidataCenter: config.wikidataCenter !== null,
      center: readCenter() ?? config.effectiveCenter ?? null,
      mapError: "",
      geocoderError: "",

      get centerLabel() {
        return formatCoordinate(this.center);
      },

      get mapBoundsLabel() {
        return formatBounds(this.mapBounds);
      },

      get geocoderBoundsLabel() {
        return formatBounds(this.geocoderBounds);
      },

      init() {
        window.PROTOMAPS_API_KEY = config.protomapsApiKey;
        const center = this.center ?? DEFAULT_MAP_CENTER;
        viewportMap = buildMap("region-map-bounds-map", this.mapBounds, center);
        geocoderMap = buildMap(
          "region-geocoder-bounds-map",
          this.geocoderBounds ?? this.mapBounds,
          center,
        );

        viewportEditor = new RegionBoundsEditor(
          viewportMap,
          boundsPolygon(this.mapBounds),
          (bounds) => this.setBounds("map", bounds),
          (message) => { this.mapError = message; },
        );
        geocoderEditor = new RegionBoundsEditor(
          geocoderMap,
          boundsPolygon(this.geocoderBounds),
          (bounds) => this.setBounds("geocoder", bounds),
          (message) => { this.geocoderError = message; },
        );

        centerMarker = new maplibregl.Marker({ color: "#dc3545", draggable: true })
          .setLngLat(center)
          .addTo(viewportMap);
        centerMarker.on("drag", () => {
          const point = centerMarker?.getLngLat();
          if (point) this.setCustomCenter([point.lng, point.lat]);
        });

        onColorSchemeChange(() => {
          if (viewportMap) void retintBaseMap(viewportMap);
          if (geocoderMap) void retintBaseMap(geocoderMap);
        });
      },

      resetCenter() {
        if (!config.wikidataCenter || !centerMarker) return;
        this.customCenter = false;
        this.center = config.wikidataCenter;
        writeCenter(null);
        centerMarker.setLngLat(config.wikidataCenter);
      },

      prepareSubmit(event) {
        viewportEditor?.sync();
        geocoderEditor?.sync();
        if (!viewportEditor?.canSubmit()) {
          event.preventDefault();
          this.mapError = "Wait for the viewport rectangle tools to finish loading.";
        }
        if (!geocoderEditor?.canSubmit()) {
          event.preventDefault();
          this.geocoderError = "Wait for the geocoder rectangle tools to finish loading.";
        }
      },

      destroy() {
        centerMarker?.remove();
        centerMarker = null;
        const viewportCleanup = viewportEditor?.destroy() ?? Promise.resolve();
        const geocoderCleanup = geocoderEditor?.destroy() ?? Promise.resolve();
        viewportEditor = null;
        geocoderEditor = null;
        void Promise.all([viewportCleanup, geocoderCleanup]).finally(() => {
          viewportMap?.remove();
          geocoderMap?.remove();
          viewportMap = null;
          geocoderMap = null;
        });
      },

      setCustomCenter(coordinate) {
        this.customCenter = true;
        this.center = coordinate;
        writeCenter(coordinate);
      },

      setBounds(kind, bounds) {
        if (kind === "map") {
          this.mapBounds = bounds;
          this.mapError = "";
        } else {
          this.geocoderBounds = bounds;
          this.geocoderError = "";
        }
        writeBounds(kind, bounds);
      },
    };
  });
});
