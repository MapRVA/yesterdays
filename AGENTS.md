This repository is a Django project, powering a community effort to catalogue and georeference thousands of images of my city. Most of these are old photographs, stretching all the way back to the mid-1800s.

Besides placing images on the map (our core goal), this app provides a growing range of features:
- users can login using OpenStreetMap accounts
- favorite and share images
- tag images with their "subjects" (people, buildings, monuments) linked to Wikidata and OpenStreetMap
- search image descriptions, or their content ("semantic" search) using a CLIP model
- transcribe historical city directories with an LLM-assisted OCR pipeline
- ...and much more!

## Apps

The Django project package is `yesterdays/` (settings, root `urls.py`, Celery app). The feature apps are:

- **`images`** — the core app. Holds `Source`, `Collection`, `Image` (with EDTF dates, IIIF tiling, CLIP embeddings, transforms), the curation staging models (`PreCollection`, `PreImage`), point and aerial (`Georeference`, `AerialGeoreference`) georeferences plus their validations, subject tagging (`SubjectMapping`), `Album`, `Comment`/`ImageRating`, `ImageOfTheDay`, and singletons `SiteSettings` and `TileVersion`.
- **`subjects`** — named entities attached to images. `Subject` links to a `WikidataItem` (cached Wikidata JSON) and `OsmElement` (cached OSM geometry). `SubjectAncestor` is a denormalized P31/P279 closure projected out of the Memgraph Wikidata mirror.
- **`regions`** — admin-curated geographic regions backed by Wikidata (`Region` is one-to-one with a `subjects.WikidataItem`; its closure also mirrors the transitive P131 containment chain). `Source`, `Collection`, and `Image` carry an optional `region` FK resolved image → collection → source (`Image.effective_region`). `RegionAncestor` is a denormalized P131 containment closure projected out of the Memgraph mirror. Regions are admin-created only; the public surface is a navbar region selector (choice persisted in a cookie read by `regions.context_processors.current_region`) backed by an autocomplete endpoint at `/regions/api/autocomplete/`. No REST API exposure yet.
- **`activity`** — the activity feed and milestones (`GeoreferenceGroup`, `UserMilestone`, `SitewideMilestone`, `SubjectIntroduction`).
- **`maps`** — curated map layers (`MapLayer`, `LayerCollection`): PMTiles, XYZ, and MapLibre styles shown on the map. Served under `/layers/` (`/maps/` redirects there for backwards compatibility).
- **`osm_auth`** — OpenStreetMap OAuth login (via `osm_login_python`), plus `UserProfile` and `UserPreferences`. Provides the auth backend and user-settings pages.
- **`directories`** — **optional**, enabled by the `DIRECTORIES_ENABLED` setting. An LLM/OCR pipeline (via OpenRouter) for transcribing historical city directories into structured `Entry`/`Address` records linked back to subjects. Only added to `INSTALLED_APPS` and routed when enabled.
- **`api`** — the public REST API (see below). Its only model is `ApplicationConsent`, which remembers a user's OAuth consent per application so they aren't re-prompted on every authorization.

Several features are toggled by settings/env vars, e.g. `DIRECTORIES_ENABLED`, `PROMETHEUS_ENABLED`, `LOCAL_DEV`, `DJANGO_DEBUG`. Use `django.conf.settings` rather than reading env vars directly in app code.

Besides the Django apps, **`services/clip/`** is a standalone microservice (own `pyproject.toml`/`uv.lock`/Containerfile, no Django imports) serving CLIP embeddings over HTTP via OpenVINO — CPU everywhere, Intel Arc GPU in production. Django talks to it through `images/clip_client.py` when `CLIP_SERVICE_URL` is set, and falls back to the legacy in-worker torch path when it isn't.

## Development environment (Docker Compose)

Local development runs as a Docker Compose stack. **`docker compose up`** starts everything:

- **`web`** — Django dev server on **port 8000** (runs `migrate` then `runserver`)
- **`postgres`** — PostGIS + pgvector (image `ghcr.io/maprva/postgis-pgvector-local`)
- **`rabbitmq`** — Celery broker; management UI on **port 15672** (guest/guest)
- **`memgraph`** — property-graph database (Cypher over Bolt) for the Wikidata subject mirror
- **`lab`** — Memgraph Lab web UI on **port 3000**, for inspecting/querying the mirror
- **`worker-urgent`** and **`worker-background`** — Celery workers, one per queue
- **`beat`** — Celery Beat scheduler
- **`vite`** — frontend bundler on **port 5173**

Only `web` (8000), `vite` (5173), the RabbitMQ management UI (15672), Memgraph (Bolt 7687 + log websocket 7444, for host-side clients and Lab), and Memgraph Lab (3000) publish host ports. **Postgres and the broker are internal to the Compose network**, so `localhost` does not reach them from the host. Run Django and Celery commands inside the `web` container:

`docker compose exec web uv run manage.py <command>`

`my.env` (gitignored) now holds **only external secrets** (Cloudflare R2, Protomaps, OpenRouter). Every dev connection setting and feature flag is supplied by the `environment:` block in `compose.yaml`, which points the services at each other by name (`PG_HOST=postgres`, `CELERY_BROKER_URL=amqp://…@rabbitmq…`, `MEMGRAPH_URL=bolt://memgraph:7687`).

The Python venv and `node_modules` live **inside the image**, not in the bind-mounted source, so after changing dependencies (`pyproject.toml`/`uv.lock` or `package.json`) you must `docker compose build`. Python deps are managed with **`uv`**; frontend deps with **`bun`**.

As always, **please ask permission before running Django commands.** Thank you.

## Frontend assets (Vite + bun)

Vite (managed by bun) bundles JavaScript and CSS from `/assets` into the gitignored `/static` directory (with a `manifest.json`). The application makes heavy use of **MapLibre**, and **Alpine.js** is universally available, so follow Alpine.js best practices where we can. The `vite` Compose service runs the dev server automatically with HMR.

New frontend code is **TypeScript** (`strict`); the sitewide migration is in progress, starting with `assets/js/components/map_display/`. `tsconfig.json` at the repo root uses `allowJs`/`checkJs: false`, so existing `.js` files keep working unchecked until they're converted. `bun run typecheck` runs `tsc` — it's enforced by CI (`.github/workflows/typecheck.yml`) and by the container build. Multi-module components live in a directory with an `index.ts` entry.

**Important**: When creating new page-specific JavaScript/TypeScript files in `assets/js/pages/`, you must also add them as entry points in `vite.config.js` under `rollupOptions.input`. Otherwise, the asset will work in development but fail in production with a 500 error. The same applies to renames (including `.js` → `.ts`): the `{% vite_asset %}` path in the template must match the entry's source path exactly, extension included, or production breaks while development keeps working.

Other conventions worth knowing:
- Templates load assets with **`django-vite`**: `{% load django_vite %}` then `{% vite_asset 'assets/js/pages/<name>.js' %}`. `templates/base.html` emits `{% vite_hmr_client %}` and the global `assets/index.js` bundle; pages add their own bundle via `{% block extra_js %}`.
- `base.html` exposes server-side config to JS as `window.*` globals (e.g. `window.DEFAULT_MAP_CENTER`, `window.MAP_LAYERS_DATA`).
- Shared helpers are global: `window.showAlert(type, message, duration)`, `window.getCsrfToken()`, `window.Alpine`, `window.bootstrap`.
- Register Alpine components with `Alpine.data(...)` in page scripts before `Alpine.start()` runs (handled in `index.js` on `DOMContentLoaded`).

## REST API

A public REST API lives in the `api/` app, served at `/api/v2/`. It uses Django REST Framework, django-rest-framework-gis, django-filter, and drf-spectacular (OpenAPI schema). Apart from `ApplicationConsent` (remembered OAuth consent, see `api/models.py`), it exposes data from `images`, `subjects`, `activity`, and friends through serializers, viewsets, and filter classes in `api/serializers.py`, `api/views.py`, and `api/filters.py`.

Much of the API is **publicly readable without authentication** — images, subjects, activity, georeferences, licenses, and the semantic/text search endpoints (`/search/semantic/`, `/search/text/`). Layered on top is a substantial **OAuth2 layer** (`oauth2_provider`, PKCE required): clients register an application (`/apps/`), obtain a token, and make authenticated requests on a user's behalf, scoped by OAuth scopes (`/auth/me/` returns the authenticated user). A few legacy `/api/v1/subjects/...` endpoints also exist.

When modifying models or fields in `images`, `subjects`, or `activity`, check whether the API serializers or filters need a corresponding update. **API documentation lives in `docs/dev/api/`** and should be kept in sync with any endpoint changes.

## Background tasks (Celery)

Celery handles background work (thumbnail and IIIF tile generation, embedding generation, metadata refresh) with **RabbitMQ** as the broker and **django-celery-results** for results. Tasks are routed across two queues, **`urgent`** and **`background`**, each served by its own worker. Celery Beat schedules periodic tasks, including rate-limited refreshes of external data from Wikidata and OpenStreetMap (one item per interval, so request volume is independent of worker count) and reconciliation of the Memgraph subject graph.

## Semantic search (CLIP) and the database

Semantic search uses an OpenAI **CLIP ViT-L/14@336px** model. All encoding happens in the standalone CLIP service (`services/clip/`, reached via `CLIP_SERVICE_URL`); Django posts query text and images to it through `images/clip_client.py`. Embeddings are 768-dimensional, stored on `Image.embedding` and queried with **pgvector** (HNSW index, cosine distance). The `generate_embeddings` management command backfills them by streaming images through the service.

The database is **PostgreSQL with PostGIS, pgvector, and pg_trgm** (trigram text search). Image dates are stored as **EDTF**. A couple of config models (`SiteSettings`, `TileVersion`) are singletons (pk=1).

## External integrations

- **Wikidata** — subject metadata via WDQS; closures mirrored into **Memgraph** (property-graph store).
- **OpenStreetMap** — geometry via the **Postpass** API; login via OAuth (`osm_auth`).
- **Nominatim** — address geocoding.
- **OpenRouter** — the LLM behind the `directories` OCR pipeline.
- **Cloudflare R2** — object storage for image assets and avatars.
- **Protomaps** — base map tiles.

## Documentation site

Project documentation (published at docs.yesterdays.today) is built from `docs/` with **Zensical** (config in `zensical.toml`) into the gitignored `site/` directory, and deployed to GitHub Pages on push to `trunk`. Build with `uv run --only-group docs zensical build --clean --strict`. To preview locally use `uv run zensical serve -a localhost:8080` — Zensical defaults to port 8000, which collides with the Django dev server.

## Testing and CI

Tests use Django's built-in `TestCase` and live in `api/tests.py`, `images/tests.py`, and `maps/tests.py` (no pytest). Run them with `docker compose exec web uv run manage.py test`. CI (`.github/workflows/`) builds the container image and the docs site, and type-checks the frontend (`typecheck.yml`, also run during the container build) — there is no automated Python test or lint step, so run tests locally.

## Coding guidelines

Please follow these coding guidelines for this project:
- refrain from adding extraneous files, including code samples or markdown summaries / plans,
- keep Python imports at the top of the file, never nested inside of functions,
- use `django.conf.settings` for configurable values rather than hardcoding them in models or views,
- templates live in `/templates/` (project-level), organized by app name (e.g., `templates/activity/`),
- custom template tags go in `<app>/templatetags/` and require a server restart to be discovered.
