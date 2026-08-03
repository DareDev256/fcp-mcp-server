"""HTML timeline preview rendering."""
import xml.etree.ElementTree as ET

import pytest

from fcpxml.parser import FCPXMLParser
from fcpxml.preview import render_timeline_html


def _timeline():
    project = FCPXMLParser().parse_file("examples/sample.fcpxml")
    return project.primary_timeline


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
        import re

        tl = _timeline()
        if len(tl.clips) < 2:
            pytest.skip("sample timeline needs at least two clips")
        html = render_timeline_html(tl)

        widths = [float(w) for w in re.findall(r"width:([\d.]+)%", html)]
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

    def test_escapes_clip_names_that_contain_markup(self):
        """A clip called <script> must not become a script tag."""
        tl = _timeline()
        if tl.clips:
            tl.clips[0].name = '<script>alert("x")</script>'
        html = render_timeline_html(tl)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_reports_timeline_metadata(self):
        tl = _timeline()
        html = render_timeline_html(tl)
        assert str(tl.total_clips) in html
        assert f"{tl.width}" in html

    def test_handles_a_timeline_with_no_clips(self):
        tl = _timeline()
        tl.clips = []
        html = render_timeline_html(tl)
        assert "<!DOCTYPE html>" in html
