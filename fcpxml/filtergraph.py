"""Compile a parsed Timeline into an ffmpeg graph description.

Pure — no subprocess, no filesystem access beyond an existence check. The
execution half lives in fcpxml/render.py so this can be asserted exactly, on
any machine, with or without ffmpeg installed.

THE INVARIANT (0.28.0). The proxy is exactly as long as the timeline, and
every instant of it is either the timeline's own picture or a black
substitute that the report names. Output time IS timeline time. Everything
below exists to keep that true:

* Source time is read on the ASSET's clock. A clip's ``start`` is written on
  the clock that begins at ``<asset start>`` (a camera's timecode, 3600s for
  01:00:00:00), so the file position is ``start - origin``. A still has no
  clock at all: it is looped, never seeked.
* Connected clips are placed by ``timeline_start``, which the parser
  resolves from the parent's clock. Their raw ``offset`` is in the PARENT's
  local time and is only the fallback for a hand-built model.
* Gaps, and anything on the spine with no picture, render as black for
  their full duration instead of collapsing.
* Transitions are drawn the way Final Cut draws them: on the media OUTSIDE
  both clips' used ranges, with no change to the timeline's length. The
  outgoing stream runs on past its out-point into its tail handle, the
  incoming one starts before its in-point in its head handle. Where the file
  has no handle the edge frame is held, and the report says so.
* A clip whose file has no picture where the timeline asks for one becomes
  black (spine) or is not drawn (lane), with a note, instead of an input
  that yields no frames and empties the whole render.

Time is carried as fractions.Fraction end to end and quantised to output
frames ONCE, cumulatively (``_frame``), so per-piece rounding never adds up
to drift. A preview built on float seconds drifts against the timeline it
claims to represent; at 23.976 the drift is visible within a minute.
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Optional

from fcpxml.media_intel import media_src_to_path

# The proxy's frame rate. Fixed: the proxy exists to be cheap to make and
# quick to scrub, and every quantisation below is to this grid.
FPS = 24

_IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".bmp",
    ".gif", ".psd", ".exr", ".webp", ".dpx", ".tga",
)


@dataclass(frozen=True)
class SourceInfo:
    """What a probe of the actual file found. Overrides the FCPXML's claims.

    ``duration`` is None for a still (it has no end) or when unmeasurable.
    """

    duration: Optional[Fraction] = None
    width: Optional[int] = None
    height: Optional[int] = None
    still: bool = False
    has_video: bool = True


@dataclass(frozen=True)
class Segment:
    """One piece of the timeline: a clip's source media placed on it, or a gap.

    ``src_in``/``src_out`` are the FCPXML's own source times, on the asset's
    clock. ``file_in`` is where that is in the file.
    """

    source: str
    src_in: Fraction
    src_out: Fraction
    tl_in: Fraction
    lane: int
    label: str
    missing: bool = False
    origin: Fraction = Fraction(0)
    still: bool = False
    kind: str = "clip"  # "clip" | "gap"
    file_duration: Optional[Fraction] = None
    width: Optional[int] = None
    height: Optional[int] = None
    conform: str = "fit"
    transform: Any = None
    effects: tuple = ()
    has_video: bool = True

    @property
    def duration(self) -> Fraction:
        return self.src_out - self.src_in

    @property
    def tl_out(self) -> Fraction:
        return self.tl_in + self.duration

    @property
    def file_in(self) -> Fraction:
        """Where ``src_in`` is in the file (a still has one picture: 0)."""
        return Fraction(0) if self.still else self.src_in - self.origin

    def file_time(self, into: Fraction) -> Fraction:
        """File position *into* seconds after the clip's in-point."""
        return Fraction(0) if self.still else self.file_in + into


@dataclass(frozen=True)
class CompiledTransition:
    """A timeline transition resolved onto a spine boundary.

    ``boundary`` is the index of the spine segment the transition follows: it
    joins ``spine[boundary]`` to ``spine[boundary + 1]``. ``start``/``end``
    are output frames; the transition occupies ``[start, end)`` and the cut
    it straddles lies inside. ``kind`` is an ffmpeg xfade name.
    """

    boundary: int
    kind: str
    duration: Fraction
    name: str
    start: int = 0
    end: int = 0


@dataclass(frozen=True)
class Piece:
    """One stream the render reads, in output frames.

    ``source`` None is black. Otherwise ``read_in``/``read_len`` are file
    seconds to read and ``hold_head`` is how long to hold the first frame
    before them (a missing head handle). A short tail is held by the
    compositor, which repeats the last frame.
    """

    segment: int
    lane: int
    start: int
    frames: int
    source: Optional[str] = None
    still: bool = False
    read_in: Fraction = Fraction(0)
    read_len: Fraction = Fraction(0)
    hold_head: Fraction = Fraction(0)


@dataclass(frozen=True)
class FilterGraph:
    """A renderable description of a timeline, plus what we could not honour."""

    segments: tuple[Segment, ...]
    total: Fraction
    substitutions: tuple[str, ...]
    transitions: tuple[CompiledTransition, ...] = ()
    pieces: tuple[Piece, ...] = ()
    frame: tuple[int, int] = (1920, 1080)
    lanes: tuple[Piece, ...] = ()

    @property
    def total_frames(self) -> int:
        return _frame(self.total)


def _frame(t: Fraction) -> int:
    """Nearest output frame to timeline instant *t* (half rounds up)."""
    scaled = Fraction(t) * FPS
    return int((scaled + Fraction(1, 2)).__floor__())


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


def _segment_from(item: Any, lane: int, probes: Optional[Mapping[str, SourceInfo]]) -> Segment:
    """Build a Segment from a Clip or a ConnectedClip.

    Position: a spine Clip carries it on ``start``. A ConnectedClip carries it
    on ``timeline_start`` (resolved by the parser from the parent's clock);
    its ``offset`` is in the PARENT's local time and is used only when a
    hand-built model has no ``timeline_start``. Reading ``offset`` for a parsed
    clip placed every connected clip on a trimmed parent, or on a gap or still
    whose clock starts at 3600s, somewhere it never appears.
    """
    duration = _seconds(item.duration)
    src_in = _seconds(getattr(item, "source_start", None))
    source, missing = _resolve(getattr(item, "media_path", "") or "")
    resolved = getattr(item, "timeline_start", None)
    offset = getattr(item, "offset", None)
    if resolved is not None:
        tl_in = _seconds(resolved)
    elif offset is not None:
        tl_in = _seconds(offset)
    else:
        tl_in = _seconds(item.start)

    media = getattr(item, "media", None)
    origin = Fraction(media.origin) if media is not None else Fraction(0)
    still = bool(media.still) if media is not None else source.lower().endswith(_IMAGE_EXTENSIONS)
    width = media.width if media is not None else None
    height = media.height if media is not None else None
    file_duration = (
        Fraction(media.duration) if media is not None and media.duration else None
    )
    has_video = bool(media.has_video) if media is not None else True

    probe = (probes or {}).get(source) if not missing else None
    if probe is not None:
        still = probe.still
        has_video = probe.has_video
        width = probe.width or width
        height = probe.height or height
        file_duration = probe.duration if not probe.still else None

    return Segment(
        source=source,
        src_in=src_in,
        src_out=src_in + duration,
        tl_in=tl_in,
        lane=lane,
        label=item.name,
        missing=missing,
        origin=origin,
        still=still,
        file_duration=file_duration,
        width=width,
        height=height,
        conform=getattr(item, "conform", "fit") or "fit",
        transform=getattr(item, "transform", None),
        effects=tuple(getattr(item, "effects", ()) or ()),
        has_video=has_video,
    )


def _gap(tl_in: Fraction, length: Fraction) -> Segment:
    return Segment(
        source="", src_in=Fraction(0), src_out=length, tl_in=tl_in,
        lane=0, label="gap", kind="gap",
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
    "fade-to-black": "fadeblack",
    "fade-from-black": "fadeblack",
    "dip to color": "fadeblack",
    "wipe": "wipeleft",
    "slide": "slideleft",
}


def _xfade_kind(transition: Any) -> tuple[str, bool]:
    """Return (xfade transition name, was it recognised?).

    The NAME is read first: it is what the document says the transition is.
    ``transition_type`` only speaks for a hand-built model; the parser leaves
    it empty, because its "cross-dissolve" default used to vouch for every
    transition Final Cut wrote, so "Color Panels" previewed as a dissolve
    without a word.
    """
    for value in (
        getattr(transition, "name", "") or "",
        getattr(transition, "transition_type", "") or "",
    ):
        kind = _XFADE_KINDS.get(value.strip().lower())
        if kind is not None:
            return kind, True
    return "fade", False


def _place_transitions(
    transitions: list[Any], spine: list[Segment]
) -> tuple[list[CompiledTransition], list[str]]:
    """Resolve each transition onto the spine boundary it straddles.

    A transition occupies ``[cut - lead, cut - lead + duration)``: by default
    centred on its cut, or wherever its own offset put it. It needs a cut
    within its own duration, a boundary not already spoken for, and both
    neighbours present. It must start after the outgoing clip starts and end
    before the incoming one ends; Final Cut guarantees that for anything it
    exports, so a transition that overruns is clipped to fit and reported.
    Its length is never squeezed to its neighbours' length: it draws on their
    handles, not on their used ranges.
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
        cut = boundaries[index]
        if abs(cut - at) > length:
            note(f"no spine cut within {float(length):.2f}s of it")
            continue
        if index in taken:
            note("another transition already occupies that cut")
            continue
        before, after = spine[index], spine[index + 1]
        if before.missing or after.missing:
            note("a clip on one side of it is missing its media")
            continue

        lead_tc = getattr(transition, "lead", None)
        lead = _seconds(lead_tc) if lead_tc is not None else length / 2
        lead = min(max(lead, Fraction(0)), length)
        start, end = _frame(cut - lead), _frame(cut - lead + length)
        low, high = _frame(before.tl_in), _frame(after.tl_out)
        clipped = (max(start, low + 1), min(end, high - 1))
        if clipped[1] - clipped[0] < 1 or not clipped[0] <= _frame(cut) <= clipped[1]:
            note("its neighbours are too short to hold it")
            continue
        if clipped != (start, end):
            notes.append(
                f"{name!r} at {float(at):.2f}s clipped to "
                f"{(clipped[1] - clipped[0]) / FPS:.2f}s: it ran past the end of "
                f"{before.label!r} or {after.label!r}"
            )
            start, end = clipped
        if not recognised:
            notes.append(
                f"{name!r} at {float(at):.2f}s rendered as a dissolve "
                f"(no xfade equivalent for this transition)"
            )
        taken.add(index)
        compiled.append(CompiledTransition(
            boundary=index, kind=kind, duration=Fraction(end - start, FPS),
            name=name, start=start, end=end,
        ))

    compiled.sort(key=lambda c: c.boundary)
    # Two transitions on either side of one short clip may not overlap: the
    # second xfade would start before the first has finished with the clip.
    kept: list[CompiledTransition] = []
    for transition in compiled:
        previous = kept[-1] if kept else None
        if previous is not None and previous.boundary == transition.boundary - 1 \
                and transition.start < previous.end:
            notes.append(
                f"{transition.name!r} at {transition.start / FPS:.2f}s rendered as "
                f"a hard cut (it overlaps {previous.name!r} across "
                f"{spine[transition.boundary].label!r})"
            )
            continue
        kept.append(transition)
    return kept, notes


def _availability(segment: Segment, want_in: Fraction, want_len: Fraction):
    """(read_in, read_len, hold_head, hold_tail) for a file span, or None.

    None means the file has no picture anywhere in the span. ``file_duration``
    None is "unknown": trusted rather than clamped, as before 0.28.
    """
    if segment.still:
        return Fraction(0), want_len, Fraction(0), Fraction(0)
    end = segment.file_duration
    read_in = max(Fraction(0), want_in)
    read_out = want_in + want_len if end is None else min(end, want_in + want_len)
    if read_out - read_in < Fraction(1, 2 * FPS):
        return None
    return read_in, read_out - read_in, read_in - want_in, (want_in + want_len) - read_out


def _picture_notes(segment: Segment) -> list[str]:
    """What about this clip's picture the proxy does not draw."""
    unread = list(segment.effects)
    t = segment.transform
    if t is not None:
        if t.rotation:
            unread.append("rotation")
        if any(t.anchor):
            unread.append("anchor")
        if t.animated:
            unread.append("keyframed transform (drawn at rest)")
        if t.scale[0] < 0 or t.scale[1] < 0:
            unread.append("flip")
    return unread


def compile_timeline(
    timeline: Any,
    probes: Optional[Mapping[str, SourceInfo]] = None,
    *,
    with_transitions: bool = True,
    with_lanes: bool = True,
) -> FilterGraph:
    """Build a FilterGraph from a parsed Timeline.

    ``probes`` maps a source path to what a probe of the real file found
    (render.py supplies it); without it the FCPXML's own asset data is used.
    ``with_transitions`` / ``with_lanes`` are the render's fallbacks: when
    ffmpeg refuses the full graph it is rebuilt without them, and the report
    says which was dropped.
    """
    clips = sorted(getattr(timeline, "clips", None) or [], key=lambda c: _seconds(c.start))
    spine_clips = [_segment_from(clip, 0, probes) for clip in clips]

    # Holes in the spine (gaps, spine titles, generators) are black, and keep
    # their time. Before 0.28 they collapsed and everything after a gap came
    # early.
    spine: list[Segment] = []
    cursor = Fraction(0)
    for segment in spine_clips:
        if segment.tl_in > cursor:
            spine.append(_gap(cursor, segment.tl_in - cursor))
        spine.append(segment)
        cursor = max(cursor, segment.tl_out)
    declared = _seconds(getattr(timeline, "duration", None))
    if declared > cursor:
        spine.append(_gap(cursor, declared - cursor))
        cursor = declared
    total = cursor

    notes: list[str] = []
    lanes: list[Segment] = []
    for connected in getattr(timeline, "connected_clips", None) or []:
        lane = getattr(connected, "lane", 1)
        if lane <= 0:
            continue
        segment = _segment_from(connected, lane, probes)
        kind = getattr(connected, "clip_type", "asset-clip")
        if kind in ("title", "caption") or not getattr(connected, "media_path", ""):
            what = "a title" if kind == "title" else "a caption" if kind == "caption" else "no media"
            notes.append(
                f"lane {lane} clip {segment.label!r} not drawn ({what}: "
                f"the proxy renders footage only)"
            )
            continue
        lanes.append(segment)
    if any(getattr(c, "lane", 0) < 0 for c in getattr(timeline, "connected_clips", None) or []):
        notes.append("audio lanes are not mixed: this preview is video only")

    compiled, transition_notes = (
        _place_transitions(list(getattr(timeline, "transitions", None) or []), spine)
        if with_transitions else ([], [])
    )
    if not with_transitions and getattr(timeline, "transitions", None):
        transition_notes = [
            f"every transition rendered as a hard cut ({len(timeline.transitions)} "
            f"dropped after ffmpeg refused the crossfade graph)"
        ]
    notes = transition_notes + notes

    left = {t.boundary + 1: t for t in compiled}
    right = {t.boundary: t for t in compiled}
    pieces: list[Piece] = []
    undrawn: list[str] = []
    for index, segment in enumerate(spine):
        own_start, own_end = _frame(segment.tl_in), _frame(segment.tl_out)
        start = left[index].start if index in left else own_start
        end = right[index].end if index in right else own_end
        frames = end - start
        if frames <= 0:
            continue
        if segment.kind == "gap":
            pieces.append(Piece(segment=index, lane=0, start=start, frames=frames))
            continue
        if segment.missing or not segment.has_video:
            why = "its media is missing" if segment.missing else "its media has no picture"
            notes.append(f"{segment.label!r} at {float(segment.tl_in):.2f}s shown black ({why})")
            pieces.append(Piece(segment=index, lane=0, start=start, frames=frames))
            continue
        into = Fraction(start, FPS) - segment.tl_in
        span = Fraction(frames, FPS)
        available = _availability(segment, segment.file_time(into), span)
        if available is None:
            length = (
                f"the file is {float(segment.file_duration):.2f}s long"
                if segment.file_duration is not None else "the file is shorter"
            )
            notes.append(
                f"{segment.label!r} at {float(segment.tl_in):.2f}s shown black "
                f"(no picture at {float(segment.file_time(into)):.2f}s in its file; {length})"
            )
            pieces.append(Piece(segment=index, lane=0, start=start, frames=frames))
            continue
        read_in, read_len, hold_head, hold_tail = available
        if hold_head > 0:
            notes.append(
                f"{segment.label!r} at {float(segment.tl_in):.2f}s holds its first frame "
                f"for {float(hold_head):.2f}s (its file has no picture before "
                f"{float(read_in):.2f}s{', so the transition into it has no head handle' if index in left else ''})"
            )
        if hold_tail > Fraction(1, FPS):
            notes.append(
                f"{segment.label!r} at {float(segment.tl_in):.2f}s holds its last frame "
                f"for {float(hold_tail):.2f}s (its file ends at "
                f"{float(read_in + read_len):.2f}s{', so the transition out of it has no tail handle' if index in right else ''})"
            )
        unread = _picture_notes(segment)
        if unread:
            undrawn.append(f"{segment.label!r} ({', '.join(unread)})")
        pieces.append(Piece(
            segment=index, lane=0, start=start, frames=frames, source=segment.source,
            still=segment.still, read_in=read_in, read_len=read_len, hold_head=hold_head,
        ))

    lane_pieces: list[Piece] = []
    ordered_lanes = sorted(lanes, key=lambda s: (s.lane, s.tl_in))
    if with_lanes:
        for position, segment in enumerate(ordered_lanes):
            start, end = _frame(segment.tl_in), _frame(segment.tl_out)
            if end - start <= 0:
                continue
            if segment.missing:
                notes.append(
                    f"lane {segment.lane} clip {segment.label!r} not drawn (its media is missing)"
                )
                continue
            if not segment.has_video:
                notes.append(
                    f"lane {segment.lane} clip {segment.label!r} not drawn (its media has no picture)"
                )
                continue
            available = _availability(segment, segment.file_in, Fraction(end - start, FPS))
            if available is None:
                notes.append(
                    f"lane {segment.lane} clip {segment.label!r} not drawn (no picture at "
                    f"{float(segment.file_in):.2f}s in its file)"
                )
                continue
            read_in, read_len, hold_head, _ = available
            unread = _picture_notes(segment)
            if unread:
                undrawn.append(f"lane {segment.lane} {segment.label!r} ({', '.join(unread)})")
            lane_pieces.append(Piece(
                segment=len(spine) + position, lane=segment.lane, start=start,
                frames=end - start, source=segment.source, still=segment.still,
                read_in=read_in, read_len=read_len, hold_head=hold_head,
            ))
    elif any(not s.missing for s in lanes):
        notes.append(
            f"{len(lanes)} connected video clip(s) not drawn (dropped after ffmpeg "
            f"refused the compositing graph)"
        )
    if undrawn:
        notes.append(
            "drawn without what the proxy cannot render: " + "; ".join(undrawn)
        )

    frame = (
        int(getattr(timeline, "width", 1920) or 1920),
        int(getattr(timeline, "height", 1080) or 1080),
    )
    return FilterGraph(
        segments=tuple(spine) + tuple(ordered_lanes),
        total=total,
        substitutions=tuple(notes),
        transitions=tuple(compiled),
        pieces=tuple(pieces),
        frame=frame,
        lanes=tuple(lane_pieces),
    )


def canvas_size(graph: FilterGraph, height: int) -> tuple[int, int]:
    """The proxy's pixel size: *height* tall at the SEQUENCE's aspect, even."""
    if not 1 <= height <= 2160:
        raise ValueError(f"height must be between 1 and 2160, got {height}")
    seq_w, seq_h = graph.frame
    canvas_h = max(2, height - height % 2)
    canvas_w = max(2, int(round(canvas_h * seq_w / seq_h / 2)) * 2)
    return canvas_w, canvas_h


def placement(graph: FilterGraph, segment: Segment, height: int) -> tuple[int, int, int, int]:
    """(width, height, x, y) of *segment*'s picture on the proxy canvas.

    Final Cut conforms a clip to the sequence frame first (``fit`` letterboxes,
    ``fill`` crops, ``none`` keeps native pixels), then applies the
    transform: ``scale``, then ``position`` in percent of the frame height
    from the centre, y up. A clip of unknown size is assumed to be the
    sequence's own size.
    """
    canvas_w, canvas_h = canvas_size(graph, height)
    seq_w, seq_h = graph.frame
    src_w = segment.width or seq_w
    src_h = segment.height or seq_h
    if segment.conform == "fill":
        conform = max(Fraction(seq_w, src_w), Fraction(seq_h, src_h))
    elif segment.conform == "none":
        conform = Fraction(1)
    else:
        conform = min(Fraction(seq_w, src_w), Fraction(seq_h, src_h))
    t = segment.transform
    scale_x = abs(Fraction(t.scale[0]).limit_denominator(10000)) if t is not None else Fraction(1)
    scale_y = abs(Fraction(t.scale[1]).limit_denominator(10000)) if t is not None else Fraction(1)
    pixels = Fraction(canvas_h, seq_h)

    def even(value: Fraction) -> int:
        return max(2, int(round(value / 2)) * 2)

    width = even(src_w * conform * scale_x * pixels)
    height_px = even(src_h * conform * scale_y * pixels)
    pos_x, pos_y = (t.position if t is not None else (0.0, 0.0))
    centre_x = Fraction(canvas_w, 2) + Fraction(pos_x).limit_denominator(10000) / 100 * canvas_h
    centre_y = Fraction(canvas_h, 2) - Fraction(pos_y).limit_denominator(10000) / 100 * canvas_h
    return width, height_px, int(round(centre_x - Fraction(width, 2))), int(round(centre_y - Fraction(height_px, 2)))


def _input_args(piece: Piece) -> list[str]:
    if piece.still:
        # A still has no clock: loop its one picture for as long as it plays.
        # Seeking into it (the pre-0.28 "-ss 3600") finds nothing at all.
        return [
            "-loop", "1", "-framerate", str(FPS),
            "-t", f"{float(Fraction(piece.frames + 2, FPS)):.6f}", "-i", piece.source,
        ]
    # -ss before -i is the fast seek; -t bounds the read so a two-second cut
    # out of an hour-long source costs two seconds of decode.
    return [
        "-ss", f"{float(piece.read_in):.6f}",
        "-t", f"{float(piece.read_len):.6f}",
        "-i", piece.source,
    ]


def _picture_chain(label_in: str, piece: Piece, size: tuple[int, int], label_out: str) -> str:
    width, height = size
    hold = (
        f",tpad=start_mode=clone:start_duration={float(piece.hold_head):.6f}"
        if piece.hold_head > 0 else ""
    )
    return (
        f"{label_in}fps={FPS},scale={width}:{height},setsar=1,format=yuv420p"
        f"{hold},setpts=PTS-STARTPTS{label_out}"
    )


def graph_to_args(graph: FilterGraph, out_path: str, height: int = 480) -> list[str]:
    """Build the ffmpeg argument list that renders *graph* to *out_path*.

    An argument list, never a shell string: no user-supplied value is ever
    interpreted by a shell.

    Every spine piece is drawn onto a black canvas of the sequence's aspect
    for exactly its own frame count, so pieces of any size, rate or format
    join cleanly and the canvas shows what Final Cut's conform would. Pieces
    join with concat, or with xfade where a transition was compiled; the
    xfade offset is the transition's own start, because output time is
    timeline time. Video lanes overlay the result for their own frames.
    """
    canvas_w, canvas_h = canvas_size(graph, height)
    # A gap-based edit (every picture on a lane over a gap, issue #23) is
    # renderable; only a timeline with no picture anywhere is not.
    if not graph.pieces or not (any(p.source for p in graph.pieces) or graph.lanes):
        raise ValueError("nothing renderable: every spine clip is missing its media")

    args: list[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    chains: list[str] = []
    inputs = 0
    for number, piece in enumerate(graph.pieces):
        black = (
            f"color=c=black:s={canvas_w}x{canvas_h}:r={FPS},format=yuv420p,"
            f"trim=end_frame={piece.frames},setpts=PTS-STARTPTS"
        )
        if piece.source is None:
            chains.append(f"{black}[p{number}]")
            continue
        args += _input_args(piece)
        width, height_px, x, y = placement(graph, graph.segments[piece.segment], height)
        chains.append(_picture_chain(f"[{inputs}:v]", piece, (width, height_px), f"[s{number}]"))
        chains.append(f"{black}[b{number}]")
        # eof_action=repeat holds the last frame when a file ends short.
        chains.append(
            f"[b{number}][s{number}]overlay=x={x}:y={y}:eof_action=repeat,"
            f"trim=end_frame={piece.frames},setpts=PTS-STARTPTS,format=yuv420p[p{number}]"
        )
        inputs += 1

    by_segment = {piece.segment: number for number, piece in enumerate(graph.pieces)}
    linked = {
        by_segment[t.boundary]: t for t in graph.transitions
        if t.boundary in by_segment and by_segment.get(t.boundary + 1) == by_segment[t.boundary] + 1
    }
    count = len(graph.pieces)
    final = "[base]" if graph.lanes else "[vout]"
    if not linked:
        chains.append(
            "".join(f"[p{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=0{final}"
        )
    else:
        accumulated = "[p0]"
        for number in range(1, count):
            label = final if number == count - 1 else f"[x{number}]"
            transition = linked.get(number - 1)
            if transition is None:
                chains.append(f"{accumulated}[p{number}]concat=n=2:v=1:a=0{label}")
            else:
                chains.append(
                    f"{accumulated}[p{number}]xfade=transition={transition.kind}"
                    f":duration={float(Fraction(transition.end - transition.start, FPS)):.6f}"
                    f":offset={float(Fraction(transition.start, FPS)):.6f}{label}"
                )
            accumulated = label

    base = "[base]"
    for offset, piece in enumerate(graph.lanes):
        segment = graph.segments[piece.segment]
        args += _input_args(piece)
        width, height_px, x, y = placement(graph, segment, height)
        label = "[vout]" if offset == len(graph.lanes) - 1 else f"[o{offset}]"
        chains.append(
            _picture_chain(f"[{inputs}:v]", piece, (width, height_px), "")
            + f",trim=end_frame={piece.frames},setpts=PTS-STARTPTS+{piece.start}/({FPS}*TB)[l{offset}]"
        )
        chains.append(
            f"{base}[l{offset}]overlay=x={x}:y={y}:eof_action=pass:"
            f"enable='between(n,{piece.start},{piece.start + piece.frames - 1})'{label}"
        )
        base = label
        inputs += 1

    args += [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    return args
