// Shared popup builder for map image features. Used by the map_display
// component and the georeference interface.
import type { MapGeoJSONFeature } from "maplibre-gl";

// The image-point properties a popup renders. Vector tile features type their
// properties as `any`, so this shape is asserted at the boundary in
// buildPopupWrapper().
interface ImagePopupProperties {
  id: string | number;
  thumbnail?: string | null;
  original_date?: string | null;
}

interface ImageApiDetail {
  thumbnail: string | null;
  permalink: string;
  original_date: string | null;
}

// Tiles below MAP_TILE_DETAIL_MIN_ZOOM carry no popup details, so those are
// fetched from the image API, once per image per page load.
const detailCache = new Map<string, Promise<ImagePopupProperties>>();

function loadPopupDetails(
  properties: ImagePopupProperties,
): Promise<ImagePopupProperties> {
  if (properties.thumbnail) return Promise.resolve(properties);
  const key = String(properties.id);
  let pending = detailCache.get(key);
  if (!pending) {
    pending = fetch(`/api/v2/images/${encodeURIComponent(key)}/`)
      .then((response) => {
        if (!response.ok) throw new Error(`Image ${key}: ${response.status}`);
        return response.json() as Promise<ImageApiDetail>;
      })
      .then((image) => ({
        id: properties.id,
        // Same fallback as the tile view's COALESCE(thumbnail, permalink).
        thumbnail: image.thumbnail || image.permalink,
        original_date: image.original_date,
      }));
    // Let a failed lookup be retried on the next click.
    pending.catch(() => detailCache.delete(key));
    detailCache.set(key, pending);
  }
  return pending;
}

// Build DOM content for a single image feature popup.
function buildPopupContent(properties: ImagePopupProperties): HTMLDivElement {
  const imgEntry = window.location.origin + "/" + properties.id + "/";
  const container = document.createElement("div");

  const imgLink = document.createElement("a");
  imgLink.href = imgEntry;

  const img = document.createElement("img");
  img.className = "popup-image";
  imgLink.appendChild(img);
  container.appendChild(imgLink);

  const footer = document.createElement("div");
  footer.className = "d-flex align-items-center justify-content-between mt-1";

  const link = document.createElement("a");
  link.href = imgEntry;
  link.className = "btn btn-primary btn-sm";
  link.innerHTML = '<i class="fas fa-eye me-1"></i>View';
  footer.appendChild(link);

  container.appendChild(footer);

  loadPopupDetails(properties)
    .then((details) => {
      if (details.thumbnail) img.src = details.thumbnail;
      if (details.original_date) {
        const dateSpan = document.createElement("span");
        dateSpan.className = "text-muted";
        const icon = document.createElement("i");
        icon.className = "fas fa-calendar me-1";
        dateSpan.append(icon, details.original_date);
        footer.prepend(dateSpan);
      }
    })
    .catch(() => {
      // The View link still works; just go without a preview.
      img.remove();
    });

  return container;
}

// Deduplicate an array of map features by properties.id. A single click can
// return the same image from more than one layer.
export function deduplicateFeatures(
  features: MapGeoJSONFeature[],
): MapGeoJSONFeature[] {
  const seen = new Set<unknown>();
  return features.filter((f) => {
    const id = f.properties.id;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

// Build a popup DOM wrapper with navigation for multiple features.
// For a single feature, returns just the content with no nav row.
export function buildPopupWrapper(
  features: MapGeoJSONFeature[],
): HTMLDivElement {
  const wrapper = document.createElement("div");
  const contentSlot = document.createElement("div");

  let currentIndex = 0;

  let prevBtn: HTMLButtonElement | null = null;
  let nextBtn: HTMLButtonElement | null = null;
  let navLabel: HTMLSpanElement | null = null;

  if (features.length > 1) {
    const navRow = document.createElement("div");
    navRow.className = "popup-nav-row d-flex align-items-center gap-2 mb-1";

    prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "btn btn-sm btn-outline-secondary py-0 px-1";
    prevBtn.innerHTML = '<i class="fas fa-chevron-left"></i>';

    nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "btn btn-sm btn-outline-secondary py-0 px-1";
    nextBtn.innerHTML = '<i class="fas fa-chevron-right"></i>';

    navLabel = document.createElement("span");
    navLabel.className = "text-muted small";

    navRow.appendChild(prevBtn);
    navRow.appendChild(navLabel);
    navRow.appendChild(nextBtn);
    wrapper.appendChild(navRow);
  }

  wrapper.appendChild(contentSlot);

  const showFeature = (index: number) => {
    const feature = features[index];
    if (!feature) return;

    currentIndex = index;
    contentSlot.replaceChildren(
      buildPopupContent(feature.properties as ImagePopupProperties),
    );
    if (navLabel) {
      navLabel.textContent = index + 1 + " of " + features.length;
    }
    if (prevBtn) {
      prevBtn.disabled = index === 0;
    }
    if (nextBtn) {
      nextBtn.disabled = index === features.length - 1;
    }
  };

  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      if (currentIndex > 0) showFeature(currentIndex - 1);
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", () => {
      if (currentIndex < features.length - 1) showFeature(currentIndex + 1);
    });
  }

  showFeature(0);

  return wrapper;
}
