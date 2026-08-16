"""Broadcast (NTSC-fractional) frame rates must survive every time path.

Issue #17. ``TimeValue`` built its denominators with ``int(fps)``, so 23.976
became 23 and every value parsed at a broadcast rate came back on the wrong
timebase — 3604s read as 3756.957s, a 152-second error on a value that is
exact on the page.

The three rates that matter are exact rationals, not decimals:

    23.976 -> 24000/1001      29.97 -> 30000/1001      59.94 -> 60000/1001

Every test here fails against the ``int(fps)`` implementation and passes
against the rational one. ``examples/sample.fcpxml`` is 24fps, which is why
the whole suite exercised only the integer path for five months.
"""

from fractions import Fraction

import pytest

from fcpxml.models import TimeValue
from fcpxml.rational import (
    fcp_frame_rate_name,
    frame_duration_attr,
    frame_duration_seconds,
    rational_fps,
)

# (nominal float as a caller would pass it, exact rational)
BROADCAST_RATES = [
    (23.976, Fraction(24000, 1001)),
    (23.98, Fraction(24000, 1001)),
    (29.97, Fraction(30000, 1001)),
    (59.94, Fraction(60000, 1001)),
    (47.952, Fraction(48000, 1001)),
    (119.88, Fraction(120000, 1001)),
]

INTEGER_RATES = [24, 25, 30, 50, 60, 120]

ALL_RATES = [23.976, 24, 25, 29.97, 30, 59.94]


class TestRationalFps:
    """A float frame rate resolves to the exact rate it is standing in for."""

    @pytest.mark.parametrize("nominal,exact", BROADCAST_RATES)
    def test_broadcast_rate_snaps_to_exact_rational(self, nominal, exact):
        assert rational_fps(nominal) == exact

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_integer_rate_is_left_alone(self, rate):
        assert rational_fps(rate) == Fraction(rate)
        assert rational_fps(float(rate)) == Fraction(rate)

    def test_24_does_not_collapse_to_23_976(self):
        """The snap tolerance must not swallow the neighbouring integer rate."""
        assert rational_fps(24.0) == Fraction(24)
        assert rational_fps(30.0) == Fraction(30)
        assert rational_fps(60.0) == Fraction(60)

    def test_exact_fraction_passes_through(self):
        assert rational_fps(Fraction(24000, 1001)) == Fraction(24000, 1001)

    def test_zero_and_negative_rejected(self):
        with pytest.raises(ValueError):
            rational_fps(0)
        with pytest.raises(ValueError):
            rational_fps(-24)

    @pytest.mark.parametrize("nominal,exact", BROADCAST_RATES)
    def test_frame_duration_is_the_reciprocal(self, nominal, exact):
        assert frame_duration_seconds(nominal) == 1 / exact

    def test_frame_duration_attr_matches_what_fcp_writes(self):
        assert frame_duration_attr(23.976) == "1001/24000s"
        assert frame_duration_attr(29.97) == "1001/30000s"
        assert frame_duration_attr(24) == "1/24s"
        assert frame_duration_attr(30) == "1/30s"

    def test_fcp_frame_rate_name_is_the_enumerated_string(self):
        """conform-rate srcFrameRate is an enum; int(23.976) -> "23" is invalid."""
        assert fcp_frame_rate_name(23.976) == "23.98"
        assert fcp_frame_rate_name(29.97) == "29.97"
        assert fcp_frame_rate_name(59.94) == "59.94"
        assert fcp_frame_rate_name(24) == "24"
        assert fcp_frame_rate_name(30) == "30"


class TestFromTimecodeSeconds:
    """The bug in the issue, stated as an assertion."""

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_plain_seconds_round_trip_exactly(self, fps):
        """An FCPXML seconds attribute is already exact. Do not quantise it."""
        assert TimeValue.from_timecode("3604s", fps).to_seconds() == 3604

    def test_the_reported_reproduction(self):
        tv = TimeValue.from_timecode("3604s", 23.976)
        assert tv.to_seconds() == pytest.approx(3604.0, abs=1e-9)
        # The old behaviour: 86410/23s = 3756.957s
        assert tv.denominator != 23

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_rational_attribute_is_preserved_verbatim(self, fps):
        tv = TimeValue.from_timecode("86496410/24000s", fps)
        assert tv.numerator == 86496410
        assert tv.denominator == 24000

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_fractional_seconds_stay_exact(self, fps):
        assert TimeValue.from_timecode("1.001s", fps).to_seconds() == pytest.approx(
            1.001, abs=1e-9
        )


class TestFromTimecodeFrames:
    """Frame counts land on the exact frame grid, not a truncated one."""

    def test_frames_at_23_976(self):
        # 24 frames at 24000/1001 fps is exactly 1.001 seconds
        tv = TimeValue.from_timecode("24f", 23.976)
        assert Fraction(tv.numerator, tv.denominator) == Fraction(1001, 1000)

    def test_frames_at_29_97(self):
        tv = TimeValue.from_timecode("30f", 29.97)
        assert Fraction(tv.numerator, tv.denominator) == Fraction(1001, 1000)

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_frames_at_integer_rates_unchanged(self, rate):
        tv = TimeValue.from_timecode(f"{rate}f", rate)
        assert tv.to_seconds() == 1.0


class TestFromTimecodeSmpte:
    """HH:MM:SS:FF is a frame count, so it needs the exact rate too."""

    def test_one_second_of_timecode_at_23_976(self):
        # Non-drop timecode counts 24 frames per labelled second at 23.98,
        # and 24 frames is 1.001 real seconds.
        tv = TimeValue.from_timecode("00:00:01:00", 23.976)
        assert Fraction(tv.numerator, tv.denominator) == Fraction(1001, 1000)

    def test_hour_of_timecode_at_23_976(self):
        tv = TimeValue.from_timecode("01:00:00:00", 23.976)
        assert Fraction(tv.numerator, tv.denominator) == Fraction(3600 * 1001, 1000)

    def test_hhmmss_without_frames_at_29_97(self):
        tv = TimeValue.from_timecode("00:00:10", 29.97)
        assert Fraction(tv.numerator, tv.denominator) == Fraction(300 * 1001, 30000)

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_smpte_at_integer_rates_unchanged(self, rate):
        tv = TimeValue.from_timecode("00:00:01:00", rate)
        assert tv.to_seconds() == 1.0


class TestFromSeconds:
    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_from_seconds_is_frame_aligned_and_close(self, fps):
        tv = TimeValue.from_seconds(3604.0, fps)
        assert tv.to_seconds() == pytest.approx(3604.0, abs=1.0 / 20)
        # And it lands on a whole frame of the exact grid.
        frames = Fraction(tv.numerator, tv.denominator) * rational_fps(fps)
        assert frames.denominator == 1


class TestToTimecode:
    """Rendering back out must use the nominal count, not a truncated rate."""

    def test_23_976_counts_24_frames_per_second(self):
        # 1.001s is exactly one second of 23.98 non-drop timecode.
        tv = TimeValue(1001, 1000)
        assert tv.to_timecode(23.976) == "00:00:01:00"

    def test_23_976_frame_field_reaches_23(self):
        """int(23.976) == 23 made frame 23 impossible to render."""
        tv = TimeValue(23 * 1001, 24000)
        assert tv.to_timecode(23.976) == "00:00:00:23"

    def test_29_97_counts_30_frames_per_second(self):
        tv = TimeValue(1001, 1000)
        assert tv.to_timecode(29.97) == "00:00:01:00"

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_integer_rates_unchanged(self, rate):
        assert TimeValue(1, 1).to_timecode(rate) == "00:00:01:00"

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_round_trip_through_timecode(self, fps):
        original = TimeValue.from_timecode("00:01:30:07", fps)
        assert original.to_timecode(fps) == "00:01:30:07"


class TestToFrames:
    def test_frames_at_23_976(self):
        assert TimeValue(1001, 1000).to_frames(23.976) == 24

    def test_frames_at_29_97(self):
        assert TimeValue(1001, 1000).to_frames(29.97) == 30

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_frames_at_integer_rates(self, rate):
        assert TimeValue(1, 1).to_frames(rate) == rate


class TestSnapToFrame:
    """2400 ticks per second has no whole number of ticks per 23.976 frame."""

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_snap_lands_on_a_whole_frame(self, fps):
        tv = TimeValue.from_timecode("12.3456789s", fps)
        snapped = tv.snap_to_frame(fps)
        frames = Fraction(snapped.numerator, snapped.denominator) * rational_fps(fps)
        assert frames.denominator == 1, f"{snapped!r} is not a whole frame at {fps}"

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_snap_moves_less_than_half_a_frame(self, fps):
        tv = TimeValue.from_timecode("12.3456789s", fps)
        snapped = tv.snap_to_frame(fps)
        drift = abs(snapped.to_seconds() - tv.to_seconds())
        assert drift <= float(frame_duration_seconds(fps)) / 2 + 1e-9

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_integer_rates_keep_the_2400_timebase(self, rate):
        """No regression: the tick timebase integer rates already used."""
        snapped = TimeValue.from_timecode("12.3456789s", rate).snap_to_frame(rate)
        assert snapped.denominator == 2400

    def test_snap_rejects_zero_fps(self):
        with pytest.raises(ValueError):
            TimeValue(1, 1).snap_to_frame(0)


class TestTimecodeAtBroadcastRates:
    """The legacy ``Timecode`` wrapper carried the same ``int(frame_rate)``."""

    def test_to_rational_at_23_976(self):
        from fcpxml.models import Timecode

        tc = Timecode(frames=24, frame_rate=23.976)
        # 24000 is a timebase FCP's DTD accepts; 1000 is not, so the value
        # stays in the format's own timebase rather than reducing.
        assert tc.to_rational() == "24024/24000s"
        assert "/23s" not in tc.to_rational()

    def test_to_time_value_at_23_976(self):
        from fcpxml.models import Timecode

        tv = Timecode(frames=24, frame_rate=23.976).to_time_value()
        assert Fraction(tv.numerator, tv.denominator) == Fraction(1001, 1000)

    def test_from_rational_at_23_976(self):
        from fcpxml.models import Timecode

        tc = Timecode.from_rational("1001/1000s", frame_rate=23.976)
        assert tc.frames == 24

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_integer_rates_unchanged(self, rate):
        from fcpxml.models import Timecode

        assert Timecode(frames=rate, frame_rate=rate).to_rational() == f"{rate}/{rate}s"


class TestWriterAtBroadcastRates:
    """The writer emitted ``1/23s`` one-frame durations and srcFrameRate="23"."""

    def test_generated_format_frame_duration(self):
        import xml.etree.ElementTree as ET

        from fcpxml.models import Project, Timecode, Timeline
        from fcpxml.writer import FCPXMLWriter

        project = Project(
            name="Broadcast",
            timelines=[
                Timeline(
                    name="Seq",
                    duration=Timecode(frames=240, frame_rate=23.976),
                    clips=[],
                    frame_rate=23.976,
                )
            ],
        )
        root = FCPXMLWriter()._build_fcpxml(project)
        fmt = root.find(".//format")
        assert fmt.get("frameDuration") == "1001/24000s"
        assert "1/23s" not in ET.tostring(root, encoding="unicode")

    @pytest.mark.parametrize("rate", INTEGER_RATES)
    def test_generated_format_at_integer_rates_unchanged(self, rate):
        from fcpxml.models import Project, Timecode, Timeline
        from fcpxml.writer import FCPXMLWriter

        project = Project(
            name="Integer",
            timelines=[
                Timeline(
                    name="Seq",
                    duration=Timecode(frames=rate, frame_rate=float(rate)),
                    clips=[],
                    frame_rate=float(rate),
                )
            ],
        )
        root = FCPXMLWriter()._build_fcpxml(project)
        assert root.find(".//format").get("frameDuration") == f"1/{rate}s"

    def test_marker_duration_on_a_23_98_timeline(self, tmp_path):
        """A one-frame marker on the real 23.98 fixture, not the 24fps one."""
        import shutil
        import xml.etree.ElementTree as ET

        from fcpxml.writer import FCPXMLModifier

        target = tmp_path / "music-video.fcpxml"
        shutil.copy("examples/music-video.fcpxml", target)
        mod = FCPXMLModifier(str(target))
        assert rational_fps(mod.fps) == Fraction(24000, 1001)

        clip_id = next(iter(mod.clips))
        marker = mod.add_marker(clip_id, "0s", "Beat")
        assert marker.get("duration") == "1001/24000s"
        assert "1/23s" not in ET.tostring(mod.root, encoding="unicode")

    def test_conform_rate_is_a_valid_enum_value(self, tmp_path):
        """srcFrameRate="23" is not in FCP's enumeration; "23.98" is."""
        import shutil

        from fcpxml.writer import FCPXMLModifier

        target = tmp_path / "music-video.fcpxml"
        shutil.copy("examples/music-video.fcpxml", target)
        mod = FCPXMLModifier(str(target))

        clip_id = next(iter(mod.clips))
        clip = mod.change_speed(clip_id, 2.0)
        conform = clip.find("conform-rate")
        assert conform is not None
        assert conform.get("srcFrameRate") == "23.98"

    def test_speed_change_stays_frame_aligned_at_23_98(self, tmp_path):
        """2400 // int(23.976) == 104 ticks per frame is not a frame at all."""
        import shutil

        from fcpxml.rational import parse_seconds
        from fcpxml.writer import FCPXMLModifier

        target = tmp_path / "music-video.fcpxml"
        shutil.copy("examples/music-video.fcpxml", target)
        mod = FCPXMLModifier(str(target))

        clip_id = next(iter(mod.clips))
        clip = mod.change_speed(clip_id, 2.0)
        seconds = parse_seconds(clip.get("duration"))
        frames = seconds * Fraction(24000, 1001)
        assert frames.denominator == 1, f"{clip.get('duration')} is not a whole frame"


class TestXmemlExportAtBroadcastRates:
    """XMEML names a broadcast rate as ``timebase`` + ``<ntsc>``, not 23."""

    def _export(self, tmp_path):
        import shutil
        import xml.etree.ElementTree as ET

        from fcpxml.export import DaVinciExporter

        source = tmp_path / "music-video.fcpxml"
        shutil.copy("examples/music-video.fcpxml", source)
        out = tmp_path / "out.xml"
        DaVinciExporter(str(source)).export_xmeml(str(out))
        return ET.parse(out).getroot()

    def test_sequence_rate_is_24_plus_ntsc(self, tmp_path):
        root = self._export(tmp_path)
        rate = root.find("./sequence/rate")
        assert rate.find("timebase").text == "24"
        assert rate.find("ntsc").text == "TRUE"

    def test_timecode_rate_matches_the_sequence(self, tmp_path):
        root = self._export(tmp_path)
        tc_rate = root.find("./sequence/timecode/rate")
        assert tc_rate.find("timebase").text == "24"
        assert tc_rate.find("ntsc").text == "TRUE"

    def test_no_timebase_23_anywhere(self, tmp_path):
        root = self._export(tmp_path)
        for timebase in root.iter("timebase"):
            assert timebase.text == "24"

    def test_clipitem_rates_carry_the_ntsc_flag(self, tmp_path):
        root = self._export(tmp_path)
        clipitems = list(root.iter("clipitem"))
        assert clipitems, "fixture produced no clipitems"
        for item in clipitems:
            rate = item.find("rate")
            assert rate.find("timebase").text == "24"
            assert rate.find("ntsc").text == "TRUE"

    def test_frame_counts_round_rather_than_truncate(self):
        from fcpxml.export import _to_frames

        # 2 seconds at 23.976 is 47.952 frames. int() said 47.
        assert _to_frames(2.0, 23.976) == 48
        assert _to_frames(1.0, 29.97) == 30
        assert _to_frames(2.0, 24) == 48


class TestFrameRateDisplay:
    """23.976023976023978fps is not a frame rate anyone writes down."""

    def test_analyze_timeline_prints_the_enum_name(self):
        import asyncio

        import server as server_module

        result = asyncio.run(
            server_module.call_tool(
                "inspect",
                {
                    "action": "analyze_timeline",
                    "args": {"filepath": "examples/music-video.fcpxml"},
                },
            )
        )
        text = result[0].text
        assert "23.98fps" in text
        assert "23.976023976" not in text

    def test_preview_render_prints_the_enum_name(self):
        from fcpxml.parser import FCPXMLParser
        from fcpxml.preview import render_timeline_html

        tl = FCPXMLParser().parse_file("examples/music-video.fcpxml").primary_timeline
        html = render_timeline_html(tl)
        assert "23.98fps" in html
        assert "23.976023976" not in html
