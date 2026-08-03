"""HTML timeline preview rendering."""
import asyncio
import re
import xml.etree.ElementTree as ET
from html import escape as _escape
from pathlib import Path
from urllib.parse import quote

import pytest
from mcp.types import ReadResourceRequest, ReadResourceRequestParams

import server as server_module
from fcpxml.models import ConnectedClip, Timecode
from fcpxml.parser import FCPXMLParser
from fcpxml.preview import render_timeline_html


def _timeline():
    project = FCPXMLParser().parse_file("examples/sample.fcpxml")
    return project.primary_timeline


def _timeline_with_connected_clips():
    """The reviewer's fixture shape: B-roll (lane 1), a title (lane 2), a music bed (lane -1)."""
    tl = _timeline()
    tl.connected_clips = [
        ConnectedClip(
            name="B-roll Drone Shot",
            start=Timecode(0, 24),
            duration=Timecode(48, 24),
            lane=1,
            offset=Timecode(24, 24),
        ),
        ConnectedClip(
            name="Title Card",
            start=Timecode(0, 24),
            duration=Timecode(24, 24),
            lane=2,
            offset=Timecode(0, 24),
        ),
        ConnectedClip(
            name="Music Bed",
            start=Timecode(0, 24),
            duration=Timecode(240, 24),
            lane=-1,
            offset=Timecode(0, 24),
        ),
    ]
    return tl


class TestRenderTimelineHTML:
    def test_returns_a_parseable_html_document(self):
        html = render_timeline_html(_timeline())
        assert html.lstrip().startswith("<!DOCTYPE html>")
        # Must be well formed, not just string-concatenated soup.
        ET.fromstring(html[html.index("<html"):])

    def test_every_clip_appears(self):
        tl = _timeline()
        html = render_timeline_html(tl)
        for clip in tl.clips:
            assert clip.name in html

    def test_clip_widths_are_proportional_to_duration(self):
        """A clip twice as long must render twice as wide.

        Asserting that the string 'width:' appears would pass even if every
        clip rendered identically, which is the bug this guards against.
        """
        tl = _timeline()
        if len(tl.clips) < 2:
            pytest.skip("sample timeline needs at least two clips")
        html = render_timeline_html(tl)

        widths = [
            float(w) for w in
            re.findall(r'<div class="clip" style="left:[\d.]+%;width:([\d.]+)%', html)
        ]
        assert len(widths) == len(tl.clips), "one width per clip"

        total = float(tl.duration.seconds)
        for width, clip in zip(widths, tl.clips):
            expected = max((float(clip.duration.seconds) / total) * 100, 0.4)
            assert abs(width - expected) < 0.01, (
                f"{clip.name}: rendered {width}%, expected {expected}%"
            )

        # And the relationship holds between clips, not just against the formula.
        longest = max(range(len(tl.clips)), key=lambda i: tl.clips[i].duration.seconds)
        shortest = min(range(len(tl.clips)), key=lambda i: tl.clips[i].duration.seconds)
        if tl.clips[longest].duration.seconds > tl.clips[shortest].duration.seconds:
            assert widths[longest] > widths[shortest]

    def test_clip_left_offsets_are_proportional_to_start(self):
        """A clip starting partway through the timeline must render at the matching left%.

        Guards against a renderer that hardcodes left to 0 for every clip
        (they would all stack at the origin, which passed every other
        assertion in the earlier test suite).
        """
        tl = _timeline()
        if len(tl.clips) < 2:
            pytest.skip("sample timeline needs at least two clips")
        html = render_timeline_html(tl)

        lefts = [
            float(w) for w in
            re.findall(r'<div class="clip" style="left:([\d.]+)%', html)
        ]
        assert len(lefts) == len(tl.clips), "one left offset per clip"

        total = float(tl.duration.seconds)
        for left, clip in zip(lefts, tl.clips):
            expected = max(min((float(clip.start.seconds) / total) * 100, 100.0), 0.0)
            assert abs(left - expected) < 0.01, (
                f"{clip.name}: rendered left:{left}%, expected {expected}%"
            )

        # The spine is contiguous and clips have distinct durations in the
        # fixture, so lefts must differ — a hardcoded 0.0 would fail this.
        assert len(set(lefts)) > 1, "clips must not all stack at the origin"

    def test_escapes_clip_names_that_contain_markup(self):
        """A clip called <script> must not become a script tag."""
        tl = _timeline()
        if tl.clips:
            tl.clips[0].name = '<script>alert("x")</script>'
        html = render_timeline_html(tl)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_escapes_timeline_name(self):
        """The timeline name lands in <title> and <h1> — must be escaped there too."""
        tl = _timeline()
        tl.name = '<script>alert("timeline")</script>'
        html = render_timeline_html(tl)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_escapes_marker_names(self):
        """A marker named with markup must not inject into the document."""
        tl = _timeline()
        if not tl.markers:
            pytest.skip("sample timeline needs at least one marker")
        tl.markers[0].name = '<script>alert("marker")</script>'
        html = render_timeline_html(tl)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_renders_a_tick_per_marker(self):
        """Every timeline marker must produce a visible tick, not silently vanish."""
        tl = _timeline()
        assert tl.markers, "fixture is expected to carry markers"
        html = render_timeline_html(tl)

        marker_titles = re.findall(r'<div class="marker"[^>]*title="([^"]*)"', html)
        assert len(marker_titles) == len(tl.markers)
        for marker in tl.markers:
            assert _escape(str(marker.name)) in html

    def test_reports_timeline_metadata(self):
        tl = _timeline()
        html = render_timeline_html(tl)
        match = re.search(r"(\d+) clips", html)
        assert match is not None, "clip count must be reported in the meta line"
        assert match.group(1) == str(tl.total_clips)
        assert f"{tl.width}" in html

    def test_handles_a_timeline_with_no_clips(self):
        tl = _timeline()
        tl.clips = []
        html = render_timeline_html(tl)
        assert "<!DOCTYPE html>" in html

    def test_connected_clips_render_on_lane_rows(self):
        """B-roll, titles, and connected audio must not vanish from the preview.

        examples/sample.fcpxml has zero connected clips, so a renderer that
        only drew timeline.clips passed every test while silently dropping
        every overlay and connected audio track.
        """
        tl = _timeline_with_connected_clips()
        html = render_timeline_html(tl)

        for cc in tl.connected_clips:
            assert cc.name in html

        lane_clip_divs = re.findall(r'<div class="lane-clip"', html)
        assert len(lane_clip_divs) == len(tl.connected_clips) == 3

    def test_connected_clip_lanes_are_positioned_above_and_below_spine(self):
        tl = _timeline_with_connected_clips()
        html = render_timeline_html(tl)

        # Positive lanes (1, 2) render above the spine in document order,
        # highest lane topmost; negative lane (-1) renders below.
        title_pos = html.index("Title Card")   # lane 2
        broll_pos = html.index("B-roll Drone Shot")  # lane 1
        spine_track_pos = html.index('<div class="track">')
        music_pos = html.index("Music Bed")   # lane -1

        assert title_pos < broll_pos < spine_track_pos, "higher lane (2) renders above lane 1, above spine"
        assert spine_track_pos < music_pos, "negative lane renders below the spine"

    def test_degenerate_zero_duration_timeline_clamps_width(self):
        """A zero-duration timeline must not blow up clip width into four digits."""
        tl = _timeline()
        tl.duration = Timecode(0, 24)
        html = render_timeline_html(tl)
        widths = [float(w) for w in re.findall(r"width:([\d.]+)%", html)]
        assert widths, "expected at least one rendered block"
        assert all(w <= 100.0 for w in widths)

    def test_clip_longer_than_timeline_clamps_to_remaining_space(self):
        tl = _timeline()
        if not tl.clips:
            pytest.skip("sample timeline needs at least one clip")
        tl.clips[0].duration = Timecode(int(tl.duration.frames) * 10, tl.duration.frame_rate)
        html = render_timeline_html(tl)
        widths = [
            float(w) for w in
            re.findall(r'<div class="clip" style="left:[\d.]+%;width:([\d.]+)%', html)
        ]
        assert all(w <= 100.0 for w in widths)

    def test_negative_start_clamps_left_to_zero(self):
        tl = _timeline()
        if not tl.clips:
            pytest.skip("sample timeline needs at least one clip")
        tl.clips[0].start = Timecode(-int(tl.duration.frame_rate) * 5, tl.duration.frame_rate)
        html = render_timeline_html(tl)
        lefts = [float(w) for w in re.findall(r'<div class="clip" style="left:([\d.]+)%', html)]
        assert lefts[0] >= 0.0


def _read_resource(uri: str):
    """Drive the real ReadResourceRequest handler for `uri`.

    `method=` is passed explicitly on purpose. mcp only gained a default for it
    after 1.3.0, so omitting it makes these tests fail with a pydantic
    ValidationError on the oldest supported SDK while the product itself works
    fine — exactly the kind of blind spot that let the dependency floor drift.
    """
    handler = server_module.server.request_handlers[ReadResourceRequest]
    request = ReadResourceRequest(
        method="resources/read",
        params=ReadResourceRequestParams(uri=uri),
    )
    return asyncio.run(handler(request)).root.contents


class TestPreviewResourceServesHTML:
    """Drives the real ReadResourceRequest handler, not just render_timeline_html directly.

    A resource that advertises mimeType="text/html" in list_resources but
    returns a bare str from read_resource gets stamped text/plain by mcp's
    lowlevel wrapper (mcp/server/lowlevel/server.py) — a client would render
    the HTML source as a wall of text instead of a document.
    """

    def test_preview_uri_is_served_with_html_mime_type(self):
        contents = _read_resource("preview://examples/sample.fcpxml")
        assert len(contents) == 1
        assert contents[0].mimeType == "text/html"
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")

    def test_file_uri_is_unaffected(self):
        """The pre-existing file:// branch keeps its original text/plain behaviour."""
        contents = _read_resource("file://examples/sample.fcpxml")
        assert len(contents) == 1
        assert "FCPXML Project" in contents[0].text


class TestResourceUriWithSpacesInFilename:
    """Filenames with spaces are the norm in ~/Movies.

    `list_resources` hands the client a pydantic-normalized URI, so
    `My Project.fcpxml` comes back percent-encoded. If read_resource does not
    unquote, every one of those reads fails with "File not found" — which
    would make the flagship preview:// feature look broken for a large share
    of real libraries.
    """

    @staticmethod
    def _spaced_copy(tmp_path):
        src = Path("examples/sample.fcpxml").read_bytes()
        target = tmp_path / "My Project Final.fcpxml"
        target.write_bytes(src)
        return target

    def test_preview_uri_percent_encoded_space_resolves(self, tmp_path):
        target = self._spaced_copy(tmp_path)
        uri = "preview://" + quote(str(target))
        contents = _read_resource(uri)
        assert contents[0].mimeType == "text/html"
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")

    def test_file_uri_percent_encoded_space_resolves(self, tmp_path):
        target = self._spaced_copy(tmp_path)
        uri = "file://" + quote(str(target))
        contents = _read_resource(uri)
        assert "FCPXML Project" in contents[0].text
        assert "File not found" not in contents[0].text

    def test_list_resources_uri_round_trips_through_read(self, tmp_path, monkeypatch):
        """End to end: whatever list_resources advertises must be readable."""
        self._spaced_copy(tmp_path)
        monkeypatch.setattr(server_module, "PROJECTS_DIR", str(tmp_path))
        resources = asyncio.run(server_module.list_resources())
        previews = [r for r in resources if str(r.uri).startswith("preview://")]
        assert previews, "expected a preview:// resource for the spaced file"
        contents = _read_resource(str(previews[0].uri))
        assert contents[0].text.lstrip().startswith("<!DOCTYPE html>")
