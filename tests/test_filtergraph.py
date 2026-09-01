"""Timeline -> ffmpeg graph. Pure: no subprocess, no filesystem writes.

Exactness is the point. A preview built on float seconds drifts against the
timeline it claims to show, and at 23.976 the drift is visible within a minute.
These tests run on a machine with no ffmpeg installed, which is why the
compilation lives apart from the execution.
"""

from fractions import Fraction

import pytest

from fcpxml.filtergraph import compile_timeline, graph_to_args
from fcpxml.models import Clip, ConnectedClip, Timecode, Timeline, Transition

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


def test_every_transition_is_reported_as_a_substitution():
    tl = _timeline(
        [_clip("a", 0, 24), _clip("b", 24, 24)],
        transitions=[Transition(
            name="Cross Dissolve",
            duration=Timecode(frames=4, frame_rate=24.0),
            start=Timecode(frames=22, frame_rate=24.0),
        )],
    )
    graph = compile_timeline(tl)
    assert len(graph.substitutions) == 1
    assert "Cross Dissolve" in graph.substitutions[0]
    assert "hard cut" in graph.substitutions[0]


def test_no_transitions_means_no_substitutions():
    assert compile_timeline(_timeline([_clip("a", 0, 24)])).substitutions == ()


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
