import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, Response
from PIL import UnidentifiedImageError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import config
from .encoder import ClipEncoder
from .metrics import (
    IMAGE_BYTES,
    MODEL_LOAD_SECONDS,
    MODEL_STATE,
    REQUEST_DURATION,
    REQUESTS,
    set_model_info,
)

logger = logging.getLogger("clip-service")

_encoder = None
_load_error = None

# Only these paths are measured, which keeps probes and stray paths (scanners'
# 404s) out of the request metrics.
MEASURED_PATHS = {"/embed/text", "/embed/image"}


def _load_encoder():
    global _encoder, _load_error
    started = time.perf_counter()
    try:
        encoder = ClipEncoder(
            config.MODEL_DIR,
            config.TEXT_DEVICE,
            config.VISION_DEVICE,
            config.CACHE_DIR,
            config.INFERENCE_PRECISION,
            config.INFERENCE_THREADS,
        )
        encoder.warmup()
    except Exception as exc:
        _load_error = str(exc)
        MODEL_STATE.state("failed")
        logger.critical(
            "Failed to load embedding model from %s — the service will stay "
            "unready. Is the IR bundle present? (%s)",
            config.MODEL_DIR,
            exc,
        )
        return
    MODEL_LOAD_SECONDS.set(time.perf_counter() - started)
    set_model_info(encoder.meta["model_id"], encoder.devices())
    MODEL_STATE.state("ready")
    _encoder = encoder
    logger.info(
        "Embedding model %s ready (devices: %s)",
        encoder.meta["model_id"],
        encoder.devices(),
    )


@asynccontextmanager
async def lifespan(app):
    # Load in a background thread so the server accepts health probes while
    # OpenVINO compiles kernels (slow on first GPU start without a warm cache).
    threading.Thread(target=_load_encoder, daemon=True).start()
    yield


app = FastAPI(title="yesterdays-clip", lifespan=lifespan)


@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    endpoint = request.url.path
    if endpoint not in MEASURED_PATHS:
        return await call_next(request)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        REQUEST_DURATION.labels(endpoint).observe(time.perf_counter() - started)
        REQUESTS.labels(endpoint, str(status)).inc()


def _require_encoder():
    if _encoder is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return _encoder


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/embed/text")
def embed_text(request: TextRequest):
    encoder = _require_encoder()
    embedding = encoder.encode_text(request.text)
    return {
        "embedding": embedding,
        "model": encoder.meta["model_id"],
        "dim": len(embedding),
    }


@app.post("/embed/image")
def embed_image(data: bytes = Body(media_type="application/octet-stream")):
    IMAGE_BYTES.observe(len(data))
    encoder = _require_encoder()
    if len(data) > config.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large")
    try:
        embedding = encoder.encode_image(data)
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid image")
    return {
        "embedding": embedding,
        "model": encoder.meta["model_id"],
        "dim": len(embedding),
    }


@app.get("/info")
def info():
    encoder = _require_encoder()
    return {**encoder.meta, "devices": encoder.devices()}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    if _encoder is None:
        detail = _load_error or "Model still loading"
        raise HTTPException(status_code=503, detail=detail)
    return {"status": "ready"}
