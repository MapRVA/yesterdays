import "../../styles/components/region-selector.css";
import { writeRegionCookie } from "./region_cookie";

interface RegionSuggestion {
  short_name: string;
  long_name: string;
  wikidata_id: string;
}

interface RegionSelector {
  query: string;
  suggestions: RegionSuggestion[];
  currentQid: string | null;
  selecting: boolean;
  fetched: boolean;
  _controller: AbortController | null;
  init(): void;
  fetchSuggestions(): Promise<void>;
  focusFirstItem(): void;
  currentIsSuggested(): boolean;
  // Only the QID is ever read, so the template can hand over the pinned
  // current region without restating what the server already rendered.
  select(region: Pick<RegionSuggestion, "wikidata_id"> | null): void;
  selectFirst(): void;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * Navbar region selector: a type-to-search dropdown persisting the chosen
 * region QID in a cookie, which regions.context_processors.current_region
 * reads back on every page render. Selecting reloads the page so the
 * server rerenders everything (navbar label included) under the new
 * region.
 *
 * Bootstrap's Dropdown plugin (data-bs-toggle on the trigger in
 * templates/regions/partials/region_selector.html) owns opening, closing,
 * positioning, Escape and arrow-key movement between items; this
 * component only reacts to its lifecycle events to fetch and render the
 * suggestion list.
 *
 * Must be called from assets/index.js after the window.Alpine assignment
 * (import hoisting evaluates this module before index.js's own body, so a
 * top-level registration here would find window.Alpine undefined) and
 * before Alpine.start() fires on DOMContentLoaded.
 */
export function initRegionSelector(): void {
  window.Alpine.data("regionSelector", function (): RegionSelector {
    // Config arrives via data attributes on the root <li> in
    // templates/regions/partials/region_selector.html. The factory runs
    // during Alpine.start(), so the element exists; reading it here keeps
    // us off Alpine's untyped magic properties (this.$el).
    const root = document.getElementById("region-selector");
    const autocompleteUrl = root?.dataset.autocompleteUrl ?? null;
    const cookieName = root?.dataset.cookieName || "region";

    return {
      query: "",
      suggestions: [],
      currentQid: root?.dataset.currentQid || null,
      selecting: false,
      fetched: false,
      _controller: null,

      init() {
        // Bootstrap fires these on the toggle button; they bubble to the
        // root. Bootstrap focuses the toggle as part of showing, so the
        // search field can only take focus once "shown" has fired.
        root?.addEventListener("show.bs.dropdown", () => {
          this.query = "";
          // Refresh in place: the previous list stays visible while the
          // request is in flight, so reopening doesn't flash empty.
          this.fetchSuggestions();
        });
        root?.addEventListener("shown.bs.dropdown", () => {
          root.querySelector<HTMLInputElement>("input[type=search]")?.focus();
        });
        root?.addEventListener("hidden.bs.dropdown", () => {
          this._controller?.abort();
        });
      },

      async fetchSuggestions() {
        if (!autocompleteUrl) return;
        this._controller?.abort();
        this._controller = new AbortController();
        try {
          const url = new URL(autocompleteUrl, window.location.origin);
          if (this.query) {
            url.searchParams.set("q", this.query);
          }
          const response = await fetch(url.toString(), {
            headers: { "X-Requested-With": "XMLHttpRequest" },
            signal: this._controller.signal,
          });
          if (!response.ok) return;
          this.suggestions = (await response.json()) as RegionSuggestion[];
        } catch (error) {
          if (!isAbortError(error)) {
            console.error("Region autocomplete error:", error);
          }
        } finally {
          // Gates the pinned current region in the template: before the
          // first list lands, suggestions is empty and currentIsSuggested()
          // can only answer "no", which would flash a pinned row that the
          // popular list is about to carry itself. Set even when the
          // request failed, so a region still shows somewhere.
          this.fetched = true;
        }
      },

      // ArrowDown from the search field: Bootstrap ignores arrow keys while
      // focus is in an input, so hand focus to the first item, after which
      // Bootstrap's own handler moves between items. Skip hidden items —
      // "Global" is still in the DOM while a query is in the field, and
      // focusing a display:none element would do nothing.
      focusFirstItem() {
        const items =
          root?.querySelectorAll<HTMLElement>(".dropdown-menu .dropdown-item") ??
          [];
        for (const item of items) {
          if (item.checkVisibility()) {
            item.focus();
            return;
          }
        }
      },

      // Whether the popular list already carries the selected region: the
      // template pins it under "Global" only when it doesn't, so a popular
      // region is never offered twice in one menu.
      currentIsSuggested() {
        return this.suggestions.some(
          (region) => region.wikidata_id === this.currentQid
        );
      },

      select(region) {
        const qid = region ? region.wikidata_id : null;
        writeRegionCookie(cookieName, qid);
        // Move the highlight optimistically so the clicked item shows the
        // spinner until the reload lands.
        this.currentQid = qid;
        this.selecting = true;
        // Clear the URL hash so that map pages will fit to the selected
        // region's bounds instead of restoring the previous view.
        if (window.location.hash) {
          window.location.hash = "";
        }
        window.location.reload();
      },

      selectFirst() {
        const first = this.suggestions[0];
        if (first) {
          this.select(first);
        }
      },
    };
  });
}
