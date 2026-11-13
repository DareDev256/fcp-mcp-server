"""Proof you can read (0.32): the gates on every picture the server draws.

Each gate reads the OUTPUT (the PNG written, the MP4 burned, the MCP reply on
the wire) and compares it with a truth computed here, independently of the
code that drew it: a cut's timecode from the parsed timeline's own clip
starts, a tick's place from its time, a burned frame's timecode from its
frame number. Each one was broken once in the real code and seen red (the
mutation list is in the 0.32 CHANGELOG entry).

The real-footage fixture (demo/fixtures/gym_session.py: the author's own gym
session, silent proxies plus a synthesised tone) is built once per module.
"""

import asyncio
import base64
import contextlib
import dataclasses
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


def tc(seconds, fps: int = 24) -> str:
    """Non-drop timecode written here, not imported: the truth the drawn
    timecodes are compared with must not share their code."""
    frames = int(Fraction(seconds) * fps + Fraction(1, 2))
    s, f = divmod(frames, fps)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


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
        result = render.render_proxy(gym["timeline"], out_path=str(out), burn_in=True)
    return result


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
    """testsrc and flat white, 1920x1080 at 24 fps."""
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("synth")
    _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24:duration=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "bars.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=white:size=1920x1080:rate=24:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "white.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=0x202020:size=1920x1080:rate=24:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "dark.mp4"))
    return d


def _assets(d: Path) -> str:
    return "\n".join(
        f'        <asset id="a{i}" name="{n}" start="0s" duration="{s}s" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file://{d}/{f}"/></asset>'
        for i, (n, f, s) in enumerate((("Bars", "bars.mp4", 30), ("White", "white.mp4", 4),
                                       ("Dark", "dark.mp4", 4)), start=1))


def _sheet(timeline, out: Path, layout: str):
    graph = filtergraph.compile_timeline(timeline)
    return proofs.contact_sheet(timeline, graph, out, layout=layout, filepath="", today="2026-11-13")


def _tc_runs(raster, below: int) -> list:
    return sorted((r for r in raster.runs if TC.fullmatch(r.text) and r.y >= below), key=lambda r: (r.y, r.x))


# ---- 1. the contact sheet's timecodes are the timeline's ---------------------------

@needs_ffmpeg
@pytest.mark.parametrize("layout", ["desktop", "phone"])
def test_every_cut_timecode_read_off_the_sheet_is_the_timelines(gym, tmp_path, layout):
    """Read each cell's timecode back off the PNG as written and compare it
    with the parsed timeline's own clip start. The phone page is also read at
    the size it is delivered (390 px wide, area-downscaled)."""
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, layout)
    truth = [tc(Fraction(c.start._exact_seconds)) for c in tl.clips]
    lay, geo = sheet["layout"], sheet["geometry"]
    read = []
    for p, (path, raster) in enumerate(zip(sheet["pages"], sheet["rasters"])):
        png = rs.load(path)
        runs = _tc_runs(raster, lay.margin + geo["head_h"])
        cells = sheet["cells"][p * sheet["per_page"]:(p + 1) * sheet["per_page"]]
        assert len(runs) == len(cells)
        for k, run in enumerate(runs):
            col = k % lay.columns
            assert run.x == lay.margin + col * (geo["cell_w"] + lay.gutter), "timecode not at its cell's left"
            read.append(rs.read_text(png, run.face, run.x, run.y, 11, alphabet="0123456789:"))
        if lay.delivered != 1:
            small = rs.resize(png, int(png.width * lay.delivered), int(png.height * lay.delivered))
            base = rs.face(24)
            for run, want in zip(runs, read[-len(runs):]):
                at = rs.read_text(small, base, int(run.x * lay.delivered), int(run.y * lay.delivered), 11,
                                  alphabet="0123456789:")
                assert at == want, f"unreadable at the delivered size: {at!r}"
    assert read == truth


@needs_ffmpeg
def test_the_sheet_names_every_clip_whole_and_marks_the_transition(gym, tmp_path):
    tl = gym["timeline"]
    sheet = _sheet(tl, tmp_path, "desktop")
    texts = [r.text for r in sheet["rasters"][0].runs]
    for clip in tl.clips:
        assert clip.name in texts
    # one dissolve, Press into Finish: the glyph sits on Finish's cut
    assert [g["into"] for g in sheet["glyphs"]] == [6]
    png = rs.load(sheet["pages"][0])
    g = sheet["glyphs"][0]
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
    over = rs.Raster.filled(400, 100, proofs.GROUND)
    assert rs.legibility_problems(over, cells=13)


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["desktop", "phone"])
def test_both_layouts_are_legible_at_their_delivered_size(gym, tmp_path, layout):
    sheet = _sheet(gym["timeline"], tmp_path, layout)
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
def test_more_than_a_page_of_cuts_paginates_with_no_collision(synth, tmp_path, layout):
    spine = "\n".join(f'            <asset-clip ref="a1" offset="{i}s" name="Shot {i + 1}" start="{i}s" duration="1s"/>'
                      for i in range(14))
    tl = _xml(tmp_path / "many.fcpxml", spine, _assets(synth), "14s", "Fourteen cuts")
    sheet = _sheet(tl, tmp_path, layout)
    assert len(sheet["pages"]) == 2 and sheet["problems"] == []
    assert all(len(sheet["cells"][i:i + sheet["per_page"]]) <= rs.CELLS_MAX
               for i in range(0, 14, sheet["per_page"]))
    assert "page 2/2" in [r.text for r in sheet["rasters"][1].runs]
    shown = []
    for raster in sheet["rasters"]:
        shown += [r.text for r in _tc_runs(raster, 0)]
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
        lines = sorted((r for r in raster.runs if r.y >= top and r.x == sheet["layout"].margin
                        and not TC.fullmatch(r.text) and not r.text.startswith("#")), key=lambda r: r.y)
        assert " ".join(r.text for r in lines) == name, [r.text for r in lines]
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


# ---- 3. the check's ruler: every tick within 1 px of its time ----------------------

@needs_ffmpeg
@pytest.mark.parametrize("start, end", [(Fraction(1, 2), Fraction(37, 10)), (Fraction(0), Fraction(29)),
                                        (Fraction(3), Fraction(7, 2))])
def test_every_ruler_tick_is_within_a_pixel_of_its_time(synth, tmp_path, start, end):
    result = proofs.visual_check(str(synth / "bars.mp4"), start, end, str(tmp_path / "c.png"),
                                 cut=(start + end) / 2, fps=24)
    g, png = result["geometry"], rs.load(result["path"])
    x0, x1, y = g["x0"], g["x1"], g["ruler_y"]
    span = x1 - 1 - x0

    def where(t):
        return x0 + float((t - start) / (end - start)) * span

    drawn = [x for x in range(x0, x1) if png.get(x, y + 5)[:3] == pytest.approx(proofs.ACCENT, abs=24)]
    assert len(drawn) == len(result["ticks"]) >= 2
    for (t, _), x in zip(result["ticks"], drawn):
        assert abs(x - where(t)) <= 1, f"tick for {t}s at {x}, its time is at {where(t):.2f}"
        for row in range(y + 1, y + 10):  # every row the tick claims
            assert png.get(x, row)[:3] == pytest.approx(proofs.ACCENT, abs=24), (x, row)
    label = rs.face(24)
    labels = [r for r in result["raster"].runs if r.y == y + 12]
    assert labels, "no tick is labelled"
    for run in labels:  # the label read off the PNG is the time of the tick it hangs from
        text = rs.read_text(png, label, run.x, run.y, 11, alphabet="0123456789:")
        nearest = min(drawn, key=lambda x: abs(x - (run.x + run.width / 2)))
        t = next(t for (t, _), x in zip(result["ticks"], drawn) if x == nearest)
        assert text == tc(t)
    assert result["problems"] == []


# ---- 4. the burn-in: every frame's timecode and name ------------------------------

@needs_ffmpeg
def test_every_burned_frame_carries_its_own_timecode_and_clip(gym, burned):
    tl = gym["timeline"]
    assert burned["error"] is None and burned["verified"] is True, burned
    assert burned["size"] == "1280x720"
    assert burned["burned"] and Path(burned["burned"]).is_file()
    strips = proofs.burned_strips(burned["burned"], 1280, 720)
    spine = [(Fraction(c.start._exact_seconds), Fraction(c.start._exact_seconds) + Fraction(c.duration._exact_seconds),
              c.name) for c in tl.clips]
    total = spine[-1][1]
    assert len(strips) == int(total * 24 + Fraction(1, 2))
    letters = "".join(sorted({ch for _, _, n in spine for ch in n}))
    wrong = []
    for k, strip in enumerate(strips):  # every frame
        t = Fraction(k, 24)
        name = next(n for a, b, n in spine if a <= t < b)
        got_tc, got_name = proofs.read_burn(strip, 720, len(name), letters)
        if (got_tc, got_name) != (tc(t), name):
            wrong.append((k, got_tc, got_name, tc(t), name))
    assert wrong == [], wrong[:5]
    for a, _, name in spine:  # and at each cut, the cut's own time
        got, _ = proofs.read_burn(strips[int(a * 24 + Fraction(1, 2))], 720)
        assert got == tc(a), name


@needs_ffmpeg
def test_a_dissolve_after_hard_cuts_renders_in_the_full_graph(burned):
    """0.31 failed this graph: xfade refused the concat's 1/1000000 timebase
    against the clips' 1/24, so the proxy fell back to hard cuts and silence.
    The gym edit has four hard cuts, then the Press-to-Finish dissolve."""
    assert not any("refused" in note for note in burned["substitutions"]), burned["substitutions"]
    assert [t.kind for t in burned["transitions"]] == ["fade"]


@needs_ffmpeg
def test_the_poster_and_the_burned_letters_are_legible(burned):
    poster = rs.load(burned["poster"])
    assert (poster.width, poster.height) == (1280, 720)
    f = proofs.burn_face(720)
    assert f.cap * f.scale >= rs.GLYPH_MIN
    assert f.cap * f.scale * rs.LONG_EDGE_MAX / 1280 >= rs.GLYPH_MIN  # still 14 px if sent at the model's size


# ---- 5. the burn goes on AFTER the lane instrument has read the clean render --------

@needs_ffmpeg
def test_burning_before_verify_lanes_would_fail_the_instrument(synth, tmp_path):
    """A white picture-in-picture at a quarter size over a dark spine. The
    lane badge across its top is 18% of it: the instrument, run on the burned
    file, reports the lane NOT drawn. render_proxy must therefore read the
    clean file, and does: the clean render verifies."""
    spine = '''            <asset-clip ref="a3" offset="0s" name="Dark" start="0s" duration="3s">
                <asset-clip ref="a2" lane="1" offset="0s" name="White" start="0s" duration="3s">
                    <adjust-transform position="0 0" scale="0.25 0.25"/>
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


# ---- 6. image blocks on the wire, each under its cap ----------------------------

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


def _size(data: bytes, tmp_path: Path) -> tuple:
    path = tmp_path / "block.bin"
    path.write_bytes(data)
    return rs.image_size(str(path))


@needs_ffmpeg
@pytest.mark.parametrize("action, args", [
    ("preview_sheet", lambda g: {"filepath": str(g["edited"])}),
    ("preview_sheet", lambda g: {"filepath": str(g["edited"]), "layout": "phone"}),
    ("preview_check", lambda g: {"source": str(g["media"]["Squat"]), "start": 4, "end": 8, "cut": 5}),
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


@needs_ffmpeg
def test_the_scenes_strip_shows_both_sides_of_every_cut(gym, tmp_path):
    reply = _call("scenes", "detect_scenes", {"filepath": str(gym["takes"])})
    text = reply[0].text
    times = re.findall(r"\|\s*Montage\s*\|[^|]*\|\s*([0-9.]+)s?\s*\|", text)
    assert len(times) >= 2, text
    images = _images(reply)
    assert images
    page = tmp_path / "strip.png"
    page.write_bytes(images[0][1])
    png = rs.load(str(page))
    label = rs.face(24)
    tcs = [rs.read_text(png, label, 16 + 2 * (400 + 12), y, 11, alphabet="0123456789:")
           for y in [16 + rs.face(32).height + 8 + i * (225 + 12) + label.height for i in range(len(times))]]
    assert tcs == [tc(Fraction(t)) for t in times]


# ---- 7. one palette; contrast; red only for failure -------------------------------

def test_every_text_colour_the_proofs_use_reads_at_4_5_to_1():
    hexes = {k: palette.dark(k) for k in ("ground", "panel", "ink", "mute", "accent", "danger", "on-media")}
    for fg in ("ink", "mute", "accent", "danger"):
        for bg in ("ground", "panel"):
            assert palette.contrast(hexes[fg], hexes[bg]) >= 4.5, (fg, bg)
    # the burn-in box: 70% black over the brightest picture there can be
    box = palette.composite(hexes["ground"] + f"{proofs.BURN_ALPHA:02X}", "#FFFFFF")
    for fg in ("on-media", "accent"):
        assert palette.contrast(hexes[fg], box) >= 4.5, fg
    assert hexes["ground"] == "#000000" and hexes["accent"] == "#00E5FF"


@needs_ffmpeg
def test_red_appears_only_for_a_failure(synth, tmp_path):
    spine = ('            <asset-clip ref="a1" offset="0s" name="Here" start="0s" duration="2s"/>\n'
             '            <asset-clip ref="a9" offset="2s" name="Gone" start="0s" duration="2s"/>')
    assets = _assets(synth) + ('\n        <asset id="a9" name="Gone" start="0s" duration="4s" hasVideo="1" format="r1">'
                               f'<media-rep kind="original-media" src="file://{tmp_path}/gone.mp4"/></asset>')
    tl = _xml(tmp_path / "missing.fcpxml", spine, assets, "4s")
    raster = _sheet(tl, tmp_path, "desktop")["rasters"][0]
    red = [r.text for r in raster.runs if r.face and raster.get(r.x, r.y) is not None
           and any(raster.get(x, y)[:3] == proofs.DANGER for x in range(r.x, r.x + r.width)
                   for y in range(r.y, r.y + r.height))]
    assert red == ["media missing"]


# ---- 8. the text timeline and the diagnose column --------------------------------

@needs_ffmpeg
def test_the_text_timeline_has_a_ruler_lanes_markers_and_a_time_ordered_list(gym):
    tl = gym["timeline"]
    graph = filtergraph.compile_timeline(tl)
    rows = proofs.text_timeline(tl, graph, 64).splitlines()
    names = [r[:10].strip() for r in rows]
    assert names[1:6] == ["time", "", "markers", "lane 1", "spine"]
    gutter, width, total = 10, 64, graph.total
    spine_row = rows[5]
    finish = Fraction(tl.clips[-1].start._exact_seconds)
    assert spine_row[gutter + int(finish / total * width)] == "X"
    marker_cols = {i - gutter for i, ch in enumerate(rows[3]) if ch == "^"}
    expected = {int(Fraction(c.start._exact_seconds) / total * width) for c in tl.clips if c.markers}
    assert marker_cols == expected
    listed = [r for r in rows if TC.match(r)]
    assert listed == sorted(listed, key=lambda r: r[:11])
    assert any(r.startswith(tc(finish)) and "Cross Dissolve" in r for r in listed)


def test_diagnose_reads_by_a_verdict_column_with_a_timecode_for_where():
    text = _call("diagnose", "validate_timeline", {"filepath": str(REPO / "examples" / "sample.fcpxml")})[0].text
    summary = [r for r in text.splitlines() if r.startswith("| ") and "|--" not in r][1:4]
    assert len(summary) == 3
    glyph = re.compile(r"^\| (✓ PASS|⚠ WARN|✗ FAIL|· INFO) \| ")
    for row in summary:
        assert glyph.match(row), row
        cells = [c.strip() for c in row.strip("|").split("|")]
        count, where = int(cells[2]), cells[3]
        assert (where == "—") == (count == 0), row
        if count:
            assert TC.search(where), row
    issues = [r for r in text.splitlines() if glyph.match(r) and r.count("|") == 4]
    assert issues and all(TC.search(r.split("|")[2]) for r in issues), issues


def test_lengths_round_half_up_exactly():
    """A float's half-to-even printed 5.25 s as 5.2 on the sheet."""
    assert [proofs._seconds_label(Fraction(x)) for x in ("21/4", "19/4", "0", "125/2", "120")] == \
        ["5.3s", "4.8s", "0.0s", "1m02.5s", "2m00.0s"]
