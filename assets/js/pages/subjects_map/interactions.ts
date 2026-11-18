import type {
  GeoJSONSource,
  MapGeoJSONFeature,
  Map as MapLibreMap,
  Point,
} from "maplibre-gl";
import {
  IMAGE_CIRCLES_LAYER,
  IMAGE_DIRECTIONS_LAYER,
  SUBJECT_CLUSTERS_LAYER,
  SUBJECT_LAYER_IDS,
  imageDirectionsFilter,
} from "./layers";
import { SUBJECT_MAP_EVENTS } from "./types";
import type { SubjectFeatureProperties, SubjectHit } from "./types";

// Restyling the image points and refiltering their arrows is the expensive
// part of a hover, so it trails the panel update by a beat.
const HIGHLIGHT_DEBOUNCE_MS = 10;

export interface SubjectInteractions {
  // Re-applies the current highlight after a style swap has rebuilt the
  // layers from scratch.
  reapplyHighlight(): void;
}

// Wires hover previews, click-to-pin, and the image-point highlight. State
// reaches the panel as window CustomEvents, so this module knows nothing
// about the DOM beyond the map canvas.
export function registerSubjectInteractions(
  map: MapLibreMap,
): SubjectInteractions {
  // The subject key the user committed to by clicking. While set, hovering other
  // subjects must not steal the panel.
  let pinnedKey: string | null = null;
  let hoveredKey: string | null = null;
  let highlightedImageIds: number[] = [];
  let highlightTimeout: number | undefined;

  function dispatch(name: string, detail?: SubjectHit): void {
    window.dispatchEvent(new CustomEvent(name, { detail }));
  }

  // Layers are absent between a setStyle() and the following styledata, and
  // querying a missing layer makes MapLibre fire an error event.
  function queryableLayers(): string[] {
    return SUBJECT_LAYER_IDS.filter((id) => map.getLayer(id));
  }

  function clusterAt(point: Point): MapGeoJSONFeature | null {
    if (!map.getLayer(SUBJECT_CLUSTERS_LAYER)) return null;
    return (
      map.queryRenderedFeatures(point, { layers: [SUBJECT_CLUSTERS_LAYER] })[0] ??
      null
    );
  }

  // Zooms to where the cluster splits apart, centred on it.
  async function expandCluster(cluster: MapGeoJSONFeature): Promise<void> {
    if (cluster.geometry.type !== "Point") return;
    const source = map.getSource<GeoJSONSource>(cluster.source);
    if (!source) return;
    const zoom = await source.getClusterExpansionZoom(
      cluster.properties.cluster_id as number,
    );
    map.easeTo({
      center: cluster.geometry.coordinates as [number, number],
      zoom,
    });
  }

  function subjectAt(point: Point): SubjectHit | null {
    const layers = queryableLayers();
    if (layers.length === 0) return null;

    const feature = map.queryRenderedFeatures(point, { layers })[0];
    if (!feature) return null;

    const properties = feature.properties as Partial<SubjectFeatureProperties>;
    if (!properties.subject_key) return null;

    return {
      key: properties.subject_key,
      title: properties.subject_name || "Unknown subject",
      imageIds: (properties.image_ids ?? "")
        .split(",")
        .filter(Boolean)
        .map(Number)
        .filter((id) => !Number.isNaN(id)),
    };
  }

  // Reveals exactly the given images and hides every other point. An empty
  // list restores the default state, where no image points are drawn.
  function applyHighlight(imageIds: number[]): void {
    highlightedImageIds = imageIds;

    const opacity = imageIds.length
      ? ["case", ["in", ["get", "id"], ["literal", imageIds]], 1, 0]
      : 0;

    if (map.getLayer(IMAGE_CIRCLES_LAYER)) {
      map.setPaintProperty(IMAGE_CIRCLES_LAYER, "circle-opacity", opacity);
      map.setPaintProperty(
        IMAGE_CIRCLES_LAYER,
        "circle-stroke-opacity",
        opacity,
      );
    }
    // Arrows are filtered rather than faded: see imageDirectionsFilter.
    if (map.getLayer(IMAGE_DIRECTIONS_LAYER)) {
      map.setFilter(IMAGE_DIRECTIONS_LAYER, imageDirectionsFilter(imageIds));
    }
  }

  function reset(): void {
    pinnedKey = null;
    hoveredKey = null;
    window.clearTimeout(highlightTimeout);
    applyHighlight([]);
  }

  map.on("mousemove", (e) => {
    const hit = subjectAt(e.point);
    map.getCanvas().style.cursor = hit || clusterAt(e.point) ? "pointer" : "";

    if (pinnedKey) return;

    if (!hit) {
      if (hoveredKey === null) return;
      hoveredKey = null;
      window.clearTimeout(highlightTimeout);
      applyHighlight([]);
      dispatch(SUBJECT_MAP_EVENTS.clear);
      return;
    }

    if (hit.key === hoveredKey) return;
    hoveredKey = hit.key;
    dispatch(SUBJECT_MAP_EVENTS.preview, hit);

    window.clearTimeout(highlightTimeout);
    highlightTimeout = window.setTimeout(
      () => applyHighlight(hit.imageIds),
      HIGHLIGHT_DEBOUNCE_MS,
    );
  });

  // A tap arrives here too, which is what makes the panel reachable on
  // touch: it pins the subject instead of navigating away from the map.
  map.on("click", (e) => {
    const cluster = clusterAt(e.point);
    if (cluster) {
      void expandCluster(cluster);
      return;
    }

    const hit = subjectAt(e.point);

    if (!hit) {
      if (!pinnedKey) return;
      reset();
      dispatch(SUBJECT_MAP_EVENTS.clear);
      return;
    }

    pinnedKey = hit.key;
    hoveredKey = hit.key;
    window.clearTimeout(highlightTimeout);
    applyHighlight(hit.imageIds);
    dispatch(SUBJECT_MAP_EVENTS.pin, hit);
  });

  window.addEventListener(SUBJECT_MAP_EVENTS.closed, () => reset());

  return {
    reapplyHighlight: () => applyHighlight(highlightedImageIds),
  };
}
