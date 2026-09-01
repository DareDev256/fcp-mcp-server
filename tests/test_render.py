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
from fcpxml.models import Clip, Timecode, Timeline

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


def test_render_with_all_media_missing_reports_rather_than_raises(tmp_path):
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
