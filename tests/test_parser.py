"""Tests for FCPXML parser."""

import tempfile
from pathlib import Path

import pytest

from fcpxml.models import MarkerType
from fcpxml.parser import FCPXMLParser, parse_fcpxml

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


def _fcpxml(spine_content="", resources_extra="", project_name="Test",
            frame_dur="1/24s", seq_dur="240/24s"):
    """Build a minimal FCPXML string for testing."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<fcpxml version="1.11"><resources>'
        f'<format id="r1" frameDuration="{frame_dur}" width="1920" height="1080"/>'
        f'{resources_extra}</resources>'
        f'<library location="file:///lib.fcpbundle/"><event name="Evt">'
        f'<project name="{project_name}">'
        f'<sequence format="r1" duration="{seq_dur}"><spine>'
        f'{spine_content}</spine></sequence></project>'
        f'</event></library></fcpxml>'
    )


ASSET_R2 = '<asset id="r2" name="A" src="file:///a.mov" start="0s" duration="100s"/>'
CLIP_A = '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="120/24s" format="r1"/>'


# ============================================================
# File-based Parsing (existing)
# ============================================================

def test_parse_sample():
    project = FCPXMLParser().parse_file(str(SAMPLE))
    assert project.name == "Music Video Edit"
    assert len(project.timelines) == 1


def test_timeline_props():
    tl = FCPXMLParser().parse_file(str(SAMPLE)).primary_timeline
    assert tl.frame_rate == 24.0
    assert tl.width == 1920


def test_clips_parsed():
    tl = FCPXMLParser().parse_file(str(SAMPLE)).primary_timeline
    assert len(tl.clips) > 0
    assert tl.clips[0].name == "Interview_A"


def test_markers_parsed():
    tl = FCPXMLParser().parse_file(str(SAMPLE)).primary_timeline
    assert len([m for m in tl.markers if m.marker_type == MarkerType.CHAPTER]) == 4


def test_short_clip_detection():
    assert len(FCPXMLParser().parse_file(str(SAMPLE)).primary_timeline
               .get_clips_shorter_than(0.5)) >= 1


# ============================================================
# Library Clips
# ============================================================

def test_get_library_clips_returns_assets():
    parser = FCPXMLParser()
    parser.parse_file(str(SAMPLE))
    assert len(parser.get_library_clips()) == 3


def test_get_library_clips_includes_metadata():
    parser = FCPXMLParser()
    parser.parse_file(str(SAMPLE))
    interview = next(c for c in parser.get_library_clips() if c['name'] == 'Interview_A')
    assert interview['asset_id'] == 'r2'
    assert interview['duration_seconds'] == 300.0
    assert interview['src'] is not None


def test_get_library_clips_filter_by_keyword():
    parser = FCPXMLParser()
    parser.parse_file(str(SAMPLE))
    assert len(parser.get_library_clips(keywords=['NonExistent'])) == 0


# ============================================================
# parse_string
# ============================================================

def test_parse_string_basic():
    xml = _fcpxml(CLIP_A, ASSET_R2, project_name="MyProject")
    project = FCPXMLParser().parse_string(xml)
    assert project.name == "MyProject"
    assert len(project.timelines) == 1


def test_parse_string_clip_properties():
    xml = _fcpxml(CLIP_A, ASSET_R2)
    clip = FCPXMLParser().parse_string(xml).primary_timeline.clips[0]
    assert clip.name == "A"
    assert clip.duration.seconds == pytest.approx(5.0, abs=0.01)
    assert clip.media_path == "file:///a.mov"


def test_parse_string_matches_file():
    from_file = FCPXMLParser().parse_file(str(SAMPLE))
    from_string = FCPXMLParser().parse_string(SAMPLE.read_text())
    assert from_file.name == from_string.name
    assert len(from_file.timelines[0].clips) == len(from_string.timelines[0].clips)


def test_parse_string_30fps():
    xml = _fcpxml(
        '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="300/30s" format="r1"/>',
        '<asset id="r2" name="A" src="a.mov" start="0s" duration="60s"/>',
        frame_dur="1/30s", seq_dur="300/30s",
    )
    assert FCPXMLParser().parse_string(xml).primary_timeline.frame_rate == 30.0


def test_parse_string_2997fps():
    xml = _fcpxml(
        '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="30000/1001s" format="r1"/>',
        '<asset id="r2" name="A" src="a.mov" start="0s" duration="60s"/>',
        frame_dur="1001/30000s", seq_dur="30000/1001s",
    )
    assert FCPXMLParser().parse_string(xml).primary_timeline.frame_rate == pytest.approx(29.97, abs=0.01)


# ============================================================
# Markers
# ============================================================

def test_standard_marker():
    clip_xml = ('<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
                '<marker start="24/24s" duration="1/24s" value="Mark1"/></asset-clip>')
    clip = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0]
    assert clip.markers[0].marker_type == MarkerType.STANDARD
    assert clip.markers[0].name == "Mark1"


def test_todo_marker():
    clip_xml = ('<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
                '<marker start="24/24s" duration="1/24s" value="Fix" completed="0"/></asset-clip>')
    m = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0].markers[0]
    assert m.marker_type == MarkerType.INCOMPLETE


def test_completed_marker():
    """Marker with completed='1' should be COMPLETED, not incomplete."""
    clip_xml = ('<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
                '<marker start="24/24s" duration="1/24s" value="Done" completed="1"/></asset-clip>')
    m = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0].markers[0]
    assert m.marker_type == MarkerType.COMPLETED


def test_multiple_marker_types_on_one_clip():
    """All four marker types coexist on a single clip without cross-contamination."""
    clip_xml = (
        '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
        '<marker start="6/24s" duration="1/24s" value="Note"/>'
        '<marker start="12/24s" duration="1/24s" value="Task" completed="0"/>'
        '<marker start="18/24s" duration="1/24s" value="Done" completed="1"/>'
        '<chapter-marker start="24/24s" duration="1/24s" value="Ch1"/>'
        '</asset-clip>'
    )
    clip = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0]
    by_name = {m.name: m.marker_type for m in clip.markers}
    assert by_name["Note"] == MarkerType.STANDARD
    assert by_name["Task"] == MarkerType.INCOMPLETE
    assert by_name["Done"] == MarkerType.COMPLETED
    assert by_name["Ch1"] == MarkerType.CHAPTER


def test_marker_without_completed_is_standard():
    """A plain <marker> with no completed attribute must parse as STANDARD, not incomplete."""
    clip_xml = ('<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
                '<marker start="6/24s" duration="1/24s" value="Plain"/></asset-clip>')
    m = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0].markers[0]
    assert m.marker_type == MarkerType.STANDARD
    assert m.name == "Plain"


# The strict exact-match tests below verify that only completed='0' and
# completed='1' (no whitespace, no variants) produce non-STANDARD types.

@pytest.mark.parametrize("completed_val,label", [
    (" 0 ", "space-padded zero"),
    (" 1 ", "space-padded one"),
    ("", "empty string"),
    ("\n0\n", "newline-padded zero"),
    ("\n1\n", "newline-padded one"),
    ("\t0\t", "tab-padded zero"),
    ("\r\n1\r\n", "crlf-padded one"),
    ("00", "double-zero"),
    ("true", "boolean-string true"),
])
def test_non_exact_completed_values_are_standard(completed_val, label):
    """completed attribute must be exactly '0' or '1' — anything else is STANDARD."""
    clip_xml = (
        '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
        f'<marker start="24/24s" duration="1/24s" value="{label}" completed="{completed_val}"/>'
        '</asset-clip>'
    )
    m = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0].markers[0]
    assert m.marker_type == MarkerType.STANDARD, (
        f"completed='{completed_val}' ({label}) should be STANDARD, got {m.marker_type}"
    )


def test_chapter_marker_ignores_completed_attribute():
    """A <chapter-marker> with completed='0' must still parse as CHAPTER, not as a to-do."""
    clip_xml = (
        '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="240/24s" format="r1">'
        '<chapter-marker start="24/24s" duration="1/24s" value="Ch" completed="0"/>'
        '</asset-clip>'
    )
    m = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2)).primary_timeline.clips[0].markers[0]
    assert m.marker_type == MarkerType.CHAPTER


def test_chapter_markers_on_sequence():
    # Chapter markers are children of sequence (parsed via findall .//chapter-marker)
    tl = FCPXMLParser().parse_file(str(SAMPLE)).primary_timeline
    chapters = [m for m in tl.markers if m.marker_type == MarkerType.CHAPTER]
    assert len(chapters) == 4
    assert chapters[0].name == "Intro"


# ============================================================
# Keywords
# ============================================================

def test_keyword_with_range():
    clip_xml = ('<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="120/24s" format="r1">'
                '<keyword start="0s" duration="48/24s" value="Interview"/></asset-clip>')
    kw = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2, seq_dur="120/24s")).primary_timeline.clips[0].keywords[0]
    assert kw.start is not None
    assert kw.duration.seconds == pytest.approx(2.0, abs=0.01)


def test_keyword_without_range():
    clip_xml = ('<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="120/24s" format="r1">'
                '<keyword value="B-Roll"/></asset-clip>')
    kw = FCPXMLParser().parse_string(_fcpxml(clip_xml, ASSET_R2, seq_dur="120/24s")).primary_timeline.clips[0].keywords[0]
    assert kw.start is None
    assert kw.duration is None


# ============================================================
# Transitions & Gaps
# ============================================================

def test_transition_parsed():
    spine = (CLIP_A
             + '<transition name="Cross Dissolve" offset="120/24s" duration="24/24s"/>'
             + '<asset-clip ref="r2" offset="120/24s" name="B" start="10s" duration="120/24s" format="r1"/>')
    tl = FCPXMLParser().parse_string(_fcpxml(spine, ASSET_R2)).primary_timeline
    assert len(tl.transitions) == 1
    assert tl.transitions[0].name == "Cross Dissolve"
    assert len(tl.clips) == 2


def test_gap_advances_offset():
    spine = (CLIP_A
             + '<gap offset="120/24s" duration="48/24s"/>'
             + '<asset-clip ref="r2" offset="168/24s" name="B" start="10s" duration="72/24s" format="r1"/>')
    tl = FCPXMLParser().parse_string(_fcpxml(spine, ASSET_R2, seq_dur="240/24s")).primary_timeline
    assert len(tl.clips) == 2
    assert tl.clips[1].start.frames == 168  # 120 clip + 48 gap


# ============================================================
# Error Handling
# ============================================================

def test_parse_file_not_found():
    with pytest.raises(FileNotFoundError):
        FCPXMLParser().parse_file("/nonexistent/path.fcpxml")


def test_parse_string_invalid_xml():
    with pytest.raises(Exception):
        FCPXMLParser().parse_string("<not valid xml")


def test_parse_empty_sequence():
    xml = ('<?xml version="1.0" encoding="UTF-8"?><fcpxml version="1.11"><resources>'
           '<format id="r1" frameDuration="1/24s" width="1920" height="1080"/></resources>'
           '<library location="file:///x/"><event name="E"><project name="Empty">'
           '<sequence format="r1" duration="0s"/></project></event></library></fcpxml>')
    project = FCPXMLParser().parse_string(xml)
    assert project.name == "Empty"
    assert len(project.primary_timeline.clips) == 0


def test_parse_no_project():
    xml = '<?xml version="1.0" encoding="UTF-8"?><fcpxml version="1.11"><resources/></fcpxml>'
    project = FCPXMLParser().parse_string(xml)
    assert project.name == "Untitled"
    assert len(project.timelines) == 0


def test_fcpxmld_missing_info():
    with tempfile.TemporaryDirectory(suffix=".fcpxmld") as tmpdir:
        with pytest.raises(FileNotFoundError, match="Info.fcpxml not found"):
            FCPXMLParser().parse_file(tmpdir)


# ============================================================
# Helpers & Convenience
# ============================================================

def test_duration_to_seconds_rational():
    assert FCPXMLParser()._parse_duration_to_seconds("150/30s") == pytest.approx(5.0)


def test_duration_to_seconds_plain():
    assert FCPXMLParser()._parse_duration_to_seconds("10s") == pytest.approx(10.0)


def test_duration_to_seconds_bare_number():
    assert FCPXMLParser()._parse_duration_to_seconds("5") == pytest.approx(5.0)


def test_duration_to_seconds_zero_denominator():
    """Zero-denominator rational returns 0.0 instead of crashing."""
    assert FCPXMLParser()._parse_duration_to_seconds("10/0s") == 0.0


def test_duration_to_seconds_multiple_slashes():
    """Malformed rational with extra slashes returns 0.0 (unparseable)."""
    # "10/20/30s" → int("20/30") raises ValueError inside TimeValue.from_timecode,
    # which _parse_duration_to_seconds catches and returns 0.0
    assert FCPXMLParser()._parse_duration_to_seconds("10/20/30s") == 0.0


def test_parse_fcpxml_convenience():
    project = parse_fcpxml(str(SAMPLE))
    assert project.name == "Music Video Edit"
    assert len(project.timelines[0].clips) == 9


def test_version_preserved():
    xml = _fcpxml(CLIP_A, ASSET_R2).replace('version="1.11"', 'version="1.10"')
    assert FCPXMLParser().parse_string(xml).fcpxml_version == "1.10"


def test_default_version():
    assert FCPXMLParser().parse_string('<fcpxml><resources/></fcpxml>').fcpxml_version == "1.11"


def test_asset_media_rep_fallback():
    """Asset with no direct src should fall back to media-rep child src."""
    asset_xml = (
        '<asset id="r2" name="MediaRep" start="0s" duration="100s">'
        '<media-rep src="file:///video.mov" kind="original-media"/>'
        '</asset>'
    )
    xml = _fcpxml(CLIP_A.replace('ref="r2"', 'ref="r2"'), asset_xml)
    parser = FCPXMLParser()
    parser.parse_string(xml)
    assert parser.resources.get('r2', {}).get('src') == 'file:///video.mov'


def test_asset_no_src_no_media_rep():
    """Asset with neither src attr nor media-rep child should yield empty string, not crash."""
    asset_xml = '<asset id="r2" name="NoSrc" start="0s" duration="100s"/>'
    xml = _fcpxml(CLIP_A, asset_xml)
    parser = FCPXMLParser()
    parser.parse_string(xml)
    assert parser.resources.get('r2', {}).get('src') == ''


def test_zero_denominator_frame_duration_raises():
    """frameDuration with zero denominator must raise, not silently set fps=0."""
    xml = _fcpxml(CLIP_A, ASSET_R2, frame_dur="1/0s")
    with pytest.raises(ValueError, match="denominator"):
        FCPXMLParser().parse_string(xml)


class TestSequenceFrameRateWins:
    """The sequence's own format decides the frame rate, not a source asset's.

    The parser used to overwrite the rate from every <format> in resources, so
    the LAST one in the file won. A 23.98 timeline in a project holding a
    single 25p drone clip reported 50.0 fps, and Timecode.total_frames /
    to_smpte were then wrong by that factor everywhere — including anything
    snapping a cut to a beat.
    """

    # r1 is the sequence (23.976). r9 is a source asset (50p) declared AFTER
    # it, which is the ordering that made last-wins pick the wrong one.
    XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat3840x2160p2398" frameDuration="1001/24000s" width="3840" height="2160"/>
    <format id="r9" name="FFVideoFormat1080p50" frameDuration="1/50s" width="1920" height="1080"/>
    <asset id="a1" name="drone" start="0s" duration="10s" hasVideo="1" hasAudio="0"/>
  </resources>
  <library>
    <event name="E">
      <project name="P">
        <sequence format="r1" duration="164s" tcStart="0s" tcFormat="NDF">
          <spine/>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""

    def _timeline(self, tmp_path):
        p = tmp_path / "rates.fcpxml"
        p.write_text(self.XML)
        return FCPXMLParser().parse_file(str(p)).primary_timeline

    def test_frame_rate_comes_from_the_sequence_format(self, tmp_path):
        tl = self._timeline(tmp_path)
        assert abs(tl.frame_rate - 24000 / 1001) < 0.001, (
            f"got {tl.frame_rate}; a later 50p asset format must not win"
        )

    def test_resolution_also_comes_from_the_sequence_format(self, tmp_path):
        tl = self._timeline(tmp_path)
        assert (tl.width, tl.height) == (3840, 2160)

    def test_frame_counts_use_the_sequence_rate(self, tmp_path):
        """The reason the rate matters: seconds-to-frames must be right."""
        tl = self._timeline(tmp_path)
        # 164s at 23.976 is ~3932 frames; at the wrong 50.0 it would be 8200.
        assert 3900 < tl.duration.total_frames < 3960
