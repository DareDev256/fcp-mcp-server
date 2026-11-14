"""Tests for v0.5.0 features: connected clips, roles, diff, reformat, silence, export."""

import os
import tempfile
import xml.etree.ElementTree as ET

import pytest

from fcpxml.diff import ClipDiff, MarkerDiff, TimelineDiff, compare_timelines
from fcpxml.export import DaVinciExporter
from fcpxml.models import (
    Clip,
    CompoundClip,
    ConnectedClip,
    SilenceCandidate,
    Timecode,
    Timeline,
)
from fcpxml.parser import FCPXMLParser
from fcpxml.writer import FCPXMLModifier

# ============================================================================
# TEST FIXTURES
# ============================================================================

SAMPLE = os.path.join(os.path.dirname(__file__), '..', 'examples', 'sample.fcpxml')

CONNECTED_CLIPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="Interview" src="file:///interview.mov" start="0s" duration="2400/24s" hasVideo="1" hasAudio="1"/>
        <asset id="r3" name="BRoll_City" src="file:///broll.mov" start="0s" duration="1200/24s" hasVideo="1" hasAudio="1"/>
        <asset id="r4" name="Music_BG" src="file:///music.mp3" start="0s" duration="4800/24s" hasVideo="0" hasAudio="1"/>
        <asset id="r5" name="Title_Intro" src="" start="0s" duration="120/24s" hasVideo="1" hasAudio="0"/>
    </resources>
    <library>
        <event name="Test Event">
            <project name="Connected Clips Test">
                <sequence format="r1" duration="4800/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="Interview" start="0s" duration="2400/24s" audioRole="dialogue">
                            <marker start="240/24s" duration="1/24s" value="Good take"/>
                            <keyword value="interview" start="0s" duration="2400/24s"/>
                            <asset-clip ref="r3" lane="1" offset="240/24s" name="BRoll_City" start="0s" duration="480/24s" videoRole="b-roll"/>
                            <asset-clip ref="r4" lane="-1" offset="0s" name="Music_BG" start="0s" duration="2400/24s" audioRole="music"/>
                        </asset-clip>
                        <gap offset="2400/24s" duration="120/24s">
                            <asset-clip ref="r5" lane="1" offset="2400/24s" name="Title_Intro" start="0s" duration="120/24s"/>
                        </gap>
                        <asset-clip ref="r2" offset="2520/24s" name="Interview_2" start="2400/24s" duration="2280/24s" audioRole="dialogue" videoRole="video">
                            <spine lane="2" offset="2448/24s">
                                <asset-clip ref="r3" offset="0s" name="BRoll_Street" start="480/24s" duration="240/24s"/>
                            </spine>
                        </asset-clip>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""


ROLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="Clip_A" src="file:///a.mov" start="0s" duration="240/24s"/>
        <asset id="r3" name="Clip_B" src="file:///b.mov" start="0s" duration="240/24s"/>
        <asset id="r4" name="Clip_C" src="file:///c.mov" start="0s" duration="240/24s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Roles Test">
                <sequence format="r1" duration="720/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="Clip_A" start="0s" duration="240/24s" audioRole="dialogue"/>
                        <asset-clip ref="r3" offset="240/24s" name="Clip_B" start="0s" duration="240/24s" audioRole="music"/>
                        <asset-clip ref="r4" offset="480/24s" name="Clip_C" start="0s" duration="240/24s" audioRole="dialogue" videoRole="titles"/>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""


SILENCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
        <asset id="r2" name="Clip_Main" src="file:///main.mov" start="0s" duration="300/30s"/>
        <asset id="r3" name="Silence_Fill" src="file:///silence.mov" start="0s" duration="60/30s"/>
        <asset id="r4" name="Short" src="file:///short.mov" start="0s" duration="10/30s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="Silence Test">
                <sequence format="r1" duration="670/30s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="Clip_Main" start="0s" duration="300/30s"/>
                        <gap offset="300/30s" duration="60/30s"/>
                        <asset-clip ref="r3" offset="360/30s" name="Silence_Fill" start="0s" duration="60/30s"/>
                        <asset-clip ref="r4" offset="420/30s" name="Short" start="0s" duration="10/30s"/>
                        <asset-clip ref="r2" offset="430/30s" name="Clip_Main_2" start="0s" duration="240/30s"/>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""


def _write_temp_fcpxml(xml_str: str) -> str:
    """Write XML string to a temp file and return path."""
    fd, path = tempfile.mkstemp(suffix='.fcpxml')
    with os.fdopen(fd, 'w') as f:
        f.write(xml_str)
    return path


# ============================================================================
# CONNECTED CLIPS TESTS
# ============================================================================

class TestConnectedClips:

    def test_parse_connected_clips_from_spine(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            assert tl is not None
            assert len(tl.connected_clips) > 0
        finally:
            os.unlink(path)

    def test_connected_clip_has_lane(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            broll = [c for c in tl.connected_clips if c.name == "BRoll_City"]
            assert len(broll) == 1
            assert broll[0].lane == 1
        finally:
            os.unlink(path)

    def test_connected_clip_negative_lane(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            music = [c for c in tl.connected_clips if c.name == "Music_BG"]
            assert len(music) == 1
            assert music[0].lane == -1
        finally:
            os.unlink(path)

    def test_connected_clips_on_parent_clip(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            interview = tl.clips[0]
            assert len(interview.connected_clips) == 2  # BRoll + Music
        finally:
            os.unlink(path)

    def test_gap_connected_clips(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            title = [c for c in tl.connected_clips if c.name == "Title_Intro"]
            assert len(title) == 1
        finally:
            os.unlink(path)

    def test_secondary_storyline_clips(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            street = [c for c in tl.connected_clips if c.name == "BRoll_Street"]
            assert len(street) == 1
            assert street[0].lane == 2
            # a connected storyline is <spine lane offset> (there is no
            # <storyline> in Apple's DTD): its offset on Interview_2's clock
            # (from 100s) is 102s, so BRoll_Street plays 2s into Interview_2
            assert street[0].timeline_start.seconds == 107
        finally:
            os.unlink(path)

    def test_connected_clip_role(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            music = [c for c in tl.connected_clips if c.name == "Music_BG"]
            assert music[0].role == "music"
        finally:
            os.unlink(path)

    def test_connected_clip_parent_name(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            broll = [c for c in tl.connected_clips if c.name == "BRoll_City"]
            assert broll[0].parent_clip_name == "Interview"
        finally:
            os.unlink(path)

    def test_total_connected_clips_count(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            # BRoll_City, Music_BG on Interview
            # Title_Intro on gap
            # BRoll_Street in storyline on Interview_2
            assert len(tl.connected_clips) == 4
        finally:
            os.unlink(path)

    def test_add_connected_clip(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        out = path.replace('.fcpxml', '_modified.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.add_connected_clip(
                parent_clip_id="Interview",
                asset_id="r3",
                offset="480/24s",
                duration="240/24s",
                lane=1,
            )
            modifier.save(out)

            # Verify the connected clip was added
            tree = ET.parse(out)
            interview = tree.find('.//asset-clip[@name="Interview"]')
            connected = [c for c in interview if c.get('lane') == '1']
            assert len(connected) == 2  # Original BRoll_City + new one
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_connected_clip_model_duration_seconds(self):
        cc = ConnectedClip(
            name="Test", start=Timecode(0, 24.0),
            duration=Timecode(48, 24.0), lane=1,
        )
        assert cc.duration_seconds == 2.0

    def test_chapter_markers_on_connected_clips(self):
        """Chapter markers on connected clips must be parsed — not silently dropped."""
        from fcpxml.models import MarkerType

        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="Main" src="main.mov" start="0s" duration="240/24s"/>
        <asset id="r3" name="Overlay" src="overlay.mov" start="0s" duration="120/24s"/>
    </resources>
    <library>
        <event name="Test">
            <project name="ChapterOnConnected">
                <sequence format="r1" duration="240/24s">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="Main" start="0s" duration="240/24s">
                            <asset-clip ref="r3" lane="1" offset="24/24s" name="Overlay"
                                        start="0s" duration="120/24s">
                                <chapter-marker start="0s" duration="1/24s" value="CH1"/>
                                <marker start="48/24s" duration="1/24s" value="Note"/>
                            </asset-clip>
                        </asset-clip>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>"""
        path = _write_temp_fcpxml(xml)
        try:
            project = FCPXMLParser().parse_file(path)
            tl = project.primary_timeline
            overlay = [c for c in tl.connected_clips if c.name == "Overlay"]
            assert len(overlay) == 1
            markers = overlay[0].markers
            assert len(markers) == 2
            by_name = {m.name: m.marker_type for m in markers}
            assert by_name["CH1"] == MarkerType.CHAPTER
            assert by_name["Note"] == MarkerType.STANDARD
        finally:
            os.unlink(path)

    def test_compound_clip_model(self):
        cc = CompoundClip(
            name="Nested", ref_id="r5",
            duration=Timecode(120, 24.0),
            start=Timecode(0, 24.0),
        )
        assert cc.duration_seconds == 5.0
        assert cc.ref_id == "r5"


# ============================================================================
# ROLES TESTS
# ============================================================================

class TestRoles:

    def test_parse_audio_role(self):
        path = _write_temp_fcpxml(ROLES_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            clip_a = tl.clips[0]
            assert clip_a.audio_role == "dialogue"
        finally:
            os.unlink(path)

    def test_parse_video_role(self):
        path = _write_temp_fcpxml(ROLES_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            clip_c = tl.clips[2]
            assert clip_c.video_role == "titles"
        finally:
            os.unlink(path)

    def test_parse_no_role_is_empty(self):
        path = _write_temp_fcpxml(ROLES_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            clip_a = tl.clips[0]
            assert clip_a.video_role == ""
        finally:
            os.unlink(path)

    def test_assign_role(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_role.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.assign_role("Clip_A", audio_role="effects")
            modifier.save(out)

            tree = ET.parse(out)
            clip = tree.find('.//asset-clip[@name="Clip_A"]')
            assert clip.get('audioRole') == 'effects'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_assign_video_role(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_role.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.assign_role("Clip_B", video_role="titles")
            modifier.save(out)

            tree = ET.parse(out)
            clip = tree.find('.//asset-clip[@name="Clip_B"]')
            assert clip.get('videoRole') == 'titles'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_assign_role_invalid_clip(self):
        path = _write_temp_fcpxml(ROLES_XML)
        try:
            modifier = FCPXMLModifier(path)
            with pytest.raises(ValueError, match="Clip not found"):
                modifier.assign_role("Nonexistent", audio_role="music")
        finally:
            os.unlink(path)

    def test_clip_role_fields_default_empty(self):
        clip = Clip(
            name="Test", start=Timecode(0, 24.0),
            duration=Timecode(24, 24.0),
        )
        assert clip.audio_role == ""
        assert clip.video_role == ""

    def test_connected_clip_role_parsed(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        try:
            parser = FCPXMLParser()
            project = parser.parse_file(path)
            tl = project.primary_timeline
            broll = [c for c in tl.connected_clips if c.name == "BRoll_City"]
            assert broll[0].role == "b-roll"
        finally:
            os.unlink(path)


# ============================================================================
# TIMELINE DIFF TESTS
# ============================================================================

class TestTimelineDiff:

    def test_identical_timelines_no_changes(self):
        path = _write_temp_fcpxml(ROLES_XML)
        try:
            diff = compare_timelines(path, path)
            assert not diff.has_changes
        finally:
            os.unlink(path)

    def test_added_clip_detected(self):
        xml_a = ROLES_XML
        xml_b = ROLES_XML.replace('duration="720/24s"', 'duration="960/24s"')
        # Add a new clip to xml_b
        xml_b = xml_b.replace(
            '</spine>',
            '<asset-clip ref="r2" offset="720/24s" name="Clip_D" start="0s" duration="240/24s"/>\n</spine>'
        )
        path_a = _write_temp_fcpxml(xml_a)
        path_b = _write_temp_fcpxml(xml_b)
        try:
            diff = compare_timelines(path_a, path_b)
            added = [d for d in diff.clip_diffs if d.action == "added"]
            assert len(added) >= 1
            assert any(d.clip_name == "Clip_D" for d in added)
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_removed_clip_detected(self):
        xml_a = ROLES_XML
        # Remove Clip_C from xml_b
        xml_b = xml_a.replace(
            '<asset-clip ref="r4" offset="480/24s" name="Clip_C" start="0s" duration="240/24s" audioRole="dialogue" videoRole="titles"/>',
            ''
        )
        path_a = _write_temp_fcpxml(xml_a)
        path_b = _write_temp_fcpxml(xml_b)
        try:
            diff = compare_timelines(path_a, path_b)
            removed = [d for d in diff.clip_diffs if d.action == "removed"]
            assert len(removed) >= 1
            assert any(d.clip_name == "Clip_C" for d in removed)
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_trimmed_clip_detected(self):
        xml_a = ROLES_XML
        xml_b = xml_a.replace(
            'name="Clip_A" start="0s" duration="240/24s"',
            'name="Clip_A" start="0s" duration="120/24s"'
        )
        path_a = _write_temp_fcpxml(xml_a)
        path_b = _write_temp_fcpxml(xml_b)
        try:
            diff = compare_timelines(path_a, path_b)
            trimmed = [d for d in diff.clip_diffs if d.action == "trimmed"]
            assert len(trimmed) >= 1
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_marker_added_detected(self):
        xml_a = ROLES_XML
        # Replace the self-closing Clip_A tag with one containing a marker
        xml_b = xml_a.replace(
            '<asset-clip ref="r2" offset="0s" name="Clip_A" start="0s" duration="240/24s" audioRole="dialogue"/>',
            '<asset-clip ref="r2" offset="0s" name="Clip_A" start="0s" duration="240/24s" audioRole="dialogue">'
            '<marker start="0s" duration="1/24s" value="New Marker"/>'
            '</asset-clip>'
        )
        path_a = _write_temp_fcpxml(xml_a)
        path_b = _write_temp_fcpxml(xml_b)
        try:
            diff = compare_timelines(path_a, path_b)
            marker_added = [d for d in diff.marker_diffs if d.action == "added"]
            assert len(marker_added) >= 1
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_format_change_detected(self):
        xml_a = ROLES_XML
        xml_b = xml_a.replace('width="1920" height="1080"', 'width="1080" height="1920"')
        path_a = _write_temp_fcpxml(xml_a)
        path_b = _write_temp_fcpxml(xml_b)
        try:
            diff = compare_timelines(path_a, path_b)
            assert len(diff.format_changes) >= 1
            assert "Resolution" in diff.format_changes[0]
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_diff_result_total_changes(self):
        diff = TimelineDiff(
            timeline_a_name="A", timeline_b_name="B",
            clip_diffs=[ClipDiff(action="added", clip_name="X")],
            marker_diffs=[MarkerDiff(action="removed", marker_name="M")],
        )
        assert diff.total_changes == 2

    def test_diff_result_no_changes(self):
        diff = TimelineDiff(timeline_a_name="A", timeline_b_name="B")
        assert not diff.has_changes
        assert diff.total_changes == 0


# ============================================================================
# SOCIAL MEDIA REFORMAT TESTS
# ============================================================================

class TestReformat:

    def test_reformat_9_16(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_reformat.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.reformat_resolution(1080, 1920)
            modifier.save(out)

            tree = ET.parse(out)
            fmt = tree.find('.//format')
            assert fmt.get('width') == '1080'
            assert fmt.get('height') == '1920'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_reformat_1_1(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_reformat.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.reformat_resolution(1080, 1080)
            modifier.save(out)

            tree = ET.parse(out)
            fmt = tree.find('.//format')
            assert fmt.get('width') == '1080'
            assert fmt.get('height') == '1080'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_reformat_preserves_clips(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_reformat.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.reformat_resolution(1080, 1350)
            modifier.save(out)

            parser = FCPXMLParser()
            project = parser.parse_file(out)
            tl = project.primary_timeline
            assert len(tl.clips) == 3  # Same clip count
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_reformat_preserves_frame_rate(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_reformat.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            modifier.reformat_resolution(1080, 1920)
            modifier.save(out)

            tree = ET.parse(out)
            fmt = tree.find('.//format')
            assert 'frameDuration' in fmt.attrib
            assert fmt.get('frameDuration') == '1/24s'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_social_formats_dict(self):
        formats = FCPXMLModifier.SOCIAL_FORMATS
        assert formats["9:16"] == (1080, 1920)
        assert formats["1:1"] == (1080, 1080)
        assert formats["4:5"] == (1080, 1350)
        assert formats["16:9"] == (1920, 1080)
        assert formats["4:3"] == (1440, 1080)


# ============================================================================
# SILENCE DETECTION TESTS
# ============================================================================

class TestSilenceDetection:

    def test_detect_gap_as_silence(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        try:
            modifier = FCPXMLModifier(path)
            candidates = modifier.detect_silence_candidates(min_gap_seconds=0.5)
            gaps = [c for c in candidates if c['reason'] == 'gap']
            assert len(gaps) >= 1
        finally:
            os.unlink(path)

    def test_detect_name_match(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        try:
            modifier = FCPXMLModifier(path)
            candidates = modifier.detect_silence_candidates()
            name_matches = [c for c in candidates if c['reason'] == 'name_match']
            assert len(name_matches) >= 1
            assert any('Silence' in (c.get('clip_name') or '') for c in name_matches)
        finally:
            os.unlink(path)

    def test_detect_ultra_short(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        try:
            modifier = FCPXMLModifier(path)
            candidates = modifier.detect_silence_candidates()
            ultra_short = [c for c in candidates if c['reason'] == 'ultra_short']
            assert len(ultra_short) >= 1
        finally:
            os.unlink(path)

    def test_confidence_filtering(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        try:
            modifier = FCPXMLModifier(path)
            all_candidates = modifier.detect_silence_candidates()
            high_conf = [c for c in all_candidates if c['confidence'] >= 0.8]
            assert len(high_conf) > 0
        finally:
            os.unlink(path)

    def test_remove_silence_mark_mode(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        out = path.replace('.fcpxml', '_cleaned.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            actions = modifier.remove_silence_candidates(mode="mark", min_confidence=0.7)
            modifier.save(out)
            assert len(actions) > 0
            assert all(a['action'] == 'marked' for a in actions)
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_remove_silence_delete_mode(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        out = path.replace('.fcpxml', '_cleaned.fcpxml')
        try:
            modifier = FCPXMLModifier(path)
            actions = modifier.remove_silence_candidates(mode="delete", min_confidence=0.8)
            modifier.save(out)
            # Should have deleted something
            assert len(actions) > 0
            assert all(a['action'] == 'deleted' for a in actions)
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_custom_patterns(self):
        path = _write_temp_fcpxml(SILENCE_XML)
        try:
            modifier = FCPXMLModifier(path)
            candidates = modifier.detect_silence_candidates(patterns=["nonexistent_pattern"])
            name_matches = [c for c in candidates if c['reason'] == 'name_match']
            assert len(name_matches) == 0
        finally:
            os.unlink(path)

    def test_silence_candidate_model(self):
        sc = SilenceCandidate(
            start_timecode="00:00:05:00",
            duration_seconds=2.0,
            reason="gap",
            confidence=0.9,
        )
        assert sc.confidence == 0.9
        assert sc.clip_name is None


# ============================================================================
# EXPORT TESTS
# ============================================================================

class TestExport:

    def test_export_resolve_xml_version(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_resolve.fcpxml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_simplified_fcpxml(out)

            tree = ET.parse(out)
            root = tree.getroot()
            assert root.get('version') == '1.9'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_export_resolve_preserves_clips(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_resolve.fcpxml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_simplified_fcpxml(out)

            parser = FCPXMLParser()
            project = parser.parse_file(out)
            tl = project.primary_timeline
            assert len(tl.clips) == 3
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_export_xmeml_structure(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_fcp7.xml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_xmeml(out)

            tree = ET.parse(out)
            root = tree.getroot()
            assert root.tag == 'xmeml'
            assert root.get('version') == '5'

            sequence = root.find('sequence')
            assert sequence is not None
            assert sequence.find('name') is not None
            assert sequence.find('media') is not None
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_export_xmeml_has_video_track(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_fcp7.xml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_xmeml(out)

            tree = ET.parse(out)
            video_tracks = tree.findall('.//media/video/track')
            assert len(video_tracks) >= 1

            clipitems = video_tracks[0].findall('clipitem')
            assert len(clipitems) == 3  # 3 clips from primary storyline
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_export_xmeml_clipitem_has_timing(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_fcp7.xml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_xmeml(out)

            tree = ET.parse(out)
            clipitem = tree.find('.//clipitem')
            assert clipitem.find('name') is not None
            assert clipitem.find('duration') is not None
            assert clipitem.find('start') is not None
            assert clipitem.find('end') is not None
            assert clipitem.find('in') is not None
            assert clipitem.find('out') is not None
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_export_xmeml_connected_clips_to_tracks(self):
        path = _write_temp_fcpxml(CONNECTED_CLIPS_XML)
        out = path.replace('.fcpxml', '_fcp7.xml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_xmeml(out)

            tree = ET.parse(out)
            video_tracks = tree.findall('.//media/video/track')
            # Track 0 = primary, tracks 1+ = connected video clips
            assert len(video_tracks) >= 2
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)

    def test_export_xmeml_rate_element(self):
        path = _write_temp_fcpxml(ROLES_XML)
        out = path.replace('.fcpxml', '_fcp7.xml')
        try:
            exporter = DaVinciExporter(path)
            exporter.export_xmeml(out)

            tree = ET.parse(out)
            rate = tree.find('.//sequence/rate')
            assert rate is not None
            assert rate.find('timebase').text == '24'
            assert rate.find('ntsc').text == 'FALSE'
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)


# ============================================================================
# BACKWARD COMPATIBILITY TESTS
# ============================================================================

class TestBackwardCompatibility:

    def test_existing_sample_still_parses(self):
        parser = FCPXMLParser()
        project = parser.parse_file(SAMPLE)
        tl = project.primary_timeline
        assert tl is not None
        assert len(tl.clips) > 0

    def test_existing_sample_no_connected_clips(self):
        parser = FCPXMLParser()
        project = parser.parse_file(SAMPLE)
        tl = project.primary_timeline
        assert len(tl.connected_clips) == 0

    def test_clip_without_roles_has_empty_strings(self):
        parser = FCPXMLParser()
        project = parser.parse_file(SAMPLE)
        tl = project.primary_timeline
        for clip in tl.clips:
            assert isinstance(clip.audio_role, str)
            assert isinstance(clip.video_role, str)

    def test_timeline_new_fields_default_empty(self):
        tl = Timeline(
            name="Test",
            duration=Timecode(100, 24.0),
        )
        assert tl.connected_clips == []
        assert tl.compound_clips == []
