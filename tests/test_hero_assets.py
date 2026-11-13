"""The README hero, as committed, keeps the promises of 0.31 round 6.

The hero is a composed stage around the REAL timeline view (demo/hero/
record.js): the app as served, fed real view_timeline results, every change
its own animation; the framing, captions and timing are composed. These
read the committed MP4 and GIF back, with ffmpeg and Pillow, and hold them
to the round-6 gates:

- frame 0 is the finished cut and is the poster (the MP4's first DECODED
  frame, not only its attached picture); the GIF is the same composition;
- no hold over 1.5 s anywhere, round the loop;
- a seamless loop: the last frame is frame 0 (the only crossfade is the
  0.3 s into it);
- no frame of the view is a dissolve: an in-between frame F of two held
  pictures A and B fitted as F = aA + (1-a)B leaves 0.17 or less unexplained
  when it is one (the round-4 assets), 0.5 or more when things move;
- the view covers at least 60% of the frame and no text is under 10 px when
  the frame is shown 358 px wide (measured by the recorder per frame,
  recorded in hero-layout.json, and checked here against the geometry);
- the chat-client recording stays linked as the unedited recording.
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

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
MP4 = ASSETS / "hero-watch-claude-cut.mp4"
GIF = ASSETS / "hero-watch-claude-cut.gif"
STILL = ASSETS / "hero-still.png"
UNEDITED = ASSETS / "hero-unedited-recording.mp4"
LAYOUT = ASSETS / "hero-layout.json"
CARD_FRAME = ASSETS / "card-frame.png"
SAME = 6.0     # mean grey difference out of 255: re-encoding stays well under this (0.6 measured)
DIFFERENT = 10.0  # the imported cut against the finished one measures ~11.6
HOLD_MAX = 1.5
PHONE = 358

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _grey(im: Image.Image) -> Image.Image:
    return im.convert("L").resize((240, 138), Image.Resampling.LANCZOS)


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


def mp4_grey_frames(path: Path, box=None, width: int = 160) -> list[list[int]]:
    """Every decoded frame, grey and small (optionally the box x, y, w, h only)."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    w0, h0 = (int(v) for v in probe.strip().split(",")[:2])
    x, y, w, h = box or (0, 0, w0, h0)
    hh = round(h * width / w)
    hh += hh % 2
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-vf",
                          f"crop={w}:{h}:{x}:{y},scale={width}:{hh},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True, timeout=120).stdout
    step = width * hh
    return [list(raw[i:i + step]) for i in range(0, len(raw) - step + 1, step)]


def mean_step(a: list[int], b: list[int]) -> float:
    return sum(abs(p - q) for p, q in zip(a, b)) / len(a)


# ---- frame 0, the poster, the same composition -------------------------------

@needs_ffmpeg
def test_the_mp4s_first_decoded_frame_is_the_poster_and_the_finished_cut():
    assert difference(mp4_frame(0), Image.open(STILL)) < SAME


@needs_ffmpeg
def test_the_instrument_can_tell_a_later_frame():
    """Mutation check: the imported cut (1.0 s in, after the rewind) reads as different."""
    assert difference(mp4_frame(1.0), Image.open(STILL)) > DIFFERENT


@needs_ffmpeg
def test_the_mp4_carries_the_poster_as_an_attached_picture_too():
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index:stream_disposition=attached_pic", "-of", "csv=p=0",
         str(MP4)], capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    assert "1,1" in out.replace(" ", "")


def test_the_gif_is_the_same_composition_scaled_and_under_5_mb():
    still = Image.open(STILL)
    with Image.open(GIF) as g:
        assert abs(g.width / g.height - still.width / still.height) < 0.01
        assert 900 <= g.width < still.width
        g.seek(0)
        assert difference(g.convert("RGB"), still) < SAME
    assert GIF.stat().st_size <= 5_000_000


# ---- holds and the loop ---------------------------------------------------------

def longest_hold(frames: list[list[int]], fps: float, still: float = 0.05) -> float:
    """Seconds of the longest run of unchanging frames, round the loop."""
    moving = [mean_step(frames[i], frames[(i + 1) % len(frames)]) >= still for i in range(len(frames))]
    if not any(moving):
        return len(frames) / fps
    k = moving.index(True) + 1
    run = longest = 0
    for j in range(len(moving)):
        run = run + 1 if not moving[(k + j) % len(moving)] else 0
        longest = max(longest, run)
    return (longest + 1) / fps


@needs_ffmpeg
def test_no_hold_lasts_over_one_and_a_half_seconds():
    assert longest_hold(mp4_grey_frames(MP4), 30) <= HOLD_MAX


def test_the_hold_check_sees_a_long_hold():
    """Mutation check: two seconds of one picture fails it."""
    a, b = [0] * 100, [9] * 100
    assert longest_hold([a] * 60 + [b] * 10, 30) > HOLD_MAX
    assert longest_hold([a, b] * 30, 30) <= HOLD_MAX


@needs_ffmpeg
def test_the_loop_is_seamless():
    frames = mp4_grey_frames(MP4)
    assert mean_step(frames[-1], frames[0]) < 1.5
    # the instrument sees a jump: the middle of the story against frame 0
    assert mean_step(frames[len(frames) // 2], frames[0]) > 5


# ---- no dissolves ------------------------------------------------------------------

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
    bad = []
    for i in range(len(frames)):
        if i in holds or not holds:
            continue
        a = max((h for h in holds if h < i), default=holds[-1])
        b = min((h for h in holds if h > i), default=holds[0])
        if is_dissolve(blend_fit(frames[i], frames[a], frames[b])):
            bad.append(i)
    return bad


def mp4_dissolves(path: Path, box, skip_last: int = 0) -> list[int]:
    frames = mp4_grey_frames(path, box)
    if skip_last:
        frames = frames[:-skip_last]
    still = [mean_step(frames[i], frames[i + 1]) < 0.6 for i in range(len(frames) - 1)]
    holds = [i for i in range(1, len(frames) - 1) if still[i - 1] and still[i]]
    return dissolve_frames(frames, holds)


def _old(rev: str, name: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    old = subprocess.run([git, "show", f"{rev}:docs/assets/{name}"], capture_output=True, cwd=REPO, timeout=60)
    if old.returncode != 0:
        pytest.skip(f"{rev} is not in this clone (shallow checkout)")
    return old.stdout


@needs_ffmpeg
def test_no_frame_of_the_view_is_a_dissolve():
    """Every change is the app's motion. The loop's seam (the last 0.3 s, a
    crossfade into frame 0, which is the same picture) is the one exception."""
    stage = json.loads(LAYOUT.read_text())["stage"]
    assert mp4_dissolves(MP4, stage["view"], skip_last=round(stage["seam_s"] * stage["fps"])) == []


@needs_ffmpeg
def test_the_dissolve_check_sees_the_round_4_mp4(tmp_path):
    """Mutation check: the round-4 MP4, whose four changes were 180 ms
    cross-fades, fails it (its timeline sat at 700,240 930x320 of 1720x1200)."""
    old = tmp_path / "old.mp4"
    old.write_bytes(_old("f880e40", "hero-watch-claude-cut.mp4"))
    assert len(mp4_dissolves(old, (700, 240, 930, 320))) >= 8


# ---- the view's share of the frame and the type size -----------------------------

def test_the_view_fills_the_frame_and_the_type_reads_at_358():
    stage = json.loads(LAYOUT.read_text())["stage"]
    (fw, fh), (_x, _y, w, h) = stage["size"], stage["view"]
    assert (w * h) / (fw * fh) >= 0.60, stage
    assert stage["min_coverage"] >= 0.60
    # the app's 10 px type, drawn at the stage's zoom, on a frame shown 358 px wide
    assert 10 * stage["zoom"] * (PHONE / (fw / 1.6)) >= 10 - 1e-6, stage
    assert stage["min_type_px_at_358"] >= 10


def test_the_coverage_rule_can_go_red():
    assert (600 * 300) / (1280 * 736) < 0.60  # a view this size would fail the rule above


# ---- honesty ---------------------------------------------------------------------------

def test_the_unedited_recording_is_kept_and_linked():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert UNEDITED.is_file() and UNEDITED.stat().st_size > 100_000
    assert "docs/assets/hero-unedited-recording.mp4" in readme
    head = readme[:readme.index("**See it.**")]
    assert "composed" in head and "real" in head


def test_the_card_frame_has_no_transparency():
    with Image.open(CARD_FRAME) as im:
        assert im.mode == "RGB"
