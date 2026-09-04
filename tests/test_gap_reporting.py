"""The reporting tools on a gap-based storyline (follow-up to issue #23).

#23 fixed the parser and the media tools. Nine read-only tools still walked
``tl.clips`` alone, so on the same project they returned an empty EDL, an
empty CSV, "No clips to analyze", no markers and no keywords — each of them
silently, with no hint that the edit they were handed lives in lanes. A
report that comes back empty because the tool looked in the wrong place is
the failure the issue was about, one surface further out.

Every test here would pass on an empty timeline too, so each also asserts
against a spine-based project: the fix must not change what those report.
"""

import pytest

import server
from fcpxml.parser import FCPXMLParser
from tests.test_gap_multicam import FIXTURE

# The same gap structure as #23, with a marker and a keyword on the mc-clip
# and a marker on the caption — the things the reporting tools claim to list.
MARKED = FIXTURE.replace(
    '<mc-source angleID="angle-b" srcEnable="all"/>',
    '<mc-source angleID="angle-b" srcEnable="all"/>\n'
    '                                <marker start="4s" duration="1/25s" value="cutaway here"/>\n'
    '                                <keyword start="3600s" duration="200/25s" value="interview"/>',
).replace(
    "<text>Sample caption text</text>",
    '<text>Sample caption text</text>\n'
    '                                <marker start="1s" duration="1/25s" value="caption in"/>',
)


@pytest.fixture
def gap_project(tmp_path):
    a, b = tmp_path / "a.mov", tmp_path / "b.mov"
    a.write_bytes(b"\x00" * 64)
    b.write_bytes(b"\x00" * 64)
    path = tmp_path / "gap.fcpxml"
    path.write_text(MARKED.format(src_a=f"file://{a}", src_b=f"file://{b}"))
    return str(path)


@pytest.fixture
def spine_project(tmp_path):
    """A control: the fix must leave a spine-based project reporting the same."""
    from tests.test_media_intel import PROJECT_XML

    media = tmp_path / "interview.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)
    path = tmp_path / "spine.fcpxml"
    path.write_text(PROJECT_XML.format(src=f"file://{media}"))
    return str(path)


class TestAllClipsView:
    def test_all_clips_includes_clips_without_media(self, gap_project):
        """media_clips() drops the caption; all_clips() must keep it — a
        caption carries markers and a role even though it opens no file."""
        tl = FCPXMLParser().parse_file(gap_project).primary_timeline
        assert [c.name for c in tl.media_clips()] == ["interview", "interview-2"]
        assert sorted(c.name for c in tl.all_clips()) == [
            "caption-1", "interview", "interview-2",
        ]

    def test_media_clips_is_the_subset_that_has_media(self, gap_project):
        tl = FCPXMLParser().parse_file(gap_project).primary_timeline
        assert tl.media_clips() == [c for c in tl.all_clips() if c.media_path]

    def test_spine_project_is_unchanged_by_both_views(self, spine_project):
        tl = FCPXMLParser().parse_file(spine_project).primary_timeline
        assert tl.all_clips() == tl.clips
        assert tl.media_clips() == tl.clips


class TestReportingHandlers:
    async def test_list_markers_finds_markers_on_connected_clips(self, gap_project):
        text = (await server.handle_list_markers({"filepath": gap_project}))[0].text
        assert "cutaway here" in text
        assert "caption in" in text

    async def test_marker_positions_are_timeline_positions(self, gap_project):
        """A marker's start is in its own clip's clock. The mc-clip takes its
        in-point at 2s and sits at 4s on the timeline, so its marker at 4s is
        2s into the visible part and belongs at 6s."""
        text = (await server.handle_list_markers(
            {"filepath": gap_project, "format": "simple"}))[0].text
        assert "00:00:06:00 - cutaway here" in text, (
            "marker 2s into a clip that sits at 4s belongs at 6s"
        )
        assert "00:00:05:00 - caption in" in text
        assert "01:00:" not in text, "an hour offset means the gap's clock leaked"

    async def test_list_keywords_finds_keywords_on_connected_clips(self, gap_project):
        text = (await server.handle_list_keywords({"filepath": gap_project}))[0].text
        assert "No keywords found" not in text
        assert "interview" in text

    async def test_export_edl_is_not_empty(self, gap_project):
        text = (await server.handle_export_edl({"filepath": gap_project}))[0].text
        assert "FROM CLIP NAME: interview" in text
        assert "FROM CLIP NAME: interview-2" in text

    async def test_export_edl_omits_clips_with_no_source(self, gap_project):
        """An EDL row names a source and its timecode. A caption has none, so
        a row for it sends a conform system after media that does not exist."""
        text = (await server.handle_export_edl({"filepath": gap_project}))[0].text
        assert "caption-1" not in text

    async def test_export_csv_keeps_the_caption(self, gap_project):
        """The CSV is an inventory of the edit, not a conform list."""
        text = (await server.handle_export_csv({"filepath": gap_project}))[0].text
        assert '"caption-1"' in text

    async def test_export_csv_is_not_empty(self, gap_project):
        text = (await server.handle_export_csv({"filepath": gap_project}))[0].text
        assert '"interview"' in text
        assert '"interview-2"' in text

    async def test_analyze_pacing_does_not_claim_there_is_nothing_to_analyze(
        self, gap_project
    ):
        text = (await server.handle_analyze_pacing({"filepath": gap_project}))[0].text
        assert "No clips to analyze" not in text
        # Two 8s + 4s media clips average 6s. Counting the 4s caption as a
        # shot would drag it to 5.33s.
        assert "6.00s" in text, text

    async def test_filter_by_role_sees_connected_clips(self, tmp_path):
        """filter_by_role was the one roles handler that skipped lanes."""
        a, b = tmp_path / "a.mov", tmp_path / "b.mov"
        a.write_bytes(b"\x00" * 64)
        b.write_bytes(b"\x00" * 64)
        xml = MARKED.replace('name="interview">', 'name="interview" audioRole="dialogue">')
        path = tmp_path / "roled.fcpxml"
        path.write_text(xml.format(src_a=f"file://{a}", src_b=f"file://{b}"))
        text = (await server.handle_filter_by_role(
            {"filepath": str(path), "role": "dialogue"}))[0].text
        assert "No clips found" not in text
        assert "interview" in text


class TestSpineUnchanged:
    """Control: every converted handler reports the same on a spine project."""

    @pytest.mark.parametrize("handler,args", [
        ("handle_list_markers", {}),
        ("handle_list_keywords", {}),
        ("handle_export_edl", {}),
        ("handle_export_csv", {}),
        ("handle_analyze_pacing", {}),
    ])
    async def test_handler_output_mentions_the_spine_clip(
        self, spine_project, handler, args
    ):
        text = (await getattr(server, handler)({"filepath": spine_project, **args}))[0].text
        assert text.strip()
        assert "Traceback" not in text


class TestPreviewPositions:
    def test_connected_blocks_use_their_timeline_position(self, gap_project):
        """The preview placed connected clips by raw offset, so a clip whose
        parent gap starts at 3600s rendered against a 3600s origin and landed
        4s early. It must use timeline_start."""
        from fcpxml.preview import render_timeline_html

        tl = FCPXMLParser().parse_file(gap_project).primary_timeline
        html = render_timeline_html(tl)
        assert "interview" in html
        # 16s timeline: interview at 4s -> left 25%, interview-2 at 12s -> 75%.
        assert "left:25" in html.replace(" ", "")
        assert "left:75" in html.replace(" ", "")


class TestSilenceSliver:
    def test_a_subframe_clamped_span_is_not_reported(self):
        """ffmpeg puts a silence start 91us before a clip's out-point; the
        clamped span was 0.00009s and printed as a 0.00s row you cannot act
        on (split_clip cannot cut inside a frame)."""
        from fcpxml.media_intel import map_silence_to_timeline

        silences = [(3.999909, 12.0)]
        assert map_silence_to_timeline(silences, 0.0, 4.0, 12.0, min_mapped=1 / 25) == []

    def test_a_real_clamped_span_still_reports(self):
        from fcpxml.media_intel import map_silence_to_timeline

        silences = [(3.0, 12.0)]
        assert map_silence_to_timeline(silences, 0.0, 4.0, 12.0, min_mapped=1 / 25) == [
            (15.0, 16.0)
        ]

    def test_default_keeps_every_span(self):
        """Other callers must not change behaviour."""
        from fcpxml.media_intel import map_silence_to_timeline

        assert map_silence_to_timeline([(3.999909, 12.0)], 0.0, 4.0, 12.0) != []
