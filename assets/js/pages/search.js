import autoComplete from "@tarekraafat/autocomplete.js";
import "../../styles/components/autocomplete.css";

// Import image grid component (includes bulk selection and modal functionality)
import { imageGrid } from "../components/image_grid.js";
import {
  createImagePointOverlay,
  imagePointOverlayLayerIds,
} from "../components/image_point_overlay";
import { initPointPicker } from "../components/point_picker";

/**
 * The search modes, and the handful of things that differ between them: which
 * description and input container to show, which endpoint to ask, and whether
 * the results line can report a meaningful total.
 *
 * Looked up here rather than written out per mode at each of the six places
 * that care. That also fixes a real bug: the "Load More" endpoint used to be
 * chosen by a ternary that fell through to the *text* endpoint for any mode it
 * didn't recognise, so paging quietly changed what was being searched.
 */
const SEARCH_MODES = {
  semantic: {
    description: "semanticDescription",
    container: "textSearchContainer",
    containerDisplay: "flex",
    endpoint: "/api/v1/search/",
    placeholder: "Describe what you're looking for...",
    label: "semantic search",
    // Semantic, reverse and in-view rank the whole corpus, so a total would
    // only ever say "all of them". Only text search has a real count.
    reportsCount: false,
  },
  text: {
    description: "textDescription",
    container: "textSearchContainer",
    containerDisplay: "flex",
    endpoint: "/api/v1/search/text/",
    placeholder: "Search for keywords...",
    label: "text search",
    reportsCount: true,
  },
  reverse: {
    description: "reverseDescription",
    container: "reverseSearchContainer",
    containerDisplay: "block",
    endpoint: "/api/v1/search/reverse/",
    placeholder: "Upload an image to search...",
    label: "reverse image search",
    reportsCount: false,
  },
  "in-view": {
    description: "inViewDescription",
    container: "inViewSearchContainer",
    containerDisplay: "block",
    endpoint: "/api/v1/search/in-view/",
    placeholder: "Pick a point on the map...",
    label: "in-view search",
    reportsCount: false,
  },
};

const DEFAULT_SEARCH_MODE = "semantic";

// The one mode that searches by coordinate rather than by query.
const IN_VIEW_MODE = "in-view";

// Every distinct input container, so switching modes can hide the others
// without needing to know which mode owns which.
const SEARCH_CONTAINERS = [
  ...new Set(Object.values(SEARCH_MODES).map((mode) => mode.container)),
];

function searchModeConfig(mode) {
  return SEARCH_MODES[mode] || SEARCH_MODES[DEFAULT_SEARCH_MODE];
}

// The map overlay for modes that plot their results (in-view search). Set once
// its picker exists; both the first page of results and "Load More" push at it.
let resultPointOverlay = null;

/**
 * Read the result geometry the server embeds alongside the cards.
 * Returns null for modes that don't send any, which is different from an
 * empty array: null means "leave the map alone", [] means "nothing to show".
 */
function readResultPoints(doc) {
  const el = doc.getElementById("search-result-points");
  if (!el) return null;
  try {
    return JSON.parse(el.textContent);
  } catch (error) {
    console.error("Could not parse search result points:", error);
    return null;
  }
}

// The metadata elements are read from the parsed document, not inserted into
// the page with the cards.
const RESULT_METADATA_PATTERN =
  /<template[^>]*data-has-more[^>]*>.*?<\/template>|<script[^>]*id="search-result-points"[^>]*>.*?<\/script>/gis;

// Add x-cloak style to prevent flash of unstyled content
const style = document.createElement("style");
style.textContent = "[x-cloak] { display: none !important; }";
document.head.appendChild(style);

// Store for current search state (used by searchGrid for load more)
window.searchState = {
  query: "",
  mode: DEFAULT_SEARCH_MODE, // a key of SEARCH_MODES
  params: new URLSearchParams(),
  uploadedImageFile: null,
  hasMore: false,
  offset: 0,
  limit: 20,
  totalCount: 0,
};

/**
 * Alpine.js component for search results grid with "Load More" functionality.
 * Extends the base imageGrid component.
 */
window.searchGrid = function () {
  const base = imageGrid();

  return {
    ...base,

    // Re-declare getter since spread doesn't preserve getters
    get selectedCount() {
      return this.selectedIds.size;
    },

    // Load More specific state - must be reactive Alpine state
    hasMore: false,
    loading: false, // Controls spinner visibility (debounced)

    // Pending fetch promise from prefetch (mousedown)
    _pendingFetch: null,
    // Timer for debounced loading indicator
    _loadingTimer: null,
    // Flag to prevent concurrent requests (separate from loading indicator)
    _isLoadingMore: false,

    init() {
      base.init.call(this);

      // Listen for search state updates from displayHtmlResults
      document.addEventListener("searchStateUpdated", (e) => {
        this.hasMore = e.detail.hasMore;
        // Clear any pending fetch when new search results arrive
        this._pendingFetch = null;
      });
    },

    /**
     * Build fetch request for loading more results.
     * Used by both prefetchMore and loadMore.
     */
    _buildLoadMoreFetch() {
      const state = window.searchState;
      const nextPage = Math.floor(state.offset / state.limit) + 1;

      if (state.mode === "reverse" && state.uploadedImageFile) {
        // Reverse image search uses POST with FormData
        const formData = new FormData();
        formData.append("image", state.uploadedImageFile);
        formData.append("page", nextPage);
        formData.append("pagelimit", state.limit);

        // Copy filter params to formData
        for (const [key, value] of state.params.entries()) {
          if (
            !["q", "mode", "page", "format", "pagelimit"].includes(key) &&
            value
          ) {
            formData.append(key, value);
          }
        }

        return fetch("/api/v1/search/reverse/?format=html", {
          method: "POST",
          body: formData,
          headers: {
            "X-CSRFToken": getCsrfToken(),
          },
        });
      } else {
        // Every other mode pages with GET
        const apiEndpoint = searchModeConfig(state.mode).endpoint;
        const params = new URLSearchParams(state.params);
        params.set("page", nextPage);
        params.set("format", "html");

        return fetch(`${apiEndpoint}?${params.toString()}`);
      }
    },

    /**
     * Prefetch next page on mousedown for faster perceived loading.
     * Called via @mousedown on the Load More button.
     */
    prefetchMore() {
      if (this._isLoadingMore || !this.hasMore || this._pendingFetch) return;
      this._pendingFetch = this._buildLoadMoreFetch();
    },

    /**
     * Load more search results via AJAX.
     * Uses prefetched response if available.
     */
    async loadMore() {
      if (this._isLoadingMore || !this.hasMore) return;
      this._isLoadingMore = true;

      // Debounce the loading indicator - only show after 200ms
      this._loadingTimer = setTimeout(() => {
        this.loading = true;
      }, 200);

      const state = window.searchState;

      try {
        let response;

        if (this._pendingFetch) {
          // Use the prefetched request
          response = await this._pendingFetch;
          this._pendingFetch = null;
        } else {
          // No prefetch, make the request now
          response = await this._buildLoadMoreFetch();
        }

        if (!response.ok) {
          const errorData = await response.json();
          throw new Error(errorData.error || "Failed to load more results");
        }

        const html = await response.text();

        if (html.trim()) {
          const parser = new DOMParser();
          const doc = parser.parseFromString(html, "text/html");
          const newItems = doc.querySelectorAll(".image-card-wrapper");
          const metaEl = doc.querySelector("template[data-has-more]");
          const newPoints = readResultPoints(doc);

          if (newPoints && resultPointOverlay) {
            resultPointOverlay.addPoints(newPoints);
          }

          if (newItems.length > 0) {
            // Remove the metadata elements from HTML before inserting
            const cleanHtml = html.replace(RESULT_METADATA_PATTERN, "");

            // Append to the grid
            const gridEl = document.querySelector("#searchResults .row");
            if (gridEl) {
              gridEl.insertAdjacentHTML("beforeend", cleanHtml);

              // Re-initialize Alpine on new content
              if (window.Alpine) {
                // Only init the newly added elements
                newItems.forEach((item) => {
                  const addedItem = gridEl.querySelector(
                    `[data-image-id="${item.dataset.imageId}"]`,
                  );
                  if (addedItem) {
                    window.Alpine.initTree(addedItem);
                  }
                });
              }
            }

            // Update state
            state.offset += newItems.length;

            if (metaEl) {
              this.hasMore = metaEl.dataset.hasMore === "true";
            } else {
              this.hasMore = newItems.length >= state.limit;
            }
          } else {
            this.hasMore = false;
          }
        } else {
          this.hasMore = false;
        }
      } catch (error) {
        console.error("Error loading more results:", error);
        this._pendingFetch = null;
        // Don't set hasMore to false on error - let user retry
      } finally {
        clearTimeout(this._loadingTimer);
        this.loading = false;
        this._isLoadingMore = false;
      }
    },
  };
};

// Register the search grid component with Alpine
window.Alpine.data("searchGrid", window.searchGrid);

// Also register the base imageGrid for backwards compatibility
window.Alpine.data("imageGrid", imageGrid);

// Helper function to get CSRF token (defined here for use by searchGrid)
function getCsrfToken() {
  const name = "csrftoken";
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

document.addEventListener("DOMContentLoaded", async function () {
  // Fetch all subjects from the API
  let allSubjects = [];
  let subjectMap = new Map();

  try {
    const response = await fetch("/api/v1/subjects/all/");
    if (response.ok) {
      allSubjects = await response.json();
      subjectMap = new Map(allSubjects.map((s) => [s.id.toString(), s.title]));
    } else {
      console.error("Failed to fetch subjects:", response.statusText);
    }
  } catch (error) {
    console.error("Error fetching subjects:", error);
  }

  let selectedSubjects = [];

  const searchForm = document.getElementById("searchForm");
  const searchQuery = document.getElementById("searchQuery");
  const pagelimitSelect = document.getElementById("pagelimitSelect");
  const startYear = document.getElementById("startYear");
  const endYear = document.getElementById("endYear");
  const searchResults = document.getElementById("searchResults");
  const modeRadios = document.querySelectorAll('input[name="searchMode"]');
  const inViewLat = document.getElementById("inViewLat");
  const inViewLon = document.getElementById("inViewLon");
  const inViewRadius = document.getElementById("inViewRadius");
  const imageDropZone = document.getElementById("imageDropZone");
  const imageFileInput = document.getElementById("imageFileInput");
  const selectFileBtn = document.getElementById("selectFileBtn");
  const dropZoneContent = document.getElementById("dropZoneContent");
  const imagePreviewContainer = document.getElementById(
    "imagePreviewContainer",
  );
  const imagePreview = document.getElementById("imagePreview");
  const imageActionsContainer = document.getElementById(
    "imageActionsContainer",
  );
  const changeImageBtn = document.getElementById("changeImageBtn");
  const subjectSearchWrapper = document.getElementById(
    "subject-search-wrapper",
  );
  const noSubjectsRadio = document.getElementById("noSubjects");

  let uploadedImageFile = null;

  function renderSelectedSubjects() {
    const container = document.getElementById("selected-subjects");
    container.innerHTML = selectedSubjects
      .map(
        (subject) => `
            <span class="badge bg-primary d-flex align-items-center">
                ${escapeHtml(subject.title)}
                <button type="button" class="btn-close btn-close-white ms-2" aria-label="Remove" data-id="${subject.id}"></button>
            </span>
        `,
      )
      .join("");
  }

  function initializeFromURL() {
    const urlParams = new URLSearchParams(window.location.search);

    // The in-view inputs are filled before the mode is applied, so the picker
    // (built by applyMode) opens on the shared coordinate rather than the
    // default city view.
    if (inViewLat && urlParams.has("lat")) {
      inViewLat.value = urlParams.get("lat");
    }
    if (inViewLon && urlParams.has("lon")) {
      inViewLon.value = urlParams.get("lon");
    }
    if (inViewRadius && urlParams.has("radius")) {
      inViewRadius.value = urlParams.get("radius");
    }

    const requestedMode = urlParams.get("mode");
    const mode = SEARCH_MODES[requestedMode]
      ? requestedMode
      : DEFAULT_SEARCH_MODE;
    const modeRadio = document.querySelector(
      `input[name="searchMode"][value="${mode}"]`,
    );
    if (modeRadio) {
      modeRadio.checked = true;
    }
    applyMode(mode);

    if (urlParams.has("q")) {
      searchQuery.value = urlParams.get("q");
    }
    if (urlParams.has("pagelimit")) {
      pagelimitSelect.value = urlParams.get("pagelimit");
    }
    if (urlParams.has("start_year")) {
      startYear.value = urlParams.get("start_year");
    }
    if (urlParams.has("end_year")) {
      endYear.value = urlParams.get("end_year");
    }
    if (urlParams.get("georeferenced_only") === "true") {
      document.getElementById("georeferencedOnly").checked = true;
    } else if (urlParams.get("non_georeferenced_only") === "true") {
      document.getElementById("notGeoreferencedOnly").checked = true;
    } else {
      document.getElementById("allImages").checked = true;
    }

    // Handle subject params
    if (urlParams.get("no_subjects") === "true") {
      document.getElementById("noSubjects").checked = true;
    } else if (urlParams.has("with_subjects")) {
      document.getElementById("withSubjects").checked = true;
      const ids = urlParams.get("with_subjects").split(",");
      selectedSubjects = ids
        .map((id) => ({ id: id, title: subjectMap.get(id) || `ID: ${id}` }))
        .filter((s) => s.title);
    } else if (urlParams.has("without_subjects")) {
      document.getElementById("withoutSubjects").checked = true;
      const ids = urlParams.get("without_subjects").split(",");
      selectedSubjects = ids
        .map((id) => ({ id: id, title: subjectMap.get(id) || `ID: ${id}` }))
        .filter((s) => s.title);
    }
    renderSelectedSubjects();
    // Trigger change to hide/show subject input
    document
      .querySelector('input[name="subjectOptions"]:checked')
      .dispatchEvent(new Event("change"));

    const subjectOption = document.querySelector(
      'input[name="subjectOptions"]:checked',
    ).value;
    const hasSubjectFilter =
      subjectOption === "none" || selectedSubjects.length > 0;

    const hasInViewPoint =
      mode === IN_VIEW_MODE && urlParams.has("lat") && urlParams.has("lon");

    if (urlParams.has("q") || hasSubjectFilter || hasInViewPoint) {
      const hasAdvanced =
        urlParams.has("start_year") ||
        urlParams.has("end_year") ||
        urlParams.has("georeferenced_only") ||
        selectedSubjects.length > 0 ||
        urlParams.has("no_subjects");
      if (hasAdvanced) {
        const advancedOptions = document.getElementById("advancedOptions");
        new bootstrap.Collapse(advancedOptions, { toggle: false }).show();
      }
      performSearch();
    }
  }

  searchForm.addEventListener("submit", function (e) {
    e.preventDefault();
    performSearch();
  });

  document
    .getElementById("selected-subjects")
    .addEventListener("click", function (e) {
      if (e.target.matches("button.btn-close")) {
        const subjectId = e.target.dataset.id;
        selectedSubjects = selectedSubjects.filter(
          (s) => s.id.toString() !== subjectId,
        );
        renderSelectedSubjects();
      }
    });

  document.querySelectorAll('input[name="subjectOptions"]').forEach((radio) => {
    radio.addEventListener("change", function () {
      subjectSearchWrapper.style.display =
        this.value === "none" ? "none" : "block";
    });
  });

  // Handle search mode toggle
  function currentMode() {
    const checked = document.querySelector('input[name="searchMode"]:checked');
    return checked && SEARCH_MODES[checked.value]
      ? checked.value
      : DEFAULT_SEARCH_MODE;
  }

  function applyMode(mode) {
    const config = searchModeConfig(mode);

    Object.entries(SEARCH_MODES).forEach(([name, modeConfig]) => {
      const description = document.getElementById(modeConfig.description);
      if (description) {
        description.style.display = name === mode ? "block" : "none";
      }
    });

    SEARCH_CONTAINERS.forEach((containerId) => {
      const container = document.getElementById(containerId);
      if (container) {
        container.style.display =
          containerId === config.container ? config.containerDisplay : "none";
      }
    });

    searchQuery.placeholder = config.placeholder;

    // The picker's map needs a visible container to size itself against, so
    // it is built the first time in-view mode is actually shown.
    if (mode === IN_VIEW_MODE) {
      ensurePointPicker();
    }
  }

  modeRadios.forEach((radio) => {
    radio.addEventListener("change", function () {
      if (this.checked) {
        applyMode(this.value);
      }
    });
  });

  // In-view search: the map/coordinate picker, built lazily (see applyMode).
  let pointPicker = null;

  function ensurePointPicker() {
    if (pointPicker || !inViewLat || !inViewLon) return pointPicker;

    const lat = parseFloat(inViewLat.value);
    const lon = parseFloat(inViewLon.value);
    const mapEl = document.getElementById("inViewPickerMap");

    pointPicker = initPointPicker({
      mapId: "inViewPickerMap",
      latInput: inViewLat,
      lonInput: inViewLon,
      initial:
        Number.isFinite(lat) && Number.isFinite(lon) ? [lon, lat] : undefined,
      // Placing a point is the search: no need to also press the button.
      onChange: () => performSearch(),
      // Keep the results above any tile overlay the layer switcher inserts,
      // and put them back after a base-layer swap discards them.
      overlayLayerIds: imagePointOverlayLayerIds(),
      onStyleSwap: () => resultPointOverlay?.redraw(),
    });

    resultPointOverlay = createImagePointOverlay(pointPicker.map, {
      spriteUrl: mapEl?.dataset.directionSprite,
    });

    return pointPicker;
  }

  // Reverse image search functionality
  function handleImageFile(file) {
    if (!file || !file.type.startsWith("image/")) {
      alert("Please select a valid image file");
      return;
    }

    uploadedImageFile = file;
    const reader = new FileReader();
    reader.onload = function (e) {
      imagePreview.src = e.target.result;
      dropZoneContent.style.display = "none";
      imagePreviewContainer.style.display = "block";
      if (imageActionsContainer) {
        imageActionsContainer.style.display = "block";
      }
    };
    reader.readAsDataURL(file);
  }

  // Click to select file
  if (selectFileBtn) {
    selectFileBtn.addEventListener("click", function (e) {
      e.preventDefault();
      imageFileInput.click();
    });
  }

  // Drop zone click
  if (imageDropZone) {
    imageDropZone.addEventListener("click", function (e) {
      // Allow clicking anywhere in the drop zone to trigger file selection
      imageFileInput.click();
    });
  }

  // File input change
  if (imageFileInput) {
    imageFileInput.addEventListener("change", function (e) {
      if (e.target.files && e.target.files[0]) {
        handleImageFile(e.target.files[0]);
      }
    });
  }

  // Drag and drop
  if (imageDropZone) {
    imageDropZone.addEventListener("dragover", function (e) {
      e.preventDefault();
      e.stopPropagation();
      this.classList.add("border-primary", "bg-primary-subtle");
    });

    imageDropZone.addEventListener("dragleave", function (e) {
      e.preventDefault();
      e.stopPropagation();
      this.classList.remove("border-primary", "bg-primary-subtle");
    });

    imageDropZone.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      this.classList.remove("border-primary", "bg-primary-subtle");

      const files = e.dataTransfer.files;
      if (files && files[0]) {
        handleImageFile(files[0]);
      }
    });
  }

  // Paste image
  document.addEventListener("paste", function (e) {
    if (currentMode() === "reverse") {
      const items = e.clipboardData.items;
      for (let i = 0; i < items.length; i++) {
        if (items[i].type.indexOf("image") !== -1) {
          const blob = items[i].getAsFile();
          handleImageFile(blob);
          e.preventDefault();
          break;
        }
      }
    }
  });

  // Change image button
  if (changeImageBtn) {
    changeImageBtn.addEventListener("click", function (e) {
      e.preventDefault();
      uploadedImageFile = null;
      imagePreview.src = "";
      dropZoneContent.style.display = "block";
      imagePreviewContainer.style.display = "none";
      if (imageActionsContainer) {
        imageActionsContainer.style.display = "none";
      }
      imageFileInput.value = "";
    });
  }

  function performReverseImageSearch() {
    if (!uploadedImageFile) {
      alert("Please upload an image first");
      return;
    }

    const limit = parseInt(pagelimitSelect.value, 10) || 20;

    const apiEndpoint = "/api/v1/search/reverse/?format=html";
    const formData = new FormData();
    formData.append("image", uploadedImageFile);
    formData.append("page", 1);
    formData.append("pagelimit", limit);

    // Build params for state storage
    const stateParams = new URLSearchParams();
    stateParams.set("pagelimit", limit);

    if (startYear.value) {
      formData.append("start_year", startYear.value);
      stateParams.set("start_year", startYear.value);
    }
    if (endYear.value) {
      formData.append("end_year", endYear.value);
      stateParams.set("end_year", endYear.value);
    }

    const georeferencedOption = document.querySelector(
      'input[name="georeferencedOptions"]:checked',
    ).value;
    if (georeferencedOption === "georeferenced") {
      formData.append("georeferenced_only", "true");
      stateParams.set("georeferenced_only", "true");
    } else if (georeferencedOption === "not_georeferenced") {
      formData.append("non_georeferenced_only", "true");
      stateParams.set("non_georeferenced_only", "true");
    }

    // Add subject params
    const subjectOption = document.querySelector(
      'input[name="subjectOptions"]:checked',
    ).value;
    const subjectIds = selectedSubjects.map((s) => s.id).join(",");

    if (subjectOption === "none") {
      formData.append("no_subjects", "true");
      stateParams.set("no_subjects", "true");
    } else if (subjectIds) {
      if (subjectOption === "with") {
        formData.append("with_subjects", subjectIds);
        stateParams.set("with_subjects", subjectIds);
      } else if (subjectOption === "without") {
        formData.append("without_subjects", subjectIds);
        stateParams.set("without_subjects", subjectIds);
      }
    }

    // Update search state for load more
    window.searchState.mode = "reverse";
    window.searchState.query = "";
    window.searchState.params = stateParams;
    window.searchState.uploadedImageFile = uploadedImageFile;
    window.searchState.limit = limit;
    window.searchState.offset = 0;
    window.searchState.hasMore = false;

    searchResults.innerHTML = renderLoadingPlaceholder();

    fetch(apiEndpoint, {
      method: "POST",
      body: formData,
      headers: {
        "X-CSRFToken": getCsrfToken(),
      },
    })
      .then((response) => {
        if (!response.ok) {
          return response.json().then((data) => {
            throw new Error(data.error || "Search failed");
          });
        }
        return response.text();
      })
      .then((html) => {
        displayHtmlResults(html, null, "reverse image search");
      })
      .catch((error) => {
        displayError("Search failed: " + error.message);
      });
  }

  function renderLoadingPlaceholder() {
    return `
      <div class="text-center py-5">
        <div class="spinner-border text-primary" role="status">
          <span class="visually-hidden">Searching...</span>
        </div>
        <p class="mt-2 text-muted">Searching images...</p>
      </div>
    `;
  }

  /** Add the shared Advanced Options filters to a query string. */
  function appendFilterParams(params) {
    if (startYear.value) {
      params.set("start_year", startYear.value);
    }
    if (endYear.value) {
      params.set("end_year", endYear.value);
    }

    const georeferencedOption = document.querySelector(
      'input[name="georeferencedOptions"]:checked',
    ).value;
    if (georeferencedOption === "georeferenced") {
      params.set("georeferenced_only", "true");
    } else if (georeferencedOption === "not_georeferenced") {
      params.set("non_georeferenced_only", "true");
    }

    const subjectOption = document.querySelector(
      'input[name="subjectOptions"]:checked',
    ).value;
    const subjectIds = selectedSubjects.map((s) => s.id).join(",");

    if (subjectOption === "none") {
      params.set("no_subjects", "true");
    } else if (subjectIds) {
      if (subjectOption === "with") {
        params.set("with_subjects", subjectIds);
      } else if (subjectOption === "without") {
        params.set("without_subjects", subjectIds);
      }
    }

    return params;
  }

  /** Share the current search as a URL, minus the parameters only the fetch needs. */
  function pushSearchUrl(params) {
    const urlParams = new URLSearchParams(params);
    urlParams.delete("format");
    urlParams.delete("page");
    history.pushState(
      null,
      "",
      `${window.location.pathname}?${urlParams.toString()}`,
    );
  }

  function performInViewSearch() {
    const latitude = parseFloat(inViewLat.value);
    const longitude = parseFloat(inViewLon.value);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      displayError("Pick a point on the map, or type a latitude and longitude.");
      return;
    }

    const limit = parseInt(pagelimitSelect.value, 10) || 20;

    const params = new URLSearchParams();
    params.set("mode", IN_VIEW_MODE);
    params.set("lat", latitude);
    params.set("lon", longitude);
    if (inViewRadius && inViewRadius.value) {
      params.set("radius", inViewRadius.value);
    }
    params.set("page", 1);
    params.set("pagelimit", limit);
    params.set("format", "html");
    appendFilterParams(params);

    window.searchState.mode = IN_VIEW_MODE;
    window.searchState.query = "";
    window.searchState.params = new URLSearchParams(params);
    window.searchState.uploadedImageFile = null;
    window.searchState.limit = limit;
    window.searchState.offset = 0;
    window.searchState.hasMore = false;

    pushSearchUrl(params);

    searchResults.innerHTML = renderLoadingPlaceholder();

    fetch(`${SEARCH_MODES["in-view"].endpoint}?${params.toString()}`)
      .then((response) => {
        if (!response.ok) {
          return response.json().then((data) => {
            throw new Error(data.error || "Search failed");
          });
        }
        return response.text();
      })
      .then((html) => {
        displayHtmlResults(
          html,
          `${latitude}, ${longitude}`,
          SEARCH_MODES["in-view"].label,
        );
      })
      .catch((error) => {
        displayError("Search failed: " + error.message);
      });
  }

  function performSearch() {
    const mode = currentMode();
    const query = searchQuery.value.trim();
    const subjectOption = document.querySelector(
      'input[name="subjectOptions"]:checked',
    ).value;
    const hasSubjectFilter =
      subjectOption === "none" || selectedSubjects.length > 0;

    // Handle reverse image search differently
    if (mode === "reverse") {
      if (!uploadedImageFile) {
        alert("Please upload an image first");
        return;
      }
      performReverseImageSearch();
      return;
    }

    // In-view search asks with a coordinate rather than a query
    if (mode === IN_VIEW_MODE) {
      performInViewSearch();
      return;
    }

    if (!query && !hasSubjectFilter) {
      return; // Do not search if there is no query and no subject filter
    }

    // If query is empty, we must use the text search endpoint, as semantic search requires a query.
    const searchMode = mode === "semantic" && query ? "semantic" : "text";

    const limit = parseInt(pagelimitSelect.value, 10) || 20;

    const apiEndpoint = SEARCH_MODES[searchMode].endpoint;

    const params = new URLSearchParams();
    params.set("q", query);
    params.set("mode", searchMode);
    params.set("page", 1);
    params.set("pagelimit", limit);
    params.set("format", "html"); // Request HTML format
    appendFilterParams(params);

    // Update search state for load more
    window.searchState.mode = searchMode;
    window.searchState.query = query;
    window.searchState.params = new URLSearchParams(params);
    window.searchState.uploadedImageFile = null;
    window.searchState.limit = limit;
    window.searchState.offset = 0;
    window.searchState.hasMore = false;

    // Update URL without the format param (for cleaner URLs)
    pushSearchUrl(params);

    searchResults.innerHTML = renderLoadingPlaceholder();

    const searchModeLabel = SEARCH_MODES[searchMode].label;

    fetch(`${apiEndpoint}?${params.toString()}`)
      .then((response) => {
        if (!response.ok) {
          return response.json().then((data) => {
            throw new Error(data.error || "Search failed");
          });
        }
        return response.text();
      })
      .then((html) => {
        displayHtmlResults(html, query, searchModeLabel);
      })
      .catch((error) => {
        displayError("Search failed: " + error.message);
      });
  }

  function displayHtmlResults(html, query, searchModeLabel) {
    const mode = window.searchState.mode;

    // Set when a region is selected in the navbar; the search endpoints scope
    // results to it server-side via the region cookie. In-view search is the
    // exception: a coordinate is its own scope, so it deliberately searches
    // globally and must not claim to be showing one region's images.
    const regionName =
      mode === IN_VIEW_MODE ? null : window.filterConfig?.regionName;

    // Parse the HTML to extract metadata from the template element
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");
    const metaEl = doc.querySelector("template[data-has-more]");
    const imageCards = doc.querySelectorAll(".image-card-wrapper");

    // A fresh search replaces whatever the map was showing, including when it
    // found nothing: stale pins under a "no results" message would be a lie.
    const resultPoints = readResultPoints(doc);
    if (resultPointOverlay) {
      resultPointOverlay.setPoints(resultPoints ?? []);
    }

    // Check if there are no results
    if (imageCards.length === 0) {
      let message;
      if (mode === IN_VIEW_MODE) {
        message = `No photographs look at <strong>${escapeHtml(query)}</strong>.`;
      } else if (query) {
        message = `No results found for "<strong>${escapeHtml(query)}</strong>".`;
      } else {
        message = "No results found for the selected filters.";
      }
      const advice =
        mode === IN_VIEW_MODE
          ? "Try another spot, or allow a greater distance."
          : "Try a different search term or filter.";
      const regionHint = regionName
        ? ` You're searching within <strong>${escapeHtml(regionName)}</strong> \u2014 switch to Global in the region selector to search everywhere.`
        : "";
      searchResults.innerHTML = `
        <div class="alert alert-info">
          <i class="fas fa-info-circle me-2"></i>
          ${message}
          ${advice}${regionHint}
        </div>
      `;
      // Hide bulk actions when no results
      const bulkActionsContainer = document.getElementById(
        "bulkActionsContainer",
      );
      if (bulkActionsContainer) {
        bulkActionsContainer.style.display = "none";
      }
      // Reset search state
      window.searchState.hasMore = false;
      window.searchState.offset = 0;
      window.searchState.totalCount = 0;
      // Dispatch event to notify Alpine component
      document.dispatchEvent(
        new CustomEvent("searchStateUpdated", { detail: { hasMore: false } }),
      );
      return;
    }

    // Show bulk actions when there are results
    const bulkActionsContainer = document.getElementById(
      "bulkActionsContainer",
    );
    if (bulkActionsContainer) {
      bulkActionsContainer.style.display = "block";
    }

    // Extract pagination data from the template element
    const hasMore = metaEl ? metaEl.dataset.hasMore === "true" : false;
    const totalCount = metaEl ? parseInt(metaEl.dataset.totalCount, 10) : 0;
    const limit = window.searchState.limit || 20;

    // Update search state for load more functionality
    window.searchState.hasMore = hasMore;
    window.searchState.offset = imageCards.length;
    window.searchState.totalCount = totalCount;

    // Dispatch event to notify Alpine component of state change
    document.dispatchEvent(
      new CustomEvent("searchStateUpdated", { detail: { hasMore } }),
    );

    // Build filter summary
    let filterSummary = "";
    const startYearVal = startYear.value;
    const endYearVal = endYear.value;
    const georeferencedOption = document.querySelector(
      'input[name="georeferencedOptions"]:checked',
    ).value;

    const radiusVal =
      mode === IN_VIEW_MODE && inViewRadius ? inViewRadius.value : "";

    if (
      startYearVal ||
      endYearVal ||
      georeferencedOption !== "all" ||
      radiusVal
    ) {
      let filters = [];
      if (radiusVal) {
        filters.push(
          `within ${inViewRadius.options[inViewRadius.selectedIndex].text}`,
        );
      }
      if (startYearVal && endYearVal) {
        filters.push(`${startYearVal}-${endYearVal}`);
      } else if (startYearVal) {
        filters.push(`from ${startYearVal}`);
      } else if (endYearVal) {
        filters.push(`until ${endYearVal}`);
      }
      if (georeferencedOption === "georeferenced") {
        filters.push("georeferenced only");
      } else if (georeferencedOption === "not_georeferenced") {
        filters.push("not georeferenced only");
      }
      filterSummary = ` (filtered: ${filters.join(", ")})`;
    }

    const forQuery = query ? ` for "<strong>${escapeHtml(query)}</strong>"` : "";
    const inRegion = regionName ? ` in ${escapeHtml(regionName)}` : "";

    // In-view search gets its own sentence rather than the shared one: naming
    // the mode reads as noise next to a coordinate ("using in-view search"),
    // where what it does says itself.
    let statsMessage;
    if (mode === IN_VIEW_MODE) {
      statsMessage =
        `Showing photographs looking at ` +
        `<strong>${escapeHtml(query)}</strong>${filterSummary}`;
    } else if (searchModeConfig(mode).reportsCount) {
      // Only text search has a meaningful total; the ranked modes return the
      // whole corpus in order, so counting it says nothing.
      statsMessage = `Found ${totalCount} results${forQuery}${inRegion} using ${searchModeLabel}${filterSummary}`;
    } else {
      statsMessage = `Showing results${forQuery}${inRegion} using ${searchModeLabel}${filterSummary}`;
    }

    // Clear previously registered IDs since we're loading new results
    if (window.imageGridInstance) {
      window.imageGridInstance.clearRegisteredIds();
    }

    // Remove the metadata elements from the HTML before inserting
    const cleanHtml = html.replace(RESULT_METADATA_PATTERN, "");

    // Build the full results HTML with Load More button instead of pagination
    let resultsHtml = `
      <div class="search-stats mb-3">
        ${statsMessage}
      </div>
      <div class="row">
        ${cleanHtml}
      </div>
    `;

    searchResults.innerHTML = resultsHtml;

    // Re-initialize Alpine on the new content
    if (window.Alpine) {
      window.Alpine.initTree(searchResults);
    }
  }

  function displayError(error) {
    searchResults.innerHTML = `
            <div class="alert alert-danger">
                <i class="fas fa-exclamation-triangle me-2"></i>
                <strong>Search Error:</strong> ${escapeHtml(error)}
            </div>
        `;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // Init Autocomplete
  const subjectAutocomplete = new autoComplete({
    selector: "#subjectSearchInput",
    placeHolder: "Search for a subject by name...",
    data: {
      src: async (query) => {
        try {
          const source = await fetch(
            `/api/v1/subjects/autocomplete/?q=${query}`,
          );
          const data = await source.json();
          return data;
        } catch (error) {
          return error;
        }
      },
      keys: ["title"],
      cache: false,
    },
    resultItem: {
      element: (item, data) => {
        item.style =
          "display: flex; justify-content: space-between; align-items: center;";
        let description = data.value.description
          ? data.value.description.substring(0, 40) + "..."
          : "";
        item.innerHTML = `
                <span style=\"text-overflow: ellipsis; white-space: nowrap; overflow: hidden;\">
                    ${data.match} <small class=\"text-muted ms-2\">${description}</small>
                </span>
                <span style=\"display: flex; align-items: center; font-size: 13px; font-weight: 100; text-transform: uppercase; color: rgba(0,0,0,.5);\">
                    ${data.value.wikidata_id || ""}
                </span>`;
      },
    },
    threshold: 2,
    // The server does the (fuzzy) filtering and ranking; never drop or
    // reorder results client-side. <mark> literal substring hits;
    // typo-only hits render as plain text.
    searchEngine: (query, record) => {
      const idx = record.toLowerCase().indexOf(query.toLowerCase());
      if (idx === -1) return record;
      return (
        record.slice(0, idx) +
        "<mark>" +
        record.slice(idx, idx + query.length) +
        "</mark>" +
        record.slice(idx + query.length)
      );
    },
    events: {
      input: {
        selection: (event) => {
          const selection = event.detail.selection.value;
          if (!selectedSubjects.some((s) => s.id === selection.id)) {
            selectedSubjects.push(selection);
            renderSelectedSubjects();
          }
          subjectAutocomplete.input.value = "";
        },
      },
    },
  });

  initializeFromURL();
});
