import "@geoman-io/maplibre-geoman-free/dist/maplibre-geoman.css";

import { Geoman } from "@geoman-io/maplibre-geoman-free";
import type {
  FeatureCreatedFwdEvent,
  FeatureData,
  FeatureEditEndFwdEvent,
  FeatureRemovedFwdEvent,
} from "@geoman-io/maplibre-geoman-free";
import type { Polygon } from "geojson";
import type { Map } from "maplibre-gl";

export type RegionBounds = [number, number, number, number];

/** One rectangular region bound edited with MapLibre Geoman. */
export class RegionBoundsEditor {
  private gm: Geoman;
  private feature: FeatureData | null = null;
  private disposed = false;
  private busy = false;
  ready = false;

  constructor(
    private map: Map,
    polygon: Polygon | null,
    private onChange: (bounds: RegionBounds | null) => void,
    private onError: (message: string) => void,
  ) {
    this.gm = new Geoman(map, {
      settings: { controlsPosition: "top-left", controlsUiEnabledByDefault: false },
      controls: {
        draw: {
          rectangle: { uiEnabled: true, title: "Draw rectangle" },
          marker: { uiEnabled: false },
          circle_marker: { uiEnabled: false },
          text_marker: { uiEnabled: false },
          circle: { uiEnabled: false },
          ellipse: { uiEnabled: false },
          line: { uiEnabled: false },
          polygon: { uiEnabled: false },
        },
        edit: {
          change: { uiEnabled: true },
          drag: { uiEnabled: true },
          delete: { uiEnabled: true },
          rotate: { uiEnabled: false },
          scale: { uiEnabled: false },
          copy: { uiEnabled: false },
          cut: { uiEnabled: false },
          split: { uiEnabled: false },
          union: { uiEnabled: false },
          difference: { uiEnabled: false },
          line_simplification: { uiEnabled: false },
          lasso: { uiEnabled: false },
        },
        helper: { snapping: { uiEnabled: false, active: false } },
      },
    });
    void this.initialize(polygon);
  }

  private async initialize(polygon: Polygon | null): Promise<void> {
    try {
      await this.gm.waitForGeomanLoaded();
      if (this.disposed) return;
      if (polygon) {
        this.feature = await this.gm.features.importGeoJsonFeature({
          type: "Feature",
          geometry: polygon,
          properties: { shape: "rectangle" },
        });
        if (!this.feature) throw new Error("Could not import the region bounds");
      }
      if (this.disposed) return;
      this.map.on("gm:create", this.created);
      this.map.on("gm:remove", this.removed);
      this.map.on("gm:editend", this.edited);
      this.map.on("gm:dragend", this.edited);
      this.ready = true;
      this.raiseLayers();
    } catch (error) {
      if (!this.disposed) {
        console.error("Region bounds editor initialization failed:", error);
        this.onError("The rectangle tools could not load. Reload the page to try again.");
      }
    }
  }

  private created = async (event: FeatureCreatedFwdEvent): Promise<void> => {
    if (this.disposed || event.shape !== "rectangle") return;
    const previous = this.feature;
    this.feature = event.feature;
    this.busy = true;
    this.sync();
    try {
      if (previous && previous.id !== event.feature.id) {
        await this.gm.features.delete(previous);
      }
    } catch (error) {
      console.error("Could not replace region bounds:", error);
      this.ready = false;
      this.onError("The previous rectangle could not be removed. Reload before saving.");
    } finally {
      this.busy = false;
    }
  };

  private removed = (event: FeatureRemovedFwdEvent): void => {
    if (event.feature.id !== this.feature?.id) return;
    this.feature = null;
    this.onChange(null);
  };

  private edited = (event: FeatureEditEndFwdEvent): void => {
    if (event.feature.id !== this.feature?.id) return;
    this.feature = event.feature;
    this.sync();
  };

  sync(): void {
    if (this.disposed || !this.feature) return;
    const geometry = this.feature.getGeoJson().geometry;
    const ring =
      geometry.type === "Polygon"
        ? geometry.coordinates[0]
        : geometry.type === "MultiPolygon" && geometry.coordinates.length === 1
          ? geometry.coordinates[0]?.[0]
          : null;
    if (!ring?.length) {
      this.onError("Draw one rectangle for these bounds.");
      return;
    }

    const longitudes = ring.map((coordinate) => coordinate[0]!);
    const latitudes = ring.map((coordinate) => coordinate[1]!);
    const bounds: RegionBounds = [
      Math.min(...longitudes),
      Math.min(...latitudes),
      Math.max(...longitudes),
      Math.max(...latitudes),
    ];
    if (
      bounds.every(Number.isFinite) &&
      bounds[0] >= -180 &&
      bounds[2] <= 180 &&
      bounds[1] >= -90 &&
      bounds[3] <= 90 &&
      bounds[0] < bounds[2] &&
      bounds[1] < bounds[3]
    ) {
      this.onChange(bounds);
    } else {
      this.onError("Draw a non-empty rectangle within valid longitude and latitude limits.");
    }
  }

  canSubmit(): boolean {
    return this.ready && !this.busy && this.gm.getActiveDrawModes().length === 0;
  }

  raiseLayers(): void {
    for (const layer of this.map.getStyle()?.layers ?? []) {
      if (layer.id.startsWith("gm_")) this.map.moveLayer(layer.id);
    }
  }

  async destroy(): Promise<void> {
    this.disposed = true;
    this.ready = false;
    this.map.off("gm:create", this.created);
    this.map.off("gm:remove", this.removed);
    this.map.off("gm:editend", this.edited);
    this.map.off("gm:dragend", this.edited);
    await this.gm.destroy();
  }
}
