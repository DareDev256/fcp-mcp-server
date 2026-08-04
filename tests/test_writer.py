"""Tests for FCPXML writer/modifier."""

import os
import shutil
import tempfile
import textwrap
from pathlib import Path

import pytest

from fcpxml.parser import FCPXMLParser
from fcpxml.writer import FCPXMLModifier

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


@pytest.fixture
def temp_fcpxml():
    """Create a temp copy of sample.fcpxml for modification tests."""
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', delete=False) as f:
        shutil.copy(SAMPLE, f.name)
        yield f.name
    Path(f.name).unlink(missing_ok=True)


# ============================================================
# Insert Clip Tests
# ============================================================

def test_insert_clip_at_end(temp_fcpxml):
    """Insert a library clip at the end of the timeline."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Get initial clip count
    initial_count = len(list(modifier._get_spine()))

    # Insert Broll_City (r3) at the end
    result = modifier.insert_clip(
        asset_id='r3',
        position='end',
        duration='2s'
    )

    assert result is not None

    # Verify clip was added
    final_count = len(list(modifier._get_spine()))
    assert final_count == initial_count + 1


def test_insert_clip_at_start(temp_fcpxml):
    """Insert a library clip at the start of the timeline."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Insert clip at start
    modifier.insert_clip(
        asset_id='r4',  # Broll_Studio
        position='start',
        duration='1s'
    )

    # Verify clip is at offset 0
    spine = modifier._get_spine()
    first_clip = list(spine)[0]
    assert first_clip.get('offset') == '0/24s' or first_clip.get('offset') == '0s'


def test_insert_clip_at_timecode(temp_fcpxml):
    """Insert a library clip at a specific timecode."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Insert at 00:00:05:00 (5 seconds)
    result = modifier.insert_clip(
        asset_id='r3',
        position='00:00:05:00',
        duration='1s'
    )

    # Verify clip was inserted
    assert result is not None


def test_insert_clip_with_subclip(temp_fcpxml):
    """Insert a portion of a library clip (subclip)."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Insert with in/out points
    result = modifier.insert_clip(
        asset_id='r2',  # Interview_A (300s long)
        position='end',
        in_point='00:00:10:00',  # Start at 10s in source
        out_point='00:00:15:00'  # End at 15s in source (5s clip)
    )

    # Verify the start attribute matches in_point
    start_attr = result.get('start')
    assert start_attr is not None
    # Should be 10 seconds worth of frames (240/24s at 24fps)
    assert '240' in start_attr or '10s' in start_attr


def test_insert_clip_ripples_subsequent(temp_fcpxml):
    """Inserting a clip should shift subsequent clips forward."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Get second clip's original offset
    spine = modifier._get_spine()
    clips = [c for c in spine if c.tag in ('asset-clip', 'clip')]
    original_second_clip_offset = clips[1].get('offset')

    # Insert 2 seconds at start
    modifier.insert_clip(
        asset_id='r4',
        position='start',
        duration='2s',
        ripple=True
    )

    # Verify second clip shifted
    spine = modifier._get_spine()
    clips = [c for c in spine if c.tag in ('asset-clip', 'clip')]
    # Original second clip is now at index 2 (after new clip at 0)
    new_second_clip = clips[2]  # Was clips[1], now clips[2]

    # Original offset was 72/24s = 3s, now should be 120/24s = 5s (+2s)
    new_offset = new_second_clip.get('offset')
    assert new_offset != original_second_clip_offset


def test_insert_clip_by_name(temp_fcpxml):
    """Can insert a clip by asset name instead of ID."""
    modifier = FCPXMLModifier(temp_fcpxml)

    result = modifier.insert_clip(
        asset_name='Broll_City',  # Instead of asset_id='r3'
        position='end',
        duration='1s'
    )

    assert result is not None
    assert result.get('ref') == 'r3'


def test_insert_clip_after_specific_clip(temp_fcpxml):
    """Insert clip after a specific clip."""
    modifier = FCPXMLModifier(temp_fcpxml)

    result = modifier.insert_clip(
        asset_id='r3',
        position='after:Interview_A',  # After first Interview_A
        duration='1s'
    )

    assert result is not None


def test_insert_clip_saves_correctly(temp_fcpxml):
    """Inserted clip should persist after save and reload."""
    modifier = FCPXMLModifier(temp_fcpxml)
    initial_count = len(list(modifier._get_spine()))

    modifier.insert_clip(
        asset_id='r3',
        position='end',
        duration='2s'
    )

    # Save to temp file
    output = temp_fcpxml.replace('.fcpxml', '_modified.fcpxml')
    modifier.save(output)

    # Reload and verify
    parser = FCPXMLParser()
    project = parser.parse_file(output)
    reloaded_clip_count = len(project.primary_timeline.clips)

    # Clean up
    Path(output).unlink(missing_ok=True)

    assert reloaded_clip_count == initial_count + 1


# ============================================================
# Add Marker Tests
# ============================================================

def test_add_marker_standard(temp_fcpxml):
    """Add a standard marker to a clip."""
    modifier = FCPXMLModifier(temp_fcpxml)

    marker = modifier.add_marker(
        clip_id='Broll_City',
        timecode='00:00:01:00',
        name='Review this'
    )

    assert marker is not None
    assert marker.tag == 'marker'
    assert marker.get('value') == 'Review this'
    assert marker.get('start') is not None


def test_add_marker_chapter(temp_fcpxml):
    """Add a chapter marker to a clip."""
    from fcpxml.models import MarkerType

    modifier = FCPXMLModifier(temp_fcpxml)

    marker = modifier.add_marker(
        clip_id='Broll_City',
        timecode='00:00:00:12',
        name='Chapter Start',
        marker_type=MarkerType.CHAPTER
    )

    assert marker.tag == 'chapter-marker'
    assert marker.get('value') == 'Chapter Start'
    assert marker.get('posterOffset') == '0s'


def test_add_marker_incomplete(temp_fcpxml):
    """Add an incomplete marker to a clip — must set completed='0' for round-trip fidelity."""
    from fcpxml.models import MarkerType

    modifier = FCPXMLModifier(temp_fcpxml)

    marker = modifier.add_marker(
        clip_id='Broll_City',
        timecode='00:00:00:06',
        name='Fix color',
        marker_type=MarkerType.INCOMPLETE
    )

    assert marker.tag == 'marker'
    assert marker.get('value') == 'Fix color'
    assert marker.get('completed') == '0'


def test_add_marker_completed(temp_fcpxml):
    """Add a completed marker to a clip."""
    from fcpxml.models import MarkerType

    modifier = FCPXMLModifier(temp_fcpxml)

    marker = modifier.add_marker(
        clip_id='Broll_City',
        timecode='00:00:00:06',
        name='Done item',
        marker_type=MarkerType.COMPLETED
    )

    assert marker.get('completed') == '1'


def test_marker_type_roundtrip(temp_fcpxml, tmp_path):
    """INCOMPLETE and COMPLETED markers survive save/re-parse without losing their type."""
    from fcpxml.models import MarkerType
    from fcpxml.parser import FCPXMLParser

    modifier = FCPXMLModifier(temp_fcpxml)
    modifier.add_marker('Broll_City', '00:00:00:06', 'Todo task', MarkerType.INCOMPLETE)
    modifier.add_marker('Broll_City', '00:00:00:12', 'Done task', MarkerType.COMPLETED)

    output = str(tmp_path / 'roundtrip.fcpxml')
    modifier.save(output)

    project = FCPXMLParser().parse_file(output)
    # Collect all markers across all clips (duplicate names mean markers land on last Broll_City)
    all_markers = []
    for clip in project.primary_timeline.clips:
        all_markers.extend(clip.markers)
    types = {m.name: m.marker_type for m in all_markers}
    assert types['Todo task'] == MarkerType.INCOMPLETE
    assert types['Done task'] == MarkerType.COMPLETED


def test_marker_type_from_string_roundtrip(temp_fcpxml, tmp_path):
    """MarkerType.from_string('todo') → write → parse must survive as INCOMPLETE, not STANDARD."""
    from fcpxml.models import MarkerType
    from fcpxml.parser import FCPXMLParser

    modifier = FCPXMLModifier(temp_fcpxml)
    # Use from_string (the path batch_add_markers takes) instead of enum directly
    modifier.add_marker('Broll_City', '00:00:00:06', 'Via string',
                        MarkerType.from_string('todo'))
    modifier.add_marker('Broll_City', '00:00:00:12', 'Via alias',
                        MarkerType.from_string('todo-marker'))

    output = str(tmp_path / 'from_string_rt.fcpxml')
    modifier.save(output)

    project = FCPXMLParser().parse_file(output)
    all_markers = []
    for clip in project.primary_timeline.clips:
        all_markers.extend(clip.markers)
    types = {m.name: m.marker_type for m in all_markers}
    assert types['Via string'] == MarkerType.INCOMPLETE
    assert types['Via alias'] == MarkerType.INCOMPLETE


def test_marker_completed_attr_no_whitespace(temp_fcpxml):
    """Written completed attributes must be exact '0' or '1' — no whitespace padding."""
    from fcpxml.models import MarkerType

    modifier = FCPXMLModifier(temp_fcpxml)
    todo = modifier.add_marker('Broll_City', '00:00:00:06', 'Strict0', MarkerType.INCOMPLETE)
    done = modifier.add_marker('Broll_City', '00:00:00:12', 'Strict1', MarkerType.COMPLETED)

    assert todo.get('completed') == '0', "INCOMPLETE marker must write exact '0'"
    assert done.get('completed') == '1', "COMPLETED marker must write exact '1'"
    # Verify no leading/trailing whitespace
    assert todo.get('completed').strip() == todo.get('completed')
    assert done.get('completed').strip() == done.get('completed')


def test_from_string_whitespace_roundtrip(temp_fcpxml, tmp_path):
    """from_string('  completed  ') must roundtrip as COMPLETED, not STANDARD."""
    from fcpxml.models import MarkerType
    from fcpxml.parser import FCPXMLParser

    modifier = FCPXMLModifier(temp_fcpxml)
    modifier.add_marker('Broll_City', '00:00:00:06', 'Padded type',
                        MarkerType.from_string('  completed  '))

    output = str(tmp_path / 'padded_type_rt.fcpxml')
    modifier.save(output)

    project = FCPXMLParser().parse_file(output)
    all_markers = []
    for clip in project.primary_timeline.clips:
        all_markers.extend(clip.markers)
    types = {m.name: m.marker_type for m in all_markers}
    assert types['Padded type'] == MarkerType.COMPLETED


def test_add_marker_with_note(temp_fcpxml):
    """Add a marker with a note."""
    modifier = FCPXMLModifier(temp_fcpxml)

    marker = modifier.add_marker(
        clip_id='Broll_City',
        timecode='00:00:00:06',
        name='Note marker',
        note='Needs color correction'
    )

    assert marker.get('note') == 'Needs color correction'


def test_add_marker_invalid_clip(temp_fcpxml):
    """Adding a marker to a nonexistent clip raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="Clip not found"):
        modifier.add_marker(
            clip_id='nonexistent_clip',
            timecode='00:00:01:00',
            name='Should fail'
        )


# ============================================================
# Trim Clip Tests
# ============================================================

def test_trim_clip_end_delta(temp_fcpxml):
    """Trim the end of a clip by a delta."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Broll_Studio at offset 222/24s has duration 120/24s = 5s
    clip = modifier.trim_clip(
        clip_id='Broll_Studio',
        trim_end='-1s',
        ripple=False
    )

    # Duration should decrease by 1s (24 frames at 24fps)
    new_duration = modifier._parse_time(clip.get('duration'))
    assert new_duration.to_seconds() == pytest.approx(4.0, abs=0.1)


def test_trim_clip_start_delta(temp_fcpxml):
    """Trim the start of a clip by a delta."""
    modifier = FCPXMLModifier(temp_fcpxml)

    original_clip = modifier.clips['Broll_Studio']
    original_start = modifier._parse_time(original_clip.get('start')).to_seconds()
    original_duration = modifier._parse_time(original_clip.get('duration')).to_seconds()

    clip = modifier.trim_clip(
        clip_id='Broll_Studio',
        trim_start='+1s',
        ripple=False
    )

    new_start = modifier._parse_time(clip.get('start')).to_seconds()
    new_duration = modifier._parse_time(clip.get('duration')).to_seconds()

    # Start moved forward by 1s, duration decreased by 1s
    assert new_start == pytest.approx(original_start + 1.0, abs=0.1)
    assert new_duration == pytest.approx(original_duration - 1.0, abs=0.1)


def test_trim_clip_strips_only_first_sign_char(temp_fcpxml):
    """Trim delta must strip only the leading sign, not multiple +/- chars.

    Regression: lstrip('+-') would eat '-+--120/24s' → '120/24s', silently
    consuming the entire prefix.  The fix uses [1:] so only the first
    character is removed — triple-signs like '---1s' become '--1s' which
    the parser rejects instead of silently treating as '1s'.
    """
    modifier = FCPXMLModifier(temp_fcpxml)

    # A well-formed single-sign delta should still work
    clip = modifier.trim_clip(clip_id='Broll_Studio', trim_end='-1s', ripple=False)
    dur = modifier._parse_time(clip.get('duration')).to_seconds()
    assert dur == pytest.approx(4.0, abs=0.1)

    # A triple-sign like '---1s' should fail (old lstrip would silently accept it)
    modifier2 = FCPXMLModifier(temp_fcpxml)
    with pytest.raises((ValueError, Exception)):
        modifier2.trim_clip(clip_id='Broll_Studio', trim_end='---1s', ripple=False)


def test_trim_clip_invalid_clip(temp_fcpxml):
    """Trimming a nonexistent clip raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="Clip not found"):
        modifier.trim_clip(clip_id='ghost_clip', trim_end='-1s')


# ============================================================
# Delete Clip Tests
# ============================================================

def test_delete_clip_ripple(temp_fcpxml):
    """Delete a clip with ripple (shifts subsequent clips)."""
    modifier = FCPXMLModifier(temp_fcpxml)
    initial_count = len(list(modifier._get_spine()))
    broll_before = sum(
        1 for c in modifier._get_spine() if c.get('name') == 'Broll_City'
    )

    modifier.delete_clip(clip_ids=['Broll_City'], ripple=True)

    final_count = len(list(modifier._get_spine()))
    assert final_count == initial_count - 1
    # One fewer Broll_City in spine; index still valid if duplicates remain
    broll_after = sum(
        1 for c in modifier._get_spine() if c.get('name') == 'Broll_City'
    )
    assert broll_after == broll_before - 1


def test_delete_clip_no_ripple_leaves_gap(temp_fcpxml):
    """Delete a clip without ripple replaces it with a gap."""
    modifier = FCPXMLModifier(temp_fcpxml)
    initial_count = len(list(modifier._get_spine()))

    modifier.delete_clip(clip_ids=['Broll_City'], ripple=False)

    # Element count stays the same (clip replaced by gap)
    final_count = len(list(modifier._get_spine()))
    assert final_count == initial_count

    # Verify a gap element was created
    spine = modifier._get_spine()
    gaps = [c for c in spine if c.tag == 'gap']
    assert len(gaps) >= 1


def test_delete_nonexistent_clip_is_noop(temp_fcpxml):
    """Deleting a nonexistent clip silently succeeds."""
    modifier = FCPXMLModifier(temp_fcpxml)
    initial_count = len(list(modifier._get_spine()))

    modifier.delete_clip(clip_ids=['does_not_exist'])

    final_count = len(list(modifier._get_spine()))
    assert final_count == initial_count


def test_delete_clip_duplicate_names_targets_first(temp_fcpxml):
    """Deleting a duplicate-named clip removes the first spine match, not the last-indexed."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()

    # Sample has 4 Interview_A clips; collect their offsets before deletion
    interview_clips_before = [
        c for c in spine if c.get('name') == 'Interview_A'
    ]
    assert len(interview_clips_before) == 4, "fixture should have 4 Interview_A clips"
    first_offset = interview_clips_before[0].get('offset')

    modifier.delete_clip(clip_ids=['Interview_A'], ripple=False)

    # The first Interview_A (by spine order) should be gone
    interview_clips_after = [
        c for c in modifier._get_spine() if c.get('name') == 'Interview_A'
    ]
    assert len(interview_clips_after) == 3
    # The removed clip was the one at first_offset
    remaining_offsets = {c.get('offset') for c in interview_clips_after}
    assert first_offset not in remaining_offsets

    # Index should still point to a remaining Interview_A (not orphaned)
    assert 'Interview_A' in modifier.clips
    assert modifier.clips['Interview_A'] in interview_clips_after


def test_delete_clip_duplicate_names_sequential(temp_fcpxml):
    """Deleting duplicate-named clips one by one removes them in spine order."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Delete all 4 Interview_A clips one at a time
    for expected_remaining in [3, 2, 1, 0]:
        remaining = [
            c for c in modifier._get_spine() if c.get('name') == 'Interview_A'
        ]
        if not remaining:
            break
        modifier.delete_clip(clip_ids=['Interview_A'], ripple=False)
        after = [
            c for c in modifier._get_spine() if c.get('name') == 'Interview_A'
        ]
        assert len(after) == expected_remaining

    # All gone, index entry removed
    assert 'Interview_A' not in modifier.clips


# ============================================================
# Split Clip Tests
# ============================================================

def test_split_clip_single_point(temp_fcpxml):
    """Split a clip at one point produces two segments."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # The indexed Interview_A is the last one: offset 1122/24s, duration 168/24s = 7s
    original_duration = modifier._parse_time(
        modifier.clips['Interview_A'].get('duration')
    ).to_seconds()

    results = modifier.split_clip(
        clip_id='Interview_A',
        split_points=['00:00:03:00']  # Split 3s into a 7s clip
    )

    assert len(results) == 2

    # Both segments should have duration summing to original
    dur1 = modifier._parse_time(results[0].get('duration')).to_seconds()
    dur2 = modifier._parse_time(results[1].get('duration')).to_seconds()
    assert dur1 + dur2 == pytest.approx(original_duration, abs=0.5)


def test_split_clip_removes_stale_index_entry(temp_fcpxml):
    """After splitting, the original clip_id must be removed from self.clips.

    Previously, split_clip left the original key pointing at a detached
    XML element, causing silent corruption on subsequent operations.
    """
    modifier = FCPXMLModifier(temp_fcpxml)
    assert 'Interview_A' in modifier.clips

    modifier.split_clip(
        clip_id='Interview_A',
        split_points=['00:00:03:00']
    )

    # Original key must be gone
    assert 'Interview_A' not in modifier.clips
    # Split keys must exist and reference live spine elements
    assert 'Interview_A_split_0' in modifier.clips
    assert 'Interview_A_split_1' in modifier.clips

    spine = modifier._get_spine()
    spine_elements = list(spine)
    assert modifier.clips['Interview_A_split_0'] in spine_elements
    assert modifier.clips['Interview_A_split_1'] in spine_elements


def test_split_clip_invalid_clip(temp_fcpxml):
    """Splitting a nonexistent clip raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="Clip not found"):
        modifier.split_clip(clip_id='ghost', split_points=['00:00:01:00'])


# ============================================================
# Change Speed Tests
# ============================================================

def test_change_speed_double(temp_fcpxml):
    """Doubling speed halves clip duration."""
    modifier = FCPXMLModifier(temp_fcpxml)

    original_duration = modifier._parse_time(
        modifier.clips['Broll_Studio'].get('duration')
    ).to_seconds()

    clip = modifier.change_speed(clip_id='Broll_Studio', speed=2.0)

    new_duration = modifier._parse_time(clip.get('duration')).to_seconds()
    assert new_duration == pytest.approx(original_duration / 2.0, abs=0.1)

    # Should have a timeMap element
    timemap = clip.find('timeMap')
    assert timemap is not None


def test_change_speed_half(temp_fcpxml):
    """Halving speed doubles clip duration."""
    modifier = FCPXMLModifier(temp_fcpxml)

    original_duration = modifier._parse_time(
        modifier.clips['Broll_Studio'].get('duration')
    ).to_seconds()

    clip = modifier.change_speed(clip_id='Broll_Studio', speed=0.5)

    new_duration = modifier._parse_time(clip.get('duration')).to_seconds()
    assert new_duration == pytest.approx(original_duration * 2.0, abs=0.1)


def test_change_speed_invalid_clip(temp_fcpxml):
    """Changing speed on nonexistent clip raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="Clip not found"):
        modifier.change_speed(clip_id='ghost', speed=2.0)


def test_change_speed_zero_raises(temp_fcpxml):
    """Speed=0 raises ValueError instead of ZeroDivisionError."""
    modifier = FCPXMLModifier(temp_fcpxml)
    with pytest.raises(ValueError, match="Speed must be positive"):
        modifier.change_speed(clip_id='Broll_Studio', speed=0)


def test_change_speed_negative_raises(temp_fcpxml):
    """Negative speed raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)
    with pytest.raises(ValueError, match="Speed must be positive"):
        modifier.change_speed(clip_id='Broll_Studio', speed=-1.0)


def test_change_speed_twice_no_duplicate_elements(temp_fcpxml):
    """Calling change_speed twice must not create duplicate timeMap/conform-rate."""
    modifier = FCPXMLModifier(temp_fcpxml)
    modifier.change_speed(clip_id='Broll_Studio', speed=2.0)
    modifier.change_speed(clip_id='Broll_Studio', speed=0.5)

    clip = modifier.clips['Broll_Studio']
    timemaps = clip.findall('timeMap')
    conform_rates = clip.findall('conform-rate')

    assert len(timemaps) == 1, (
        f"Expected 1 timeMap after two speed changes, got {len(timemaps)}"
    )
    assert len(conform_rates) == 1, (
        f"Expected 1 conform-rate after two speed changes, got {len(conform_rates)}"
    )

    # Verify the final speed is 0.5x (duration should double)
    duration_str = clip.get('duration')
    duration = modifier._parse_time(duration_str)
    original = modifier._parse_time('2400/2400s')  # 1s original
    assert duration.to_seconds() > original.to_seconds(), (
        "0.5x speed should produce longer duration than original"
    )


# ============================================================
# change_speed — Fractional & Edge-Case Speeds
# ============================================================

def test_change_speed_fractional_1_5x(temp_fcpxml):
    """1.5x speed (cinema ramp) produces valid rational timeMap values."""
    modifier = FCPXMLModifier(temp_fcpxml)
    orig_dur = modifier._parse_time(
        modifier.clips['Broll_Studio'].get('duration')
    ).to_seconds()
    clip = modifier.change_speed(clip_id='Broll_Studio', speed=1.5)
    new_dur = modifier._parse_time(clip.get('duration')).to_seconds()
    # 1.5x → duration shrinks to ~2/3
    assert new_dur == pytest.approx(orig_dur / 1.5, abs=0.05)
    # timeMap end keyframe must use rational format, never floats
    timemap = clip.find('timeMap')
    tp2 = list(timemap)[1]
    assert 's' in tp2.get('time')
    assert '.' not in tp2.get('time'), "timeMap must use rational time, not floats"


def test_change_speed_quarter(temp_fcpxml):
    """0.25x extreme slow-mo: duration 4x, timeMap keyframes are rational."""
    modifier = FCPXMLModifier(temp_fcpxml)
    orig_dur = modifier._parse_time(
        modifier.clips['Broll_Studio'].get('duration')
    ).to_seconds()
    clip = modifier.change_speed(clip_id='Broll_Studio', speed=0.25)
    new_dur = modifier._parse_time(clip.get('duration')).to_seconds()
    assert new_dur == pytest.approx(orig_dur * 4.0, abs=0.2)


def test_change_speed_conform_rate_has_src_framerate(temp_fcpxml):
    """conform-rate element must carry srcFrameRate matching timeline fps."""
    modifier = FCPXMLModifier(temp_fcpxml)
    clip = modifier.change_speed(clip_id='Broll_Studio', speed=2.0)
    conform = clip.find('conform-rate')
    assert conform is not None
    assert conform.get('srcFrameRate') == str(int(modifier.fps))


def test_change_speed_preserve_pitch_default(temp_fcpxml):
    """preserve_pitch=True is default — conform-rate scaleEnabled should be set."""
    modifier = FCPXMLModifier(temp_fcpxml)
    clip = modifier.change_speed(clip_id='Broll_Studio', speed=2.0, preserve_pitch=True)
    conform = clip.find('conform-rate')
    assert conform.get('scaleEnabled') == '1'


def test_change_speed_three_times_still_single_elements(temp_fcpxml):
    """Triple speed change must still have exactly 1 timeMap + 1 conform-rate."""
    modifier = FCPXMLModifier(temp_fcpxml)
    modifier.change_speed(clip_id='Broll_Studio', speed=2.0)
    modifier.change_speed(clip_id='Broll_Studio', speed=0.5)
    modifier.change_speed(clip_id='Broll_Studio', speed=3.0)
    clip = modifier.clips['Broll_Studio']
    assert len(clip.findall('timeMap')) == 1
    assert len(clip.findall('conform-rate')) == 1


# ============================================================
# DTD Element Ordering Tests
# ============================================================

def test_change_speed_dtd_order(temp_fcpxml):
    """timeMap and conform-rate must precede markers in DTD order."""
    modifier = FCPXMLModifier(temp_fcpxml)
    # First add a marker, then change speed — timeMap must end up before markers
    modifier.add_marker(clip_id='Broll_City', timecode='00:00:04:00', name='test')
    modifier.change_speed(clip_id='Broll_City', speed=2.0)

    clip = modifier.clips['Broll_City']
    children = list(clip)
    tags = [c.tag for c in children]

    # timeMap must come before any marker/chapter-marker
    if 'timeMap' in tags and 'marker' in tags:
        assert tags.index('timeMap') < tags.index('marker'), \
            f"timeMap must precede marker in DTD order, got: {tags}"
    if 'conform-rate' in tags and 'marker' in tags:
        assert tags.index('conform-rate') < tags.index('marker'), \
            f"conform-rate must precede marker in DTD order, got: {tags}"


def test_marker_after_adjust_elements(temp_fcpxml):
    """Markers added after adjust elements must maintain DTD order."""
    modifier = FCPXMLModifier(temp_fcpxml)
    modifier.change_speed(clip_id='Broll_City', speed=0.5)
    modifier.add_marker(clip_id='Broll_City', timecode='00:00:04:00', name='after-speed')

    clip = modifier.clips['Broll_City']
    children = list(clip)
    tags = [c.tag for c in children]

    if 'timeMap' in tags and 'marker' in tags:
        assert tags.index('timeMap') < tags.index('marker'), \
            f"timeMap must precede marker, got: {tags}"


# ============================================================
# Add Transition Tests
# ============================================================

def test_add_transition_at_end(temp_fcpxml):
    """Add a cross-dissolve transition at the end of a clip."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    initial_count = len(list(spine))

    transition = modifier.add_transition(
        clip_id='Broll_City',
        position='end',
        transition_type='cross-dissolve',
        duration='00:00:00:12'
    )

    assert transition.tag == 'transition'
    assert transition.get('name') == 'Cross Dissolve'
    assert transition.get('duration') is not None
    fv = transition.find('filter-video')
    assert fv is not None
    assert fv.get('ref') is not None  # must reference an effect resource
    assert len(list(modifier._get_spine())) == initial_count + 1


def test_add_transition_at_both(temp_fcpxml):
    """Add transitions at both ends inserts two elements."""
    modifier = FCPXMLModifier(temp_fcpxml)
    initial_count = len(list(modifier._get_spine()))

    result = modifier.add_transition(
        clip_id='Broll_City', position='both',
        transition_type='cross-dissolve', duration='00:00:00:12'
    )

    assert isinstance(result, list) or result.tag == 'transition'
    assert len(list(modifier._get_spine())) == initial_count + 2


def test_add_transition_invalid_clip(temp_fcpxml):
    """Adding a transition to a nonexistent clip raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="Clip not found"):
        modifier.add_transition(clip_id='ghost_clip', position='end')


# ============================================================
# Reorder Clips Tests
# ============================================================

def test_reorder_clips_to_end(temp_fcpxml):
    """Move a clip to the end of the timeline."""
    modifier = FCPXMLModifier(temp_fcpxml)
    initial_count = len(list(modifier._get_spine()))

    modifier.reorder_clips(
        clip_ids=['Broll_City'], target_position='end', ripple=True
    )

    spine_list = list(modifier._get_spine())
    assert len(spine_list) == initial_count
    assert spine_list[-1].get('name') == 'Broll_City'


def test_reorder_clips_to_start(temp_fcpxml):
    """Move a clip to the start of the timeline."""
    modifier = FCPXMLModifier(temp_fcpxml)

    modifier.reorder_clips(
        clip_ids=['Broll_City'], target_position='start', ripple=True
    )

    first_clip = list(modifier._get_spine())[0]
    assert first_clip.get('name') == 'Broll_City'
    assert first_clip.get('offset') in ('0s', '0/24s')


def test_reorder_clips_invalid_raises(temp_fcpxml):
    """Reordering nonexistent clips raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="No clips found"):
        modifier.reorder_clips(clip_ids=['nonexistent'], target_position='start')


# ============================================================
# Add Marker at Timeline Tests
# ============================================================

def test_add_marker_at_timeline_position(temp_fcpxml):
    """Add a marker at a timeline position within an indexed clip."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Broll_Studio (last indexed): offset 222/24s=9.25s, dur 120/24s=5s
    marker = modifier.add_marker_at_timeline(
        timecode='00:00:10:00', name='Timeline marker'
    )

    assert marker is not None
    assert marker.get('value') == 'Timeline marker'


def test_add_marker_at_timeline_invalid_position(temp_fcpxml):
    """Adding a marker past the timeline end still raises ValueError.

    ``add_marker_at_timeline`` now falls back to a connected-clip-aware
    lookup (a music video's spine holds one ``<gap>`` and no clip at all),
    so the failure message comes from that second pass. The contract that
    matters — an out-of-range position raises rather than silently landing
    somewhere — is unchanged, and the message now names the timeline origin
    it resolved against.
    """
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="No spine element at position"):
        modifier.add_marker_at_timeline(timecode='99:00:00:00', name='Way past end')


def test_add_marker_at_timeline_duplicate_clip_names(temp_fcpxml):
    """Marker lands on the correct clip even when multiple clips share a name.

    The sample has 4 clips named 'Interview_A'. The old implementation used
    the name-indexed ``self.clips`` dict which only kept the *last* one,
    so markers targeting earlier Interview_A clips would silently fail.
    The fix uses ``_find_spine_clip_at_seconds`` which walks the spine
    directly.
    """
    modifier = FCPXMLModifier(temp_fcpxml)

    # First Interview_A: offset=0s, duration=72/24s (3s) → timeline 0–3s
    marker = modifier.add_marker_at_timeline(
        timecode='00:00:01:00', name='First interview marker'
    )
    assert marker is not None
    assert marker.get('value') == 'First interview marker'

    # The marker should be a child of the FIRST spine clip (offset=0s),
    # not the last Interview_A clip (offset=1122/24s=46.75s).
    spine = modifier._get_spine()
    first_clip = list(spine)[0]
    assert first_clip.get('offset') == '0s'
    marker_values = [m.get('value') for m in first_clip.findall('marker')]
    assert 'First interview marker' in marker_values


# ============================================================
# Batch Add Markers Tests
# ============================================================

def test_batch_add_markers_explicit(temp_fcpxml):
    """Add multiple explicit markers in a batch."""
    modifier = FCPXMLModifier(temp_fcpxml)

    markers = [
        {'timecode': '00:00:10:00', 'name': 'Marker A'},
        {'timecode': '00:00:48:00', 'name': 'Marker B'},
    ]

    created = modifier.batch_add_markers(markers=markers)

    assert len(created) == 2
    assert created[0].get('value') == 'Marker A'
    assert created[1].get('value') == 'Marker B'


def test_batch_add_markers_with_type(temp_fcpxml):
    """Batch markers respect marker_type parameter."""
    modifier = FCPXMLModifier(temp_fcpxml)

    markers = [
        {'timecode': '00:00:10:00', 'name': 'Chapter 1', 'marker_type': 'chapter'},
    ]

    created = modifier.batch_add_markers(markers=markers)

    assert len(created) == 1
    assert created[0].tag == 'chapter-marker'


# ============================================================
# Select by Keyword Tests
# ============================================================

def test_select_by_keyword_any(temp_fcpxml):
    """Select clips matching any of the given keywords."""
    modifier = FCPXMLModifier(temp_fcpxml)

    matches = modifier.select_by_keyword(keywords=['B-Roll'], match_mode='any')

    assert len(matches) >= 1
    for clip_id in matches:
        clip = modifier.clips[clip_id]
        kw_values = [kw.get('value', '') for kw in clip.findall('keyword')]
        assert 'B-Roll' in kw_values


def test_select_by_keyword_no_matches(temp_fcpxml):
    """Selecting with a nonexistent keyword returns empty list."""
    modifier = FCPXMLModifier(temp_fcpxml)

    matches = modifier.select_by_keyword(keywords=['FakeKeyword'], match_mode='any')
    assert matches == []


def test_select_by_keyword_all_mode(temp_fcpxml):
    """'all' mode requires clips to have every keyword."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # No clip has both 'Interview' AND 'B-Roll'
    matches = modifier.select_by_keyword(
        keywords=['Interview', 'B-Roll'], match_mode='all'
    )
    assert matches == []


# ============================================================
# _absorb_into_neighbor Tests
# ============================================================

def test_absorb_into_prev_extends_duration(temp_fcpxml):
    """Absorbing a clip into its previous neighbor grows the neighbor's duration."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    clips = list(modifier._iter_spine_clips())
    assert len(clips) >= 3

    target_idx, target = clips[1]
    prev_idx, prev_clip = clips[0]
    prev_dur_before = modifier._parse_time(prev_clip.get('duration', '0s')).to_seconds()
    target_dur = modifier._parse_time(target.get('duration', '0s')).to_seconds()

    result = modifier._absorb_into_neighbor(spine, target, 'prev')
    assert result is prev_clip
    prev_dur_after = modifier._parse_time(prev_clip.get('duration', '0s')).to_seconds()
    assert abs(prev_dur_after - (prev_dur_before + target_dur)) < 0.001
    assert target not in list(spine)


def test_absorb_into_next_extends_duration(temp_fcpxml):
    """Absorbing a clip into its next neighbor grows the neighbor and adjusts start."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    clips = list(modifier._iter_spine_clips())
    assert len(clips) >= 3

    target_idx, target = clips[1]
    next_idx, next_clip = clips[2]
    next_dur_before = modifier._parse_time(next_clip.get('duration', '0s')).to_seconds()
    next_start_before = modifier._parse_time(next_clip.get('start', '0s')).to_seconds()
    target_dur = modifier._parse_time(target.get('duration', '0s')).to_seconds()

    result = modifier._absorb_into_neighbor(spine, target, 'next')
    assert result is next_clip
    next_dur_after = modifier._parse_time(next_clip.get('duration', '0s')).to_seconds()
    assert abs(next_dur_after - (next_dur_before + target_dur)) < 0.001
    assert target not in list(spine)

    # Start should be pulled earlier (if it was >= absorbed duration)
    if next_start_before >= target_dur:
        next_start_after = modifier._parse_time(next_clip.get('start', '0s')).to_seconds()
        assert abs(next_start_after - (next_start_before - target_dur)) < 0.001


def test_absorb_no_neighbor_returns_none(temp_fcpxml):
    """Returns None when there's no neighbor in the requested direction."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    clips = list(modifier._iter_spine_clips())

    first_clip = clips[0][1]
    result = modifier._absorb_into_neighbor(spine, first_clip, 'prev')
    assert result is None
    # Element should NOT have been removed
    assert first_clip in list(spine)


def test_trim_clip_rejects_negative_duration(temp_fcpxml):
    """Trimming beyond clip length must raise, not silently write negative duration."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Broll_Studio has 5s duration — trimming end by 6s would give -1s
    with pytest.raises(ValueError, match="non-positive duration"):
        modifier.trim_clip(clip_id='Broll_Studio', trim_end='-6s', ripple=False)


def test_trim_clip_rejects_zero_duration(temp_fcpxml):
    """Trimming to exactly zero must also raise."""
    modifier = FCPXMLModifier(temp_fcpxml)

    # Broll_Studio has 5s duration — trimming end by exactly 5s gives 0s
    with pytest.raises(ValueError, match="non-positive duration"):
        modifier.trim_clip(clip_id='Broll_Studio', trim_end='-5s', ripple=False)


def test_trim_clip_start_rejects_over_trim(temp_fcpxml):
    """Moving start forward by more than duration must raise."""
    modifier = FCPXMLModifier(temp_fcpxml)

    with pytest.raises(ValueError, match="non-positive duration"):
        modifier.trim_clip(clip_id='Broll_Studio', trim_start='+6s', ripple=False)


def test_add_transition_rejects_negative_offset():
    """Transition at start of first clip must raise when offset would go negative."""
    xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>
            <asset id="r2" src="file:///clip.mov" hasVideo="1" hasAudio="1"/>
          </resources>
          <library>
            <event name="Test">
              <project name="Test">
                <sequence format="r1">
                  <spine>
                    <asset-clip name="FirstClip" ref="r2" offset="0s"
                                start="0s" duration="120/24s"/>
                    <asset-clip name="SecondClip" ref="r2" offset="120/24s"
                                start="0s" duration="120/24s"/>
                  </spine>
                </sequence>
              </project>
            </event>
          </library>
        </fcpxml>
    """)
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml)
        f.flush()
        modifier = FCPXMLModifier(f.name)

    # FirstClip offset=0, half of 20s transition = 10s → offset -10s
    with pytest.raises(ValueError, match="negative offset"):
        modifier.add_transition(clip_id='FirstClip', duration='20s', position='start')

    os.unlink(f.name)


def test_absorb_next_clamps_when_start_insufficient(temp_fcpxml):
    """When absorbing forward, if neighbor start < absorbed duration, clamp to 0."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    clips = list(modifier._iter_spine_clips())

    # Find a neighbor whose start is 0 (or very small)
    target_idx, target = clips[1]
    next_idx, next_clip = clips[2]
    # Force next_clip start to 0 so absorb can't shift it further back
    next_clip.set('start', '0s')
    next_dur_before = modifier._parse_time(next_clip.get('duration', '0s')).to_seconds()

    result = modifier._absorb_into_neighbor(spine, target, 'next')
    assert result is next_clip
    # Start should be clamped to 0, not negative
    next_start_after = modifier._parse_time(next_clip.get('start', '0s')).to_seconds()
    assert next_start_after == 0.0
    # Duration only extended by available headroom (0), so unchanged
    next_dur_after = modifier._parse_time(next_clip.get('duration', '0s')).to_seconds()
    assert next_dur_after == pytest.approx(next_dur_before, abs=0.001)


# ============================================================
# _resolve_asset Tests
# ============================================================

def test_resolve_asset_by_id(temp_fcpxml):
    """Resolves an asset by its resource ID."""
    modifier = FCPXMLModifier(temp_fcpxml)
    first_id = next(iter(modifier.resources))
    asset, resolved_id = modifier._resolve_asset(first_id, None)
    assert resolved_id == first_id
    assert asset is modifier.resources[first_id]


def test_resolve_asset_by_name(temp_fcpxml):
    """Resolves an asset by name when no ID is given."""
    modifier = FCPXMLModifier(temp_fcpxml)
    first_id = next(iter(modifier.resources))
    expected_name = modifier.resources[first_id].get('name')
    asset, resolved_id = modifier._resolve_asset(None, expected_name)
    assert resolved_id == first_id
    assert asset.get('name') == expected_name


def test_resolve_asset_not_found(temp_fcpxml):
    """Raises ValueError when neither ID nor name matches."""
    modifier = FCPXMLModifier(temp_fcpxml)
    with pytest.raises(ValueError, match="Asset not found"):
        modifier._resolve_asset('r_nonexistent', None)


# ============================================================
# _unique_resource_id Tests
# ============================================================

def test_unique_resource_id_no_collision(temp_fcpxml):
    """Returns the prefix as-is when no collision exists."""
    modifier = FCPXMLModifier(temp_fcpxml)
    resources = modifier.root.find('.//resources')
    result = modifier._unique_resource_id(resources, 'r_fresh1')
    assert result == 'r_fresh1'


def test_unique_resource_id_with_collision(temp_fcpxml):
    """Increments the counter suffix until no collision."""
    modifier = FCPXMLModifier(temp_fcpxml)
    resources = modifier.root.find('.//resources')
    # The first resource ID should already exist
    existing_id = next(iter(modifier.resources))
    result = modifier._unique_resource_id(resources, existing_id)
    assert result != existing_id
    assert result not in {el.get('id', '') for el in resources}


# ============================================================
# _find_spine_element_at_timecode Tests
# ============================================================

def test_find_spine_element_at_timecode_match(temp_fcpxml):
    """Finds a spine element whose offset matches the given timecode."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    first_child = list(spine)[0]
    offset_str = first_child.get('offset', '0s')
    from fcpxml.models import TimeValue
    tc = TimeValue.from_timecode(offset_str, modifier.fps).to_timecode(modifier.fps)

    found = modifier._find_spine_element_at_timecode(spine, tc)
    assert found is first_child


def test_find_spine_element_at_timecode_no_match(temp_fcpxml):
    """Returns None when no element has a matching offset."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    result = modifier._find_spine_element_at_timecode(spine, '99999/1s')
    assert result is None


def test_find_spine_element_require_clip(temp_fcpxml):
    """With require_clip=True, skips non-clip elements like gaps."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    # Insert a gap at the beginning to test filtering
    import xml.etree.ElementTree as ET
    gap = ET.Element('gap')
    gap.set('offset', '0s')
    gap.set('duration', '100/2400s')
    spine.insert(0, gap)

    # require_clip=True should skip the gap
    result = modifier._find_spine_element_at_timecode(spine, '0s', require_clip=True)
    assert result is None or result.tag != 'gap'


# ============================================================
# _require_clip / _require_spine_clip Tests
# ============================================================

def test_require_clip_returns_element(temp_fcpxml):
    """_require_clip returns the clip element for a valid ID."""
    modifier = FCPXMLModifier(temp_fcpxml)
    clip_id = next(iter(modifier.clips))
    result = modifier._require_clip(clip_id)
    assert result is modifier.clips[clip_id]


def test_require_clip_raises_on_missing(temp_fcpxml):
    """_require_clip raises ValueError for a non-existent ID."""
    modifier = FCPXMLModifier(temp_fcpxml)
    with pytest.raises(ValueError, match="Clip not found: no_such_clip"):
        modifier._require_clip("no_such_clip")


def test_require_spine_clip_returns_tuple(temp_fcpxml):
    """_require_spine_clip returns (spine, clip, index) for a spine clip."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    spine_children = list(spine)
    # Find a clip_id that's actually in the spine
    target_id = None
    for cid, elem in modifier.clips.items():
        if elem in spine_children:
            target_id = cid
            break
    assert target_id is not None, "No clip in spine for this test"

    result_spine, result_clip, result_idx = modifier._require_spine_clip(target_id)
    assert result_spine is spine
    assert result_clip is modifier.clips[target_id]
    assert spine_children[result_idx] is result_clip


def test_require_spine_clip_raises_for_missing(temp_fcpxml):
    """_require_spine_clip raises ValueError for a non-existent clip."""
    modifier = FCPXMLModifier(temp_fcpxml)
    with pytest.raises(ValueError, match="Clip not found"):
        modifier._require_spine_clip("nonexistent_clip")


def test_require_spine_clip_raises_for_non_spine(temp_fcpxml):
    """_require_spine_clip raises ValueError for a clip not in the spine."""
    import xml.etree.ElementTree as ET
    modifier = FCPXMLModifier(temp_fcpxml)
    # Inject a clip into the index that isn't in the spine
    fake = ET.Element('clip')
    fake.set('name', 'orphan')
    modifier.clips['orphan'] = fake
    with pytest.raises(ValueError, match="Clip not in spine: orphan"):
        modifier._require_spine_clip("orphan")


# ============================================================
# _ripple_from_index Tests
# ============================================================

def test_ripple_from_index_shifts_offsets(temp_fcpxml):
    """Shifts offsets of spine elements from start_index onward by positive delta."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    children = list(spine)
    assert len(children) >= 3, "Need at least 3 clips for this test"

    from fcpxml.models import TimeValue
    original_offsets = [c.get('offset', '0s') for c in children]
    delta = TimeValue(240, 24)  # +10 seconds

    modifier._ripple_from_index(spine, 1, delta)

    updated = list(spine)
    # First element unchanged
    assert updated[0].get('offset') == original_offsets[0]
    # Elements from index 1 onward shifted by delta
    for i in range(1, len(updated)):
        orig = modifier._parse_time(original_offsets[i])
        shifted = modifier._parse_time(updated[i].get('offset', '0s'))
        assert shifted == orig + delta


def test_ripple_from_index_negative_delta(temp_fcpxml):
    """Shifts offsets backward with a negative delta."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    children = list(spine)

    from fcpxml.models import TimeValue
    # Use a small negative delta that won't go below zero for later clips
    delta = TimeValue(-24, 24)  # -1 second
    original_offset_2 = modifier._parse_time(children[2].get('offset', '0s'))

    modifier._ripple_from_index(spine, 2, delta)

    new_offset_2 = modifier._parse_time(list(spine)[2].get('offset', '0s'))
    assert new_offset_2 == original_offset_2 + delta


def test_ripple_from_index_past_end_is_noop(temp_fcpxml):
    """Rippling from an index beyond children count does nothing."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    children = list(spine)
    original_offsets = [c.get('offset') for c in children]

    from fcpxml.models import TimeValue
    modifier._ripple_from_index(spine, len(children) + 10, TimeValue(100, 1))

    for i, child in enumerate(spine):
        assert child.get('offset') == original_offsets[i]


def test_ripple_from_index_skips_non_spine_tags(temp_fcpxml):
    """Non-spine-element tags (e.g. <note>) are left unchanged."""
    import xml.etree.ElementTree as ET
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()

    note = ET.SubElement(spine, 'note')
    note.text = 'test note'
    note.set('offset', '0s')

    from fcpxml.models import TimeValue
    modifier._ripple_from_index(spine, 0, TimeValue(48, 24))

    # <note> is not in SPINE_ELEMENT_TAGS, so its offset stays
    assert note.get('offset') == '0s'


# ============================================================
# _timeline_duration Tests
# ============================================================

def test_timeline_duration_from_sequence(temp_fcpxml):
    """Reads duration from <sequence> element when present."""
    modifier = FCPXMLModifier(temp_fcpxml)
    seq = modifier.root.find('.//sequence')
    assert seq is not None, "Sample must have a <sequence>"
    expected = modifier._parse_time(seq.get('duration'))

    result = modifier._timeline_duration()
    assert result == expected


def test_timeline_duration_fallback_to_spine_sum(temp_fcpxml):
    """Falls back to summing spine durations when <sequence> has no duration attr."""
    modifier = FCPXMLModifier(temp_fcpxml)
    seq = modifier.root.find('.//sequence')
    # Remove the duration attribute to force fallback
    if seq is not None and 'duration' in seq.attrib:
        del seq.attrib['duration']

    from fcpxml.models import TimeValue
    spine = modifier._get_spine()
    expected = TimeValue.zero()
    for child in spine:
        if child.tag in ('clip', 'asset-clip', 'video', 'audio', 'gap',
                         'transition', 'ref-clip'):
            expected = expected + modifier._parse_time(child.get('duration', '0s'))

    result = modifier._timeline_duration()
    assert result == expected
    assert result.numerator > 0  # sanity: non-empty timeline


def test_timeline_duration_no_sequence_element():
    """Returns spine sum when no <sequence> element exists at all."""
    xml_str = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
            <resources>
                <format id="r1" name="FFVideoFormat1080p24"
                        frameDuration="100/2400s" width="1920" height="1080"/>
            </resources>
            <library>
                <event name="Test">
                    <project name="Test">
                        <sequence format="r1">
                            <spine>
                                <asset-clip ref="r1" offset="0s" name="A"
                                            start="0s" duration="2400/2400s"/>
                                <asset-clip ref="r1" offset="2400/2400s" name="B"
                                            start="0s" duration="1200/2400s"/>
                            </spine>
                        </sequence>
                    </project>
                </event>
            </library>
        </fcpxml>""")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        # Remove duration from sequence to force fallback
        seq = modifier.root.find('.//sequence')
        if seq is not None and 'duration' in seq.attrib:
            del seq.attrib['duration']
        result = modifier._timeline_duration()
        from fcpxml.models import TimeValue
        assert result == TimeValue(3600, 2400)  # 2400 + 1200
    Path(f.name).unlink(missing_ok=True)


# ============================================================
# _find_neighbor_clip Tests
# ============================================================

def test_find_neighbor_clip_prev(temp_fcpxml):
    """Finds the previous clip in the spine."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    spine_list = list(spine)
    assert len(spine_list) >= 2
    result = FCPXMLModifier._find_neighbor_clip(spine_list, 1, 'prev')
    assert result is spine_list[0]


def test_find_neighbor_clip_next(temp_fcpxml):
    """Finds the next clip in the spine."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    spine_list = list(spine)
    result = FCPXMLModifier._find_neighbor_clip(spine_list, 0, 'next')
    assert result is spine_list[1]


def test_find_neighbor_clip_none_at_boundary(temp_fcpxml):
    """Returns None when no neighbor exists in the given direction."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    spine_list = list(spine)
    assert FCPXMLModifier._find_neighbor_clip(spine_list, 0, 'prev') is None
    last = len(spine_list) - 1
    assert FCPXMLModifier._find_neighbor_clip(spine_list, last, 'next') is None


def test_find_neighbor_clip_skips_gaps():
    """Skips gap elements when searching for neighbor clips."""
    import xml.etree.ElementTree as ET
    spine_list = [
        ET.Element('asset-clip'),
        ET.Element('gap'),
        ET.Element('asset-clip'),
    ]
    # Searching next from index 0 should skip the gap at index 1
    result = FCPXMLModifier._find_neighbor_clip(spine_list, 0, 'next')
    assert result is spine_list[2]


# ============================================================
# _resolve_asset Edge Cases
# ============================================================

def test_resolve_asset_both_none(temp_fcpxml):
    """Raises ValueError when both asset_id and asset_name are None."""
    modifier = FCPXMLModifier(temp_fcpxml)
    with pytest.raises(ValueError, match="Asset not found"):
        modifier._resolve_asset(None, None)


def test_resolve_asset_id_takes_precedence(temp_fcpxml):
    """When both ID and name are given, ID lookup wins."""
    modifier = FCPXMLModifier(temp_fcpxml)
    first_id = next(iter(modifier.resources))
    asset, resolved = modifier._resolve_asset(first_id, 'wrong_name')
    assert resolved == first_id


# ============================================================
# _find_clip_index Tests
# ============================================================

def test_find_clip_index_returns_position(temp_fcpxml):
    """Returns correct index for a clip present in spine."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    children = list(spine)
    for i, child in enumerate(children):
        assert modifier._find_clip_index(spine, child) == i


def test_find_clip_index_returns_none_for_missing():
    """Returns None for an element not in the spine."""
    import xml.etree.ElementTree as ET
    xml_str = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
            <resources>
                <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>
            </resources>
            <library><event name="T"><project name="T">
                <sequence format="r1" duration="2400/2400s">
                    <spine>
                        <asset-clip ref="r1" offset="0s" name="A"
                                    start="0s" duration="2400/2400s"/>
                    </spine>
                </sequence>
            </project></event></library>
        </fcpxml>""")
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        spine = modifier._get_spine()
        orphan = ET.Element('asset-clip')
        assert modifier._find_clip_index(spine, orphan) is None
    Path(f.name).unlink(missing_ok=True)


# ============================================================
# _absorb_into_neighbor Tests
# ============================================================

def _make_absorb_modifier(clips):
    """Build a modifier with a custom spine for absorption tests.

    clips: list of (tag, name, offset, start, duration) tuples.
    """
    clip_xml = '\n'.join(
        f'<{tag} ref="r1" offset="{off}" name="{n}" start="{st}" duration="{dur}"/>'
        for tag, n, off, st, dur in clips
    )
    xml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.11">\n'
        '  <resources>\n'
        '    <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>\n'
        '  </resources>\n'
        '  <library><event name="T"><project name="T">\n'
        '    <sequence format="r1" duration="9600/2400s">\n'
        f'      <spine>{clip_xml}</spine>\n'
        '    </sequence>\n'
        '  </project></event></library>\n'
        '</fcpxml>'
    )
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        return FCPXMLModifier(f.name), f.name


def test_absorb_into_neighbor_prev_extends_duration():
    """Absorbing into previous clip extends its duration."""
    modifier, path = _make_absorb_modifier([
        ('asset-clip', 'A', '0s', '0s', '2400/2400s'),
        ('gap', 'G', '2400/2400s', '0s', '600/2400s'),
        ('asset-clip', 'B', '3000/2400s', '0s', '2400/2400s'),
    ])
    try:
        spine = modifier._get_spine()
        gap = list(spine)[1]
        result = modifier._absorb_into_neighbor(spine, gap, 'prev')
        assert result is not None
        assert result.get('name') == 'A'
        # A was 2400/2400s, absorbed gap of 600/2400s → 3000/2400s
        assert modifier._parse_time(result.get('duration')).numerator == 3000
        # Gap should be removed
        assert len(list(spine)) == 2
    finally:
        Path(path).unlink(missing_ok=True)


def test_absorb_into_neighbor_next_shifts_start():
    """Absorbing into next clip shifts its start backward."""
    modifier, path = _make_absorb_modifier([
        ('asset-clip', 'A', '0s', '0s', '2400/2400s'),
        ('gap', 'G', '2400/2400s', '0s', '600/2400s'),
        ('asset-clip', 'B', '3000/2400s', '2400/2400s', '2400/2400s'),
    ])
    try:
        spine = modifier._get_spine()
        gap = list(spine)[1]
        result = modifier._absorb_into_neighbor(spine, gap, 'next')
        assert result is not None
        assert result.get('name') == 'B'
        # B start was 2400/2400s, shifted back by 600/2400s → 1800/2400s
        assert modifier._parse_time(result.get('start')).numerator == 1800
        # B duration was 2400/2400s + 600/2400s → 3000/2400s
        assert modifier._parse_time(result.get('duration')).numerator == 3000
    finally:
        Path(path).unlink(missing_ok=True)


def test_absorb_into_neighbor_next_clamps_negative_start():
    """When absorbing would push start below zero, clamp to 0."""
    modifier, path = _make_absorb_modifier([
        ('asset-clip', 'A', '0s', '0s', '2400/2400s'),
        ('gap', 'G', '2400/2400s', '0s', '1200/2400s'),
        # B starts at 0s in source — can't go negative
        ('asset-clip', 'B', '3600/2400s', '0s', '2400/2400s'),
    ])
    try:
        spine = modifier._get_spine()
        gap = list(spine)[1]
        result = modifier._absorb_into_neighbor(spine, gap, 'next')
        assert result is not None
        # Start clamped to 0
        from fcpxml.models import TimeValue
        assert modifier._parse_time(result.get('start')) == TimeValue(0, 1)
        # available = neighbor_start (0s) → only extends by 0, so duration unchanged
        assert modifier._parse_time(result.get('duration')).to_seconds() == 1.0
    finally:
        Path(path).unlink(missing_ok=True)


def test_absorb_into_neighbor_no_neighbor_returns_none():
    """Returns None when no neighbor clip exists in given direction."""
    modifier, path = _make_absorb_modifier([
        ('gap', 'G', '0s', '0s', '600/2400s'),
    ])
    try:
        spine = modifier._get_spine()
        gap = list(spine)[0]
        assert modifier._absorb_into_neighbor(spine, gap, 'prev') is None
        assert modifier._absorb_into_neighbor(spine, gap, 'next') is None
    finally:
        Path(path).unlink(missing_ok=True)


# ============================================================
# _resolve_insert_position Tests
# ============================================================

def test_resolve_insert_position_start(temp_fcpxml):
    """'start' returns zero offset and index 0."""
    from fcpxml.models import TimeValue
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    offset, idx = modifier._resolve_insert_position('start', list(spine))
    assert offset == TimeValue(0, 1)
    assert idx == 0


def test_resolve_insert_position_end(temp_fcpxml):
    """'end' returns offset past last clip and index at end."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    children = list(spine)
    offset, idx = modifier._resolve_insert_position('end', children)
    assert idx == len(children)
    # Offset should equal last clip offset + last clip duration
    last = children[-1]
    expected = (modifier._parse_time(last.get('offset', '0s'))
                + modifier._parse_time(last.get('duration', '0s')))
    assert offset == expected


def test_resolve_insert_position_end_empty_spine():
    """'end' on empty spine returns zero offset and index 0."""
    from fcpxml.models import TimeValue
    xml_str = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
            <resources>
                <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>
            </resources>
            <library><event name="T"><project name="T">
                <sequence format="r1" duration="0s">
                    <spine/>
                </sequence>
            </project></event></library>
        </fcpxml>""")
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        offset, idx = modifier._resolve_insert_position('end', [])
        assert offset == TimeValue(0, 1)
        assert idx == 0
    Path(f.name).unlink(missing_ok=True)


def test_resolve_insert_position_after_clip():
    """'after:ClipName' resolves to offset+duration of named clip."""
    from fcpxml.models import TimeValue
    xml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.11">\n'
        '  <resources>\n'
        '    <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>\n'
        '  </resources>\n'
        '  <library><event name="T"><project name="T">\n'
        '    <sequence format="r1" duration="4800/2400s">\n'
        '      <spine>\n'
        '        <asset-clip ref="r1" offset="0s" name="UniqueA"'
        ' start="0s" duration="2400/2400s"/>\n'
        '        <asset-clip ref="r1" offset="2400/2400s" name="UniqueB"'
        ' start="0s" duration="2400/2400s"/>\n'
        '      </spine>\n'
        '    </sequence>\n'
        '  </project></event></library>\n'
        '</fcpxml>'
    )
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        spine = modifier._get_spine()
        children = list(spine)
        offset, idx = modifier._resolve_insert_position('after:UniqueA', children)
        assert offset == TimeValue(2400, 2400)
        assert idx == 1
    Path(f.name).unlink(missing_ok=True)


def test_resolve_insert_position_before_clip():
    """'before:ClipName' resolves to offset of named clip."""
    from fcpxml.models import TimeValue
    xml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.11">\n'
        '  <resources>\n'
        '    <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>\n'
        '  </resources>\n'
        '  <library><event name="T"><project name="T">\n'
        '    <sequence format="r1" duration="4800/2400s">\n'
        '      <spine>\n'
        '        <asset-clip ref="r1" offset="0s" name="UniqueX"'
        ' start="0s" duration="2400/2400s"/>\n'
        '        <asset-clip ref="r1" offset="2400/2400s" name="UniqueY"'
        ' start="0s" duration="2400/2400s"/>\n'
        '      </spine>\n'
        '    </sequence>\n'
        '  </project></event></library>\n'
        '</fcpxml>'
    )
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        spine = modifier._get_spine()
        children = list(spine)
        offset, idx = modifier._resolve_insert_position('before:UniqueY', children)
        assert offset == TimeValue(2400, 2400)
        assert idx == 1
    Path(f.name).unlink(missing_ok=True)


def test_resolve_insert_position_invalid_ref_raises(temp_fcpxml):
    """Invalid reference clip ID raises ValueError."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    with pytest.raises(ValueError, match="Reference clip not found"):
        modifier._resolve_insert_position('after:nonexistent_clip_xyz', list(spine))


def test_resolve_insert_position_timecode(temp_fcpxml):
    """Timecode string resolves to correct index via binary search."""
    modifier = FCPXMLModifier(temp_fcpxml)
    spine = modifier._get_spine()
    children = list(spine)
    # Insert at the offset of the second clip — should get index 1
    if len(children) >= 2:
        second_offset = children[1].get('offset', '0s')
        offset, idx = modifier._resolve_insert_position(second_offset, children)
        assert idx == 1


# ============================================================
# _make_transition_element Tests
# ============================================================

def test_make_transition_element_with_effect_ref(temp_fcpxml):
    """Creates transition with filter-video child when ref ID given."""
    from fcpxml.models import TimeValue
    modifier = FCPXMLModifier(temp_fcpxml)
    t = modifier._make_transition_element(
        'Cross Dissolve', TimeValue(0, 1), TimeValue(600, 2400), 'effect_1'
    )
    assert t.tag == 'transition'
    assert t.get('name') == 'Cross Dissolve'
    assert t.get('duration') == '600/2400s'
    fv = t.find('filter-video')
    assert fv is not None
    assert fv.get('ref') == 'effect_1'
    assert fv.get('name') == 'Cross Dissolve'


def test_make_transition_element_without_effect_ref(temp_fcpxml):
    """Creates transition without filter-video child when ref ID is None."""
    from fcpxml.models import TimeValue
    modifier = FCPXMLModifier(temp_fcpxml)
    t = modifier._make_transition_element(
        'Fade', TimeValue(0, 1), TimeValue(300, 2400), None
    )
    assert t.tag == 'transition'
    assert t.get('name') == 'Fade'
    assert t.find('filter-video') is None


# ============================================================
# _recalculate_offsets Tests
# ============================================================

def test_recalculate_offsets_sequential():
    """Offsets are recalculated sequentially based on durations."""
    from fcpxml.models import TimeValue
    xml_str = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
            <resources>
                <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>
            </resources>
            <library><event name="T"><project name="T">
                <sequence format="r1" duration="4800/2400s">
                    <spine>
                        <asset-clip ref="r1" offset="9999/2400s" name="A"
                                    start="0s" duration="2400/2400s"/>
                        <asset-clip ref="r1" offset="9999/2400s" name="B"
                                    start="0s" duration="1200/2400s"/>
                        <asset-clip ref="r1" offset="9999/2400s" name="C"
                                    start="0s" duration="1200/2400s"/>
                    </spine>
                </sequence>
            </project></event></library>
        </fcpxml>""")
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        spine = modifier._get_spine()
        modifier._recalculate_offsets(spine)
        children = list(spine)
        # A: offset=0, B: offset=2400/2400, C: offset=3600/2400
        assert modifier._parse_time(children[0].get('offset')) == TimeValue(0, 1)
        assert modifier._parse_time(children[1].get('offset')) == TimeValue(2400, 2400)
        assert modifier._parse_time(children[2].get('offset')) == TimeValue(3600, 2400)
    Path(f.name).unlink(missing_ok=True)


def test_recalculate_offsets_skips_non_spine_tags():
    """Non-spine tags (e.g. <note>) are left untouched."""
    import xml.etree.ElementTree as ET
    xml_str = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
            <resources>
                <format id="r1" frameDuration="100/2400s" width="1920" height="1080"/>
            </resources>
            <library><event name="T"><project name="T">
                <sequence format="r1" duration="2400/2400s">
                    <spine>
                        <asset-clip ref="r1" offset="0s" name="A"
                                    start="0s" duration="2400/2400s"/>
                    </spine>
                </sequence>
            </project></event></library>
        </fcpxml>""")
    with tempfile.NamedTemporaryFile(suffix='.fcpxml', mode='w', delete=False) as f:
        f.write(xml_str)
        f.flush()
        modifier = FCPXMLModifier(f.name)
        spine = modifier._get_spine()
        # Add a non-spine element
        note = ET.SubElement(spine, 'note')
        note.text = 'test'
        modifier._recalculate_offsets(spine)
        # Note should have no offset attribute
        assert note.get('offset') is None
    Path(f.name).unlink(missing_ok=True)
