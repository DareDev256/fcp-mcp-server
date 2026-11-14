"""A marker on a gap is a timeline marker (0.33, bug 1 of gauntlet-033).

On a gap-based storyline (``examples/music-video.fcpxml``: the spine is one
``<gap>`` and every clip hangs off it) the mark tool writes a marker INSIDE
the gap, after the anchored clips, which is where Final Cut keeps it. Until
0.33 the parser read only the gap's anchored clips, so the markers were
written and then lost: view, inspect, list_markers and the text timeline all
showed none. Each gate below reads the OUTPUT of a real tool against a truth
computed here from the XML the mark tool wrote.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml.parser import FCPXMLParser
from fcpxml.rational import parse_seconds, rational_fps
from fcpxml.timeline_app import extract_payload

REPO = Path(__file__).resolve().parent.parent
MARKERS = [  # (timecode the tool is given, name, type)
    ("00:00:02:00", "Hook", "standard"),
    ("00:00:08:00", "Verse 1", "chapter"),
    ("00:00:12:00", "Fix colour", "todo"),
]


@pytest.fixture
def marked(tmp_path):
    import server

    src = tmp_path / "music-video.fcpxml"
    shutil.copy(REPO / "examples" / "music-video.fcpxml", src)
    out = tmp_path / "marked.fcpxml"
    reply = asyncio.run(server.handle_batch_add_markers({
        "filepath": str(src), "output_path": str(out),
        "markers": [{"timecode": tc, "name": name, "marker_type": kind} for tc, name, kind in MARKERS],
    }))[0].text
    assert reply.startswith("Added 3 markers"), reply
    return out


def _truth(path: Path) -> dict:
    """name -> (type, sequence-clock frames), from the XML: the gap's place in
    the spine plus how far into the gap's own clock the marker is."""
    root = ET.parse(path).getroot()
    fmt = root.find(".//format")
    fps = rational_fps(Fraction(1) / parse_seconds(fmt.get("frameDuration")))
    spine = root.find(".//sequence/spine")
    first = parse_seconds(spine[0].get("offset"))
    out = {}
    for gap in spine.findall("gap"):
        at = parse_seconds(gap.get("offset")) - first
        for m in gap:
            if m.tag in ("marker", "chapter-marker"):
                kind = "chapter" if m.tag == "chapter-marker" else ("todo" if m.get("completed") == "0" else
                                                                     "completed" if m.get("completed") == "1" else "standard")
                seconds = at + parse_seconds(m.get("start")) - parse_seconds(gap.get("start"))
                out[m.get("value")] = (kind, int(round(seconds * fps)))
    return out


def test_the_mark_tool_writes_the_markers_on_the_gap(marked):
    truth = _truth(marked)
    assert sorted(truth) == sorted(name for _tc, name, _k in MARKERS)
    # the timecodes asked for, on the 23.976 sequence: 48, 192 and 288 frames in
    assert {name: frames for name, (_k, frames) in truth.items()} == {"Hook": 48, "Verse 1": 192, "Fix colour": 288}


def test_a_gaps_markers_come_back_as_timeline_markers(marked):
    truth = _truth(marked)
    tl = FCPXMLParser().parse_file(str(marked)).timelines[0]
    got = {m.name: (m.marker_type.value, m.position.frames) for m in tl.markers}
    assert got == truth, got
    assert all(m.timeline_start is not None for m in tl.markers)


def test_every_reader_sees_the_three_markers(marked):
    import server

    path = str(marked)
    inspect = asyncio.run(server.TOOL_HANDLERS["analyze_timeline"]({"filepath": path}))[0].text
    assert "**Total**: 3" in inspect and "**Chapters**: 1" in inspect
    listed = asyncio.run(server.TOOL_HANDLERS["list_markers"]({"filepath": path}))[0].text
    assert listed.startswith("# Markers (3)")
    for tc, name, kind in MARKERS:
        assert f"| {tc} | {name} | {kind} |" in listed, listed
    view = asyncio.run(server.call_tool("view", {"action": "view_timeline", "args": {"filepath": path, "thumbnails": False}}))
    payload = extract_payload(view[0].text)
    rows = {m["name"]: (m["type"], m["start"]) for m in payload["markers"]}
    fps = rational_fps(Fraction(24000, 1001))
    truth = _truth(marked)
    assert rows == {name: (kind, f"{(Fraction(frames) / fps).numerator}/{(Fraction(frames) / fps).denominator}")
                    for name, (kind, frames) in truth.items()}, rows
    text = asyncio.run(server.call_tool("preview", {"action": "preview_timeline", "args": {"filepath": path}}))[0].text
    carets = [r for r in text.splitlines() if r.startswith("^")]
    assert len(carets) == 3, text
    for (tc, name, _kind), row in zip(MARKERS, carets):
        assert tc in row and name in row, row
    marker_row = next(r for r in text.splitlines() if r.startswith("markers"))
    assert marker_row.count("^") == 3, marker_row


def test_one_marker_is_one_marker(tmp_path):
    """The tool said "Added 1 markers"."""
    import server

    src = tmp_path / "mv.fcpxml"
    shutil.copy(REPO / "examples" / "music-video.fcpxml", src)
    reply = asyncio.run(server.handle_batch_add_markers({
        "filepath": str(src), "output_path": str(tmp_path / "one.fcpxml"),
        "markers": [{"timecode": "00:00:02:00", "name": "Hook"}]}))[0].text
    assert reply.startswith("Added 1 marker\n"), reply
    assert not re.search(r"\b1 markers\b", reply)
    json.dumps(reply)
