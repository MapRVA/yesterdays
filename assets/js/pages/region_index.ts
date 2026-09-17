// Entry point for the region directory at /regions/
// (templates/regions/browse_regions.html): the global homepage's picker
// map over a searchable grid of every region, busiest first.
import "../../styles/pages/region-index.css";
import { writeRegionCookie } from "../components/region_cookie";
import {
  initRegionMap,
  readRegionCards,
  readRegionSummaries,
  whenScrolledNear,
} from "../components/region_map";

function selectRegion(cookieName: string, qid: string | null): void {
  writeRegionCookie(cookieName, qid);
  window.location.href = "/";
}

function initRegionDirectory(): void {
  const grid = document.getElementById("region-directory-grid");
  if (!(grid instanceof HTMLElement)) return;

  const cookieName = grid.dataset.cookieName;
  if (!cookieName) return;

  document.querySelectorAll<HTMLElement>("[data-region-select]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      selectRegion(cookieName, link.dataset.regionSelect ?? null);
    });
  });

  document.querySelector<HTMLElement>("[data-region-clear]")?.addEventListener(
    "click",
    (event) => {
      event.preventDefault();
      selectRegion(cookieName, null);
    },
  );

  // The map, once it's nearly scrolled to. Its pins cover the regions a
  // visitor lands in, so the grid's unadvertised cards simply go
  // unlinked — readRegionCards keys by QID and the map looks each up.
  const mapContainer = document.getElementById("global-home-map");
  if (mapContainer instanceof HTMLElement) {
    const cards = readRegionCards("#region-directory-grid [data-region-card]");
    whenScrolledNear(mapContainer, () =>
      initRegionMap({
        container: mapContainer,
        summaries: readRegionSummaries(),
        cards,
        onSelect: (qid) => selectRegion(cookieName, qid),
      }),
    );
  }

  const search = document.getElementById("region-directory-search");
  const count = document.getElementById("region-directory-count");
  const empty = document.getElementById("region-directory-empty");
  const cards = Array.from(grid.querySelectorAll<HTMLElement>("[data-region-card]"));

  if (!(search instanceof HTMLInputElement)) return;

  search.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;

    cards.forEach((card) => {
      // Unadvertised regions stay hidden while the search box is empty, so
      // the resting grid mirrors the map's pins.
      const surfaced =
        query !== "" || card.dataset.regionUnadvertised === undefined;
      const matches =
        surfaced &&
        (card.dataset.regionSearch ?? "").toLocaleLowerCase().includes(query);
      card.classList.toggle("d-none", !matches);
      if (matches) visible += 1;
    });

    empty?.classList.toggle("d-none", visible !== 0);
    if (count) {
      count.textContent = `${visible} region${visible === 1 ? "" : "s"}`;
    }
  });
}

initRegionDirectory();
