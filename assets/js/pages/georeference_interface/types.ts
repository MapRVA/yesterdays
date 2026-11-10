// Types for the point georeference interface. The config shapes mirror the
// window.georeferenceConfig object serialized by
// templates/images/georeference_interface.html.
import type { Map as MapLibreMap, Popup } from "maplibre-gl";
import type { TimeSliderControl } from "../../components/map_display/time_slider_control";

export type Difficulty = "easy" | "medium" | "hard";
export type DifficultyFilter = Difficulty | "unlabeled";
export type ContextDisplayMode = "hidden" | "ghost" | "clickable";
export type LocationHintType = "georeference" | "source" | "detected";

export interface LocationHint {
  type: LocationHintType;
  lat: number;
  lng: number;
  direction: number | null;
  label: string;
}

export interface SubjectHint {
  lat: number;
  lng: number;
  label: string;
}

export interface CurrentImageConfig {
  id: number;
  isGeoreferenced: boolean;
  title: string;
  permalink: string;
  originalUrl: string;
  description: string;
  dateDisplay: string;
  difficulty: string;
  collection: { name: string; source: { name: string } };
}

// The per-image URLs are null when no image is assigned; they are narrowed to
// ImagePageUrls before the map interface initializes.
export interface GeoreferenceConfigUrls {
  georeferenceImage: string | null;
  skipImage: string | null;
  markDifficulty: string | null;
  markWillNotGeoref: string | null;
  imageDetail: string | null;
  vectorTiles: string;
  georeferenceInterface: string;
  osmLogin: string;
}

export interface GeoreferenceConfig {
  currentImage: CurrentImageConfig | null;
  locationHint: LocationHint | null;
  subjectHints: SubjectHint[];
  isStaff: boolean;
  isAuthenticated: boolean;
  difficultyFilters: DifficultyFilter[];
  remainingCount: number;
  urls: GeoreferenceConfigUrls;
  csrfToken: string;
}

// GeoreferenceConfigUrls with every per-image URL guaranteed present.
export interface ImagePageUrls {
  georeferenceImage: string;
  skipImage: string;
  markDifficulty: string;
  markWillNotGeoref: string;
  imageDetail: string;
  vectorTiles: string;
}

// Mutable page state shared across the modules through the context object.
export interface GeoreferenceState {
  pinPlaced: boolean;
  currentDirection: number | null;
  isJoystickDragging: boolean;
  bearingLineEnabled: boolean;
  osdCenterlineOverlay: HTMLElement | null;
  contextDisplayMode: ContextDisplayMode;
  isHoveringContextImage: boolean;
  activePopup: Popup | null;
}

export interface GeoreferenceElements {
  submitButton: HTMLButtonElement;
  latitudeInput: HTMLInputElement;
  longitudeInput: HTMLInputElement;
  directionInput: HTMLInputElement;
  joystickContainer: HTMLElement;
  joystickHandle: HTMLElement;
  confidenceNotes: HTMLTextAreaElement;
  confidenceRadios: HTMLInputElement[];
  notesRequiredIndicator: HTMLElement | null;
  notesHelpText: HTMLElement | null;
  confidenceHighRadio: HTMLInputElement | null;
}

// Context image interactions need stable listener references so they can be
// detached again; ./context_images builds this controller around them.
export interface ContextImagesController {
  updateDisplay(): void;
}

export interface GeoreferenceContext {
  map: MapLibreMap;
  config: GeoreferenceConfig;
  image: CurrentImageConfig;
  urls: ImagePageUrls;
  els: GeoreferenceElements;
  state: GeoreferenceState;
  contextImages?: ContextImagesController;
  // Assigned once the context images reveal a filterable date range
  timeSlider?: TimeSliderControl;
}

// JSON response shape of the georeference and skip endpoints.
export interface ActionResponse {
  success: boolean;
  error?: string;
}
