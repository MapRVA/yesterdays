// The outline of a year bubble together with its arrow ("tail"), for a tip
// that may sit close to either end of the bubble. Drawn as one SVG path, so
// the edge is antialiased once: separately drawn body and tail each
// half-cover the pixels where they meet, leaving a faint light seam at 1x.
//
// The bubble keeps its full corner radius everywhere; the tail's outer sides
// adapt instead:
//
// - Normally each side runs down from the tip at the arrow's own slope and
//   meets the bubble's flat top edge, as Bootstrap draws it.
// - When that would land inside a rounded top corner, the side stops where
//   it meets the corner's curve.
// - Closer still, the side would miss the curve entirely; it becomes the line
//   from the tip tangent to the curve, flowing smoothly into the corner.
//
// The stages meet without a jump: at the switch to a tangent, the sloped
// side already just touches the curve.

type Point = [number, number];

export interface BubbleGeometry {
  // The bubble's body, and its corners' radius
  width: number;
  height: number;
  radius: number;
  // Arrow height (tip above the body's top edge) and half its base width
  arrowHeight: number;
  arrowHalfWidth: number;
}

// Where an arrow side joins the body: on the flat top edge, or on the
// rounded corner's curve.
interface Join {
  point: Point;
  onCurve: boolean;
}

/**
 * The bubble's outline as an SVG path in body coordinates: x from the body's
 * left edge, y down from its top edge, so the tip is at (tip, -arrowHeight).
 * Traced clockwise from the tip.
 */
export function bubblePath(tip: number, g: BubbleGeometry): string {
  const { width: w, height: hgt, radius: r } = g;
  const left = sideJoin(tip, g);
  const mirrored = sideJoin(w - tip, g);
  const right: Join = {
    point: [w - mirrored.point[0], mirrored.point[1]],
    onCurve: mirrored.onCurve,
  };
  const p = ([x, y]: Point) => `${x.toFixed(3)} ${y.toFixed(3)}`;
  const arc = (to: Point) => `A ${r} ${r} 0 0 1 ${p(to)}`;

  return [
    `M ${p([tip, -g.arrowHeight])}`,
    `L ${p(right.point)}`,
    right.onCurve ? "" : `L ${p([w - r, 0])}`,
    arc([w, r]),
    `L ${p([w, hgt - r])}`,
    arc([w - r, hgt]),
    `L ${p([r, hgt])}`,
    arc([0, hgt - r]),
    `L ${p([0, r])}`,
    arc(left.onCurve ? left.point : [r, 0]),
    left.onCurve ? "" : `L ${p(left.point)}`,
    "Z",
  ]
    .filter(Boolean)
    .join(" ");
}

// Where the left side of the arrow, for a tip `c` from the left edge, joins
// the body. (The right side is the same, mirrored.)
function sideJoin(c: number, g: BubbleGeometry): Join {
  const { radius: r, arrowHeight: h, arrowHalfWidth: b } = g;
  // Lands on the flat top edge
  if (c - b >= r) return { point: [c - b, 0], onCurve: false };

  // The tip relative to the corner circle's center, (r, r)
  const fx = c - r;
  const fy = -h - r;

  // Where the sloped side first meets the corner circle, if it does
  const len = Math.hypot(b, h);
  const ux = -b / len;
  const uy = h / len;
  const B = 2 * (fx * ux + fy * uy);
  const disc = B * B - 4 * (fx * fx + fy * fy - r * r);
  if (disc >= 0) {
    const t = (-B - Math.sqrt(disc)) / 2;
    return { point: [c + t * ux, -h + t * uy], onCurve: true };
  }

  // Otherwise, the tangent from the tip to the circle on its outer side
  const angle = Math.atan2(fy, fx) - Math.acos(r / Math.hypot(fx, fy));
  return {
    point: [r + r * Math.cos(angle), r + r * Math.sin(angle)],
    onCurve: true,
  };
}
