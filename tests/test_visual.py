"""The verification instrument: preview_check (fcpxml/proofs.visual_check).

The timeline picture is drawn from the XML: a picture of what we wrote. A
fix (fix_flash_frames, remove_media_silence) cannot be confirmed by re-reading
our own output, so this reads the MEDIA: two rows of frames, a labelled time
ruler, the cut marked, the waveform with its silences named. 0.32 replaced the
ffmpeg tile/showwavespic montage (fcpxml/visual.py, deleted) with a composed
image the model can read; these tests read the PNG it wrote, every pixel row of
the parts they claim.
"""

import shutil
import subprocess
from fractions import Fraction

import pytest

from fcpxml import proofs
from fcpxml import raster as rs

FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def _make(out, video_seconds, audio=None):
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=24:duration={video_seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", audio]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    subprocess.run(args + [str(out)], capture_output=True, timeout=120, check=True)
    return out


@pytest.fixture
def bars_with_audio(tmp_path):
    return _make(tmp_path / "bars.mp4", 4, "aevalsrc='0.5*sin(2*PI*440*t)':s=48000:d=4")


@pytest.fixture
def bars_silent(tmp_path):
    """A video-only source. The waveform half has no input."""
    return _make(tmp_path / "silent.mp4", 3)


@pytest.fixture
def bars_quiet(tmp_path):
    """Same picture as bars_with_audio, near-silent sound: any difference
    between the two checks is the waveform half."""
    return _make(tmp_path / "quiet.mp4", 4, "anoisesrc=amplitude=0.001:duration=4")


@pytest.fixture
def tone_gap_tone(tmp_path):
    """1.5 s of tone, 1 s of silence, 1.5 s of tone."""
    return _make(tmp_path / "gap.mp4", 4,
                 "aevalsrc='if(between(t,1.5,2.5),0,0.5*sin(2*PI*440*t))':s=48000:d=4")


def _check(source, start, end, out, **kw):
    return proofs.visual_check(str(source), Fraction(start), Fraction(end), str(out), **kw)


# ---- refusals ---------------------------------------------------------------------

def test_missing_ffmpeg_returns_no_path(monkeypatch, tmp_path):
    monkeypatch.setattr(proofs.shutil, "which", lambda name: None)
    assert _check(tmp_path / "x.mov", 0, 1, tmp_path / "o.png")["path"] is None


def test_missing_source_returns_no_path(tmp_path):
    assert _check(tmp_path / "nope.mov", 0, 1, tmp_path / "o.png")["path"] is None


def test_reversed_range_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="end must be after start"):
        _check(tmp_path / "x.mov", 2, 1, tmp_path / "o.png")


@pytest.mark.parametrize("frames", [1, 3, 14, 500])
def test_frames_must_fill_two_rows_of_at_most_six(tmp_path, frames):
    with pytest.raises(ValueError, match="frames"):
        _check(tmp_path / "x.mov", 0, 1, tmp_path / "o.png", frames=frames)


def test_a_cut_outside_the_range_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="cut must be inside"):
        _check(tmp_path / "x.mov", 0, 1, tmp_path / "o.png", cut=Fraction(2))


# ---- the image ----------------------------------------------------------------------

@needs_ffmpeg
def test_two_rows_of_frames_at_least_240_wide_inside_the_budget(bars_with_audio, tmp_path):
    out = tmp_path / "check.png"
    result = _check(bars_with_audio, 0, 2, out)
    assert result["path"] == str(out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = rs.image_size(str(out))
    assert w == proofs.CHECK_WIDTH and w <= rs.LONG_EDGE_MAX and h <= rs.LONG_EDGE_MAX
    assert rs.visual_tokens(w, h) <= rs.VISUAL_TOKENS_MAX
    per_row = 12 // 2
    frame_w = (proofs.CHECK_WIDTH - 2 * 16 - (per_row - 1) * 8) // per_row
    assert frame_w >= 240
    assert result["problems"] == []


@needs_ffmpeg
def test_the_frame_times_are_written_under_each_frame(bars_with_audio, tmp_path):
    """Read every frame's label back off the PNG and compare it with the time
    the frame was taken at."""
    out = tmp_path / "check.png"
    result = _check(bars_with_audio, 0, 4, out, fps=24)
    png = rs.load(str(out))
    label = rs.face(24)
    times = [Fraction(4) * (2 * i + 1) / 24 for i in range(12)]
    under = sorted((run for run in result["raster"].runs if run.face.cell_h == label.cell_h
                    and run.y < result["geometry"]["ruler_y"]), key=lambda run: (run.y, run.x))
    assert [run.text for run in under] == [proofs.timecode(t, 24) for t in times]
    for run in under:
        assert rs.read_text(png, label, run.x, run.y, len(run.text)) == run.text


@needs_ffmpeg
def test_a_silent_source_still_produces_a_filmstrip(bars_silent, tmp_path):
    out = tmp_path / "silent.png"
    result = _check(bars_silent, 0, 1, out)
    assert result["path"] == str(out)
    assert result["audio"] is False
    assert any(run.text == "no audio in this source" for run in result["raster"].runs)


@needs_ffmpeg
def test_the_instrument_distinguishes_different_ranges(bars_with_audio, tmp_path):
    """testsrc animates and counts frames: an instrument returning the same
    bytes for 0-1 s and 3-4 s would be reading nothing."""
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    _check(bars_with_audio, 0, 1, first)
    _check(bars_with_audio, 3, 4, second)
    assert first.read_bytes() != second.read_bytes()


@needs_ffmpeg
def test_the_waveform_is_drawn_in_the_accent_on_black_with_a_baseline(bars_with_audio, bars_quiet, tmp_path):
    """Same picture, different sound: the frames are identical, so every
    difference must be in the waveform band. A 440 Hz tone fills the band in
    the accent on every column; near-silence leaves only the baseline."""
    loud = _check(bars_with_audio, 0, 4, tmp_path / "loud.png")
    quiet = _check(bars_quiet, 0, 4, tmp_path / "quiet.png")
    g = loud["geometry"]
    mid = g["wave_y"] + g["wave_h"] // 2
    loud_png, quiet_png = rs.load(loud["path"]), rs.load(quiet["path"])
    for x in range(g["x0"], g["x1"]):  # every column
        assert loud_png.get(x, mid - 20)[:3] == pytest.approx(proofs.ACCENT, abs=12), x
        assert quiet_png.get(x, mid - 20)[:3] != pytest.approx(proofs.ACCENT, abs=40), x
        assert quiet_png.get(x, mid)[:3] == pytest.approx(proofs.LINE_STRONG, abs=12), x
        assert loud_png.get(x, g["wave_y"] + 1)[:3] == pytest.approx(proofs.GROUND, abs=12), x
    # the frames, their times and the ruler (everything below the header,
    # which names the file, and above the waveform) are the same pixels
    top, bottom = 16 + rs.face(32).height + 8, g["wave_y"]
    row = loud_png.width * loud_png.channels
    assert loud_png.px[top * row:bottom * row] == quiet_png.px[top * row:bottom * row]


@needs_ffmpeg
def test_silence_spans_are_found_and_labelled(tone_gap_tone, tmp_path):
    result = _check(tone_gap_tone, 0, 4, tmp_path / "gap.png")
    assert len(result["spans"]) == 1
    a, b = result["spans"][0]
    assert abs(float(a) - 1.5) <= 0.06 and abs(float(b) - 2.5) <= 0.06
    png, label = rs.load(result["path"]), rs.face(24)
    runs = [run for run in result["raster"].runs if run.text.startswith("silence")]
    assert [run.text for run in runs] == ["silence 1.0s"]
    assert rs.read_text(png, label, runs[0].x, runs[0].y, len(runs[0].text)) == "silence 1.0s"


@needs_ffmpeg
def test_the_cut_is_marked_and_the_first_frame_after_it_is_named(bars_with_audio, tmp_path):
    result = _check(bars_with_audio, 0, 4, tmp_path / "cut.png", cut=Fraction(2), fps=24)
    g, png = result["geometry"], rs.load(result["path"])
    cut_x = result["cut_x"]
    assert cut_x == g["x0"] + round((g["x1"] - 1 - g["x0"]) / 2)
    for y in range(g["wave_y"], g["wave_y"] + g["wave_h"]):  # every row of the line
        assert png.get(cut_x, y)[:3] == pytest.approx(proofs.ACCENT, abs=12), y
    assert any(run.text == "cut 00:00:02:00" for run in result["raster"].runs)
    assert result["problems"] == []
