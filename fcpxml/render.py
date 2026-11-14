"""Execute a compiled filtergraph and verify the artifact it produced.

The verification is the point. A rendered file existing is not evidence the
render is correct — ffmpeg exits 0 having written a near-empty container more
often than anyone expects, and a check that reads the same on a good and a bad
render certifies nothing. Every proxy therefore reads its own duration back
off the artifact and reports the drift against the timeline's exact rational.
"""

import json
import logging
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

from fcpxml.filtergraph import (
    FPS,
    SourceInfo,
    canvas_size,
    compile_timeline,
    graph_to_args,
    placement,
)

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 60

# A proxy is "the timeline's length" when it is within one proxy frame of it
# (plus a hair for the container's own rounding). Anything further is a
# render that is not the timeline, and is reported as UNVERIFIED.
DURATION_TOLERANCE = Fraction(1, FPS) + Fraction(1, 1000)

# Lane clips sampled per render to prove they were drawn. Two frame grabs
# each; beyond this many the report says how many went unchecked.
MAX_LANE_CHECKS = 12

# Mean absolute difference, 0-255 on an 8-bit grey thumbnail, below which a
# region of the render is taken to BE the lane clip. Compression noise on a
# 24x24 grey thumbnail of a crf-28 proxy measures under 6; a different
# picture measures in the tens.
LANE_MATCH_THRESHOLD = 16
_THUMB = 24
# The proxy's default height since 0.32 (1280x720 at 16:9): a frame of it is
# what a model or a person reads names and timecodes off.
DEFAULT_HEIGHT = 720

# ffprobe format names for single images.
_IMAGE_FORMATS = ("image2", "png_pipe", "jpeg_pipe", "tiff_pipe", "webp_pipe",
                  "bmp_pipe", "psd_pipe", "exr_pipe", "dpx_pipe", "gif", "heif")


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


def probe_source(path: str) -> Optional[SourceInfo]:
    """What the file behind a clip really is: length, size, still or not.

    The FCPXML's own claims are not enough to cut from. A still's asset says
    nothing about being a still beyond a zero duration, a relinked file can
    be shorter than the asset says, and an audio-only file has no picture for
    a ``[n:v]`` to bind to, which fails the whole graph.
    """
    if shutil.which("ffprobe") is None or not Path(path).is_file():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_type,width,height,nb_frames",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not data:
        return None
    video = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"), None)
    audio = any(st.get("codec_type") == "audio" for st in data.get("streams", []))
    form = data.get("format", {})
    names = set((form.get("format_name") or "").split(","))
    still = bool(video) and (
        bool(names & set(_IMAGE_FORMATS)) or str(video.get("nb_frames", "")) == "1"
    )
    try:
        duration = Fraction(form["duration"]).limit_denominator(100000)
    except (KeyError, ValueError, ZeroDivisionError):
        duration = None
    return SourceInfo(
        duration=None if still else duration,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        still=still,
        has_video=video is not None,
        has_audio=audio,
    )


def stream_facts(path: str) -> Optional[dict]:
    """A media file's picture and sound as diagnose and inspect report them:
    {"fps": Fraction or None, "width", "height", "channels", "sample_rate",
    "duration": Fraction or None}. None when the file or ffprobe is absent."""
    if shutil.which("ffprobe") is None or not Path(path).is_file():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,r_frame_rate,sample_rate,channels",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not data:
        return None
    video = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"), None)
    audio = next((st for st in data.get("streams", []) if st.get("codec_type") == "audio"), None)

    def rate(text):
        try:
            value = Fraction(str(text))
            return value if value > 0 else None
        except (ValueError, ZeroDivisionError):
            return None

    try:
        duration = Fraction(data.get("format", {})["duration"]).limit_denominator(100000)
    except (KeyError, ValueError, ZeroDivisionError):
        duration = None
    return {
        "fps": rate(video.get("r_frame_rate")) if video else None,
        "width": int(video["width"]) if video and video.get("width") else None,
        "height": int(video["height"]) if video and video.get("height") else None,
        "channels": int(audio["channels"]) if audio and audio.get("channels") else 0,
        "sample_rate": int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        "duration": duration,
    }


def probe_audio_duration(path: str) -> Optional[Fraction]:
    """Length of the first audio stream of *path*, None when it has none."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = (result.stdout or "").strip().splitlines()
    try:
        return Fraction(raw[0]).limit_denominator(100000) if result.returncode == 0 and raw else None
    except (ValueError, ZeroDivisionError):
        return None


def _probes(timeline: Any) -> dict:
    """One probe per distinct source file on the timeline."""
    seen: dict = {}
    items = list(getattr(timeline, "clips", None) or []) + list(
        getattr(timeline, "connected_clips", None) or []
    )
    from fcpxml.media_intel import media_src_to_path

    for item in items:
        path = media_src_to_path(getattr(item, "media_path", "") or "")
        if path and path not in seen:
            info = probe_source(path)
            if info is not None:
                seen[path] = info
    return seen


def _grey(source: str, at: Fraction, vf: str) -> Optional[bytes]:
    """A tiny grey thumbnail of one frame, as raw bytes (None if unreadable)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
             "-ss", f"{float(at):.6f}", "-i", str(source), "-frames:v", "1",
             "-vf", f"{vf},scale={_THUMB}:{_THUMB},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = result.stdout or b""
    return data if result.returncode == 0 and len(data) == _THUMB * _THUMB else None


def verify_lanes(graph, out_path: str, height: int) -> list:
    """Look at the render and say, per lane clip, whether it is really there.

    For each drawn lane clip, the frame at the middle of its window is cut
    out of the render at the clip's own placement and compared with the same
    instant of its source, scaled the same way. "Drawn" in the log is then a
    measurement, not a claim: before 0.28 a clip placed where it never
    appeared was reported drawn while the spine showed instead.

    Returns ``(label, verdict, detail)`` rows; verdict is True (seen), False
    (NOT seen) or None (not checked: covered by a higher lane, off-canvas,
    or over the check budget).
    """
    canvas_w, canvas_h = canvas_size(graph, height)
    rows = []
    lanes = list(graph.lanes)
    for number, piece in enumerate(lanes):
        segment = graph.segments[piece.segment]
        label = f"lane {segment.lane} clip {segment.label!r}"
        if number >= MAX_LANE_CHECKS:
            rows.append((label, None, "over the per-render check budget"))
            continue
        frame = piece.start + piece.frames // 2
        width, height_px, x, y = placement(graph, segment, height)
        left, top = max(0, x), max(0, y)
        right, bottom = min(canvas_w, x + width), min(canvas_h, y + height_px)
        if right - left < 8 or bottom - top < 8:
            rows.append((label, None, "it sits off the canvas"))
            continue
        covered = any(
            other.lane > piece.lane and other.start <= frame < other.start + other.frames
            for other in lanes
        )
        if covered:
            rows.append((label, None, "a higher lane covers it at the sampled frame"))
            continue
        at = Fraction(frame, FPS) + Fraction(1, 2 * FPS)
        rendered = _grey(out_path, at, f"crop={right - left}:{bottom - top}:{left}:{top}")
        into = Fraction(frame - piece.start, FPS)
        source_at = Fraction(0) if piece.still else max(
            piece.read_in, piece.read_in + into - piece.hold_head
        )
        # The expected frame takes the render's own pixel route (scaled, then
        # yuv420p, then grey), so the comparison measures whether the clip is
        # THERE, not how two colour conversions differ. (On ffmpeg 6.1 this
        # check reported a 10-bit ProRes lane NOT drawn at 27/255, and it was
        # right: the frame was black. See filtergraph.lane_tracks.)
        expected = _grey(
            piece.source, source_at,
            f"scale={width}:{height_px},setsar=1,format=yuv420p,"
            f"crop={right - left}:{bottom - top}:{left - x}:{top - y}",
        )
        if rendered is None or expected is None:
            rows.append((label, None, "a frame could not be read back"))
            continue
        difference = sum(abs(a - b) for a, b in zip(rendered, expected)) / len(rendered)
        rows.append((
            label, difference <= LANE_MATCH_THRESHOLD,
            f"at {float(at):.2f}s, difference {difference:.0f}/255",
        ))
    return rows


def _failure(graph, skipped: list, error: str) -> dict:
    return {
        "path": None, "duration": None, "expected": graph.total, "drift": None,
        "substitutions": graph.substitutions, "transitions": graph.transitions,
        "skipped": skipped, "error": error, "verified": False, "lanes": [],
    }


def render_proxy(
    timeline: Any, out_path: Optional[str] = None, height: int = DEFAULT_HEIGHT,
    burn_in: bool = False,
) -> dict:
    """Render a low-resolution proxy of *timeline* and verify it.

    Never raises on a missing tool or missing media. Returns a dict whose
    ``path`` is None and whose ``error`` names exactly what is absent, because
    an operator who cannot preview needs to know which one thing to install.

    One bad clip does not cost the whole preview. Each source is probed
    first, so a clip with no picture where the timeline asks for one becomes
    a named black substitute before ffmpeg ever sees it. If ffmpeg still
    refuses the graph, it is rebuilt with transitions as hard cuts, then
    without connected clips, and the report says which fallback was taken.

    ``verified`` is True only when the artifact's own duration is within one
    proxy frame of the timeline and every lane clip that could be checked was
    seen in it.

    ``burn_in`` writes a SECOND file beside it with the timeline timecode,
    the clip's name and a badge on each lane clip drawn over the picture
    (fcpxml/proofs.py). It is composited AFTER verify_lanes() has read the
    clean render, so the instrument measures the timeline's pixels, never
    our labels; burning first makes it fail (tests/test_proofs.py). A poster
    frame (the middle of the timeline) is written either way.
    """
    probes = _probes(timeline) if shutil.which("ffprobe") else {}
    graph = compile_timeline(timeline, probes)
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

    attempts = [dict(with_transitions=True, with_lanes=True, with_audio=True)]
    if graph.audio:
        attempts.append(dict(with_transitions=True, with_lanes=True, with_audio=False))
    if graph.transitions:
        attempts.append(dict(with_transitions=False, with_lanes=True, with_audio=False))
    if graph.lanes:
        attempts.append(dict(with_transitions=False, with_lanes=False, with_audio=False))
    errors: list = []
    result = None
    for options in attempts:
        if errors:
            graph = compile_timeline(timeline, probes, **options)
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
        if result.returncode == 0:
            break
        tail = (result.stderr or "").strip().splitlines()[-3:]
        errors.append(" / ".join(tail))
    if result is None or result.returncode != 0:
        return _failure(graph, skipped, "ffmpeg failed: " + (errors[0] if errors else "no output"))

    duration = probe_duration(out_path)
    if duration is None:
        # ffmpeg exited 0 and wrote a container nothing can measure. That is
        # not a preview, and must not open the review gate as one.
        return _failure(
            graph, skipped,
            f"ffmpeg exited cleanly but {out_path} has no readable video "
            f"(nothing was rendered).",
        )
    drift = duration - graph.total
    lanes = verify_lanes(graph, out_path, height) if graph.lanes else []
    substitutions = list(graph.substitutions)
    heard = True
    if graph.audio:
        audio_length = probe_audio_duration(out_path)
        heard = audio_length is not None and abs(audio_length - graph.total) <= DURATION_TOLERANCE
        if not heard:
            substitutions.append(
                "audio was mixed but the file's audio track is "
                + ("missing" if audio_length is None else f"{float(audio_length):.3f}s long")
            )
    if errors:
        substitutions.insert(0, f"ffmpeg refused the full graph ({errors[0]}); rendered the fallback below")
    burned = burn_error = None
    if burn_in:
        from fcpxml import proofs

        burned = str(Path(out_path).with_name(Path(out_path).stem + "_burnin.mp4"))
        burn_error = proofs.burn_in(graph, out_path, burned, height, getattr(timeline, "frame_rate", 24) or 24)
        if burn_error:
            burned = None
    poster = str(Path(out_path).with_name(Path(out_path).stem + "_poster.png"))
    poster = render_frame(burned or out_path, graph.total / 2, poster)
    canvas = canvas_size(graph, height)
    return {
        "path": out_path,
        "duration": duration,
        "expected": graph.total,
        "drift": drift,
        "substitutions": tuple(substitutions),
        "transitions": graph.transitions,
        "skipped": skipped,
        "error": None,
        "verified": abs(drift) <= DURATION_TOLERANCE and heard and all(v is not False for _, v, _ in lanes),
        "lanes": lanes,
        "burned": burned,
        "burn_error": burn_error,
        "poster": poster,
        "size": f"{canvas[0]}x{canvas[1]}",
    }


def frame_scale_filter(max_short_side: int) -> str:
    """An ffmpeg scale filter that caps the SHORT edge at *max_short_side*.

    The "1080p" convention: 3840x2160 becomes 1920x1080 and a 2160x3840 phone
    clip 1080x1920, aspect preserved either way. ``min`` keeps a source already
    smaller than the cap at its own size — this never upscales — and ``-2``
    keeps the long edge even for the encoder. Only the short edge is bounded:
    an extreme panorama such as 7680x1080 passes through untouched.
    """
    cap = int(max_short_side)
    # Bounded for the same reason graph_to_args bounds its height: a zero or
    # negative cap makes ffmpeg exit non-zero, render_frame turns that into
    # None, and caption_shots turns THAT into a silently skipped shot. A bad
    # constant would read as "this clip had nothing to caption".
    if not 1 <= cap <= 8192:
        raise ValueError(f"max_short_side must be between 1 and 8192, got {cap}")
    return (
        f"scale=w='if(gt(iw,ih),-2,min(iw,{cap}))'"
        f":h='if(gt(iw,ih),min(ih,{cap}),-2)'"
    )


def render_frame(
    source: str, at: Fraction, out_path: str, *, max_short_side: Optional[int] = None
) -> Optional[str]:
    """Extract a single frame from *source* at *at* seconds.

    Full source resolution by default. ``max_short_side`` caps the frame's
    shorter edge (see :func:`frame_scale_filter`) for consumers that only need
    to look at the picture, not measure it — a vision model captions a 1080p
    frame in a fraction of the time it takes on a 4K one, and says the same
    thing.
    """
    if shutil.which("ffmpeg") is None or not Path(source).is_file():
        return None
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-ss", str(float(at)), "-i", str(source)]
    if max_short_side is not None:
        args += ["-vf", frame_scale_filter(max_short_side)]
    args += ["-frames:v", "1", str(out_path)]
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return str(out_path) if result.returncode == 0 and Path(out_path).is_file() else None
