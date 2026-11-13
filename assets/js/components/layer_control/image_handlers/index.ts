// Runtime handlers for images a MapLibre style asks for but doesn't ship in
// its sprite, such as Americana's route shields. MapLibre fires
// "styleimagemissing" for each such image while laying out a tile; a handler
// that calls map.addImage() during the event gets the image into that tile,
// and an image added later still appears, because MapLibre reloads the tiles
// that depend on it.
//
// A global map layer opts in through MapLayer.image_handler. Each handler is
// its own lazily loaded chunk, created at most once per map and style URL.
import type {
  Map as MapLibreMap,
  MapStyleImageMissingEvent,
} from "maplibre-gl";
import type { ImageHandlerKey } from "../types";

export interface StyleImageHandler {
  /** Add the image for event.id with map.addImage(), or ignore the event. */
  handleMissingImage(event: MapStyleImageMissingEvent): void;
  /** Re-apply style-level state (e.g. global state) that setStyle() discards. */
  onStyleLoad?(): void;
}

export type StyleImageHandlerFactory = (
  map: MapLibreMap,
  styleUrl: string,
) => Promise<StyleImageHandler>;

// Keys mirror maps.models.IMAGE_HANDLER_CHOICES.
const HANDLER_FACTORIES: Record<
  ImageHandlerKey,
  () => Promise<StyleImageHandlerFactory>
> = {
  americana: async () => (await import("./americana")).createAmericanaHandler,
};

/** The parts of a base layer that select its image handler. */
export interface ImageHandlerSource {
  url: string;
  image_handler?: ImageHandlerKey | "";
}

interface ActiveHandler {
  handler: StyleImageHandler | null;
  // Images requested while the handler was still loading, replayed once it is
  pending: Map<string, MapStyleImageMissingEvent>;
}

// True once the style document is parsed, which is when style-level state can
// be set. Unlike map.isStyleLoaded(), this doesn't wait for tiles.
function styleIsParsed(map: MapLibreMap): boolean {
  return Boolean(map.style?._loaded);
}

class StyleImageDispatcher {
  private readonly handlers = new Map<string, Promise<StyleImageHandler>>();
  private active: ActiveHandler | null = null;

  constructor(private readonly map: MapLibreMap) {
    map.on("styleimagemissing", this.onImageMissing);
    map.on("style.load", this.onStyleLoad);
  }

  /**
   * Route missing images to the handler for `source`, or to nothing when it
   * has none. Call this before setStyle() and await it, so the handler is
   * ready by the time the new style starts asking for images.
   */
  async use(source: ImageHandlerSource | null): Promise<void> {
    const key = source?.image_handler;
    if (!source || !key) {
      this.active = null;
      return;
    }

    const active: ActiveHandler = { handler: null, pending: new Map() };
    this.active = active;
    try {
      const handler = await this.load(key, source.url);
      // A later use() call has already replaced this one
      if (this.active !== active) return;
      active.handler = handler;
      if (styleIsParsed(this.map)) handler.onStyleLoad?.();
      for (const [id, event] of active.pending) {
        if (!this.map.hasImage(id)) handler.handleMissingImage(event);
      }
    } catch (error) {
      console.error(`Could not load the "${key}" image handler:`, error);
      if (this.active === active) this.active = null;
    } finally {
      active.pending.clear();
    }
  }

  private load(
    key: ImageHandlerKey,
    styleUrl: string,
  ): Promise<StyleImageHandler> {
    const cacheKey = `${key}\n${styleUrl}`;
    let handler = this.handlers.get(cacheKey);
    if (!handler) {
      handler = HANDLER_FACTORIES[key]().then((factory) =>
        factory(this.map, styleUrl),
      );
      // Let a failed load be retried the next time this layer is chosen
      handler.catch(() => this.handlers.delete(cacheKey));
      this.handlers.set(cacheKey, handler);
    }
    return handler;
  }

  private onImageMissing = (event: MapStyleImageMissingEvent): void => {
    const active = this.active;
    if (!active) return;
    if (active.handler) {
      active.handler.handleMissingImage(event);
    } else {
      active.pending.set(event.id, event);
    }
  };

  private onStyleLoad = (): void => {
    this.active?.handler?.onStyleLoad?.();
  };
}

const dispatchers = new WeakMap<MapLibreMap, StyleImageDispatcher>();

/** The map's image handler dispatcher, created on first use. */
export function styleImageHandlers(map: MapLibreMap): StyleImageDispatcher {
  let dispatcher = dispatchers.get(map);
  if (!dispatcher) {
    dispatcher = new StyleImageDispatcher(map);
    dispatchers.set(map, dispatcher);
  }
  return dispatcher;
}
