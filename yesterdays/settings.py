"""
Django settings for yesterdays project.
"""

import os
import re
from pathlib import Path

from celery.schedules import crontab

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

LOCAL_DEV = os.getenv("LOCAL_DEV", "False").lower() in ("true", "1", "yes")

# Set DEBUG to True if LOCAL_DEV is True (unless explicitly overwritten)
if LOCAL_DEV and os.getenv("DJANGO_DEBUG") is None:
    DEBUG = True
else:
    DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("true", "1", "yes")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = (
            "django-insecure-ydhsy&ts2zv0tq9b#nbtsjiga1cbo39hgo0vzlj9y!8#c+t*+2"
        )
    else:
        raise ValueError("DJANGO_SECRET_KEY or DEBUG environment variable must be set")

# Allow hosts from environment variable or use defaults
# localhost is always included for Kubernetes health probes
ALLOWED_HOSTS = ["localhost", "127.0.0.1"] + [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

# Proxy settings for Cloudflare tunnel
# Tell Django to trust the X-Forwarded-Proto header from the proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

BEHIND_CLOUDFLARE_TUNNEL = os.getenv("BEHIND_CLOUDFLARE_TUNNEL", "False").lower() in (
    "true",
    "1",
    "yes",
)

# CORS settings
CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL_ORIGINS", "True").lower() in (
    "true",
    "1",
    "yes",
)

# Prometheus metrics (disabled by default)
PROMETHEUS_ENABLED = os.getenv("PROMETHEUS_ENABLED", "False").lower() in (
    "true",
    "1",
    "yes",
)

DIRECTORIES_ENABLED = os.getenv("DIRECTORIES_ENABLED", "True").lower() in (
    "true",
    "1",
    "yes",
)

# CLIP embedding microservice (services/clip). Semantic search and embedding
# generation post images/text to this service over HTTP; it is required for
# those features to work.
CLIP_SERVICE_URL = os.getenv("CLIP_SERVICE_URL", "").rstrip("/")
# The vision tower encodes serially and a single CPU-only encode of
# ViT-L/14@336px takes several seconds.
CLIP_SERVICE_TIMEOUT = float(os.getenv("CLIP_SERVICE_TIMEOUT", "30"))

# pgvector HNSW search depth. Sets the per-query hnsw.ef_search parameter and
# also caps how deep semantic-search pagination can go (the index can only rank
# this many candidates per query). pgvector hard-caps ef_search at 1000.
HNSW_EF_SEARCH = int(os.getenv("HNSW_EF_SEARCH", "1000"))

# Subject "find similar images" query construction (subjects/similarity.py).
# Shrinkage strength toward the corpus mean for small collections' means:
# mu_c = (n * mean_c + n0 * mu_corpus) / (n + n0)
SUBJECT_SIMILARITY_SHRINKAGE_N0 = int(
    os.getenv("SUBJECT_SIMILARITY_SHRINKAGE_N0", "20")
)
# Cosine similarity above which two set images count as near-duplicates
# (multiple scans/prints of one photo) and only the first is kept
SUBJECT_SIMILARITY_DEDUPE_COSINE = float(
    os.getenv("SUBJECT_SIMILARITY_DEDUPE_COSINE", "0.97")
)

# Nightly visual-duplicate scan (images.tasks.refresh_duplicate_image_pairs).
# How many of the globally closest embedding pairs to store for staff review,
# how many neighbors to examine per image, and the HNSW search depth used for
# the scan (lower than HNSW_EF_SEARCH since we only need the very nearest).
DUPLICATE_PAIRS_COUNT = int(os.getenv("DUPLICATE_PAIRS_COUNT", "100"))
DUPLICATE_PAIRS_NEIGHBORS = int(os.getenv("DUPLICATE_PAIRS_NEIGHBORS", "5"))
DUPLICATE_PAIRS_EF_SEARCH = int(os.getenv("DUPLICATE_PAIRS_EF_SEARCH", "200"))
DUPLICATE_PAIRS_BATCH_SIZE = int(os.getenv("DUPLICATE_PAIRS_BATCH_SIZE", "500"))
# Hour (0-23, in CELERY_TIMEZONE) at which the nightly duplicate scan runs
DUPLICATE_PAIRS_SCAN_HOUR = int(os.getenv("DUPLICATE_PAIRS_SCAN_HOUR", "4"))

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "django.contrib.humanize",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework_gis",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    "django_celery_results",
    "django_vite",
    "oauth2_provider",
    "api",
    "osm_auth",
    "subjects",
    "regions",
    "images",
    "maps",
    "activity",
    "yesterdays",
]

if DIRECTORIES_ENABLED:
    INSTALLED_APPS.insert(-1, "directories")

if PROMETHEUS_ENABLED:
    INSTALLED_APPS.insert(0, "django_prometheus")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "osm_auth.middleware.OSMAuthenticationMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if PROMETHEUS_ENABLED:
    MIDDLEWARE.insert(0, "django_prometheus.middleware.PrometheusBeforeMiddleware")
    MIDDLEWARE.append("django_prometheus.middleware.PrometheusAfterMiddleware")

# Rewrite REMOTE_ADDR from CF-Connecting-IP so DRF throttling and any other
# IP-based logic see the real client IP rather than the tunnel's loopback.
# Mounted first so all downstream middleware sees the corrected value.
if BEHIND_CLOUDFLARE_TUNNEL:
    MIDDLEWARE.insert(0, "yesterdays.middleware.CloudflareTunnelMiddleware")

ROOT_URLCONF = "yesterdays.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "osm_auth.context_processors.osm_auth",
                "images.context_processors.site_settings",
                "regions.context_processors.current_region",
            ],
        },
    },
]

WSGI_APPLICATION = "yesterdays.wsgi.application"


# Use PostgreSQL
DATABASES = {
    "default": {
        "ENGINE": "django_prometheus.db.backends.postgis"
        if PROMETHEUS_ENABLED
        else "django.contrib.gis.db.backends.postgis",
        "NAME": os.getenv("PG_DBNAME", "georef"),
        "USER": os.getenv("PG_USER", "django_user"),
        "PASSWORD": os.getenv("PG_PASSWORD", ""),
        "HOST": os.getenv("PG_HOST", "georef-db-rw"),
        "PORT": os.getenv("PG_PORT", "5432"),
        "OPTIONS": {
            "sslmode": os.getenv("PG_SSL_MODE", "prefer"),
        },
        "CONN_MAX_AGE": 0,  # New connection per request
    }
}

# Local memory cache (might consider e.g. Redis in the future).
# Vector tiles are deliberately not cached here: they are cached at the
# Cloudflare edge (Cache Rule on /api/v1/tiles/v*), keeping large tile blobs
# out of process memory.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "default",
    },
}

# Read database password from mounted secret if available
db_password_file = os.getenv("DB_PASSWORD_FILE", "/etc/georef-db/password")
if os.path.exists(db_password_file):
    with open(db_password_file, "r") as f:
        DATABASES["default"]["PASSWORD"] = f.read().strip()


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "UTC")

USE_I18N = True

USE_TZ = True

# Django Vite configuration
DJANGO_VITE = {
    "default": {
        "dev_mode": os.getenv("DJANGO_VITE_DEV_MODE", "False").lower() == "true",
        "dev_server_host": "localhost",
        "dev_server_port": int(os.getenv("DJANGO_VITE_DEV_PORT", "5173")),
        "manifest_path": BASE_DIR / "static" / "manifest.json",
    }
}

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/
STATIC_URL = "static/"
# In production: STATIC_ROOT is where collectstatic puts files and where they're served from
# In development: STATICFILES_DIRS tells Django where to find static files
if DEBUG:
    STATIC_ROOT = None  # Not used in development
    STATICFILES_DIRS = [
        BASE_DIR / "static",  # Vite build output (for production builds during dev)
    ]
else:
    STATIC_ROOT = BASE_DIR / "static"  # Vite outputs here, Whitenoise serves from here
    STATICFILES_DIRS = []  # No additional dirs in production


def immutable_file_test(path, url):
    # Match Vite's hash pattern: main-CSliV9zW.js, style-a4ef2389.css
    return re.match(r"^.+[.-][0-9a-zA-Z_-]{8,12}\..+$", url)


WHITENOISE_IMMUTABLE_FILE_TEST = immutable_file_test

# Whitenoise configuration
# In development, use default storage so Vite rebuilds are picked up immediately
# In production, use CompressedManifestStaticFilesStorage for caching/compression
if DEBUG:
    WHITENOISE_AUTOREFRESH = True
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
        },
    }

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Celery Configuration (RabbitMQ)
CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//"
)
CELERY_RESULT_BACKEND = "django-db"
CELERY_CACHE_BACKEND = "django-cache"
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
    "polling_interval": 0.1,
}
CELERY_RESULT_EXPIRES = 3600  # Auto-cleanup results after 1 hour
CELERY_TASK_IGNORE_RESULT = True  # Default: ignore results unless a task opts in
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_TIME_LIMIT = 300  # 5 minutes max per task
CELERY_TASK_SOFT_TIME_LIMIT = 240  # Soft limit at 4 minutes

# RabbitMQ-specific settings
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_HEARTBEAT = 10  # Heartbeat interval in seconds
CELERY_BROKER_POOL_LIMIT = 10  # Connection pool size

# Queue routing configuration
CELERY_TASK_ROUTES = {
    # Image processing (thumbnails + transforms) goes to background queue
    "images.tasks.process_image": {"queue": "background"},
    "images.tasks.process_images_batch": {"queue": "background"},
    # Metadata refresh tasks go to background queue
    "subjects.tasks.refresh_next_wikidata_item": {"queue": "background"},
    "subjects.tasks.refresh_next_osm_element": {"queue": "background"},
    "subjects.tasks.reconcile_project_graph": {"queue": "background"},
    # IIIF tile generation goes to background queue
    "images.tasks.generate_iiif_tiles": {"queue": "background"},
    "images.tasks.cleanup_old_image_assets": {"queue": "background"},
    "images.tasks.cleanup_stale_import_slots": {"queue": "background"},
    "images.tasks.reconcile_collection_stats": {"queue": "background"},
    "images.tasks.reconcile_collection_region_stats": {"queue": "background"},
    "images.tasks.refresh_collection_embedding_stats": {"queue": "background"},
    "images.tasks.refresh_next_collection_embedding_stats": {"queue": "background"},
    "images.tasks.refresh_duplicate_image_pairs": {"queue": "background"},
    # First-time Wikidata closure hydration fires off a request thread
    # right after a new WikidataItem is saved; the user is waiting on the
    # subject to populate, so this one stays on the urgent queue.
    "subjects.tasks.hydrate_wikidata_item": {"queue": "urgent"},
    # OSM avatar mirroring goes to background queue
    "osm_auth.tasks.download_osm_avatar": {"queue": "background"},
}

if DIRECTORIES_ENABLED:
    CELERY_TASK_ROUTES.update(
        {
            "directories.tasks.ocr.generate_iiif_tiles": {"queue": "background"},
            "directories.tasks.ocr.run_page_ocr": {"queue": "background"},
        }
    )

# Metadata refresh intervals (seconds between each refresh)
# These control how often Celery Beat triggers each refresh task
# Default 1 minute (60s)
METADATA_REFRESH_WIKIDATA_INTERVAL = int(
    os.getenv("METADATA_REFRESH_WIKIDATA_INTERVAL", "60")
)
METADATA_REFRESH_OSM_INTERVAL = int(os.getenv("METADATA_REFRESH_OSM_INTERVAL", "60"))

# Default queue for tasks not explicitly routed
CELERY_TASK_DEFAULT_QUEUE = "urgent"
CELERY_TASK_DEFAULT_EXCHANGE = "tasks"
CELERY_TASK_DEFAULT_EXCHANGE_TYPE = "direct"
CELERY_TASK_DEFAULT_ROUTING_KEY = "urgent"

# Queue definitions
CELERY_TASK_QUEUES = {
    "urgent": {
        "exchange": "tasks",
        "routing_key": "urgent",
    },
    "background": {
        "exchange": "tasks",
        "routing_key": "background",
    },
}

# Celery Beat evaluates crontab schedules in this timezone. It defaults to UTC
# via TIME_ZONE; interval (seconds) schedules are timezone-independent, so this
# only affects crontab entries like the nightly duplicate scan.
CELERY_TIMEZONE = TIME_ZONE

# Celery Beat schedule for periodic tasks
# Rate limiting is achieved by Beat's schedule interval, not per-worker limits
# Tasks expire shortly before the next one is scheduled to prevent backlog buildup
CELERY_BEAT_SCHEDULE = {
    "refresh-next-wikidata-item": {
        "task": "subjects.tasks.refresh_next_wikidata_item",
        "schedule": float(METADATA_REFRESH_WIKIDATA_INTERVAL),
        "options": {"expires": METADATA_REFRESH_WIKIDATA_INTERVAL - 5},
    },
    "refresh-next-osm-element": {
        "task": "subjects.tasks.refresh_next_osm_element",
        "schedule": float(METADATA_REFRESH_OSM_INTERVAL),
        "options": {"expires": METADATA_REFRESH_OSM_INTERVAL - 5},
    },
    "cleanup-stale-import-slots": {
        "task": "images.tasks.cleanup_stale_import_slots",
        "schedule": 3600.0,  # every hour
    },
    "reconcile-collection-stats": {
        "task": "images.tasks.reconcile_collection_stats",
        "schedule": 3600.0,  # every hour; signals keep stats current in real time
    },
    "reconcile-collection-region-stats": {
        "task": "images.tasks.reconcile_collection_region_stats",
        # Also hourly, but on the half hour rather than a float interval, so
        # the two heaviest full-table aggregates don't both fire off the same
        # beat-startup instant
        "schedule": crontab(minute=30),
    },
    "refresh-next-collection-embedding-stats": {
        "task": "images.tasks.refresh_next_collection_embedding_stats",
        "schedule": 600.0,  # every 10 minutes: top up the most stale collection
    },
    "refresh-collection-embedding-stats": {
        "task": "images.tasks.refresh_collection_embedding_stats",
        "schedule": 86400.0,  # daily reconcile for count-neutral changes
    },
    "refresh-duplicate-image-pairs": {
        "task": "images.tasks.refresh_duplicate_image_pairs",
        # Nightly at DUPLICATE_PAIRS_SCAN_HOUR (in CELERY_TIMEZONE)
        "schedule": crontab(hour=DUPLICATE_PAIRS_SCAN_HOUR, minute=0),
    },
    "reconcile-project-graph": {
        "task": "subjects.tasks.reconcile_project_graph",
        "schedule": 21600.0,  # every 6 hours
        "options": {"expires": 870},
    },
}

# External Metadata Refresh Settings
# Hours after which metadata is considered stale and needs refresh
METADATA_REFRESH_STALE_HOURS = int(os.getenv("METADATA_REFRESH_STALE_HOURS", "24"))
# Max consecutive failures before giving up on a record
METADATA_REFRESH_MAX_FAILURES = int(os.getenv("METADATA_REFRESH_MAX_FAILURES", "5"))
# Postpass API settings for OSM geometry fetching
METADATA_REFRESH_POSTPASS_URL = os.getenv(
    "METADATA_REFRESH_POSTPASS_URL",
    "https://postpass.geofabrik.de/api/0.2/interpreter",
)
METADATA_REFRESH_POSTPASS_TIMEOUT = int(
    os.getenv("METADATA_REFRESH_POSTPASS_TIMEOUT", "60")
)

# Memgraph property-graph store (Bolt) for the Wikidata subject mirror
MEMGRAPH_URL = os.getenv("MEMGRAPH_URL", "bolt://localhost:7687")
MEMGRAPH_TIMEOUT = int(os.getenv("MEMGRAPH_TIMEOUT", "60"))

# How long the subjects-map info panel's JSON responses may be cached.
SUBJECT_MAP_INFO_CACHE_SECONDS = int(os.getenv("SUBJECT_MAP_INFO_CACHE_SECONDS", "300"))

# Languages whose literals the Wikidata mirror keeps (comma-separated BCP47
# tags).
WIKIDATA_MIRROR_LANGUAGES = [
    lang.strip()
    for lang in os.getenv("WIKIDATA_MIRROR_LANGUAGES", "en,mul").split(",")
    if lang.strip()
]

# OSM Authentication Settings
OSM_URL = os.getenv("OSM_URL", "https://www.openstreetmap.org")
OSM_CLIENT_ID = os.getenv("OSM_CLIENT_ID")
OSM_CLIENT_SECRET = os.getenv("OSM_CLIENT_SECRET")
OSM_SECRET_KEY = os.getenv("OSM_SECRET_KEY")
OSM_LOGIN_REDIRECT_URI = os.getenv("OSM_LOGIN_REDIRECT_URI")
OSM_SCOPE = "read_prefs"

# OSM Admin Settings
OSM_ADMIN_USERNAMES = (
    os.getenv("OSM_ADMIN_USERNAMES", "").split(",")
    if os.getenv("OSM_ADMIN_USERNAMES")
    else []
)

# Authentication Backends
AUTHENTICATION_BACKENDS = [
    "osm_auth.auth_backends.OSMAuthBackend",  # Primary: OSM OAuth authentication
    "django.contrib.auth.backends.ModelBackend",  # Usually unused
]

# Optional hardcoded admin (enabled by default in LOCAL_DEV mode unless explicitly overwritten)
if LOCAL_DEV and os.getenv("ALLOW_HARDCODED_ADMIN") is None:
    ALLOW_HARDCODED_ADMIN = True
else:
    ALLOW_HARDCODED_ADMIN = DEBUG and os.getenv(
        "ALLOW_HARDCODED_ADMIN", "false"
    ).lower() in ("true", "1", "yes")

if ALLOW_HARDCODED_ADMIN:
    AUTHENTICATION_BACKENDS.append("osm_auth.auth_backends.HardcodedAdminBackend")

# Session settings for authentication
SESSION_COOKIE_AGE = 86400  # 24 hours
SESSION_SAVE_EVERY_REQUEST = True

# Login URL for @login_required decorator
LOGIN_URL = "/auth/login/"

# Flickr API Settings
FLICKR_API_KEY = os.getenv("FLICKR_API_KEY")
FLICKR_API_SECRET = os.getenv("FLICKR_API_SECRET")

# R2 Storage
R2_PUBLIC_URL_BASE = os.getenv("IMPORT_R2_PUBLIC_URL_BASE", "")

# OpenRouter API (for OCR and other LLM tasks)
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
OPENROUTER_DEFAULT_MODEL = os.getenv("OPENROUTER_DEFAULT_MODEL", "")

# Map Settings
# Protomaps API key for map tiles
PROTOMAPS_API_KEY = os.getenv("PROTOMAPS_API_KEY")

# Activity Feed Settings
# Milestone thresholds for user georeference achievements
ACTIVITY_MILESTONE_THRESHOLDS = [
    5,
    15,
    50,
    100,
    250,
    500,
    1000,
    2000,
    3000,
    4000,
    5000,
    6000,
    7000,
    8000,
    9000,
    10000,
]

# Milestone thresholds for sitewide achievements
ACTIVITY_SITEWIDE_MILESTONE_THRESHOLDS = [
    50,
    100,
    250,
    500,
    1000,
    2000,
    3000,
    4000,
    5000,
    6000,
    7000,
    8000,
    9000,
    10000,
    11000,
    12000,
    13000,
    14000,
    15000,
    16000,
    17000,
    18000,
    19000,
    20000,
]

# ---------------------------------------------------------------------------
# Community write endpoints
# ---------------------------------------------------------------------------
# Input limits applied by images.validation to every session-authenticated
# mutation endpoint (georeferences, validations, comments, ratings, skips,
# subject tagging, album membership). They bound what a single request can
# cost us, so they are deliberately generous for humans and tight for scripts.

# Maximum accepted request body, in bytes, for JSON mutation endpoints. Read
# before json.loads so an oversized body is rejected without being parsed.
COMMUNITY_WRITE_MAX_BODY_BYTES = int(
    os.getenv("COMMUNITY_WRITE_MAX_BODY_BYTES", str(64 * 1024))
)
# Polygon submissions carry a whole ring of coordinates, so they get their own,
# larger body budget.
POLYGON_MAX_BODY_BYTES = int(os.getenv("POLYGON_MAX_BODY_BYTES", str(256 * 1024)))

# Maximum lengths for free-text fields written by community endpoints.
COMMENT_MAX_LENGTH = int(os.getenv("COMMENT_MAX_LENGTH", "10000"))
GEOREFERENCE_NOTES_MAX_LENGTH = int(os.getenv("GEOREFERENCE_NOTES_MAX_LENGTH", "2000"))
VALIDATION_NOTES_MAX_LENGTH = int(os.getenv("VALIDATION_NOTES_MAX_LENGTH", "2000"))
SKIP_REASON_MAX_LENGTH = int(os.getenv("SKIP_REASON_MAX_LENGTH", "500"))

# Album metadata. The title cap matches Album.title's max_length so the API and
# the HTML edit form agree.
ALBUM_TITLE_MAX_LENGTH = int(os.getenv("ALBUM_TITLE_MAX_LENGTH", "500"))
ALBUM_DESCRIPTION_MAX_LENGTH = int(os.getenv("ALBUM_DESCRIPTION_MAX_LENGTH", "10000"))

# Maximum number of image IDs accepted by a single bulk request.
BULK_MAX_IMAGE_IDS = int(os.getenv("BULK_MAX_IMAGE_IDS", "500"))

# Polygon georeference geometry limits, checked structurally on the GeoJSON
# before any GEOS parsing or database work happens.
POLYGON_MAX_RINGS = int(os.getenv("POLYGON_MAX_RINGS", "16"))
POLYGON_MAX_VERTICES_PER_RING = int(os.getenv("POLYGON_MAX_VERTICES_PER_RING", "2000"))
POLYGON_MAX_TOTAL_VERTICES = int(os.getenv("POLYGON_MAX_TOTAL_VERTICES", "4000"))
# Area ceiling in square degrees. An aerial photograph covers a neighbourhood,
# not a continent; 0.25 sq deg is roughly a 55 km by 43 km box at this
# latitude, which is far larger than any legitimate submission.
POLYGON_MAX_AREA_SQ_DEGREES = float(os.getenv("POLYGON_MAX_AREA_SQ_DEGREES", "0.25"))

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "api.pagination.DefaultPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "api.authentication.HeaderOnlyOAuth2Authentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_THROTTLE_RATES": {
        "register": "1/min",
        "token": "150/min",
    },
}

# Django OAuth Toolkit
OAUTH2_PROVIDER = {
    "SCOPES": {
        "read": "Read-only API access",
        "import": "Import images into collections",
    },
    "DEFAULT_SCOPES": ["read"],
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600 * 8,  # 8 hours
    "REFRESH_TOKEN_EXPIRE_SECONDS": 86400 * 30,  # 30 days
    "ROTATE_REFRESH_TOKEN": True,
    # If a rotated (already-used) refresh token is re-presented, revoke the
    # entire token family — this is the BCP-recommended response to suspected
    # token theft. The grace period absorbs legitimate network-glitch retries.
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 30,
    "PKCE_REQUIRED": True,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["http", "https"],
    # RFC 8414 discovery document (/.well-known/oauth-authorization-server).
    # DOT's defaults advertise everything it *can* do; these narrow the document
    # to what this server actually accepts. Applications are registered with the
    # authorization-code grant only (see api/views.register_app_view), so the
    # implicit, password, client-credentials, and device-code grants never apply.
    "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    # Public clients authenticate at /oauth/token/ with PKCE and no secret,
    # which RFC 8414 spells as the "none" auth method; DOT's default omits it.
    "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": [
        "client_secret_post",
        "client_secret_basic",
        "none",
    ],
    # S256OnlyAuthorizationView already rejects code_challenge_method=plain at
    # /oauth/authorize/. Enabling the gate makes the validator reject it too and
    # keeps "plain" out of code_challenge_methods_supported.
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
}

# drf-spectacular (OpenAPI schema generation)
SPECTACULAR_SETTINGS = {
    "TITLE": "Yesterdays API",
    "DESCRIPTION": "API for querying historical georeferenced images.",
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
