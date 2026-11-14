import type maplibregl from "maplibre-gl";
import { LAYER_IDS, SOURCE_IDS } from "./layer_ids";
import {
  computeYearHistogram,
  histogramPath,
  type TimeSliderHistogramSource,
} from "./time_slider_histogram";
import { bubblePath } from "./time_slider_bubble";

// A layer the slider filters by date range. requiresDirection and extraFilter
// reproduce the layer's base filter, which setFilter() overwrites wholesale.
export interface TimeSliderTarget {
  layerId: string;
  requiresDirection?: boolean;
  extraFilter?: maplibregl.ExpressionSpecification | null;
}

// The map_display image point layers, filtered by default.
export const DEFAULT_TIME_SLIDER_TARGETS: TimeSliderTarget[] = [
  { layerId: LAYER_IDS.imageHeatmap },
  { layerId: LAYER_IDS.imageCircles },
  { layerId: LAYER_IDS.imageDirections, requiresDirection: true },
  { layerId: LAYER_IDS.imageCirclesSimple },
  { layerId: LAYER_IDS.imageDirectionsSimple, requiresDirection: true },
];

// The map_display images, counted by the histogram by default.
export const DEFAULT_HISTOGRAM_SOURCE: TimeSliderHistogramSource = {
  sourceId: SOURCE_IDS.images,
  sourceLayer: "image_points",
};

// The same images from the per-image source the simple style reads: only the
// source behind the visible layers has tiles loaded, so the histogram follows
// the display style.
export const POINTS_HISTOGRAM_SOURCE: TimeSliderHistogramSource = {
  sourceId: SOURCE_IDS.imagePoints,
  sourceLayer: "image_points",
};

// The slider's fixed span: from settings.TIME_SLIDER_MIN_YEAR (the oldest
// surviving photograph) to the current year. Fixed rather than fitted to the
// loaded images, so the scale doesn't shift as the map pans.
const DEFAULT_MIN_YEAR = 1827;

// The shortest gap between filter updates while the range is being adjusted.
// Refiltering the map's layers is costly, so a drag refreshes the map at this
// interval rather than on every input event.
const FILTER_INTERVAL_MS = 250;

export interface TimeSliderOptions {
  // Whether the card starts collapsed. Collapsed by default, since it crowds
  // a small embedded map; the full /map/ page opens it.
  collapsed?: boolean;
}

/**
 * Date-range filter shown as a collapsible Bootstrap card in the map's
 * bottom-right corner. Add it with `map.addControl(slider, "bottom-right")`.
 */
export class TimeSliderControl implements maplibregl.IControl {
  _minYear: number;
  _maxYear: number;
  _mapId: string;
  _targets: TimeSliderTarget[];
  _histogramSource: TimeSliderHistogramSource;
  _defaultCollapsed: boolean;
  _map: maplibregl.Map | undefined;
  _outerContainer!: HTMLDivElement;
  _range!: HTMLElement;
  _startSlider!: HTMLInputElement;
  _endSlider!: HTMLInputElement;
  _startInput!: HTMLInputElement;
  _endInput!: HTMLInputElement;
  _resetButton!: HTMLButtonElement;
  _scale!: HTMLElement;
  _ticks: { el: HTMLElement; year: number }[] = [];
  // Year bubbles under the handles, and the scale's end labels
  _startBubble!: HTMLElement;
  _endBubble!: HTMLElement;
  _minLabel!: HTMLElement;
  _maxLabel!: HTMLElement;
  // The selected range, shown in the header while collapsed with a filter set
  _pill!: HTMLElement;
  _body!: HTMLElement;
  _histogram!: HTMLElement;
  _histogramBase!: SVGPathElement;
  _histogramSelected!: SVGPathElement;
  _histogramClip!: SVGRectElement;
  // Set when the view or the loaded tiles change. The histogram is recounted
  // once things settle, not on every intermediate frame: when the map goes
  // idle, or a moment after the last move or tile load, whichever comes
  // first. The timer matters because idle waits for every tile; one request
  // that never finishes (an extension holding it, say) would otherwise
  // freeze the histogram.
  _histogramStale = true;
  _refreshTimer: number | undefined;
  _markStale = () => {
    this._histogramStale = true;
    window.clearTimeout(this._refreshTimer);
    this._refreshTimer = window.setTimeout(() => this.refreshHistogram(), 250);
  };
  _onSourceData = (e: maplibregl.MapSourceDataEvent) => {
    if (e.sourceId === this._histogramSource.sourceId) this._markStale();
  };
  _onIdle = () => this.refreshHistogram();
  // The marks are snapped to device pixels, so follow the card if the map
  // resizes and carries it to a new subpixel position
  _onResize = () => this.layoutScale();
  // Publishes the space the card takes up from the map's bottom edge (its
  // height plus margins) as --time-slider-height on the map container, so the
  // bottom-left corner can clear the card when it spans the map's width
  _resizeObserver: ResizeObserver | undefined;
  // The card's width animation between collapsed and expanded, if running
  _widthAnimation: Animation | undefined;
  // Throttles range changes into map filter updates (see scheduleFilter)
  _filterTimer: number | undefined;
  _lastFilterAt = -Infinity;

  constructor(
    mapId: string,
    targets: TimeSliderTarget[] = DEFAULT_TIME_SLIDER_TARGETS,
    histogramSource: TimeSliderHistogramSource = DEFAULT_HISTOGRAM_SOURCE,
    { collapsed = true }: TimeSliderOptions = {},
  ) {
    this._minYear = window.TIME_SLIDER_MIN_YEAR ?? DEFAULT_MIN_YEAR;
    this._maxYear = new Date().getFullYear();
    this._mapId = mapId;
    this._targets = targets;
    this._histogramSource = histogramSource;
    this._defaultCollapsed = collapsed;
  }

  onAdd(map: maplibregl.Map) {
    this._map = map;
    this._outerContainer = document.createElement("div");
    this._outerContainer.className = "maplibregl-ctrl time-slider-ctrl";

    const collapsed = this._defaultCollapsed;
    this._outerContainer.classList.toggle("time-slider-collapsed", collapsed);
    const bodyId = `time-slider-body-${this._mapId}`;
    const clipId = `time-slider-clip-${this._mapId}`;
    const span = this._maxYear - this._minYear;
    const min = this._minYear;
    const max = this._maxYear;

    this._outerContainer.innerHTML = `
      <div class="card time-slider-card">
        <button type="button"
                class="card-header time-slider-header ${collapsed ? "collapsed" : ""}"
                data-bs-toggle="collapse"
                data-bs-target="#${bodyId}"
                aria-expanded="${collapsed ? "false" : "true"}"
                aria-controls="${bodyId}">
          <i class="fas fa-calendar-days text-primary-emphasis" aria-hidden="true"></i>
          <span class="fw-semibold">Filter by date range</span>
          <span class="badge rounded-pill text-bg-primary time-slider-pill" data-role="pill" hidden></span>
          <i class="fas fa-chevron-up time-slider-chevron ms-auto" aria-hidden="true"></i>
        </button>
        <div class="collapse ${collapsed ? "" : "show"}" id="${bodyId}">
          <div class="card-body p-2">
            <div class="time-slider-histogram" data-role="histogram" aria-hidden="true">
              <svg viewBox="0 0 ${span} 1" preserveAspectRatio="none">
                <clipPath id="${clipId}">
                  <rect data-role="histogram-clip" x="-0.5" y="0" width="${span + 1}" height="1"></rect>
                </clipPath>
                <path class="time-slider-histogram-base" data-role="histogram-base"></path>
                <path class="time-slider-histogram-selected" data-role="histogram-selected"
                      clip-path="url(#${clipId})"></path>
              </svg>
              <span class="time-slider-histogram-empty">No dated images in view</span>
            </div>
            <div class="time-slider-range" data-role="range">
              <div class="time-slider-track"></div>
              <div class="time-slider-fill"></div>
              <input type="range" class="form-range" data-role="start-slider"
                     min="${min}" max="${max}" value="${min}" aria-label="Start year">
              <input type="range" class="form-range" data-role="end-slider"
                     min="${min}" max="${max}" value="${max}" aria-label="End year">
              <div class="tooltip show time-slider-bubble" data-role="start-bubble" aria-hidden="true">
                <svg class="time-slider-bubble-shape" preserveAspectRatio="none"><path></path></svg><div class="tooltip-inner"></div>
              </div>
              <div class="tooltip show time-slider-bubble" data-role="end-bubble" aria-hidden="true">
                <svg class="time-slider-bubble-shape" preserveAspectRatio="none"><path></path></svg><div class="tooltip-inner"></div>
              </div>
            </div>
            <div class="time-slider-scale" data-role="scale" aria-hidden="true"></div>
            <div class="time-slider-labels" aria-hidden="true">
              <span class="time-slider-bound" data-role="min-label">${min}</span>
              <span class="time-slider-bound" data-role="max-label">${max}</span>
            </div>
            <div class="input-group input-group-sm mt-1">
              <span class="input-group-text">From</span>
              <input type="number" class="form-control time-slider-year" data-role="start-input"
                     min="${min}" max="${max}" value="${min}" inputmode="numeric" aria-label="Start year">
              <span class="input-group-text">to</span>
              <input type="number" class="form-control time-slider-year" data-role="end-input"
                     min="${min}" max="${max}" value="${max}" inputmode="numeric" aria-label="End year">
              <button type="button" class="btn btn-outline-primary" data-role="reset"
                      title="Show all dates" aria-label="Show all dates">
                <i class="fas fa-undo" aria-hidden="true"></i>
              </button>
            </div>
          </div>
        </div>
      </div>
    `;

    // The elements always exist: the markup was assigned via innerHTML above.
    const q = <T extends Element>(role: string) =>
      this._outerContainer.querySelector<T>(`[data-role="${role}"]`)!;
    this._range = q("range");
    this._startSlider = q("start-slider");
    this._endSlider = q("end-slider");
    this._startInput = q("start-input");
    this._endInput = q("end-input");
    this._resetButton = q("reset");
    this._startBubble = q("start-bubble");
    this._endBubble = q("end-bubble");
    this._minLabel = q("min-label");
    this._maxLabel = q("max-label");
    this._pill = q("pill");
    this._scale = q("scale");
    this.buildScale();
    this._body = this._outerContainer.querySelector<HTMLElement>(".collapse")!;
    this._histogram = q("histogram");
    this._histogramBase = q("histogram-base");
    this._histogramSelected = q("histogram-selected");
    this._histogramClip = q("histogram-clip");

    this._resizeObserver = new ResizeObserver(() => {
      const container = map.getContainer();
      // Hidden (display: none) measures as zero height; it then takes no room
      const height = this._outerContainer.offsetHeight
        ? container.getBoundingClientRect().bottom -
          this._outerContainer.getBoundingClientRect().top
        : 0;
      container.style.setProperty("--time-slider-height", `${height}px`);
      // The marks are placed in pixels, so follow the card's width
      this.layoutScale();
    });
    this._resizeObserver.observe(this._outerContainer);

    map.on("moveend", this._markStale);
    map.on("sourcedata", this._onSourceData);
    map.on("idle", this._onIdle);
    map.on("resize", this._onResize);

    this.setupEventListeners();
    this.updateView();

    return this._outerContainer;
  }

  setupEventListeners() {
    this._startSlider.addEventListener("input", () => {
      this.setRange(Number(this._startSlider.value), null, "start");
    });
    this._endSlider.addEventListener("input", () => {
      this.setRange(null, Number(this._endSlider.value), "end");
    });

    // Typed years apply on commit (Enter/blur), not per keystroke, so
    // half-typed values like "18" don't snap the range around.
    this._startInput.addEventListener("change", () => {
      this.setRange(this.parseYear(this._startInput, this._minYear), null, "start");
    });
    this._endInput.addEventListener("change", () => {
      this.setRange(null, this.parseYear(this._endInput, this._maxYear), "end");
    });

    const handles = [
      [this._startSlider, "start"],
      [this._endSlider, "end"],
    ] as const;
    for (const [slider, handle] of handles) {
      this.setupHandleDrag(slider, handle);
    }

    this._resetButton.addEventListener("click", () => {
      this.setRange(this._minYear, this._maxYear, "start");
    });

    // Collapsed, the card shrinks to its header. Expanding, it widens first
    // and then opens its body (holding Bootstrap's show until the width
    // settles); collapsing, the body shuts and then the card narrows. Either
    // way the body never reflows mid-animation.
    this._body.addEventListener("show.bs.collapse", (event) => {
      if (this._outerContainer.classList.contains("time-slider-collapsed")) {
        event.preventDefault();
        this.animateWidth(
          () => this._outerContainer.classList.remove("time-slider-collapsed"),
          () =>
            window.bootstrap.Collapse.getOrCreateInstance(this._body, {
              toggle: false,
            }).show(),
        );
      }
    });
    this._body.addEventListener("hidden.bs.collapse", () => {
      this.animateWidth(() =>
        this._outerContainer.classList.add("time-slider-collapsed"),
      );
    });
    // The histogram isn't recounted while collapsed; catch up on expanding
    this._body.addEventListener("shown.bs.collapse", () => this.refreshHistogram());
  }

  /**
   * Apply a class change that resizes the card, animating its width from the
   * old size to the new one (both measured, since CSS can't transition to or
   * from `width: auto`), then call `done`. A change made mid-animation starts
   * from wherever the card had got to.
   */
  animateWidth(change: () => void, done?: () => void): void {
    const el = this._outerContainer;
    // Measured before cancelling, which would snap back to the old width
    const from = el.getBoundingClientRect().width;
    this._widthAnimation?.cancel();
    this._widthAnimation = undefined;
    el.classList.remove("time-slider-resizing");
    change();
    const to = el.getBoundingClientRect().width;
    if (
      from === to ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      done?.();
      return;
    }

    // Anchored to the right while its width is pinned (on phones, the
    // expanded card otherwise fills the corner from the left)
    el.classList.add("time-slider-resizing");
    const animation = el.animate(
      [{ width: `${from}px` }, { width: `${to}px` }],
      { duration: 200, easing: "ease-in-out" },
    );
    this._widthAnimation = animation;
    animation.onfinish = () => {
      el.classList.remove("time-slider-resizing");
      this._widthAnimation = undefined;
      done?.();
    };
  }

  // The thumb's width (--thumb, 1rem) in pixels. Year positions run between
  // the thumb centers, half a thumb in from either end of the track.
  thumbPx(): number {
    return parseFloat(getComputedStyle(document.documentElement).fontSize);
  }

  // How far along the track a year sits, 0 (first year) to 1 (last)
  yearFraction(year: number): number {
    const span = this._maxYear - this._minYear;
    return span ? (year - this._minYear) / span : 0;
  }

  parseYear(input: HTMLInputElement, fallback: number): number {
    const year = parseInt(input.value, 10);
    return Number.isNaN(year) ? fallback : year;
  }

  /**
   * Drag a handle with pointer events instead of the browser's native range
   * dragging. Mobile browsers revert a native drag to its starting value when
   * the touch is cancelled (e.g. the finger runs off the screen edge); here a
   * cancelled drag simply stops, keeping the last year it reached. The native
   * input still handles the keyboard and assistive technology.
   */
  setupHandleDrag(slider: HTMLInputElement, handle: "start" | "end") {
    // Pointer x minus the thumb center at grab time, so the thumb doesn't
    // jump to center itself under the finger
    let grabOffset = 0;
    let dragging = false;

    // The span the thumb centers move along, in viewport pixels
    const geometry = () => {
      const rect = this._range.getBoundingClientRect();
      const thumb = this.thumbPx();
      return { left: rect.left + thumb / 2, width: rect.width - thumb };
    };
    const yearAt = (clientX: number) => {
      const { left, width } = geometry();
      const fraction = width > 0 ? (clientX - grabOffset - left) / width : 0;
      const span = this._maxYear - this._minYear;
      return this._minYear + Math.round(fraction * span);
    };
    const moveTo = (clientX: number) => {
      const year = yearAt(clientX);
      if (handle === "start") this.setRange(year, null, "start");
      else this.setRange(null, year, "end");
    };

    // Suppress the native drag. preventDefault on pointerdown alone doesn't
    // stop it everywhere; mousedown/touchstart are what the browsers honor.
    slider.addEventListener("mousedown", (e) => e.preventDefault());
    slider.addEventListener("touchstart", (e) => e.preventDefault(), {
      passive: false,
    });

    slider.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      e.preventDefault();
      const { left, width } = geometry();
      const fraction = this.yearFraction(Number(slider.value));
      grabOffset = e.clientX - (left + fraction * width);
      dragging = true;
      slider.setPointerCapture(e.pointerId);
      // The suppressed mousedown would have focused the handle. Like that
      // native focus, this one shows no focus ring.
      slider.focus({ preventScroll: true, focusVisible: false });
    });

    slider.addEventListener("pointermove", (e) => {
      if (dragging) moveTo(e.clientX);
    });

    // Up, cancel, or a capture lost for any other reason all end the drag
    // where it is.
    slider.addEventListener("lostpointercapture", () => {
      dragging = false;
    });
  }

  /**
   * Place the scale's ticks and the year bubbles in pixels. Each bubble sits
   * under its handle with its arrow on the year's tick; near either end it
   * slides inward to stay within the track's width, lining up with the track
   * and the From/To inputs; when the two would touch they merge into one
   * bubble giving the range. An end label hides under any bubble.
   *
   * Everything is snapped to device pixels: each 1px tick fills a whole
   * column, and each arrow tip is aimed at its column's center. Left to the
   * browser, a tick centered on a pixel boundary rounds one way or the other
   * (differently between browsers), off-center from the tip.
   *
   * Skipped while the card isn't laid out (the resize observer catches up
   * when it is).
   */
  layoutScale() {
    const rangeBox = this._range.getBoundingClientRect();
    const width = rangeBox.width;
    if (!width) return;
    const thumb = this.thumbPx();
    const gap = thumb / 4;
    const dpr = window.devicePixelRatio || 1;
    const snap = (v: number) => Math.round(v * dpr) / dpr;
    // A year's position relative to the range: the center of the pixel
    // column its 1px tick fills
    const x = (year: number) => {
      const exact =
        rangeBox.left + thumb / 2 + (width - thumb) * this.yearFraction(year);
      return snap(exact - 0.5) + 0.5 - rangeBox.left;
    };

    const scaleLeft = this._scale.getBoundingClientRect().left;
    for (const { el, year } of this._ticks) {
      el.style.left = `${rangeBox.left + x(year) - 0.5 - scaleLeft}px`;
    }

    const place = (
      bubble: HTMLElement,
      text: string,
      center: number,
    ): [number, number] => {
      const inner = bubble.querySelector<HTMLElement>(".tooltip-inner")!;
      const shape = bubble.querySelector<SVGSVGElement>(".time-slider-bubble-shape")!;
      // The SVG's edges land between pixels, blurring into a light fringe,
      // unless its size and position are whole device pixels. Round the
      // width up to one, and put the bubble on the grid (otherwise the
      // browser rounds it there as it paints, shifting the arrow off the
      // tick).
      bubble.style.width = "";
      inner.textContent = text;
      const w = Math.ceil(inner.getBoundingClientRect().width * dpr) / dpr;
      const h = inner.getBoundingClientRect().height;
      bubble.style.width = `${w}px`;
      const clamped = Math.min(Math.max(center - w / 2, 0), width - w);
      const left = snap(rangeBox.left + clamped) - rangeBox.left;
      bubble.style.left = `${left}px`;

      // Draw the body and arrow as one outline for this tip position (see
      // time_slider_bubble.ts). The arrow's height comes from the stylesheet,
      // which starts the SVG's box at the tip; the box is then extended to a
      // whole number of pixels above the body (a fractional offset smears
      // its edges the same way), and the viewBox matches it 1:1. Bootstrap's
      // arrow is twice as wide as it is tall.
      shape.style.top = "";
      shape.style.height = "";
      const arrowHeight =
        inner.getBoundingClientRect().top - shape.getBoundingClientRect().top;
      const above = Math.ceil(arrowHeight * dpr) / dpr;
      shape.style.top = `${-above}px`;
      shape.style.height = `${h + above}px`;
      shape.setAttribute("viewBox", `0 ${-above} ${w} ${h + above}`);
      shape.firstElementChild!.setAttribute(
        "d",
        bubblePath(center - left, {
          width: w,
          height: h,
          radius: parseFloat(getComputedStyle(inner).borderTopLeftRadius),
          arrowHeight,
          arrowHalfWidth: arrowHeight,
        }),
      );
      return [left, left + w];
    };

    const start = this.startYear;
    const end = this.endYear;
    const startX = x(start);
    const endX = x(end);
    this._endBubble.hidden = false;
    const startBox = place(this._startBubble, String(start), startX);
    const endBox = place(this._endBubble, String(end), endX);
    let boxes = [startBox, endBox];
    if (startBox[1] + gap > endBox[0]) {
      this._endBubble.hidden = true;
      const text = start === end ? String(start) : `${start}–${end}`;
      boxes = [place(this._startBubble, text, (startX + endX) / 2)];
    }

    // The end labels sit flush with the track's ends
    const ends: [HTMLElement, number, number][] = [
      [this._minLabel, 0, this._minLabel.offsetWidth],
      [this._maxLabel, width - this._maxLabel.offsetWidth, width],
    ];
    for (const [label, from, to] of ends) {
      const covered = boxes.some(([l, r]) => from < r + gap && to > l - gap);
      label.style.visibility = covered ? "hidden" : "";
    }
  }

  // Recount and redraw the histogram if the view or data changed since the
  // last count. Skipped while collapsed or hidden, since nobody can see it.
  refreshHistogram() {
    const map = this._map;
    if (!map || !this._histogramStale) return;
    if (!this._body.classList.contains("show")) return;
    if (this._outerContainer.style.display === "none") return;
    this._histogramStale = false;

    const bins = computeYearHistogram(
      map,
      this._histogramSource,
      this._minYear,
      this._maxYear,
    );
    const path = histogramPath(bins);
    this._histogramBase.setAttribute("d", path);
    this._histogramSelected.setAttribute("d", path);
    this._histogram.classList.toggle("empty", path === "");
  }

  get startYear(): number {
    return Number(this._startSlider.value);
  }

  get endYear(): number {
    return Number(this._endSlider.value);
  }

  get isFullRange(): boolean {
    return this.startYear === this._minYear && this.endYear === this._maxYear;
  }

  // Set either or both ends (null keeps the current value). `moved` names the
  // end the user is dragging, which wins when the two cross.
  setRange(start: number | null, end: number | null, moved: "start" | "end") {
    const clamp = (y: number) =>
      Math.min(this._maxYear, Math.max(this._minYear, y));
    let s = clamp(start ?? this.startYear);
    let e = clamp(end ?? this.endYear);
    if (s > e) {
      if (moved === "start") e = s;
      else s = e;
    }
    this._startSlider.value = String(s);
    this._endSlider.value = String(e);
    this.updateView();
    this.scheduleFilter();
  }

  /**
   * Apply the filter now if it hasn't been applied within the last
   * FILTER_INTERVAL_MS, otherwise once that interval is up. The control
   * itself updates on every change; only the map's refiltering is spaced
   * out. A pending update reads the range when it fires, so the final
   * position always lands.
   */
  scheduleFilter() {
    if (this._filterTimer !== undefined) return;
    const wait = this._lastFilterAt + FILTER_INTERVAL_MS - performance.now();
    if (wait <= 0) {
      this.applyFilter();
    } else {
      this._filterTimer = window.setTimeout(() => this.applyFilter(), wait);
    }
  }

  // Graduations every 10 years (every 50 when that would crowd the track),
  // with longer marks on the half-centuries (centuries at the coarser step)
  // and longest at the first and last years.
  buildScale() {
    const span = this._maxYear - this._minYear;
    if (!span) return;
    const step = span / 10 > 40 ? 50 : 10;
    const major = step * 5;
    const addTick = (year: number, kind: string) => {
      const el = document.createElement("span");
      el.className = `time-slider-tick ${kind}`.trim();
      el.style.setProperty("--pos", String(this.yearFraction(year)));
      this._scale.appendChild(el);
      this._ticks.push({ el, year });
    };

    addTick(this._minYear, "end");
    const first = Math.ceil(this._minYear / step) * step;
    for (let year = first; year <= this._maxYear; year += step) {
      // Leave the end ticks room: drop graduations within half a step of
      // them (this also skips a min or max that falls on a decade).
      if (year - this._minYear < step / 2 || this._maxYear - year < step / 2) {
        continue;
      }
      addTick(year, year % major === 0 ? "major" : "");
    }
    addTick(this._maxYear, "end");
  }

  updateView() {
    const start = this.startYear;
    const end = this.endYear;
    const full = this.isFullRange;

    this._startInput.value = String(start);
    this._endInput.value = String(end);

    this._resetButton.disabled = full;

    // An open end reads as "and earlier"/"and later", matching the filter
    this._pill.hidden = full;
    if (start === this._minYear) this._pill.textContent = `≤ ${end}`;
    else if (end === this._maxYear) this._pill.textContent = `≥ ${start}`;
    else if (start === end) this._pill.textContent = String(start);
    else this._pill.textContent = `${start}–${end}`;

    this._range.style.setProperty("--start", String(this.yearFraction(start)));
    this._range.style.setProperty("--end", String(this.yearFraction(end)));

    for (const { el, year } of this._ticks) {
      el.classList.toggle("in-range", year >= start && year <= end);
    }

    this.layoutScale();

    // The histogram's primary overlay shows only the selected years' bars
    this._histogramClip.setAttribute("x", String(start - this._minYear - 0.5));
    this._histogramClip.setAttribute("width", String(end - start + 1));

    // When the thumbs meet, raise whichever can still move so they can't
    // trap each other at either end of the track.
    const startOnTop = start === end && end === this._maxYear;
    this._startSlider.style.zIndex = startOnTop ? "3" : "2";
    this._endSlider.style.zIndex = startOnTop ? "2" : "3";
  }

  // Also called from outside after style swaps recreate the target layers
  // with their base filters, which would otherwise silently drop the range.
  applyFilter() {
    // Applying now covers any pending update
    window.clearTimeout(this._filterTimer);
    this._filterTimer = undefined;
    this._lastFilterAt = performance.now();

    const map = this._map;
    if (!map) return;

    const startYear = this.startYear;
    const endYear = this.endYear;

    // The ends are open: a handle at either end of the track drops that
    // bound, so the first year means "and earlier" and the last "and later".
    // At the full range no date clause remains, so undated images stay
    // visible until the range is actually narrowed.
    const dateRange: maplibregl.ExpressionSpecification[] = [];
    if (endYear < this._maxYear) {
      dateRange.push(["<=", ["get", "fuzzy_start_decdate"], endYear]);
    }
    if (startYear > this._minYear) {
      dateRange.push([">=", ["get", "fuzzy_end_decdate"], startYear]);
    }

    for (const target of this._targets) {
      if (!map.getLayer(target.layerId)) continue;

      const clauses: maplibregl.ExpressionSpecification[] = [];
      if (target.requiresDirection) clauses.push(["has", "direction"]);
      if (target.extraFilter) clauses.push(target.extraFilter);
      clauses.push(...dateRange);

      map.setFilter(target.layerId, clauses.length ? ["all", ...clauses] : null);
    }
  }

  // Count a different source, e.g. when the display style switches to layers
  // reading another one.
  setHistogramSource(source: TimeSliderHistogramSource) {
    if (source.sourceId === this._histogramSource.sourceId) return;
    this._histogramSource = source;
    this._markStale();
  }

  // Show or hide the whole control (e.g. while the layers it filters are
  // hidden). The filter state is preserved either way.
  setVisible(visible: boolean) {
    if (this._outerContainer) {
      this._outerContainer.style.display = visible ? "" : "none";
      if (visible) this.refreshHistogram();
    }
  }

  onRemove() {
    this._resizeObserver?.disconnect();
    this._map?.getContainer().style.removeProperty("--time-slider-height");
    this._map?.off("moveend", this._markStale);
    window.clearTimeout(this._refreshTimer);
    window.clearTimeout(this._filterTimer);
    this._filterTimer = undefined;
    this._map?.off("sourcedata", this._onSourceData);
    this._map?.off("idle", this._onIdle);
    this._map?.off("resize", this._onResize);
    if (this._outerContainer && this._outerContainer.parentNode) {
      this._outerContainer.parentNode.removeChild(this._outerContainer);
    }
    this._map = undefined;
  }
}
