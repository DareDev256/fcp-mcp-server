"""The README hero, as committed, keeps the promises of 0.31 rounds 6 and 7.

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
- the chat-client recording stays linked as the unedited recording, and it
  is the SAME run as the close-up: both were written from one states.json
  and each shows the run's calls and notes, results and thumbnails
  (hero-run.json, with each file's sha256);
- round 7: every result is held fully typed 1.5 s or more (the prompt 1.8),
  the split moves for 12 frames or more, every push is 1.25-1.4x, and no
  frame changes the clips on screen by more than one without motion on both
  sides (the recorder measures these per frame and refuses; the numbers it
  saw are in hero-layout.json and are held here).
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
RUN = ASSETS / "hero-run.json"
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
    """Mutation check: the empty stage (3.9 s in, after the rewind) reads as different."""
    assert difference(mp4_frame(3.9), Image.open(STILL)) > DIFFERENT


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
        assert 800 <= g.width < still.width
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


def gif_frames() -> tuple[list[Image.Image], list[int]]:
    out, ms = [], []
    with Image.open(GIF) as g:
        for i in range(g.n_frames):
            g.seek(i)
            out.append(g.convert("RGB"))
            ms.append(g.info.get("duration", 0))
    return out, ms


def test_the_gif_holds_one_frame_at_a_time_and_loops():
    """Round 7, on the GIF itself: a still picture is ONE frame with its delay
    (Pillow merges repeats), none over 1.5 s, the frames add up to the MP4's
    length, and its last frame is its first."""
    frames, ms = gif_frames()
    assert max(ms) <= HOLD_MAX * 1000, max(ms)
    assert abs(sum(ms) / 1000 - json.loads(LAYOUT.read_text())["stage"]["frames"] / 30) < 0.1
    repeats = sum(1 for a, b in zip(frames, frames[1:]) if ImageChops.difference(a, b).getbbox() is None)
    assert repeats == 0, f"{repeats} frames repeat the one before instead of lengthening its delay"
    assert difference(frames[-1], frames[0]) < SAME


CAPTION = (0, 0, 1280, 124)  # the caption band over the stage (CSS 0-78 at 1.6x)


def caption_holds(frames: list[list[int]], fps: float) -> list[tuple[float, float]]:
    """(start, seconds) of every run of the caption band unchanged for half a
    second or more, round the loop: the captions as a viewer reads them."""
    n = len(frames)
    still = [mean_step(frames[i], frames[(i + 1) % n]) < 0.2 for i in range(n)]  # (a keyframe refresh moves it ~0.1; a typed character 0.3+)
    k = still.index(False) + 1  # start just after a change
    runs, j = [], 0
    while j < n:
        if still[(k + j) % n]:
            start = j
            while j < n and still[(k + j) % n]:
                j += 1
            if (j - start + 1) / fps >= 0.5:
                runs.append((((k + start) % n) / fps, (j - start + 1) / fps))
        j += 1
    return runs


@needs_ffmpeg
def test_every_caption_is_held_to_be_read_in_the_pixels():
    """The recorder's read-time gate, checked again on the MP4's pixels: seven
    held captions (the prompt and six results), each 1.5 s or more, the
    prompt's (the one held at 3.5 s) 1.8 s or more."""
    runs = caption_holds(mp4_grey_frames(MP4, CAPTION), 30)
    assert len(runs) == 7, runs
    assert min(sec for _t, sec in runs) >= 1.5 - 1e-6, runs
    prompt = [sec for t, sec in runs if t <= 3.5 < t + sec]
    assert prompt and prompt[0] >= 1.8 - 1e-6, runs


@needs_ffmpeg
def test_the_caption_check_sees_a_hold_cut_short(tmp_path):
    """Mutation check: 0.6 s cut out of the edit's held result fails it."""
    cut = tmp_path / "cut.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(MP4), "-filter_complex",
                    "[0:v:0]trim=0:8.0,setpts=PTS-STARTPTS[a];[0:v:0]trim=8.6,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(cut)], check=True, timeout=120)
    runs = caption_holds(mp4_grey_frames(cut, CAPTION), 30)
    assert min(sec for _t, sec in runs) < 1.5, runs


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


def tile_alphas(f: list[int], a: list[int], b: list[int], width: int, cols: int = 8, rows: int = 4) -> list[float]:
    """The mix a, fitted tile by tile (8x4) where A and B differ (a mean of 4
    grey levels or more). A dissolve mixes every tile alike; motion, and
    things that come or go in turn, leave some tiles A and others B."""
    h, out = len(f) // width, []
    for r in range(rows):
        for c in range(cols):
            idx = [y * width + x for y in range(r * h // rows, (r + 1) * h // rows)
                   for x in range(c * width // cols, (c + 1) * width // cols)]
            d = [a[i] - b[i] for i in idx]
            dd = sum(v * v for v in d)
            if not idx or dd / len(idx) < 16:
                continue
            out.append(sum((f[i] - b[i]) * v for i, v in zip(idx, d)) / dd)
    return out


def is_dissolve(fit, alphas=None) -> bool:
    if fit is None or not (0.1 < fit[0] < 0.9 and fit[1] < 0.3):
        return False
    # (round 7: clips leaving in turn, most of them still there, also fit as a
    # mix overall; tile by tile they are not one)
    return alphas is None or len(alphas) < 2 or max(alphas) - min(alphas) < 0.3


def dissolve_frames(frames: list[list[int]], holds: list[int], width: int = 160) -> list[int]:
    bad = []
    for i in range(len(frames)):
        if i in holds or not holds:
            continue
        a = max((h for h in holds if h < i), default=holds[-1])
        b = min((h for h in holds if h > i), default=holds[0])
        fit = blend_fit(frames[i], frames[a], frames[b])
        if is_dissolve(fit) and is_dissolve(fit, tile_alphas(frames[i], frames[a], frames[b], width)):
            bad.append(i)
    return bad


# A held picture: under 0.1 grey levels from its neighbours (an unchanging
# frame of this encode measures 0.00 to 0.02). A small-region dissolve moves
# about 0.3 a frame, so a looser line would count its frames as holds.
HELD_STEP = 0.1


def mp4_dissolves(path: Path, box, skip_last: int = 0) -> list[int]:
    frames = mp4_grey_frames(path, box)
    if skip_last:
        frames = frames[:-skip_last]
    still = [mean_step(frames[i], frames[i + 1]) < HELD_STEP for i in range(len(frames) - 1)]
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


# ---- round 7: read time, the split's motion, the camera, the clip count ---------------

def stage_problems(stage: dict) -> list[str]:
    out = []
    holds = stage.get("caption_holds_s", {})
    if len(holds) != 7:
        out.append(f"{len(holds)} captions measured, not the prompt and six results")
    for name, seconds in holds.items():
        need = 1.8 if name == "prompt" else 1.5
        if seconds < need:
            out.append(f"{name} held {seconds} s (at least {need})")
    if stage.get("split_frames", 0) < 12:
        out.append(f"the split moves for {stage.get('split_frames')} frames (at least 12)")
    pushes = stage.get("pushes", {})
    if set(pushes) != {"squat", "markers<", "markers>", "broll"} or not all(1.25 <= v <= 1.4 for v in pushes.values()):
        out.append(f"pushes {pushes} (1.25-1.4x onto the squat, along the markers and onto the B-roll)")
    if stage.get("frames", 0) / stage.get("fps", 30) < 16:
        out.append(f"the loop is {stage.get('frames', 0) / stage.get('fps', 30):.1f} s (about 16 to 18)")
    return out


def test_each_result_is_held_long_enough_to_read_and_the_edit_moves():
    assert stage_problems(json.loads(LAYOUT.read_text())["stage"]) == []


def test_the_read_time_rules_can_go_red():
    stage = json.loads(LAYOUT.read_text())["stage"]
    for broken in ({"caption_holds_s": dict(stage["caption_holds_s"], prompt=1.2)},
                   {"split_frames": 4},                       # the round-6 snap
                   {"pushes": dict(stage["pushes"], squat=1.08)},  # the round-6 push
                   {"frames": 306}):                          # the round-6 10.2 s
        assert stage_problems(dict(stage, **broken)) != [], broken


# ---- honesty ---------------------------------------------------------------------------

def test_the_unedited_recording_is_kept_and_linked():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert UNEDITED.is_file() and UNEDITED.stat().st_size > 100_000
    assert "docs/assets/hero-unedited-recording.mp4" in readme
    head = readme[:readme.index("**See it.**")]
    assert "composed" in head and "real" in head


def run_problems(assets: Path) -> list[str]:
    """Critic round 6: the staged hero said "2.83s removed", the linked
    unedited recording "3.83s" and other thumbnails, because they came from
    different runs. Both now come from one states.json; each recording's file
    is the one hero-run.json names, and each showed the run's calls and notes,
    result digests and thumbnail digests."""
    import hashlib
    record = json.loads((assets / "hero-run.json").read_text())
    run, out = record["run"], []
    for name in ("hero-watch-claude-cut.mp4", "hero-watch-claude-cut.gif", "hero-unedited-recording.mp4"):
        entry = record["recordings"].get(name)
        if entry is None:
            out.append(f"{name} is not in the run record")
            continue
        got = hashlib.sha256((assets / name).read_bytes()).hexdigest()
        if got != entry["sha256"]:
            out.append(f"{name} is not the file the run recorded ({got[:12]} vs {entry['sha256'][:12]})")
        for key in ("calls", "results", "thumbs"):
            if entry["shown"][key] != run[key]:
                out.append(f"{name} showed other {key} than the run")
    return out


def test_both_recordings_show_the_one_run():
    assert run_problems(ASSETS) == []
    calls = json.loads(RUN.read_text())["run"]["calls"]
    assert len(calls) == 6 and all(note for _call, note in calls)


def test_the_run_check_sees_another_runs_recording(tmp_path):
    """Mutation check: the round-6 unedited recording (another run, "3.83s
    removed") in place of this one fails it; so does one note changed in what
    the close-up showed."""
    for name in ("hero-watch-claude-cut.mp4", "hero-watch-claude-cut.gif", "hero-run.json"):
        (tmp_path / name).write_bytes((ASSETS / name).read_bytes())
    (tmp_path / "hero-unedited-recording.mp4").write_bytes(_old("fb55dd4", "hero-unedited-recording.mp4"))
    assert any("not the file" in p for p in run_problems(tmp_path))
    (tmp_path / "hero-unedited-recording.mp4").write_bytes(UNEDITED.read_bytes())
    record = json.loads(RUN.read_text())
    record["recordings"]["hero-watch-claude-cut.mp4"]["shown"]["calls"][1][1] = "2 rests cut, 3.83s removed"
    (tmp_path / "hero-run.json").write_text(json.dumps(record))
    assert any("showed other calls" in p for p in run_problems(tmp_path))


def test_the_card_frame_has_no_transparency():
    with Image.open(CARD_FRAME) as im:
        assert im.mode == "RGB"
