// The region picker map: a basemap-only map with one photo pin per
// region, beside (global homepage) or above (/regions/) a list of region
// cards. Pins open a small preview popup with an explicit button, so
// panning the map can't select a region by accident; hovering a card
// highlights its pin and vice versa.
//
// Both pages render templates/partials/global_home_map.html, so both read
// the same inline summaries out of the same json_script and share the
// styles this module carries.
import "../../styles/components/region-map.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import { onColorSchemeChange, protomapsStyleUrl } from "./map_display/basemap";

// Fallback view for the no-regions case, when there is nothing to frame:
// the contiguous US — the geographic center near Lebanon, Kansas, zoomed
// out far enough to take in the whole country.
const FALLBACK_CENTER: [number, number] = [-96, 38];
const FALLBACK_ZOOM = 3.8;

// How the pins are framed on load. The max zoom keeps a lone pin (whose
// bounds are a single point) at a metro-area view instead of maplibre's
// maximum; the padding keeps edge pins clear of the map border while
// staying well under half the map's smallest dimension (256px), which
// is where fitBounds gives up.
const FIT_PADDING = 56;
const FIT_MAX_ZOOM = 8;

// Build the map a screenful before it's reached, so it has settled by the
// time it's actually looked at.
const REGION_MAP_PRELOAD_MARGIN = "600px";

// One region, as serialized by regions.summaries.get_region_summaries.
export interface RegionSummary {
  slug: string;
  short_name: string;
  long_name: string;
  subtitle: string;
  lng: number;
  lat: number;
  image_count: number;
  georeferenced_count: number;
  thumbnail: string | null;
  advertise: boolean;
}

export function readRegionSummaries(): RegionSummary[] {
  const data = document.getElementById("region-summaries-data");
  if (!data?.textContent) return [];
  return JSON.parse(data.textContent) as RegionSummary[];
}

// The server-rendered cards, by slug, for keeping card and pin hover
// states in step. Each page passes its own card selector; the slug is
// data-region-slug either way.
export function readRegionCards(selector: string): Map<string, HTMLElement> {
  const cards = new Map<string, HTMLElement>();
  document.querySelectorAll<HTMLElement>(selector).forEach((card) => {
    const slug = card.dataset.regionSlug;
    if (slug) cards.set(slug, card);
  });
  return cards;
}

// Run once `target` is close to being scrolled into view.
export function whenScrolledNear(target: Element, callback: () => void): void {
  if (!("IntersectionObserver" in window)) {
    callback();
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      callback();
    },
    { rootMargin: REGION_MAP_PRELOAD_MARGIN },
  );
  observer.observe(target);
}

const numberFormat = new Intl.NumberFormat();

function formatCounts(region: RegionSummary): string {
  const photos = region.image_count === 1 ? "photo" : "photos";
  return (
    `${numberFormat.format(region.image_count)} ${photos} · ` +
    `${numberFormat.format(region.georeferenced_count)} mapped`
  );
}

// Popup for one pin: a compact mirror of the region's card — name,
// subtitle, counts — plus the button that adopts it. Assembled as DOM
// rather than markup because the names are admin-entered free text —
// textContent is what keeps them out of the parser.
function buildPinPopup(
  region: RegionSummary,
  onSelect: (slug: string) => void,
): HTMLDivElement {
  const container = document.createElement("div");

  const name = document.createElement("div");
  name.className = "fw-semibold";
  name.textContent = region.long_name;
  container.appendChild(name);

  if (region.subtitle) {
    const subtitle = document.createElement("div");
    subtitle.className = "small text-body-secondary";
    subtitle.textContent = region.subtitle;
    container.appendChild(subtitle);
  }

  const counts = document.createElement("div");
  counts.className = "small text-body-secondary mb-2";
  counts.textContent = formatCounts(region);
  container.appendChild(counts);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-primary btn-sm w-100";
  button.innerHTML = '<i class="fas fa-map-marker-alt me-1"></i>';
  button.appendChild(document.createTextNode(`Explore ${region.short_name}`));
  button.addEventListener("click", () => onSelect(region.slug));
  container.appendChild(button);

  return container;
}

// Pin element: a circular photo of the region (its representative
// image), or a pin glyph on the brand colour for a region without one.
// The visual circle is a child of the returned root, not the root
// itself, because maplibre positions the root via style.transform — a
// CSS scale there would be overwritten on every render.
function buildMarkerElement(region: RegionSummary): HTMLDivElement {
  const root = document.createElement("div");
  root.className = "region-marker";

  const visual = document.createElement("div");
  visual.className = "region-photo-marker";
  if (region.thumbnail) {
    const img = document.createElement("img");
    img.src = region.thumbnail;
    img.alt = "";
    visual.appendChild(img);
  } else {
    visual.classList.add("region-photo-marker-empty");
    const icon = document.createElement("i");
    icon.className = "fas fa-map-marker-alt";
    icon.setAttribute("aria-hidden", "true");
    visual.appendChild(icon);
  }
  root.appendChild(visual);

  return root;
}

export interface RegionMapOptions {
  container: HTMLElement;
  summaries: RegionSummary[];
  // Cards to keep in step with the pins, by slug. A region without a card
  // (the directory lists unadvertised regions, which get no pin, and a page
  // may show only its first few) simply isn't linked.
  cards: Map<string, HTMLElement>;
  // What a popup's button does: each page owns where selecting a region
  // takes the visitor.
  onSelect: (slug: string) => void;
}

export function initRegionMap({
  container,
  summaries,
  cards,
  onSelect,
}: RegionMapOptions): void {
  // Frame the actual pins rather than a hardcoded view, so none can
  // start off-screen and the map keeps working wherever regions exist.
  const bounds = new maplibregl.LngLatBounds();
  for (const region of summaries) {
    bounds.extend([region.lng, region.lat]);
  }

  const map = new maplibregl.Map({
    container,
    style: protomapsStyleUrl(),
    ...(summaries.length > 0
      ? {
          bounds,
          fitBoundsOptions: { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM },
        }
      : { center: FALLBACK_CENTER, zoom: FALLBACK_ZOOM }),
  });
  map.addControl(new maplibregl.NavigationControl());

  // Markers rather than a GeoJSON source and symbol layer: they're DOM
  // overlays, so they survive the setStyle below, and the region list is
  // small enough that the per-pin element cost doesn't matter.
  for (const region of summaries) {
    const markerElement = buildMarkerElement(region);
    new maplibregl.Marker({ element: markerElement })
      .setLngLat([region.lng, region.lat])
      // Offset past the circle's 24px radius so the popup doesn't sit
      // on top of the photo.
      .setPopup(
        new maplibregl.Popup({
          offset: 28,
          className: "region-map-popup",
        }).setDOMContent(
          buildPinPopup(region, onSelect),
        ),
      )
      .addTo(map);

    const card = cards.get(region.slug);
    if (card) {
      card.addEventListener("mouseenter", () =>
        markerElement.classList.add("is-active"),
      );
      card.addEventListener("mouseleave", () =>
        markerElement.classList.remove("is-active"),
      );
      markerElement.addEventListener("mouseenter", () =>
        card.classList.add("is-active"),
      );
      markerElement.addEventListener("mouseleave", () =>
        card.classList.remove("is-active"),
      );
    }
  }

  // Basemap-only map, so a colour-scheme flip can swap the whole style —
  // there are no overlay layers to preserve (unlike map_detail's retint),
  // and the pins are DOM overlays that ride along.
  onColorSchemeChange(() => map.setStyle(protomapsStyleUrl()));
}
