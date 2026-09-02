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
class CompiledTransition:
    """A timeline transition resolved onto a spine boundary.

    ``boundary`` is the index of the spine segment the transition follows: it
    joins ``spine[boundary]`` to ``spine[boundary + 1]``. ``kind`` is an
    ffmpeg xfade name, already checked against the supported set.
    """

    boundary: int
    kind: str
    duration: Fraction
    name: str


@dataclass(frozen=True)
class FilterGraph:
    """A renderable description of a timeline, plus what we could not honour."""

    segments: tuple[Segment, ...]
    total: Fraction
    substitutions: tuple[str, ...]
    transitions: tuple[CompiledTransition, ...] = ()


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


# Final Cut's transition names and types, mapped onto the ffmpeg xfade
# transitions that actually look like them. A name that is not here still
# renders — as a plain dissolve — but says so, because a wipe silently shown
# as a dissolve is the preview lying about the cut it exists to show.
_XFADE_KINDS = {
    "cross dissolve": "fade",
    "cross-dissolve": "fade",
    "dissolve": "fade",
    "fade to color": "fadeblack",
    "fade from color": "fadeblack",
    "dip to color": "fadeblack",
    "wipe": "wipeleft",
    "slide": "slideleft",
}


def _xfade_kind(transition: Any) -> tuple[str, bool]:
    """Return (xfade transition name, was it recognised?)."""
    for field in (
        getattr(transition, "transition_type", "") or "",
        getattr(transition, "name", "") or "",
    ):
        kind = _XFADE_KINDS.get(field.strip().lower())
        if kind is not None:
            return kind, True
    return "fade", False


def _place_transitions(
    transitions: list[Any], spine: list[Segment]
) -> tuple[list[CompiledTransition], list[str]]:
    """Resolve each transition onto a spine boundary it can actually run on.

    A transition is compiled when it sits within its own duration of a
    boundary between two present, non-empty segments, that boundary is still
    free, and its length fits inside both neighbours. Everything else is
    returned as a substitution string rather than dropped, because the
    operator has to be told the preview shows a hard cut where the timeline
    has a dissolve.
    """
    boundaries = [spine[index + 1].tl_in for index in range(len(spine) - 1)]
    compiled: list[CompiledTransition] = []
    taken: set[int] = set()
    notes: list[str] = []

    for transition in transitions:
        at = _seconds(getattr(transition, "start", None))
        length = _seconds(getattr(transition, "duration", None))
        name = getattr(transition, "name", "") or "transition"
        kind, recognised = _xfade_kind(transition)

        def note(why: str) -> None:
            notes.append(
                f"{name!r} at {float(at):.2f}s rendered as a hard cut ({why})"
            )

        if length <= 0:
            note("it has no duration")
            continue
        if not boundaries:
            note("the spine has no cut to place it on")
            continue

        index = min(range(len(boundaries)), key=lambda i: abs(boundaries[i] - at))
        if abs(boundaries[index] - at) > length:
            note(f"no spine cut within {float(length):.2f}s of it")
            continue
        if index in taken:
            note("another transition already occupies that cut")
            continue
        before, after = spine[index], spine[index + 1]
        if before.missing or after.missing:
            note("a clip on one side of it is missing its media")
            continue
        fits = min(length, before.duration, after.duration)
        if fits <= 0:
            note("a clip on one side of it is empty")
            continue
        if fits < length:
            notes.append(
                f"{name!r} at {float(at):.2f}s shortened to "
                f"{float(fits):.2f}s to fit its neighbours"
            )
        if not recognised:
            notes.append(
                f"{name!r} at {float(at):.2f}s rendered as a dissolve "
                f"(no xfade equivalent for this transition)"
            )
        taken.add(index)
        compiled.append(
            CompiledTransition(boundary=index, kind=kind, duration=fits, name=name)
        )

    compiled.sort(key=lambda c: c.boundary)
    return compiled, notes


def compile_timeline(timeline: Any) -> FilterGraph:
    """Build a FilterGraph from a parsed Timeline.

    Spine clips are ordered by timeline position and carry lane 0. Connected
    clips keep their own lane: positive is video above the spine, negative is
    audio below.

    Transitions are compiled onto the spine boundaries they straddle and
    rendered with ffmpeg's xfade. One that cannot be placed there — no cut
    near it, media missing on one side, a boundary already spoken for — is
    recorded as a substitution instead, so the operator is told the preview
    shows a hard cut where their timeline has a dissolve. A silent
    substitution would make the preview lie, which is worse than having no
    preview at all.
    """
    clips = sorted(getattr(timeline, "clips", None) or [], key=lambda c: _seconds(c.start))
    spine: list[Segment] = [_segment_from(clip, 0) for clip in clips]
    segments: list[Segment] = list(spine)

    for connected in getattr(timeline, "connected_clips", None) or []:
        segments.append(_segment_from(connected, connected.lane))

    compiled, notes = _place_transitions(
        list(getattr(timeline, "transitions", None) or []), spine
    )

    # An xfade overlaps its two neighbours, so the render is shorter than the
    # sum of the cuts by exactly the compiled transition lengths. total is
    # what render.py reads the artifact back against; it has to say so.
    for lane_segment in (s for s in segments if s.lane > 0):
        if lane_segment.missing:
            notes.append(
                f"lane {lane_segment.lane} clip {lane_segment.label!r} not drawn "
                f"(its media is missing)"
            )
        else:
            notes.append(
                f"lane {lane_segment.lane} clip {lane_segment.label!r} drawn "
                f"full-frame (transform, scale, crop and opacity are not read)"
            )
    if any(s.lane < 0 for s in segments):
        notes.append("audio lanes are not mixed: this preview is video only")

    total = sum((s.duration for s in spine), Fraction(0)) - sum(
        (c.duration for c in compiled), Fraction(0)
    )
    return FilterGraph(
        segments=tuple(segments),
        total=total,
        substitutions=tuple(notes),
        transitions=tuple(compiled),
    )


def _output_seconds(graph: FilterGraph, timeline_seconds: Fraction) -> Fraction:
    """Where a timeline instant lands in the rendered file.

    Every compiled crossfade overlaps its two neighbours, so the render is
    shorter than the timeline by that much from the cut onwards. An overlay
    placed at its raw timeline time would drift later and later behind the
    spine it is supposed to sit on.
    """
    spine = [s for s in graph.segments if s.lane == 0]
    shift = Fraction(0)
    for transition in graph.transitions:
        if transition.boundary + 1 >= len(spine):
            continue
        if timeline_seconds >= spine[transition.boundary + 1].tl_in:
            shift += transition.duration
    return max(Fraction(0), timeline_seconds - shift)


def graph_to_args(graph: FilterGraph, out_path: str, height: int = 480) -> list[str]:
    """Build the ffmpeg argument list that renders *graph* to *out_path*.

    An argument list, never a shell string: no user-supplied value is ever
    interpreted by a shell.

    Compiled transitions become xfade links between adjacent spine segments;
    every other join is a straight concat. A transition whose neighbour was
    dropped for missing media is skipped here — compile_timeline has already
    reported it.

    Video lanes are composited over the spine with an overlay chain, each one
    enabled only for the window it occupies and shifted by any crossfade that
    shortened the timeline before it. They are drawn full-frame: this module
    does not read transforms, so a picture-in-picture previews as a full-frame
    cutaway, and compile_timeline says so. Audio lanes are not mixed — the
    whole graph is video only.
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

    # Spine indices shift when missing media is dropped, so a transition is
    # only applied when BOTH of the segments it joined survived the filter.
    kept = {
        original: filtered
        for filtered, original in enumerate(
            index for index, seg in enumerate(graph.segments)
            if seg.lane == 0 and not seg.missing
        )
    }
    links: dict[int, CompiledTransition] = {}
    for transition in graph.transitions:
        left, right = kept.get(transition.boundary), kept.get(transition.boundary + 1)
        if left is not None and right == left + 1:
            links[left] = transition

    if not links:
        concat_inputs = "".join(f"[v{i}]" for i in range(len(spine)))
        chains.append(f"{concat_inputs}concat=n={len(spine)}:v=1:a=0[vout]")
    else:
        # Fold left to right, carrying the running length: xfade's offset is
        # measured from the start of everything accumulated so far, not from
        # the start of the segment it is fading out of.
        accumulated = "[v0]"
        length = spine[0].duration
        for index in range(1, len(spine)):
            label = "[vout]" if index == len(spine) - 1 else f"[x{index}]"
            transition = links.get(index - 1)
            if transition is None:
                chains.append(
                    f"{accumulated}[v{index}]concat=n=2:v=1:a=0{label}"
                )
                length += spine[index].duration
            else:
                offset = length - transition.duration
                chains.append(
                    f"{accumulated}[v{index}]xfade=transition={transition.kind}"
                    f":duration={float(transition.duration):.6f}"
                    f":offset={float(offset):.6f}{label}"
                )
                length += spine[index].duration - transition.duration
            accumulated = label

    lanes = sorted(
        (s for s in graph.segments if s.lane > 0 and not s.missing),
        key=lambda s: (s.lane, s.tl_in),
    )
    if lanes:
        # The spine's own last label becomes the base the lanes sit on.
        chains[-1] = chains[-1].replace("[vout]", "[base]")
        base = "[base]"
        for offset, lane_segment in enumerate(lanes):
            index = len(spine) + offset
            args += [
                "-ss", str(float(lane_segment.src_in)),
                "-t", str(float(lane_segment.duration)),
                "-i", lane_segment.source,
            ]
            starts = _output_seconds(graph, lane_segment.tl_in)
            ends = starts + lane_segment.duration
            label = "[vout]" if offset == len(lanes) - 1 else f"[o{index}]"
            chains.append(
                f"[{index}:v]scale=-2:{height},setsar=1,fps=24,"
                f"setpts=PTS-STARTPTS+{float(starts):.6f}/TB[l{index}]"
            )
            chains.append(
                f"{base}[l{index}]overlay=eof_action=pass:"
                f"enable='between(t,{float(starts):.6f},{float(ends):.6f})'{label}"
            )
            base = label

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    return args
