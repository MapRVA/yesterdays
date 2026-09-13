// Entry point for the staff map layer create/edit page
// (templates/maps/layer_form.html): the MapLayerForm beside a live MapLibre
// preview that re-renders as the type, URL and attribution fields change.
// The template debounces the field events (@input.debounce) before calling
// render(), so nothing here throttles.
import "../../styles/pages/layer-edit.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import type { Map as MapLibreMap } from "maplibre-gl";
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
  status: PreviewStatus;
  statusText: string;
  init(): void;
  render(): void;
  _map: MapLibreMap | null;
  _mode: MapLayerType | null;
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

    return {
      type: config?.type ?? "pmtiles",
      url: config?.url ?? "",
      attribution: config?.attribution ?? "",
      status: "waiting",
      statusText: "",
      _map: null,
      _mode: null,

      init() {
        window.setupPMTilesProtocol();
        this.render();
        // A "style" layer replaces the Protomaps basemap outright, so there
        // is nothing to retint in that mode.
        onColorSchemeChange(() => {
          if (this._map && this._mode !== "style") retintBaseMap(this._map);
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
        const url = this.url.trim();
        const shapeError = urlShapeError(this.type, url);
        const wantStyleMode = this.type === "style" && !shapeError;

        // Switching into or out of style mode swaps the whole style, so the
        // map has to be rebuilt; a plain URL edit never does.
        const currentlyStyleMode = this._mode === "style";
        if (this._map && wantStyleMode !== currentlyStyleMode) {
          this._map.remove();
          this._map = null;
        }

        if (wantStyleMode) {
          if (!this._map) {
            this._map = this._buildMap(url);
          } else {
            this._map.setStyle(url);
          }
          this._mode = "style";
          this._setStatus("ok");
          return;
        }

        if (!this._map) {
          this._map = this._buildMap(protomapsStyleUrl());
        }
        this._mode = this.type === "style" ? null : this.type;

        const map = this._map;
        const draw = () => {
          removeOverlayLayer(map);
          if (shapeError) {
            this._setStatus("waiting", shapeError);
            return;
          }
          addOverlayLayer(map, {
            name: config?.name ?? "",
            type: this.type,
            url,
            attribution: this.attribution,
          });
          this._setStatus("ok");
        };
        if (map.isStyleLoaded()) draw();
        else map.once("load", draw);
      },

      _setStatus(status: PreviewStatus, text = "") {
        this.status = status;
        this.statusText = text;
      },
    };
  });
});
