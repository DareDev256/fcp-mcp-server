"""Proof you can read (0.32): the gates on every picture and report the server makes.

Each gate reads the OUTPUT (the PNG written, every frame of the MP4 burned,
the MCP reply on the wire, the text a model reads) and compares it with a
truth computed HERE from the parsed timeline, with this file's own timecode
arithmetic: a cell's record timecode from the clip's start, a burned chip
from the clips present at that frame, a thumbnail's time from the ruler it
sits on, a picture flash from the clips' own frame ranges. Each was broken
once in the real code and seen red (the list is in the 0.32 CHANGELOG entry).

The real-footage fixture (demo/fixtures/gym_session.py: the author's own gym
session, silent proxies plus a synthesised tone) is built once per module.
Its connected Bench clip ends one frame before Press: frame 00:00:20:03
shows one frame of Squat, the picture flash the QC must find.
"""

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml import filtergraph, palette, proofs, render
from fcpxml import raster as rs
from fcpxml.parser import FCPXMLParser

REPO = Path(__file__).resolve().parent.parent
FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
TC = re.compile(r"\d\d:\d\d:\d\d:\d\d")
DIGITS = "0123456789:"


def tcf(frames: int, fps: int = 24) -> str:
    """A frame count as non-drop timecode: this file's own arithmetic, so the
    truth never shares code with what drew the picture."""
    s, f = divmod(int(frames), fps)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def tc(seconds, fps: int = 24) -> str:
    return tcf(int(Fraction(seconds) * fps + Fraction(1, 2)), fps)


def seconds_of(text: str, fps: int = 24) -> Fraction:
    h, m, s, f = (int(v) for v in text.split(":"))
    return Fraction((h * 3600 + m * 60 + s) * fps + f, fps)


def frames_of(timecode_obj) -> int:
    return int(timecode_obj.frames)


@contextlib.contextmanager
def _offline():
    """The fixture builds run outside any test's autouse isolation: keep the
    journal and the index off, and put the environment back after."""
    saved = {k: os.environ.get(k) for k in ("FCP_MCP_JOURNAL", "FCP_MCP_INDEX")}
    os.environ.update({"FCP_MCP_JOURNAL": "off", "FCP_MCP_INDEX": "off"})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
                   capture_output=True, timeout=120, check=True)


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    """Tool calls write their pictures here, never into ~/.fcp-mcp."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(render, "cache_dir", lambda: cache)


@pytest.fixture(scope="module")
def gym(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    spec = importlib.util.spec_from_file_location("gym_session", REPO / "demo" / "fixtures" / "gym_session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with _offline():
        built = module.build(tmp_path_factory.mktemp("gym"))
    tl = FCPXMLParser().parse_file(str(built["edited"])).timelines[0]
    return {**built, "timeline": tl}


@pytest.fixture(scope="module")
def burned(gym, tmp_path_factory):
    """The gym edit rendered at the default size with burn-in."""
    out = tmp_path_factory.mktemp("burn") / "gym_proxy.mp4"
    with _offline():
        return render.render_proxy(gym["timeline"], out_path=str(out), burn_in=True)


def _xml(path: Path, spine: str, assets: str, duration: str, name: str = "T") -> object:
    path.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
    <resources>
        <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
{assets}
    </resources>
    <library><event name="E"><project name="{name}">
        <sequence format="r1" duration="{duration}" tcStart="0s" tcFormat="NDF"><spine>
{spine}
        </spine></sequence>
    </project></event></library>
</fcpxml>''')
    return FCPXMLParser().parse_file(str(path)).timelines[0]


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    """testsrc, flat white, flat dark, and a loud tone under testsrc."""
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("synth")
    _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24:duration=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "bars.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=white:size=1920x1080:rate=24:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "white.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=0x202020:size=1920x1080:rate=24:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "dark.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=4",
        "-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*440*t)':s=48000:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-shortest", str(d / "tone.mov"))
    return d


def _assets(d: Path) -> str:
    return "\n".join(
        f'        <asset id="a{i}" name="{n}" start="0s" duration="{s}s" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file://{d}/{f}"/></asset>'
        for i, (n, f, s) in enumerate((("Bars", "bars.mp4", 30), ("White", "white.mp4", 4),
                                       ("Dark", "dark.mp4", 4)), start=1))


def _sheet(timeline, out: Path, layout: str, filepath: str = ""):
    graph = filtergraph.compile_timeline(timeline)
    return proofs.contact_sheet(timeline, graph, out, layout=layout, filepath=filepath, today="2026-11-13")


def _runs(raster, pattern, below: int = 0) -> list:
    rx = re.compile(pattern)
    return sorted((r for r in raster.runs if rx.fullmatch(r.text) and r.y >= below), key=lambda r: (r.y, r.x))


def _read(png, run, n=None, alphabet=rs.READABLE) -> str:
    return rs.read_text(png, run.face, run.x, run.y, n or len(run.text), alphabet=alphabet)


def _record_truth(tl) -> list:
    """(name, lane, start frame, length frames, source in frames, file) for
    every video clip, in record order (spine first at a tie), from the
    PARSED timeline."""
    rows = [(c.name, 0, frames_of(c.start), frames_of(c.duration), frames_of(c.source_start),
             Path(c.media_path or "").name) for c in tl.clips]
    rows += [(c.name, c.lane, frames_of(c.timeline_start), frames_of(c.duration), frames_of(c.source_start),
              Path(c.media_path or "").name) for c in tl.connected_clips]
    return sorted(rows, key=lambda r: (r[2], r[1] != 0, r[1]))


def _frame_by_index(source: str, k: int, w: int, h: int) -> rs.Raster:
    """Frame number ``k`` of a file, decoded by its index, fitted as raster.frame fits it."""
    vf = (f"select=eq(n\\,{k}),scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=rgb24")
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source),
                          "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
                         capture_output=True, timeout=120, check=True).stdout
    return rs.Raster(w, h, 3, bytearray(out[: w * h * 3]))


def _diff(a: rs.Raster, ax: int, ay: int, b: rs.Raster, w: int, h: int) -> float:
    """Mean absolute RGB difference of a w x h box of ``a`` at (ax, ay) and the top-left of ``b``."""
    total = 0
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            pa, pb = a.get(ax + x, ay + y), b.get(x, y)
            total += sum(abs(int(p) - int(q)) for p, q in zip(pa[:3], pb[:3]))
    count = len(range(0, h, 3)) * len(range(0, w, 3)) * 3
    return total / count


# ---- 1. the contact sheet ------------------------------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("layout", ["desktop", "phone"])
def test_every_record_timecode_read_off_the_sheet_is_the_timelines(gym, tmp_path, layout):
    """Every video clip is a cell, connected ones too, in record order; each
    cell's "rec" timecode read back off the PNG is the parsed clip's start.
    The phone page is also read at the size it is delivered (390 px)."""
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, layout, str(gym["edited"]))
    truth = [f"rec {tcf(r[2])}" for r in _record_truth(tl)]
    lay = sheet["layout"]
    read = []
    for path, raster in zip(sheet["pages"], sheet["rasters"]):
        png = rs.load(path)
        runs = _runs(raster, r"rec \d\d:\d\d:\d\d:\d\d")
        page = [_read(png, run) for run in runs]
        read += page
        if lay.delivered != 1:
            small = rs.resize(png, int(png.width * lay.delivered), int(png.height * lay.delivered))
            base = rs.face(24)
            for run, want in zip(runs, page):
                at = rs.read_text(small, base, int(run.x * lay.delivered), int(run.y * lay.delivered), len(want))
                assert at == want, f"unreadable at the delivered size: {at!r}"
    assert read == truth


@needs_ffmpeg
def test_the_connected_clip_has_its_own_cell_with_its_lane(gym, tmp_path):
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, "desktop", str(gym["edited"]))
    png, raster = rs.load(sheet["pages"][0]), sheet["rasters"][0]
    chips = [_read(png, run) for run in _runs(raster, r"#\d+( L\d+)?")]
    truth = [f"#{i}" + (f" L{r[1]}" if r[1] else "") for i, r in enumerate(_record_truth(tl), 1)]
    assert chips == truth
    assert "#5 L1" in chips


@needs_ffmpeg
def test_three_takes_of_one_clip_are_told_apart_by_their_source_range(gym, tmp_path):
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, "desktop", str(gym["edited"]))
    png, raster = rs.load(sheet["pages"][0]), sheet["rasters"][0]
    src = [_read(png, run) for run in _runs(raster, r"src \d\d:\d\d:\d\d:\d\d")]
    out = [_read(png, run) for run in _runs(raster, r"to  \d\d:\d\d:\d\d:\d\d")]
    truth = _record_truth(tl)
    assert src == [f"src {tcf(r[4])}" for r in truth]
    assert out == [f"to  {tcf(r[4] + r[3])}" for r in truth]
    squats = [s for s, r in zip(src, truth) if r[0] == "Squat"]
    assert len(squats) == 3 and len(set(squats)) == 3


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["desktop", "phone"])
def test_lengths_on_the_sheet_add_up_to_the_next_cut(gym, tmp_path, layout):
    """One rule: lengths are whole frames, so a spine cell's start plus its
    length is the next spine cell's start (5.0s hid 121f)."""
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, layout, str(gym["edited"]))
    starts, lengths = [], []
    lay, geo = sheet["layout"], sheet["geometry"]
    rights = {lay.margin + col * (geo["cell_w"] + lay.gutter) + geo["cell_w"] for col in range(lay.columns)}
    for path, raster in zip(sheet["pages"], sheet["rasters"]):
        png = rs.load(path)
        starts += [seconds_of(_read(png, r)[4:]) for r in _runs(raster, r"rec \d\d:\d\d:\d\d:\d\d")]
        # the length sits at the right edge of its cell's name line
        lengths += [int(_read(png, r)[:-1]) for r in _runs(raster, r"\d+f") if r.x + r.width in rights]
    truth = _record_truth(tl)
    spine = [(s, n) for (s, n), r in zip(zip(starts, lengths), truth) if r[1] == 0]
    for (a, n), (b, _) in zip(spine, spine[1:]):
        assert a + Fraction(n, 24) == b, (a, n, b)
    assert lengths == [r[3] for r in truth]


@needs_ffmpeg
def test_the_header_names_the_hash_and_the_counts(gym, tmp_path):
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, "desktop", str(gym["edited"]))
    png, raster = rs.load(sheet["pages"][0]), sheet["rasters"][0]
    header = [r for r in raster.runs if r.y < sheet["geometry"]["head_h"]]
    text = " ".join(_read(png, r) for r in header)
    sha = hashlib.sha256(Path(gym["edited"]).read_bytes()).hexdigest()
    assert f"sha256 {sha[:7]}" in text
    assert f"{len(tl.clips)} clips + {len(tl.connected_clips)} connected" in text


@needs_ffmpeg
def test_the_dissolve_shows_its_glyph_and_length(gym, tmp_path):
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, "desktop", str(gym["edited"]))
    (g,) = sheet["glyphs"]
    truth = _record_truth(tl)
    assert truth[g["into"] - 1][0] == "Finish"
    assert g["label"] == f"{frames_of(tl.transitions[0].duration)}f"
    png = rs.load(sheet["pages"][0])
    run = next(r for r in sheet["rasters"][0].runs if r.text == g["label"] and abs(r.x + r.width // 2 - g["x"]) < 4)
    assert _read(png, run) == g["label"]
    assert png.get(g["x"] - g["size"] // 2 + 1, g["y"])[:3] == pytest.approx(proofs.ACCENT, abs=24)


# ---- 2. legibility: glyphs >= 14 px delivered, long edge and patches in budget ------

def test_a_6px_font_is_refused():
    r = rs.Raster.filled(400, 100, proofs.GROUND)
    six = dataclasses.replace(rs.face(24), cap=6)
    r.text("00:00:01:00", six, 8, 8, proofs.INK)
    assert any("6" in p and "14" in p for p in rs.legibility_problems(r)), rs.legibility_problems(r)
    ok = rs.Raster.filled(400, 100, proofs.GROUND)
    ok.text("00:00:01:00", rs.face(24), 8, 8, proofs.INK)
    assert rs.legibility_problems(ok) == []


def test_a_page_over_the_budget_is_refused():
    assert rs.legibility_problems(rs.Raster.filled(1700, 400, proofs.GROUND))
    assert rs.legibility_problems(rs.Raster.filled(1500, 1500, proofs.GROUND))  # 2,916 patches
    assert rs.legibility_problems(rs.Raster.filled(1568, 560, proofs.GROUND)) == []
    assert rs.legibility_problems(rs.Raster.filled(400, 100, proofs.GROUND), cells=13)


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["desktop", "phone"])
def test_both_layouts_are_legible_at_their_delivered_size(gym, tmp_path, layout):
    sheet = _sheet(gym["timeline"], tmp_path, layout, str(gym["edited"]))
    lay = sheet["layout"]
    assert sheet["problems"] == []
    assert sheet["per_page"] <= rs.CELLS_MAX
    for raster in sheet["rasters"]:
        assert raster.width <= rs.LONG_EDGE_MAX and raster.height <= rs.LONG_EDGE_MAX
        assert rs.visual_tokens(raster.width, raster.height) <= rs.VISUAL_TOKENS_MAX
        for run in raster.runs:
            assert run.face.cap * run.face.scale * lay.delivered >= rs.GLYPH_MIN, run.text
    if layout == "phone":
        assert lay.width * lay.delivered == 390


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["desktop", "phone"])
def test_more_than_a_page_of_clips_paginates_with_no_collision(synth, tmp_path, layout):
    spine = "\n".join(f'            <asset-clip ref="a1" offset="{i}s" name="Shot {i + 1}" start="{i}s" duration="1s"/>'
                      for i in range(14))
    tl = _xml(tmp_path / "many.fcpxml", spine, _assets(synth), "14s", "Fourteen cuts")
    sheet = _sheet(tl, tmp_path, layout)
    assert len(sheet["pages"]) >= 2 and sheet["problems"] == []
    assert sheet["per_page"] <= rs.CELLS_MAX
    assert f"page {len(sheet['pages'])}/{len(sheet['pages'])}" in [r.text for r in sheet["rasters"][-1].runs]
    shown = []
    for raster in sheet["rasters"]:
        shown += [r.text[4:] for r in _runs(raster, r"rec \d\d:\d\d:\d\d:\d\d")]
    assert shown == [tc(i) for i in range(14)]


@needs_ffmpeg
def test_a_long_name_wraps_and_is_never_cut(synth, tmp_path):
    name = "A very long clip name from the camera roll take 12 of 40"
    spine = (f'            <asset-clip ref="a1" offset="0s" name="{name}" start="0s" duration="2s"/>\n'
             '            <asset-clip ref="a1" offset="2s" name="B" start="5s" duration="2s"/>')
    tl = _xml(tmp_path / "long.fcpxml", spine, _assets(synth), "4s")
    for layout in ("desktop", "phone"):
        sheet = _sheet(tl, tmp_path, layout)
        raster = sheet["rasters"][0]
        top = sheet["layout"].margin + sheet["geometry"]["head_h"]
        first_col = [r for r in raster.runs if r.y >= top and r.x == sheet["layout"].margin
                     and not re.match(r"(rec|src|to) ", r.text) and not r.text.startswith("#")]
        assert " ".join(r.text for r in sorted(first_col, key=lambda r: r.y)) == name
        assert sheet["problems"] == []


def test_collisions_are_seen():
    r = rs.Raster.filled(400, 100, proofs.GROUND)
    f = rs.face(24)
    r.text("Squat", f, 10, 10, proofs.INK)
    r.text("Press", f, 10 + f.measure("Squa"), 10, proofs.INK)
    assert rs.collisions(r)
    clear = rs.Raster.filled(400, 100, proofs.GROUND)
    clear.text("Squat", f, 10, 10, proofs.INK)
    clear.text("Press", f, 10 + f.measure("Squat "), 10, proofs.INK)
    assert rs.collisions(clear) == []


# ---- 3. counts, markers and edit points agree across outputs -----------------------

COUNTS = re.compile(r"(\d+) clips? \+ (\d+) connected")


@needs_ffmpeg
def test_the_clip_count_is_one_number_in_every_output(gym, tmp_path):
    import server
    from tools import preview as pv

    tl, path = gym["timeline"], str(gym["edited"])
    truth = (str(len(tl.clips)), str(len(tl.connected_clips)))
    timeline_text = asyncio.run(pv.handle_preview_timeline({"filepath": path}))[0].text
    analyze = asyncio.run(server.TOOL_HANDLERS["analyze_timeline"]({"filepath": path}))[0].text
    sheet = _sheet(tl, tmp_path, "desktop", path)
    png = rs.load(sheet["pages"][0])
    header = " ".join(_read(png, r) for r in sheet["rasters"][0].runs if r.y < sheet["geometry"]["head_h"])
    found = [COUNTS.search(t).groups() for t in (timeline_text.splitlines()[0], analyze, header)]
    assert found == [truth] * 3, found
    assert truth == ("6", "1")


@needs_ffmpeg
def test_inspect_counts_every_marker_and_says_what_the_edit_points_are(gym):
    import server

    tl = gym["timeline"]
    text = asyncio.run(server.TOOL_HANDLERS["analyze_timeline"]({"filepath": str(gym["edited"])}))[0].text
    markers = list(tl.markers) + [m for c in tl.all_clips() for m in c.markers]
    chapters = [m for m in markers if m.marker_type.value == "chapter"]
    assert f"**Total**: {len(markers)}" in text and len(markers) == 4
    assert f"**Chapters**: {len(chapters)}" in text
    assert "**Edit points**: 5 (4 cuts, 1 dissolve)" in text


# ---- 4. the check: bands on their own axis, removed spans, the cut, levels ----------

def _gym_check(gym, tmp_path, **kw):
    graph = filtergraph.compile_timeline(gym["timeline"])
    squat = str(gym["media"]["Squat"])
    kept = proofs.kept_ranges(graph, squat)
    return proofs.visual_check(squat, Fraction(3), Fraction(9), str(tmp_path / "check.png"), fps=24, kept=kept, **kw)


def _ruler_map(png, raster, band, x0, x1):
    """The time axis a band's ruler DRAWS, read off the PNG: its tick columns
    and the timecodes printed under them, fitted to t = a + b x."""
    y = band["y_ruler"] + 4
    cols = [x for x in range(x0, x1) if png.get(x, y)[:3] == pytest.approx(proofs.ACCENT, abs=40)]
    labels = [r for r in raster.runs if r.y == band["y_ruler"] + 10]
    pairs = []
    for run in labels:
        text = _read(png, run, alphabet=DIGITS)
        centre = run.x + run.width / 2
        x = min(cols, key=lambda c: abs(c - centre))
        if abs(x - centre) <= 2:
            pairs.append((x, seconds_of(text)))
    assert len(pairs) >= 2, pairs
    (xa, ta), (xb, tb) = pairs[0], pairs[-1]
    return lambda x: ta + (tb - ta) * (Fraction(str(x)) - xa) / (xb - xa)


@needs_ffmpeg
def test_every_thumbnail_sits_at_its_own_time_on_its_bands_ruler(gym, tmp_path):
    """Final Cut's filmstrip and waveform share one axis. Each band here is
    its frames over its own ruler: a thumbnail's centre, read through the
    ruler drawn under it, is the timecode printed under the thumbnail, to a
    frame. (Round 1 stacked two rows on a one-row ruler: 05:06 over 07:00.)"""
    result = _gym_check(gym, tmp_path)
    png, raster = rs.load(result["path"]), result["raster"]
    g = result["geometry"]
    for b, band in enumerate(result["bands"]):
        to_t = _ruler_map(png, raster, band, g["x0"], g["x1"])
        labels = [r for r in raster.runs if r.y == band["y_label"]]
        boxes = [f for f in result["frames"] if f["band"] == b]
        assert len(labels) == len(boxes) == 5
        for box, run in zip(boxes, sorted(labels, key=lambda r: r.x)):
            printed = seconds_of(_read(png, run, alphabet=DIGITS))
            centre = box["x"] + box["w"] / 2
            assert abs(to_t(centre) - printed) <= Fraction(1, 24), (b, float(to_t(centre)), float(printed))
            assert box["x"] <= run.x + run.width / 2 <= box["x"] + box["w"]
    assert result["problems"] == []


@needs_ffmpeg
@pytest.mark.parametrize("start, end", [(Fraction(1, 2), Fraction(37, 10)), (Fraction(0), Fraction(29)),
                                        (Fraction(3), Fraction(7, 2))])
def test_every_ruler_tick_is_within_a_pixel_of_its_time(synth, tmp_path, start, end):
    """Each band's ticks, read off every tick column and every row the tick
    claims, sit within a pixel of their time on that band's axis."""
    result = proofs.visual_check(str(synth / "bars.mp4"), start, end, str(tmp_path / "c.png"),
                                 cut=(start + end) / 2, fps=24)
    png, g = rs.load(result["path"]), result["geometry"]
    x0, x1, span = g["x0"], g["x1"], g["span"]
    for band in result["bands"]:
        b0, b1, y = band["start"], band["end"], band["y_ruler"]
        drawn = [x for x in range(x0, x1) if png.get(x, y + 4)[:3] == pytest.approx(proofs.ACCENT, abs=24)]
        assert len(drawn) == len(band["ticks"]) >= 2
        for (t, _), x in zip(band["ticks"], drawn):
            where = x0 + float((t - b0) / (b1 - b0)) * span
            assert abs(x - where) <= 1, f"tick for {t}s at {x}, its time is at {where:.2f}"
            for row in range(y + 1, y + 9):  # every row the tick claims
                assert png.get(x, row)[:3] == pytest.approx(proofs.ACCENT, abs=24), (x, row)
    assert result["problems"] == []


@needs_ffmpeg
def test_removed_frames_are_dimmed_and_the_cut_shows_both_kept_frames(gym, tmp_path):
    """Truth from the parsed clips of Squat.mov: the edit keeps
    [in, in + length) of each; between two kept pieces the frames are
    removed. Frames inside are dimmed and tagged; the first kept frame after
    the cut is highlighted; the pair names the last and first kept frames and
    the record time of the cut."""
    tl = gym["timeline"]
    pieces = sorted((frames_of(c.source_start), frames_of(c.source_start) + frames_of(c.duration), frames_of(c.start))
                    for c in tl.clips if c.name == "Squat")
    (_a_in, a_out, _), (b_in, _, b_rec) = pieces[0], pieces[1]
    result = _gym_check(gym, tmp_path)
    png, raster = rs.load(result["path"]), result["raster"]
    tags = [r for r in raster.runs if r.text == "removed"]
    for box in result["frames"]:
        k = int(box["t"] * 24 + Fraction(1, 2))
        gone = a_out <= k < b_in
        tagged = any(t.x == box["x"] + 4 and t.y == box["y"] + 1 for t in tags)
        assert tagged == gone, (tc(box["t"]), gone)
    after = [box for box in result["frames"] if int(box["t"] * 24 + Fraction(1, 2)) >= b_in]
    lit = [box for box in result["frames"] if png.get(box["x"] + 1, box["y"] + 60)[:3] == pytest.approx(proofs.ACCENT, abs=40)]
    assert lit == after[:1]
    lines = [_read(png, r) for r in raster.runs if r.y >= result["pair"]["y"] and r.x > 500]
    assert f"cut at rec {tcf(b_rec)}" in lines
    assert f"last kept  src {tcf(a_out - 1)}" in lines
    assert f"first kept src {tcf(b_in)}" in lines
    pair = [_read(png, r, alphabet=DIGITS) for r in raster.runs if r.y == result["pair"]["y"] + proofs.CHECK_FRAME_H + 4]
    assert pair == [tcf(a_out - 1), tcf(b_in)]


@needs_ffmpeg
def test_the_waveform_is_drawn_in_decibels_with_the_threshold(synth, tmp_path):
    """A 0.5-amplitude sine: RMS -9.03 dBFS, peak -6.02. On a -60..0 dB
    scale over ``half`` pixels each side of the baseline the RMS bar is
    round(51/60 * half) tall in the accent with the peak above it in grey,
    and the -40 dB threshold is a dashed line, on every column."""
    result = proofs.visual_check(str(synth / "tone.mov"), Fraction(0), Fraction(4), str(tmp_path / "t.png"), fps=24)
    png, g = rs.load(result["path"]), result["geometry"]
    half = proofs.WAVE_H // 2 - 2
    rms_h = round((60 - 9.0309) / 60 * half)
    thr_h = round((60 - 40) / 60 * half)
    for band in result["bands"]:
        mid = band["wave_mid"]
        for x in range(g["x0"], g["x1"]):
            assert png.get(x, mid - rms_h + 1)[:3] == pytest.approx(proofs.ACCENT, abs=40), x
            assert png.get(x, mid - rms_h - 3)[:3] != pytest.approx(proofs.ACCENT, abs=40), x
        dashes = [x for x in range(g["x0"], g["x1"]) if png.get(x, mid - thr_h)[:3] == pytest.approx(proofs.INK, abs=30)]
        assert len(dashes) >= (g["x1"] - g["x0"]) // 2 - 4


@needs_ffmpeg
def test_a_silence_is_named_once_and_its_box_is_neutral_grey(gym, tmp_path):
    result = _gym_check(gym, tmp_path)
    names = [r.text for r in result["raster"].runs if r.text.startswith("silence")]
    assert names == ["silence 1.5s"]
    png = rs.load(result["path"])
    band = result["bands"][0]
    x = result["geometry"]["x0"] + result["geometry"]["span"] - 40
    r, gg, b = png.get(x, band["y_wave"] + proofs.WAVE_H - 4)[:3]
    assert r == gg == b == palette.rgb(palette.dark("panel"))[0]


# ---- 5. the burn-in names what is on screen, every frame --------------------------

def _burn_truth(tl, k: int) -> tuple:
    """(line 1, line 2) a frame of the gym proxy must carry, from the parsed
    clips: the connected clip on top while it plays, over the spine clip it
    hides, else the spine clip; the source timecode of the top one."""
    spine = next(c for c in tl.clips if frames_of(c.start) <= k < frames_of(c.start) + frames_of(c.duration))
    lane = next((c for c in tl.connected_clips
                 if frames_of(c.timeline_start) <= k < frames_of(c.timeline_start) + frames_of(c.duration)), None)
    if lane is not None:
        what, top, start = f"{lane.name} (L{lane.lane}) over {spine.name}", lane, frames_of(lane.timeline_start)
    else:
        what, top, start = spine.name, spine, frames_of(spine.start)
    src = frames_of(top.source_start) + (k - start)
    return f"REC {tcf(k)}  {what}", f"SRC {Path(top.media_path).name} {tcf(src)}"


@needs_ffmpeg
def test_every_burned_frame_names_what_is_on_screen_and_its_source(gym, burned):
    tl = gym["timeline"]
    assert burned["error"] is None and burned["verified"] is True, burned
    assert burned["size"] == "1280x720"
    strips = proofs.burned_strips(burned["burned"], 1280, 720)
    total = frames_of(tl.clips[-1].start) + frames_of(tl.clips[-1].duration)
    assert len(strips) == total
    wrong = []
    for k, strip in enumerate(strips):  # every frame
        one, two = _burn_truth(tl, k)
        alphabet = "".join(sorted(set(one + two + DIGITS)))
        got = (proofs.read_burn(strip, 720, 0, len(one), alphabet), proofs.read_burn(strip, 720, 1, len(two), alphabet))
        if got != (one, two):
            wrong.append((k, got, (one, two)))
    assert wrong == [], wrong[:3]
    assert _burn_truth(tl, 432) == ("REC 00:00:18:00  Bench (L1) over Squat", "SRC Bench.mov 00:00:00:21")


@needs_ffmpeg
def test_the_chip_is_opaque_true_black(burned):
    """One chip style, opaque #000 (round 1 blended 70% black and read as
    warm charcoal, 57,57,55, over the bench footage)."""
    strips = proofs.burned_strips(burned["burned"], 1280, 720)
    for k in range(0, len(strips), 24):
        r, g, b = strips[k].get(2, 2)[:3]
        assert max(r, g, b) <= 10, (k, (r, g, b))


@needs_ffmpeg
def test_a_dissolve_after_hard_cuts_renders_in_the_full_graph(burned):
    """0.31 failed this graph: xfade refused the concat's 1/1000000 timebase
    against the clips' 1/24, so the proxy fell back to hard cuts and silence."""
    assert not any("refused" in note for note in burned["substitutions"]), burned["substitutions"]
    assert [t.kind for t in burned["transitions"]] == ["fade"]


@needs_ffmpeg
def test_the_poster_and_the_burned_letters_are_legible(burned):
    poster = rs.load(burned["poster"])
    assert (poster.width, poster.height) == (1280, 720)
    f = proofs.burn_face(720)
    assert f.cap * f.scale >= rs.GLYPH_MIN
    assert f.cap * f.scale * 880 / 1280 >= 12  # a README shows it at 880 px


@needs_ffmpeg
def test_burning_before_verify_lanes_would_fail_the_instrument(synth, tmp_path):
    """A white quarter-size picture-in-picture in the bottom-left corner over
    a dark spine. The burn-in chip covers its lower half: the instrument run
    on the burned file reports the lane NOT drawn. render_proxy must read the
    clean file, and does: the clean render verifies."""
    spine = '''            <asset-clip ref="a3" offset="0s" name="Dark" start="0s" duration="3s">
                <asset-clip ref="a2" lane="1" offset="0s" name="White" start="0s" duration="3s">
                    <adjust-transform position="-66.6667 -37.5" scale="0.25 0.25"/>
                </asset-clip>
            </asset-clip>'''
    tl = _xml(tmp_path / "pip.fcpxml", spine, _assets(synth), "3s")
    with _offline():
        result = render.render_proxy(tl, out_path=str(tmp_path / "pip.mp4"), burn_in=True)
    assert result["burned"], result.get("burn_error")
    assert [v for _, v, _ in result["lanes"]] == [True], result["lanes"]
    assert result["verified"] is True
    graph = filtergraph.compile_timeline(tl, render._probes(tl))
    on_burned = render.verify_lanes(graph, result["burned"], 720)
    assert [v for _, v, _ in on_burned] == [False], on_burned


# ---- 6. the frame burn and the scenes strip ----------------------------------------

def _call(tool, action, args):
    import server

    return asyncio.run(server.call_tool(tool, {"action": action, "args": args}))


def _images(blocks) -> list:
    out = []
    for b in blocks:
        if getattr(b, "type", "") != "image":
            continue
        wire = b.model_dump(by_alias=True, exclude_none=True)
        out.append((wire["mimeType"], base64.b64decode(wire["data"])))
    return out


def _png(data: bytes, tmp_path: Path, name: str = "block.png") -> rs.Raster:
    path = tmp_path / name
    path.write_bytes(data)
    return rs.load(str(path))


@needs_ffmpeg
def test_the_frame_burn_names_the_file_its_timecode_and_frame_number(gym, tmp_path):
    squat = str(gym["media"]["Squat"])
    reply = _call("preview", "preview_frame", {"source": squat, "at": 7.25, "burn": True})
    (_mime, data), = _images(reply)
    png = _png(data, tmp_path)
    f = rs.face(24)
    y = png.height - proofs.BURN_MARGIN - (f.height + proofs.BURN_PAD) + proofs.BURN_PAD // 2
    x = proofs.BURN_MARGIN + proofs.BURN_PAD
    want = f"SRC Squat.mov {tcf(174)}"
    assert rs.read_text(png, f, x, y, len(want)) == want
    assert rs.read_text(png, f, x + f.measure(want + " · "), y, 4) == "f174"


@needs_ffmpeg
@pytest.mark.parametrize("at", [7.25, 7.26, 3.0])
def test_the_frame_is_the_frame_its_timecode_names(synth, tmp_path, at):
    """testsrc2 changes every frame: the picture preview_frame returns is the
    frame round(at x 24), decoded here by its index, not the one after it."""
    bars = str(synth / "bars.mp4")
    reply = _call("preview", "preview_frame", {"source": bars, "at": at})
    (_mime, data), = _images(reply)
    png = _png(data, tmp_path)
    k = int(Fraction(str(at)) * 24 + Fraction(1, 2))
    same = _diff(png, 0, 0, _frame_by_index(bars, k, png.width, png.height), png.width, png.height)
    after = _diff(png, 0, 0, _frame_by_index(bars, k + 1, png.width, png.height), png.width, png.height)
    assert same < 3 and after > same + 3, (same, after)


@needs_ffmpeg
def test_the_scenes_strip_names_each_side_and_scores_each_cut(gym, tmp_path):
    reply = _call("scenes", "detect_scenes", {"filepath": str(gym["takes"])})
    text = reply[0].text
    times = [Fraction(t).limit_denominator(1000) for t in
             re.findall(r"\|\s*Montage\s*\|[^|]*\|\s*([0-9.]+)s?\s*\|", text)]
    assert len(times) >= 2, text
    (_mime, data), *_ = _images(reply)
    png = _png(data, tmp_path)
    f = rs.face(32)
    montage = str(Path(gym["takes"]).parent / "media" / "Montage.mov")
    # the detector's own score for each cut, measured here
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", montage, "-an", "-vf",
                            "select='gt(scene,0.3)',metadata=print", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120).stderr
    scores = [float(v) for v in re.findall(r"lavfi\.scene_score=([0-9.]+)", probe)]
    head_h = 2 * f.height + 8
    for i, t in enumerate(times):
        y = 16 + head_h + i * (225 + 12)
        k = int(t * 24 + Fraction(1, 2))
        before = rs.read_text(png, f, 16 + 6, y + 225 - f.height - 2, 11, alphabet=DIGITS)
        after = rs.read_text(png, f, 16 + 412 + 6, y + 225 - f.height - 2, 11, alphabet=DIGITS)
        assert (before, after) == (tcf(k - 1), tcf(k))
        tx = 16 + 2 * (400 + 12)
        assert rs.read_text(png, f, tx, y + f.height, 15) == f"rec {tcf(k)}"
        line = f"score {scores[i]:.2f} > 0.3"
        assert rs.read_text(png, f, tx, y + 2 * f.height, len(line)) == line
        # the before frame IS the last frame before the cut
        shot = _frame_by_index(montage, k - 1, 400, 225)
        assert _diff(png, 16, y, shot, 400, 150) < 6
        assert _diff(png, 16, y, _frame_by_index(montage, k, 400, 225), 400, 150) > 15
    runs_text = rs.read_text(png, f, 16, 16 + f.height, 50)
    assert "source = timeline" in runs_text
    bounds = [0] + [int(t * 24 + Fraction(1, 2)) for t in times] + [240]
    list_y = 16 + head_h + len(times) * (225 + 12)
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        want = f"{i}  {tcf(a)}  {b - a}f"
        assert rs.read_text(png, f, 16, list_y + i * f.height, len(want)) == want


# ---- 7. image blocks on the wire, each under its cap ----------------------------

def _size(data: bytes, tmp_path: Path) -> tuple:
    path = tmp_path / "block.bin"
    path.write_bytes(data)
    return rs.image_size(str(path))


@needs_ffmpeg
@pytest.mark.parametrize("action, args", [
    ("preview_sheet", lambda g: {"filepath": str(g["edited"])}),
    ("preview_sheet", lambda g: {"filepath": str(g["edited"]), "layout": "phone"}),
    ("preview_check", lambda g: {"source": str(g["media"]["Squat"]), "start": 3, "end": 9,
                                 "filepath": str(g["edited"])}),
    ("preview_frame", lambda g: {"source": str(g["media"]["Press"]), "at": 2, "burn": True}),
])
def test_each_preview_answers_with_an_image_block_under_its_cap(gym, tmp_path, action, args):
    from tools import preview as pv

    reply = _call("preview", action, args(gym))
    assert reply[0].type == "text"
    images = _images(reply)
    assert len(images) >= 1, reply[0].text
    for mime, data in images:
        assert mime in ("image/png", "image/jpeg")
        assert data[:8] == b"\x89PNG\r\n\x1a\n" if mime == "image/png" else data[:2] == b"\xff\xd8"
        assert len(data) <= pv.IMAGE_BLOCK_MAX_BYTES
        w, h = _size(data, tmp_path)
        assert max(w, h) <= rs.LONG_EDGE_MAX and rs.visual_tokens(w, h) <= rs.VISUAL_TOKENS_MAX


@needs_ffmpeg
def test_preview_render_answers_with_a_poster_and_a_link(synth, tmp_path, monkeypatch):
    from tools import preview as pv

    monkeypatch.setattr(pv, "_open_beside_terminal", lambda paths: None)
    spine = '            <asset-clip ref="a1" offset="0s" name="Bars" start="0s" duration="2s"/>'
    path = tmp_path / "short.fcpxml"
    _xml(path, spine, _assets(synth), "2s")
    reply = _call("preview", "preview_render", {"filepath": str(path), "burn_in": True})
    assert "1280x720" in reply[0].text
    assert len(_images(reply)) == 1
    links = [b for b in reply if getattr(b, "type", "") == "resource_link"]
    assert len(links) == 1
    uri = links[0].model_dump(by_alias=True)["uri"]
    assert str(uri).startswith("file://") and str(uri).endswith("_burnin.mp4")
    assert Path(str(uri)[len("file://"):]).is_file()


def test_the_journal_seam_keeps_the_pictures_when_it_rewrites_the_text():
    """_journaled rewrites a reply's leading text (the sha256 footer after a
    write, the autopush line, the unreviewed note). Until 0.32 it rebuilt the
    reply from that text alone; a write that also answers with a picture
    would have lost it. No write action returns a picture yet, so this is
    held at the helper every one of those rewrites goes through."""
    from mcp.types import ImageContent

    import server

    image = ImageContent(type="image", data=base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(), mimeType="image/png")
    reply = server._text_result("Saved to: x.fcpxml") + [image]
    out = server._with_text(reply, "Saved to: x.fcpxml\nsha256: 0")
    assert out[0].text.endswith("sha256: 0")
    assert out[1:] == [image]


@needs_ffmpeg
def test_an_image_over_the_cap_is_left_out_and_said_so(gym, monkeypatch):
    from tools import preview as pv

    monkeypatch.setattr(pv, "IMAGE_BLOCK_MAX_BYTES", 2_000)
    reply = _call("preview", "preview_sheet", {"filepath": str(gym["edited"])})
    assert _images(reply) == []
    assert "over the inline size cap" in reply[0].text


# ---- 8. one palette; contrast; red only for failure -------------------------------

def test_every_text_colour_the_proofs_use_reads_at_4_5_to_1():
    hexes = {k: palette.dark(k) for k in ("ground", "panel", "ink", "mute", "accent", "danger", "on-media")}
    for fg in ("ink", "mute", "accent", "danger", "on-media"):
        for bg in ("ground", "panel"):
            assert palette.contrast(hexes[fg], hexes[bg]) >= 4.5, (fg, bg)
    assert hexes["ground"] == "#000000" and hexes["accent"] == "#00E5FF"


@needs_ffmpeg
def test_red_appears_only_for_a_failure(synth, tmp_path):
    spine = ('            <asset-clip ref="a1" offset="0s" name="Here" start="0s" duration="2s"/>\n'
             '            <asset-clip ref="a9" offset="2s" name="Gone" start="0s" duration="2s"/>')
    assets = _assets(synth) + ('\n        <asset id="a9" name="Gone" start="0s" duration="4s" hasVideo="1" format="r1">'
                               f'<media-rep kind="original-media" src="file://{tmp_path}/gone.mp4"/></asset>')
    tl = _xml(tmp_path / "missing.fcpxml", spine, assets, "4s")
    raster = _sheet(tl, tmp_path, "desktop")["rasters"][0]
    red = [r.text for r in raster.runs
           if any(raster.get(x, y)[:3] == proofs.DANGER for x in range(r.x, r.x + r.width)
                  for y in range(r.y, r.y + r.height))]
    assert red == ["media missing"]


# ---- 9. the text timeline ---------------------------------------------------------

ROW = re.compile(r"^(\d+)\s+(V|L\d+)\s+(\S+)\s+(\S+)\s+(\d+)f\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")


@needs_ffmpeg
def test_the_text_timeline_is_an_edl_whose_numbers_add_up(gym):
    tl = gym["timeline"]
    graph = filtergraph.compile_timeline(tl)
    text = proofs.text_timeline(tl, graph, 64)
    rows = text.splitlines()
    names = [r[:10].strip() for r in rows]
    assert names[1:6] == ["time", "", "markers", "lane 1", "spine"]
    # the last tick labelled, ending on its own tick (not past the ruler)
    assert rows[1].rstrip().endswith("30.0s"), rows[1]
    assert len(rows[1].rstrip()) == len(rows[2].rstrip()), (rows[1], rows[2])
    assert "Substituted" not in text
    assert rows[4].split()[-1] == "Bench" and "[" in rows[4]
    events = [ROW.match(r) for r in rows if ROW.match(r)]
    truth = _record_truth(tl)
    assert [e.group(6) for e in events] == [r[0] for r in truth]
    for e, r in zip(events, truth):
        rec_in, rec_out, length = seconds_of(e.group(3)), seconds_of(e.group(4)), int(e.group(5))
        assert (rec_in, length) == (Fraction(r[2], 24), r[3])
        assert rec_in + Fraction(length, 24) == rec_out
        assert (e.group(7), e.group(8), e.group(9)) == (r[5], tcf(r[4]), tcf(r[4] + r[3]))
    spine = [e for e in events if e.group(2) == "V"]
    for a, b in zip(spine, spine[1:]):
        assert a.group(4) == b.group(3)
    dissolve = next(r for r in rows if r.startswith("X"))
    t = tl.transitions[0]
    cut, half = frames_of(t.start), frames_of(t.duration) // 2
    assert tcf(cut - half) in dissolve and tcf(cut + half) in dissolve and "centred" in dissolve
    marked = [r for r in rows if re.match(r"^\s+marker ", r)]
    assert len(marked) == 4


# ---- 10. diagnose: the picture's flash, the fixes, every duplicate -----------------

@needs_ffmpeg
def test_the_flash_frame_in_the_picture_is_found(gym):
    """Bench (L1) ends at frame 483 and Press starts at 484: one frame of
    Squat shows between them. No clip is short, so a clip-length check
    passes it; the picture check does not."""
    import server

    tl = gym["timeline"]
    lane = tl.connected_clips[0]
    lane_end = frames_of(lane.timeline_start) + frames_of(lane.duration)
    nxt = next(c for c in tl.clips if frames_of(c.start) > frames_of(lane.timeline_start))
    gap = frames_of(nxt.start) - lane_end
    assert gap == 1
    text = asyncio.run(server.handle_validate_timeline({"filepath": str(gym["edited"])}))[0].text
    row = next(r for r in text.splitlines() if "| Flash Frames |" in r)
    assert "FAIL" in row and tcf(lane_end) in row
    assert f"Squat shows for {gap}f between Bench (L1) and Press" in text
    assert f"Flash at {tcf(lane_end)}: extend Bench (L1) by 1f" in text


@needs_ffmpeg
def test_diagnose_prints_fixes_only_for_what_failed_and_counts_every_duplicate(gym):
    import server

    text = asyncio.run(server.handle_validate_timeline({"filepath": str(gym["edited"])}))[0].text
    assert "fill_gaps" not in text and "Use `fix_flash_frames`" not in text
    summary = [r for r in text.splitlines() if r.startswith("| ") and "|--" not in r][1:4]
    glyph = re.compile(r"^\| (✓ PASS|⚠ WARN|✗ FAIL|· INFO) \| ")
    for r in summary:
        assert glyph.match(r), r
    dup = next(r for r in summary if "Duplicate Sources" in r)
    assert "Bench.mov: 00:00:00:00, 00:00:17:03" in dup
    assert "Squat.mov: 00:00:05:00, 00:00:10:01, 00:00:15:03" in dup
    assert "Same frames used twice: Bench.mov" in text
    # FAIL -10, WARN -3, INFO costs nothing
    assert "## Health Score: 87%" in text


def test_a_clip_flash_names_the_grouped_tool_that_fixes_it():
    import server

    text = asyncio.run(server.handle_validate_timeline({"filepath": str(REPO / "examples" / "sample.fcpxml")}))[0].text
    if "Flash frame:" in text:
        assert "`edit · fix_flash_frames`" in text
    assert "Use `fix_flash_frames` and `fill_gaps`" not in text


def test_lengths_round_half_up_exactly():
    """A float's half-to-even printed 5.25 s as 5.2 on the sheet."""
    assert [proofs._seconds_label(Fraction(x)) for x in ("21/4", "19/4", "0", "125/2", "120")] == \
        ["5.3s", "4.8s", "0.0s", "1m02.5s", "2m00.0s"]
