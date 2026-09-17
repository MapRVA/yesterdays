/**
 * LayerControl - A shared MapLibre control for switching between base layers
 * and overlays.
 *
 * Base layers and the discovery tileset come from window.MAP_LAYERS_DATA.
 * Collection overlays are fetched as the camera changes; the active overlay
 * remains available regardless of its extent or minimum zoom.
 *
 * Usage:
 *   import { LayerControl } from "../components/layer_control";
 *
 *   // Basic usage (for georeference interfaces)
 *   map.addControl(new LayerControl(), "top-right");
 *
 *   // With image layer toggle (for map_display)
 *   map.addControl(new LayerControl({
 *     showImageLayerToggle: true,
 *     overlayLayerIds: OVERLAY_LAYER_IDS,
 *     beforeLayerId: LAYER_IDS.imageHeatmap,
 *   }), "top-right");
 */

import type {
  IControl,
  Map as MapLibreMap,
  StyleSpecification,
} from "maplibre-gl";
import "../../../styles/components/layer-control.css";
import {
  hideRasterBaseLayers,
  moveBaseLayerBelowOverlays,
  setDefaultStyleLayersVisibility,
  setRasterBaseLayerVisibility,
  setupRasterBaseLayer,
} from "./base_layers";
import {
  DEFAULT_SIMPLE_RADIUS,
  updateImageLayerVisibility,
  updateSimpleCircleRadius,
} from "./image_layers";
import {
  buildImageLayerPanel,
  type ImageLayerPanel,
} from "./image_layer_panel";
import {
  initialMapStyle,
  initialPrimaryLayer,
  rasterBaseLayerIds,
} from "./initial_style";
import { findBeforeLayerId, isOverlayLayerId } from "./layer_classification";
import {
  createLayerOffcanvas,
  populateBaseLayerButtons,
  populateCollectionSubmenus,
  showLayerLoadError,
  type LayerOffcanvas,
} from "./offcanvas";
import {
  addOverlayLayer,
  addOverlaySource,
  ensurePMTilesProtocol,
  hideOtherOverlayLayers,
} from "./overlay_layers";
import { showSwapIndicator } from "./swap_indicator";
import {
  TileOverlays,
  withActiveOverlay,
  type OverlayLoadState,
} from "./tile_overlays";
import type {
  BaseLayer,
  ImageDisplayStyle,
  LayerCollectionData,
  LayerControlOptions,
  OverlayLayerConfig,
  PrimaryLayerData,
  ResolvedLayerControlOptions,
} from "./types";

export { initialMapStyle } from "./initial_style";

export class LayerControl implements IControl {
  private readonly options: ResolvedLayerControlOptions;

  private baseLayers: Record<string, BaseLayer> = {};
  private collections: LayerCollectionData[] = [];
  private tileOverlays: TileOverlays | undefined;
  private overlayLoadState: OverlayLoadState = "loading";
  private collectionContainer!: HTMLElement;
  private currentBaseLayer: string | null = null;
  private currentOverlay: OverlayLayerConfig | null = null;
  private readonly initialStyle: string | StyleSpecification;
  // True while a non-initial MapLibre style is loaded, so the next base layer
  // change knows it has to swap the initial style back in first
  private nonDefaultStyleActive = false;

  private imageLayersVisible = true;
  private imageDisplayStyle: ImageDisplayStyle = "heatmap";
  private simpleCircleRadius = DEFAULT_SIMPLE_RADIUS;
  // True from the moment a style swap starts until the map settles, so the
  // control button reports the swap instead of the layer it is switching to
  private swapping = false;

  private map: MapLibreMap | undefined;
  private mapId = "";
  // Assigned in onAdd(), which MapLibre calls before anything else
  private container!: HTMLElement;
  private layerLabel!: HTMLElement;
  private triggerButton!: HTMLButtonElement;
  private offcanvas!: LayerOffcanvas;
  private imagePanel: ImageLayerPanel | null = null;
  private fullscreenChangeHandler: (() => void) | null = null;

  constructor(options: LayerControlOptions = {}) {
    this.initialStyle = initialMapStyle();
    this.options = {
      showImageLayerToggle: options.showImageLayerToggle ?? false,
      overlayLayerIds: options.overlayLayerIds ?? [],
      beforeLayerId: options.beforeLayerId ?? null,
      onBaseLayerChange: options.onBaseLayerChange ?? null,
      onStyleSwap: options.onStyleSwap ?? null,
    };
  }

  onAdd(map: MapLibreMap): HTMLElement {
    this.map = map;
    this.mapId = map.getContainer().id;

    this.container = document.createElement("div");
    this.container.className =
      "maplibregl-ctrl maplibregl-ctrl-group layer-control";
    // Both icons are present in the markup and swapped by CSS on aria-busy:
    // Font Awesome replaces these <i> elements with <svg> nodes, so toggling
    // icon classes from JS would fight that replacement.
    this.container.innerHTML = `
      <button type="button" class="layer-control-button" aria-label="Map layers" aria-busy="false">
        <i class="fas fa-layer-group layer-control-icon"></i>
        <i class="fas fa-spinner fa-spin layer-control-busy-icon"></i>
        <span class="layer-control-label"></span>
      </button>
    `;

    // The elements always exist: the markup was assigned via innerHTML above.
    this.layerLabel = this.container.querySelector<HTMLElement>(
      ".layer-control-label",
    )!;
    this.triggerButton = this.container.querySelector<HTMLButtonElement>(
      ".layer-control-button",
    )!;

    this.offcanvas = createLayerOffcanvas(this.mapId);
    this.triggerButton.addEventListener("click", () => {
      this.offcanvas.instance.show();
    });

    // Keep the offcanvas reachable when the map goes fullscreen
    this.fullscreenChangeHandler = () => this.handleFullscreenChange();
    document.addEventListener("fullscreenchange", this.fullscreenChangeHandler);

    if (map.loaded()) {
      this.initializeLayers();
    } else {
      map.once("load", this.initializeLayers);
    }

    this.renderLayerLabel();

    return this.container;
  }

  onRemove(): void {
    this.map?.off("load", this.initializeLayers);
    this.tileOverlays?.destroy();
    if (this.fullscreenChangeHandler) {
      document.removeEventListener(
        "fullscreenchange",
        this.fullscreenChangeHandler,
      );
      this.fullscreenChangeHandler = null;
    }

    this.offcanvas.instance.dispose();
    this.offcanvas.element.remove();
    this.container.remove();
    this.map = undefined;
  }

  /**
   * Apply all control-owned image layer state. Called whenever map_display has
   * added or recreated its layers.
   */
  applyImageLayerState(): void {
    if (!this.map) return;
    updateImageLayerVisibility(
      this.map,
      this.imageLayersVisible,
      this.imageDisplayStyle,
    );
    updateSimpleCircleRadius(this.map, this.simpleCircleRadius);
  }

  private handleFullscreenChange(): void {
    if (!this.map) return;

    const mapContainer = this.map.getContainer();
    if (document.fullscreenElement === mapContainer) {
      // Map is now fullscreen - move offcanvas inside the map container
      mapContainer.appendChild(this.offcanvas.element);
    } else if (!document.fullscreenElement) {
      // Exited fullscreen - move offcanvas back to document.body
      document.body.appendChild(this.offcanvas.element);
    }
  }

  private initializeLayers = (): void => {
    const data = window.MAP_LAYERS_DATA;
    if (!data) {
      console.error("MAP_LAYERS_DATA not found on window");
      showLayerLoadError(this.offcanvas.overlayContainer);
      return;
    }

    if (data.primary_layers && data.primary_layers.length > 0) {
      this.buildBaseLayers(data.primary_layers);
    }

    populateBaseLayerButtons(
      this.offcanvas.baseLayersList,
      this.baseLayers,
      this.currentBaseLayer,
      (layerKey) => {
        if (layerKey === this.currentBaseLayer) return;
        this.switchToBaseLayer(layerKey);
        this.offcanvas.instance.hide();
      },
    );

    this.collectionContainer = document.createElement("div");
    this.offcanvas.overlayContainer.appendChild(this.collectionContainer);
    this.populateOverlays();

    // Image controls are built once, so refreshing collection choices preserves
    // the display style, point size, and keyboard focus in this separate panel.
    if (this.options.showImageLayerToggle) {
      this.imagePanel = buildImageLayerPanel(this.offcanvas.overlayContainer, {
        mapId: this.mapId,
        initialRadius: this.simpleCircleRadius,
        onToggleImageLayers: () => {
          this.toggleImageLayers();
          this.offcanvas.instance.hide();
        },
        onStyleChange: (style) => this.setImageDisplayStyle(style),
        onRadiusChange: (radius) => this.setSimpleCircleRadius(radius),
      });
    }
    if (this.map && data.overlay_tiles) {
      this.tileOverlays = new TileOverlays(
        this.map,
        data.overlay_tiles,
        (collections, state) => {
          this.collections = collections;
          this.overlayLoadState = state;
          this.populateOverlays();
        },
      );
    } else {
      console.error(
        "MAP_LAYERS_DATA.overlay_tiles is missing; layer discovery is disabled",
      );
      this.overlayLoadState = "error";
      this.populateOverlays();
    }
    this.renderLayerLabel();
  };

  /**
   * Build base layers from the primary_layers payload. The initial style-type
   * layer is shown and hidden in place; other style-type layers swap the map
   * style, and raster layers get their own source and layer.
   */
  private buildBaseLayers(primaryLayers: PrimaryLayerData[]): void {
    const initialLayer = initialPrimaryLayer({ primary_layers: primaryLayers });

    for (const layer of primaryLayers) {
      const key = layer.slug;
      const isInitial = layer.slug === initialLayer?.slug;

      if (layer.type === "style" && isInitial) {
        this.baseLayers[key] = {
          name: layer.name,
          isDefault: true,
          type: "style",
          url: layer.url,
          setupLayer: () => {},
          activate: () => this.showDefaultStyleLayers(),
          deactivate: () => this.setDefaultStyleLayersVisible(false),
        };
        this.currentBaseLayer = key;
      } else if (layer.type === "style") {
        this.baseLayers[key] = {
          name: layer.name,
          isDefault: false,
          type: "style",
          url: layer.url,
          setupLayer: () => {},
          activate: () => this.activateStyle(layer.url),
          deactivate: () => {},
        };
      } else {
        const tileType = layer.type;
        const { sourceId, layerId } = rasterBaseLayerIds(key);
        this.baseLayers[key] = {
          name: layer.name,
          isDefault: isInitial,
          type: tileType,
          sourceId,
          layerId,
          setupLayer: () =>
            this.setupRasterBase(
              sourceId,
              layerId,
              layer.url,
              layer.attribution || "",
              tileType,
            ),
          activate: () => this.activateRasterBase(layerId),
          deactivate: () => this.deactivateRasterBase(layerId),
        };
        if (isInitial) this.currentBaseLayer = key;
      }
    }

    for (const baseLayer of Object.values(this.baseLayers)) {
      baseLayer.setupLayer();
    }
  }

  private populateOverlays(): void {
    const container = this.collectionContainer;
    populateCollectionSubmenus(
      container,
      withActiveOverlay(this.collections, this.currentOverlay),
      (overlay) => {
        this.switchToOverlayLayer(overlay);
        this.offcanvas.instance.hide();
      },
    );

    // With no collection layers in view the menu only offers base layers, so
    // there is nothing to say; only loading and failures get a status line.
    if (this.overlayLoadState !== "ready") {
      const message = document.createElement("p");
      message.className = "text-muted small mt-3";
      message.setAttribute("role", "status");
      message.textContent = this.overlayLoadState === "loading"
        ? "Loading layers in view…"
        : "Error fetching additional layers";
      container.appendChild(message);
    }

    // Separate this section from the base layers only when it has content.
    if (container.firstChild) {
      const divider = document.createElement("hr");
      divider.className = "my-3 border-2 opacity-50";
      container.prepend(divider);
    }
    this.updateSelection();
  }

  private switchToBaseLayer(newLayerKey: string): void {
    const newLayer = this.baseLayers[newLayerKey];
    if (!newLayer || newLayerKey === this.currentBaseLayer) return;

    const currentLayer = this.currentBaseLayer
      ? this.baseLayers[this.currentBaseLayer]
      : undefined;
    currentLayer?.deactivate();
    newLayer.activate();
    this.currentBaseLayer = newLayerKey;

    this.updateSelection();
    this.options.onBaseLayerChange?.(newLayerKey);
  }

  private isOverlay = (layerId: string): boolean =>
    isOverlayLayerId(layerId, this.options.overlayLayerIds);

  // Layers belonging to the map's own style: everything that is neither an
  // overlay nor one of our raster base layers.
  private isDefaultStyleLayer = (layerId: string): boolean =>
    !this.isOverlay(layerId) && !this.isCustomBaseLayer(layerId);

  private isCustomBaseLayer(layerId: string): boolean {
    return Object.values(this.baseLayers).some(
      (baseLayer) =>
        baseLayer.type !== "style" && baseLayer.layerId === layerId,
    );
  }

  private setupRasterBase(
    sourceId: string,
    layerId: string,
    url: string,
    attribution: string,
    tileType: "pmtiles" | "xyz",
  ): void {
    if (!this.map) return;
    setupRasterBaseLayer(
      this.map,
      sourceId,
      layerId,
      url,
      attribution,
      tileType,
    );
  }

  private activateRasterBase(layerId: string): void {
    if (this.nonDefaultStyleActive) {
      // Returning from a non-initial style — restore it, then show the raster
      this.nonDefaultStyleActive = false;
      this.swapStyle(this.initialStyle, () => this.showRasterBase(layerId));
      return;
    }
    this.showRasterBase(layerId);
  }

  private showRasterBase(layerId: string): void {
    const map = this.map;
    if (!map) return;

    this.setDefaultStyleLayersVisible(false);
    hideRasterBaseLayers(map, this.baseLayers, layerId);
    if (map.getLayer(layerId)) {
      moveBaseLayerBelowOverlays(map, layerId, this.isOverlay);
      setRasterBaseLayerVisibility(map, layerId, true);
    }
  }

  private deactivateRasterBase(layerId: string): void {
    if (!this.map) return;
    setRasterBaseLayerVisibility(this.map, layerId, false);
  }

  private showDefaultStyleLayers(): void {
    if (this.nonDefaultStyleActive) {
      // Returning from a non-initial style — restore the initial style
      this.nonDefaultStyleActive = false;
      this.swapStyle(this.initialStyle);
      return;
    }

    if (!this.map) return;
    hideRasterBaseLayers(this.map, this.baseLayers);
    this.setDefaultStyleLayersVisible(true);
  }

  private setDefaultStyleLayersVisible(visible: boolean): void {
    if (!this.map) return;
    setDefaultStyleLayersVisibility(
      this.map,
      visible,
      this.isDefaultStyleLayer,
    );
  }

  private activateStyle(styleUrl: string): void {
    this.nonDefaultStyleActive = true;
    this.swapStyle(styleUrl);
  }

  /**
   * Swap the map style and restore the raster base layers and the active tile
   * overlay, both of which setStyle() destroys.
   */
  private swapStyle(
    style: string | StyleSpecification,
    afterRestore?: () => void,
  ): void {
    const map = this.map;
    if (!map) return;

    // Captured before the swap, since switching resets the overlay state
    const savedOverlay = this.currentOverlay;

    // Rebuilding a style's layers blocks the main thread for seconds on large
    // styles; report that on the button before the freeze starts
    showSwapIndicator(map, this.triggerButton, (swapping) => {
      this.swapping = swapping;
      this.renderLayerLabel();
    });

    map.setStyle(style);

    map.once("style.load", async () => {
      try {
        for (const baseLayer of Object.values(this.baseLayers)) {
          if (baseLayer.type !== "style") baseLayer.setupLayer();
        }

        // Notify consumers to re-add their layers BEFORE restoring the overlay
        // tile layer. This ensures consumer layers (Geoman polygons, hint
        // markers, pins, etc.) exist when switchToOverlayLayer looks for the
        // layer to insert before, so the overlay raster ends up beneath them.
        await this.options.onStyleSwap?.(map);
      } catch (error) {
        console.error("Failed to restore layers after style swap:", error);
      } finally {
        try {
          this.applyImageLayerState();
        } catch (error) {
          console.error("Failed to restore image layer state:", error);
        }

        try {
          if (savedOverlay) {
            // Reset so switchToOverlayLayer re-adds instead of toggling off
            this.currentOverlay = null;
            this.switchToOverlayLayer(savedOverlay);
          }
        } finally {
          afterRestore?.();
        }
      }
    });
  }

  private switchToOverlayLayer(overlay: OverlayLayerConfig): void {
    const map = this.map;
    if (!map) return;

    const { layerId, tileUrl, tileType, attribution } = overlay;

    if (tileType === "pmtiles" && !ensurePMTilesProtocol()) {
      alert(
        "PMTiles map overlay layers are not available - PMTiles protocol not loaded.",
      );
      return;
    }

    // Clicking the active overlay turns it off
    if (this.currentOverlay?.layerId === layerId) {
      map.setLayoutProperty(layerId, "visibility", "none");
      this.currentOverlay = null;
      this.populateOverlays();
      return;
    }

    hideOtherOverlayLayers(map, layerId);

    const sourceId = `${layerId}-source`;
    if (!addOverlaySource(map, sourceId, tileUrl, tileType, attribution)) {
      return;
    }

    if (!map.getLayer(layerId)) {
      const beforeId = findBeforeLayerId(map, this.options.beforeLayerId);
      if (!addOverlayLayer(map, layerId, sourceId, beforeId)) return;
    }

    map.setLayoutProperty(layerId, "visibility", "visible");
    this.currentOverlay = overlay;
    this.populateOverlays();
  }

  private toggleImageLayers(): void {
    this.imageLayersVisible = !this.imageLayersVisible;
    this.applyImageLayerState();
    this.imagePanel?.setImageLayersActive(this.imageLayersVisible);
    this.imagePanel?.setControlsEnabled(this.imageLayersVisible);
  }

  private setImageDisplayStyle(style: ImageDisplayStyle): void {
    this.imageDisplayStyle = style;
    this.applyImageLayerState();
    this.imagePanel?.setRadiusVisible(style === "simple");
  }

  private setSimpleCircleRadius(radius: number): void {
    this.simpleCircleRadius = radius;
    if (!this.map) return;
    updateSimpleCircleRadius(this.map, radius);
  }

  private updateSelection(): void {
    const { element } = this.offcanvas;

    // Reset base layer and overlay selections, but not the independent image
    // layer toggle
    element.querySelectorAll(".layer-option").forEach((item) => {
      item.classList.remove("active");
    });
    element
      .querySelectorAll(
        '.overlay-layer:not([data-layer="georeferenced-images"])',
      )
      .forEach((item) => {
        item.classList.remove("active");
      });

    element
      .querySelector(`.layer-option[data-layer="${this.currentBaseLayer}"]`)
      ?.classList.add("active");

    if (this.currentOverlay) {
      element
        .querySelector(
          `.overlay-layer[data-layer="${this.currentOverlay.layerId}"]`,
        )
        ?.classList.add("active");
    }

    this.renderLayerLabel();
  }

  private renderLayerLabel(): void {
    if (this.swapping) {
      this.layerLabel.textContent = "Switching…";
      return;
    }

    const baseLayerName = this.currentBaseLayer
      ? (this.baseLayers[this.currentBaseLayer]?.name ?? "")
      : "";
    const overlayName = this.currentOverlay?.title;

    this.layerLabel.textContent = overlayName
      ? `${baseLayerName} + ${overlayName}`
      : baseLayerName;
  }
}
