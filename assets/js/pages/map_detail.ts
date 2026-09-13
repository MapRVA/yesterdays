// Entry point for the map layer preview page (templates/maps/map_detail.html):
// a single MapLayer drawn over the Protomaps basemap, plus the two widgets in
// the header card (source domain, IIIF manifest copy button).
import "../../styles/pages/map-detail.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import {
  onColorSchemeChange,
  protomapsStyleUrl,
} from "../components/map_display/basemap";
import {
  addOverlayLayer,
  retintBaseMap,
} from "../components/map_display/layer_preview";
import "../components/map_display/pmtiles_protocol";
import { initialMapView } from "../constants/map";

const PREVIEW_ZOOM = 13;
const COPIED_REVERT_MS = 1500;

// The source link is labelled with the bare hostname rather than the full URL.
function showSourceDomain(): void {
  const sourceLink = document.querySelector<HTMLElement>("[data-source-url]");
  const domainSpan = sourceLink?.querySelector(".source-domain");
  if (!sourceLink || !domainSpan) return;

  const sourceUrl = sourceLink.dataset.sourceUrl ?? "";
  try {
    domainSpan.textContent = new URL(sourceUrl).hostname.replace(/^www\./, "");
  } catch (e) {
    console.error("Error parsing source URL:", e);
    domainSpan.textContent = sourceUrl;
  }
}

function setupIiifCopyButton(): void {
  const button = document.querySelector<HTMLElement>("[data-iiif-copy]");
  const manifestUrl = button?.dataset.iiifCopy;
  if (!button || !manifestUrl) return;

  const originalHtml = button.innerHTML;
  let revertTimer: ReturnType<typeof setTimeout> | undefined;

  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(manifestUrl);
      clearTimeout(revertTimer);
      button.innerHTML = '<i class="fas fa-check me-1"></i>Copied!';
      revertTimer = setTimeout(() => {
        button.innerHTML = originalHtml;
      }, COPIED_REVERT_MS);
    } catch (e) {
      console.error("Could not copy IIIF manifest URL:", e);
      window.showAlert("danger", "Could not copy the IIIF manifest URL.");
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  showSourceDomain();
  setupIiifCopyButton();

  const container = document.getElementById("layer-map");
  const layer = window.MAP_LAYER;
  if (!container || !layer) return;

  window.setupPMTilesProtocol();

  // A "style" layer is a basemap in its own right, so it replaces the
  // Protomaps style instead of being drawn over it — and there is then no
  // Protomaps layer left to retint when the colour scheme changes.
  const isStyleLayer = layer.type === "style";

  const map = new maplibregl.Map({
    container,
    style: isStyleLayer ? layer.url : protomapsStyleUrl(),
    ...initialMapView({ zoom: PREVIEW_ZOOM }),
  });

  map.addControl(new maplibregl.NavigationControl());
  map.addControl(new maplibregl.FullscreenControl());

  if (!isStyleLayer) {
    map.on("load", () => addOverlayLayer(map, layer));
    onColorSchemeChange(() => retintBaseMap(map));
  }
});
