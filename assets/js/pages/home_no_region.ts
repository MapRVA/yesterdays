// Entry point for the global (no-region-selected) homepage
// (templates/home_no_region.html). Two maps live on this page:
//
//   - the hero's, which shows the single point one curated photograph was
//     taken from beneath an overlapping thumbnail;
//   - the region picker's, one selectable card per region beside a
//     basemap-only map with one photo pin each. The map itself belongs to
//     ../components/region_map, which the directory at /regions/ shows
//     too; this page supplies the cards and says where selecting a region
//     leads.
//
// Neither is built on DOMContentLoaded. The hero's waits for the hero
// photograph, which is the page's largest paint and shouldn't be racing a
// map for the network; the picker's waits until it's nearly scrolled to.
import "../../styles/pages/home-no-region.css";
// Styles for the activity cards in the "Connect with Other Contributors"
// band (templates/partials/global_home_activity.html).
import "../../styles/pages/activity.css";
import "maplibre-gl/dist/maplibre-gl.css";

import maplibregl from "maplibre-gl";
import {
  onColorSchemeChange,
  protomapsStyleUrl,
} from "../components/map_display/basemap";
import {
  initRegionMap,
  readRegionCards,
  readRegionSummaries,
  whenScrolledNear,
} from "../components/region_map";
import { ensureDirectionSprite } from "../components/map_display/direction_sprite";
import {
  DIRECTION_SPRITE_ID,
  LAYER_IDS,
  SOURCE_IDS,
} from "../components/map_display/layer_ids";
import { primaryColor } from "../components/map_display/colors";
import { writeRegionCookie } from "../components/region_cookie";

// Close enough on the hero's point to show the streets the photographer
// was standing among, without implying the georeference is accurate to
// the doorstep.
const HERO_ZOOM = 15;

// The photograph and the map are the same size: two panels, each taking
// this share of the composition along both axes and anchored to opposite
// corners, so 2f - 1 of the composition is the diagonal overlap between
// them. What f actually comes to is the arrow's to decide (see
// positionHeroFeature): the ceiling only keeps two panels reading as two,
// and the floor keeps them at least corner to corner however little room
// the connector is left.
const HERO_PANEL_MAX_FRACTION = 0.6;
const HERO_PANEL_MIN_FRACTION = 0.5;

// The composition takes the thumbnail's own aspect ratio, so both panels
// carry it and the photograph fills its own exactly. The bounds stop an
// extreme thumbnail from dictating a figure that towers over the copy
// beside it or flattens into a strip; past them the picture is
// letterboxed into its panel rather than cropped, since it is an archival
// photograph. The fallback stands in for a thumbnail that never loaded to
// be measured.
const HERO_VISUAL_MIN_RATIO = 0.9;
const HERO_VISUAL_MAX_RATIO = 1.9;
const HERO_VISUAL_FALLBACK_RATIO = 1.25;

const HERO_ARROW_POINT_GAP_EM = 1.5;
const HERO_ARROWHEAD_LENGTH = 4.5;
const HERO_ARROWHEAD_HALF_WIDTH = 2.25;
// The shortest shaft worth drawing, in viewBox units; the panels give way
// until at least this much clears the frame's edge before the head begins.
const HERO_ARROW_MIN_SHAFT = 10;
// The connector is one circular arc: it leaves the photograph closer to
// the horizontal and arrives at the point closer to the vertical, each
// end deviating from the chord by up to this angle. The bend eases back
// to straight as the shaft shortens — a guard for the panel floor above,
// since a shaft that made it past HERO_ARROW_MIN_SHAFT is already full.
const HERO_ARROW_MAX_BEND = (25 * Math.PI) / 180;
const HERO_ARROW_FULL_BEND_SHAFT = 8;
// The arc begins on the frame's edge — the bottom edge when it departs
// steeper than 45°, the right edge otherwise, so it always leaves the edge
// it crosses most squarely — this far from the corner, clear of its
// rounding. A short straight stub continues back under the frame along
// the departure tangent, so the path's cut end is hidden (the SVG stacks
// beneath the photograph) and the shaft appears to slip out from under
// the picture. The bend never sends the departure back toward the frame,
// so once out the shaft stays out.
const HERO_ARROW_EDGE_OFFSET = 3;
const HERO_ARROW_STUB_LENGTH = 3;

// How long to wait on the hero photograph before building its map anyway.
const HERO_MAP_MAX_WAIT_MS = 3000;

function selectRegion(cookieName: string, slug: string): void {
  // Same cookie the navbar selector writes; the reload lands on this
  // region's homepage, since yesterdays.views.home branches on it.
  writeRegionCookie(cookieName, slug);
  window.location.reload();
}

// The hero's map: one fixed point. Layer and source ids are map_display's
// "current image" ones, which is what this is — the one photograph the
// page is about. Keeping the map non-interactive also keeps the connector
// in the surrounding composition aimed at the correct spot.
function initHeroMap(container: HTMLElement): maplibregl.Map | undefined {
  const lng = Number.parseFloat(container.dataset.lng ?? "");
  const lat = Number.parseFloat(container.dataset.lat ?? "");
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return;

  const rawDirection = container.dataset.direction;
  const direction =
    rawDirection === undefined ? Number.NaN : Number.parseFloat(rawDirection);
  const hasDirection = Number.isFinite(direction);
  const spriteUrl = container.dataset.directionSprite;

  const map = new maplibregl.Map({
    container,
    style: protomapsStyleUrl(),
    center: [lng, lat],
    zoom: HERO_ZOOM,
    interactive: false,
  });

  // style.load, not load: it fires again after each setStyle below, and a
  // style swap discards sources, layers, and registered images, so this
  // hook rebuilds all three.
  map.on("style.load", () => {
    void (async () => {
      if (hasDirection && spriteUrl) {
        await ensureDirectionSprite(map, spriteUrl);
      }
      // A rapid theme change can start a second style.load while the sprite
      // is loading. Whichever run restores the source first owns this style.
      if (map.getSource(SOURCE_IDS.currentImage)) return;

      map.addSource(SOURCE_IDS.currentImage, {
        type: "geojson",
        data: {
          type: "Feature",
          geometry: { type: "Point", coordinates: [lng, lat] },
          properties: {},
        },
      });

      // Match the full image map: direction cone beneath the point marker.
      if (hasDirection && map.hasImage(DIRECTION_SPRITE_ID)) {
        map.addLayer({
          id: LAYER_IDS.currentImageDirection,
          type: "symbol",
          source: SOURCE_IDS.currentImage,
          layout: {
            "icon-image": DIRECTION_SPRITE_ID,
            "icon-overlap": "always",
            "icon-size": 1,
            "icon-rotate": direction,
            "icon-rotation-alignment": "map",
            "icon-pitch-alignment": "map",
          },
          paint: {
            "icon-opacity": 1,
          },
        });
      }

      map.addLayer({
        id: LAYER_IDS.currentImageCircle,
        type: "circle",
        source: SOURCE_IDS.currentImage,
        paint: {
          "circle-radius": 7,
          "circle-color": primaryColor,
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
        },
      });
    })();
  });

  onColorSchemeChange(() => map.setStyle(protomapsStyleUrl()));
  return map;
}

interface Point {
  x: number;
  y: number;
}

// The arc from `start` to the head's base, and the head, given the point it
// must indicate. The shaft is a circular arc whose two end tangents each
// sit `bend` away from its chord: the departure rotated toward the
// horizontal, the arrival the same amount toward the vertical. The arrival
// tangent runs through the target, so the head — which continues it —
// points at the georeference exactly. Capping the bend at the chord's own
// angle divided by (1 + headReach / distance) keeps the departure from
// ever crossing the horizontal.
interface ArrowGeometry {
  departureAngle: number;
  arrivalAngle: number;
  bend: number;
  shaftEnd: Point;
  tip: Point;
}

function solveArrow(
  start: Point,
  target: Point,
  headReach: number,
  pointGap: number,
): ArrowGeometry | null {
  const chordX = target.x - start.x;
  const chordY = target.y - start.y;
  const targetDistance = Math.hypot(chordX, chordY);
  if (!targetDistance) return null;
  const shaftLength = Math.max(0, targetDistance - headReach);

  const chordAngle = Math.atan2(chordY, chordX);
  const bendSign = chordX * chordY >= 0 ? 1 : -1;
  const bend = Math.min(
    HERO_ARROW_MAX_BEND * Math.min(1, shaftLength / HERO_ARROW_FULL_BEND_SHAFT),
    Math.abs(chordAngle) / (1 + headReach / targetDistance),
  );
  // Rotating the arrival by `bend` alone would overshoot, because the
  // shaft's chord ends `headReach` short of the target and so tilts too.
  // This solves for the arrival direction that leaves exactly `bend`
  // between the actual chord and the tangent.
  const arrivalOffset =
    bend - Math.asin((headReach / targetDistance) * Math.sin(bend));
  const arrivalAngle = chordAngle + bendSign * arrivalOffset;
  const departureAngle = arrivalAngle - bendSign * 2 * bend;
  const arrivalUnitX = Math.cos(arrivalAngle);
  const arrivalUnitY = Math.sin(arrivalAngle);
  const tip = {
    x: target.x - arrivalUnitX * pointGap,
    y: target.y - arrivalUnitY * pointGap,
  };
  return {
    departureAngle,
    arrivalAngle,
    bend,
    shaftEnd: {
      x: tip.x - arrivalUnitX * HERO_ARROWHEAD_LENGTH,
      y: tip.y - arrivalUnitY * HERO_ARROWHEAD_LENGTH,
    },
    tip,
  };
}

// Lay the two panels out and draw the connector between them. The SVG's
// viewBox is 100 units across, with its height following the composition
// so x and y stay on one scale in both the diagonal and horizontal layouts.
function positionHeroFeature(): void {
  const visual = document.querySelector<HTMLElement>(".hero-visual");
  const photoFrame = visual?.querySelector<HTMLElement>(".hero-photo-frame");
  const photo = photoFrame?.querySelector<HTMLImageElement>("img");
  const map = visual?.querySelector<HTMLElement>("#hero-map");
  const svg = visual?.querySelector<SVGSVGElement>(".hero-georef-arrow");
  const paths = visual?.querySelectorAll<SVGPathElement>(
    ".hero-georef-arrow-halo, .hero-georef-arrow-line",
  );
  const arrowhead = visual?.querySelector<SVGPathElement>(
    ".hero-georef-arrow-head",
  );
  if (
    !visual ||
    !photoFrame ||
    !photo ||
    !map ||
    !svg ||
    !paths?.length ||
    !arrowhead
  ) {
    return;
  }

  // The one real measurement: the column's width, which the em-based gap
  // below has to be expressed against.
  const visualWidth = visual.getBoundingClientRect().width;
  if (!visualWidth) return;

  const ratio = Math.min(
    HERO_VISUAL_MAX_RATIO,
    Math.max(
      HERO_VISUAL_MIN_RATIO,
      photo.naturalWidth && photo.naturalHeight
        ? photo.naturalWidth / photo.naturalHeight
        : HERO_VISUAL_FALLBACK_RATIO,
    ),
  );
  // CSS owns the layout breakpoint, height, and panel widths. Read its mode
  // so changing the breakpoint cannot leave the arrow in the other layout.
  const visualStyle = getComputedStyle(visual);
  const horizontal =
    visualStyle.getPropertyValue("--hero-layout").trim() === "horizontal";
  visual.style.setProperty("--hero-ratio", `${ratio}`);
  const viewBoxHeight = horizontal
    ? (visual.getBoundingClientRect().height / visualWidth) * 100
    : 100 / ratio;

  // Keep the arrowhead visually separate from the point. Convert ems into
  // viewBox units so the gap follows type scale at every viewport width.
  const fontSize = Number.parseFloat(visualStyle.fontSize);
  const pointGap = ((fontSize * HERO_ARROW_POINT_GAP_EM) / visualWidth) * 100;
  const headReach = pointGap + HERO_ARROWHEAD_LENGTH;

  // Each panel is anchored to the corner opposite the other's, so panels of
  // f leave 1 - 1.5f of the composition between the photograph's
  // lower-right corner and the point at the map's centre, measured along
  // the composition's diagonal. Take the largest f whose run still fits the
  // head, its gap, the shortest shaft worth drawing, and the offset the arc
  // starts back from the corner: the panels are as large as the connector
  // between them can afford, and its curve is never squeezed.
  const diagonal = Math.hypot(100, viewBoxHeight);
  const clearance = headReach + HERO_ARROW_MIN_SHAFT + HERO_ARROW_EDGE_OFFSET;
  const panel = Math.min(
    HERO_PANEL_MAX_FRACTION,
    Math.max(HERO_PANEL_MIN_FRACTION, (1 - clearance / diagonal) / 1.5),
  );

  // The CSS reads both panels off --hero-panel, and the viewBox has to
  // follow the composition so the arrow's units stay square.
  visual.style.setProperty("--hero-panel", `${(panel * 100).toFixed(3)}%`);
  svg.setAttribute("viewBox", `0 0 100 ${viewBoxHeight.toFixed(3)}`);

  const corner = horizontal
    ? {
        x: (photoFrame.getBoundingClientRect().width / visualWidth) * 100,
        y: viewBoxHeight / 2,
      }
    : { x: panel * 100, y: panel * viewBoxHeight };
  const target = horizontal
    ? {
        x: 100 - (map.getBoundingClientRect().width / visualWidth) * 50,
        y: viewBoxHeight / 2,
      }
    : {
        x: (1 - panel / 2) * 100,
        y: (1 - panel / 2) * viewBoxHeight,
      };

  // Which edge the arc leaves through depends on how steeply it departs,
  // which depends (weakly) on where on the edge it starts: solve once from
  // the corner to choose the edge, then for real from the chosen start.
  const provisional = solveArrow(corner, target, headReach, pointGap);
  if (!provisional) return;
  const start = horizontal
    ? corner
    : provisional.departureAngle > Math.PI / 4
      ? { x: corner.x - HERO_ARROW_EDGE_OFFSET, y: corner.y }
      : { x: corner.x, y: corner.y - HERO_ARROW_EDGE_OFFSET };
  const arrow = solveArrow(start, target, headReach, pointGap);
  if (!arrow) return;

  const { departureAngle, arrivalAngle, bend, shaftEnd, tip } = arrow;
  const arrivalUnitX = Math.cos(arrivalAngle);
  const arrivalUnitY = Math.sin(arrivalAngle);
  const stub = {
    x: start.x - Math.cos(departureAngle) * HERO_ARROW_STUB_LENGTH,
    y: start.y - Math.sin(departureAngle) * HERO_ARROW_STUB_LENGTH,
  };

  // A cubic reproduces a circular arc to within a hair when both handles
  // are chord / (3 cos²(bend / 2)) long — which is exactly a third of the
  // chord, three evenly spaced points, when the arc degenerates to a line.
  const shaftChord = Math.hypot(shaftEnd.x - start.x, shaftEnd.y - start.y);
  const handleLength = shaftChord / (3 * Math.cos(bend / 2) ** 2);
  const path = [
    `M ${stub.x.toFixed(2)} ${stub.y.toFixed(2)}`,
    `L ${start.x.toFixed(2)} ${start.y.toFixed(2)}`,
    `C ${(start.x + Math.cos(departureAngle) * handleLength).toFixed(2)} ${(start.y + Math.sin(departureAngle) * handleLength).toFixed(2)},`,
    `${(shaftEnd.x - arrivalUnitX * handleLength).toFixed(2)} ${(shaftEnd.y - arrivalUnitY * handleLength).toFixed(2)},`,
    `${shaftEnd.x.toFixed(2)} ${shaftEnd.y.toFixed(2)}`,
  ].join(" ");
  paths.forEach((connector) => connector.setAttribute("d", path));

  const perpendicularX = -arrivalUnitY;
  const perpendicularY = arrivalUnitX;
  const firstBaseX = shaftEnd.x + perpendicularX * HERO_ARROWHEAD_HALF_WIDTH;
  const firstBaseY = shaftEnd.y + perpendicularY * HERO_ARROWHEAD_HALF_WIDTH;
  const secondBaseX = shaftEnd.x - perpendicularX * HERO_ARROWHEAD_HALF_WIDTH;
  const secondBaseY = shaftEnd.y - perpendicularY * HERO_ARROWHEAD_HALF_WIDTH;
  arrowhead.setAttribute(
    "d",
    [
      `M ${firstBaseX.toFixed(2)} ${firstBaseY.toFixed(2)}`,
      `L ${tip.x.toFixed(2)} ${tip.y.toFixed(2)}`,
      `L ${secondBaseX.toFixed(2)} ${secondBaseY.toFixed(2)} Z`,
    ].join(" "),
  );
}

// Run once the hero photograph has settled, so the map isn't competing
// with the page's largest paint for bandwidth. The timeout is the escape
// hatch: a photo that never loads shouldn't cost the map entirely.
function whenHeroPhotoSettled(callback: () => void): void {
  const photo = document.querySelector<HTMLImageElement>(
    ".hero-photo-frame img",
  );
  if (!photo || photo.complete) {
    callback();
    return;
  }

  let ran = false;
  const run = (): void => {
    if (ran) return;
    ran = true;
    callback();
  };
  photo.addEventListener("load", run, { once: true });
  photo.addEventListener("error", run, { once: true });
  window.setTimeout(run, HERO_MAP_MAX_WAIT_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  const heroMap = document.getElementById("hero-map");
  const heroVisual = document.querySelector<HTMLElement>(".hero-visual");
  if (heroMap && heroVisual) {
    whenHeroPhotoSettled(() => {
      positionHeroFeature();
      const map = initHeroMap(heroMap);
      const refresh = (): void => {
        positionHeroFeature();
        map?.resize();
      };
      new ResizeObserver(refresh).observe(heroVisual);
      // The map may have started after the timeout, before the photograph
      // loaded. Its eventual dimensions must still update the composition.
      heroVisual.querySelector("img")?.addEventListener("load", refresh, {
        once: true,
      });
    });
  }

  const container = document.getElementById("global-home-map");
  if (!container) return;
  const cookieName = container.dataset.cookieName || "region";
  const cards = readRegionCards("#region-cards .region-card");

  // Card clicks are wired eagerly even though the map below them is not:
  // someone can scroll to the picker and click faster than the observer
  // fires, and they still have to land on a region. The click lands on
  // the stretched-link button (it covers the whole card), a real button
  // so keyboard activation comes for free.
  cards.forEach((card, slug) => {
    card
      .querySelector<HTMLButtonElement>(".region-card-link")
      ?.addEventListener("click", () => selectRegion(cookieName, slug));
  });

  whenScrolledNear(container, () =>
    initRegionMap({
      container,
      summaries: readRegionSummaries(),
      cards,
      onSelect: (slug) => selectRegion(cookieName, slug),
    }),
  );
});
