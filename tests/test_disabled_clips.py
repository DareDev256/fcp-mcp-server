"""A disabled clip is in the cut and out of the picture and the sound (0.33).

Final Cut writes ``enabled="0"`` on a clip switched off with V: on the spine
it keeps its time and plays black and silence; on a lane (and inside a
connected storyline) it is simply not there. Until 0.33 the flag was not
read anywhere, so the render drew and played a clip Final Cut does not, and
diagnose called it a flash, a duplicate or offline media. An editor still has
to SEE it is there, so the view and the text timeline show it, greyed and
marked off.

The render case is checked the way the lane checks are: the real proxy,
read back by colour and loudness at the instants that matter.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from array import array
from pathlib import Path

import pytest

from fcpxml import proofs, render
from fcpxml.export import DaVinciExporter
from fcpxml.filtergraph import compile_timeline, lane_tracks
from fcpxml.parser import FCPXMLParser
from fcpxml.timeline_app import extract_payload

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
                   capture_output=True, timeout=120, check=True)


RENDER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="blue" name="Blue" start="0s" duration="6s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="{d}/blue.mp4"/></asset>
    <asset id="red" name="Red" start="0s" duration="6s" hasVideo="1" hasAudio="1" audioSources="1" format="r1">
      <media-rep kind="original-media" src="{d}/red.mp4"/></asset>
    <asset id="green" name="Green" start="0s" duration="6s" hasVideo="1" hasAudio="1" audioSources="1" format="r1">
      <media-rep kind="original-media" src="{d}/green.mp4"/></asset>
  </resources>
  <library><event name="E"><project name="Off">
    <sequence format="r1" duration="6s" tcStart="0s" tcFormat="NDF">
      <spine>
        <asset-clip ref="blue" name="SHOWN" offset="0s" start="0s" duration="2s">
          <asset-clip ref="red" lane="1" name="HIDDEN" offset="1/2s" start="0s" duration="1s" enabled="0"/>
          <asset-clip ref="red" lane="-1" name="MUTED" offset="0s" start="0s" duration="2s" enabled="0"/>
        </asset-clip>
        <asset-clip ref="red" name="OFF" offset="2s" start="0s" duration="2s" enabled="0"/>
        <asset-clip ref="green" name="AFTER" offset="4s" start="0s" duration="2s">
          <spine lane="2" offset="1s">
            <asset-clip ref="red" name="STORY_OFF" offset="0s" start="0s" duration="1/2s" enabled="0"/>
          </spine>
        </asset-clip>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
"""


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("off")
    if FFMPEG:
        _ff("-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=24:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "blue.mp4"))
        for colour in ("red", "green"):
            _ff("-f", "lavfi", "-i", f"color=c={colour}:size=320x180:rate=24:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / f"{colour}.mp4"))
    else:
        for name in ("blue", "red", "green"):
            (d / f"{name}.mp4").write_bytes(b"\0" * 64)
    return d


@pytest.fixture
def project(media, tmp_path):
    path = tmp_path / "off.fcpxml"
    path.write_text(RENDER_XML.replace("{d}", media.as_uri()))
    return path


def _timeline(path):
    return FCPXMLParser().parse_file(str(path)).timelines[0]


def test_the_flag_is_read_on_spine_lane_and_storyline_clips(project):
    tl = _timeline(project)
    assert {c.name: c.enabled for c in tl.clips} == {"SHOWN": True, "OFF": False, "AFTER": True}
    assert {c.name: c.enabled for c in tl.connected_clips} == {"HIDDEN": False, "MUTED": False, "STORY_OFF": False}
    # and it travels with the clip into Clip shape (the media and QC readers)
    assert {c.name: c.enabled for c in tl.all_clips()}["STORY_OFF"] is False


def test_the_render_graph_leaves_disabled_clips_out_of_picture_and_sound(project):
    graph = compile_timeline(_timeline(project))
    drawn = {graph.segments[p.segment].label for track in lane_tracks(graph) for p in track}
    assert drawn == set()  # both lane clips are disabled
    spine = {graph.segments[p.segment].label: p for p in graph.pieces}
    assert spine["OFF"].source is None and spine["OFF"].frames == 48  # black, and its 2s kept
    assert spine["AFTER"].start == 96  # nothing after it moved
    assert sorted(s.label for s in graph.audio) == ["AFTER"]
    note = next(n for n in graph.substitutions if "disabled" in n)
    assert note.startswith("4 disabled clips left out of the picture and the sound"), note
    assert "'MUTED'" in note  # an audio lane below the spine too
    # still in the graph, marked, for the readers that show the cut
    assert {s.label for s in graph.segments if not s.enabled} == {"OFF", "HIDDEN", "STORY_OFF"}


def _rgb(path, at):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{float(at):.4f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    return tuple(out[:3])


def _is(rgb, colour):
    r, g, b = rgb
    return {
        "red": r > 150 and g < 90 and b < 90,
        "green": g > 90 and r < 60 and b < 60,
        "blue": b > 150 and r < 60 and g < 60,
        "black": r < 30 and g < 30 and b < 30,
    }[colour]


def _rms(path, at, length=0.25):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-t", f"{length:.3f}", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    samples = array("h", raw[: len(raw) // 2 * 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768 if samples else 0.0


@needs_ffmpeg
def test_the_real_render_shows_what_final_cut_plays(project, tmp_path):
    out = tmp_path / "off.mp4"
    result = render.render_proxy(_timeline(project), out_path=str(out), height=180)
    assert result["error"] is None, result["error"]
    seen = {t: _rgb(out, t) for t in (1.0, 3.0, 4.6, 5.25)}
    assert _is(seen[1.0], "blue"), seen      # the spine picture, not the disabled red lane clip over it
    assert _is(seen[3.0], "black"), seen     # the disabled spine clip keeps its time, as black
    assert _is(seen[4.6], "green"), seen     # the clip after it, on time
    assert _is(seen[5.25], "green"), seen    # not the disabled clip in the storyline over it
    assert _rms(out, 2.8) < 0.005            # the disabled spine clip's tone is not heard
    assert _rms(out, 0.9) < 0.005            # nor the disabled lane clip's, nor the disabled audio lane's
    assert _rms(out, 4.6) > 0.05             # the control: AFTER's tone is


DIAG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="a" name="A" start="0s" duration="60s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="{d}/a.mov"/></asset>
    <asset id="b" name="B" start="0s" duration="60s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="{d}/b.mov"/></asset>
    <asset id="gone" name="Gone" start="0s" duration="60s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="{d}/gone.mov"/></asset>
  </resources>
  <library><event name="E"><project name="Diag">
    <sequence format="r1" duration="97/24s" tcStart="0s">
      <spine>
        <asset-clip ref="a" name="A" offset="0s" start="0s" duration="2s">
          <asset-clip ref="gone" lane="1" name="GONE" offset="1/2s" start="0s" duration="1s" enabled="0"/>
        </asset-clip>
        <asset-clip ref="a" name="FLICK" offset="2s" start="0s" duration="1/24s" enabled="0"/>
        <asset-clip ref="b" name="B" offset="49/24s" start="0s" duration="2s"/>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
"""


@pytest.fixture
def diag(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.mov").write_bytes(b"\0" * 64)
    path = tmp_path / "diag.fcpxml"
    path.write_text(DIAG_XML.replace("{d}", tmp_path.as_uri()))
    return path


def _call(tool, action, **args):
    import server

    return asyncio.run(server.call_tool(tool, {"action": action, "args": args}))[0].text


def _row(report, check):
    return next(r for r in report.splitlines() if f"| {check} |" in r)


def test_diagnose_leaves_disabled_clips_out_and_says_how_many(diag):
    """FLICK is one frame long and shows A's first frame again; GONE's file is
    missing. Enabled, they are a flash, a duplicate and offline media; off,
    they are none of those, and every report says two clips are off."""
    report = _call("diagnose", "validate_timeline", filepath=str(diag))
    for check in ("Flash Frames", "Duplicate frames", "Reused source", "Offline media"):
        assert "| 0 |" in _row(report, check), _row(report, check)
    assert "FLICK" not in report and "GONE" not in report, report
    assert "Disabled: 2 clips" in report, report
    assert "Disabled: 2 clips" in _call("diagnose", "detect_flash_frames", filepath=str(diag))
    dups = _call("diagnose", "detect_duplicates", filepath=str(diag))
    assert dups.startswith("No duplicate clips found") and "Disabled: 2 clips" in dups, dups


def test_the_text_timeline_shows_a_disabled_clip_marked_off(diag):
    text = _call("preview", "preview_timeline", filepath=str(diag))
    flick = next(r for r in text.splitlines() if " FLICK " in r and r.split()[0].isdigit())
    assert flick.rstrip().endswith("off (disabled: not seen, not heard)"), flick
    assert "[-] off (disabled)" in next(r for r in text.splitlines() if r.startswith("legend"))
    gone = next(r for r in text.splitlines() if " GONE " in r and r.split()[0].isdigit())
    assert "off (disabled" in gone and "MEDIA MISSING" in gone, gone
    # numbering is the cut's: the disabled clips keep their numbers
    numbers = [r.split()[0] for r in text.splitlines() if r[:1].isdigit()]
    assert numbers == ["1", "2", "3", "4"]


def test_the_sheet_skips_a_disabled_clip_like_the_render(diag):
    graph = compile_timeline(_timeline(diag))
    cells = proofs.sheet_cells(graph)
    assert [(c.index, c.name) for c in cells] == [(1, "A"), (4, "B")]


def test_the_view_payload_carries_the_flag(diag):
    payload = extract_payload(_call("view", "view_timeline", filepath=str(diag), thumbnails=False))
    assert {c["name"]: c["enabled"] for c in payload["clips"]} == {
        "A": True, "FLICK": False, "B": True, "GONE": False}


def test_the_app_draws_a_disabled_clip_greyed_and_marked_off(diag):
    playwright_api = pytest.importorskip("playwright.sync_api")
    from fcpxml import timeline_app

    payload = timeline_app.build_payload(_timeline(diag))
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser installed here
            pytest.skip(f"no Chromium for Playwright: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(timeline_app.render_page_html(json.loads(json.dumps(payload))))
            page.wait_for_timeout(50)
            facts = page.evaluate("""() => [...document.querySelectorAll('#canvas .clip')].map(e => ({
                name: e.__c && e.__c.name, off: e.classList.contains('off'),
                opacity: Number(getComputedStyle(e).opacity), title: e.title,
                tag: (e.querySelector('.off-tag') || {}).textContent || null}))""")
        finally:
            browser.close()
    by_name = {f["name"]: f for f in facts}
    for name in ("FLICK", "GONE"):
        assert by_name[name]["off"] and by_name[name]["opacity"] < 0.6, by_name[name]
        assert by_name[name]["title"].endswith("off (disabled)"), by_name[name]
    assert by_name["GONE"]["tag"] == "off", by_name["GONE"]  # wide enough to carry its name and the tag
    for name in ("A", "B"):
        assert not by_name[name]["off"] and by_name[name]["opacity"] == 1, by_name[name]


def test_the_xmeml_export_keeps_a_disabled_clip_disabled(diag, tmp_path):
    out = tmp_path / "diag.xml"
    DaVinciExporter(str(diag)).export_xmeml(str(out))
    items = {i.findtext("name"): i.findtext("enabled") for i in ET.parse(out).getroot().iter("clipitem")}
    assert items["FLICK"] == "FALSE" and items["GONE"] == "FALSE"
    assert items["A"] is None and items["B"] is None  # enabled is the default; nothing written


REAL = __import__("os").environ.get("FCP_MCP_REAL_FCPXML", "")


@pytest.mark.skipif(not REAL, reason="set FCP_MCP_REAL_FCPXML to a Final Cut export to check it")
def test_a_real_exports_disabled_clips_are_out_of_the_render_graph():
    """Run by hand against a real export (nothing from one is committed)."""
    path = REAL if not Path(REAL).is_dir() else str(Path(REAL) / "Info.fcpxml")
    root = ET.parse(path).getroot()
    # clips switched off inside the projects' sequences (not in compound media)
    in_xml = sum(1 for project in root.iter("project") for e in project.iter()
                 if e.get("enabled") == "0" and e.tag in ("asset-clip", "clip", "video", "audio", "title",
                                                          "ref-clip", "mc-clip", "sync-clip", "caption"))
    total, in_graph = 0, 0
    for tl in FCPXMLParser().parse_file(path).timelines:
        total += sum(1 for c in tl.all_clips() if not c.enabled)
        graph = compile_timeline(tl)
        off = [s for s in graph.segments if not s.enabled]
        drawn = {id(graph.segments[p.segment]) for p in list(graph.lanes) + list(graph.pieces) if p.source}
        assert not any(id(s) in drawn for s in off), "a disabled clip is in the picture"
        heard = {a.label for a in graph.audio}
        only_off = {s.label for s in off} - {s.label for s in graph.segments if s.enabled}
        assert not heard & only_off, "a disabled clip is in the sound"
        in_graph += len(off)
    print(f"\ndisabled in the XML: {in_xml}; parsed: {total}; in the render graph, none drawn or heard: {in_graph}")
    assert total == in_xml
