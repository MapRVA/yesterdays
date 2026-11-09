import os

# Directory holding the exported IR bundle (text.xml/bin, vision.xml/bin,
# tokenizer files, metadata.json). Baked into the release image;
# bind-mounted in the dev compose stack.
MODEL_DIR = os.getenv("CLIP_MODEL_DIR", "/app/model")

# OpenVINO device for each tower, chosen per deployment: production runs both
# towers on the Arc GPU, staging/dev on CPU. AUTO picks a GPU when one is
# present, otherwise CPU.
TEXT_DEVICE = os.getenv("CLIP_TEXT_DEVICE", "CPU")
VISION_DEVICE = os.getenv("CLIP_VISION_DEVICE", "AUTO")

# Inference precision for both towers: "f16" or "f32". Empty picks per
# device: f16 on a GPU, f32 on CPU (pinned explicitly, since OpenVINO would
# otherwise run bf16 on CPUs with AVX512-BF16/AMX).
INFERENCE_PRECISION = os.getenv("CLIP_INFERENCE_PRECISION", "")

# Inference threads per tower on CPU; unset or 0 leaves it to OpenVINO, which
# sizes its pool from the visible CPUs and ignores a container's CPU quota. Set
# it to the quota, or every thread spends most of its time being throttled.
INFERENCE_THREADS = int(os.getenv("CLIP_INFERENCE_THREADS") or 0)

# Compiled-kernel cache. Hardware/driver specific, so it belongs on an
# emptyDir/PV, never in the image. Empty string disables caching.
CACHE_DIR = os.getenv("CLIP_CACHE_DIR", "")

# Reject request bodies larger than this on /embed/image.
MAX_IMAGE_BYTES = int(os.getenv("CLIP_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
