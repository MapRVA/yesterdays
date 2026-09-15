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

/** One editable footprint. Geometry is owned by the form, not the map style. */
export class LayerExtentEditor {
  private gm: Geoman;
  private feature: FeatureData | null = null;
  private disposed = false;
  private busy = false;
  ready = false;

  constructor(
    private map: Map,
    polygon: Polygon | null,
    private onChange: (polygon: Polygon | null) => void,
    private onError: (message: string) => void,
  ) {
    this.gm = new Geoman(map, {
      settings: { controlsPosition: "top-left", controlsUiEnabledByDefault: false },
      controls: {
        draw: {
          polygon: { uiEnabled: true, title: "Draw layer extent (one polygon)" },
          marker: { uiEnabled: false },
          circle_marker: { uiEnabled: false },
          text_marker: { uiEnabled: false },
          circle: { uiEnabled: false },
          ellipse: { uiEnabled: false },
          line: { uiEnabled: false },
          rectangle: { uiEnabled: false },
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
          type: "Feature", geometry: polygon, properties: { shape: "polygon" },
        });
        if (!this.feature) throw new Error("Could not import the layer extent");
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
        console.error("Extent editor initialization failed:", error);
        this.onError("The extent tools could not load. Reload the page to try again.");
      }
    }
  }

  private created = async (event: FeatureCreatedFwdEvent): Promise<void> => {
    if (this.disposed || event.shape !== "polygon") return;
    const previous = this.feature;
    this.feature = event.feature;
    this.busy = true;
    this.sync();
    try {
      if (previous && previous.id !== event.feature.id) {
        await this.gm.features.delete(previous);
      }
    } catch (error) {
      console.error("Could not replace extent:", error);
      this.ready = false;
      this.onError("The previous outline could not be removed. Reload before saving.");
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
    if (this.disposed) return;
    const geometry = this.feature?.getGeoJson().geometry;
    if (geometry?.type === "Polygon") {
      this.onChange(geometry);
    } else if (geometry?.type === "MultiPolygon" && geometry.coordinates.length === 1) {
      // Geoman 0.7 emits a one-part MultiPolygon when drawing a polygon.
      this.onChange({ type: "Polygon", coordinates: geometry.coordinates[0]! });
    } else if (geometry) {
      this.onChange(null);
      this.onError("Draw a single polygon for this layer’s extent.");
    }
  }

  canSubmit(): boolean {
    return this.ready && !this.busy;
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
