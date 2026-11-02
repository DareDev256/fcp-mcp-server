"""Timeline -> ffmpeg graph. Pure: no subprocess, no filesystem writes.

Exactness is the point. A preview built on float seconds drifts against the
timeline it claims to show, and at 23.976 the drift is visible within a minute.
These tests run on a machine with no ffmpeg installed, which is why the
compilation lives apart from the execution.
"""

from fractions import Fraction

import pytest

from fcpxml.filtergraph import compile_timeline, graph_to_args
from fcpxml.models import Clip, ConnectedClip, Timecode, Timeline, Transform, Transition

NTSC_24 = 24000 / 1001


def _clip(name, start_frames, dur_frames, fps=24.0, path="/tmp/a.mov"):
    return Clip(
        name=name,
        start=Timecode(frames=start_frames, frame_rate=fps),
        duration=Timecode(frames=dur_frames, frame_rate=fps),
        source_start=Timecode(frames=0, frame_rate=fps),
        media_path=path,
    )


def _timeline(clips, fps=24.0, **kw):
    total = sum(c.duration.frames for c in clips)
    return Timeline(
        name="T", duration=Timecode(frames=total, frame_rate=fps),
        frame_rate=fps, clips=clips, **kw
    )


def test_spine_clips_become_segments_in_timeline_order():
    graph = compile_timeline(_timeline([_clip("b", 24, 24), _clip("a", 0, 24)]))
    assert [s.label for s in graph.segments] == ["a", "b"]
    assert [s.lane for s in graph.segments] == [0, 0]


def test_segment_times_are_exact_fractions_never_floats():
    graph = compile_timeline(_timeline([_clip("a", 0, 24, fps=NTSC_24)], fps=NTSC_24))
    seg = graph.segments[0]
    assert isinstance(seg.tl_in, Fraction)
    assert isinstance(seg.src_out, Fraction)
    # 24 frames at 23.976 is exactly 24*1001/24000s, not 1.0 and not 24/23.
    assert seg.duration == Fraction(24 * 1001, 24000)


def test_total_is_the_sum_of_spine_durations():
    graph = compile_timeline(_timeline([_clip("a", 0, 24), _clip("b", 24, 48)]))
    assert graph.total == Fraction(3, 1)


def test_connected_clips_keep_their_lane_and_timeline_offset():
    tl = _timeline(
        [_clip("a", 0, 24)],
        connected_clips=[ConnectedClip(
            name="broll",
            start=Timecode(frames=0, frame_rate=24.0),
            duration=Timecode(frames=8, frame_rate=24.0),
            lane=2,
            offset=Timecode(frames=4, frame_rate=24.0),
            source_start=Timecode(frames=0, frame_rate=24.0),
            media_path="/tmp/b.mov",
        )],
    )
    broll = next(s for s in compile_timeline(tl).segments if s.label == "broll")
    assert broll.lane == 2
    assert broll.tl_in == Fraction(4, 24)


def test_connected_clips_do_not_inflate_the_total():
    """Total is spine time. A lane sitting over the spine adds no duration."""
    tl = _timeline(
        [_clip("a", 0, 24)],
        connected_clips=[ConnectedClip(
            name="broll",
            start=Timecode(frames=0, frame_rate=24.0),
            duration=Timecode(frames=8, frame_rate=24.0),
            lane=2, media_path="/tmp/b.mov",
        )],
    )
    assert compile_timeline(tl).total == Fraction(1, 1)


def test_missing_media_is_flagged_not_fatal():
    graph = compile_timeline(_timeline([_clip("ghost", 0, 24, path="")]))
    assert graph.segments[0].missing is True


def test_a_path_that_does_not_exist_is_also_flagged():
    graph = compile_timeline(_timeline([_clip("gone", 0, 24, path="/nope/nope.mov")]))
    assert graph.segments[0].missing is True


def test_a_file_url_is_resolved_to_a_filesystem_path(tmp_path):
    real = tmp_path / "real clip.mov"
    real.write_bytes(b"\x00")
    graph = compile_timeline(
        _timeline([_clip("real", 0, 24, path=f"file://{real}".replace(" ", "%20"))])
    )
    assert graph.segments[0].missing is False
    assert graph.segments[0].source == str(real)


def test_a_transition_on_absent_media_is_reported_rather_than_compiled():
    """The fixture clips point at a path that does not exist, so there is
    nothing to fade between and the operator has to be told."""
    tl = _timeline(
        [_clip("a", 0, 24), _clip("b", 24, 24)],
        transitions=[Transition(
            name="Cross Dissolve",
            duration=Timecode(frames=4, frame_rate=24.0),
            start=Timecode(frames=22, frame_rate=24.0),
        )],
    )
    graph = compile_timeline(tl)
    assert graph.transitions == ()
    transition_notes = [n for n in graph.substitutions if "Cross Dissolve" in n]
    assert len(transition_notes) == 1
    assert "hard cut" in transition_notes[0]
    # The clips themselves are named as black, not silently dropped.
    assert sum("shown black" in n for n in graph.substitutions) == 2


def test_no_transitions_and_present_media_means_no_substitutions(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    assert compile_timeline(_timeline([_clip("a", 0, 24, path=str(media))])).substitutions == ()


def test_args_are_a_list_never_a_shell_string(tmp_path):
    real = tmp_path / "a.mov"
    real.write_bytes(b"\x00")
    graph = compile_timeline(_timeline([_clip("a", 0, 24, path=str(real))]))
    args = graph_to_args(graph, "/tmp/out.mp4", height=360)
    assert isinstance(args, list)
    assert all(isinstance(a, str) for a in args)
    assert args[0] == "ffmpeg"
    assert args[-1] == "/tmp/out.mp4"


def test_args_reject_an_absurd_height(tmp_path):
    real = tmp_path / "a.mov"
    real.write_bytes(b"\x00")
    graph = compile_timeline(_timeline([_clip("a", 0, 24, path=str(real))]))
    with pytest.raises(ValueError, match="height"):
        graph_to_args(graph, "/tmp/out.mp4", height=9000)


def test_args_refuse_a_graph_with_no_renderable_media():
    graph = compile_timeline(_timeline([_clip("ghost", 0, 24, path="")]))
    with pytest.raises(ValueError, match="missing its media"):
        graph_to_args(graph, "/tmp/out.mp4")


def test_empty_timeline_compiles_to_an_empty_graph():
    tl = Timeline(name="T", duration=Timecode(frames=0, frame_rate=24.0))
    graph = compile_timeline(tl)
    assert graph.segments == ()
    assert graph.total == Fraction(0)


# --- transition compilation (0.20.0) --------------------------------------


def _transition(name, start_frames, duration_frames=4, kind="cross-dissolve"):
    return Transition(
        name=name,
        duration=Timecode(frames=duration_frames, frame_rate=24.0),
        start=Timecode(frames=start_frames, frame_rate=24.0),
        transition_type=kind,
    )


def test_a_dissolve_on_a_cut_compiles_to_an_xfade(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))],
        transitions=[_transition("Cross Dissolve", 22)],
    )
    graph = compile_timeline(tl)

    assert len(graph.transitions) == 1
    compiled = graph.transitions[0]
    assert (compiled.boundary, compiled.kind) == (0, "fade")
    assert compiled.duration == Fraction(4, 24)
    # Centred on the cut at frame 24: it runs 22..26.
    assert (compiled.start, compiled.end) == (22, 26)
    # A transition moves no offsets: the timeline, and so the render, keep
    # their length. Before 0.28 the render came out 4 frames short.
    assert graph.total == Fraction(48, 24)

    args = graph_to_args(graph, "/tmp/out.mp4", height=360)
    chain = args[args.index("-filter_complex") + 1]
    # Output time is timeline time: the xfade starts where the transition does.
    assert "xfade=transition=fade:duration=0.166667:offset=0.916667[vout]" in chain
    assert "concat" not in chain
    # The outgoing clip runs on into its tail handle (0..26), the incoming
    # one starts in its head handle (22..48).
    assert [(p.start, p.frames) for p in graph.pieces] == [(0, 26), (22, 26)]


def test_an_unrecognised_transition_still_renders_but_says_so(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))],
        transitions=[_transition("Kaleidoscope", 24, kind="kaleidoscope")],
    )
    graph = compile_timeline(tl)
    assert graph.transitions[0].kind == "fade"
    assert "rendered as a dissolve" in graph.substitutions[0]


def test_a_transition_nowhere_near_a_cut_is_reported_not_moved(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))],
        transitions=[_transition("Cross Dissolve", 2)],
    )
    graph = compile_timeline(tl)
    assert graph.transitions == ()
    assert "hard cut" in graph.substitutions[0]
    assert "no spine cut within" in graph.substitutions[0]


def test_a_transition_beside_missing_media_is_reported_as_a_hard_cut(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path="/nope/gone.mov")],
        transitions=[_transition("Cross Dissolve", 24)],
    )
    graph = compile_timeline(tl)
    assert graph.transitions == ()
    assert "missing its media" in graph.substitutions[0]


def test_a_transition_longer_than_a_neighbour_keeps_its_length(tmp_path):
    """John Palchak's case 1: a 1.87s transition beside a 1.27s clip.

    Before 0.28 it was squeezed to the short clip's whole length, which handed
    xfade a second input that was nothing but transition, and the render
    failed with "Nothing was written into output file". Final Cut draws a
    transition on HANDLES, so the short clip only has to cover the half after
    the cut.
    """
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 8, path=str(media))],
        transitions=[_transition("Cross Dissolve", 24, duration_frames=12)],
    )
    graph = compile_timeline(tl)
    assert graph.transitions[0].duration == Fraction(12, 24)
    assert not any("shortened" in n or "clipped" in n for n in graph.substitutions)
    assert graph.total == Fraction(32, 24)


def test_a_transition_that_overruns_its_clip_is_clipped_and_reported(tmp_path):
    """Final Cut never exports this, but our own writers could: a centred
    12-frame transition into a 4-frame clip would end after the clip does."""
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 4, path=str(media))],
        transitions=[_transition("Cross Dissolve", 24, duration_frames=12)],
    )
    graph = compile_timeline(tl)
    compiled = graph.transitions[0]
    assert compiled.end <= 27 and compiled.start == 18
    assert any("clipped to" in n for n in graph.substitutions)


def test_a_transition_uses_its_own_lead_not_an_assumed_centre(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    transition = _transition("Cross Dissolve", 24, duration_frames=12)
    transition.lead = Timecode(frames=0, frame_rate=24.0)
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))],
        transitions=[transition],
    )
    assert (compile_timeline(tl).transitions[0].start, compile_timeline(tl).transitions[0].end) == (24, 36)


def test_two_transitions_cannot_share_one_cut(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))],
        transitions=[_transition("Cross Dissolve", 24), _transition("Wipe", 24, kind="wipe")],
    )
    graph = compile_timeline(tl)
    assert len(graph.transitions) == 1
    assert "already occupies that cut" in graph.substitutions[0]


def test_a_wipe_compiles_to_a_wipe_not_a_dissolve(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))],
        transitions=[_transition("Wipe", 24, kind="wipe")],
    )
    graph = compile_timeline(tl)
    assert graph.transitions[0].kind == "wipeleft"
    assert not any("Wipe" in n for n in graph.substitutions)


def test_untransitioned_cuts_beside_an_xfade_stay_concats(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [
            _clip("a", 0, 24, path=str(media)),
            _clip("b", 24, 24, path=str(media)),
            _clip("c", 48, 24, path=str(media)),
        ],
        transitions=[_transition("Cross Dissolve", 24)],
    )
    args = graph_to_args(compile_timeline(tl), "/tmp/out.mp4")
    chain = args[args.index("-filter_complex") + 1]
    assert "[p0][p1]xfade=transition=fade:duration=0.166667:offset=0.916667[x1]" in chain
    assert "[x1][p2]concat=n=2:v=1:a=0[vout]" in chain


def test_a_timeline_without_transitions_still_uses_one_nway_concat(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 24, path=str(media))]
    )
    args = graph_to_args(compile_timeline(tl), "/tmp/out.mp4")
    chain = args[args.index("-filter_complex") + 1]
    assert "[p0][p1]concat=n=2:v=1:a=0[vout]" in chain
    assert "xfade" not in chain


# --- lane compositing (0.20.0) ---------------------------------------------


def _lane(name, lane, offset_frames, dur_frames, path):
    return ConnectedClip(
        name=name,
        start=Timecode(frames=0, frame_rate=24.0),
        duration=Timecode(frames=dur_frames, frame_rate=24.0),
        lane=lane,
        offset=Timecode(frames=offset_frames, frame_rate=24.0),
        source_start=Timecode(frames=0, frame_rate=24.0),
        media_path=path,
    )


def test_a_video_lane_is_overlaid_for_its_own_window_only(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 48, path=str(media))],
        connected_clips=[_lane("broll", 1, 24, 24, str(media))],
    )
    args = graph_to_args(compile_timeline(tl), "/tmp/out.mp4", height=360)
    chain = args[args.index("-filter_complex") + 1]

    assert "setpts=PTS-STARTPTS+24/(24*TB)[l0]" in chain
    assert "overlay=x=0:y=0:eof_action=pass:enable='between(n,24,47)'[vout]" in chain
    # The lane's source is opened as its own input, after the spine's.
    assert args.count("-i") == 2


def test_a_crossfade_before_a_lane_no_longer_moves_it(tmp_path):
    """Output time is timeline time, so a lane after a crossfade sits at its
    own timeline frame. Before 0.28 the render was shorter than the timeline
    and every later overlay had to be shifted back to match."""
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 24, path=str(media)), _clip("b", 24, 48, path=str(media))],
        connected_clips=[_lane("broll", 1, 48, 12, str(media))],
        transitions=[_transition("Cross Dissolve", 24, duration_frames=12)],
    )
    args = graph_to_args(compile_timeline(tl), "/tmp/out.mp4")
    chain = args[args.index("-filter_complex") + 1]
    assert "setpts=PTS-STARTPTS+48/(24*TB)" in chain
    assert "enable='between(n,48,59)'" in chain


def test_a_lane_whose_media_is_missing_is_not_drawn_but_is_reported(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 48, path=str(media))],
        connected_clips=[_lane("gone", 1, 0, 24, "/nope/missing.mov")],
    )
    graph = compile_timeline(tl)
    args = graph_to_args(graph, "/tmp/out.mp4")
    assert "[l0]" not in args[args.index("-filter_complex") + 1]
    assert graph.lanes == ()
    assert any("its media is missing" in note for note in graph.substitutions)


def test_lane_compositing_says_what_it_cannot_read(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    lane = _lane("broll", 1, 0, 24, str(media))
    lane.effects = ("adjust-crop", "filter-video")
    lane.transform = Transform(rotation=15.0)
    tl = _timeline([_clip("a", 0, 48, path=str(media))], connected_clips=[lane])
    notes = compile_timeline(tl).substitutions
    undrawn = next(n for n in notes if "cannot render" in n)
    assert "adjust-crop" in undrawn and "filter-video" in undrawn and "rotation" in undrawn


def test_a_plain_lane_carries_no_caveat(tmp_path):
    """Conform and transform are drawn now, so an unadjusted lane needs no
    "drawn full-frame" disclaimer — that line used to sit on every lane,
    including the ones that were never drawn at all."""
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 48, path=str(media))],
        connected_clips=[_lane("broll", 1, 0, 24, str(media))],
    )
    assert compile_timeline(tl).substitutions == ()


def test_an_audio_lane_is_reported_rather_than_silently_dropped(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 48, path=str(media))],
        connected_clips=[_lane("music", -1, 0, 48, str(media))],
    )
    graph = compile_timeline(tl)
    assert any("audio lanes are not mixed" in note for note in graph.substitutions)
    args = graph_to_args(graph, "/tmp/out.mp4")
    # The only overlay is the spine piece onto its own canvas.
    assert "[l0]" not in args[args.index("-filter_complex") + 1]


def test_two_video_lanes_stack_in_lane_order(tmp_path):
    media = tmp_path / "a.mov"
    media.write_bytes(b"\x00")
    tl = _timeline(
        [_clip("a", 0, 96, path=str(media))],
        connected_clips=[
            _lane("top", 2, 0, 24, str(media)),
            _lane("under", 1, 0, 24, str(media)),
        ],
    )
    chain = graph_to_args(compile_timeline(tl), "/tmp/out.mp4")[
        graph_to_args(compile_timeline(tl), "/tmp/out.mp4").index("-filter_complex") + 1
    ]
    # lane 1 composites onto the spine, lane 2 onto the result of lane 1.
    assert "[base][l0]overlay" in chain
    assert "[o0][l1]overlay" in chain and chain.endswith("[vout]")
    graph = compile_timeline(tl)
    assert [graph.segments[p.segment].label for p in graph.lanes] == ["under", "top"]
