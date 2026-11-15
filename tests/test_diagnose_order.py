"""Two 0.34 fixes to what diagnose reports (residue owed since 0.32/0.33).

1. validate_timeline's Issues table listed issues in the order the checks
   ran, so on ``examples/sample.fcpxml`` three INFO rows about reused
   sources sat above nine offline-media FAILs. It is FAIL, WARN, INFO now,
   each severity in check order (the Summary table keeps check order: it is
   a list of checks, not of problems).
2. detect_flash_frames placed a connected clip by its raw ``offset``, which
   is on its PARENT's clock (gauntlet-033 bug 6): a 2-frame lane clip over
   the gym fixture's third Squat set (parent start 311/24 s, offset 359/24 s)
   was reported at 00:00:14:23 instead of where it plays, 00:00:17:03.

Each is broken on purpose once and seen red.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import server

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "examples" / "sample.fcpxml"
RANK = {"FAIL": 0, "WARN": 1, "INFO": 2}


def _text(tool: str, action: str, **args) -> str:
    return asyncio.run(server.call_tool(tool, {"action": action, "args": args}))[0].text


def issue_verdicts(report: str) -> list[str]:
    table = report[report.index("## Issues"):]
    return re.findall(r"^\| \S+ (FAIL|WARN|INFO) \|", table, re.M)


def test_issues_are_listed_fail_then_warn_then_info(monkeypatch):
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    verdicts = issue_verdicts(_text("diagnose", "validate_timeline", filepath=str(SAMPLE)))
    assert {"FAIL", "INFO"} <= set(verdicts), verdicts  # the fixture has both, or this proves nothing
    assert verdicts == sorted(verdicts, key=RANK.get), verdicts


def test_the_order_check_goes_red(monkeypatch):
    """Mutation check: the table in check order (0.33) puts INFO first here."""
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    monkeypatch.setattr(server, "_by_severity", lambda issues: list(issues))
    verdicts = issue_verdicts(_text("diagnose", "validate_timeline", filepath=str(SAMPLE)))
    assert verdicts != sorted(verdicts, key=RANK.get)


@pytest.fixture(scope="module")
def short_lane(tmp_path_factory):
    """The gym cut with its lane clip (Bench, over the third Squat set) cut
    to 2 frames: a flash on a connected clip whose parent is trimmed."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    out = tmp_path_factory.mktemp("gym")
    env = {"FCP_MCP_JOURNAL": "off", "FCP_MCP_INDEX": "off", "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, str(REPO / "demo" / "fixtures" / "gym_session.py"), str(out)],
                   check=True, capture_output=True, env=dict(os.environ, **env), timeout=600)
    text = (out / "edited.fcpxml").read_text()
    lane = 'name="Bench" start="0s" duration="3s" lane="1"'
    assert text.count(lane) == 1
    path = out / "short-lane.fcpxml"
    path.write_text(text.replace(lane, lane.replace('duration="3s"', 'duration="2/24s"')))
    return path


def lane_flash_timecode(report: str) -> str:
    row = next(line for line in report.splitlines() if line.startswith("| Bench |"))
    return row.split("|")[2].strip()


def test_a_connected_flash_is_reported_where_it_plays(short_lane, monkeypatch):
    from fcpxml.parser import FCPXMLParser

    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    tl = FCPXMLParser().parse_file(str(short_lane)).timelines[0]
    (lane,) = tl.connected_clips
    frames = int(lane.timeline_start.frames)
    truth = f"00:00:{frames // 24:02d}:{frames % 24:02d}"
    raw = int(lane.offset.frames)
    assert raw != frames, "the parent is not trimmed: this would prove nothing"
    assert lane_flash_timecode(_text("diagnose", "detect_flash_frames", filepath=str(short_lane))) == truth == "00:00:17:03"


def test_the_lane_flash_check_goes_red(short_lane, monkeypatch):
    """Mutation check: placing the lane clip by its raw offset (0.33) reports
    it 2 s early."""
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    original = server._detect_flash_frames

    def raw_offsets(tl, **kw):
        for c in tl.connected_clips:
            object.__setattr__(c, "timeline_start", None)
        return original(tl, **kw)

    monkeypatch.setattr(server, "_detect_flash_frames", raw_offsets)
    assert lane_flash_timecode(_text("diagnose", "detect_flash_frames", filepath=str(short_lane))) == "00:00:14:23"
