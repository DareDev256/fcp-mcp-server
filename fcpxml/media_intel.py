"""Media intelligence — real analysis of source media referenced by timelines.

v0.10 slice 1: audio silence detection via ffmpeg's silencedetect filter.
No new Python dependencies: ffmpeg is invoked as a bounded subprocess
(list-form arguments, validated numeric parameters, hard timeout), and
detection degrades gracefully — ``detect_silence`` returns ``None`` when
ffmpeg is unavailable or the file cannot be analyzed, so callers can fall
back or report instead of crashing.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Hard ceiling on a single ffmpeg analysis pass. Decoding audio-only is far
# faster than realtime, so this covers multi-hour media while still bounding
# an adversarial/corrupt file that makes the decoder hang.
PROBE_TIMEOUT_SECONDS = 120

# silencedetect prints times as plain seconds on stderr; starts can be
# slightly negative (encoder priming samples), so allow a leading minus.
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def parse_silencedetect_output(
    stderr: str, total_duration: Optional[float] = None
) -> List[Tuple[float, float]]:
    """Parse ffmpeg silencedetect stderr into (start, end) ranges in seconds.

    A trailing ``silence_start`` with no matching ``silence_end`` (media that
    ends silent) is closed at ``total_duration`` when known, otherwise dropped.
    """
    ranges: List[Tuple[float, float]] = []
    pending: Optional[float] = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = max(0.0, float(start_match.group(1)))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            ranges.append((pending, float(end_match.group(1))))
            pending = None
    if pending is not None and total_duration is not None and total_duration > pending:
        ranges.append((pending, total_duration))
    return ranges


def map_silence_to_timeline(
    silences: List[Tuple[float, float]],
    source_start: float,
    clip_duration: float,
    timeline_offset: float,
) -> List[Tuple[float, float]]:
    """Map source-time silence ranges onto the timeline.

    A clip uses ``[source_start, source_start + clip_duration)`` of its source
    media and sits at ``timeline_offset``. Ranges outside the used window are
    excluded; ranges overlapping its edges are clamped.
    """
    source_end = source_start + clip_duration
    mapped: List[Tuple[float, float]] = []
    for start, end in silences:
        clamped_start = max(start, source_start)
        clamped_end = min(end, source_end)
        if clamped_end <= clamped_start:
            continue
        mapped.append((
            timeline_offset + (clamped_start - source_start),
            timeline_offset + (clamped_end - source_start),
        ))
    return mapped


def detect_beats(
    path: str, max_analysis_seconds: float = 1200.0
) -> Optional[dict]:
    """Detect musical beats in an audio file via librosa's beat tracker.

    librosa is an optional dependency (``pip install 'fcp-mcp-server[intelligence]'``);
    without it, or when the file is missing/unreadable, this returns ``None``
    so callers can degrade to a helpful message instead of crashing.

    Returns:
        ``{'bpm': float, 'beats': [seconds, ...]}`` or ``None``.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        import librosa
    except ImportError:
        logger.info("librosa not installed; beat detection unavailable")
        return None
    try:
        # duration cap bounds memory on adversarially long media
        y, sr = librosa.load(str(file_path), sr=None, mono=True,
                             duration=max_analysis_seconds)
        tempo, frames = librosa.beat.beat_track(y=y, sr=sr)
        beats = librosa.frames_to_time(frames, sr=sr)
    except Exception:
        logger.warning("librosa beat analysis failed for %s", file_path)
        return None
    bpm = float(tempo[0] if hasattr(tempo, "__len__") else tempo)
    return {"bpm": bpm, "beats": [float(b) for b in beats]}


def media_src_to_path(src: str) -> str:
    """Convert an FCPXML media src (``file://`` URL or plain path) to a filesystem path."""
    if src.startswith("file://"):
        from urllib.parse import unquote, urlparse

        return unquote(urlparse(src).path)
    return src


def _parse_total_duration(stderr: str) -> Optional[float]:
    match = _DURATION_RE.search(stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def detect_silence(
    path: str, noise_db: float = -30.0, min_duration: float = 0.5
) -> Optional[List[Tuple[float, float]]]:
    """Detect silence in an audio/video file's first audio stream.

    Returns (start, end) ranges in source seconds, or ``None`` when the file
    is missing, ffmpeg is unavailable, or analysis fails. Raises ``ValueError``
    on out-of-bounds parameters (they end up in a subprocess argument, so they
    are validated, not trusted).
    """
    if not (-120.0 <= noise_db <= 0.0):
        raise ValueError(f"noise_db must be between -120 and 0 dB, got {noise_db}")
    if not (0 < min_duration <= 3600):
        raise ValueError(f"min_duration must be between 0 and 3600 seconds, got {min_duration}")

    file_path = Path(path)
    if not file_path.is_file():
        return None
    if shutil.which("ffmpeg") is None:
        logger.info("ffmpeg not found on PATH; silence detection unavailable")
        return None

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin",
                "-i", str(file_path),
                # -vn: silence detection only needs the audio stream. Without it
                # ffmpeg decodes the full video track into the null muxer, which
                # blows past PROBE_TIMEOUT_SECONDS on long/high-bitrate files.
                "-vn",
                "-af", f"silencedetect=noise={float(noise_db)}dB:d={float(min_duration)}",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("ffmpeg silence analysis failed for %s", file_path)
        return None
    if result.returncode != 0:
        return None
    return parse_silencedetect_output(
        result.stderr, total_duration=_parse_total_duration(result.stderr)
    )
