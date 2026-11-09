import html
import json
import threading
import time
from io import BytesIO
from pathlib import Path

import ftfy
import numpy as np
import openvino as ov
import regex
from PIL import Image
from transformers import CLIPTokenizerFast

from .metrics import INFLIGHT, PHASE_DURATION, TEXT_TOKENS, TEXT_TRUNCATED

# Bundles exported before metadata.json grew "preprocess"/"tokenizer" keys
# were OpenAI CLIP: shortest-edge bicubic resize + center crop, CLIP stats,
# the same BPE special ids, and HF's padding with <|endoftext|>.
CLIP_PREPROCESS = {
    "resize": "shortest",
    "interpolation": "bicubic",
    "mean": [0.48145466, 0.4578275, 0.40821073],
    "std": [0.26862954, 0.26130258, 0.27577711],
}
CLIP_TOKENIZER = {"sot": 49406, "eot": 49407, "pad": 49407}

INTERPOLATION = {"bilinear": Image.BILINEAR, "bicubic": Image.BICUBIC}

# open_clip's pre-tokenizer regex. Its special-token names differ from HF's
# (<|startoftext|>), so a literal "<|endoftext|>" in a query is plain text to
# open_clip but a special token to HF; splitting words ourselves keeps HF from
# ever seeing a special-token string.
SPECIAL_TOKENS = ("<start_of_text>", "<end_of_text>")
WORD_PATTERN = regex.compile(
    "|".join(SPECIAL_TOKENS)
    + r"""|'s|'t|'re|'ve|'m|'ll|'d|[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+""",
    regex.IGNORECASE,
)


class ClipEncoder:
    """
    Both towers of a CLIP-style model compiled with OpenVINO, plus the
    tokenizer and preprocessing described by the bundle's metadata.json.

    Each tower keeps one persistent infer request guarded by a lock: with the
    LATENCY hint the device runs a single stream anyway, and FastAPI calls us
    from a thread pool, so concurrent requests simply queue on the lock.
    """

    def __init__(
        self, model_dir, text_device, vision_device, cache_dir="", precision="", threads=0
    ):
        model_dir = Path(model_dir)
        self.meta = json.loads((model_dir / "metadata.json").read_text())
        self.preprocess, self.special = bundle_specs(self.meta)

        core = ov.Core()
        if cache_dir:
            core.set_property({"CACHE_DIR": cache_dir})
        self._requested = {}
        compiled = {}
        for name, device in (("text", text_device), ("vision", vision_device)):
            props = compile_config(device, precision, core.available_devices, threads)
            self._requested[name] = props
            compiled[name] = core.compile_model(
                model_dir / f"{name}.xml",
                device,
                {"PERFORMANCE_HINT": "LATENCY", **props},
            )
        self._text, self._vision = compiled["text"], compiled["vision"]
        self._text_req = self._text.create_infer_request()
        self._vision_req = self._vision.create_infer_request()
        self._text_lock = threading.Lock()
        self._vision_lock = threading.Lock()

        # Words are BPE-encoded one at a time and truncated by
        # build_token_array, so HF's length limit (and its warning) is moot.
        self._tokenizer = CLIPTokenizerFast.from_pretrained(
            model_dir, model_max_length=2**31
        )
        # clean_text() already did open_clip's normalization; HF's own NFC +
        # lowercase pass would re-normalize a few rare inputs differently.
        self._tokenizer.backend_tokenizer.normalizer = None

    def devices(self):
        """Actual execution devices and inference precision per tower, for /info."""
        result = {}
        for name, compiled in (("text", self._text), ("vision", self._vision)):
            try:
                devices = compiled.get_property("EXECUTION_DEVICES")
            except RuntimeError:
                devices = "unknown"
            try:
                precision = compiled.get_property(
                    "INFERENCE_PRECISION_HINT"
                ).get_type_name()
            except RuntimeError:
                # AUTO can't report it; show what was requested per device.
                requested = self._requested[name]
                precision = {
                    device: props["INFERENCE_PRECISION_HINT"]
                    for device, props in requested.get("DEVICE_PROPERTIES", {}).items()
                } or "unknown"
            result[name] = {"devices": devices, "precision": precision}
        return result

    def tokenize(self, text):
        words = split_words(text)
        plain = [w for w in words if w not in SPECIAL_TOKENS]
        encoded = iter(
            self._tokenizer(plain, add_special_tokens=False)["input_ids"]
            if plain
            else []
        )
        special_ids = dict(zip(SPECIAL_TOKENS, (self.special["sot"], self.special["eot"])))
        ids = []
        for word in words:
            ids.extend([special_ids[word]] if word in special_ids else next(encoded))
        context_length = self.meta["context_length"]
        TEXT_TOKENS.observe(len(ids))
        if len(ids) > context_length - 2:
            TEXT_TRUNCATED.inc()
        return build_token_array(
            ids,
            context_length,
            self.special["sot"],
            self.special["eot"],
            self.special["pad"],
        )

    def encode_text(self, text):
        with INFLIGHT.labels("text").track_inprogress():
            with PHASE_DURATION.labels("text", "tokenize").time():
                input_ids = self.tokenize(text)
            return self._infer("text", self._text_lock, self._text_req, input_ids)

    def encode_image(self, data):
        with INFLIGHT.labels("vision").track_inprogress():
            with (
                PHASE_DURATION.labels("vision", "decode").time(),
                Image.open(BytesIO(data)) as img,
            ):
                # Let libjpeg decode at a reduced DCT scale (still >= the input
                # size on both sides): far cheaper than a full 1920px decode,
                # and how the canonical corpus vectors were computed.
                size = self.meta["image_size"]
                img.draft("RGB", (size, size))
                rgb = img.convert("RGB")
            return self.encode_pil(rgb)

    def encode_pil(self, img):
        with PHASE_DURATION.labels("vision", "preprocess").time():
            pixel_values = preprocess_image(
                img, self.meta["image_size"], self.preprocess
            )
        return self._infer("vision", self._vision_lock, self._vision_req, pixel_values)

    def _infer(self, tower, lock, request, inputs):
        """Run one tower's infer request, timing the wait for its lock separately."""
        queued = time.perf_counter()
        with lock:
            started = time.perf_counter()
            result = request.infer({0: inputs})
        PHASE_DURATION.labels(tower, "lock_wait").observe(started - queued)
        PHASE_DURATION.labels(tower, "infer").observe(time.perf_counter() - started)
        return _normalize(next(iter(result.values()))[0])

    def warmup(self):
        # Straight to the infer requests: the slow first inference would
        # otherwise land in the latency histograms on every restart.
        self._text_req.infer({0: self.tokenize("warmup")})
        image = Image.new("RGB", (64, 64))
        size = self.meta["image_size"]
        self._vision_req.infer({0: preprocess_image(image, size, self.preprocess)})


def bundle_specs(meta):
    """(preprocess, special token ids) for a bundle, defaulting to OpenAI CLIP's."""
    return meta.get("preprocess", CLIP_PREPROCESS), meta.get("tokenizer", CLIP_TOKENIZER)


def clean_text(text):
    """open_clip's "lower" cleaning: ftfy, double HTML unescape, collapse whitespace, lowercase."""
    text = ftfy.fix_text(text)
    text = html.unescape(html.unescape(text)).strip()
    return " ".join(text.split()).strip().lower()


def split_words(text):
    return WORD_PATTERN.findall(clean_text(text))


def build_token_array(ids, context_length, sot, eot, pad):
    """[sot] + ids + [eot], truncated keeping eot last, padded: open_clip's layout."""
    tokens = [sot, *ids[: context_length - 2], eot]
    tokens += [pad] * (context_length - len(tokens))
    return np.array([tokens], dtype=np.int64)


def preprocess_image(img, size, preprocess=CLIP_PREPROCESS):
    """RGB PIL image -> (1, 3, size, size) float32, per the bundle's preprocess spec."""
    resample = INTERPOLATION[preprocess["interpolation"]]
    if preprocess["resize"] == "squash":
        img = img.resize((size, size), resample)
    elif preprocess["resize"] == "shortest":
        img = _center_crop(_resize_shortest(img, size, resample), size)
    else:
        raise ValueError(f"Unknown resize mode {preprocess['resize']!r}")
    pixels = np.asarray(img, dtype=np.float32) / np.float32(255)
    mean = np.asarray(preprocess["mean"], dtype=np.float32)
    std = np.asarray(preprocess["std"], dtype=np.float32)
    pixels = (pixels - mean) / std
    return np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])


def _resize_shortest(img, size, resample):
    # Same rounding as CLIPImageProcessor's shortest_edge resize.
    width, height = img.size
    short, long = (width, height) if width <= height else (height, width)
    new_long = int(size * long / short)
    new_size = (size, new_long) if width <= height else (new_long, size)
    return img.resize(new_size, resample)


def _center_crop(img, size):
    # Floor offsets, as CLIPImageProcessor (which built the old CLIP corpus).
    width, height = img.size
    left = (width - size) // 2
    top = (height - size) // 2
    return img.crop((left, top, left + size, top + size))


def resolve_precision(device, precision=""):
    """Explicit f16/f32 wins; otherwise f16 on a GPU and f32 elsewhere."""
    if precision:
        if precision not in ("f16", "f32"):
            raise ValueError(f"Unknown inference precision {precision!r}")
        return precision
    return "f16" if device.upper().startswith("GPU") else "f32"


def device_properties(device, precision="", threads=0):
    """Compile properties for one physical device."""
    props = {"INFERENCE_PRECISION_HINT": resolve_precision(device, precision)}
    if threads and device.upper().startswith("CPU"):
        # OpenVINO pins by default, which would tie so few threads to fixed
        # cores even while other pods on the node are busy on them.
        props["INFERENCE_NUM_THREADS"] = threads
        props["ENABLE_CPU_PINNING"] = False
    return props


def compile_config(device, precision, available_devices, threads=0):
    """
    OpenVINO compile properties pinning the inference precision.

    Always explicit, because CPUs with AVX512-BF16/AMX otherwise silently run
    bf16. Virtual devices (AUTO/MULTI/HETERO) only forward the hint as
    per-device properties, resolved for each candidate they might pick. A
    thread count applies to CPU only.
    """
    virtual, _, candidates = device.partition(":")
    if virtual.upper() not in ("AUTO", "MULTI", "HETERO"):
        return device_properties(device, precision, threads)
    names = candidates.split(",") if candidates else available_devices
    return {
        "DEVICE_PROPERTIES": {
            name: device_properties(name, precision, threads) for name in names
        }
    }


def _normalize(vector):
    vector = vector.astype(np.float32)
    return (vector / np.linalg.norm(vector)).tolist()
