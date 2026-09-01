"""The verification instrument.

fcpxml/preview.py draws coloured blocks from the XML — a picture of what we
wrote. That means fix_flash_frames and remove_media_silence were confirmed by
re-parsing our own output, which cannot see a bad cut. This module reads the
media, so a fix is confirmable from an image.
"""

import shutil
import subprocess
from fractions import Fraction

import pytest

from fcpxml import visual

FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def bars_with_audio(tmp_path):
    out = tmp_path / "bars.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(out)],
        capture_output=True, timeout=120, check=True,
    )
    return out


@pytest.fixture
def bars_silent(tmp_path):
    """A video-only source. The waveform half of the graph has no input."""
    out = tmp_path / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, timeout=120, check=True,
    )
    return out


def test_missing_ffmpeg_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(visual.shutil, "which", lambda name: None)
    assert visual.visual_check(
        str(tmp_path / "x.mov"), Fraction(0), Fraction(1), str(tmp_path / "o.png")
    ) is None


def test_missing_source_returns_none(tmp_path):
    assert visual.visual_check(
        str(tmp_path / "nope.mov"), Fraction(0), Fraction(1), str(tmp_path / "o.png")
    ) is None


def test_reversed_range_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="end must be after start"):
        visual.visual_check(
            str(tmp_path / "x.mov"), Fraction(2), Fraction(1), str(tmp_path / "o.png")
        )


def test_absurd_frame_count_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="frames"):
        visual.visual_check(
            str(tmp_path / "x.mov"), Fraction(0), Fraction(1),
            str(tmp_path / "o.png"), frames=500,
        )


def test_absurd_width_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="width"):
        visual.visual_check(
            str(tmp_path / "x.mov"), Fraction(0), Fraction(1),
            str(tmp_path / "o.png"), width=99999,
        )


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_produces_a_real_png(bars_with_audio, tmp_path):
    out = tmp_path / "check.png"
    assert visual.visual_check(
        str(bars_with_audio), Fraction(0), Fraction(2), str(out)
    ) == str(out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out.stat().st_size > 1000


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_silent_source_still_produces_a_filmstrip(bars_silent, tmp_path):
    """A video-only clip must not report 'no image' just because it is silent."""
    out = tmp_path / "silent.png"
    assert visual.visual_check(
        str(bars_silent), Fraction(0), Fraction(1), str(out)
    ) == str(out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_the_instrument_distinguishes_different_ranges(bars_with_audio, tmp_path):
    """Mutation check: two different ranges must not produce identical images.

    testsrc animates and counts frames, so an instrument returning the same
    bytes for 0-1s and 3-4s would be reading nothing and certifying everything.
    """
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    visual.visual_check(str(bars_with_audio), Fraction(0), Fraction(1), str(first))
    visual.visual_check(str(bars_with_audio), Fraction(3), Fraction(4), str(second))
    assert first.read_bytes() != second.read_bytes()


@pytest.fixture
def bars_quiet(tmp_path):
    """Same video as bars_with_audio, near-silent audio.

    Identical picture, different sound: any byte difference between this and
    the loud version must come from the waveform half of the image.
    """
    out = tmp_path / "quiet.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=4",
         "-f", "lavfi", "-i", "anoisesrc=amplitude=0.001:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(out)],
        capture_output=True, timeout=120, check=True,
    )
    return out


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_the_waveform_is_actually_drawn(bars_with_audio, bars_quiet, tmp_path):
    """Mutation check for the waveform half specifically.

    showwavespic draws on a transparent background that flattens to white, so
    a white trace is invisible while every ordinary check still passes: the
    PNG exists, it is valid, and two time ranges still differ because the
    FILMSTRIP differs. That is an instrument reporting success on a blank
    readout, which is the exact failure this suite exists to prevent.

    Both sources here have the same testsrc video and different audio, so the
    filmstrips are identical by construction. Any difference is the waveform.
    """
    loud = tmp_path / "loud.png"
    quiet = tmp_path / "quiet.png"
    visual.visual_check(str(bars_with_audio), Fraction(0), Fraction(4), str(loud))
    visual.visual_check(str(bars_quiet), Fraction(0), Fraction(4), str(quiet))
    assert loud.read_bytes() != quiet.read_bytes(), (
        "identical output for loud and near-silent audio means the waveform "
        "is not being drawn"
    )
