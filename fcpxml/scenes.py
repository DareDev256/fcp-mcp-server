"""Shot-boundary detection.

Two backends, one answer shape. PySceneDetect (the ``scenes`` extra) is the
better detector — its content and adaptive modes handle fades and slow
pushes that a frame-difference threshold misses. Without it, ffmpeg's own
``select=gt(scene,T)`` filter runs instead, so a fresh install with nothing
but ffmpeg still gets real cuts rather than an import error. The result
names which backend produced it, because the two do not agree on hard
content and a reader must know which one they are looking at.

Cuts are source-time seconds. Callers map them into timeline time the same
way ``media_intel.map_silence_to_timeline`` maps silences.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCENE_TIMEOUT_SECONDS = 300
BACKENDS = ("auto", "content", "adaptive", "ffmpeg")
# ffmpeg's scene score is 0..1; PySceneDetect's content threshold is 0..255
# (27 by default). Each backend has its own default; a caller-supplied
# threshold is interpreted in the backend's own units. ffmpeg's score is
# coarse — a red->blue hard cut on solid frames reads exactly 0.4 and a
# red->green one reads 0.0 (measured, ffmpeg 7) — so it is a fallback, not
# the detector of record.
FFMPEG_DEFAULT_THRESHOLD = 0.3
CONTENT_DEFAULT_THRESHOLD = 27.0
ADAPTIVE_DEFAULT_THRESHOLD = 3.0
_PTS_RE = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def backends_available() -> dict[str, bool]:
    try:
        import scenedetect  # noqa: F401

        have_sd = True
    except ImportError:
        have_sd = False
    return {
        "content": have_sd,
        "adaptive": have_sd,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


def parse_showinfo(stderr: str) -> tuple[list[float], Optional[float]]:
    """Cut times from ``showinfo`` stderr, plus the container duration."""
    cuts = [float(m) for m in _PTS_RE.findall(stderr)]
    duration = None
    m = _DURATION_RE.search(stderr)
    if m:
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    return cuts, duration


def _scenes_from_cuts(cuts: list[float], duration: Optional[float], min_scene_len: float) -> list[tuple[float, float]]:
    kept: list[float] = []
    last = 0.0
    for c in sorted(cuts):
        if c <= 0.0:
            continue
        if c - last >= min_scene_len:
            kept.append(c)
            last = c
    bounds = [0.0, *kept]
    if duration is not None and duration > bounds[-1]:
        bounds.append(duration)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _ffmpeg_backend(path: str, threshold: float, min_scene_len: float) -> Optional[dict]:
    if not (0.0 < threshold <= 1.0):
        raise ValueError(f"ffmpeg scene threshold must be in (0, 1], got {threshold}")
    if shutil.which("ffmpeg") is None:
        return None
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-i", path,
                "-an", "-vf", f"select='gt(scene,{threshold})',showinfo",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=SCENE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("ffmpeg scene detection failed for %s", path)
        return None
    if result.returncode != 0:
        return None
    cuts, duration = parse_showinfo(result.stderr[-2_000_000:])
    scenes = _scenes_from_cuts(cuts, duration, min_scene_len)
    return {"backend": "ffmpeg", "cuts": [s for s, _ in scenes[1:]], "scenes": scenes,
            "duration": duration}


def _scenedetect_backend(path: str, mode: str, threshold: Optional[float], min_scene_len: float) -> Optional[dict]:
    try:
        from scenedetect import AdaptiveDetector, ContentDetector, detect
    except ImportError:
        return None
    if mode == "adaptive":
        detector = AdaptiveDetector(
            adaptive_threshold=threshold if threshold is not None else ADAPTIVE_DEFAULT_THRESHOLD,
            min_scene_len=min_scene_len,
        )
    else:
        detector = ContentDetector(
            threshold=threshold if threshold is not None else CONTENT_DEFAULT_THRESHOLD,
            min_scene_len=min_scene_len,
        )
    try:
        pairs = detect(path, detector, start_in_scene=True)
    except Exception:
        logger.warning("scenedetect (%s) failed for %s", mode, path)
        return None
    scenes = [(a.get_seconds(), b.get_seconds()) for a, b in pairs]
    duration = scenes[-1][1] if scenes else None
    return {"backend": mode, "cuts": [s for s, _ in scenes[1:]], "scenes": scenes,
            "duration": duration}


def detect_scenes(
    path: str, backend: str = "auto", threshold: Optional[float] = None,
    min_scene_len: float = 0.5,
) -> Optional[dict]:
    """Shot boundaries for a media file.

    Returns ``{"backend", "cuts": [s, ...], "scenes": [(start, end), ...],
    "duration"}`` in source seconds, or ``None`` when the file is missing or
    no backend can run. ``backend="auto"`` prefers PySceneDetect's content
    mode and falls back to ffmpeg.
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}, got {backend!r}")
    if not (0.0 < min_scene_len <= 3600):
        raise ValueError(f"min_scene_len must be between 0 and 3600 seconds, got {min_scene_len}")
    if not Path(path).is_file():
        return None
    if backend in ("content", "adaptive"):
        return _scenedetect_backend(path, backend, threshold, min_scene_len)
    if backend == "ffmpeg":
        return _ffmpeg_backend(path, threshold if threshold is not None else FFMPEG_DEFAULT_THRESHOLD,
                               min_scene_len)
    result = _scenedetect_backend(path, "content", threshold, min_scene_len)
    if result is not None:
        return result
    return _ffmpeg_backend(path, threshold if threshold is not None else FFMPEG_DEFAULT_THRESHOLD,
                           min_scene_len)
