import maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import type { Polygon, Position } from "geojson";
import type { MapLayerType } from "./layer_control/types";
import {
  addOverlayLayer,
  retintBaseMap,
} from "./map_display/layer_preview";

const PICKER_WIDTH = 400;
const PICKER_HEIGHT = 240;
const PICKER_PADDING = 24;
const WORLD_SIZE = 512;
const MAX_MERCATOR_LATITUDE = 85.051129;
const MIN_ZOOM = 0;
const MAX_ZOOM = 24;

const POLYGON_SOURCE_ID = "min-zoom-polygon";
const POLYGON_FILL_ID = "min-zoom-polygon-fill";
const POLYGON_LINE_ID = "min-zoom-polygon-line";

type Center = [number, number];

export interface MinZoomPreviewLayer {
  name: string;
  type: MapLayerType;
  url: string;
  attribution: string;
}

interface MinZoomPickerOptions {
  container: HTMLElement;
  polygon: Polygon;
  minZoom: number | null;
  minZoomWasChosen: boolean;
  style: string;
  previewLayer: MinZoomPreviewLayer | null;
  onChange: (zoom: number) => void;
}

function ringAreaAndCentroid(ring: Position[]): {
  area: number;
  center: Center;
} | null {
  let twiceArea = 0;
  let longitudeTotal = 0;
  let latitudeTotal = 0;

  for (let index = 0; index < ring.length - 1; index++) {
    const current = ring[index];
    const next = ring[index + 1];
    if (!current || !next) continue;
    const cross = current[0]! * next[1]! - next[0]! * current[1]!;
    twiceArea += cross;
    longitudeTotal += (current[0]! + next[0]!) * cross;
    latitudeTotal += (current[1]! + next[1]!) * cross;
  }

  if (Math.abs(twiceArea) < Number.EPSILON) return null;
  return {
    area: Math.abs(twiceArea) / 2,
    center: [
      longitudeTotal / (3 * twiceArea),
      latitudeTotal / (3 * twiceArea),
    ],
  };
}

/** Area-weighted polygon centroid, subtracting any interior rings. */
export function polygonCentroid(polygon: Polygon): Center {
  let weightedLongitude = 0;
  let weightedLatitude = 0;
  let totalArea = 0;

  polygon.coordinates.forEach((ring, index) => {
    const result = ringAreaAndCentroid(ring);
    if (!result) return;
    const weight = index === 0 ? result.area : -result.area;
    weightedLongitude += result.center[0] * weight;
    weightedLatitude += result.center[1] * weight;
    totalArea += weight;
  });

  if (totalArea > Number.EPSILON) {
    return [weightedLongitude / totalArea, weightedLatitude / totalArea];
  }

  const outerRing = polygon.coordinates[0] ?? [];
  const vertices = outerRing.slice(0, -1);
  if (vertices.length === 0) return [0, 0];
  return [
    vertices.reduce((total, point) => total + point[0]!, 0) / vertices.length,
    vertices.reduce((total, point) => total + point[1]!, 0) / vertices.length,
  ];
}

function project([longitude, latitude]: Center): Center {
  const clampedLatitude = Math.max(
    -MAX_MERCATOR_LATITUDE,
    Math.min(MAX_MERCATOR_LATITUDE, latitude),
  );
  const latitudeRadians = (clampedLatitude * Math.PI) / 180;
  return [
    (longitude + 180) / 360,
    0.5 -
      Math.log(
        (1 + Math.sin(latitudeRadians)) /
          (1 - Math.sin(latitudeRadians)),
      ) /
        (4 * Math.PI),
  ];
}

/** Highest integer zoom where the polygon fits around its locked centroid. */
export function fittedMinZoom(polygon: Polygon, center: Center): number {
  const [centerX, centerY] = project(center);
  let maxXDistance = 0;
  let maxYDistance = 0;

  for (const ring of polygon.coordinates) {
    for (const coordinate of ring) {
      const [x, y] = project([coordinate[0]!, coordinate[1]!]);
      const xDistance = Math.abs(x - centerX);
      maxXDistance = Math.max(maxXDistance, Math.min(xDistance, 1 - xDistance));
      maxYDistance = Math.max(maxYDistance, Math.abs(y - centerY));
    }
  }

  const halfWidth = (PICKER_WIDTH - 2 * PICKER_PADDING) / 2;
  const halfHeight = (PICKER_HEIGHT - 2 * PICKER_PADDING) / 2;
  const limits: number[] = [];
  if (maxXDistance > 0) {
    limits.push(Math.log2(halfWidth / (WORLD_SIZE * maxXDistance)));
  }
  if (maxYDistance > 0) {
    limits.push(Math.log2(halfHeight / (WORLD_SIZE * maxYDistance)));
  }
  const zoom = limits.length > 0 ? Math.floor(Math.min(...limits)) : MAX_ZOOM;
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
}

export class LayerMinZoomPicker {
  private map: MapLibreMap | null = null;
  private polygon: Polygon;
  private center: Center;
  private zoom: number;
  private zoomWasChosen: boolean;
  private style: string;
  private previewLayer: MinZoomPreviewLayer | null;
  private programmaticMove = false;
  private disposed = false;

  constructor(private options: MinZoomPickerOptions) {
    this.polygon = options.polygon;
    this.center = polygonCentroid(options.polygon);
    this.zoomWasChosen = options.minZoomWasChosen;
    this.zoom =
      options.minZoom ?? fittedMinZoom(options.polygon, this.center);
    this.style = options.style;
    this.previewLayer = options.previewLayer;
    this.options.onChange(this.zoom);
    this.buildMap();
  }

  updatePolygon(polygon: Polygon): void {
    this.polygon = polygon;
    this.center = polygonCentroid(polygon);
    if (!this.zoomWasChosen) {
      this.zoom = fittedMinZoom(polygon, this.center);
      this.options.onChange(this.zoom);
    }
    this.updatePolygonSource();
    this.applyCamera();
  }

  updatePreview(style: string, previewLayer: MinZoomPreviewLayer | null): void {
    const unchanged =
      style === this.style &&
      previewLayer?.type === this.previewLayer?.type &&
      previewLayer?.url === this.previewLayer?.url &&
      previewLayer?.attribution === this.previewLayer?.attribution;
    if (unchanged) return;
    this.style = style;
    this.previewLayer = previewLayer;
    this.buildMap();
  }

  retint(): void {
    if (this.map && this.previewLayer) void retintBaseMap(this.map);
  }

  destroy(): void {
    this.disposed = true;
    this.map?.remove();
    this.map = null;
  }

  private buildMap(): void {
    this.map?.remove();
    if (this.disposed) return;

    const map = new maplibregl.Map({
      container: this.options.container,
      style: this.style,
      center: this.center,
      zoom: this.zoom,
      zoomSnap: 1,
      minZoom: MIN_ZOOM,
      maxZoom: MAX_ZOOM,
      dragPan: false,
      dragRotate: false,
      boxZoom: false,
      keyboard: false,
      touchPitch: false,
      pitchWithRotate: false,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(
      new maplibregl.NavigationControl({
        showCompass: false,
        visualizePitch: false,
      }),
      "top-right",
    );
    map.on("zoomstart", () => {
      if (!this.programmaticMove) this.zoomWasChosen = true;
    });
    map.on("zoomend", () => this.handleZoomEnd());
    map.on("load", () => {
      if (this.map !== map) return;
      map.resize();
      if (this.previewLayer) addOverlayLayer(map, this.previewLayer);
      this.addPolygonLayers();
    });
    this.map = map;
  }

  private handleZoomEnd(): void {
    const map = this.map;
    if (!map || this.programmaticMove) return;
    this.zoom = Math.max(
      MIN_ZOOM,
      Math.min(MAX_ZOOM, Math.round(map.getZoom())),
    );
    this.options.onChange(this.zoom);
    this.applyCamera();
  }

  private applyCamera(): void {
    if (!this.map) return;
    this.programmaticMove = true;
    this.map.jumpTo({
      center: this.center,
      zoom: this.zoom,
      bearing: 0,
      pitch: 0,
    });
    this.programmaticMove = false;
  }

  private polygonFeature(): GeoJSON.Feature<Polygon> {
    return {
      type: "Feature",
      properties: {},
      geometry: this.polygon,
    };
  }

  private addPolygonLayers(): void {
    const map = this.map;
    if (!map || map.getSource(POLYGON_SOURCE_ID)) return;
    map.addSource(POLYGON_SOURCE_ID, {
      type: "geojson",
      data: this.polygonFeature(),
    });
    map.addLayer({
      id: POLYGON_FILL_ID,
      type: "fill",
      source: POLYGON_SOURCE_ID,
      paint: { "fill-color": "#0d6efd", "fill-opacity": 0.12 },
    });
    map.addLayer({
      id: POLYGON_LINE_ID,
      type: "line",
      source: POLYGON_SOURCE_ID,
      paint: { "line-color": "#0d6efd", "line-width": 2 },
    });
  }

  private updatePolygonSource(): void {
    const source = this.map?.getSource(POLYGON_SOURCE_ID) as
      | GeoJSONSource
      | undefined;
    source?.setData(this.polygonFeature());
  }
}
