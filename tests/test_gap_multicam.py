"""Issue #23: a gap-based primary storyline carrying ``mc-clip`` and ``caption``.

Final Cut Pro 12.3 exports every connected-clip multicam edit this way: the
spine holds only ``<gap>`` elements and the picture hangs off them in lanes.
Before the fix the parser dropped both element types (they were not in
``_CONNECTED_CLIP_TAGS``), never indexed ``<media>`` resources (so an
``mc-clip`` had no media path even on the spine), and every media tool read
``tl.clips`` alone. Result: 0 clips, health 100 %, 0 media files opened.
"""

import pytest

import server
from fcpxml.parser import FCPXMLParser

FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14">
    <resources>
        <format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s" width="1920" height="1080"/>
        <asset id="r2" name="angle-a-source" start="0s" duration="300/25s" hasVideo="1" hasAudio="1">
            <media-rep kind="original-media" src="{src_a}"/>
        </asset>
        <asset id="r4" name="angle-b-source" start="0s" duration="300/25s" hasVideo="1" hasAudio="1">
            <media-rep kind="original-media" src="{src_b}"/>
        </asset>
        <media id="r3" name="multicam-sequence">
            <multicam format="r1" tcStart="0s">
                <mc-angle name="angle-a" angleID="angle-a">
                    <asset-clip ref="r2" offset="0s" start="0s" duration="300/25s"/>
                </mc-angle>
                <mc-angle name="angle-b" angleID="angle-b">
                    <asset-clip ref="r4" offset="0s" start="0s" duration="300/25s"/>
                </mc-angle>
            </multicam>
        </media>
    </resources>
    <library location="file:///Volumes/library/library.fcpbundle/">
        <event name="event">
            <project name="project">
                <sequence duration="400/25s" format="r1" tcStart="0s">
                    <spine>
                        <gap name="Gap" offset="0s" duration="100/25s" start="0s"/>
                        <gap name="Gap" offset="100/25s" duration="300/25s" start="3600s">
                            <mc-clip ref="r3" offset="3600s" duration="200/25s" start="50/25s" lane="1" name="interview">
                                <mc-source angleID="angle-b" srcEnable="all"/>
                            </mc-clip>
                            <mc-clip ref="r3" offset="3608s" duration="100/25s" start="0s" lane="1" name="interview-2"/>
                            <caption lane="-1" offset="3600s" duration="100/25s" name="caption-1">
                                <text>Sample caption text</text>
                            </caption>
                        </gap>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""


@pytest.fixture
def project(tmp_path):
    a = tmp_path / "angle-a.mov"
    b = tmp_path / "angle-b.mov"
    a.write_bytes(b"\x00" * 64)
    b.write_bytes(b"\x00" * 64)
    path = tmp_path / "gap-multicam.fcpxml"
    path.write_text(FIXTURE.format(src_a=f"file://{a}", src_b=f"file://{b}"))
    return str(path), str(a), str(b)


def _timeline(path):
    return FCPXMLParser().parse_file(path).primary_timeline


class TestParser:
    def test_mc_clip_and_caption_are_connected_clips(self, project):
        tl = _timeline(project[0])
        assert tl.clips == [], "the spine is gaps only"
        types = sorted(c.clip_type for c in tl.connected_clips)
        assert types == ["caption", "mc-clip", "mc-clip"]

    def test_mc_clip_resolves_the_enabled_angle_to_its_asset(self, project):
        _, a, b = project
        tl = _timeline(project[0])
        by_name = {c.name: c for c in tl.connected_clips}
        assert by_name["interview"].media_path == f"file://{b}", "mc-source chose angle-b"
        assert by_name["interview-2"].media_path == f"file://{a}", "no mc-source: first angle"

    def test_unnamed_mc_clip_takes_the_multicam_name(self, project):
        """FCP omits ``name`` on the mc-clip in the reporter's export; the
        timeline shows the multicam's name, so we do too."""
        _, a, b = project
        xml = FIXTURE.format(src_a=f"file://{a}", src_b=f"file://{b}").replace(' name="interview"', "")
        tl = FCPXMLParser().parse_string(xml).primary_timeline
        names = [c.name for c in tl.connected_clips if c.clip_type == "mc-clip"]
        assert names == ["multicam-sequence", "interview-2"]

    def test_caption_carries_its_text_and_no_media(self, project):
        tl = _timeline(project[0])
        cap = next(c for c in tl.connected_clips if c.clip_type == "caption")
        assert cap.text == "Sample caption text"
        assert cap.media_path == ""
        assert cap.lane == -1

    def test_timeline_start_accounts_for_the_gap_origin(self, project):
        """The second gap sits at 4s on the timeline but its local time
        begins at 3600s. A connected clip at offset 3608s is therefore at
        4s + 8s = 12s, not 3608s and not 8s."""
        tl = _timeline(project[0])
        by_name = {c.name: c for c in tl.connected_clips}
        assert by_name["interview"].timeline_start.seconds == pytest.approx(4.0)
        assert by_name["interview-2"].timeline_start.seconds == pytest.approx(12.0)

    def test_media_clips_view_presents_connected_media_as_clips(self, project):
        _, a, b = project
        tl = _timeline(project[0])
        view = tl.media_clips()
        assert [c.name for c in view] == ["interview", "interview-2"], "caption has no media"
        first = view[0]
        assert first.media_path == f"file://{b}"
        assert first.start.seconds == pytest.approx(4.0), "timeline position, not in-point"
        assert first.source_start.seconds == pytest.approx(2.0), "in-point kept for mapping"
        assert first.duration.seconds == pytest.approx(8.0)

    def test_spine_mc_clip_also_resolves_media(self, tmp_path, project):
        """Cause 4: <media> was never indexed, so even a spine mc-clip had no path."""
        _, a, b = project
        xml = FIXTURE.format(src_a=f"file://{a}", src_b=f"file://{b}")
        xml = xml.replace(
            '<gap name="Gap" offset="100/25s" duration="300/25s" start="3600s">', ""
        ).replace("</gap>\n                    </spine>", "</spine>")
        xml = xml.replace(' lane="1"', "").replace(' lane="-1"', "")
        path = tmp_path / "spine-mc.fcpxml"
        path.write_text(xml)
        tl = _timeline(str(path))
        assert [c.media_path for c in tl.clips] == [f"file://{b}", f"file://{a}"]


class TestHandlers:
    async def test_list_connected_clips_reports_them(self, project):
        text = (await server.handle_list_connected_clips({"filepath": project[0]}))[0].text
        assert "No connected clips" not in text
        assert "**Total**: 3" in text
        assert "mc-clip" in text and "caption" in text

    async def test_list_clips_does_not_hide_a_gap_based_edit(self, project):
        text = (await server.handle_list_clips({"filepath": project[0]}))[0].text
        assert "interview" in text and "caption-1" in text
        assert "Lane" in text

    async def test_analyze_timeline_counts_connected_and_says_why(self, project):
        text = (await server.handle_analyze_timeline({"filepath": project[0]}))[0].text
        assert "**Connected Clips**: 3" in text
        assert "gap-based" in text.lower()

    async def test_validate_timeline_does_not_claim_a_clean_bill_on_zero_clips(self, project):
        text = (await server.handle_validate_timeline({"filepath": project[0]}))[0].text
        assert "gap-based" in text.lower()
        assert "connected clips" in text.lower()

    async def test_detect_media_silence_opens_the_angle_media(self, project, monkeypatch):
        filepath, a, b = project
        calls = []
        monkeypatch.setattr(
            server, "detect_silence", lambda p, **kw: calls.append(p) or [(0.0, 1.0)]
        )
        text = (await server.handle_detect_media_silence({"filepath": filepath}))[0].text
        assert sorted(calls) == sorted([a, b])
        assert "interview" in text

    async def test_detect_scenes_opens_the_angle_media(self, project, monkeypatch):
        filepath, a, b = project
        calls = []
        monkeypatch.setattr(
            "tools.scenes.scenes_cached",
            lambda p, *args, **kw: calls.append(p) or {"cuts": [3.0], "backend": "ffmpeg"},
        )
        result = await server.call_tool("scenes", {"action": "detect_scenes", "args": {"filepath": filepath}})
        assert sorted(calls) == sorted([a, b]), result[0].text


class TestFilters:
    def test_media_clips_view_on_a_spine_edit_is_the_spine(self, tmp_path):
        """Spine-based projects must not change shape under the view."""
        from tests.test_media_intel import PROJECT_XML

        media = tmp_path / "interview.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        path = tmp_path / "project.fcpxml"
        path.write_text(PROJECT_XML.format(src=f"file://{media}"))
        tl = _timeline(str(path))
        assert tl.media_clips() == tl.clips
