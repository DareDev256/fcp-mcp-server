"""The committed hero opens on the finished cut, and stays small (0.31).

The README shows the GIF inline, PyPI shows it, and a player shows an MP4's
first decoded frame before anything plays. The 0.31 first pass checked the
MP4's attached poster and called it done while frame 0 was still the empty
"waiting for a timeline" state; this decodes the frames themselves with
ffmpeg and compares them with ``docs/assets/hero-still.png`` (the finished
frame the recorder writes). The README GIF is the timeline alone, cropped
from the same frames so its type stays large at phone width; its frame 0 is
compared with ``hero-timeline-still.png``, the same crop of the finished
frame.
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
TIMELINE_STILL = ASSETS / "hero-timeline-still.png"
CARD_FRAME = ASSETS / "card-frame.png"
SAME = 6.0     # mean grey difference out of 255: re-encoding stays well under this (0.3 measured)
DIFFERENT = 10.0  # the story's opening frame against the finished cut measures ~14

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
    """Mutation check: a frame from the story's start (the imported cut of
    four clips, the prompt being typed, no calls yet) reads as different
    from the finished cut."""
    assert difference(mp4_frame(4.0), Image.open(STILL)) > DIFFERENT


def test_the_gifs_first_frame_is_the_finished_cut():
    with Image.open(GIF) as g:
        g.seek(0)
        assert difference(g.convert("RGB"), Image.open(TIMELINE_STILL)) < SAME


def test_the_gif_check_can_tell_a_later_frame():
    """Mutation check: the frame after the loop's seam (the imported cut,
    four clips, no markers) reads as different from the finished cut."""
    with Image.open(GIF) as g:
        finished = Image.open(TIMELINE_STILL)
        later = []
        for i in range(1, g.n_frames):
            g.seek(i)
            later.append(difference(g.convert("RGB"), finished))
    assert max(later) > DIFFERENT


def test_the_gif_is_the_timeline_crop_and_under_5_mb():
    """The timeline alone at full crop width (1000 px: the app's 10-11 px type
    is 20-22 px), not the whole 1720 px frame with the chat beside it."""
    still = Image.open(STILL)
    with Image.open(GIF) as g:
        assert g.size == Image.open(TIMELINE_STILL).size
        assert 1000 <= g.width < still.width
        assert g.height < still.height / 2
    assert GIF.stat().st_size <= 5_000_000


def edge_problems(gif_bytes: bytes, rows: int = 8, inset: int = 4, ground_max: int = 12) -> list[int]:
    """GIF frames with anything drawn in the bottom *rows* pixels (inside the
    frame's side borders). The crop ends 6 CSS px under the finished
    timeline; an earlier, shorter state must show nothing there either, or
    the panel under it is sliced by the edge (the round-4 first push did)."""
    bad = []
    with Image.open(io.BytesIO(gif_bytes)) as g:
        for i in range(g.n_frames):
            g.seek(i)
            f = g.convert("RGB")
            if max(f.crop((inset, f.height - rows, f.width - inset, f.height)).tobytes()) > ground_max:
                bad.append(i)
    return bad


def test_nothing_touches_the_gifs_bottom_edge():
    assert edge_problems(GIF.read_bytes()) == []


def test_the_edge_check_sees_a_sliced_panel():
    """Mutation check: the first round-4 GIF, whose early states showed the
    SELECTION panel cut by the crop, fails it."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    old = subprocess.run([git, "show", "adf2879:docs/assets/hero-watch-claude-cut.gif"], capture_output=True,
                         cwd=ASSETS.parent.parent, timeout=60)
    if old.returncode != 0:
        pytest.skip("adf2879 is not in this clone (shallow checkout)")
    assert edge_problems(old.stdout) != []


def test_the_card_frame_has_no_transparency():
    """Flattened onto the ground, so no host draws its own colour into the corners."""
    with Image.open(CARD_FRAME) as im:
        assert im.mode == "RGB"


@needs_ffmpeg
def test_the_mp4_carries_the_finished_frame_as_its_poster_too():
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index:stream_disposition=attached_pic", "-of", "csv=p=0",
         str(MP4)], capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    assert "1,1" in out.replace(" ", "")
