// OpenStreetMap Americana (https://github.com/osm-americana/openstreetmap-americana)
// draws its route shields and recoloured POI icons in the browser instead of
// shipping them in its sprite. This ports the runtime half of Americana's
// src/js/map_builder.ts, src/js/shield_format.ts and src/js/poi.ts (all CC0).
import {
  getGlobalStateForLocalization,
  getLocales,
} from "@americana/diplomat";
import {
  ShieldRenderer,
  transposeImageData,
  type RouteParser,
  type ShieldSpecification,
  type StringPredicate,
} from "@americana/maplibre-shield-generator";
import type {
  Map as MapLibreMap,
  MapStyleImageMissingEvent,
} from "maplibre-gl";
import type { StyleImageHandler } from "./index";

// The shield generator's declarations reference the maplibre-gl 2.x it pins,
// so its map, event and image types don't line up with ours. The objects are
// compatible at runtime; these aliases keep the casts at the boundary.
type ShieldMap = Parameters<ShieldRenderer["renderOnMaplibreGL"]>[0];
type ShieldEvent = Parameters<
  ReturnType<ShieldRenderer["getStyleImageMissingHandler"]>
>[0];
type ShieldStyleImage = Parameters<typeof transposeImageData>[1];

// Americana's hosted web fonts for shield text. shields.json names the family
// (options.shieldFont); the generator draws with it on a canvas, so it has to
// be a document font rather than a MapLibre glyph stack.
const SHIELD_FONTS: ReadonlyArray<[family: string, baseUrl: string]> = [
  [
    "Noto Sans Condensed",
    "https://webfont.americanamap.org/noto/NotoSans-CondensedMedium-PropNums",
  ],
  [
    "Noto Sans Armenian Condensed",
    "https://webfont.americanamap.org/noto/NotoSansArmenian-CondensedMedium-PropNums",
  ],
];

let shieldFontsRegistered = false;

function registerShieldFonts(): void {
  if (shieldFontsRegistered) return;
  shieldFontsRegistered = true;
  for (const [family, baseUrl] of SHIELD_FONTS) {
    document.fonts.add(
      new FontFace(
        family,
        `url("${baseUrl}.woff2") format("woff2"), url("${baseUrl}.woff") format("woff")`,
        { weight: "500", display: "swap" },
      ),
    );
  }
}

// Shield image IDs are "shield\n{network}\n{ref}\n{name}\n{color}"; see
// parseImageName() in Americana's src/layer/highway_shield.js.
const routeParser: RouteParser = {
  parse(id) {
    const [, network = "", ref, name] = id.split("\n");
    return { network, ref, name };
  },
  format: (network, ref, name) => `shield\n${network}\n${ref}\n${name}\n`,
};

// On recreational route relations, network=* is the network's scope (lcn,
// rwn, …), not the network itself, so there is no shield to draw.
const networkPredicate: StringPredicate = (network) =>
  !/^[lrni][chimpw]n$/.test(network);

/**
 * Draw a "poi\nsprite={name}\ncolor={css color}" image: the named sprite
 * icon, recoloured.
 */
function addRecoloredIcon(
  map: MapLibreMap,
  renderer: ShieldRenderer,
  id: string,
): void {
  const [, spriteField = "", colorField = ""] = id.split("\n");
  const sprite = spriteField.split("=")[1];
  const color = colorField.split("=")[1];
  const source = sprite ? map.getImage(sprite) : undefined;
  if (!source || !color) {
    console.warn("Can't draw Americana icon", JSON.stringify(id));
    return;
  }

  const { width, height } = source.data;
  const ctx = renderer.createGraphics({ width, height });
  transposeImageData(
    ctx,
    source as unknown as ShieldStyleImage,
    0,
    false,
    color,
    "",
  );
  map.addImage(
    id,
    { width, height, data: ctx.getImageData(0, 0, width, height).data },
    { pixelRatio: renderer.pixelRatio() },
  );
}

export async function createAmericanaHandler(
  map: MapLibreMap,
  styleUrl: string,
): Promise<StyleImageHandler> {
  registerShieldFonts();

  // Americana publishes shields.json beside style.json. Fetch it before any
  // shield is drawn: the generator stores a blank for any shield requested
  // before its definitions arrive, and never redraws it.
  const shieldsUrl = new URL("shields.json", styleUrl);
  const response = await fetch(shieldsUrl);
  if (!response.ok) {
    throw new Error(`${shieldsUrl} returned HTTP ${response.status}`);
  }
  const shields = (await response.json()) as ShieldSpecification;

  // renderOnMaplibreGL() adds the renderer's own styleimagemissing listener,
  // which can't be removed later. It's still needed, as it hands the renderer
  // the map it redraws shields on once the shield font loads. Gating the image
  // filter on `forwarding` keeps that listener inert, so shields are only drawn
  // when the dispatcher forwards an event — that is, while this style is
  // active.
  let forwarding = false;
  const renderer = new ShieldRenderer(shields, routeParser)
    .filterImageID((imageId) => forwarding && imageId.startsWith("shield"))
    .filterNetwork(networkPredicate)
    .renderOnMaplibreGL(map as unknown as ShieldMap) as ShieldRenderer;
  const drawShield = renderer.getStyleImageMissingHandler();

  return {
    handleMissingImage(event: MapStyleImageMissingEvent): void {
      const kind = event.id.split("\n", 1)[0];
      if (kind === "shield") {
        forwarding = true;
        try {
          drawShield(event as unknown as ShieldEvent);
        } finally {
          forwarding = false;
        }
      } else if (kind === "poi") {
        try {
          addRecoloredIcon(map, renderer, event.id);
        } catch (error) {
          console.error(`Couldn't draw Americana icon ‘${event.id}’:`, error);
        }
      }
    },

    // Country labels look their names up in global state, falling back to
    // "(USA)"-style codes when it's unset.
    onStyleLoad(): void {
      const state = getGlobalStateForLocalization(getLocales(), {
        uppercaseCountryNames: true,
      });
      for (const [name, value] of Object.entries(state)) {
        map.setGlobalStateProperty(name, value);
      }
    },
  };
}
