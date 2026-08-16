"""Edit operations on connected-clip timelines (issue #16).

A music video cut in Final Cut has nothing on the spine: one ``<gap>`` holds
the whole timeline and every visual hangs off it on a lane. Every test in this
repo used ``examples/sample.fcpxml``, which is spine-based, starts at 0s, and
has no connected clips — so the connected path had never once been executed,
and ``snap_to_beats`` reported "Your edits are now synced to the beat!" while
moving nothing at all on a 129-clip project.

These tests run against ``examples/music-video.fcpxml`` and inline fixtures
shaped the same way. Each one was verified to FAIL against the pre-fix
behaviour before being committed — see the notes on the individual tests for
what was sabotaged to confirm it.
"""

import asyncio
import json
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

import server
from fcpxml.models import TimeValue
from fcpxml.rational import format_seconds, parse_seconds
from fcpxml.writer import FCPXMLModifier

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MUSIC_VIDEO = EXAMPLES / "music-video.fcpxml"
SAMPLE = EXAMPLES / "sample.fcpxml"

# One beat every 2 seconds — the downbeat grid of a 120 BPM track, which is
# what an editor cuts a music video to. The fixture's lane 1 sits on it and
# lane 2's 6.5s cut deliberately does not.
BAR_GRID = [i * 2.0 for i in range(0, 82)]


def _run(coro):
    return asyncio.run(coro)


def _offsets(path, lane=None):
    """``{clip name: offset in exact seconds}`` for connected clips in *path*."""
    root = ET.parse(path).getroot()
    found = {}
    for element in root.iter():
        lane_attr = element.get('lane')
        if lane_attr is None or element.get('offset') is None:
            continue
        if lane is not None and int(lane_attr) != lane:
            continue
        found[element.get('name')] = parse_seconds(element.get('offset'))
    return found


def _marked_music_video(tmp_path, beats=None):
    """The music-video fixture with beat markers imported, ready to snap."""
    source = tmp_path / "mv.fcpxml"
    shutil.copy(MUSIC_VIDEO, source)
    beats_path = tmp_path / "beats.json"
    beats_path.write_text(json.dumps({
        "bpm": 120.0,
        "beats": BAR_GRID if beats is None else beats,
        "source": "grid",
    }))
    marked = tmp_path / "mv_marked.fcpxml"
    _run(server.handle_import_beat_markers({
        "filepath": str(source),
        "beats_path": str(beats_path),
        "output_path": str(marked),
    }))
    return marked


def _snap(path, output, **kwargs):
    result = _run(server.handle_snap_to_beats({
        "filepath": str(path), "output_path": str(output), **kwargs,
    }))
    return result[0].text


# ---------------------------------------------------------------------------
# The exact-time foundation the connected path is built on
# ---------------------------------------------------------------------------

class TestRationalTime:
    """Exact seconds survive a fractional frame rate on both paths."""

    def test_plain_seconds_survive_a_fractional_frame_rate(self):
        # This guard originally asserted that TimeValue got 3604s WRONG at
        # 23.976 (86410/23s, read back as 3756.96s), to prove parse_seconds
        # was not routing through it. #17 fixed TimeValue itself, so the two
        # now agree — and agreeing on the exact value is the stronger claim.
        ntsc = 24000 / 1001
        assert TimeValue.from_timecode("3604s", ntsc).to_seconds() == 3604
        assert parse_seconds("3604s") == Fraction(3604)

    def test_rational_attributes_stay_exact(self):
        assert parse_seconds("86496410/24000s") == Fraction(86496410, 24000)
        assert parse_seconds("3606500/1000s") == Fraction(3606500, 1000)
        assert parse_seconds(None) == 0
        assert parse_seconds("") == 0

    def test_a_zero_denominator_raises_rather_than_reading_as_zero(self):
        # Returning 0 would silently park a clip at the head of the timeline.
        with pytest.raises(ValueError):
            parse_seconds("100/0s")

    def test_written_offsets_land_on_a_frame_boundary(self):
        frame = Fraction(1001, 24000)
        written = format_seconds(Fraction(6), frame)
        assert parse_seconds(written) % frame == 0
        # Whole seconds collapse rather than being written as a ratio.
        assert format_seconds(Fraction(3), Fraction(1, 24)) == "3s"


# ---------------------------------------------------------------------------
# Geometry of a connected timeline
# ---------------------------------------------------------------------------

class TestConnectedGeometry:

    def test_every_connected_clip_is_found(self, tmp_path):
        modifier = FCPXMLModifier(str(MUSIC_VIDEO))
        found = modifier.iter_connected_clips()
        assert len(found) == 8
        assert sorted({lane for lane, _, _ in found}) == [-1, 1, 2]
        # Every one of them reports its host, without which it cannot be
        # placed on the timeline at all.
        assert all(host.tag == 'gap' for _, _, host in found)

    def test_origin_comes_from_the_elements_not_tcstart(self):
        # The fixture's tcStart reads 0s while every element starts at 3600s.
        root = ET.parse(MUSIC_VIDEO).getroot()
        assert root.find('.//sequence').get('tcStart') == '0s'
        assert FCPXMLModifier(str(MUSIC_VIDEO)).timeline_origin() == 3600

    def test_markers_read_back_in_timeline_seconds(self, tmp_path):
        # Markers land on the gap at 3600+t. If the origin were not removed
        # they would read as 3600, 3602, ... and no cut would ever match one.
        marked = _marked_music_video(tmp_path)
        times = FCPXMLModifier(str(marked)).timeline_marker_seconds()
        assert len(times) == 82
        assert float(times[0]) == pytest.approx(0.0, abs=0.05)
        assert float(times[1]) == pytest.approx(2.0, abs=0.05)


class TestHostRelativeOffsets:
    """A connected clip's offset is in its HOST's time frame, not the timeline's.

    ``examples/music-video.fcpxml`` writes ``<gap offset="3600s"
    start="3600s">``, where the two conventions coincide. A real Final Cut
    export writes ``<gap offset="0s" start="86400314/24000s">`` — measured on
    a 129-clip project — where reading the raw attribute puts every clip an
    hour past the end of its own 164-second timeline.
    """

    def _real_export_shape(self, tmp_path):
        path = tmp_path / "real.fcpxml"
        path.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s"
            width="1920" height="1080"/>
    <asset id="a1" name="A" start="0s" duration="60s" hasVideo="1">
      <media-rep kind="original-media" src="file:///A.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="r1" duration="20s" tcStart="0s" tcFormat="NDF">
      <spine>
        <gap name="Gap" offset="0s" start="3600s" duration="20s">
          <marker start="3602s" duration="1/24s" value="Beat"/>
          <asset-clip ref="a1" lane="1" name="ONE" offset="3600s"
                      start="0s" duration="2s"/>
          <asset-clip ref="a1" lane="1" name="TWO" offset="3602500/1000s"
                      start="0s" duration="2s"/>
        </gap>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
""")
        return path

    def test_positions_are_measured_through_the_host(self, tmp_path):
        modifier = FCPXMLModifier(str(self._real_export_shape(tmp_path)))
        lane = {
            element.get('name'):
                modifier.connected_timeline_offset(element, host)
            for _, element, host in modifier.iter_connected_clips()
        }
        assert lane["ONE"] == 0
        assert lane["TWO"] == Fraction(5, 2)
        assert modifier.timeline_origin() == 0
        assert modifier.timeline_marker_seconds() == [Fraction(2)]

    def test_a_snap_writes_back_into_the_host_frame(self, tmp_path):
        path = self._real_export_shape(tmp_path)
        modifier = FCPXMLModifier(str(path))
        report = modifier.snap_connected_clips(
            modifier.timeline_marker_seconds(), max_shift_frames=20,
        )
        assert [m['name'] for m in report['moved']] == ["TWO"]
        assert report['moved'][0]['to_seconds'] == pytest.approx(2.0)

        output = tmp_path / "out.fcpxml"
        modifier.save(str(output))
        # The attribute stays in the host's frame (3600 + 2.0), so Final Cut
        # reads the clip at 2s. Writing the timeline position raw would put
        # it an hour early and out of the gap entirely.
        assert _offsets(output)["TWO"] == 3602


class TestMarkersOnAGapSpine:
    """``import_beat_markers`` used to raise on every music video."""

    def test_beats_import_onto_a_spine_holding_only_a_gap(self, tmp_path):
        # Pre-fix this raised "No spine clip at position 0.000s" — the whole
        # beat workflow was unreachable, so snap_to_beats had nothing to
        # snap to even once it could see the lanes.
        marked = _marked_music_video(tmp_path)
        gap = ET.parse(marked).getroot().find('.//spine/gap')
        assert len(gap.findall('marker')) == 82

    def test_a_position_past_the_end_still_raises(self, tmp_path):
        source = tmp_path / "mv.fcpxml"
        shutil.copy(MUSIC_VIDEO, source)
        modifier = FCPXMLModifier(str(source))
        with pytest.raises(ValueError, match="No spine element at position"):
            modifier.add_marker_at_timeline(timecode="9000s", name="past end")


# ---------------------------------------------------------------------------
# snap_to_beats
# ---------------------------------------------------------------------------

class TestSnapToBeatsOnConnectedClips:

    def test_a_connected_cut_moves_onto_the_beat(self, tmp_path):
        # The headline of issue #16. Pre-fix: 0 clips seen, 0 moved, success
        # reported. Sabotage check: making snap_connected_clips return its
        # empty report without touching any element fails this.
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        text = _snap(marked, output, max_shift_frames=16)

        before = _offsets(marked, lane=2)
        after = _offsets(output, lane=2)
        assert float(before["OVERLAY 2"] - 3600) == pytest.approx(6.5, abs=0.02)
        assert float(after["OVERLAY 2"] - 3600) == pytest.approx(6.0, abs=0.03)
        assert "1 of 7 cuts moved" in text
        assert "OVERLAY 2" in text

    def test_moving_a_clip_does_not_ripple_its_lane(self, tmp_path):
        # Connected clips are not magnetic to each other. Everything except
        # the one clip that moved keeps its exact offset.
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        _snap(marked, output, max_shift_frames=16)

        before, after = _offsets(marked), _offsets(output)
        changed = {name for name in before if before[name] != after[name]}
        assert changed == {"OVERLAY 2"}

    def test_lanes_are_independent(self, tmp_path):
        # Lane 1 is already on the grid and stays put while lane 2 moves.
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        _snap(marked, output, max_shift_frames=16)
        assert _offsets(marked, lane=1) == _offsets(output, lane=1)

    def test_the_audio_bed_is_left_alone(self, tmp_path):
        # Sliding the track the beat grid was derived FROM would desync the
        # whole edit against the thing it is being synced to.
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        text = _snap(marked, output, max_shift_frames=16)
        assert _offsets(marked, lane=-1) == _offsets(output, lane=-1)
        assert "negative (audio) lanes" in text
        assert "7" in text  # 8 connected clips, 7 considered

    def test_audio_lanes_can_be_opted_into(self, tmp_path):
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        text = _snap(marked, output, max_shift_frames=16, include_audio_lanes=True)
        assert "8 connected" in text


class TestSnapReportsHonestly:
    """The silent no-op was the worst part of the bug."""

    def test_zero_moves_is_reported_as_zero_not_as_success(self, tmp_path):
        # Default max_shift of 6 frames cannot reach the 12-frame gap to the
        # nearest downbeat, so nothing moves — and the tool has to say so.
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        text = _snap(marked, output)

        assert "0 of 7 cuts moved" in text
        assert "Nothing was changed" in text
        assert "Your edits are now synced to the beat" not in text

    def test_a_cut_out_of_reach_is_named_with_its_distance(self, tmp_path):
        marked = _marked_music_video(tmp_path)
        output = tmp_path / "snapped.fcpxml"
        text = _snap(marked, output)
        assert "No Marker Within 6 Frames" in text
        assert "OVERLAY 2" in text
        assert "11.5f away" in text

    def test_every_considered_cut_lands_in_exactly_one_bucket(self, tmp_path):
        marked = _marked_music_video(tmp_path)
        modifier = FCPXMLModifier(str(marked))
        report = modifier.snap_connected_clips(
            modifier.timeline_marker_seconds(), max_shift_frames=16,
        )
        accounted = (
            len(report['moved']) + len(report['already_aligned'])
            + len(report['out_of_range']) + len(report['skipped'])
        )
        assert report['considered'] == 7
        assert accounted == report['considered']


# ---------------------------------------------------------------------------
# Collision handling — skip, never force
# ---------------------------------------------------------------------------

def _lane_fixture(tmp_path, clips, marker_seconds, name="lanes.fcpxml"):
    """A gap-spine timeline at 24fps holding *clips* on lane 1.

    *clips* is a list of ``(name, offset_seconds, duration_seconds)`` in
    timeline-relative seconds; the file itself puts them at 3600+ like a real
    Final Cut export.
    """
    def clip_xml(clip_name, offset, duration):
        return (
            f'<asset-clip ref="a1" lane="1" name="{clip_name}" '
            f'offset="{3600 + offset}s" start="0s" duration="{duration}s"/>'
        )

    markers = "".join(
        f'<marker start="{3600 + at}s" duration="1/24s" value="Beat"/>'
        for at in marker_seconds
    )
    body = "".join(clip_xml(*c) for c in clips)
    path = tmp_path / name
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s"
            width="1920" height="1080"/>
    <asset id="a1" name="A" start="0s" duration="60s" hasVideo="1">
      <media-rep kind="original-media" src="file:///A.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="r1" duration="20s" tcStart="0s" tcFormat="NDF">
      <spine>
        <gap name="Gap" offset="3600s" start="3600s" duration="20s">
          {markers}{body}
        </gap>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
""")
    return path


class TestCollisionsAreSkipped:

    def test_a_move_into_the_previous_clip_is_skipped(self, tmp_path):
        # PREV occupies [0, 2.5). Snapping LATER from 2.75 back to the beat
        # at 2.0 would bury 0.5s of PREV under it.
        path = _lane_fixture(
            tmp_path,
            clips=[("PREV", 0, 2.5), ("LATER", 2.75, 2)],
            marker_seconds=[2.0],
        )
        modifier = FCPXMLModifier(str(path))
        before = _offsets(path)
        report = modifier.snap_connected_clips(
            modifier.timeline_marker_seconds(), max_shift_frames=20,
        )

        assert report['moved'] == []
        assert len(report['skipped']) == 1
        skipped = report['skipped'][0]
        assert skipped['name'] == "LATER"
        assert "overlap PREV" in skipped['reason']
        # Skipped means untouched, not "moved as far as it fits".
        modifier.save(str(tmp_path / "out.fcpxml"))
        assert _offsets(tmp_path / "out.fcpxml") == before

    def test_a_move_into_the_next_clip_is_skipped(self, tmp_path):
        # FIRST would slide from 0.5 to 1.0, pushing its tail from 2.5 to 3.0
        # and over SECOND, which starts at 2.75.
        path = _lane_fixture(
            tmp_path,
            clips=[("FIRST", 0.5, 2), ("SECOND", 2.75, 2)],
            marker_seconds=[1.0],
        )
        modifier = FCPXMLModifier(str(path))
        report = modifier.snap_connected_clips(
            modifier.timeline_marker_seconds(), max_shift_frames=20,
        )

        assert report['moved'] == []
        assert len(report['skipped']) == 1
        assert "overlap SECOND" in report['skipped'][0]['reason']

    def test_butting_up_against_a_neighbour_is_allowed(self, tmp_path):
        # Landing exactly on the next clip's first frame is a clean cut, not
        # a collision — an off-by-one in the comparison would reject it.
        path = _lane_fixture(
            tmp_path,
            clips=[("FIRST", 0.5, 2), ("SECOND", 3.0, 2)],
            marker_seconds=[1.0],
        )
        modifier = FCPXMLModifier(str(path))
        report = modifier.snap_connected_clips(
            modifier.timeline_marker_seconds(), max_shift_frames=20,
        )
        assert [m['name'] for m in report['moved']] == ["FIRST"]
        assert report['skipped'] == []

    def test_prefer_earlier_only_looks_backwards(self, tmp_path):
        path = _lane_fixture(
            tmp_path,
            clips=[("ONLY", 2.0, 2)],
            marker_seconds=[1.5, 2.2],
        )
        modifier = FCPXMLModifier(str(path))
        report = modifier.snap_connected_clips(
            modifier.timeline_marker_seconds(),
            max_shift_frames=20, prefer="earlier",
        )
        assert [m['name'] for m in report['moved']] == ["ONLY"]
        assert report['moved'][0]['to_seconds'] == pytest.approx(1.5, abs=0.03)


# ---------------------------------------------------------------------------
# The spine path must be exactly as it was
# ---------------------------------------------------------------------------

def _spine_fixture(tmp_path, marker_at):
    """A conventional spine timeline with a sequence marker near a cut."""
    path = tmp_path / "spine.fcpxml"
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s"
            width="1920" height="1080"/>
    <asset id="a1" name="A" start="0s" duration="60s" hasVideo="1">
      <media-rep kind="original-media" src="file:///A.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="r1" duration="6s" tcStart="0s" tcFormat="NDF">
      <spine>
        <asset-clip ref="a1" name="ONE" offset="0s" start="0s" duration="2s"/>
        <asset-clip ref="a1" name="TWO" offset="2s" start="10s" duration="2s"/>
        <asset-clip ref="a1" name="THREE" offset="4s" start="20s" duration="2s"/>
      </spine>
      <chapter-marker start="{marker_at}" duration="1/24s" value="Beat"/>
    </sequence>
  </project></event></library>
</fcpxml>
""")
    return path


class TestSpinePathUnchanged:

    def test_sample_fcpxml_reports_its_spine_cuts(self, tmp_path):
        source = tmp_path / "sample.fcpxml"
        shutil.copy(SAMPLE, source)
        output = tmp_path / "sample_synced.fcpxml"
        text = _snap(source, output)

        # Eight cuts, none within 6 frames of a chapter marker.
        assert "**Cuts Considered**: 8" in text
        assert "0 of 8 cuts moved" in text
        # No connected clips, so none of the lane reporting appears.
        assert "connected across lanes" not in text
        assert "negative (audio) lanes" not in text

    def test_sample_spine_offsets_are_untouched(self, tmp_path):
        source = tmp_path / "sample.fcpxml"
        shutil.copy(SAMPLE, source)
        output = tmp_path / "sample_synced.fcpxml"
        _snap(source, output)

        def spine_offsets(path):
            return [parse_seconds(c.get('offset'))
                    for c in ET.parse(path).getroot().find('.//spine')]

        assert spine_offsets(output) == spine_offsets(source)

    def test_a_spine_cut_still_snaps_and_the_previous_clip_absorbs_it(self, tmp_path):
        # The spine is magnetic, so its snap trims the preceding clip rather
        # than sliding — the opposite of the connected rule. Guards against
        # the connected path having quietly replaced this one.
        path = _spine_fixture(tmp_path, marker_at="51/24s")  # 2.125s
        output = tmp_path / "out.fcpxml"
        text = _snap(path, output)

        spine = ET.parse(output).getroot().find('.//spine')
        clips = {c.get('name'): c for c in spine}
        assert parse_seconds(clips["TWO"].get('offset')) == Fraction(51, 24)
        assert parse_seconds(clips["ONE"].get('duration')) == Fraction(51, 24)
        assert parse_seconds(clips["THREE"].get('offset')) == Fraction(4)
        assert "1 of 2 cuts moved" in text


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

class TestDetectorsSeeLanes:

    def test_a_flash_frame_on_a_lane_is_detected(self, tmp_path):
        # Pre-fix _detect_flash_frames walked tl.clips only, so a 1-frame
        # connected clip on a music video was invisible.
        path = _lane_fixture(
            tmp_path,
            clips=[("KEEPER", 0, 4), ("FLASH", 5, 1 / 24)],
            marker_seconds=[],
        )
        text = _run(server.handle_detect_flash_frames({"filepath": str(path)}))[0].text
        assert "FLASH" in text
        assert "KEEPER" not in text

    def test_a_lane_flash_frame_reports_its_real_position(self, tmp_path):
        # Offsets start at 3600s; without removing the origin this would be
        # reported an hour into the timeline.
        path = _lane_fixture(
            tmp_path,
            clips=[("HEAD", 0, 4), ("FLASH", 5, 1 / 24)],
            marker_seconds=[],
        )
        text = _run(server.handle_detect_flash_frames({"filepath": str(path)}))[0].text
        assert "00:00:05:00" in text
        assert "01:00:05" not in text

    def test_spine_flash_frames_are_unaffected(self, tmp_path):
        # sample.fcpxml's shortest clip is exactly 6 frames, so it only
        # surfaces above that threshold. Position and label must be what
        # they always were.
        text = _run(server.handle_detect_flash_frames({
            "filepath": str(SAMPLE), "warning_threshold_frames": 7,
        }))[0].text
        assert "Broll_Studio" in text
        assert "00:00:09:00" in text

    def test_gap_detection_states_that_it_skipped_the_lanes(self, tmp_path):
        # Silence here reads as a clean bill of health for a timeline the
        # check never looked at.
        text = _run(server.handle_detect_gaps({"filepath": str(MUSIC_VIDEO)}))[0].text
        assert "primary storyline" in text
        assert "8 connected clip(s) across 3 lane(s) were not checked" in text
