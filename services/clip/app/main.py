import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from PIL import UnidentifiedImageError
from pydantic import BaseModel, Field

from . import config
from .encoder import ClipEncoder

logger = logging.getLogger("clip-service")

_encoder = None
_load_error = None


def _load_encoder():
    global _encoder, _load_error
    try:
        encoder = ClipEncoder(
            config.MODEL_DIR,
            config.TEXT_DEVICE,
            config.VISION_DEVICE,
            config.CACHE_DIR,
        )
        encoder.warmup()
    except Exception as exc:
        _load_error = str(exc)
        logger.critical(
            "Failed to load CLIP model from %s — the service will stay "
            "unready. Is the IR bundle present? (%s)",
            config.MODEL_DIR,
            exc,
        )
        return
    _encoder = encoder
    logger.info(
        "CLIP model %s ready (devices: %s)", encoder.meta["model_id"], encoder.devices()
    )


@asynccontextmanager
async def lifespan(app):
    # Load in a background thread so the server accepts health probes while
    # OpenVINO compiles kernels (slow on first GPU start without a warm cache).
    threading.Thread(target=_load_encoder, daemon=True).start()
    yield


app = FastAPI(title="yesterdays-clip", lifespan=lifespan)


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
    encoder = _require_encoder()
    if len(data) > config.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large")
    try:
        embedding = encoder.encode_image(data)
    except UnidentifiedImageError, OSError, ValueError:
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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    if _encoder is None:
        detail = _load_error or "Model still loading"
        raise HTTPException(status_code=503, detail=detail)
    return {"status": "ready"}
