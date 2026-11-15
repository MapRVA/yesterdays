// Pin placement, direction joystick, and the bearing line drawn from the pin.
import type { GeoJSONSource } from "maplibre-gl";
import { updateConfidenceValidation } from "./form";
import { emptyFeatureCollection, pinFeatureCollection } from "./geojson";
import type { GeoreferenceContext } from "./types";

/**
 * Project a point along a bearing for a given distance.
 * Returns [lng, lat] for the destination point.
 */
function destinationPoint(
  lngLat: [number, number],
  bearingDeg: number,
  distanceMeters: number,
): [number, number] {
  const R = 6371000; // Earth radius in meters
  const toRad = (d: number): number => (d * Math.PI) / 180;
  const toDeg = (r: number): number => (r * 180) / Math.PI;
  const lat1 = toRad(lngLat[1]);
  const lng1 = toRad(lngLat[0]);
  const bearing = toRad(bearingDeg);
  const angularDist = distanceMeters / R;

  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDist) +
      Math.cos(lat1) * Math.sin(angularDist) * Math.cos(bearing),
  );
  const lng2 =
    lng1 +
    Math.atan2(
      Math.sin(bearing) * Math.sin(angularDist) * Math.cos(lat1),
      Math.cos(angularDist) - Math.sin(lat1) * Math.sin(lat2),
    );

  return [toDeg(lng2), toDeg(lat2)];
}

/**
 * Initial compass bearing (degrees clockwise from north) of the great circle
 * from one [lng, lat] point to another.
 */
function initialBearing(
  from: [number, number],
  to: [number, number],
): number {
  const toRad = (d: number): number => (d * Math.PI) / 180;
  const lat1 = toRad(from[1]);
  const lat2 = toRad(to[1]);
  const dLng = toRad(to[0] - from[0]);
  const y = Math.sin(dLng) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
  return (Math.atan2(y, x) * 180) / Math.PI;
}

function normalizeDegrees(deg: number): number {
  const d = deg % 360;
  return d < 0 ? d + 360 : d;
}

// The pin's [lng, lat] from the coordinate inputs, or null if it isn't placed.
function pinLngLat(ctx: GeoreferenceContext): [number, number] | null {
  if (!ctx.state.pinPlaced) return null;
  const lat = parseFloat(ctx.els.latitudeInput.value);
  const lng = parseFloat(ctx.els.longitudeInput.value);
  return isNaN(lat) || isNaN(lng) ? null : [lng, lat];
}

// Screen distance, in pixels, used to sample the direction around the pin.
// Short enough to stay well below the horizon on a steeply tilted map.
const SCREEN_SAMPLE_PX = 20;

/**
 * On-screen angle (degrees clockwise from straight up) at which a compass
 * direction from the pin is drawn. Projecting through the map, instead of
 * just subtracting its bearing, keeps the joystick lined up with the
 * direction cone when the map is tilted as well as rotated.
 */
function directionToScreenAngle(
  ctx: GeoreferenceContext,
  direction: number,
): number {
  const { map } = ctx;
  const pin = pinLngLat(ctx);
  if (!pin) return direction - map.getBearing();

  const metersPerPixel =
    (40075016.686 * Math.cos((pin[1] * Math.PI) / 180)) /
    (512 * 2 ** map.getZoom());
  const from = map.project(pin);
  const to = map.project(
    destinationPoint(pin, direction, metersPerPixel * SCREEN_SAMPLE_PX),
  );
  return (Math.atan2(to.x - from.x, from.y - to.y) * 180) / Math.PI;
}

// Inverse of directionToScreenAngle().
function screenAngleToDirection(
  ctx: GeoreferenceContext,
  screenAngle: number,
): number {
  const { map } = ctx;
  const pin = pinLngLat(ctx);
  if (!pin) return normalizeDegrees(screenAngle + map.getBearing());

  const radians = (screenAngle * Math.PI) / 180;
  const from = map.project(pin);
  const to = map.unproject([
    from.x + Math.sin(radians) * SCREEN_SAMPLE_PX,
    from.y - Math.cos(radians) * SCREEN_SAMPLE_PX,
  ]);
  return normalizeDegrees(initialBearing(pin, [to.lng, to.lat]));
}

export function updateCoordinates(
  ctx: GeoreferenceContext,
  lng: number,
  lat: number,
): void {
  ctx.els.latitudeInput.value = lat.toFixed(6);
  ctx.els.longitudeInput.value = lng.toFixed(6);
  ctx.els.submitButton.disabled = false;
}

export function updateBearingLine(ctx: GeoreferenceContext): void {
  const { map, els, state } = ctx;
  const source = map.getSource<GeoJSONSource>("bearing-line");
  if (!source) return;

  const lat = parseFloat(els.latitudeInput.value);
  const lng = parseFloat(els.longitudeInput.value);
  const direction = state.currentDirection;

  const bearingLineVisible =
    state.bearingLineEnabled &&
    state.pinPlaced &&
    direction !== null &&
    !isNaN(lat) &&
    !isNaN(lng);

  // Sync image centerline with map bearing line visibility.
  // Uses `visibility` rather than `display` because OSD's overlay renderer
  // resets `display` to "block" on every repaint.
  if (state.osdCenterlineOverlay) {
    state.osdCenterlineOverlay.style.visibility = bearingLineVisible
      ? "visible"
      : "hidden";
  }
  const imageCenterline =
    document.querySelector<HTMLElement>(".image-centerline");
  if (imageCenterline) {
    imageCenterline.style.visibility = bearingLineVisible
      ? "visible"
      : "hidden";
  }

  if (bearingLineVisible) {
    // 5000 km should be plenty...
    const totalDist = 5000000;

    // Interpolate points along the great circle so the line curves correctly
    // when zoomed out on a Mercator projection
    const numSegments = Math.max(2, Math.ceil(64 * (totalDist / 20000000)));
    const stepDist = totalDist / numSegments;
    const origin: [number, number] = [lng, lat];
    const coordinates: [number, number][] = [origin];
    for (let i = 1; i <= numSegments; i++) {
      coordinates.push(destinationPoint(origin, direction, stepDist * i));
    }

    source.setData({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: { type: "LineString", coordinates },
          properties: {},
        },
      ],
    });
  } else {
    source.setData(emptyFeatureCollection());
  }
}

export function updateDirection(
  ctx: GeoreferenceContext,
  direction: number | null,
): void {
  const { map, els, state } = ctx;
  state.currentDirection = direction;

  if (direction !== null) {
    els.directionInput.value = Math.round(direction) + "°";
    els.directionInput.classList.remove("inactive");
  } else {
    els.directionInput.value = "";
    els.directionInput.classList.add("inactive");
    updateJoystickHandle(ctx, 0, 0);
  }

  // Update pin direction
  if (state.pinPlaced) {
    const lat = parseFloat(els.latitudeInput.value);
    const lng = parseFloat(els.longitudeInput.value);
    if (!isNaN(lat) && !isNaN(lng)) {
      map
        .getSource<GeoJSONSource>("pin")
        ?.setData(pinFeatureCollection(lng, lat, direction));
    }
  }

  // Update confidence validation based on new direction
  updateConfidenceValidation(ctx);

  // Update bearing centerline
  updateBearingLine(ctx);
}

function getJoystickMaxRadius(ctx: GeoreferenceContext): number {
  const containerRect = ctx.els.joystickContainer.getBoundingClientRect();
  const handleRect = ctx.els.joystickHandle.getBoundingClientRect();
  const handleMargin = handleRect.width / 2; // Half the handle size as margin
  return containerRect.width / 2 - handleMargin;
}

export function updateJoystickHandle(
  ctx: GeoreferenceContext,
  x: number,
  y: number,
): void {
  const maxRadius = getJoystickMaxRadius(ctx);
  const distance = Math.sqrt(x * x + y * y);

  if (distance > maxRadius) {
    const ratio = maxRadius / distance;
    x *= ratio;
    y *= ratio;
  }

  ctx.state.joystickDistance = Math.min(distance, maxRadius);
  ctx.els.joystickHandle.style.transform =
    "translate(calc(-50% + " + x + "px), calc(-50% + " + y + "px))";
}

// Place the joystick handle at the given bearing, on the container edge
// unless a distance from the center is given. The joystick is oriented like
// the map on screen, so the handle points the same way as the direction cone.
export function setJoystickFromDirection(
  ctx: GeoreferenceContext,
  direction: number,
  radius: number = getJoystickMaxRadius(ctx),
): void {
  const screenAngle = directionToScreenAngle(ctx, direction);
  const radians = ((screenAngle - 90) * Math.PI) / 180;
  updateJoystickHandle(
    ctx,
    Math.cos(radians) * radius,
    Math.sin(radians) * radius,
  );
}

function updatePinLocation(
  ctx: GeoreferenceContext,
  lng: number,
  lat: number,
  direction: number | null,
): void {
  const source = ctx.map.getSource<GeoJSONSource>("pin");
  if (!source) return;

  source.setData(pinFeatureCollection(lng, lat, direction));
  ctx.map.setCenter([lng, lat]);
}

function handleJoystickMove(
  ctx: GeoreferenceContext,
  clientX: number,
  clientY: number,
): void {
  if (!ctx.state.pinPlaced) return;

  const rect = ctx.els.joystickContainer.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const x = clientX - centerX;
  const y = clientY - centerY;

  // The joystick is oriented like the map on screen, so convert the handle's
  // on-screen angle to a compass direction (0° is north) through the map.
  const screenAngle = (Math.atan2(y, x) * 180) / Math.PI + 90;
  const direction = screenAngleToDirection(ctx, screenAngle);

  updateDirection(ctx, direction);
  updateJoystickHandle(ctx, x, y);
}

export function initJoystick(ctx: GeoreferenceContext): void {
  const { els, state } = ctx;

  // Mouse events
  els.joystickContainer.addEventListener("mousedown", (e) => {
    e.preventDefault();
    state.isJoystickDragging = true;
    handleJoystickMove(ctx, e.clientX, e.clientY);
  });

  document.addEventListener("mousemove", (e) => {
    if (state.isJoystickDragging) {
      handleJoystickMove(ctx, e.clientX, e.clientY);
    }
  });

  document.addEventListener("mouseup", () => {
    state.isJoystickDragging = false;
  });

  // Touch events
  els.joystickContainer.addEventListener("touchstart", (e) => {
    e.preventDefault();
    state.isJoystickDragging = true;
    const touch = e.touches[0];
    if (touch) handleJoystickMove(ctx, touch.clientX, touch.clientY);
  });

  document.addEventListener("touchmove", (e) => {
    if (state.isJoystickDragging) {
      e.preventDefault();
      const touch = e.touches[0];
      if (touch) handleJoystickMove(ctx, touch.clientX, touch.clientY);
    }
  });

  document.addEventListener("touchend", () => {
    state.isJoystickDragging = false;
  });

  // Keep the handle pointing the same way as the direction cone as the camera
  // moves, swinging it around the center at its current distance. "move"
  // rather than "rotate": on a tilted map, pitching and panning also change
  // the cone's on-screen angle.
  ctx.map.on("move", () => {
    if (state.currentDirection !== null) {
      setJoystickFromDirection(
        ctx,
        state.currentDirection,
        state.joystickDistance,
      );
    }
  });
}

export function initCoordinateInputs(ctx: GeoreferenceContext): void {
  const { els, state } = ctx;

  els.latitudeInput.addEventListener("change", () => {
    const lat = parseFloat(els.latitudeInput.value);
    const lng = parseFloat(els.longitudeInput.value);
    if (!isNaN(lat) && !isNaN(lng) && lat >= -90 && lat <= 90) {
      updatePinLocation(ctx, lng, lat, state.currentDirection);
      state.pinPlaced = true;
      els.submitButton.disabled = false;
    }
  });

  els.longitudeInput.addEventListener("change", () => {
    const lng = parseFloat(els.longitudeInput.value);
    const lat = parseFloat(els.latitudeInput.value);
    if (!isNaN(lng) && !isNaN(lat) && lng >= -180 && lng <= 180) {
      updatePinLocation(ctx, lng, lat, state.currentDirection);
      state.pinPlaced = true;
      els.submitButton.disabled = false;
    }
  });

  els.directionInput.addEventListener("change", () => {
    if (!state.pinPlaced) return;

    const directionText = els.directionInput.value.replace("°", "");
    let direction = parseFloat(directionText);

    if (!isNaN(direction)) {
      direction = direction % 360;
      if (direction < 0) direction += 360;

      updateDirection(ctx, direction);
      setJoystickFromDirection(ctx, direction);
    }
  });

  document.getElementById("reset-direction")?.addEventListener("click", () => {
    if (!state.pinPlaced) return;
    updateDirection(ctx, null);
  });
}

// The checkbox is the source of truth; the browser restores its state on
// reload. Layer visibility is synced in restoreOverlayState() once the map's
// layers exist.
export function initBearingLineToggle(ctx: GeoreferenceContext): void {
  const { map, state } = ctx;
  const bearingLineToggle = document.getElementById(
    "bearing-line-toggle",
  ) as HTMLInputElement | null;
  if (!bearingLineToggle) return;

  state.bearingLineEnabled = bearingLineToggle.checked;

  bearingLineToggle.addEventListener("change", () => {
    state.bearingLineEnabled = bearingLineToggle.checked;
    const vis = state.bearingLineEnabled ? "visible" : "none";
    for (const layerId of ["bearing-line", "bearing-line-bg"]) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", vis);
      }
    }
    updateBearingLine(ctx);
  });
}
