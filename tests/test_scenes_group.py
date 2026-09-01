"""detect_scenes / scenes_to_markers / scenes_split through the MCP seam.

The detector is monkeypatched to return known source-time cuts; what is
under test is the mapping into timeline time, the used-window filter, the
marker and split writes, and that the second call comes from the index.
"""

import pytest

import server
from fcpxml.parser import FCPXMLParser
from tests.conftest import requires_index

PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="take" start="0s" duration="20s" hasVideo="1" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="{src}"/>
    </asset>
  </resources>
  <library>
    <event name="Test">
      <project name="ScenesTest">
        <sequence format="r1" duration="16s" tcStart="0s">
          <spine>
            <gap name="Gap" offset="0s" start="0s" duration="10s"/>
            <asset-clip ref="r2" offset="10s" name="take" start="2s" duration="6s"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


@pytest.fixture
def project(tmp_path):
    media = tmp_path / "take.mp4"
    media.write_bytes(b"\x00" * 128)
    path = tmp_path / "project.fcpxml"
    path.write_text(PROJECT.format(src=f"file://{media}"))
    return str(path), str(media)


@pytest.fixture
def detector(monkeypatch):
    calls = []

    def fake(path, backend="auto", threshold=None, min_scene_len=0.5):
        calls.append(path)
        # Source cuts at 1s (before the used window 2s-8s), 4s and 6s (inside), 9s (after).
        return {"backend": "content", "cuts": [1.0, 4.0, 6.0, 9.0],
                "scenes": [(0.0, 1.0), (1.0, 4.0), (4.0, 6.0), (6.0, 9.0), (9.0, 20.0)], "duration": 20.0}

    monkeypatch.setattr("tools.scenes.detect_scenes", fake)
    return calls


async def _call(action, args):
    return (await server.call_tool("scenes", {"action": action, "args": args}))[0].text


class TestDetect:
    async def test_maps_used_window_cuts_into_timeline_time(self, project, detector):
        text = await _call("detect_scenes", {"filepath": project[0]})
        assert "**Cuts found**: 2" in text
        assert "**Backend**: content" in text
        # source 4s -> timeline 10 + (4-2) = 12s; source 6s -> 14s
        assert "| take | 4.000s | 12.000s |" in text
        assert "| take | 6.000s | 14.000s |" in text
        assert "1.000s" not in text and "9.000s" not in text

    async def test_ffmpeg_only_result_suggests_the_extra(self, project, monkeypatch):
        monkeypatch.setattr(
            "tools.scenes.detect_scenes",
            lambda p, **kw: {"backend": "ffmpeg", "cuts": [4.0], "scenes": [(0, 4.0), (4.0, 20.0)], "duration": 20.0},
        )
        text = await _call("detect_scenes", {"filepath": project[0]})
        assert "fcp-mcp-server[scenes]" in text

    async def test_missing_media_is_skipped(self, tmp_path, detector):
        path = tmp_path / "p.fcpxml"
        path.write_text(PROJECT.format(src="file:///nope/take.mp4"))
        text = await _call("detect_scenes", {"filepath": str(path)})
        assert "media file missing" in text
        assert detector == []

    async def test_bad_backend_is_rejected(self, project, detector):
        text = await _call("detect_scenes", {"filepath": project[0], "backend": "magic"})
        assert "Validation error" in text and "magic" in text
        assert detector == []

    @requires_index
    async def test_second_call_comes_from_the_index(self, project, detector):
        a = await _call("detect_scenes", {"filepath": project[0]})
        b = await _call("detect_scenes", {"filepath": project[0]})
        assert detector == [project[1]]
        assert a == b


class TestMarkers:
    async def test_markers_land_at_timeline_cuts(self, project, detector):
        text = await _call("scenes_to_markers", {"filepath": project[0]})
        assert "Added 2 markers" in text
        out = text.split("Saved to: ")[1].splitlines()[0].strip()
        tl = FCPXMLParser().parse_file(out).primary_timeline
        names = {m.name: m for c in tl.clips for m in c.markers}
        assert set(names) == {"take cut 1", "take cut 2"}
        # The writer stores marker start relative to the clip's timeline offset
        # (timeline 12s and 14s inside a clip at offset 10s -> 2s and 4s).
        starts = sorted(float(m.start.seconds) for m in names.values())
        assert starts == [2.0, 4.0]


class TestSplit:
    async def test_split_yields_one_more_piece_than_cuts(self, project, detector):
        text = await _call("scenes_split", {"filepath": project[0]})
        assert "Split 1 clips into 3 pieces" in text
        out = text.split("Saved to: ")[1].splitlines()[0].strip()
        tl = FCPXMLParser().parse_file(out).primary_timeline
        takes = [c for c in tl.clips if c.name == "take"]
        assert [float(c.start.seconds) for c in takes] == [10.0, 12.0, 14.0]
        assert [float(c.duration.seconds) for c in takes] == [2.0, 2.0, 2.0]
        assert [float(c.source_start.seconds) for c in takes] == [2.0, 4.0, 6.0]
