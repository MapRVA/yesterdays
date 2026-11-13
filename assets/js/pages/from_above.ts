// Entry point for the From Above page (templates/images/from_above.html): a
// coverage map of every aerial georeference over the aerial image grid.
// Clicking the map filters the grid to aerials covering that point, swapping
// the results in place instead of reloading the page.
import "../../styles/pages/from-above.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import type { FeatureCollection, MultiPolygon, Polygon } from "geojson";
import { imageGrid } from "../components/image_grid";
import { initialMapStyle } from "../components/layer_control";
import { styleImageHandlers } from "../components/layer_control/image_handlers";
import { initialPrimaryLayer } from "../components/layer_control/initial_style";
import { primaryColor } from "../components/map_display/colors";
import "../components/map_display/pmtiles_protocol";
import { initialMapView } from "../constants/map";

// Parsed from the map container's data-config attribute.
interface FromAboveConfig {
  aerialGeojsonUrl?: string;
  aerialAtPointUrl?: string;
  filterLat?: number;
  filterLon?: number;
}

type AerialCollection = FeatureCollection<Polygon | MultiPolygon>;

interface AerialsPage {
  error: string | null;
  isFiltered: boolean;
  isFiltering: boolean;
  initMap(): void;
  applyPointFilter(lat: number, lon: number): Promise<void>;
  clearFilter(): Promise<void>;
}

const SOURCE_ID = "aerial-georeferences";
const FILL_LAYER_ID = "aerial-fill";

// Regions of the page re-rendered from the server's HTML after the filter
// changes. The grid toolbar, filter panel and modals around the results are
// left alone: their listeners are bound once at page load.
const SWAPPED_REGIONS = [
  "#from-above-results [data-grid-results]",
  "#from-above-count",
];

function filterMarkerElement(): HTMLElement {
  const color = encodeURIComponent(primaryColor);
  const el = document.createElement("div");
  el.className = "aerial-filter-marker";
  el.style.backgroundImage = `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='10' fill='${color}' stroke='%23fff' stroke-width='2'/%3E%3C/svg%3E")`;
  return el;
}

function swapPageContent(html: string): void {
  const doc = new DOMParser().parseFromString(html, "text/html");
  for (const selector of SWAPPED_REGIONS) {
    const current = document.querySelector(selector);
    const next = doc.querySelector(selector);
    if (current && next) current.innerHTML = next.innerHTML;
  }
}

async function fetchPageContent(url: string): Promise<string> {
  const response = await fetch(url, {
    headers: { "X-Requested-With": "XMLHttpRequest" },
  });
  if (!response.ok) throw new Error(`Failed to fetch ${url}`);
  return response.text();
}

// Alpine.start() is called by assets/index.js on DOMContentLoaded.
window.Alpine.data("imageGrid", imageGrid);

window.Alpine.data("aerialsPage", function (): AerialsPage {
  // MapLibre objects stay out of Alpine's reactive state, which would wrap
  // them in proxies.
  let map: maplibregl.Map | null = null;
  let filterMarker: maplibregl.Marker | null = null;
  let allAerialData: AerialCollection | null = null;
  let config: FromAboveConfig = {};

  function setAerialData(data: AerialCollection): void {
    map?.getSource<maplibregl.GeoJSONSource>(SOURCE_ID)?.setData(data);
  }

  async function fetchAerialsAtPoint(
    lat: number,
    lon: number,
  ): Promise<AerialCollection | null> {
    if (!config.aerialAtPointUrl) return null;
    const params = new URLSearchParams({ lat: String(lat), lon: String(lon) });
    const response = await fetch(`${config.aerialAtPointUrl}?${params}`);
    if (!response.ok) throw new Error("Failed to fetch aerials at point");
    return (await response.json()) as AerialCollection;
  }

  function placeFilterMarker(lat: number, lon: number): void {
    if (!map) return;
    filterMarker?.remove();
    filterMarker = new maplibregl.Marker({ element: filterMarkerElement() })
      .setLngLat([lon, lat])
      .addTo(map);
  }

  function removeFilterMarker(): void {
    filterMarker?.remove();
    filterMarker = null;
  }

  async function loadAerialData(): Promise<void> {
    if (!map || !config.aerialGeojsonUrl) return;

    const response = await fetch(config.aerialGeojsonUrl);
    if (!response.ok) throw new Error("Failed to fetch aerial georeferences");
    allAerialData = (await response.json()) as AerialCollection;

    map.addSource(SOURCE_ID, { type: "geojson", data: allAerialData });
    map.addLayer({
      id: FILL_LAYER_ID,
      type: "fill",
      source: SOURCE_ID,
      paint: { "fill-color": primaryColor, "fill-opacity": 0.2 },
    });
    map.addLayer({
      id: "aerial-outline",
      type: "line",
      source: SOURCE_ID,
      paint: { "line-color": primaryColor, "line-width": 2 },
    });
  }

  return {
    error: null,
    isFiltered: false,
    isFiltering: false,

    initMap() {
      const container = document.getElementById("aerial-map");
      if (!container) return;

      try {
        config = JSON.parse(container.dataset.config || "{}") as FromAboveConfig;
      } catch (error) {
        console.error("Invalid From Above map config:", error);
        this.error = "Failed to initialize map";
        return;
      }

      if (!config.aerialGeojsonUrl) {
        this.error = "Aerial GeoJSON URL not provided in configuration";
        return;
      }

      window.setupPMTilesProtocol();
      const newMap = new maplibregl.Map({
        container,
        style: initialMapStyle(),
        ...initialMapView(),
      });
      map = newMap;
      // No LayerControl here, so attach the base layer's image handler directly
      void styleImageHandlers(newMap).use(initialPrimaryLayer());
      newMap.addControl(new maplibregl.NavigationControl());
      newMap.addControl(new maplibregl.FullscreenControl());

      newMap.on("load", async () => {
        try {
          await loadAerialData();
        } catch (error) {
          console.error("Error loading aerial georeferences:", error);
          this.error = "Error loading map data";
          return;
        }

        newMap.on("click", (e) => {
          this.applyPointFilter(e.lngLat.lat, e.lngLat.lng);
        });
        newMap.on("mouseenter", FILL_LAYER_ID, () => {
          newMap.getCanvas().style.cursor = "pointer";
        });
        newMap.on("mouseleave", FILL_LAYER_ID, () => {
          newMap.getCanvas().style.cursor = "";
        });

        // Arrived on an already-filtered URL: the server rendered the
        // filtered grid, so only the map needs to catch up.
        const { filterLat, filterLon } = config;
        if (filterLat !== undefined && filterLon !== undefined) {
          this.isFiltered = true;
          placeFilterMarker(filterLat, filterLon);
          try {
            const filtered = await fetchAerialsAtPoint(filterLat, filterLon);
            if (filtered) setAerialData(filtered);
          } catch (error) {
            console.error("Error loading filtered map data:", error);
          }
        }
      });
    },

    async applyPointFilter(lat, lon) {
      this.isFiltering = true;
      this.isFiltered = true;
      placeFilterMarker(lat, lon);

      const params = new URLSearchParams(window.location.search);
      params.set("lat", lat.toFixed(6));
      params.set("lon", lon.toFixed(6));
      params.delete("page");
      const url = `${window.location.pathname}?${params}`;
      window.history.replaceState({}, "", url);

      try {
        swapPageContent(await fetchPageContent(url));
        const filtered = await fetchAerialsAtPoint(lat, lon);
        if (filtered) setAerialData(filtered);
      } catch (error) {
        console.error("Error filtering aerials:", error);
        this.error = "Error filtering aerials. Please try again.";
      } finally {
        this.isFiltering = false;
      }
    },

    async clearFilter() {
      this.isFiltering = true;
      this.isFiltered = false;
      removeFilterMarker();
      window.history.replaceState({}, "", window.location.pathname);

      try {
        swapPageContent(await fetchPageContent(window.location.pathname));
        if (allAerialData) setAerialData(allAerialData);
        this.isFiltering = false;
      } catch (error) {
        console.error("Error clearing filter:", error);
        // Fall back to a full reload of the unfiltered page.
        window.location.href = window.location.pathname;
      }
    },
  };
});
