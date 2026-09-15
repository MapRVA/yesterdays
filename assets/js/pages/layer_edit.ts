// Entry point for the staff map layer create/edit page
// (templates/maps/layer_form.html): the MapLayerForm beside a live MapLibre
// preview that re-renders as the type, URL and attribution fields change.
// The template debounces the field events (@input.debounce) before calling
// render(), so nothing here throttles.
import "../../styles/pages/layer-edit.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import type { Map as MapLibreMap } from "maplibre-gl";
import type { Polygon } from "geojson";
import { LayerExtentEditor } from "../components/layer_extent_editor";
import type { MapLayerType } from "../components/layer_control/types";
import {
  onColorSchemeChange,
  protomapsStyleUrl,
} from "../components/map_display/basemap";
import {
  addOverlayLayer,
  removeOverlayLayer,
  retintBaseMap,
} from "../components/map_display/layer_preview";
import "../components/map_display/pmtiles_protocol";
import { initialMapView } from "../constants/map";

const PREVIEW_ZOOM = 13;

type PreviewStatus = "ok" | "waiting" | "error";

interface LayerEditor {
  type: MapLayerType;
  url: string;
  attribution: string;
  collection: string;
  polygonError: string;
  status: PreviewStatus;
  statusText: string;
  init(): void;
  render(): void;
  collectionChanged(): void;
  prepareSubmit(event: SubmitEvent): void;
  destroy(): void;
  _buildMap(style: string): MapLibreMap;
  _setStatus(status: PreviewStatus, text?: string): void;
}

// Mirrors MapLayer.url_shape_error() closely enough to avoid firing tile
// requests that can't succeed. The model remains the authority on save.
function urlShapeError(type: MapLayerType, url: string): string | null {
  const trimmed = url.trim();
  if (!trimmed) return "Waiting for a tile URL";
  if (!/^https?:\/\//.test(trimmed)) return "Waiting for a valid tile URL";
  switch (type) {
    case "xyz":
      return ["{z}", "{x}", "{y}"].every((p) => trimmed.includes(p))
        ? null
        : "Waiting for a valid tile URL ({z}/{x}/{y} placeholders required)";
    case "pmtiles":
      return trimmed.endsWith(".pmtiles")
        ? null
        : "Waiting for a valid tile URL (must end in .pmtiles)";
    case "style":
      return trimmed.includes("{z}")
        ? "Waiting for a valid style URL (not a tile template)"
        : null;
  }
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("layerEditor", function (): LayerEditor {
    const config = window.layerEditConfig;
    window.PROTOMAPS_API_KEY = config?.protomapsApiKey ?? "";
    const polygonData = JSON.parse(
      document.getElementById("layer-polygon-data")?.textContent ?? "{}",
    ) as { polygon: Polygon | null };
    // Keep library instances outside Alpine's reactive proxies.
    let map: MapLibreMap | null = null;
    let editor: LayerExtentEditor | null = null;
    let styleUrl: string | null = null;
    let polygon = polygonData.polygon;
    let hasCollection = Boolean(config?.collection);
    let revision = 0;
    let renderQueue = Promise.resolve();
    let disposed = false;
    let fitted = false;

    const writePolygon = () => {
      const input = document.querySelector<HTMLInputElement>('input[name="polygon"]');
      if (input) input.value = hasCollection && polygon ? JSON.stringify(polygon) : "";
    };

    const stopEditor = async () => {
      const previous = editor;
      editor = null;
      if (previous) {
        previous.sync();
        await previous.destroy();
      }
    };

    return {
      type: config?.type ?? "pmtiles",
      url: config?.url ?? "",
      attribution: config?.attribution ?? "",
      collection: config?.collection ?? "",
      polygonError: "",
      status: "waiting",
      statusText: "",

      init() {
        window.setupPMTilesProtocol();
        this.render();
        // A "style" layer replaces the Protomaps basemap outright, so there
        // is nothing to retint in that mode.
        onColorSchemeChange(() => {
          if (map && styleUrl === null) void retintBaseMap(map);
        });
      },

      _buildMap(style: string): MapLibreMap {
        const container = document.getElementById("layer-preview-map");
        if (!container) throw new Error("Missing #layer-preview-map");
        const map = new maplibregl.Map({
          container,
          style,
          ...initialMapView({ zoom: PREVIEW_ZOOM }),
        });
        map.addControl(new maplibregl.NavigationControl());
        map.addControl(new maplibregl.FullscreenControl());
        map.on("error", (event) => {
          // MapLibre also reports missing-tile 404s here; only surface
          // failures that involve the layer being previewed.
          const message = event.error?.message ?? "";
          const sourceId = (event as { sourceId?: string }).sourceId;
          if (sourceId && sourceId !== "overlay-layer") return;
          console.warn("Preview source error:", message);
          this._setStatus("error", "This source failed to load");
        });
        return map;
      },

      render() {
        const currentRevision = ++revision;
        renderQueue = renderQueue.then(async () => {
          if (disposed || currentRevision !== revision) return;
          const url = this.url.trim();
          const shapeError = urlShapeError(this.type, url);
          const wantStyleMode = this.type === "style" && !shapeError;
          const nextStyleUrl = wantStyleMode ? url : null;

          // Tear down Geoman before replacing its map. The form owns geometry
          // throughout, including when a style URL fails to load.
          if (map && nextStyleUrl !== styleUrl) {
            const camera = { center: map.getCenter(), zoom: map.getZoom() };
            await stopEditor();
            map.remove();
            map = this._buildMap(nextStyleUrl ?? protomapsStyleUrl());
            map.jumpTo(camera);
          }
          if (!map) map = this._buildMap(nextStyleUrl ?? protomapsStyleUrl());
          styleUrl = nextStyleUrl;
          const currentMap = map;

          if (!hasCollection && editor) await stopEditor();
          if (disposed || currentRevision !== revision) return;
          if (hasCollection && !editor) {
            editor = new LayerExtentEditor(currentMap, polygon, (geometry) => {
              polygon = geometry;
              this.polygonError = geometry ? "" : "Draw a polygon for this collection layer.";
              writePolygon();
            }, (message) => { this.polygonError = message; });
            if (polygon && !fitted) {
              const bounds = new maplibregl.LngLatBounds();
              for (const coordinate of polygon.coordinates[0] ?? []) {
                bounds.extend([coordinate[0]!, coordinate[1]!]);
              }
              if (!bounds.isEmpty()) currentMap.fitBounds(bounds, { padding: 40, duration: 0 });
              fitted = true;
            }
          }

          if (wantStyleMode) {
            this._setStatus("ok");
            return;
          }

          const draw = () => {
            if (disposed || currentRevision !== revision || currentMap !== map) return;
            removeOverlayLayer(currentMap);
            if (shapeError) {
              this._setStatus("waiting", shapeError);
              return;
            }
            addOverlayLayer(currentMap, {
              name: config?.name ?? "",
              type: this.type,
              url,
              attribution: this.attribution,
            });
            editor?.raiseLayers();
            this._setStatus("ok");
          };
          if (currentMap.isStyleLoaded()) draw();
          else currentMap.once("load", draw);
        }).catch((error: unknown) => {
          console.error("Could not update layer preview:", error);
          this._setStatus("error", "The preview could not load");
          this.polygonError = "The extent tools could not load. Reload before saving.";
        });
      },

      collectionChanged() {
        editor?.sync();
        const wasGlobal = !hasCollection;
        hasCollection = Boolean(this.collection);
        if (wasGlobal && hasCollection) fitted = false;
        this.polygonError = "";
        writePolygon();
        this.render();
      },

      prepareSubmit(event) {
        editor?.sync();
        writePolygon();
        if (hasCollection && (!polygon || !editor?.canSubmit())) {
          event.preventDefault();
          this.polygonError = !polygon
            ? "Draw a polygon for this collection layer."
            : "Wait for the extent tools to finish loading before saving.";
        }
      },

      destroy() {
        disposed = true;
        ++revision;
        void renderQueue.then(stopEditor).finally(() => {
          map?.remove();
          map = null;
        });
      },

      _setStatus(status: PreviewStatus, text = "") {
        this.status = status;
        this.statusText = text;
      },
    };
  });
});
