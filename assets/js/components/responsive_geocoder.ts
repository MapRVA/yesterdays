/**
 * ResponsiveGeocoder - A wrapper for MaplibreGeocoder that responds to screen size changes
 *
 * This module provides a geocoder that:
 * - Starts collapsed on small screens (≤1000px)
 * - Starts expanded on larger screens
 * - Automatically recreates itself when crossing the breakpoint to properly update hover behavior
 *
 * Usage:
 *   import { addResponsiveGeocoder } from "./responsive_geocoder";
 *
 *   // With default Nominatim API (the selected region's search box, else worldwide)
 *   addResponsiveGeocoder(map);
 *
 *   // With custom geocoder API
 *   addResponsiveGeocoder(map, { geocoderApi: myCustomApi });
 *
 *   // With custom breakpoint
 *   addResponsiveGeocoder(map, { breakpoint: 768 });
 */

import MaplibreGeocoder, {
  type CarmenGeojsonFeature,
  type MaplibreGeocoderApi,
} from "@maplibre/maplibre-gl-geocoder";
import "@maplibre/maplibre-gl-geocoder/dist/maplibre-gl-geocoder.css";
import "../../styles/components/geocoder-overrides.css";
import maplibregl from "maplibre-gl";
import type { ControlPosition, Map as MapLibreMap } from "maplibre-gl";
import type { MapBounds } from "../constants/map";

// The slice of Nominatim's GeoJSON search response this module reads. `bbox`
// is [minX, minY, maxX, maxY]; results without one are skipped.
interface NominatimFeature {
  bbox?: [number, number, number, number];
  properties: { display_name: string; [key: string]: unknown };
}

export interface ResponsiveGeocoderOptions {
  // Custom API (default: Nominatim, bounded to the selected region's box)
  geocoderApi?: MaplibreGeocoderApi;
  // Box the default API restricts results to. Omitted: the selected region's
  // box, else the whole world. null: search the whole world regardless.
  searchBbox?: MapBounds | null;
  // Screen width breakpoint in pixels below which the control starts collapsed
  breakpoint?: number;
  placeholder?: string;
  position?: ControlPosition;
}

export interface ResponsiveGeocoderHandle {
  // Remove the control and stop listening for breakpoint changes
  remove: () => void;
  getGeocoder: () => MaplibreGeocoder;
}

const createDefaultGeocoderApi = (
  email: string | null | undefined,
  bbox: MapBounds | undefined,
): MaplibreGeocoderApi => ({
  forwardGeocode: async (config) => {
    const features: CarmenGeojsonFeature[] = [];
    try {
      const params = new URLSearchParams({
        // `query` is typed for reverse geocoding too, where it is a
        // coordinate pair; this API only ever forward geocodes text.
        q: String(config.query ?? ""),
        format: "geojson",
        polygon_geojson: "1",
        addressdetails: "1",
        layer: "address",
      });
      // With no region selected there is no box: search the whole world
      // rather than sending an empty viewbox.
      if (bbox) {
        params.set("viewbox", bbox.join(","));
        params.set("bounded", "1");
      }
      if (email) {
        params.set("email", email);
      }

      const request = `https://nominatim.openstreetmap.org/search?${params}`;
      const response = await fetch(request);
      const geojson = (await response.json()) as {
        features?: NominatimFeature[];
      };
      for (const feature of geojson.features ?? []) {
        if (!feature.bbox) continue;
        const center: [number, number] = [
          feature.bbox[0] + (feature.bbox[2] - feature.bbox[0]) / 2,
          feature.bbox[1] + (feature.bbox[3] - feature.bbox[1]) / 2,
        ];
        const point: CarmenGeojsonFeature = {
          type: "Feature",
          geometry: { type: "Point", coordinates: center },
          place_name: feature.properties.display_name,
          properties: feature.properties,
          text: feature.properties.display_name,
          place_type: ["place"],
          center,
        };
        features.push(point);
      }
    } catch (e) {
      console.error(`Failed to forwardGeocode with error: ${e}`);
    }
    return { type: "FeatureCollection", features };
  },
});

// Add a responsive geocoder control to a map. Returns a handle for removing it.
export function addResponsiveGeocoder(
  map: MapLibreMap,
  options: ResponsiveGeocoderOptions = {},
): ResponsiveGeocoderHandle {
  const {
    searchBbox = window.SEARCH_BBOX ?? null,
    geocoderApi = createDefaultGeocoderApi(
      window.ADMIN_EMAIL,
      searchBbox ?? undefined,
    ),
    breakpoint = 1000,
    placeholder = "Search places",
    position = "top-left",
  } = options;

  const smallScreenQuery = window.matchMedia(`(max-width: ${breakpoint}px)`);

  const createGeocoder = (collapsed: boolean) => {
    return new MaplibreGeocoder(geocoderApi, {
      maplibregl,
      placeholder,
      collapsed,
    });
  };

  let geocoder = createGeocoder(smallScreenQuery.matches);
  map.addControl(geocoder, position);

  // Fix geocoder search on mobile Chrome
  const fixGeocoderInput = () => {
    const geocoderInput = map
      .getContainer()
      .querySelector<HTMLInputElement>(".maplibregl-ctrl-geocoder--input");
    if (geocoderInput) {
      geocoderInput.type = "search";
    }
  };
  fixGeocoderInput();

  // Recreate geocoder on resize to properly update collapsed behavior
  const handleResize = (e: MediaQueryListEvent) => {
    map.removeControl(geocoder);
    geocoder = createGeocoder(e.matches);
    map.addControl(geocoder, position);
    fixGeocoderInput();
  };

  smallScreenQuery.addEventListener("change", handleResize);

  // Return cleanup function
  return {
    remove: () => {
      smallScreenQuery.removeEventListener("change", handleResize);
      map.removeControl(geocoder);
    },
    getGeocoder: () => geocoder,
  };
}
