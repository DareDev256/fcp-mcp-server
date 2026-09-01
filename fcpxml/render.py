"""Execute a compiled filtergraph and verify the artifact it produced.

The verification is the point. A rendered file existing is not evidence the
render is correct — ffmpeg exits 0 having written a near-empty container more
often than anyone expects, and a check that reads the same on a good and a bad
render certifies nothing. Every proxy therefore reads its own duration back
off the artifact and reports the drift against the timeline's exact rational.
"""

import logging
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

from fcpxml.filtergraph import compile_timeline, graph_to_args

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 60


def cache_dir() -> Path:
    """Private cache root for rendered previews.

    Mode 700 on both the root and this subdirectory: the cache holds frames of
    the operator's footage, which is their content and stays theirs.
    """
    root = Path(os.path.expanduser("~")) / ".fcp-mcp"
    directory = root / "preview"
    directory.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    directory.chmod(0o700)
    return directory


def probe_duration(path: str) -> Optional[Fraction]:
    """Actual duration of a media file, or None when it is unreadable."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    try:
        return Fraction(raw).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        # ffprobe prints "N/A" for a container it opened but could not measure.
        return None


def _failure(graph, skipped: list, error: str) -> dict:
    return {
        "path": None, "duration": None, "expected": graph.total, "drift": None,
        "substitutions": graph.substitutions, "skipped": skipped, "error": error,
    }


def render_proxy(
    timeline: Any, out_path: Optional[str] = None, height: int = 480
) -> dict:
    """Render a low-resolution proxy of *timeline* and verify its duration.

    Never raises on a missing tool or missing media. Returns a dict whose
    ``path`` is None and whose ``error`` names exactly what is absent, because
    an operator who cannot preview needs to know which one thing to install.
    """
    graph = compile_timeline(timeline)
    skipped = [s.label for s in graph.segments if s.missing]

    if shutil.which("ffmpeg") is None:
        return _failure(
            graph, skipped,
            "ffmpeg is not on PATH. Install it (brew install ffmpeg) to render "
            "previews; every other tool is unaffected.",
        )

    if out_path is None:
        name = str(getattr(timeline, "name", "timeline")).replace("/", "_")
        out_path = str(cache_dir() / f"{name}_proxy.mp4")

    try:
        args = graph_to_args(graph, out_path, height=height)
    except ValueError as exc:
        return _failure(graph, skipped, str(exc))

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=RENDER_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("ffmpeg proxy render failed for %s", out_path)
        return _failure(
            graph, skipped,
            f"ffmpeg did not finish within {RENDER_TIMEOUT_SECONDS}s.",
        )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        return _failure(graph, skipped, "ffmpeg failed: " + " / ".join(tail))

    duration = probe_duration(out_path)
    return {
        "path": out_path,
        "duration": duration,
        "expected": graph.total,
        "drift": None if duration is None else duration - graph.total,
        "substitutions": graph.substitutions,
        "skipped": skipped,
        "error": None,
    }


def render_frame(source: str, at: Fraction, out_path: str) -> Optional[str]:
    """Extract a single frame from *source* at *at* seconds."""
    if shutil.which("ffmpeg") is None or not Path(source).is_file():
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y",
             "-ss", str(float(at)), "-i", str(source),
             "-frames:v", "1", str(out_path)],
            capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return str(out_path) if result.returncode == 0 and Path(out_path).is_file() else None
