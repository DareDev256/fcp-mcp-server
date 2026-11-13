"""The committed hero opens on the finished cut, and stays small (0.31).

The README shows the GIF inline, PyPI shows it, and a player shows an MP4's
first decoded frame before anything plays. The 0.31 first pass checked the
MP4's attached poster and called it done while frame 0 was still the empty
"waiting for a timeline" state; this decodes the frames themselves with
ffmpeg and compares them with ``docs/assets/hero-still.png`` (the finished
frame the recorder writes).
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops, ImageStat  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"
MP4 = ASSETS / "hero-watch-claude-cut.mp4"
GIF = ASSETS / "hero-watch-claude-cut.gif"
STILL = ASSETS / "hero-still.png"
SAME = 6.0     # mean grey difference out of 255: re-encoding stays well under this
DIFFERENT = 15.0

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _grey(im: Image.Image) -> Image.Image:
    return im.convert("L").resize((240, 150), Image.Resampling.LANCZOS)


def difference(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(_grey(a), _grey(b))).mean[0]


def mp4_frame(at_seconds: float) -> Image.Image:
    """A decoded frame of the VIDEO stream (0:v:0), never the attached poster."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at_seconds}", "-i", str(MP4), "-map", "0:v:0", "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, check=True, timeout=60,
    ).stdout
    return Image.open(io.BytesIO(out))


@needs_ffmpeg
def test_the_mp4s_first_decoded_frame_is_the_finished_cut():
    assert difference(mp4_frame(0), Image.open(STILL)) < SAME


@needs_ffmpeg
def test_the_instrument_can_tell_the_story_from_the_finished_cut():
    """Mutation check: a frame from the story's start (the empty stage, the
    prompt being typed) reads as different from the finished cut."""
    assert difference(mp4_frame(4.0), Image.open(STILL)) > DIFFERENT


def test_the_gifs_first_frame_is_the_finished_cut():
    with Image.open(GIF) as g:
        g.seek(0)
        assert difference(g.convert("RGB"), Image.open(STILL)) < SAME


def test_the_gif_is_wide_and_under_5_mb():
    with Image.open(GIF) as g:
        assert g.width >= 1440
    assert GIF.stat().st_size <= 5_000_000


@needs_ffmpeg
def test_the_mp4_carries_the_finished_frame_as_its_poster_too():
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index:stream_disposition=attached_pic", "-of", "csv=p=0",
         str(MP4)], capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    assert "1,1" in out.replace(" ", "")
