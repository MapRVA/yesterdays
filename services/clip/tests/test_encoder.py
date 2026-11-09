import importlib

import numpy as np
import pytest
from PIL import Image

from app import config
from app.encoder import (
    CLIP_PREPROCESS,
    CLIP_TOKENIZER,
    bundle_specs,
    build_token_array,
    clean_text,
    compile_config,
    preprocess_image,
    resolve_precision,
    split_words,
)

SQUASH = {
    "resize": "squash",
    "interpolation": "bilinear",
    "mean": [0.5, 0.5, 0.5],
    "std": [0.5, 0.5, 0.5],
}
SOT, EOT, PAD = 49406, 49407, 0


# Tokens


def test_short_text_is_wrapped_and_zero_padded():
    tokens = build_token_array([10, 11, 12], 32, SOT, EOT, PAD)
    assert tokens.shape == (1, 32)
    assert tokens.dtype == np.int64
    assert tokens[0, :5].tolist() == [SOT, 10, 11, 12, EOT]
    assert (tokens[0, 5:] == PAD).all()


def test_long_text_truncates_keeping_eot_last():
    ids = list(range(100, 200))
    tokens = build_token_array(ids, 32, SOT, EOT, PAD)[0].tolist()
    assert len(tokens) == 32
    assert tokens[0] == SOT
    assert tokens[1:31] == ids[:30]
    assert tokens[-1] == EOT


def test_exact_fit_has_no_padding():
    ids = list(range(1, 31))
    assert build_token_array(ids, 32, SOT, EOT, PAD)[0].tolist() == [SOT, *ids, EOT]


def test_empty_text_is_sot_eot():
    tokens = build_token_array([], 32, SOT, EOT, PAD)[0].tolist()
    assert tokens[:2] == [SOT, EOT] and set(tokens[2:]) == {PAD}


def test_cleaning_matches_open_clip_lower():
    # ftfy uncurls quotes, then double HTML unescape, whitespace collapse, lowercase.
    assert clean_text("  Lee’s  “Statue”\n&amp;amp; ") == "lee's \"statue\" &"


def test_split_words_keeps_open_clip_specials_and_contractions():
    assert split_words("Don't <START_OF_TEXT> x") == ["don", "'t", "<start_of_text>", "x"]
    # HF's special-token spelling is plain text to open_clip.
    assert split_words("<|endoftext|>") == ["<|", "endoftext", "|>"]
    assert split_words("1920s") == ["1", "9", "2", "0", "s"]


# Images


def test_squash_shape_dtype_and_range():
    rng = np.random.default_rng(0)
    img = Image.fromarray(rng.integers(0, 256, (200, 500, 3), dtype=np.uint8))
    pixels = preprocess_image(img, 336, SQUASH)
    assert pixels.shape == (1, 3, 336, 336)
    assert pixels.dtype == np.float32
    assert pixels.flags["C_CONTIGUOUS"]
    assert pixels.min() >= -1.0 and pixels.max() <= 1.0


def test_squash_solid_colour_values():
    img = Image.new("RGB", (640, 480), (255, 0, 128))
    pixels = preprocess_image(img, 336, SQUASH)[0]
    np.testing.assert_allclose(pixels[0], 1.0)
    np.testing.assert_allclose(pixels[1], -1.0)
    np.testing.assert_allclose(pixels[2], (128 / 255 - 0.5) / 0.5, atol=1e-6)


def test_squash_keeps_whole_frame():
    # Black edges on a wide image survive a squash but not a center crop.
    img = Image.new("RGB", (672, 336), (255, 255, 255))
    img.paste((0, 0, 0), (0, 0, 168, 336))
    img.paste((0, 0, 0), (504, 0, 672, 336))
    squashed = preprocess_image(img, 336, SQUASH)[0, 0]
    assert squashed[:, 0].max() == -1.0 and squashed[:, 168].min() == 1.0
    cropped = preprocess_image(img, 336, CLIP_PREPROCESS)[0, 0]
    np.testing.assert_allclose(cropped, (1.0 - 0.48145466) / 0.26862954, rtol=1e-6)


def test_shortest_edge_resizes_then_crops():
    img = Image.new("RGB", (300, 900), (10, 20, 30))
    assert preprocess_image(img, 336, CLIP_PREPROCESS).shape == (1, 3, 336, 336)


def test_unknown_resize_mode_rejected():
    with pytest.raises(ValueError):
        preprocess_image(Image.new("RGB", (8, 8)), 336, {**SQUASH, "resize": "pad"})


def test_bundle_specs_from_metadata():
    meta = {"preprocess": SQUASH, "tokenizer": {"sot": 1, "eot": 2, "pad": 3}}
    assert bundle_specs(meta) == (SQUASH, {"sot": 1, "eot": 2, "pad": 3})


def test_bundle_specs_default_to_clip():
    preprocess, special = bundle_specs({"model_id": "openai/clip-vit-large-patch14-336"})
    assert preprocess["resize"] == "shortest" and preprocess["interpolation"] == "bicubic"
    assert preprocess["mean"][0] == pytest.approx(0.48145466)
    assert special == CLIP_TOKENIZER


# Precision


@pytest.mark.parametrize(
    "device,requested,expected",
    [
        ("CPU", "", "f32"),
        ("GPU", "", "f16"),
        ("GPU.0", "", "f16"),
        ("gpu", "", "f16"),
        ("GPU", "f32", "f32"),
        ("CPU", "f16", "f16"),
    ],
)
def test_resolve_precision(device, requested, expected):
    assert resolve_precision(device, requested) == expected


def test_resolve_precision_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_precision("CPU", "bf16")


def test_compile_config_physical_device():
    assert compile_config("CPU", "", ["CPU"]) == {"INFERENCE_PRECISION_HINT": "f32"}
    assert compile_config("GPU", "", ["CPU", "GPU"]) == {"INFERENCE_PRECISION_HINT": "f16"}


def test_compile_config_auto_resolves_each_candidate():
    assert compile_config("AUTO", "", ["CPU", "GPU"]) == {
        "DEVICE_PROPERTIES": {
            "CPU": {"INFERENCE_PRECISION_HINT": "f32"},
            "GPU": {"INFERENCE_PRECISION_HINT": "f16"},
        }
    }
    assert compile_config("AUTO:GPU", "f32", ["CPU", "GPU"]) == {
        "DEVICE_PROPERTIES": {"GPU": {"INFERENCE_PRECISION_HINT": "f32"}}
    }


def test_compile_config_limits_threads_on_cpu_only():
    limited = {
        "INFERENCE_PRECISION_HINT": "f32",
        "INFERENCE_NUM_THREADS": 2,
        "ENABLE_CPU_PINNING": False,
    }
    assert compile_config("CPU", "", ["CPU"], threads=2) == limited
    assert compile_config("GPU", "", ["CPU", "GPU"], threads=2) == {
        "INFERENCE_PRECISION_HINT": "f16"
    }
    assert compile_config("AUTO", "", ["CPU", "GPU"], threads=2) == {
        "DEVICE_PROPERTIES": {
            "CPU": limited,
            "GPU": {"INFERENCE_PRECISION_HINT": "f16"},
        }
    }


def test_compile_config_leaves_threads_to_openvino_by_default():
    assert compile_config("CPU", "", ["CPU"], threads=0) == {
        "INFERENCE_PRECISION_HINT": "f32"
    }


@pytest.mark.parametrize(
    "value, expected", [(None, 0), ("", 0), ("0", 0), ("2", 2)]
)
def test_threads_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CLIP_INFERENCE_THREADS", raising=False)
    else:
        monkeypatch.setenv("CLIP_INFERENCE_THREADS", value)
    assert importlib.reload(config).INFERENCE_THREADS == expected
    monkeypatch.delenv("CLIP_INFERENCE_THREADS", raising=False)
    importlib.reload(config)


def test_precision_env(monkeypatch):
    monkeypatch.setenv("CLIP_INFERENCE_PRECISION", "f16")
    assert importlib.reload(config).INFERENCE_PRECISION == "f16"
    monkeypatch.delenv("CLIP_INFERENCE_PRECISION")
    assert importlib.reload(config).INFERENCE_PRECISION == ""
