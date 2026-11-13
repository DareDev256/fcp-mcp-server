"""The committed hero opens on the finished cut, and stays small (0.31).

The README shows the GIF inline, PyPI shows it, and a player shows an MP4's
first decoded frame before anything plays. The 0.31 first pass checked the
MP4's attached poster and called it done while frame 0 was still the empty
"waiting for a timeline" state; this decodes the frames themselves with
ffmpeg and compares them with ``docs/assets/hero-still.png`` (the finished
frame the recorder writes). The README GIF is its own cut of the timeline
alone, laid out at phone width so its type stays large there; its frame 0
is compared with ``hero-timeline-still.png``.

Round 5: no frame of either may be a dissolve. The round-4 hero
cross-faded whole re-renders, so for 200 ms two layouts showed at once
("SqSquat"); now every change is the app's own motion. A dissolve frame F
between two held pictures A and B is, pixel for pixel, a mix of them:
F = aA + (1-a)B. ``dissolve_frames`` fits that a and measures what the mix
leaves unexplained; a moving frame leaves a lot (0.5 or more measured), a
dissolve almost nothing (the round-4 assets: 0.17 at most).
"""

from __future__ import annotations

import io
import json
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
LAYOUT = ASSETS / "hero-layout.json"
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
    """Mutation check: a frame from the story's start (1.5 s of the finished
    cut, then the loop starts over: the prompt being typed, no timeline yet)
    reads as different from the finished cut."""
    assert difference(mp4_frame(1.9), Image.open(STILL)) > DIFFERENT


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


def test_the_gif_is_its_own_timeline_cut_and_under_5_mb():
    """The ruler and lanes alone, not the 1720 px frame with the chat beside
    it, and short: about nine seconds, no hold longer than three."""
    still = Image.open(STILL)
    with Image.open(GIF) as g:
        assert g.size == Image.open(TIMELINE_STILL).size
        assert 1000 <= g.width < still.width
        assert g.height < still.height / 2
        holds = []
        for i in range(g.n_frames):
            g.seek(i)
            holds.append(g.info.get("duration", 0))
    assert 7000 <= sum(holds) <= 11000, sum(holds)
    assert max(holds) <= 3000, holds
    assert GIF.stat().st_size <= 5_000_000


def edge_problems(gif_bytes: bytes, rows: int = 4, left: int = 60, spread: int = 16) -> list[int]:
    """GIF frames where something is cut by the bottom edge: the crop ends
    3 CSS px under the spine, inside the panel, so its last rows are one flat
    colour (the playhead, which runs off the bottom by design, is left of
    *left*). The round-4 first push sliced the panel under the timeline here."""
    bad = []
    with Image.open(io.BytesIO(gif_bytes)) as g:
        for i in range(g.n_frames):
            g.seek(i)
            band = g.convert("L").crop((left, g.height - rows, g.width - 4, g.height)).tobytes()
            if max(band) - min(band) > spread:
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


GREY_W = 160


def _grey_vec(im: Image.Image, box=None) -> list[int]:
    im = im.convert("L")
    if box:
        im = im.crop(box)
    return list(im.resize((GREY_W, max(2, round(im.height * GREY_W / im.width))), Image.Resampling.BILINEAR).tobytes())


def blend_fit(f: list[int], a: list[int], b: list[int]):
    """(a, unexplained): the best mix F ~ aA + (1-a)B and what it leaves, as a
    fraction of how far A is from B. None when A and B are the same picture."""
    d = [x - y for x, y in zip(a, b)]
    dd = sum(v * v for v in d)
    if dd < 1e-6:
        return None
    fb = [x - y for x, y in zip(f, b)]
    alpha = sum(p * q for p, q in zip(fb, d)) / dd
    return alpha, (sum((p - alpha * q) ** 2 for p, q in zip(fb, d)) / dd) ** 0.5


def is_dissolve(fit) -> bool:
    return fit is not None and 0.1 < fit[0] < 0.9 and fit[1] < 0.3


def dissolve_frames(frames: list[list[int]], holds: list[int]) -> list[int]:
    """Frames between two held pictures that are a mix of them. Holds wrap
    round (the loop's last change runs into frame 0)."""
    bad = []
    for i in range(len(frames)):
        if i in holds or not holds:
            continue
        a = max((h for h in holds if h < i), default=holds[-1])
        b = min((h for h in holds if h > i), default=holds[0])
        if is_dissolve(blend_fit(frames[i], frames[a], frames[b])):
            bad.append(i)
    return bad


def gif_dissolves(data: bytes) -> list[int]:
    frames, holds = [], []
    with Image.open(io.BytesIO(data)) as g:
        for i in range(g.n_frames):
            g.seek(i)
            frames.append(_grey_vec(g.convert("RGB")))
            if g.info.get("duration", 0) >= 300:
                holds.append(i)
    if not holds:  # a fixed-rate GIF: a picture that does not change is a hold
        holds = [i for i in range(1, len(frames) - 1) if frames[i] == frames[i - 1] == frames[i + 1]]
    return dissolve_frames(frames, holds)


def mp4_dissolves(path: Path, box) -> list[int]:
    """The timeline's box of every decoded frame; a frame no different from
    both neighbours (mean grey step under 0.6) is a hold."""
    x, y, w, h = box
    hh = round(h * GREY_W / w)
    hh += hh % 2
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-vf",
                          f"crop={w}:{h}:{x}:{y},scale={GREY_W}:{hh},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True, timeout=120).stdout
    step = GREY_W * hh
    frames = [list(raw[i:i + step]) for i in range(0, len(raw) - step + 1, step)]
    still = [sum(abs(p - q) for p, q in zip(frames[i], frames[i + 1])) / step < 0.6 for i in range(len(frames) - 1)]
    holds = [i for i in range(1, len(frames) - 1) if still[i - 1] and still[i]]
    return dissolve_frames(frames, holds)


def _old(rev: str, name: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    old = subprocess.run([git, "show", f"{rev}:docs/assets/{name}"], capture_output=True, cwd=ASSETS.parent.parent, timeout=60)
    if old.returncode != 0:
        pytest.skip(f"{rev} is not in this clone (shallow checkout)")
    return old.stdout


def test_no_gif_frame_is_a_dissolve():
    assert gif_dissolves(GIF.read_bytes()) == []


def test_the_dissolve_check_sees_the_round_4_gif():
    """Mutation check: the GIF that cross-faded whole re-renders fails it."""
    assert len(gif_dissolves(_old("f880e40", "hero-watch-claude-cut.gif"))) >= 4


@needs_ffmpeg
def test_no_mp4_frame_of_the_timeline_is_a_dissolve():
    box = json.loads(LAYOUT.read_text())["mp4"]["timeline"]
    assert mp4_dissolves(MP4, box) == []


@needs_ffmpeg
def test_the_dissolve_check_sees_the_round_4_mp4(tmp_path):
    """Mutation check: the MP4 whose four changes were 180 ms cross-fades
    fails it (its timeline sat at 700,240 930x320 in a 1720x1200 frame)."""
    old = tmp_path / "old.mp4"
    old.write_bytes(_old("f880e40", "hero-watch-claude-cut.mp4"))
    assert len(mp4_dissolves(old, (700, 240, 930, 320))) >= 8


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
