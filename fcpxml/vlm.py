"""Frame captions from a local vision-language model (the ``find`` extra).

Runs in-process on Apple Silicon via mlx-vlm and NEVER goes online: the
Hugging Face offline switches are set before the library is imported, and a
model that is not already in the local cache is reported with the one
command that fetches it. Which model is configuration (FCP_MCP_VLM_MODEL).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from fcpxml import render

MODEL_ENV = "FCP_MCP_VLM_MODEL"
DEFAULT_MODEL = "mlx-community/Qwen2-VL-2B-Instruct-4bit"
INSTALL = "pip install 'fcp-mcp-server[find]'   # Apple Silicon only"
# Frames are downscaled to 1080p (short edge capped, aspect kept) before
# captioning. Measured on a 2160x3840 HEVC frame: 32 s at full size,
# 4.8 s at 1080x1920, same caption. The model looks at the picture; it
# does not need to count pixels.
CAPTION_SHORT_SIDE = 1080
PROMPT = "Describe this video frame in one short sentence: subject, action, setting, shot size."
_loaded: dict = {}


def model_id() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def available() -> bool:
    return importlib.util.find_spec("mlx_vlm") is not None


def _hub_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_cached(model: Optional[str] = None) -> bool:
    """The Hugging Face hub cache holds at least one snapshot of *model*."""
    name = (model or model_id()).replace("/", "--")
    snaps = _hub_cache() / f"models--{name}" / "snapshots"
    return snaps.is_dir() and any(snaps.iterdir())


def _reset() -> None:
    _loaded.clear()


def load_model(model: Optional[str] = None):
    """``(model, processor, config)``, loaded once. Raises with a named fix."""
    model = model or model_id()
    if model in _loaded:
        return _loaded[model]
    if not available():
        raise RuntimeError(
            f"vision model not available: the find extra is not installed. Install: {INSTALL}"
        )
    if not model_cached(model):
        raise RuntimeError(
            f"vision model {model} is not downloaded. Fetch it once (network) with: "
            f"hf download {model} — indexing itself never goes online."
        )
    # Before the first import, deliberately: the library reads these at load.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import mlx_vlm
    from mlx_vlm.utils import load_config

    m, processor = mlx_vlm.load(model)
    _loaded[model] = (m, processor, load_config(model))
    return _loaded[model]


def caption(image_path: str, model: Optional[str] = None, max_tokens: int = 60) -> str:
    m, processor, config = load_model(model)
    import mlx_vlm
    from mlx_vlm.prompt_utils import apply_chat_template

    prompt = apply_chat_template(processor, config, PROMPT, num_images=1)
    out = mlx_vlm.generate(m, processor, prompt, [image_path], max_tokens=max_tokens, verbose=False)
    # mlx-vlm >= 0.3 returns a GenerationResult; older versions the string.
    return str(getattr(out, "text", out)).strip()


def caption_shots(
    media_path: str,
    shots: list[tuple[Fraction, Fraction]],
    *,
    model: Optional[str] = None,
    max_frames: int = 40,
    progress: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Caption the mid-point frame of each shot. Frames are deleted after use."""
    frames_dir = render.cache_dir() / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha256(str(media_path).encode()).hexdigest()[:12]
    todo = shots[:max_frames]
    rows = []
    for n, (start, end) in enumerate(todo):
        mid = (Fraction(start) + Fraction(end)) / 2
        frame = frames_dir / f"{tag}_{n}.jpg"
        try:
            if render.render_frame(str(media_path), mid, str(frame), max_short_side=CAPTION_SHORT_SIDE) is None:
                continue
            rows.append({"start": Fraction(start), "end": Fraction(end),
                         "caption": caption(str(frame), model)})
        finally:
            try:
                frame.unlink()
            except FileNotFoundError:
                pass
        if progress is not None:
            progress(n + 1, len(todo))
    return rows
