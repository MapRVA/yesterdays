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
import { Geoman } from "@geoman-io/maplibre-geoman-free";
import { initialMapView } from "../constants/map";
import "../components/map_display/pmtiles_protocol";
import { initialMapStyle, LayerControl } from "../components/layer_control";
import { initSubjectEditor } from "../components/subject_editor.js";
import { initImageViewer } from "../components/image_viewer";

function getBootstrapColor(difficulty) {
  const colors = { easy: "success", medium: "warning", hard: "danger" };
  return colors[difficulty] || "secondary";
}

document.addEventListener("DOMContentLoaded", function () {
  // Check if configuration is available
  if (!window.fromAboveConfig) {
    console.error("From Above configuration not found");
    return;
  }

  const config = window.fromAboveConfig;

  initImageViewer();

  const submitButton = document.getElementById("submitButton");
  const backButton = document.getElementById("backButton");
  const willNotGeorefButton = document.getElementById("willNotGeorefButton");
  const markDifficultyButtons = document.querySelectorAll(".mark-difficulty");
  const confidenceNotes = document.getElementById("confidence-notes");
  const isStaff = config.isStaff;
  const isAuthenticated = config.isAuthenticated;

  window.setupPMTilesProtocol();

  var map = new maplibregl.Map({
    container: "mymap",
    style: initialMapStyle(),
    ...initialMapView(),
  });
  // Track polygon data
  var drawnPolygon = null;
  var gm = null;
  var currentPolygonId = null;
  var currentFeatureRef = null; // direct reference to the Geoman feature object
  var isEditing = !config.existingPolygon; // new georefs are "edited" by default
  var confidenceSelected = false;

  // Geoman options - only show polygon and erase tools
  var geomanOptions = {
    position: "top-left",
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

  // Named event handlers so they can be detached and re-attached across
  // Geoman re-initializations (e.g. after style swaps).
  async function handleGmCreate(event) {
    if (event.shape === "polygon") {
      console.log("Polygon created:", event.feature.id);

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

      // If there's already a polygon, remove it
      if (
        previousPolygonId !== null &&
        previousPolygonId !== newFeature.id
      ) {
        console.log("Removing previous polygon:", previousPolygonId);

        try {
          await gm.features.delete(previousFeature || previousPolygonId);
        } catch (e) {
          console.warn("Error removing polygon:", e);
          showAlert(
            "danger",
            "The previous polygon could not be removed. Please try again.",
          );
        }
      }

      // Update polygon data
      updatePolygonData();
    }
  }

  function handleGmRemove(event) {
    if (event.feature && event.feature.id === currentPolygonId) {
      console.log("Polygon removed by user");
      currentPolygonId = null;
      currentFeatureRef = null;
      updatePolygonData();
    }
  }

  function handleGmEditEnd(event) {
    console.log("Polygon edited (vertex change)");
    isEditing = true;
    if (event.feature) {
      currentFeatureRef = event.feature;
    }
    updatePolygonData();
  }

  function handleGmDragEnd(event) {
    console.log("Polygon dragged");
    isEditing = true;
    if (event.feature) {
      currentFeatureRef = event.feature;
    }
    updatePolygonData();
  }

  // Initialize (or re-initialize) Geoman and restore any existing polygon.
  // Called on initial map load and after style swaps, which destroy all
  // MapLibre sources/layers and corrupt Geoman's internal state.
  async function initializeGeoman(polygonToRestore) {
    // Tear down previous instance if one exists — its internal source/layer
    // references are stale after setStyle() and init() will refuse to
    // re-create them.
    if (gm) {
      // Detach handlers before destroying to avoid firing on stale features
      map.off("gm:create", handleGmCreate);
      map.off("gm:remove", handleGmRemove);
      map.off("gm:editend", handleGmEditEnd);
      map.off("gm:dragend", handleGmDragEnd);
      try {
        await gm.destroy();
      } catch (e) {
        console.warn("Error destroying previous Geoman instance:", e);
      }
      gm = null;
      currentPolygonId = null;
      currentFeatureRef = null;
    }

    if (!Geoman) {
      throw new Error("Geoman library not loaded.");
    }

    gm = new Geoman(map, geomanOptions);

    // Wait for Geoman to finish loading before importing features
    await new Promise((resolve) => {
      map.once("gm:loaded", resolve);
    });

    // Restore polygon if one was provided
    if (polygonToRestore) {
      var feature = {
        type: "Feature",
        geometry: polygonToRestore,
        properties: { shape: "polygon" },
      };
      var imported = await gm.features.importGeoJsonFeature(feature);
      if (imported) {
        currentPolygonId = imported.id;
        currentFeatureRef = imported;
        drawnPolygon = imported.getGeoJson
          ? imported.getGeoJson()
          : feature;
      }
    }

    // Attach event handlers
    map.on("gm:create", handleGmCreate);
    map.on("gm:remove", handleGmRemove);
    map.on("gm:editend", handleGmEditEnd);
    map.on("gm:dragend", handleGmDragEnd);
  }

  // Add LayerControl to map
  // Note: Geoman creates layers dynamically with "gm_" prefix. The LayerControl's isOverlayLayer()
  // already recognizes these. We don't set beforeLayerId since Geoman layers are created after
  // map load, so we rely on the fallback logic and moveLayer() to reposition overlays correctly.
  map.addControl(
    new LayerControl({
      onStyleSwap: async () => {
        // setStyle() destroys all MapLibre sources/layers, but Geoman's
        // internal state still holds stale references and its init() skips
        // re-creation. The only reliable fix is to destroy and re-create
        // the Geoman instance, then re-import the user's polygon.
        var savedGeometry = drawnPolygon?.geometry || null;
        try {
          await initializeGeoman(savedGeometry);
        } catch (e) {
          console.error("Error re-initializing Geoman after style swap:", e);
          showAlert(
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

  // Initialize Geoman after map loads
  map.on("load", async function () {
    try {
      await initializeGeoman(config.existingPolygon || null);

      // Fit map to the existing polygon bounds (only on initial load)
      if (config.existingPolygon) {
        var bounds = new maplibregl.LngLatBounds();
        config.existingPolygon.coordinates[0].forEach(function (c) {
          bounds.extend(c);
        });
        map.fitBounds(bounds, { padding: 50 });
      }
    } catch (e) {
      console.error("Error initializing Geoman:", e);
      showAlert(
        "danger",
        "Failed to initialize polygon drawing tools: " + e.message,
      );
    }
  });

  function updateSubmitButton() {
    if (submitButton) {
      submitButton.disabled = !(
        drawnPolygon &&
        isEditing &&
        confidenceSelected
      );
    }
  }

  function updatePolygonData() {
    try {
      if (!gm || !gm.features) {
        console.log("Geoman features not yet available");
        return;
      }

      // Get the current polygon if it exists
      drawnPolygon = null;

      gm.features.forEach(function (feature) {
        if (feature.id === currentPolygonId) {
          console.log("Found current polygon:", feature.id);
          drawnPolygon = feature.getGeoJson();
        }
      });

      // Imported features may not appear in gm.features iteration —
      // fall back to the stored reference
      if (!drawnPolygon && currentFeatureRef && currentFeatureRef.getGeoJson) {
        drawnPolygon = currentFeatureRef.getGeoJson();
      }

      // Update submit button state — require polygon, edit, and confidence
      updateSubmitButton();
    } catch (e) {
      console.warn("Error querying polygon data:", e);
    }
  }

  const confidenceRadios = document.querySelectorAll(
    'input[name="confidence"]',
  );
  const notesRequiredIndicator = document.getElementById(
    "notes-required-indicator",
  );
  const notesHelpText = document.getElementById("notes-help-text");

  // Handle confidence level changes
  if (confidenceRadios && confidenceRadios.length > 0) {
    confidenceRadios.forEach((radio) => {
      radio.addEventListener("change", function () {
        confidenceSelected = true;
        if (this.value === "low") {
          if (notesRequiredIndicator)
            notesRequiredIndicator.style.display = "inline";
          if (notesHelpText) notesHelpText.style.display = "block";
          if (confidenceNotes) confidenceNotes.required = true;
        } else {
          if (notesRequiredIndicator)
            notesRequiredIndicator.style.display = "none";
          if (notesHelpText) notesHelpText.style.display = "none";
          if (confidenceNotes) confidenceNotes.required = false;
        }
        updateSubmitButton();
      });
    });
  }

  // Handle back button
  if (backButton) {
    backButton.addEventListener("click", function () {
      window.location.href = config.urls.imageDetail;
    });
  }

  // Handle submit button
  submitButton.addEventListener("click", function () {
    if (!drawnPolygon) {
      showAlert("warning", "Please draw a polygon on the map first");
      return;
    }

    const selectedConfidence = document.querySelector(
      'input[name="confidence"]:checked',
    );
    if (!selectedConfidence) {
      showAlert("warning", "Please select a confidence level");
      return;
    }

    if (
      selectedConfidence.value === "low" &&
      confidenceNotes.value.trim() === ""
    ) {
      showAlert(
        "warning",
        "Low confidence georeferences must include a descriptive note",
      );
      confidenceNotes.focus();
      return;
    }

    performSubmission();
  });

  // Function to handle the actual submission
  function performSubmission() {
    if (!drawnPolygon || !drawnPolygon.geometry) {
      showAlert("danger", "Invalid polygon data");
      return;
    }

    const notes = confidenceNotes.value.trim();
    const selectedConfidence = document.querySelector(
      'input[name="confidence"]:checked',
    );
    const confidence = selectedConfidence ? selectedConfidence.value : "medium";

    const data = {
      polygon: drawnPolygon.geometry,
      notes: notes,
      confidence: confidence,
    };

    // Get CSRF token
    const csrfToken =
      document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
      document
        .querySelector('meta[name="csrf-token"]')
        ?.getAttribute("content") ||
      config.csrfToken;

    submitButton.disabled = true;
    submitButton.innerHTML =
      '<i class="fas fa-spinner fa-spin me-2"></i>Submitting...';

    fetch(config.urls.aerialGeoreferenceImage, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(data),
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.success) {
          showAlert(
            "success",
            "Polygonal georeference submitted successfully!",
          );
          setTimeout(() => {
            window.location.href = config.urls.imageDetail;
          }, 1500);
        } else {
          showAlert("danger", "Error: " + data.error);
          submitButton.disabled = false;
          submitButton.innerHTML =
            '<i class="fas fa-check me-2"></i>Submit Aerial Georeference';
        }
      })
      .catch((error) => {
        showAlert("danger", "Network error occurred");
        submitButton.disabled = false;
        submitButton.innerHTML =
          '<i class="fas fa-check me-2"></i>Submit Aerial Georeference';
      });
  }

  // Difficulty marking functionality (admin only)
  function handleDifficultyClick() {
    const difficulty = this.dataset.difficulty;
    const clickedButton = this;
    const csrfToken =
      document.querySelector('[name="csrfmiddlewaretoken"]')?.value ||
      config.csrfToken;

    clickedButton.disabled = true;
    const originalText = clickedButton.innerHTML;
    clickedButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

    const formData = new FormData();
    formData.append("difficulty", difficulty);
    formData.append("csrfmiddlewaretoken", csrfToken);

    fetch(config.urls.markDifficulty, {
      method: "POST",
      body: formData,
    })
      .then((response) => {
        if (response.ok) {
          showAlert("success", `Image marked as ${difficulty}`);
          clickedButton.innerHTML =
            difficulty.charAt(0).toUpperCase() + difficulty.slice(1);
          clickedButton.disabled = false;

          clickedButton.offsetHeight;
          setTimeout(() => {
            updateDifficultyButtons(difficulty);
            updateDifficultyBadge(difficulty);
          }, 10);
        } else {
          throw new Error("Network response was not ok");
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        showAlert("danger", "Error marking difficulty. Please try again.");
        clickedButton.disabled = false;
        clickedButton.innerHTML = originalText;
      });
  }

  function updateDifficultyButtons(newDifficulty) {
    let buttonGroup = null;
    document.querySelectorAll(".btn-group").forEach((group) => {
      const buttons = group.querySelectorAll("button");
      buttons.forEach((btn) => {
        const text = btn.textContent.toLowerCase().trim();
        if (
          btn.hasAttribute("data-difficulty") ||
          btn.classList.contains("mark-difficulty") ||
          ["easy", "medium", "hard"].some((d) => text.includes(d))
        ) {
          buttonGroup = group;
        }
      });
    });

    if (!buttonGroup) return;

    const buttons = buttonGroup.querySelectorAll("button");

    buttons.forEach((button) => {
      const buttonText = button.textContent.toLowerCase().trim();
      let buttonDifficulty = ["easy", "medium", "hard"].find((d) =>
        buttonText.includes(d),
      );

      if (buttonDifficulty === newDifficulty) {
        button.className = `btn btn-sm btn-${getBootstrapColor(newDifficulty)}`;
        button.disabled = true;
        button.removeAttribute("data-difficulty");
      } else {
        button.className = `btn btn-sm btn-outline-${getBootstrapColor(buttonDifficulty)} mark-difficulty`;
        button.disabled = false;
        button.setAttribute("data-difficulty", buttonDifficulty);

        if (!button.hasEventListener) {
          button.addEventListener("click", handleDifficultyClick);
          button.hasEventListener = true;
        }
      }
      button.innerHTML =
        buttonDifficulty.charAt(0).toUpperCase() + buttonDifficulty.slice(1);
    });
  }

  function updateDifficultyBadge(newDifficulty) {
    const cardHeader = document.querySelector(".card-header");
    if (!cardHeader) return;

    let difficultyBadge = cardHeader.querySelector(".badge");

    if (difficultyBadge) {
      difficultyBadge.className = `badge bg-${getBootstrapColor(newDifficulty)}`;
      difficultyBadge.innerHTML =
        newDifficulty.charAt(0).toUpperCase() + newDifficulty.slice(1);
    } else {
      const badgeContainer = cardHeader.querySelector("h5");
      if (badgeContainer) {
        const newBadge = document.createElement("span");
        newBadge.className = `badge bg-${getBootstrapColor(newDifficulty)} ms-2`;
        newBadge.innerHTML =
          newDifficulty.charAt(0).toUpperCase() + newDifficulty.slice(1);
        badgeContainer.appendChild(newBadge);
      }
    }
  }

  // Initialize difficulty buttons
  if (isStaff) {
    markDifficultyButtons.forEach((button) => {
      button.addEventListener("click", handleDifficultyClick);
      button.hasEventListener = true;
    });
  }

  // Handle will not georeference (only for staff users)
  if (willNotGeorefButton && isStaff) {
    willNotGeorefButton.addEventListener("click", function () {
      const csrfToken =
        document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
        document
          .querySelector('meta[name="csrf-token"]')
          ?.getAttribute("content") ||
        config.csrfToken;
      const formData = new FormData();
      formData.append("csrfmiddlewaretoken", csrfToken);

      fetch(config.urls.markWillNotGeoref, {
        method: "POST",
        body: formData,
      }).then((response) => {
        if (response.ok) {
          showAlert("info", 'Image marked as "will not georeference"');
          setTimeout(() => {
            window.location.href = config.urls.imageDetail;
          }, 1000);
        }
      });
    });
  }

  // Helper function to show alerts
  function showAlert(type, message) {
    const alertDiv = document.createElement("div");
    alertDiv.className = `alert alert-${type} alert-dismissible fade show position-fixed`;
    alertDiv.style.cssText =
      "top: 20px; right: 20px; z-index: 9999; min-width: 300px;";
    alertDiv.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;

    document.body.appendChild(alertDiv);

    setTimeout(() => {
      alertDiv.remove();
    }, 5000);
  }

  // Initialize subject editor component
  initSubjectEditor();
});
