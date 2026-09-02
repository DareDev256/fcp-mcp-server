"""Rendering, and the read-back that proves the render is what we asked for.

A file existing is not evidence a render succeeded. ffmpeg exits 0 having
written a near-empty container more often than anyone expects, so every proxy
reads its own duration back off the artifact and compares it against the
timeline's exact rational.
"""

import shutil
import subprocess
from fractions import Fraction

import pytest

from fcpxml import render
from fcpxml.models import Clip, ConnectedClip, Timecode, Timeline, Transition

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _timeline(media, frames=48, fps=24.0):
    return Timeline(
        name="T", duration=Timecode(frames=frames, frame_rate=fps), frame_rate=fps,
        clips=[Clip(
            name="a",
            start=Timecode(frames=0, frame_rate=fps),
            duration=Timecode(frames=frames, frame_rate=fps),
            source_start=Timecode(frames=0, frame_rate=fps),
            media_path=str(media),
        )],
    )


@pytest.fixture
def bars(tmp_path):
    """Four seconds of colour bars — a real file ffmpeg can actually cut."""
    out = tmp_path / "bars.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, timeout=120, check=True,
    )
    return out


def test_cache_dir_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = render.cache_dir()
    assert directory.is_dir()
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert (directory.parent.stat().st_mode & 0o777) == 0o700


def test_render_without_ffmpeg_names_what_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    result = render.render_proxy(_timeline(tmp_path / "nope.mov"))
    assert result["path"] is None
    assert "ffmpeg" in result["error"]
    assert "install" in result["error"].lower()


def test_render_with_all_media_missing_reports_rather_than_raises(monkeypatch, tmp_path):
    # Pretend ffmpeg is present so the check under test is the MEDIA one.
    # graph_to_args refuses before any subprocess, so nothing runs. Without
    # this the test read "ffmpeg is not on PATH" on the publish runner, which
    # installs no ffmpeg — and that is what blocked the v0.18.0 upload.
    monkeypatch.setattr(render.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    result = render.render_proxy(_timeline(tmp_path / "nope.mov"))
    assert result["path"] is None
    assert result["skipped"] == ["a"]
    assert "missing its media" in result["error"]


def test_probe_duration_without_ffprobe_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    assert render.probe_duration(str(tmp_path / "x.mp4")) is None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_rendered_proxy_duration_matches_the_timeline(bars, tmp_path):
    tl = _timeline(bars, frames=48)                    # exactly 2.0s at 24fps
    out = tmp_path / "proxy.mp4"
    result = render.render_proxy(tl, out_path=str(out), height=240)
    assert result["error"] is None
    assert result["path"] == str(out)
    assert result["expected"] == Fraction(2, 1)
    assert result["duration"] is not None
    assert abs(result["drift"]) < Fraction(1, 24), result["drift"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_the_instrument_can_see_a_wrong_duration(bars, tmp_path):
    """Mutation check: a render of the wrong length must read differently.

    An instrument that reports the same on a correct and an incorrect render
    certifies nothing. Prove this one does not.
    """
    out = tmp_path / "short.mp4"
    render.render_proxy(_timeline(bars, frames=24), out_path=str(out), height=240)
    actual = render.probe_duration(str(out))
    assert actual is not None
    assert abs(actual - Fraction(2, 1)) > Fraction(1, 24)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_probe_duration_on_a_non_media_file_returns_none(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    assert render.probe_duration(str(junk)) is None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_render_frame_extracts_a_real_image(bars, tmp_path):
    out = tmp_path / "frame.png"
    assert render.render_frame(str(bars), Fraction(1, 1), str(out)) == str(out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_frame_on_a_missing_source_returns_none(tmp_path):
    assert render.render_frame(
        str(tmp_path / "nope.mov"), Fraction(0), str(tmp_path / "o.png")
    ) is None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_a_failing_ffmpeg_reports_its_own_stderr(bars, tmp_path, monkeypatch):
    """A broken render must say why, not just return no path."""
    from fcpxml import filtergraph

    def broken(graph, out_path, height=480):
        return ["ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-i", "/definitely/not/here.mov", out_path]

    monkeypatch.setattr(render, "graph_to_args", broken)
    result = render.render_proxy(_timeline(bars), out_path=str(tmp_path / "x.mp4"))
    assert result["path"] is None
    assert "ffmpeg failed" in result["error"]
    assert filtergraph is not None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_a_compiled_crossfade_renders_and_shortens_the_artifact(bars, tmp_path):
    """The graph is only right if ffmpeg accepts it and the result overlaps.

    Every other transition test asserts the argument list. A malformed
    filter_complex passes all of them and fails only here, which is the whole
    reason this one runs the real binary.
    """
    fps = 24.0
    tl = Timeline(
        name="X", duration=Timecode(frames=96, frame_rate=fps), frame_rate=fps,
        clips=[
            Clip(name="a", start=Timecode(frames=0, frame_rate=fps),
                 duration=Timecode(frames=48, frame_rate=fps),
                 source_start=Timecode(frames=0, frame_rate=fps),
                 media_path=str(bars)),
            Clip(name="b", start=Timecode(frames=48, frame_rate=fps),
                 duration=Timecode(frames=48, frame_rate=fps),
                 source_start=Timecode(frames=0, frame_rate=fps),
                 media_path=str(bars)),
        ],
        transitions=[Transition(
            name="Cross Dissolve",
            duration=Timecode(frames=24, frame_rate=fps),
            start=Timecode(frames=48, frame_rate=fps),
        )],
    )
    out = tmp_path / "xfade.mp4"
    result = render.render_proxy(tl, out_path=str(out), height=240)

    assert result["error"] is None, result["error"]
    assert len(result["transitions"]) == 1
    # 2s + 2s overlapped by 1s.
    assert result["expected"] == Fraction(3, 1)
    assert result["duration"] is not None
    assert abs(result["drift"]) < Fraction(1, 8), result["drift"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_a_lane_overlay_renders_and_changes_the_picture(bars, tmp_path):
    """ffmpeg must accept the overlay chain, and the overlay must be visible.

    A graph that runs but composites nothing looks identical to a correct one
    on duration alone, so this compares a frame inside the lane's window
    against the same frame of a spine-only render.
    """
    fps = 24.0

    def timeline(with_lane):
        return Timeline(
            name="L", duration=Timecode(frames=96, frame_rate=fps), frame_rate=fps,
            clips=[Clip(
                name="a", start=Timecode(frames=0, frame_rate=fps),
                duration=Timecode(frames=48, frame_rate=fps),
                source_start=Timecode(frames=0, frame_rate=fps),
                media_path=str(bars),
            )],
            connected_clips=[ConnectedClip(
                name="broll", start=Timecode(frames=0, frame_rate=fps),
                duration=Timecode(frames=24, frame_rate=fps), lane=1,
                offset=Timecode(frames=24, frame_rate=fps),
                source_start=Timecode(frames=72, frame_rate=fps),
                media_path=str(bars),
            )] if with_lane else [],
        )

    plain, composited = tmp_path / "plain.mp4", tmp_path / "lane.mp4"
    assert render.render_proxy(
        timeline(False), out_path=str(plain), height=240)["error"] is None
    result = render.render_proxy(
        timeline(True), out_path=str(composited), height=240)
    assert result["error"] is None, result["error"]

    # A lane adds no timeline length.
    assert result["expected"] == Fraction(2, 1)
    assert abs(result["drift"]) < Fraction(1, 8), result["drift"]

    inside = tmp_path / "inside.png", tmp_path / "inside_plain.png"
    assert render.render_frame(str(composited), Fraction(3, 2), str(inside[0]))
    assert render.render_frame(str(plain), Fraction(3, 2), str(inside[1]))
    assert inside[0].read_bytes() != inside[1].read_bytes()
