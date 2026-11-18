/**
 * From Above Georeference Interface
 * Handles polygon-based georeferencing for aerial/overhead images
 */

// CSS imports
import "../../styles/pages/georeference-interface.css";
import "maplibre-gl/dist/maplibre-gl.css";
import "@geoman-io/maplibre-geoman-free/dist/maplibre-geoman.css";
import "../../styles/components/image-viewer.css";

// JS imports
import maplibregl from "maplibre-gl";
import { ATTRIBUTION_CONTROL_OPTIONS } from "../components/map_display/attribution";
import {
  Geoman,
  type FeatureCreatedFwdEvent,
  type FeatureData,
  type FeatureEditEndFwdEvent,
  type FeatureRemovedFwdEvent,
  type GeoJsonImportFeature,
  type GeoJsonShapeFeature,
  type GmOptionsPartial,
} from "@geoman-io/maplibre-geoman-free";
import type { Polygon } from "geojson";
import { initialMapView } from "../constants/map";
import "../components/map_display/pmtiles_protocol";
import { boundsFromAerialGeometry } from "../components/map_display/bounds";
import { initialMapStyle, LayerControl } from "../components/layer_control";
import { initSubjectEditor } from "../components/subject_editor";
import { initImageViewer } from "../components/image_viewer";

// Django-serialized configuration, set inline by
// templates/images/from_above_georeference_interface.html
export interface FromAboveConfig {
  isStaff: boolean;
  isAuthenticated: boolean;
  csrfToken: string;
  imageId: number;
  urls: {
    imageDetail: string;
    aerialGeoreferenceImage: string;
    markDifficulty: string;
    markWillNotGeoref: string;
  };
  // Present when the image already has an aerial georeference
  existingPolygon?: Polygon;
}

interface SubmissionResponse {
  success?: boolean;
  error?: string;
}

type Difficulty = "easy" | "medium" | "hard";

const DIFFICULTY_COLORS: Record<Difficulty, string> = {
  easy: "success",
  medium: "warning",
  hard: "danger",
};

function isDifficulty(value: string | undefined): value is Difficulty {
  return value !== undefined && value in DIFFICULTY_COLORS;
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

// Only the polygon and delete tools are offered
const GEOMAN_OPTIONS: GmOptionsPartial = {
  settings: {
    controlsPosition: "top-left",
  },
  controls: {
    draw: {
      polygon: {
        uiEnabled: true,
        title: "Draw Polygon (only one allowed)",
      },
      marker: { uiEnabled: false },
      circle_marker: { uiEnabled: false },
      text_marker: { uiEnabled: false },
      circle: { uiEnabled: false },
      ellipse: { uiEnabled: false },
      line: { uiEnabled: false },
      rectangle: { uiEnabled: false },
    },
    edit: {
      delete: { uiEnabled: true },
    },
    helper: {
      snapping: { uiEnabled: false },
    },
  },
};

document.addEventListener("DOMContentLoaded", function () {
  const config = window.fromAboveConfig;
  if (!config) {
    console.error("From Above configuration not found");
    return;
  }
  initFromAbove(config);
});

function initFromAbove(config: FromAboveConfig): void {
  initImageViewer();

  // The template's logged-out branch renders a "Login to Submit" button
  // without this id (unreachable today, since the view requires login).
  const submitButton = document.getElementById(
    "submitButton",
  ) as HTMLButtonElement | null;
  const confidenceNotes = document.getElementById(
    "confidence-notes",
  ) as HTMLTextAreaElement | null;

  // One CSRF lookup for every POST on the page: hidden form input first, then
  // the meta tag, then the token serialized into the config object.
  function getCsrfToken(): string {
    return (
      document.querySelector<HTMLInputElement>(
        'input[name="csrfmiddlewaretoken"]',
      )?.value ||
      document
        .querySelector('meta[name="csrf-token"]')
        ?.getAttribute("content") ||
      config.csrfToken
    );
  }

  window.setupPMTilesProtocol();

  const map = new maplibregl.Map({
    container: "mymap",
    attributionControl: ATTRIBUTION_CONTROL_OPTIONS,
    style: initialMapStyle(),
    ...initialMapView(),
  });

  // Track polygon data
  let drawnPolygon: GeoJsonShapeFeature | null = null;
  let gm: Geoman | null = null;
  let currentPolygonId: FeatureData["id"] | null = null;
  // Direct reference to the Geoman feature object
  let currentFeatureRef: FeatureData | null = null;
  // New georeferences are "edited" by default
  let isEditing = !config.existingPolygon;
  let confidenceSelected = false;

  // Named event handlers so they can be detached and re-attached across
  // Geoman re-initializations (e.g. after style swaps).
  async function handleGmCreate(event: FeatureCreatedFwdEvent): Promise<void> {
    if (event.shape !== "polygon") return;

    const newFeature = event.feature;
    const previousFeature = currentFeatureRef;
    const previousPolygonId = currentPolygonId;

    // Adopt the new polygon before removing the previous one so any removal
    // event for the old feature cannot clear the new selection.
    currentPolygonId = newFeature.id;
    currentFeatureRef = newFeature;
    drawnPolygon = null;
    isEditing = true;
    updateSubmitButton();

    // Only one polygon is allowed: remove the previous one
    if (
      gm &&
      previousPolygonId !== null &&
      previousPolygonId !== newFeature.id
    ) {
      try {
        await gm.features.delete(previousFeature ?? previousPolygonId);
      } catch (e) {
        console.warn("Error removing polygon:", e);
        window.showAlert(
          "danger",
          "The previous polygon could not be removed. Please try again.",
        );
      }
    }

    updatePolygonData();
  }

  function handleGmRemove(event: FeatureRemovedFwdEvent): void {
    if (event.feature && event.feature.id === currentPolygonId) {
      currentPolygonId = null;
      currentFeatureRef = null;
      updatePolygonData();
    }
  }

  // Shared by vertex edits (gm:editend) and drags (gm:dragend)
  function handleGmEditEnd(event: FeatureEditEndFwdEvent): void {
    isEditing = true;
    if (event.feature) {
      currentFeatureRef = event.feature;
    }
    updatePolygonData();
  }

  function detachGeomanHandlers(): void {
    map.off("gm:create", handleGmCreate);
    map.off("gm:remove", handleGmRemove);
    map.off("gm:editend", handleGmEditEnd);
    map.off("gm:dragend", handleGmEditEnd);
  }

  function attachGeomanHandlers(): void {
    map.on("gm:create", handleGmCreate);
    map.on("gm:remove", handleGmRemove);
    map.on("gm:editend", handleGmEditEnd);
    map.on("gm:dragend", handleGmEditEnd);
  }

  // Initialize (or re-initialize) Geoman and restore any existing polygon.
  // Called on initial map load and after style swaps, which destroy all
  // MapLibre sources/layers and corrupt Geoman's internal state.
  async function initializeGeoman(
    polygonToRestore: GeoJsonShapeFeature["geometry"] | null,
  ): Promise<void> {
    // Tear down the previous instance if one exists — its internal
    // source/layer references are stale after setStyle() and init() will
    // refuse to re-create them.
    if (gm) {
      // Detach handlers before destroying to avoid firing on stale features
      detachGeomanHandlers();
      try {
        await gm.destroy();
      } catch (e) {
        console.warn("Error destroying previous Geoman instance:", e);
      }
      gm = null;
      currentPolygonId = null;
      currentFeatureRef = null;
    }

    const instance = new Geoman(map, GEOMAN_OPTIONS);
    gm = instance;

    // Wait for Geoman to finish loading before importing features
    await new Promise<void>((resolve) => {
      map.once("gm:loaded", () => resolve());
    });

    if (polygonToRestore) {
      const feature: GeoJsonImportFeature = {
        type: "Feature",
        geometry: polygonToRestore,
        properties: { shape: "polygon" },
      };
      const imported = await instance.features.importGeoJsonFeature(feature);
      if (imported) {
        currentPolygonId = imported.id;
        currentFeatureRef = imported;
        drawnPolygon = imported.getGeoJson();
      }
    }

    attachGeomanHandlers();
  }

  // Geoman creates its layers dynamically with a "gm_" prefix, which the
  // LayerControl already treats as overlays-to-stay-above. No beforeLayerId is
  // set because Geoman's layers are created after map load, so the control's
  // fallback logic and moveLayer() keep overlays positioned correctly.
  map.addControl(
    new LayerControl({
      onStyleSwap: async () => {
        // setStyle() destroys all MapLibre sources/layers, but Geoman's
        // internal state still holds stale references and its init() skips
        // re-creation. The only reliable fix is to destroy and re-create
        // the Geoman instance, then re-import the user's polygon.
        try {
          await initializeGeoman(drawnPolygon?.geometry ?? null);
        } catch (e) {
          console.error("Error re-initializing Geoman after style swap:", e);
          window.showAlert(
            "danger",
            "Failed to restore polygon drawing tools after style change.",
          );
        }
      },
    }),
    "top-right",
  );
  map.addControl(new maplibregl.NavigationControl());
  map.addControl(new maplibregl.FullscreenControl());

  map.on("load", async function () {
    try {
      await initializeGeoman(config.existingPolygon ?? null);

      // Fit the map to the existing polygon (only on initial load)
      if (config.existingPolygon) {
        const bounds = boundsFromAerialGeometry(config.existingPolygon);
        if (bounds) map.fitBounds(bounds, { padding: 50 });
      }
    } catch (e) {
      console.error("Error initializing Geoman:", e);
      window.showAlert(
        "danger",
        "Failed to initialize polygon drawing tools: " +
          (e instanceof Error ? e.message : String(e)),
      );
    }
  });

  function updateSubmitButton(): void {
    if (submitButton) {
      submitButton.disabled = !(
        drawnPolygon &&
        isEditing &&
        confidenceSelected
      );
    }
  }

  function updatePolygonData(): void {
    if (!gm) return;

    try {
      drawnPolygon = null;
      gm.features.forEach(function (feature) {
        if (feature.id === currentPolygonId) {
          drawnPolygon = feature.getGeoJson();
        }
      });

      // Imported features may not appear in gm.features iteration —
      // fall back to the stored reference
      if (!drawnPolygon && currentFeatureRef) {
        drawnPolygon = currentFeatureRef.getGeoJson();
      }

      // Require polygon, edit, and confidence before enabling submit
      updateSubmitButton();
    } catch (e) {
      console.warn("Error querying polygon data:", e);
    }
  }

  // Confidence level changes: low confidence requires a note
  const notesRequiredIndicator = document.getElementById(
    "notes-required-indicator",
  );
  const notesHelpText = document.getElementById("notes-help-text");

  document
    .querySelectorAll<HTMLInputElement>('input[name="confidence"]')
    .forEach((radio) => {
      radio.addEventListener("change", function () {
        confidenceSelected = true;
        const isLow = this.value === "low";
        if (notesRequiredIndicator)
          notesRequiredIndicator.style.display = isLow ? "inline" : "none";
        if (notesHelpText)
          notesHelpText.style.display = isLow ? "block" : "none";
        if (confidenceNotes) confidenceNotes.required = isLow;
        updateSubmitButton();
      });
    });

  document.getElementById("backButton")?.addEventListener("click", () => {
    window.location.href = config.urls.imageDetail;
  });

  if (submitButton) initSubmission(submitButton);
  if (config.isStaff) {
    initDifficultyMarking();
    initWillNotGeoreference();
  }

  function initSubmission(button: HTMLButtonElement): void {
    const buttonHtml = button.innerHTML;

    button.addEventListener("click", function () {
      if (!drawnPolygon) {
        window.showAlert("warning", "Please draw a polygon on the map first");
        return;
      }

      const selectedConfidence = document.querySelector<HTMLInputElement>(
        'input[name="confidence"]:checked',
      );
      if (!selectedConfidence) {
        window.showAlert("warning", "Please select a confidence level");
        return;
      }

      const notes = confidenceNotes?.value.trim() ?? "";
      if (selectedConfidence.value === "low" && notes === "") {
        window.showAlert(
          "warning",
          "Low confidence georeferences must include a descriptive note",
        );
        confidenceNotes?.focus();
        return;
      }

      performSubmission(drawnPolygon, selectedConfidence.value, notes);
    });

    function restoreButton(): void {
      button.disabled = false;
      button.innerHTML = buttonHtml;
    }

    function performSubmission(
      polygon: GeoJsonShapeFeature,
      confidence: string,
      notes: string,
    ): void {
      button.disabled = true;
      button.innerHTML =
        '<i class="fas fa-spinner fa-spin me-2"></i>Submitting...';

      fetch(config.urls.aerialGeoreferenceImage, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrfToken(),
        },
        body: JSON.stringify({
          polygon: polygon.geometry,
          notes: notes,
          confidence: confidence,
        }),
      })
        .then(async (response) => {
          // Error pages (CSRF failure, unhandled 500) aren't JSON
          const data = (await response
            .json()
            .catch(() => null)) as SubmissionResponse | null;
          if (data?.success) {
            window.showAlert(
              "success",
              "Polygonal georeference submitted successfully!",
            );
            setTimeout(() => {
              window.location.href = config.urls.imageDetail;
            }, 1500);
          } else {
            window.showAlert(
              "danger",
              "Error: " + (data?.error ?? `server returned ${response.status}`),
            );
            restoreButton();
          }
        })
        .catch(() => {
          window.showAlert("danger", "Network error occurred");
          restoreButton();
        });
    }
  }

  // Staff-only buttons for marking the image's difficulty. The button group is
  // only rendered while the image has no difficulty; after a successful mark,
  // the chosen button is highlighted and disabled and the other two stay
  // clickable, so the choice can still be changed.
  function initDifficultyMarking(): void {
    const buttons = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".mark-difficulty"),
    );

    buttons.forEach((button) => {
      const difficulty = button.dataset.difficulty;
      if (!isDifficulty(difficulty)) return;
      button.addEventListener("click", () =>
        markDifficulty(button, difficulty),
      );
    });

    function markDifficulty(
      clickedButton: HTMLButtonElement,
      difficulty: Difficulty,
    ): void {
      clickedButton.disabled = true;
      const originalHtml = clickedButton.innerHTML;
      clickedButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

      const formData = new FormData();
      formData.append("difficulty", difficulty);
      formData.append("csrfmiddlewaretoken", getCsrfToken());

      fetch(config.urls.markDifficulty, {
        method: "POST",
        body: formData,
      })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Server returned ${response.status}`);
          }
          window.showAlert("success", `Image marked as ${difficulty}`);
          clickedButton.innerHTML = originalHtml;
          updateDifficultyButtons(difficulty);
          updateDifficultyBadge(difficulty);
        })
        .catch((error) => {
          console.error("Error marking difficulty:", error);
          window.showAlert(
            "danger",
            "Error marking difficulty. Please try again.",
          );
          clickedButton.disabled = false;
          clickedButton.innerHTML = originalHtml;
        });
    }

    function updateDifficultyButtons(selected: Difficulty): void {
      buttons.forEach((button) => {
        const difficulty = button.dataset.difficulty;
        if (!isDifficulty(difficulty)) return;
        const color = DIFFICULTY_COLORS[difficulty];
        const isSelected = difficulty === selected;
        button.className = isSelected
          ? `btn btn-sm btn-${color}`
          : `btn btn-sm btn-outline-${color} mark-difficulty`;
        button.disabled = isSelected;
      });
    }

    // Matches the server-rendered badge in the image card header
    function updateDifficultyBadge(difficulty: Difficulty): void {
      const cardHeader = document.querySelector(
        "#image-preview-card .card-header",
      );
      if (!cardHeader) return;

      let badge = cardHeader.querySelector(".badge");
      if (!badge) {
        badge = document.createElement("span");
        cardHeader.appendChild(badge);
      }
      badge.className = `badge status-badge bg-${DIFFICULTY_COLORS[difficulty]}`;
      badge.textContent = capitalize(difficulty);
    }
  }

  function initWillNotGeoreference(): void {
    const button = document.getElementById(
      "willNotGeorefButton",
    ) as HTMLButtonElement | null;
    if (!button) return;

    button.addEventListener("click", function () {
      const formData = new FormData();
      formData.append("csrfmiddlewaretoken", getCsrfToken());
      button.disabled = true;

      fetch(config.urls.markWillNotGeoref, {
        method: "POST",
        body: formData,
      })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Server returned ${response.status}`);
          }
          window.showAlert("info", 'Image marked as "will not georeference"');
          setTimeout(() => {
            window.location.href = config.urls.imageDetail;
          }, 1000);
        })
        .catch((error) => {
          console.error("Error marking will not georeference:", error);
          window.showAlert(
            "danger",
            'Error marking image as "will not georeference". Please try again.',
          );
          button.disabled = false;
        });
    });
  }

  initSubjectEditor();
}
