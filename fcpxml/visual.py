"""Filmstrip + waveform over a range of SOURCE media.

fcpxml/preview.py renders coloured blocks from the XML: a picture of what we
wrote, not of what the media looks like. That meant an edit was verified by
re-parsing our own output, which is an instrument that cannot see the failure
it is checking for — a fixed flash frame and an unfixed one read identically.

This module reads the media, so a fix is confirmable from an image. The idea
is borrowed from browser-use/video-use, which renders a filmstrip and waveform
at every cut boundary and looks at it before showing a user anything.
"""

import logging
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VISUAL_TIMEOUT_SECONDS = 180


def visual_check(
    source: str,
    start: Fraction,
    end: Fraction,
    out_path: str,
    frames: int = 8,
    width: int = 1200,
) -> Optional[str]:
    """Render a filmstrip above an audio waveform for ``source[start:end]``.

    Returns the written path, or None when ffmpeg or the media is absent.
    Raises ValueError only on caller error — a reversed or absurd range — because
    those values end up in a subprocess argument and are therefore not trusted.
    """
    if end <= start:
        raise ValueError(f"end must be after start (got {start} -> {end})")
    if not 1 <= frames <= 64:
        raise ValueError(f"frames must be between 1 and 64, got {frames}")
    if not 64 <= width <= 4096:
        raise ValueError(f"width must be between 64 and 4096, got {width}")

    if shutil.which("ffmpeg") is None:
        logger.info("ffmpeg not found on PATH; visual check unavailable")
        return None
    if not Path(source).is_file():
        return None

    duration = end - start
    cell = max(16, width // frames)
    strip_height = max(16, int(cell * 9 / 16))
    # One sampled frame every duration/frames seconds, tiled into a single row.
    rate = f"{frames}/{float(duration)}"

    strip_width = cell * frames
    # showwavespic draws on a TRANSPARENT background, which flattens to white in
    # a PNG. A white waveform on it is invisible, and every automated check
    # still passes: the file exists, it is a valid PNG, and two ranges still
    # differ because the filmstrip above differs. Composite over a dark plate
    # so the trace is actually legible, and see
    # test_the_waveform_is_actually_drawn for the check that can see this fail.
    with_waveform = (
        f"[0:v]fps={rate},scale={cell}:{strip_height},tile={frames}x1[strip];"
        f"color=c=0x101014:s={strip_width}x{strip_height}[plate];"
        f"[0:a]showwavespic=s={strip_width}x{strip_height}:colors=0x00E5FF[trace];"
        f"[plate][trace]overlay=format=auto[wave];"
        f"[strip][wave]vstack=inputs=2[out]"
    )
    video_only = f"[0:v]fps={rate},scale={cell}:{strip_height},tile={frames}x1[out]"

    # The first graph references [0:a]. A video-only source has no audio stream,
    # so the whole graph fails — and reporting "no image" for a perfectly good
    # silent clip would be the tool lying about the media rather than about
    # itself. Retry without the waveform instead.
    for graph in (with_waveform, video_only):
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-y",
                 "-ss", str(float(start)), "-t", str(float(duration)),
                 "-i", str(source),
                 "-filter_complex", graph,
                 "-map", "[out]", "-frames:v", "1", str(out_path)],
                capture_output=True, timeout=VISUAL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("visual check failed for %s", source)
            return None
        if result.returncode == 0 and Path(out_path).is_file():
            return str(out_path)
    return None
