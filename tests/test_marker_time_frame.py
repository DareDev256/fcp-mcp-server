"""A marker's ``start`` is in its host clip's LOCAL time, which begins at the
host's ``start`` (the source in-point), not at 0.

Every writer path except ``auto_at_cuts`` placed markers relative to the
clip's ``offset`` and ignored ``start``, so a marker written "at 12s" onto a
clip trimmed to begin 2s into its source landed at 10s in Final Cut Pro.
The parser exposed the raw value, so every reader that printed or compared
it (list_markers, snap_to_beats, diff, preview) inherited the same error
whenever ``start`` was non-zero. The round trip through this server was
self-consistent — which is exactly why nothing noticed.
"""

from fractions import Fraction

from fcpxml.parser import parse_fcpxml
from fcpxml.writer import FCPXMLModifier

# A 10s gap, then one 25p clip on the spine at timeline 10s, trimmed to begin 2s into its
# source, 8s long. Timeline 12s is therefore source 4s.
XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s" width="1920" height="1080"/>
    <asset id="r2" name="take" start="0s" duration="60s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/take.mov"/>
    </asset>
  </resources>
  <library>
    <event name="E">
      <project name="Trimmed">
        <sequence format="r1" duration="18s" tcStart="0s">
          <spine>
            <gap name="Gap" offset="0s" start="0s" duration="10s"/>
            <asset-clip ref="r2" name="take" offset="10s" start="2s" duration="8s">{markers}</asset-clip>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


def _write(tmp_path, markers=""):
    p = tmp_path / "trimmed.fcpxml"
    p.write_text(XML.format(markers=markers))
    return p


def test_marker_at_timeline_lands_in_source_time(tmp_path):
    m = FCPXMLModifier(str(_write(tmp_path)))
    elem = m.add_marker_at_timeline("12s", "here")
    assert Fraction(elem.get("start").rstrip("s")) == 4  # not 2
    # And the writer's own timeline read-back agrees with the request.
    assert m.timeline_marker_seconds() == [Fraction(12)]


def test_parser_exposes_timeline_position_for_clip_markers(tmp_path):
    p = _write(tmp_path, '<marker start="4s" duration="1/25s" value="here"/>')
    tl = parse_fcpxml(str(p)).timelines[0]
    marker = tl.clips[0].markers[0]
    assert marker.start.seconds == 4.0          # raw, as written
    assert marker.position.seconds == 12.0      # where it is on the timeline


def test_round_trip_write_then_list_agrees_on_timeline_time(tmp_path):
    src = _write(tmp_path)
    m = FCPXMLModifier(str(src))
    m.add_marker_at_timeline("12s", "here")
    out = tmp_path / "out.fcpxml"
    m.save(str(out))
    tl = parse_fcpxml(str(out)).timelines[0]
    assert [mk.position.seconds for mk in tl.clips[0].markers] == [12.0]


def test_interval_markers_land_in_source_time(tmp_path):
    m = FCPXMLModifier(str(_write(tmp_path)))
    created = m.batch_add_markers(markers=[], auto_at_intervals="4s")
    # Intervals at 4s and 8s fall before the clip; 12s and 16s are inside it.
    starts = sorted(Fraction(e.get("start").rstrip("s")) for e in created)
    assert starts == [Fraction(4), Fraction(8)]


def test_the_old_convention_reads_wrong_now(tmp_path):
    """Mutation check: a marker written the old way (offset-relative) is
    reported 2s early — the instrument sees the failure it was built for."""
    p = _write(tmp_path, '<marker start="2s" duration="1/25s" value="old"/>')
    tl = parse_fcpxml(str(p)).timelines[0]
    assert tl.clips[0].markers[0].position.seconds == 10.0
