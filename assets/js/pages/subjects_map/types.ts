// Properties carried by each feature of the `osm_elements` vector tile layer.
// Mirrors the SELECT in images/views/api.py::osm_elements_vector_tiles_endpoint.
export interface SubjectFeatureProperties {
  osm_id: number;
  // "N" | "W" | "R"; null for elements imported before the type was recorded.
  osm_type: string | null;
  geom_type: string;
  subject_name: string;
  subject_slug: string;
  // Comma-joined SubjectMapping image ids. Counts every mapping, including
  // non-public and duplicate images, so it drives the map highlight only —
  // never a user-facing count.
  image_ids: string;
  geometry_area: number;
}

// What the map hands the panel when a subject is hovered or pinned. Contains
// only what the vector tile knows; everything else is fetched.
export interface SubjectHit {
  slug: string;
  title: string;
  imageIds: number[];
}

// Response body of the subjects:subject_map_info endpoint.
export interface SubjectMapInfo {
  slug: string;
  title: string;
  url: string;
  description: string;
  thumbnail: string | null;
  total_images: number;
  georeferenced_images: number;
  wikidata_id: string | null;
  wikidata_url: string | null;
  // Year range built from the subject's Wikidata inception/demolished
  // dates, e.g. "1901–1910" or "1950–". Empty when neither date is known.
  date_range: string;
}

// Tile URLs templated by the page, read off the wrapper's data attributes.
export interface SubjectsMapUrls {
  imageTiles: string;
  osmElementTiles: string;
  // Below this zoom the server answers empty, so don't request at all.
  osmElementMinZoom: number;
}

// Bridge between the map modules and the Alpine panel: neither holds a
// reference to the other, they just trade these events on `window`.
export const SUBJECT_MAP_EVENTS = {
  // A hover preview: the panel may be replaced by the next hover.
  preview: "subjects-map:preview",
  // A click/tap: the panel sticks until explicitly dismissed.
  pin: "subjects-map:pin",
  // Nothing is under the pointer any more.
  clear: "subjects-map:clear",
  // The panel was dismissed from its own UI (close button or Escape).
  closed: "subjects-map:closed",
} as const;

// Stand-in slug in the URL templates rendered into data attributes.
export const SLUG_PLACEHOLDER = "__slug__";
