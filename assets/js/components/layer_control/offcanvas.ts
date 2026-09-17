// The offcanvas panel opened by the control button: base layer choices on top,
// collection overlays (and optionally the image layer panel) below.
import type {
  BaseLayer,
  LayerCollectionData,
  OverlayLayerConfig,
} from "./types";

export interface LayerOffcanvas {
  element: HTMLElement;
  instance: BootstrapOffcanvas;
  baseLayersList: HTMLElement;
  overlayContainer: HTMLElement;
}

export function createLayerOffcanvas(mapId: string): LayerOffcanvas {
  const offcanvasId = `layerOffcanvas-${mapId}`;

  // A control re-added to the same map would otherwise leave a stale panel
  document.getElementById(offcanvasId)?.remove();

  const element = document.createElement("div");
  element.className = "offcanvas offcanvas-end";
  element.setAttribute("tabindex", "-1");
  element.id = offcanvasId;
  element.setAttribute("aria-labelledby", `${offcanvasId}-label`);

  element.innerHTML = `
      <div class="offcanvas-header">
        <h5 class="offcanvas-title" id="${offcanvasId}-label">Map Layers</h5>
        <button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button>
      </div>
      <div class="offcanvas-body">
        <h6 class="text-muted small text-uppercase mb-2">Base Layers</h6>
        <div class="list-group list-group-flush base-layers-list"></div>
        <div class="overlay-layers-container"></div>
      </div>
    `;

  document.body.appendChild(element);

  // The containers always exist: the markup was assigned via innerHTML above.
  return {
    element,
    instance: new window.bootstrap.Offcanvas(element),
    baseLayersList: element.querySelector<HTMLElement>(".base-layers-list")!,
    overlayContainer: element.querySelector<HTMLElement>(
      ".overlay-layers-container",
    )!,
  };
}

export function populateBaseLayerButtons(
  baseLayersList: HTMLElement,
  baseLayers: Record<string, BaseLayer>,
  currentBaseLayer: string | null,
  onSelect: (layerKey: string) => void,
): void {
  baseLayersList.innerHTML = "";

  for (const [key, layer] of Object.entries(baseLayers)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className =
      "list-group-item list-group-item-action layer-option" +
      (key === currentBaseLayer ? " active" : "");
    button.dataset.layer = key;
    button.textContent = layer.name;

    button.addEventListener("click", (event) => {
      event.preventDefault();
      onSelect(key);
    });

    baseLayersList.appendChild(button);
  }
}

export function populateCollectionSubmenus(
  overlayContainer: HTMLElement,
  collections: LayerCollectionData[],
  onSelect: (overlay: OverlayLayerConfig) => void,
): void {
  overlayContainer.replaceChildren();

  for (const collection of collections) {
    if (collection.layers.length === 0) continue;

    const header = document.createElement("h6");
    header.className = "text-muted small text-uppercase mb-2 mt-3";
    header.textContent = collection.name;
    overlayContainer.appendChild(header);

    const listGroup = document.createElement("div");
    listGroup.className = "list-group list-group-flush mb-2";

    for (const layer of collection.layers) {
      const overlay: OverlayLayerConfig = {
        layer,
        collection: { id: collection.id, name: collection.name },
        layerId: `overlay-${layer.id}`,
        tileUrl: layer.url,
        title: layer.name,
        tileType: layer.type || "pmtiles",
        attribution: layer.attribution || "",
      };

      const button = document.createElement("button");
      button.type = "button";
      button.className = "list-group-item list-group-item-action overlay-layer";
      button.dataset.layer = overlay.layerId;
      button.textContent = overlay.title;

      button.addEventListener("click", (event) => {
        event.preventDefault();
        onSelect(overlay);
      });

      listGroup.appendChild(button);
    }

    overlayContainer.appendChild(listGroup);
  }
}

export function showLayerLoadError(overlayContainer: HTMLElement): void {
  const errorText = document.createElement("p");
  errorText.className = "text-muted small mt-3";
  errorText.textContent = "Failed to load map layers";
  overlayContainer.appendChild(errorText);
}
