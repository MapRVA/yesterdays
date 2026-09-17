// Submitting, skipping, and staff-marking the current image, plus the form
// reset that advances to the next one.
import type { GeoJSONSource } from "maplibre-gl";
import { getCsrfToken } from "./csrf";
import { emptyFeatureCollection } from "./geojson";
import { updateJoystickHandle } from "./pin_direction";
import type { ActionResponse, GeoreferenceContext } from "./types";

function submitButtonIdleLabel(ctx: GeoreferenceContext): string {
  return ctx.image.isGeoreferenced
    ? '<i class="fas fa-edit me-2"></i>Submit Correction'
    : '<i class="fas fa-check me-2"></i>Submit Georeference';
}

async function performSubmission(ctx: GeoreferenceContext): Promise<void> {
  const { els, state, urls } = ctx;

  const lat = parseFloat(els.latitudeInput.value);
  const lng = parseFloat(els.longitudeInput.value);
  const dir =
    state.currentDirection !== null ? Math.round(state.currentDirection) : null;
  const notes = els.confidenceNotes.value.trim();
  const selectedConfidence = document.querySelector<HTMLInputElement>(
    'input[name="confidence"]:checked',
  );
  let confidence = selectedConfidence ? selectedConfidence.value : "medium";

  // Check for the override from the modal
  const forceHighConfidenceCheckbox = document.getElementById(
    "force-high-confidence-checkbox",
  ) as HTMLInputElement | null;
  if (forceHighConfidenceCheckbox && forceHighConfidenceCheckbox.checked) {
    confidence = "high";
  }

  const data = {
    latitude: lat,
    longitude: lng,
    direction: dir,
    notes: notes,
    confidence: confidence,
  };

  els.submitButton.disabled = true;
  els.submitButton.innerHTML = ctx.image.isGeoreferenced
    ? '<i class="fas fa-spinner fa-spin me-2"></i>Submitting Correction...'
    : '<i class="fas fa-spinner fa-spin me-2"></i>Submitting...';

  try {
    const response = await fetch(urls.georeferenceImage, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(ctx.config),
      },
      body: JSON.stringify(data),
    });
    const result = (await response.json()) as ActionResponse;

    if (result.success) {
      window.showAlert(
        "success",
        ctx.image.isGeoreferenced
          ? "Correction submitted successfully!"
          : "Image georeferenced successfully!",
      );
      // Clear the form and reset for next image
      setTimeout(() => {
        // Check if we came from a specific image
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.has("image")) {
          // Redirect back to the image detail page
          window.location.href = urls.imageDetail;
        } else {
          clearFormAndGetNext(ctx);
        }
      }, 1500);
    } else {
      window.showAlert("danger", "Error: " + result.error);
      els.submitButton.disabled = false;
      els.submitButton.innerHTML = submitButtonIdleLabel(ctx);
    }
  } catch {
    window.showAlert("danger", "Network error occurred");
    els.submitButton.disabled = false;
    els.submitButton.innerHTML = submitButtonIdleLabel(ctx);
  }
}

export function clearFormAndGetNext(ctx: GeoreferenceContext): void {
  const { map, els, state } = ctx;

  // Clear coordinate inputs
  els.latitudeInput.value = "";
  els.longitudeInput.value = "";
  els.directionInput.value = "";
  els.directionInput.classList.add("inactive");
  els.confidenceNotes.value = "";

  // Clear confidence selection
  els.confidenceRadios.forEach((radio) => (radio.checked = false));
  if (els.notesRequiredIndicator) {
    els.notesRequiredIndicator.style.display = "none";
  }
  if (els.notesHelpText) els.notesHelpText.style.display = "none";
  els.confidenceNotes.required = false;

  // Clear map
  map.getSource<GeoJSONSource>("pin")?.setData(emptyFeatureCollection());
  map
    .getSource<GeoJSONSource>("bearing-line")
    ?.setData(emptyFeatureCollection());

  // Reset state
  state.pinPlaced = false;
  state.currentDirection = null;
  els.submitButton.disabled = true;
  updateJoystickHandle(ctx, 0, 0);

  // Reset confidence validation
  if (els.confidenceHighRadio) els.confidenceHighRadio.disabled = false;

  // Scroll to top and reload to get next image. A reload re-applies the
  // scroll offset saved on this history entry, which on mobile drops the next
  // image partway down the page (and lands late, once the viewer and map have
  // grown the document), so opt out of restoration for this entry first.
  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }
  window.scrollTo(0, 0);

  // Remove current_image parameter if present to avoid redirecting back to
  // the same image
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.has("current_image")) {
    urlParams.delete("current_image");
    window.location.href =
      window.location.pathname +
      (urlParams.toString() ? "?" + urlParams.toString() : "");
  } else {
    location.reload();
  }
}

// The submit button's validation flow, including the confirmation modal shown
// when no direction is set.
export function initSubmitFlow(ctx: GeoreferenceContext): void {
  const { els, state } = ctx;

  els.submitButton.addEventListener("click", () => {
    if (!state.pinPlaced) {
      window.showAlert("warning", "Please select a location on the map first");
      return;
    }

    // Check if confidence level is selected
    const selectedConfidence = document.querySelector<HTMLInputElement>(
      'input[name="confidence"]:checked',
    );
    if (!selectedConfidence) {
      window.showAlert("warning", "Please select a confidence level");
      return;
    }

    // Check if notes are required for low confidence
    if (
      selectedConfidence.value === "low" &&
      els.confidenceNotes.value.trim() === ""
    ) {
      window.showAlert(
        "warning",
        "Low confidence georeferences must include a descriptive note",
      );
      els.confidenceNotes.focus();
      return;
    }

    // Check if direction is missing and show confirmation modal
    if (state.currentDirection === null) {
      const forceHighConfidenceContainer = document.getElementById(
        "force-high-confidence-container",
      );
      const forceHighConfidenceCheckbox = document.getElementById(
        "force-high-confidence-checkbox",
      ) as HTMLInputElement | null;

      if (forceHighConfidenceContainer) {
        forceHighConfidenceContainer.style.display =
          selectedConfidence.value === "medium" ? "block" : "none";
      }
      if (forceHighConfidenceCheckbox) {
        forceHighConfidenceCheckbox.checked = false; // Always reset checkbox
      }

      const modalElement = document.getElementById("directionConfirmModal");
      if (modalElement) {
        new window.bootstrap.Modal(modalElement).show();
      }
      return; // Don't proceed with submission yet
    }

    // Proceed with submission
    void performSubmission(ctx);
  });

  // Handle confirmation button in modal
  document
    .getElementById("confirmSubmitWithoutDirection")
    ?.addEventListener("click", () => {
      const modalElement = document.getElementById("directionConfirmModal");
      if (modalElement) {
        window.bootstrap.Modal.getInstance(modalElement)?.hide();
      }
      void performSubmission(ctx);
    });
}

// Handle skip button (available to everyone)
export function initSkipButton(ctx: GeoreferenceContext): void {
  const skipButton = document.getElementById("skipButton");
  if (!skipButton) return;

  skipButton.addEventListener("click", async () => {
    // Check if we came from a specific image (image parameter in URL)
    const urlParams = new URLSearchParams(window.location.search);
    const specificImageRequested = urlParams.has("image");

    const response = await fetch(ctx.urls.skipImage, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(ctx.config),
      },
      body: JSON.stringify({ reason: "Skipped during georeferencing" }),
    });
    const result = (await response.json()) as ActionResponse;

    if (result.success) {
      window.showAlert("info", "Image skipped");
      setTimeout(() => {
        if (specificImageRequested) {
          // Redirect back to the image detail page
          window.location.href = ctx.urls.imageDetail;
        } else {
          clearFormAndGetNext(ctx);
        }
      }, 1000);
    } else {
      window.showAlert("danger", "Error: " + result.error);
    }
  });
}

// Handle will not georeference (only for staff users)
export function initWillNotGeoreference(ctx: GeoreferenceContext): void {
  const willNotGeorefButton = document.getElementById("willNotGeorefButton");
  if (!willNotGeorefButton || !ctx.config.isStaff) return;

  willNotGeorefButton.addEventListener("click", async () => {
    const formData = new FormData();
    formData.append("csrfmiddlewaretoken", getCsrfToken(ctx.config));

    const response = await fetch(ctx.urls.markWillNotGeoref, {
      method: "POST",
      body: formData,
    });
    if (!response.ok) return;

    window.showAlert("info", 'Image marked as "will not georeference"');
    setTimeout(() => {
      // Check if we came from a specific image
      const urlParams = new URLSearchParams(window.location.search);
      if (urlParams.has("image")) {
        // Redirect back to the image detail page
        window.location.href = ctx.urls.imageDetail;
      } else {
        clearFormAndGetNext(ctx);
      }
    }, 1000);
  });
}
