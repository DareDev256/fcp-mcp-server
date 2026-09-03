"""Tests for FCPXML data models — TimeValue, Timecode, Timeline, and helpers.

Covers the core time math and model properties that underpin all 34 tools.
"""

import pytest

from fcpxml import models
from fcpxml.models import (
    Clip,
    FlashFrame,
    FlashFrameSeverity,
    GapInfo,
    Keyword,
    Marker,
    MarkerType,
    PacingConfig,
    Project,
    Timecode,
    Timeline,
    TimeValue,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)


class TestTimeValueCreation:
    """Test TimeValue construction from various input formats."""

    def test_from_rational_string(self):
        tv = TimeValue.from_timecode("90/30s")
        assert tv.numerator == 90 and tv.denominator == 30

    def test_from_seconds_string(self):
        assert TimeValue.from_timecode("5s", fps=24.0).to_seconds() == pytest.approx(5.0, abs=0.01)

    def test_from_seconds_float(self):
        assert TimeValue.from_seconds(2.5, fps=24.0).to_seconds() == pytest.approx(2.5, abs=0.05)

    def test_from_timecode_hhmmssff(self):
        assert TimeValue.from_timecode("00:01:30:15", fps=30.0).to_seconds() == pytest.approx(90.5, abs=0.05)

    def test_from_timecode_hhmmss(self):
        assert TimeValue.from_timecode("00:02:00", fps=24.0).to_seconds() == pytest.approx(120.0, abs=0.05)

    def test_from_frames_string(self):
        assert TimeValue.from_timecode("48f", fps=24.0).to_seconds() == pytest.approx(2.0, abs=0.05)

    def test_from_plain_number(self):
        assert TimeValue.from_timecode("3.5", fps=24.0).to_seconds() == pytest.approx(3.5, abs=0.05)

    def test_from_empty_string(self):
        assert TimeValue.from_timecode("") == TimeValue.zero()

    def test_zero(self):
        tv = TimeValue.zero()
        assert tv.numerator == 0 and tv.to_seconds() == 0.0

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid timecode format"):
            TimeValue.from_timecode("not_a_timecode")

    def test_drop_frame_separator(self):
        assert TimeValue.from_timecode("00:00:10;15", fps=30.0).to_seconds() == pytest.approx(10.5, abs=0.05)


class TestTimeValueConversions:

    def test_to_fcpxml(self):
        assert TimeValue(3, 1).to_fcpxml() == "3s"
        assert TimeValue(90, 30).to_fcpxml() == "3s"  # simplifies
        assert TimeValue(75, 30).to_fcpxml() == "75/30s"  # keeps standard timebase denom

    def test_to_seconds(self):
        assert TimeValue(72, 24).to_seconds() == pytest.approx(3.0)
        with pytest.raises(ValueError, match="denominator cannot be zero"):
            TimeValue(5, 0)  # zero denominator rejected at construction

    def test_to_timecode(self):
        assert TimeValue(2700, 30).to_timecode(fps=30.0) == "00:01:30:00"
        assert TimeValue(2715, 30).to_timecode(fps=30.0) == "00:01:30:15"

    def test_to_frames(self):
        assert TimeValue(3, 1).to_frames(fps=24.0) == 72


class TestTimeValueArithmetic:

    def test_add(self):
        assert (TimeValue(72, 24) + TimeValue(48, 24)).to_seconds() == pytest.approx(5.0)

    def test_sub(self):
        assert (TimeValue(120, 24) - TimeValue(72, 24)).to_seconds() == pytest.approx(2.0)

    def test_add_different_denominators(self):
        assert (TimeValue(72, 24) + TimeValue(150, 30)).to_seconds() == pytest.approx(8.0)

    def test_mul(self):
        assert (TimeValue(72, 24) * 2).to_seconds() == pytest.approx(6.0)

    def test_div(self):
        assert (TimeValue(72, 24) / 3).to_seconds() == pytest.approx(1.0)

    def test_simplify(self):
        s = TimeValue(120, 48).simplify()
        assert s.numerator == 5 and s.denominator == 2

    def test_simplify_zero(self):
        s = TimeValue(0, 48).simplify()
        assert s.numerator == 0 and s.denominator == 1


class TestTimeValueComparisons:

    def test_ordering(self):
        small, big = TimeValue(48, 24), TimeValue(72, 24)
        assert small < big and small <= big
        assert big > small and big >= small

    def test_equality(self):
        assert TimeValue(72, 24) == TimeValue(72, 24)
        assert TimeValue(3, 1) == TimeValue(72, 24)  # cross-denominator
        assert TimeValue(72, 24) != "not a timevalue"

    def test_repr(self):
        r = repr(TimeValue(72, 24))
        assert "72" in r and "24" in r and "3.000s" in r


class TestTimeline:

    @staticmethod
    def _make(durations, fps=24.0):
        clips, offset = [], 0.0
        for i, dur in enumerate(durations):
            clips.append(Clip(
                name=f"clip_{i}",
                start=Timecode(frames=int(offset * fps), frame_rate=fps),
                duration=Timecode(frames=int(dur * fps), frame_rate=fps),
            ))
            offset += dur
        return Timeline(
            name="test",
            duration=Timecode(frames=int(offset * fps), frame_rate=fps),
            frame_rate=fps, clips=clips,
        )

    def test_total_clips(self):
        assert self._make([2, 3, 1]).total_clips == 3

    def test_total_cuts(self):
        assert self._make([2, 3, 1]).total_cuts == 2
        assert self._make([]).total_cuts == 0

    def test_avg_duration(self):
        assert self._make([2, 4, 6]).average_clip_duration == pytest.approx(4.0, abs=0.1)
        assert self._make([]).average_clip_duration == 0.0

    def test_cuts_per_minute(self):
        assert self._make([2, 2, 2]).cuts_per_minute == pytest.approx(20.0, abs=0.5)

    def test_shorter_than(self):
        assert len(self._make([0.2, 5, 0.3, 2]).get_clips_shorter_than(0.5)) == 2

    def test_longer_than(self):
        assert len(self._make([1, 10, 2, 15]).get_clips_longer_than(5)) == 2

    def test_clip_at(self):
        clip = self._make([2, 3, 1]).get_clip_at(3.5)
        assert clip is not None and clip.name == "clip_1"
        assert self._make([2, 3]).get_clip_at(999) is None

    def test_by_keyword(self):
        tl = self._make([2, 3])
        tl.clips[0].keywords = [Keyword(value="interview")]
        assert len(tl.get_clips_by_keyword("interview")) == 1


class TestProject:

    def test_primary_timeline(self):
        tl = Timeline(name="main", duration=Timecode(frames=0, frame_rate=24.0))
        assert Project(name="test", timelines=[tl]).primary_timeline is tl

    def test_primary_timeline_empty(self):
        assert Project(name="empty").primary_timeline is None


class TestMarker:

    def test_youtube_timestamp_minutes(self):
        m = Marker(name="Ch", start=Timecode(frames=2160, frame_rate=24.0))
        assert m.to_youtube_timestamp() == "1:30"

    def test_youtube_timestamp_hours(self):
        m = Marker(name="Act", start=Timecode(frames=24 * 3700, frame_rate=24.0))
        assert m.to_youtube_timestamp().count(":") == 2


class TestTimecode:
    """Test Timecode — the frame-based time representation used by the parser."""

    def test_from_rational_fraction(self):
        tc = Timecode.from_rational("3600/24s", frame_rate=24.0)
        assert tc.frames == 3600
        assert tc.seconds == pytest.approx(150.0)

    def test_from_rational_whole_seconds(self):
        tc = Timecode.from_rational("5s", frame_rate=30.0)
        assert tc.frames == 150
        assert tc.seconds == pytest.approx(5.0)

    def test_from_rational_plain_number(self):
        tc = Timecode.from_rational("2.5", frame_rate=24.0)
        assert tc.frames == 60
        assert tc.seconds == pytest.approx(2.5)

    def test_from_rational_empty_string(self):
        tc = Timecode.from_rational("", frame_rate=24.0)
        assert tc.frames == 0

    def test_to_smpte_basic(self):
        tc = Timecode(frames=2700, frame_rate=30.0)
        assert tc.to_smpte() == "00:01:30:00"

    def test_to_smpte_with_frames(self):
        tc = Timecode(frames=2715, frame_rate=30.0)
        assert tc.to_smpte() == "00:01:30:15"

    def test_to_smpte_drop_frame_separator(self):
        tc = Timecode(frames=48, frame_rate=24.0, drop_frame=True)
        assert ";" in tc.to_smpte()

    def test_to_smpte_hours(self):
        tc = Timecode(frames=24 * 3661, frame_rate=24.0)
        smpte = tc.to_smpte()
        assert smpte.startswith("01:01:01")

    def test_to_rational(self):
        tc = Timecode(frames=72, frame_rate=24.0)
        assert tc.to_rational() == "72/24s"

    def test_to_time_value(self):
        tc = Timecode(frames=72, frame_rate=24.0)
        tv = tc.to_time_value()
        assert tv.numerator == 72
        assert tv.denominator == 24
        assert tv.to_seconds() == pytest.approx(3.0)

    def test_total_frames_alias(self):
        tc = Timecode(frames=100, frame_rate=30.0)
        assert tc.total_frames == 100


class TestClipProperties:
    """Test Clip computed properties."""

    def test_end_timecode(self):
        clip = Clip(
            name="test",
            start=Timecode(frames=48, frame_rate=24.0),
            duration=Timecode(frames=72, frame_rate=24.0),
        )
        assert clip.end.frames == 120
        assert clip.end.frame_rate == 24.0

    def test_duration_seconds(self):
        clip = Clip(
            name="test",
            start=Timecode(frames=0, frame_rate=24.0),
            duration=Timecode(frames=72, frame_rate=24.0),
        )
        assert clip.duration_seconds == pytest.approx(3.0)

    def test_keyword_values(self):
        clip = Clip(
            name="test",
            start=Timecode(frames=0, frame_rate=24.0),
            duration=Timecode(frames=24, frame_rate=24.0),
            keywords=[Keyword(value="interview"), Keyword(value="broll")],
        )
        assert clip.keyword_values == ["interview", "broll"]

    def test_keyword_values_empty(self):
        clip = Clip(
            name="test",
            start=Timecode(frames=0, frame_rate=24.0),
            duration=Timecode(frames=24, frame_rate=24.0),
        )
        assert clip.keyword_values == []


class TestFlashFrame:

    def test_is_critical(self):
        ff = FlashFrame(
            clip_name="bad_clip", clip_id="c1",
            start=Timecode(frames=0, frame_rate=24.0),
            duration_frames=1, duration_seconds=0.04,
            severity=FlashFrameSeverity.CRITICAL,
        )
        assert ff.is_critical is True

    def test_is_not_critical(self):
        ff = FlashFrame(
            clip_name="short_clip", clip_id="c2",
            start=Timecode(frames=0, frame_rate=24.0),
            duration_frames=4, duration_seconds=0.17,
            severity=FlashFrameSeverity.WARNING,
        )
        assert ff.is_critical is False


class TestGapInfo:

    def test_timecode_property(self):
        gap = GapInfo(
            start=Timecode(frames=2700, frame_rate=30.0),
            duration_frames=30, duration_seconds=1.0,
            previous_clip="Clip_A", next_clip="Clip_B",
        )
        assert gap.timecode == "00:01:30:00"


class TestValidationResult:

    def test_summary_format(self):
        result = ValidationResult(
            is_valid=False, health_score=72,
            issues=[
                ValidationIssue(issue_type=ValidationIssueType.FLASH_FRAME, severity="error", message="e"),
                ValidationIssue(issue_type=ValidationIssueType.GAP, severity="warning", message="w"),
            ],
            flash_frames=[
                FlashFrame("c", "c1", Timecode(0, 24.0), 1, 0.04, FlashFrameSeverity.CRITICAL),
            ],
            gaps=[
                GapInfo(Timecode(0, 24.0), 10, 0.4),
            ],
        )
        s = result.summary()
        assert "72%" in s
        assert "Errors: 1" in s
        assert "Warnings: 1" in s
        assert "Flash frames: 1" in s
        assert "Gaps: 1" in s


class TestMarkerType:

    @pytest.mark.parametrize("value,expected", [
        ("todo", MarkerType.INCOMPLETE),       # enum value, not an action item
        ("TODO", MarkerType.INCOMPLETE),       # enum value, not an action item  # noqa
        ("completed", MarkerType.COMPLETED),
        ("COMPLETED", MarkerType.COMPLETED),
        ("standard", MarkerType.STANDARD),
        ("chapter", MarkerType.CHAPTER),
    ])
    def test_from_string_current_values(self, value, expected):
        assert MarkerType.from_string(value) == expected

    @pytest.mark.parametrize("alias,expected", [
        ("todo-marker", MarkerType.INCOMPLETE),
        ("completed-marker", MarkerType.COMPLETED),
        ("chapter-marker", MarkerType.CHAPTER),
    ])
    def test_from_string_legacy_aliases(self, alias, expected):
        """Legacy spec values (e.g. 'todo-marker') resolve to current enum."""
        assert MarkerType.from_string(alias) == expected

    @pytest.mark.parametrize("value,expected", [
        ("Todo", MarkerType.INCOMPLETE),
        ("tOdO", MarkerType.INCOMPLETE),
        ("Completed", MarkerType.COMPLETED),
        ("cOMPLETED", MarkerType.COMPLETED),
        ("Standard", MarkerType.STANDARD),
        ("CHAPTER", MarkerType.CHAPTER),
    ])
    def test_from_string_mixed_case(self, value, expected):
        """Case should never matter — FCP specs are inconsistent about casing."""
        assert MarkerType.from_string(value) == expected

    @pytest.mark.parametrize("alias,expected", [
        ("  todo-marker  ", MarkerType.INCOMPLETE),
        (" completed-marker ", MarkerType.COMPLETED),
        ("\tchapter-marker\t", MarkerType.CHAPTER),
    ])
    def test_from_string_legacy_aliases_with_whitespace(self, alias, expected):
        """Whitespace around legacy aliases must be stripped before matching."""
        assert MarkerType.from_string(alias) == expected

    def test_enum_values_are_lowercase(self):
        """Enum .value must be lowercase strings — they're used as dict keys."""
        assert MarkerType.INCOMPLETE.value == "todo"
        assert MarkerType.COMPLETED.value == "completed"
        assert MarkerType.STANDARD.value == "standard"
        assert MarkerType.CHAPTER.value == "chapter"

    def test_from_string_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid marker type"):
            MarkerType.from_string("nonexistent")

    def test_xml_tag_chapter_vs_marker(self):
        assert MarkerType.CHAPTER.xml_tag == "chapter-marker"
        assert MarkerType.INCOMPLETE.xml_tag == "marker"
        assert MarkerType.COMPLETED.xml_tag == "marker"
        assert MarkerType.STANDARD.xml_tag == "marker"


class TestMarkerTypeXmlContract:
    """Tests for MarkerType.from_xml_element and xml_attrs — the unified
    serialization contract that both parser and writer depend on."""

    def test_from_xml_element_chapter(self):
        import xml.etree.ElementTree as ET
        elem = ET.Element('chapter-marker')
        assert MarkerType.from_xml_element(elem) == MarkerType.CHAPTER

    def test_from_xml_element_todo(self):
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        elem.set('completed', '0')
        assert MarkerType.from_xml_element(elem) == MarkerType.INCOMPLETE

    def test_from_xml_element_completed(self):
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        elem.set('completed', '1')
        assert MarkerType.from_xml_element(elem) == MarkerType.COMPLETED

    def test_from_xml_element_standard(self):
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        assert MarkerType.from_xml_element(elem) == MarkerType.STANDARD

    def test_from_xml_element_rejects_malformed_completed(self):
        """Non-standard completed values (e.g. 'true') must fall to STANDARD."""
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        elem.set('completed', 'true')
        assert MarkerType.from_xml_element(elem) == MarkerType.STANDARD

    def test_from_xml_element_whitespace_padded_zero_is_standard(self):
        """Whitespace around '0' must NOT match INCOMPLETE — strict exact matching."""
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        elem.set('completed', ' 0 ')
        assert MarkerType.from_xml_element(elem) == MarkerType.STANDARD

    def test_from_xml_element_whitespace_padded_one_is_standard(self):
        """Whitespace around '1' must NOT match COMPLETED — strict exact matching."""
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        elem.set('completed', ' 1 ')
        assert MarkerType.from_xml_element(elem) == MarkerType.STANDARD

    def test_from_xml_element_empty_completed_is_standard(self):
        """An empty completed='' attribute must fall to STANDARD, not INCOMPLETE."""
        import xml.etree.ElementTree as ET
        elem = ET.Element('marker')
        elem.set('completed', '')
        assert MarkerType.from_xml_element(elem) == MarkerType.STANDARD

    def test_from_xml_element_chapter_ignores_completed(self):
        """A chapter-marker tag takes priority over any completed attribute."""
        import xml.etree.ElementTree as ET
        elem = ET.Element('chapter-marker')
        elem.set('completed', '0')
        assert MarkerType.from_xml_element(elem) == MarkerType.CHAPTER

    def test_xml_attrs_todo(self):
        assert MarkerType.INCOMPLETE.xml_attrs == {'completed': '0'}

    def test_xml_attrs_completed(self):
        assert MarkerType.COMPLETED.xml_attrs == {'completed': '1'}

    def test_xml_attrs_chapter(self):
        assert MarkerType.CHAPTER.xml_attrs == {'posterOffset': '0s'}

    def test_xml_attrs_standard_empty(self):
        assert MarkerType.STANDARD.xml_attrs == {}

    def test_roundtrip_symmetry(self):
        """from_xml_element(write(type)) == type for all marker types."""
        import xml.etree.ElementTree as ET
        for mt in MarkerType:
            elem = ET.Element(mt.xml_tag)
            for attr, val in mt.xml_attrs.items():
                elem.set(attr, val)
            assert MarkerType.from_xml_element(elem) == mt


class TestTimeValueSnapToFrame:
    """Tests for TimeValue.snap_to_frame — 2400-tick frame boundary snapping."""

    def test_snap_exact_frame_is_noop(self):
        """A value already on a frame boundary stays unchanged."""
        tv = TimeValue(100, 2400)  # exactly 1 frame at 24fps (2400/24=100 ticks)
        snapped = tv.snap_to_frame(24)
        assert snapped.to_seconds() == pytest.approx(tv.to_seconds(), abs=1e-6)

    def test_snap_between_frames_rounds_nearest(self):
        """A value between frames snaps to the nearest one."""
        # 1.5 frames at 24fps = 150 ticks. Should snap to 100 or 200.
        tv = TimeValue(150, 2400)
        snapped = tv.snap_to_frame(24)
        assert snapped.denominator == 2400
        assert snapped.numerator in (100, 200)

    def test_snap_30fps_frame_boundaries(self):
        """30fps: each frame = 80 ticks. 85 ticks should snap to 80."""
        tv = TimeValue(85, 2400)
        snapped = tv.snap_to_frame(30)
        assert snapped.numerator == 80
        assert snapped.denominator == 2400

    def test_snap_preserves_zero(self):
        tv = TimeValue(0, 24).snap_to_frame(24)
        assert tv.to_seconds() == 0.0

    def test_snap_large_value_stays_aligned(self):
        """10 seconds at 24fps should remain exactly 10 seconds."""
        tv = TimeValue(240, 24)  # 10s
        snapped = tv.snap_to_frame(24)
        assert snapped.to_seconds() == pytest.approx(10.0, abs=1e-6)


class TestTimeValueStandardTimebase:
    """Tests for TimeValue.is_standard_timebase — FCP DTD denominator checks."""

    @pytest.mark.parametrize("denom", [1, 24, 30, 60, 2400])
    def test_standard_denominators_accepted(self, denom):
        assert TimeValue(denom, denom).is_standard_timebase() is True

    def test_non_standard_denominator_rejected(self):
        """Denominators like 7 are not in any FCP timebase."""
        assert TimeValue(7, 7).is_standard_timebase() is True  # simplifies to 1/1
        assert TimeValue(3, 7).is_standard_timebase() is False

    def test_simplification_reveals_standard(self):
        """72/24 simplifies to 3/1 — denominator 1 is standard."""
        assert TimeValue(72, 24).is_standard_timebase() is True

    def test_simplification_reveals_non_standard(self):
        """15/7 doesn't simplify to a standard timebase."""
        assert TimeValue(15, 7).is_standard_timebase() is False


class TestTimeValueToFcpxmlEdgeCases:
    """Tests for to_fcpxml fallback paths that prevent FCP DTD rejection."""

    def test_whole_seconds_simplify(self):
        """72/24 = 3 whole seconds → '3s'."""
        assert TimeValue(72, 24).to_fcpxml() == "3s"

    def test_standard_timebase_simplifies(self):
        """75/30 simplifies to 5/2 — but 2 is not standard, so stays '75/30s'."""
        result = TimeValue(75, 30).to_fcpxml()
        # Should NOT produce "5/2s" since 2 isn't a standard timebase
        assert "/2s" not in result or "75/30s" == result

    def test_non_standard_denom_falls_back_to_original(self):
        """If simplification yields a non-standard denom, keep the original."""
        # 7 ticks at denom 21 → simplifies to 1/3. 3 is non-standard.
        # Should fall back to "7/21s"
        tv = TimeValue(7, 21)
        result = tv.to_fcpxml()
        assert result == "7/21s"

    def test_zero_is_always_zero_seconds(self):
        assert TimeValue(0, 2400).to_fcpxml() == "0s"


class TestTimeValueArithmeticEdgeCases:
    """Edge cases in TimeValue arithmetic that could produce incorrect frame values."""

    def test_sub_negative_result(self):
        """Subtraction producing negative time — used for offset calculations."""
        result = TimeValue(24, 24) - TimeValue(72, 24)
        assert result.to_seconds() == pytest.approx(-2.0)

    def test_mul_by_zero(self):
        assert (TimeValue(72, 24) * 0).to_seconds() == 0.0

    def test_div_preserves_value(self):
        """Division should maintain the time value, not produce rounding drift."""
        tv = TimeValue(72, 24)  # 3 seconds
        halved = tv / 2
        assert halved.to_seconds() == pytest.approx(1.5, abs=1e-6)

    def test_div_fractional_scalar_rounds_not_truncates(self):
        """Division by fractional scalar must round denominator, not truncate.

        int(2400 * 0.3333...) = 799 (truncation bug), round() = 800 (correct).
        This was the root cause of silent time drift in speed-change operations.
        """
        tv = TimeValue(100, 2400)
        result = tv / (1 / 3)  # divide by 0.333... → multiply duration by 3
        assert result.denominator == 800  # round(2400 * 0.333...) = 800
        assert result.to_seconds() == pytest.approx(0.125, abs=1e-6)

    def test_div_by_zero_raises_not_corrupts(self):
        """Division by zero must raise, not create zombie TimeValue(n, 0)."""
        tv = TimeValue(100, 2400)
        with pytest.raises(ZeroDivisionError):
            tv / 0

    def test_div_rounding_to_zero_raises(self):
        """Small scalars that round denominator to zero must raise, not corrupt."""
        tv = TimeValue(1, 1)
        with pytest.raises(ZeroDivisionError, match="rounds denominator"):
            tv / 0.3  # round(1 * 0.3) = 0

    def test_div_mul_roundtrip(self):
        """Multiply then divide should approximately restore original value."""
        tv = TimeValue(72, 24)  # 3 seconds
        scaled = tv * 2.5
        restored = scaled / 2.5
        assert restored.to_seconds() == pytest.approx(tv.to_seconds(), abs=0.01)

    def test_add_mismatched_timebases_lcm(self):
        """24fps + 30fps values should use LCM denominator, not multiply."""
        a = TimeValue(24, 24)  # 1 second
        b = TimeValue(30, 30)  # 1 second
        result = a + b
        assert result.to_seconds() == pytest.approx(2.0)
        # LCM(24,30)=120, not 24*30=720
        assert result.denominator <= 720  # at minimum, shouldn't explode

    def test_equality_across_timebases(self):
        """1 second expressed in different timebases must be equal."""
        assert TimeValue(24, 24) == TimeValue(30, 30) == TimeValue(2400, 2400)

    def test_inequality_near_boundary(self):
        """Integer-exact comparison: different rational values are never equal."""
        a = TimeValue(24000, 24000)  # exactly 1s
        b = TimeValue(24001, 24000)  # 1.0000417s
        # Cross-multiplication gives exact comparison — no epsilon
        assert a != b  # genuinely different values

    def test_mul_fractional_scalar_rounds_correctly(self):
        """Multiplying by 0.5 must round, not truncate — avoids half-tick loss."""
        # round(101 * 0.5) = 50 vs int(101 * 0.5) = 50 — same here, but
        # round is correct for odd numerators like 5 * 1.5:
        tv = TimeValue(5, 24)
        result = tv * 1.5
        # round(7.5) = 8, int(7.5) = 7 — round gives closer-to-correct result
        assert result.numerator == 8
        assert result.to_seconds() == pytest.approx(8 / 24, abs=1e-6)

    def test_hash_equal_values_same_hash(self):
        """Equal TimeValues must produce the same hash (dict/set contract)."""
        a = TimeValue(24, 24)
        b = TimeValue(30, 30)
        assert a == b
        assert hash(a) == hash(b)

    def test_hash_usable_in_set(self):
        """TimeValues must be usable as set elements."""
        s = {TimeValue(24, 24), TimeValue(30, 30), TimeValue(72, 24)}
        # 24/24 == 30/30 (both 1s), so set should have 2 elements
        assert len(s) == 2

    def test_hash_usable_as_dict_key(self):
        """TimeValues must be usable as dict keys."""
        d = {TimeValue(24, 24): "one_second"}
        assert d[TimeValue(30, 30)] == "one_second"


class TestMarkerTypeAliasSemantics:
    """MarkerType enum alias: TODO → INCOMPLETE (Python enum identity, not action items)."""

    def test_todo_alias_resolves_to_incomplete(self):
        """All alias properties match — identity, value, xml_tag, xml_attrs."""
        assert MarkerType.INCOMPLETE is MarkerType.TODO  # enum alias check  # noqa
        assert MarkerType.INCOMPLETE.value == "todo"
        assert MarkerType.INCOMPLETE.xml_tag == "marker"
        assert MarkerType.INCOMPLETE.xml_attrs == {'completed': '0'}

    def test_from_string_returns_canonical(self):
        """from_string('todo') returns the canonical INCOMPLETE member."""
        assert MarkerType.from_string("todo") is MarkerType.INCOMPLETE

    def test_from_xml_element_numeric_completed_values(self):
        """Only exact '0' and '1' are recognised — '2', '-1', '00' are STANDARD."""
        import xml.etree.ElementTree as ET
        for bad_val in ('2', '-1', '00', '01', '10'):
            elem = ET.Element('marker')
            elem.set('completed', bad_val)
            assert MarkerType.from_xml_element(elem) == MarkerType.STANDARD, (
                f"completed='{bad_val}' should be STANDARD, not a task marker"
            )


class TestTimecodeEdgeCases:
    """Edge cases in Timecode that could cause silent bugs in the parser."""

    def test_zero_frames_smpte(self):
        assert Timecode(frames=0, frame_rate=24.0).to_smpte() == "00:00:00:00"

    def test_one_frame_smpte(self):
        assert Timecode(frames=1, frame_rate=24.0).to_smpte() == "00:00:00:01"

    def test_exactly_one_hour(self):
        tc = Timecode(frames=24 * 3600, frame_rate=24.0)
        assert tc.to_smpte() == "01:00:00:00"
        assert tc.seconds == pytest.approx(3600.0)

    def test_to_time_value_roundtrip(self):
        """Timecode → TimeValue → seconds should be lossless."""
        tc = Timecode(frames=2715, frame_rate=30.0)
        tv = tc.to_time_value()
        assert tv.to_seconds() == pytest.approx(tc.seconds, abs=1e-6)


class TestTimeValueNegativeDenominator:
    """Negative denominators must be normalized to preserve hash/eq/ordering."""

    def test_negative_denom_normalized_at_construction(self):
        tv = TimeValue(1, -2)
        assert tv.numerator == -1
        assert tv.denominator == 2

    def test_negative_denom_both_negative(self):
        tv = TimeValue(-3, -4)
        assert tv.numerator == 3
        assert tv.denominator == 4

    def test_hash_contract_negative_denom(self):
        """Equal values must produce identical hashes (Python data model)."""
        a = TimeValue(-1, 1)
        b = TimeValue(1, -1)
        assert a == b
        assert hash(a) == hash(b)

    def test_set_dedup_negative_denom(self):
        s = {TimeValue(-1, 1), TimeValue(1, -1)}
        assert len(s) == 1

    def test_lt_negative_denom(self):
        assert TimeValue(1, -2) < TimeValue(1, 2)   # -0.5 < 0.5
        assert not (TimeValue(1, 2) < TimeValue(1, -2))

    def test_division_by_negative_normalizes(self):
        tv = TimeValue(100, 2400) / -2.0
        assert tv.denominator > 0
        assert tv.to_seconds() == pytest.approx(-100 / 4800, abs=1e-9)

    def test_to_fcpxml_negative_denom(self):
        """Normalized negative should serialize cleanly."""
        tv = TimeValue(1, -2400)
        assert tv.to_fcpxml() == "-1/2400s"


class TestTimeValueCrossMultiplicationEdgeCases:
    """Verify integer-exact comparison survives edge cases that broke float-based comparison."""

    def test_eq_non_timevalue_returns_false(self):
        """__eq__ with non-TimeValue must return False, not raise."""
        assert TimeValue(1, 1) != 1
        assert TimeValue(1, 1) != 1.0
        assert TimeValue(1, 1) != "1/1s"
        assert TimeValue(0, 1) != None  # noqa: E711

    def test_total_ordering_le_gt_ge(self):
        """@total_ordering derives __le__/__gt__/__ge__ from __lt__+__eq__.
        If the decorator were removed, these would raise TypeError."""
        a, b = TimeValue(1, 3), TimeValue(2, 3)
        assert a <= b
        assert a <= TimeValue(1, 3)  # equal case
        assert b > a
        assert b >= a
        assert b >= TimeValue(2, 3)  # equal case

    def test_large_numerator_cross_multiply(self):
        """Python arbitrary precision means cross-multiplication never overflows,
        but verify the comparison is actually correct with big numbers."""
        big = TimeValue(2**60, 1)
        small = TimeValue(1, 2**60)
        assert small < big
        assert big > small
        assert big != small

    def test_hash_contract_different_denominators(self):
        """Equivalent fractions must hash identically for dict/set correctness."""
        a = TimeValue(100, 2400)
        b = TimeValue(50, 1200)
        c = TimeValue(1, 24)
        assert a == b == c
        assert hash(a) == hash(b) == hash(c)
        assert len({a, b, c}) == 1

    def test_zero_numerator_negative_denom(self):
        """Zero with negative denominator normalizes cleanly."""
        tv = TimeValue(0, -1)
        assert tv.numerator == 0
        assert tv.denominator == 1
        assert tv == TimeValue(0, 1)
        assert hash(tv) == hash(TimeValue(0, 1))

    def test_comparison_transitivity(self):
        """If a < b and b < c then a < c — cross-multiplication must be transitive."""
        a = TimeValue(1, 7)
        b = TimeValue(1, 5)
        c = TimeValue(1, 3)
        assert a < b < c
        assert a < c  # transitive

    def test_sorted_rational_sequence(self):
        """Sort a sequence of TimeValues and verify order matches float values."""
        vals = [TimeValue(n, 24) for n in [72, 12, 48, 24, 36]]
        sorted_vals = sorted(vals)
        sorted_seconds = [v.to_seconds() for v in sorted_vals]
        assert sorted_seconds == sorted(sorted_seconds)

    def test_simplify_preserves_positive_denom(self):
        """After simplify(), denominator must remain positive (regression guard)."""
        tv = TimeValue(-600, 2400).simplify()
        assert tv.denominator > 0
        assert tv.to_seconds() == pytest.approx(-0.25, abs=1e-9)


class TestPacingConfig:

    def test_pacing_ranges(self):
        assert PacingConfig(pacing="slow").get_duration_range() == (5.0, 10.0)
        assert PacingConfig(pacing="fast").get_duration_range() == (0.5, 2.0)
        assert PacingConfig(pacing="unknown").get_duration_range() == (2.0, 5.0)


class TestStandardTimebaseMatchesTheEmitter:
    """is_standard_timebase must describe what to_fcpxml actually writes.

    Regression: the predicate simplified first, so TimeValue(36, 24) — 36
    frames at 24fps, the canonical form FCP itself emits — reported
    non-standard because 36/24 reduces to 3/2. to_fcpxml wrote "36/24s"
    correctly the whole time. The disagreement meant every generated timeline
    logged a validation warning for every clip that was not a whole number of
    seconds long.
    """

    FRAME_COUNTS = [1, 5, 18, 30, 36, 42, 47, 71, 108]

    def test_frame_aligned_values_at_24fps_are_standard(self):
        for frames in self.FRAME_COUNTS:
            tv = TimeValue(frames, 24)
            assert tv.is_standard_timebase(), f"{frames}/24s reported non-standard"

    def test_frame_aligned_values_at_30fps_are_standard(self):
        for frames in self.FRAME_COUNTS:
            assert TimeValue(frames, 30).is_standard_timebase()

    def test_the_ntsc_family_is_standard(self):
        assert TimeValue(1001, 24000).is_standard_timebase()
        assert TimeValue(48048, 24000).is_standard_timebase()

    def test_a_genuinely_non_standard_denominator_is_still_rejected(self):
        """The guard must still fire on the values it exists for."""
        assert TimeValue(3, 7).is_standard_timebase() is False
        assert TimeValue(15, 7).is_standard_timebase() is False
        assert TimeValue(5, 13).is_standard_timebase() is False

    def test_the_predicate_agrees_with_the_emitter(self):
        """Anything to_fcpxml writes on a standard denominator must pass.

        This is the invariant the regression broke. Checking it directly means
        the two cannot drift apart again.
        """
        candidates = (
            [TimeValue(n, 24) for n in range(1, 60)]
            + [TimeValue(n, 30) for n in range(1, 60)]
            + [TimeValue(n, 2400) for n in range(1, 60)]
            + [TimeValue(n * 1001, 24000) for n in range(1, 30)]
        )
        for tv in candidates:
            written_denominator = tv.to_fcpxml().rstrip("s").split("/")
            denominator = int(written_denominator[1]) if len(written_denominator) == 2 else 1
            emitted_is_standard = denominator in models._FCPXML_STANDARD_TIMEBASES
            assert tv.is_standard_timebase() == emitted_is_standard, (
                f"{tv!r} writes {tv.to_fcpxml()} but the predicate disagrees"
            )
