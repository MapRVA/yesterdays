import type maplibregl from "maplibre-gl";

// Where the histogram reads its images: a vector source's point layer, with
// an optional filter matching the layers' base filter (e.g. excluding the
// image being georeferenced).
export interface TimeSliderHistogramSource {
  sourceId: string;
  sourceLayer: string;
  filter?: maplibregl.FilterSpecification | null;
}

/**
 * Count the dated images among the source's loaded features that fall inside
 * the map's current view, one bin per year from minYear to maxYear.
 *
 * An image dated to an interval (its fuzzy EDTF start/end decimal years) is
 * spread evenly across it: each year receives the fraction of the interval
 * it overlaps, so every image contributes a total weight of 1. Weight falling
 * outside the range folds into the end bins, matching the slider's open ends
 * ("1827 and earlier", "this year and later"). Undated images are skipped.
 *
 * Low-zoom tiles hold clusters, not images: one feature per tile-grid cell
 * and fuzzy start/end, standing for `n` images that share them. A cluster
 * adds its interval n times over, which is exactly what its images would add.
 */
export function computeYearHistogram(
  map: maplibregl.Map,
  source: TimeSliderHistogramSource,
  minYear: number,
  maxYear: number,
): Float64Array {
  const bins = new Float64Array(maxYear - minYear + 1);
  if (!map.getSource(source.sourceId)) return bins;

  const features = map.querySourceFeatures(source.sourceId, {
    sourceLayer: source.sourceLayer,
    filter: source.filter ?? undefined,
  });
  const bounds = map.getBounds();
  // An image on a tile edge appears in both tiles. Clusters (no id) never do:
  // the server gives each edge point to exactly one tile.
  const seen = new Set<unknown>();

  for (const feature of features) {
    const props = feature.properties;
    const start = props.fuzzy_start_decdate;
    if (typeof start !== "number") continue;
    const id = props.id;
    if (id !== undefined && seen.has(id)) continue;
    if (feature.geometry.type !== "Point") continue;
    const point = feature.geometry.coordinates as [number, number];
    if (!bounds.contains(point)) continue;
    if (id !== undefined) seen.add(id);

    const rawEnd = props.fuzzy_end_decdate;
    const end = typeof rawEnd === "number" && rawEnd > start ? rawEnd : start;
    const count = typeof props.n === "number" ? props.n : 1;
    addInterval(bins, minYear, maxYear, start, end, count);
  }
  return bins;
}

// Spread `count` images' unit weights over [start, end), year bins
// [y, y + 1).
function addInterval(
  bins: Float64Array,
  minYear: number,
  maxYear: number,
  start: number,
  end: number,
  count: number,
): void {
  const last = bins.length - 1;
  const add = (bin: number, weight: number) => {
    bins[bin] = (bins[bin] ?? 0) + weight * count;
  };

  if (end <= start) {
    add(Math.min(last, Math.max(0, Math.floor(start) - minYear)), 1);
    return;
  }

  const length = end - start;
  // Whole stretches beyond either end fold into that end's bin directly,
  // so a wide interval far outside the range costs nothing to spread.
  if (start < minYear) add(0, (Math.min(end, minYear) - start) / length);
  if (end > maxYear + 1) {
    add(last, (end - Math.max(start, maxYear + 1)) / length);
  }

  const from = Math.max(Math.floor(start), minYear);
  const to = Math.min(Math.ceil(end) - 1, maxYear);
  for (let year = from; year <= to; year++) {
    const overlap = Math.min(end, year + 1) - Math.max(start, year);
    if (overlap > 0) add(year - minYear, overlap / length);
  }
}

/**
 * An SVG path tracing the histogram as adjoining bars, in a viewBox where x is
 * years from minYear (bin i centered on x = i, so bars line up with the
 * slider's thumb positions) and y runs 0 (tallest bin) to 1 (baseline).
 * Empty when there's nothing to draw.
 *
 * Heights are logarithmic, ln(1 + count) against the tallest bin's, so one
 * busy year doesn't flatten the rest; the +1 keeps empty years at zero.
 */
export function histogramPath(bins: Float64Array): string {
  let peak = 0;
  for (const value of bins) peak = Math.max(peak, value);
  if (peak === 0) return "";
  const logPeak = Math.log1p(peak);

  const parts = [`M -0.5 1`];
  bins.forEach((value, i) => {
    const top = 1 - Math.log1p(value) / logPeak;
    parts.push(`V ${top.toFixed(4)} H ${i + 0.5}`);
  });
  parts.push("V 1 Z");
  return parts.join(" ");
}
