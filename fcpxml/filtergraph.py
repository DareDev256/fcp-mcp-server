"""Compile a parsed Timeline into an ffmpeg graph description.

Pure — no subprocess, no filesystem access beyond an existence check. The
execution half lives in fcpxml/render.py so this can be asserted exactly, on
any machine, with or without ffmpeg installed.

Time is carried as fractions.Fraction end to end. A preview built on float
seconds drifts against the timeline it claims to represent; at 23.976 the
drift is visible within a minute, which would make the preview lie about the
one thing it exists to show.
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from fcpxml.media_intel import media_src_to_path


@dataclass(frozen=True)
class Segment:
    """One piece of source media placed on the timeline."""

    source: str
    src_in: Fraction
    src_out: Fraction
    tl_in: Fraction
    lane: int
    label: str
    missing: bool = False

    @property
    def duration(self) -> Fraction:
        return self.src_out - self.src_in


@dataclass(frozen=True)
class FilterGraph:
    """A renderable description of a timeline, plus what we could not honour."""

    segments: tuple[Segment, ...]
    total: Fraction
    substitutions: tuple[str, ...]


def _seconds(tc: Any) -> Fraction:
    """Exact seconds for a Timecode, never a float.

    Timecode.\\_exact_seconds is already a Fraction over the exact frame rate,
    which is the whole reason NTSC rates survive this trip.
    """
    if tc is None:
        return Fraction(0)
    exact = getattr(tc, "_exact_seconds", None)
    if exact is not None:
        return Fraction(exact)
    return Fraction(tc.seconds).limit_denominator(1000000)


def _resolve(media_path: str) -> tuple[str, bool]:
    """Return (filesystem path, missing?) for a clip's media reference."""
    if not media_path:
        return "", True
    path = media_src_to_path(media_path)
    return path, not Path(path).is_file()


def _segment_from(item: Any, lane: int) -> Segment:
    """Build a Segment from a Clip or a ConnectedClip.

    The two models disagree about where timeline position lives. A spine Clip
    carries it on ``start``; a ConnectedClip carries it on ``offset`` and
    reuses ``start`` for the source in-point. Reading ``start`` for both would
    place every lane clip at its source timecode instead of its timeline
    position, which is silently wrong rather than visibly broken.
    """
    duration = _seconds(item.duration)
    src_in = _seconds(getattr(item, "source_start", None))
    source, missing = _resolve(getattr(item, "media_path", "") or "")
    offset = getattr(item, "offset", None)
    tl_in = _seconds(offset) if offset is not None else _seconds(item.start)
    return Segment(
        source=source,
        src_in=src_in,
        src_out=src_in + duration,
        tl_in=tl_in,
        lane=lane,
        label=item.name,
        missing=missing,
    )


def compile_timeline(timeline: Any) -> FilterGraph:
    """Build a FilterGraph from a parsed Timeline.

    Spine clips are ordered by timeline position and carry lane 0. Connected
    clips keep their own lane: positive is video above the spine, negative is
    audio below.

    Transitions are NOT compiled in this version. Every one is recorded as a
    substitution so the operator is told the preview shows a hard cut where
    their timeline has a dissolve. A silent substitution would make the
    preview lie, which is worse than having no preview at all.
    """
    spine = sorted(getattr(timeline, "clips", None) or [], key=lambda c: _seconds(c.start))
    segments: list[Segment] = [_segment_from(clip, 0) for clip in spine]

    for connected in getattr(timeline, "connected_clips", None) or []:
        segments.append(_segment_from(connected, connected.lane))

    substitutions = tuple(
        f"{transition.name!r} at {float(_seconds(transition.start)):.2f}s rendered "
        f"as a hard cut (crossfade compilation is not implemented in this version)"
        for transition in (getattr(timeline, "transitions", None) or [])
    )

    total = sum((s.duration for s in segments if s.lane == 0), Fraction(0))
    return FilterGraph(segments=tuple(segments), total=total, substitutions=substitutions)


def graph_to_args(graph: FilterGraph, out_path: str, height: int = 480) -> list[str]:
    """Build the ffmpeg argument list that renders *graph* to *out_path*.

    An argument list, never a shell string: no user-supplied value is ever
    interpreted by a shell.

    This version draws the spine only. Lane compositing needs an overlay chain
    and is out of scope; lanes are still compiled into the graph, and therefore
    still reported, they are simply not drawn yet.
    """
    if not 1 <= height <= 2160:
        raise ValueError(f"height must be between 1 and 2160, got {height}")

    spine = [s for s in graph.segments if s.lane == 0 and not s.missing]
    if not spine:
        raise ValueError("nothing renderable: every spine clip is missing its media")

    args: list[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for seg in spine:
        # -ss before -i is the fast seek; -t bounds the read so a two-second
        # cut out of an hour-long source costs two seconds of decode.
        args += [
            "-ss", str(float(seg.src_in)),
            "-t", str(float(seg.duration)),
            "-i", seg.source,
        ]

    chains = [
        f"[{index}:v]scale=-2:{height},setsar=1,fps=24[v{index}]"
        for index in range(len(spine))
    ]
    concat_inputs = "".join(f"[v{i}]" for i in range(len(spine)))
    chains.append(f"{concat_inputs}concat=n={len(spine)}:v=1:a=0[vout]")

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    return args
