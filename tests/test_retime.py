"""Exact speed integration, source/time clocks, conservative edits and atomicity."""

import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from fcpxml.dtd import validate_against_dtd
from fcpxml.rational import parse_seconds
from fcpxml.retime import LinearTimeMap, RetimeEditor
from fcpxml.writer import FCPXMLModifier


@pytest.fixture
def make_editor(tmp_path):
    def make(spine=None, *, rate="1/30s", source_rate=None, asset_start="100s", asset_duration="30s", duration="12s", sequence_extra=""):
        source_rate = source_rate or rate
        if spine is None:
            spine = ('<asset-clip name="Same" ref="r2" offset="3600s" start="105s" duration="6s"/>'
                     '<asset-clip name="Same" ref="r2" offset="3606s" start="110s" duration="6s"/>')
        text = (f'<fcpxml version="1.14"><resources>'
                f'<format id="r1" frameDuration="{rate}" width="1280" height="720"/>'
                f'<format id="r3" frameDuration="{source_rate}" width="1280" height="720"/>'
                f'<asset id="r2" name="Source" start="{asset_start}" duration="{asset_duration}" format="r3" hasVideo="1" hasAudio="1">'
                '<media-rep kind="original-media" src="file:///synthetic.mov"/></asset>'
                '</resources><library><event name="Event"><project name="Project">'
                f'<sequence format="r1" duration="{duration}" tcStart="3600s"><spine>{spine}</spine>'
                f'{sequence_extra}</sequence></project></event></library></fcpxml>')
        path = tmp_path / "source.fcpxml"
        path.write_text(text)
        return RetimeEditor(FCPXMLModifier(str(path)))
    return make


def path_for(editor, index=0):
    return editor.list_speed_points()["clips"][index]["clip_path"]


def raw(editor):
    return ET.tostring(editor.modifier.root)


def curve(start_speed=1, end_speed=3, duration="3s", transition="linear"):
    return [{"time": "0s", "speed": start_speed, "transition": transition},
            {"time": duration, "speed": end_speed}]


def test_exact_ramp_and_source_offset_ripple(make_editor):
    editor = make_editor()
    result = editor.set_speed_curve(path_for(editor), curve())
    clips = editor.modifier.root.findall(".//spine/asset-clip")
    mapping = LinearTimeMap.from_element(clips[0].find("timeMap"))
    assert mapping.points[0] == (Fraction(105), Fraction(105))
    assert mapping.source_at(Fraction(213, 2)) == Fraction(429, 4)  # 105 + 1.5 + .75
    assert mapping.points[-1] == (Fraction(108), Fraction(111))
    assert len(mapping.points) == 91
    assert result["source_consumed"] == "6s"
    assert result["source_trim_change"] == "0s"
    assert result["duration_change"] == "-3s"
    assert clips[1].get("offset") == "3603s"
    assert editor.modifier.root.find(".//sequence").get("duration") == "9s"
    assert clips[0].get("start") == "105s"


def test_hold_segments_are_compact_with_outgoing_speed(make_editor):
    editor = make_editor()
    result = editor.set_speed_curve(path_for(editor), [
        {"frame": 0, "speed": 0.5, "transition": "hold"},
        {"frame": 60, "speed": 2, "transition": "hold"},
        {"frame": 120, "speed": 99},
    ], preserve_pitch=False, frame_sampling="nearest-neighbor")
    assert result["source_consumed"] == "5s"
    assert result["source_trim_change"] == "-1s"
    assert result["emitted_points"] == 3
    tm = editor.modifier.root.find(".//timeMap")
    assert tm.attrib == {"preservesPitch": "0", "frameSampling": "nearest-neighbor"}
    assert [p.get("value") for p in tm] == ["105s", "106s", "110s"]


def test_ntsc_frames_keep_exact_rationals(make_editor):
    editor = make_editor(rate="1001/30000s")
    result = editor.set_speed_curve(path_for(editor), [
        {"frame": 0, "speed": 0.5}, {"frame": 90, "speed": 1.5},
    ])
    assert result["duration"] == "3003/1000s"
    assert result["source_consumed"] == "3003/1000s"
    mapping = LinearTimeMap.from_element(editor.modifier.root.find(".//timeMap"))
    first_elapsed = Fraction(1001, 30000)
    expected = 105 + first_elapsed / 2 + first_elapsed**2 / (2 * Fraction(3003, 1000))
    assert mapping.points[1] == (105 + first_elapsed, expected)


def test_mapping_exact_inverse_and_endpoints():
    mapping = LinearTimeMap([(10, 100), (12, 101), (15, 107)])
    assert mapping.source_at(10) == 100
    assert mapping.source_at(15) == 107
    assert mapping.source_at(Fraction(13)) == 103
    assert mapping.local_at(Fraction(103)) == 13
    assert mapping.local_at(100) == 10
    assert mapping.local_at(107) == 15
    for local in (Fraction(9), Fraction(16)):
        with pytest.raises(ValueError, match="outside"):
            mapping.source_at(local)
    with pytest.raises(ValueError, match="outside"):
        mapping.local_at(108)


@pytest.mark.parametrize("body", [
    '<timept time="0s" value="0s" interp="linear"/>',
    '<timept time="0s" value="0s"/><timept time="1s" value="1s"/>',
    '<timept time="0s" value="0s" interp="smooth2"/><timept time="1s" value="1s" interp="linear"/>',
    '<timept time="0s" value="0s" interp="linear"/><timept time="0s" value="1s" interp="linear"/>',
    '<timept time="0s" value="1s" interp="linear"/><timept time="1s" value="0s" interp="linear"/>',
    '<timept time="0s" value="1s" interp="linear"/><timept time="1s" value="1s" interp="linear"/>',
    '<timept time="0s" value="0s" interp="linear" inTime="0s"/><timept time="1s" value="1s" interp="linear"/>',
    '<timept time="0s" interp="linear"/><timept time="1s" value="1s" interp="linear"/>',
])
def test_reject_ambiguous_native_map(body):
    with pytest.raises(ValueError):
        LinearTimeMap.from_element(ET.fromstring(f"<timeMap>{body}</timeMap>"))


@pytest.mark.parametrize("points", [
    [], [{"frame": 0, "speed": 1}],
    [{"frame": 1, "speed": 1}, {"frame": 30, "speed": 2}],
    [{"frame": 0, "speed": 1}, {"frame": 0, "speed": 2}],
    [{"frame": 0, "speed": 1}, {"frame": -1, "speed": 2}],
    [{"frame": 0, "speed": 1}, {"frame": True, "speed": 2}],
    [{"frame": 0, "speed": 1}, {"frame": 1.5, "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": "1/100s", "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": "1.0s", "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": "1/0s", "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": "-1s", "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": 1, "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": "1s", "frame": 30, "speed": 2}],
    [{"frame": 0, "speed": 1, "transition": "smooth"}, {"frame": 30, "speed": 2}],
    [{"frame": 0, "speed": 1, "unknown": 2}, {"frame": 30, "speed": 2}],
    [{"frame": 0, "speed": 1}, "bad"],
])
def test_invalid_points_are_atomic(make_editor, points):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_speed_curve(path_for(editor), points)
    assert raw(editor) == before


@pytest.mark.parametrize("speed", [None, 0, -1, 101, True, "2", float("nan"), float("inf"), 10**400])
def test_invalid_speeds_are_atomic(make_editor, speed):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_speed_curve(path_for(editor), curve(speed))
    assert raw(editor) == before


@pytest.mark.parametrize("options", [{"ripple": "true"}, {"preserve_pitch": 1}, {"frame_sampling": "guess"}, {"ripple": False}])
def test_bad_options_and_disabled_ripple_are_atomic(make_editor, options):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_speed_curve(path_for(editor), curve(), **options)
    assert raw(editor) == before


def test_equal_duration_nonripple_and_source_extension(make_editor):
    editor = make_editor()
    result = editor.set_speed_curve(path_for(editor), curve(2, 2, "6s"), ripple=False)
    assert result["source_consumed"] == "12s"
    assert result["source_trim_change"] == "6s"
    assert result["rippled_clips"] == 0
    assert editor.modifier.root.find(".//sequence").get("duration") == "12s"


def test_source_bounds_nonzero_asset_origin_are_enforced(make_editor):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError, match="asset bounds"):
        editor.set_speed_curve(path_for(editor), curve(10, 10, "3s"))
    assert raw(editor) == before
    invalid = make_editor(asset_start="106s")
    assert not invalid.list_speed_points()["clips"][0]["supported"]
    with pytest.raises(ValueError, match="asset bounds"):
        invalid.set_speed_curve(path_for(invalid), curve())


def test_point_limit_prevents_unbounded_baking(make_editor):
    editor = make_editor(asset_duration="10000s")
    before = raw(editor)
    with pytest.raises(ValueError, match="10000 emitted"):
        editor.set_speed_curve(path_for(editor), curve(1, 2, "400s"))
    assert raw(editor) == before
    with pytest.raises(ValueError, match="1000 points"):
        editor.set_speed_curve(path_for(editor), [{"frame": n, "speed": 1} for n in range(1001)])


def test_replace_existing_map_preserves_real_source_inpoint_and_not_old_local_clock(make_editor):
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="10s" duration="3s">'
                         '<timeMap><timept time="9s" value="103s" interp="linear"/>'
                         '<timept time="14s" value="113s" interp="linear"/></timeMap></asset-clip>', duration="3s")
    result = editor.set_speed_curve(path_for(editor), curve(1, 1, "3s"))
    assert result["source_start"] == "105s"
    assert result["source_end"] == "108s"
    assert result["source_trim_change"] == "-3s"
    assert editor.modifier.root.find(".//asset-clip").get("start") == "10s"
    assert len(editor.modifier.root.findall(".//timeMap")) == 1


def test_reset_restores_consumed_source_and_shifts_intrinsic_local_key_times(make_editor):
    spine = ('<asset-clip ref="r2" offset="0s" start="10s" duration="3s">'
             '<timeMap><timept time="10s" value="105s" interp="linear"/>'
             '<timept time="13s" value="111s" interp="linear"/></timeMap>'
             '<adjust-transform><param name="position"><keyframeAnimation>'
             '<keyframe time="10s" value="0 0" curve="linear"/>'
             '<keyframe time="15s" value="10 5" curve="linear"/>'
             '</keyframeAnimation></param></adjust-transform></asset-clip>')
    editor = make_editor(spine, duration="3s")
    result = editor.reset_speed(path_for(editor))
    clip = editor.modifier.root.find(".//asset-clip")
    assert result["changed"] is True
    assert result["duration"] == "6s"
    assert clip.get("start") == "105s"
    assert clip.find("timeMap") is None
    assert [p.get("time") for p in clip.iter("keyframe")] == ["105s", "110s"]
    assert editor.modifier.root.find(".//sequence").get("duration") == "6s"


def test_reset_fractional_consumption_reports_source_frame_snapping(make_editor):
    editor = make_editor()
    editor.set_speed_curve(path_for(editor), curve(1, 1.01, "1s"))
    result = editor.reset_speed(path_for(editor))
    assert result["duration"] == "1s"
    assert result["source_start_adjustment"] == "0s"
    assert result["source_end_adjustment"] == "-1/200s"
    assert result["warnings"]


def test_set_preserves_intrinsic_adjusted_clock_bytes_and_static_nodes(make_editor):
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="105s" duration="6s">'
                         '<adjust-transform><param name="position"><keyframeAnimation>'
                         '<keyframe time="105s" value="0 0" curve="linear"/>'
                         '<keyframe time="110s" value="10 5" curve="linear"/>'
                         '</keyframeAnimation></param></adjust-transform>'
                         '<adjust-colorConform enabled="1"/><metadata><md key="custom" value="retained"/></metadata>'
                         '</asset-clip>', duration="6s")
    clip = editor.modifier.root.find(".//asset-clip")
    original_children = [ET.tostring(child) for child in clip]
    editor.set_speed_curve(path_for(editor), curve(2, 2, "3s"))
    assert [ET.tostring(child) for child in clip if child.tag != "timeMap"] == original_children


@pytest.mark.parametrize("child", [
    '<asset-clip ref="r2" lane="1" offset="105s" start="105s" duration="1s"/>',
    '<marker start="106s" value="Marker"/>', '<filter-video ref="effect"/>',
    '<adjust-transform tracking="tracker"/>',
    '<adjust-volume><param name="amount"><fadeIn duration="1s"/></param></adjust-volume>',
    '<conform-rate scaleEnabled="1" srcFrameRate="30"/>',
])
def test_unsupported_targets_are_inspectable_but_uneditable(make_editor, child):
    editor = make_editor(f'<asset-clip ref="r2" offset="0s" start="105s" duration="6s">{child}</asset-clip>')
    report = editor.list_speed_points()["clips"][0]
    assert not report["supported"]
    assert report["unsupported_reasons"]
    before = raw(editor)
    with pytest.raises(ValueError, match="Unsupported speed target"):
        editor.set_speed_curve(report["clip_path"], curve())
    assert raw(editor) == before


def test_later_connected_clip_moves_with_its_host_without_changing_local_anchor(make_editor):
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="105s" duration="6s"/>'
                         '<asset-clip ref="r2" offset="6s" start="110s" duration="6s">'
                         '<asset-clip ref="r2" lane="1" offset="111s" start="105s" duration="1s"/>'
                         '</asset-clip>')
    editor.set_speed_curve(path_for(editor), curve())
    clips = editor.modifier.root.findall(".//spine/asset-clip")
    assert clips[1].get("offset") == "3s"
    assert clips[1].find("asset-clip").get("offset") == "111s"


def test_sequence_annotations_and_transitions_refuse_ripple_atomically(make_editor):
    editor = make_editor(sequence_extra='<marker start="8s" value="Marker"/>')
    before = raw(editor)
    with pytest.raises(ValueError, match="Sequence-level timed"):
        editor.set_speed_curve(path_for(editor), curve())
    assert raw(editor) == before
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="105s" duration="6s"/>'
                         '<transition offset="5s" duration="1s"/>')
    before = raw(editor)
    with pytest.raises(ValueError, match="Transitions"):
        editor.set_speed_curve(path_for(editor), curve())
    assert raw(editor) == before


def test_mismatched_rates_rejected_and_duplicate_names_targeted_by_path(make_editor):
    editor = make_editor(source_rate="1/24s")
    with pytest.raises(ValueError, match="frame rates differ"):
        editor.set_speed_curve(path_for(editor), curve())
    editor = make_editor()
    paths = [clip["clip_path"] for clip in editor.list_speed_points()["clips"]]
    assert paths[0] != paths[1]
    editor.set_speed_curve(paths[1], curve())
    clips = editor.modifier.root.findall(".//spine/asset-clip")
    assert clips[0].find("timeMap") is None
    assert clips[1].find("timeMap") is not None


def test_unsupported_map_raw_inspection_is_not_hidden(make_editor):
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="105s" duration="6s">'
                         '<timeMap><timept time="105s" value="105s" interp="smooth2"/>'
                         '<timept time="111s" value="111s" interp="smooth2"/></timeMap></asset-clip>')
    clip = editor.list_speed_points()["clips"][0]
    assert not clip["supported"]
    assert clip["has_time_map"]
    assert clip["raw_time_maps"][0]["points"][0]["interp"] == "smooth2"
    assert clip["speed_points"] == []


def test_apple_dtd_accepts_native_speed_output(make_editor, tmp_path):
    editor = make_editor()
    editor.set_speed_curve(path_for(editor), curve())
    output = tmp_path / "output.fcpxml"
    editor.modifier.save(str(output))
    valid, message = validate_against_dtd(output)
    if valid is None:
        pytest.skip(message)
    assert valid, message
    reloaded = RetimeEditor(FCPXMLModifier(str(output)))
    result = reloaded.list_speed_points()["clips"][0]
    assert result["supported"]
    assert len(result["speed_points"]) == 91
    assert parse_seconds(result["source_end"]) == 111


@pytest.mark.parametrize("points", [
    [{"frame": 0, "speed": 0.1234567890123456}, {"frame": 30, "speed": 1}],
    [{"frame": 0, "speed": 1}, {"frame": 10**100, "speed": 2}],
    [{"time": "0s", "speed": 1}, {"time": "100000000000000000000000000000000s", "speed": 2}],
])
def test_unrepresentable_exact_native_times_reject_atomically(make_editor, points):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError, match="64-bit numerator / 32-bit denominator"):
        editor.set_speed_curve(path_for(editor), points)
    assert raw(editor) == before


def test_invalid_reset_key_time_is_atomic(make_editor):
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="10s" duration="3s">'
                         '<timeMap><timept time="10s" value="105s" interp="linear"/>'
                         '<timept time="13s" value="111s" interp="linear"/></timeMap>'
                         '<adjust-transform><param name="position"><keyframeAnimation>'
                         '<keyframe time="invalid" value="0 0"/>'
                         '</keyframeAnimation></param></adjust-transform></asset-clip>')
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.reset_speed(path_for(editor))
    assert raw(editor) == before


def test_reset_normalized_native_export_uses_nearest_source_frames(make_editor):
    # Sanitized map from a real FCP 12.4 export of source 5..11 at 2x.
    # The app expands the map to asset boundaries and quantizes local ticks.
    editor = make_editor('<asset-clip ref="r2" offset="0s" start="5s" duration="3s">'
                         '<timeMap><timept time="60001/24000s" value="0s" interp="linear"/>'
                         '<timept time="204000/24000s" value="12s" interp="linear"/></timeMap>'
                         '</asset-clip>', asset_start="0s", asset_duration="12s", duration="3s")
    result = editor.reset_speed(path_for(editor))
    assert result["source_start"] == "5s"
    assert result["source_end"] == "11s"
    assert result["duration"] == "6s"
    assert result["warnings"]
    assert abs(parse_seconds(result["source_start_adjustment"])) < Fraction(1, 60)
    assert abs(parse_seconds(result["source_end_adjustment"])) < Fraction(1, 60)


def test_optical_flow_classic_schema_option_is_accepted(make_editor):
    editor = make_editor()
    result = editor.set_speed_curve(path_for(editor), curve(), frame_sampling="optical-flow-classic")
    assert result["frame_sampling"] == "optical-flow-classic"
