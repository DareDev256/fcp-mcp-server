"""A connected storyline is a ``<spine lane="N">`` with its own clock (0.33).

Apple's FCPXML DTD has no ``<storyline>`` element. A connected (secondary)
storyline is a ``<spine>`` carrying ``lane`` and ``offset``, and ``spine`` is
an anchor item, so it hangs off a spine clip or a gap like any connected
clip. Its ``offset`` is on the PARENT's clock (which starts at the parent's
``start``); its children's offsets are on the storyline's OWN clock, which
starts at 0. So a child sits on the timeline at

    parent position + (storyline offset - parent start) + child offset

Until 0.33 the parser and the writer knew only an invented ``<storyline>``
tag, and every clip in a connected storyline from a real Final Cut export
was dropped without a word: from the model, so from every reader and from
the render.

The fixture below is the real shape (measured against two Final Cut 1.14
exports): a storyline under a trimmed spine clip with two clips in sequence,
a transition between them and a gap inside; a storyline under a broadcast
gap (``start="3600s"``) whose first item is a transition. Every position is
checked against a truth computed here from the XML with its own arithmetic.
"""

from __future__ import annotations

import asyncio
import os
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml.filtergraph import compile_timeline, lane_tracks
from fcpxml.parser import FCPXMLParser
from fcpxml.rational import parse_seconds
from fcpxml.timeline_app import extract_payload
from fcpxml.writer import FCPXMLModifier

FPS = 24

XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="a1" name="Base" start="0s" duration="60s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="{media}"/>
    </asset>
    <asset id="a2" name="Lane" start="0s" duration="60s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="{media}"/>
    </asset>
  </resources>
  <library><event name="E"><project name="Storylines">
    <sequence format="r1" duration="20s" tcStart="0s" tcFormat="NDF">
      <spine>
        <asset-clip ref="a1" name="BASE" offset="0s" start="10s" duration="10s">
          <spine lane="1" offset="12s">
            <asset-clip ref="a2" name="ONE" offset="0s" start="1s" duration="2s"/>
            <transition name="Cross Dissolve" offset="3/2s" duration="1s"/>
            <asset-clip ref="a2" name="TWO" offset="2s" start="5s" duration="2s"/>
            <gap name="Gap" offset="4s" start="3600s" duration="1s"/>
            <asset-clip ref="a2" name="THREE" offset="5s" start="9s" duration="1s"/>
          </spine>
        </asset-clip>
        <gap name="Gap" offset="10s" start="3600s" duration="10s">
          <spine lane="2" offset="3601s">
            <transition name="Cross Dissolve" offset="0s" duration="1/2s"/>
            <asset-clip ref="a2" name="FOUR" offset="0s" start="20s" duration="3s"/>
            <asset-clip ref="a2" name="FIVE" offset="3s" start="24s" duration="2s"/>
          </spine>
          <asset-clip ref="a2" lane="1" name="PLAIN" offset="3600s" start="30s" duration="1s"/>
        </gap>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
"""

# where each clip appears, in seconds, worked out by hand from the XML above
BY_HAND = {"ONE": (1, 2), "TWO": (1, 4), "THREE": (1, 7), "FOUR": (2, 11), "FIVE": (2, 14), "PLAIN": (1, 10)}


@pytest.fixture
def project(tmp_path):
    media = tmp_path / "lane.mov"
    media.write_bytes(b"\0" * 64)
    path = tmp_path / "storylines.fcpxml"
    path.write_text(XML.replace("{media}", media.as_uri()))
    return path


def _truth(path: Path) -> dict:
    """name -> (lane, first frame, frames) from the XML: each primary element's
    place on the spine, the storyline's offset on that element's clock, the
    child's offset on the storyline's own clock."""
    root = ET.parse(path).getroot()
    fps = 1 / parse_seconds(root.find(".//format").get("frameDuration"))
    out = {}
    at = Fraction(0)  # each primary element's place on the timeline
    for element in root.find(".//sequence/spine"):
        if element.tag == "transition":
            continue
        clock = parse_seconds(element.get("start", "0s"))
        for child in element:
            if child.tag == "spine":
                base = at + parse_seconds(child.get("offset")) - clock
                for item in child:
                    if item.tag == "asset-clip":
                        out[item.get("name")] = (int(child.get("lane")),
                                                 round((base + parse_seconds(item.get("offset"))) * fps),
                                                 round(parse_seconds(item.get("duration")) * fps))
            elif child.get("lane") is not None:
                out[child.get("name")] = (int(child.get("lane")),
                                          round((at + parse_seconds(child.get("offset")) - clock) * fps),
                                          round(parse_seconds(child.get("duration")) * fps))
        at += parse_seconds(element.get("duration"))
    return out


def _timeline(path):
    return FCPXMLParser().parse_file(str(path)).timelines[0]


def test_the_truth_reads_the_xml_as_worked_out_by_hand(project):
    assert {name: (lane, Fraction(first, FPS)) for name, (lane, first, _n) in _truth(project).items()} == BY_HAND


def test_every_clip_in_a_connected_storyline_is_parsed_where_it_plays(project):
    tl = _timeline(project)
    got = {c.name: (c.lane, c.timeline_start.frames, c.duration.frames) for c in tl.connected_clips}
    assert got == _truth(project)
    # the storyline under BASE hangs off BASE; the one under the gap off the gap
    parents = {c.name: c.parent_clip_name for c in tl.connected_clips}
    assert {parents[n] for n in ("ONE", "TWO", "THREE")} == {"BASE"}
    assert [c.name for c in tl.clips[0].connected_clips] == ["ONE", "TWO", "THREE"]
    # offset stays on the PARENT's clock, as for every connected clip:
    # parent position + (offset - parent start) is where it plays
    one = next(c for c in tl.connected_clips if c.name == "ONE")
    assert one.offset.frames == 12 * FPS


def test_each_storyline_accounts_for_every_item(project):
    tl = _timeline(project)
    assert [(s.lane, s.start.frames, s.duration.frames, s.parent_clip_name) for s in tl.storylines] == [
        (1, 2 * FPS, 6 * FPS, "BASE"),
        (2, 11 * FPS, 5 * FPS, tl.storylines[1].parent_clip_name),
    ]
    first, second = tl.storylines
    assert [c.name for c in first.clips] == ["ONE", "TWO", "THREE"]
    assert [(g.frames, d.frames) for g, d in first.gaps] == [(6 * FPS, FPS)]
    assert first.skipped == [] and second.skipped == [] and second.gaps == []
    # a transition sits on its cut (start) with the part before it (lead)
    assert [(t.start.frames, t.lead.frames, t.duration.frames, t.lane) for t in first.transitions] == [
        (4 * FPS, FPS // 2, FPS, 1)]
    # the first item of a storyline can be a transition: it starts on the
    # storyline's first frame, none of it before
    assert [(t.start.frames, t.lead.frames, t.duration.frames, t.lane) for t in second.transitions] == [
        (11 * FPS, 0, FPS // 2, 2)]
    assert [c.name for c in second.clips] == ["FOUR", "FIVE"]
    # the spine's own transitions are not where a lane's go
    assert tl.transitions == []


def _call(tool, action, **args):
    import server

    return asyncio.run(server.call_tool(tool, {"action": action, "args": args}))[0].text


def _tc(frames: int) -> str:
    return f"00:00:{frames // FPS:02d}:{frames % FPS:02d}"


def test_every_reader_sees_the_storyline_clips(project):
    path = str(project)
    truth = _truth(project)

    assert "**Clips**: 1 clip + 6 connected" in _call("inspect", "analyze_timeline", filepath=path)

    listed = _call("inspect", "list_connected_clips", filepath=path)
    assert "**Total**: 6" in listed
    for name, (lane, first, _n) in truth.items():
        assert f"| {name} | {lane} | asset-clip | {_tc(first)} |" in listed, listed

    payload = extract_payload(_call("view", "view_timeline", filepath=path, thumbnails=False))
    rows = {c["name"]: (c["lane"], Fraction(c["start"])) for c in payload["clips"] if c["name"] != "BASE"}
    assert rows == {name: (lane, Fraction(first, FPS)) for name, (lane, first, _n) in truth.items()}

    text = _call("preview", "preview_timeline", filepath=path)
    for name, (lane, first, n) in truth.items():
        row = next((r for r in text.splitlines() if f" {name} " in r and r.split()[0].isdigit()), None)
        assert row is not None, (name, text)
        assert f"L{lane} " in row and _tc(first) in row and _tc(first + n) in row, row
    assert any(r.startswith("lane 2") for r in text.splitlines()), text

    gaps = _call("diagnose", "detect_gaps", filepath=path)
    assert "6 connected clips across 2 lanes were not checked" in gaps, gaps


def test_the_render_graph_draws_every_storyline_clip(project):
    graph = compile_timeline(_timeline(project))
    drawn = {}
    for track in lane_tracks(graph):
        for piece in track:
            drawn[graph.segments[piece.segment].label] = (piece.lane, piece.start, piece.frames)
    assert drawn == _truth(project)
    # what the proxy cannot draw is named, never silently cut
    assert any("2 transitions inside connected storylines" in note for note in graph.substitutions), \
        graph.substitutions


def test_the_writer_places_storyline_clips_on_their_own_clock(project):
    modifier = FCPXMLModifier(str(project))
    found = modifier.iter_connected_clips()
    placed = {element.get("name"): (lane, modifier.connected_timeline_offset(element, host) * FPS)
              for lane, element, host in found}
    assert placed == {name: (lane, first) for name, (lane, first, _n) in _truth(project).items()}
    # a storyline child's host is its storyline, whose clock starts at 0
    hosts = {element.get("name"): host.tag for _lane, element, host in found}
    assert hosts["ONE"] == "spine" and hosts["PLAIN"] == "gap"
    assert modifier.timeline_origin() == 0
    # and the inverse writes a timeline position back onto that clock
    one = next(e for _l, e, h in found if e.get("name") == "TWO")
    host = next(h for _l, e, h in found if e.get("name") == "TWO")
    assert modifier.connected_offset_attribute(Fraction(4), host) == parse_seconds(one.get("offset"))


def test_a_snap_moves_a_storyline_as_one(project, tmp_path):
    """A storyline's clips are magnetic to each other: moving one alone
    would overlap its neighbour or open a hole Final Cut never wrote. The
    storyline moves as one, by its own offset, and keeps its cuts."""
    modifier = FCPXMLModifier(str(project))
    report = modifier.snap_connected_clips([Fraction(2) + Fraction(2, FPS)], max_shift_frames=4)
    assert report["considered"] == 3  # PLAIN and two storylines
    assert [(m["lane"], m["shift_frames"]) for m in report["moved"]] == [(1, 2)]
    out = tmp_path / "snapped.fcpxml"
    modifier.save(str(out))
    before, after = _truth(project), _truth(out)
    for name in ("ONE", "TWO", "THREE"):
        assert after[name][1] == before[name][1] + 2, name
    for name in ("FOUR", "FIVE", "PLAIN"):
        assert after[name] == before[name], name
    # the children kept their offsets on the storyline's clock
    root = ET.parse(out).getroot()
    lane1 = next(s for s in root.iter("spine") if s.get("lane") == "1")
    assert [c.get("offset") for c in lane1] == ["0s", "3/2s", "2s", "4s", "5s"]


OLD_SHAPE = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="a1" name="Base" start="0s" duration="60s" hasVideo="1" src="file:///nope.mov"/>
  </resources>
  <library><event name="E"><project name="Old">
    <sequence format="r1" duration="10s">
      <spine>
        <asset-clip ref="a1" name="BASE" offset="0s" start="10s" duration="10s">
          <storyline lane="2">
            <asset-clip ref="a1" name="OLD" offset="12s" start="0s" duration="2s"/>
          </storyline>
        </asset-clip>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
"""


def test_the_invented_storyline_tag_still_reads_as_before(tmp_path):
    """Files shaped for the invented ``<storyline>`` (0.32 and earlier read
    it, Final Cut never wrote it) keep their old reading: no offset on the
    wrapper, so its clock is the parent's and OLD plays at 0 + 12s - 10s."""
    path = tmp_path / "old.fcpxml"
    path.write_text(OLD_SHAPE)
    tl = _timeline(path)
    assert [(c.name, c.lane, c.timeline_start.frames) for c in tl.connected_clips] == [("OLD", 2, 2 * FPS)]
    modifier = FCPXMLModifier(str(path))
    [(lane, element, host)] = modifier.iter_connected_clips()
    assert (lane, modifier.connected_timeline_offset(element, host)) == (2, 2)


REAL = os.environ.get("FCP_MCP_REAL_FCPXML", "")


@pytest.mark.skipif(not REAL, reason="set FCP_MCP_REAL_FCPXML to a Final Cut export to check it")
def test_a_real_export_keeps_every_storyline_item():
    """Run by hand against a real export (nothing from one is committed):
    every item in every connected spine is accounted for, clips as clips,
    gaps as gaps, transitions as transitions, and nothing else."""
    path = REAL
    if os.path.isdir(path):
        path = os.path.join(path, "Info.fcpxml")
    root = ET.parse(path).getroot()
    want = {"clips": 0, "gaps": 0, "transitions": 0, "other": 0}
    for spine in root.iter("spine"):
        if spine.get("lane") is None:
            continue
        for item in spine:
            key = {"gap": "gaps", "transition": "transitions"}.get(item.tag)
            if key is None:
                key = "clips" if item.tag in ("asset-clip", "clip", "video", "audio", "title", "ref-clip",
                                              "mc-clip", "sync-clip", "caption") else "other"
            want[key] += 1
    timelines = FCPXMLParser().parse_file(path).timelines
    storylines = [s for tl in timelines for s in tl.storylines]
    got = {
        "clips": sum(len(s.clips) for s in storylines),
        "gaps": sum(len(s.gaps) for s in storylines),
        "transitions": sum(len(s.transitions) for s in storylines),
        "other": sum(len(s.skipped) for s in storylines),
    }
    print(f"\nconnected spines: {len(storylines)}; items: {got}; "
          f"connected clips: {sum(len(tl.connected_clips) for tl in timelines)}")
    assert got == want
    # every storyline clip is on the timeline, at or after its storyline's start
    for s in storylines:
        assert all(c.timeline_start.frames >= s.start.frames for c in s.clips)
