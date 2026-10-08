"""Native keyframe semantics and preservation, plus Apple's optional real DTDs."""

import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from fcpxml.dtd import validate_against_dtd
from fcpxml.keyframes import KEYFRAME_CURVE_OPTIONS, KeyframeEditor
from fcpxml.rational import parse_seconds
from fcpxml.writer import FCPXMLModifier


@pytest.fixture
def make_editor(tmp_path):
    def make(spine=None, *, rate="1/24s", source_rate=None, version="1.14", audio="1", video="1"):
        if spine is None:
            spine = '<asset-clip name="Same" ref="r2" offset="0s" start="100s" duration="2s"/>'
        source_rate = source_rate or rate
        text = (
            f'<fcpxml version="{version}"><resources>'
            f'<format id="r1" frameDuration="{rate}" width="1920" height="1080"/>'
            f'<format id="r3" frameDuration="{source_rate}" width="1920" height="1080"/>'
            f'<asset id="r2" name="Source" start="0s" duration="200s" format="r3" hasVideo="{video}" hasAudio="{audio}">'
            '<media-rep kind="original-media" src="file:///source.mov"/></asset>'
            '</resources><library><event name="Event"><project name="Project">'
            '<sequence format="r1" duration="10s" tcStart="0s"><spine>'
            f'{spine}</spine></sequence></project></event></library></fcpxml>'
        )
        path = tmp_path / "source.fcpxml"
        path.write_text(text)
        return KeyframeEditor(FCPXMLModifier(str(path)))
    return make


def path_for(editor, index=0):
    return editor.list_keyframes()["clips"][index]["clip_path"]


def raw(editor):
    return ET.tostring(editor.modifier.root)


def test_paths_uniquely_target_duplicate_names(make_editor):
    editor = make_editor(
        '<asset-clip name="Same" ref="r2" start="100s" offset="0s" duration="2s"/>'
        '<asset-clip name="Same" ref="r2" start="102s" offset="2s" duration="2s"/>'
    )
    clips = editor.list_keyframes()["clips"]
    assert clips[0]["clip_path"] != clips[1]["clip_path"]
    editor.set_keyframes(clips[1]["clip_path"], "opacity", [{"time": "0s", "value": 0.5}])
    assert not editor.list_keyframes(clips[0]["clip_path"])["clips"][0]["properties"]["opacity"]["keyframes"]
    assert editor.modifier.root.findall(".//project/sequence/spine/asset-clip")[1].find(".//keyframe").get("time") == "102s"
    with pytest.raises(ValueError, match="exactly match"):
        editor.set_keyframes("Same", "opacity", [{"time": "0s", "value": 1}])


@pytest.mark.parametrize("prop,value,expected", [
    ("position", [12, -3], "12 -3"), ("scale", 1.2, "1.2 1.2"),
    ("scale", [1.2, 0.8], "1.2 0.8"), ("rotation", -15, "-15"),
    ("opacity", 0.4, "0.4"), ("volume", -12, "-12dB"),
])
def test_native_values_and_source_clock(make_editor, prop, value, expected):
    editor = make_editor()
    result = editor.set_keyframes(path_for(editor), prop, [{"time": "1s", "value": value}])
    point = editor.modifier.root.find(".//keyframe")
    expected_attributes = {"time": "101s", "value": expected}
    if prop in ("position", "scale", "opacity"):
        expected_attributes["curve"] = "linear"
    assert point.attrib == expected_attributes
    assert result["keyframes"][0]["time"] == "1s"


def test_exact_fractional_frame_conversion(make_editor):
    editor = make_editor(rate="1001/30000s")
    result = editor.set_keyframes(path_for(editor), "position", [{"frame": 29, "value": [1, 2]}])
    point = result["keyframes"][0]
    assert point["time"] == "29029/30000s"
    assert parse_seconds(point["source_time"]) == 100 + Fraction(29029, 30000)


@pytest.mark.parametrize("points", [
    [{"time": "-1s", "value": 0.5}], [{"time": "2s", "value": 0.5}],
    [{"time": "1/0s", "value": 0.5}], [{"time": "1.0s", "value": 0.5}],
    [{"time": "0s", "frame": 0, "value": 0.5}], [{"frame": 0.5, "value": 0.5}],
    [{"frame": True, "value": 0.5}], [{"frame": -1, "value": 0.5}],
    [{"time": "0s", "value": float("nan")}], [{"time": "0s", "value": float("inf")}],
    [{"time": "0s", "value": True}], [{"time": "0s", "value": "0.5"}],
    [{"time": "0s", "value": 1.1}], [{"time": "0s", "value": 0.5, "interp": "bezier"}],
    [{"time": "0s", "value": 0.5, "curve": "bezier"}],
    [{"time": "1s", "value": 0.5}, {"time": "24/24s", "value": 0.6}],
    [{"time": "0s", "value": 0.5}, {"time": "1s", "value": 2}], [],
])
def test_invalid_input_has_no_partial_mutation(make_editor, points):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_keyframes(path_for(editor), "opacity", points)
    assert raw(editor) == before


def test_merge_preserves_other_parameters_and_existing_point_attributes(make_editor):
    editor = make_editor(
        '<asset-clip name="A" ref="r2" start="100s" offset="0s" duration="2s">'
        '<adjust-transform position="7 8" rotation="17">'
        '<param name="anchor" value="3 4"/>'
        '<param name="position" enabled="1" value="7 8">'
        '<fadeIn duration="1/24s"/>'
        '<keyframeAnimation>'
        '<keyframe time="99s" value="0 0" interp="ease" auxValue="kept"/>'
        '<keyframe time="100s" value="1 2" interp="easeOut" curve="smooth" auxValue="handle"/>'
        '<keyframe time="103s" value="8 9"/>'
        '</keyframeAnimation></param></adjust-transform>'
        '<metadata><md key="custom" value="untouched"/></metadata></asset-clip>'
    )
    result = editor.set_keyframes(path_for(editor), "position", [{"time": "0s", "value": [3, 4]}, {"time": "1s", "value": [5, 6]}])
    points = result["keyframes"]
    assert [point["time"] for point in points] == ["-1s", "0s", "1s", "3s"]
    assert [point["in_range"] for point in points] == [False, True, True, False]
    assert points[1]["attributes"]["auxValue"] == "handle"
    assert points[1]["interp"] == "easeOut"
    assert points[1]["curve"] == "smooth"
    adjustment = editor.modifier.root.find(".//adjust-transform")
    assert adjustment.attrib == {"position": "7 8", "rotation": "17"}
    assert adjustment.find("param[@name='anchor']").get("value") == "3 4"
    assert adjustment.find("param[@name='position']/fadeIn").get("duration") == "1/24s"
    assert editor.modifier.root.find(".//md").get("value") == "untouched"


def test_replace_and_delete_preserve_static_and_other_curves(make_editor):
    editor = make_editor()
    path = path_for(editor)
    editor.set_keyframes(path, "position", [{"time": "0s", "value": [0, 0]}, {"time": "1s", "value": [1, 2]}])
    editor.set_keyframes(path, "rotation", [{"time": "0s", "value": 15}])
    result = editor.set_keyframes(path, "position", [{"time": "1s", "value": [3, 4]}], mode="replace")
    assert len(result["keyframes"]) == 1
    result = editor.delete_keyframes(path, "position", ["24/24s"])
    assert result["removed"] == 1
    assert not result["keyframes"]
    assert editor.modifier.root.find(".//param[@name='rotation']/keyframeAnimation") is not None
    assert editor.delete_keyframes(path, "rotation")["removed"] == 1
    assert editor.delete_keyframes(path, "rotation")["removed"] == 0


def test_delete_selected_and_invalid_times_are_atomic(make_editor):
    editor = make_editor()
    path = path_for(editor)
    editor.set_keyframes(path, "volume", [{"time": "0s", "value": -12}, {"time": "1s", "value": -6}])
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.delete_keyframes(path, "volume", ["0s", "2s"])
    assert raw(editor) == before
    result = editor.delete_keyframes(path, "volume", ["0s"])
    assert result["removed"] == 1
    assert result["keyframes"][0]["value"] == -6


def test_connected_gap_uses_clip_source_clock_not_host_clock(make_editor):
    editor = make_editor(
        '<gap name="Host" offset="0s" start="3600s" duration="10s">'
        '<asset-clip name="A" ref="r2" lane="1" offset="3603s" start="100s" duration="2s"/>'
        '</gap>'
    )
    result = editor.set_keyframes(path_for(editor), "rotation", [{"time": "1s", "value": 30}])
    assert result["keyframes"][0]["source_time"] == "101s"


@pytest.mark.parametrize("child,expected", [
    ('<timeMap><timept time="0s" value="0s"/></timeMap>', "timeMap/conform-rate"),
    ('<conform-rate scaleEnabled="1"/>', "timeMap/conform-rate"),
])
def test_retime_is_explicitly_rejected(make_editor, child, expected):
    editor = make_editor(f'<asset-clip ref="r2" start="0s" duration="2s">{child}</asset-clip>')
    before = raw(editor)
    with pytest.raises(ValueError, match=expected):
        editor.set_keyframes(path_for(editor), "volume", [{"time": "0s", "value": -6}])
    assert raw(editor) == before


def test_implicit_frame_rate_conversion_is_rejected(make_editor):
    editor = make_editor(rate="1/24s", source_rate="1/25s")
    info = editor.list_keyframes()["clips"][0]
    assert not info["supported"]
    assert "frame rates differ" in " ".join(info["unsupported_reasons"])
    with pytest.raises(ValueError, match="frame rates differ"):
        editor.set_keyframes(path_for(editor), "opacity", [{"time": "0s", "value": 1}])


@pytest.mark.parametrize("tag", ["ref-clip", "mc-clip", "sync-clip", "title", "clip"])
def test_unsupported_types_remain_discoverable(make_editor, tag):
    editor = make_editor(f'<{tag} ref="r2" name="Unsupported" start="0s" duration="2s"/>')
    info = editor.list_keyframes()["clips"][0]
    assert info["tag"] == tag
    assert not info["supported"]
    with pytest.raises(ValueError, match="unsupported"):
        editor.set_keyframes(info["clip_path"], "opacity", [{"time": "0s", "value": 1}])


def test_nested_and_retimed_host_are_rejected(make_editor):
    editor = make_editor(
        '<asset-clip ref="r2" start="0s" duration="10s"><timeMap/>'
        '<audio ref="r2" lane="-1" start="100s" offset="0s" duration="2s"/>'
        '</asset-clip>'
    )
    assert not editor.list_keyframes()["clips"][1]["supported"]
    editor = make_editor('<clip duration="5s"><video ref="r2" start="0s" duration="2s"/></clip>')
    assert not editor.list_keyframes()["clips"][1]["supported"]


def test_audio_video_property_capabilities(make_editor):
    editor = make_editor('<audio ref="r2" start="0s" duration="2s"/>', video="0")
    properties = editor.list_keyframes()["clips"][0]["properties"]
    assert properties["volume"]["supported"]
    assert not properties["opacity"]["supported"]
    editor.set_keyframes(path_for(editor), "volume", [{"time": "1/48000s", "value": -6}])
    editor = make_editor('<video ref="r2" start="0s" duration="2s"/>')
    assert not editor.list_keyframes()["clips"][0]["properties"]["volume"]["supported"]


def test_duplicate_existing_curve_and_nested_components_are_rejected(make_editor):
    for params in (
        '<param name="position"/><param name="position"/>',
        '<param name="position"><param name="x"/></param>',
        '<param name="position"><keyframeAnimation><keyframe time="0s" value="0 0"/><keyframe time="0/24s" value="1 1"/></keyframeAnimation></param>',
    ):
        editor = make_editor(f'<asset-clip ref="r2" start="0s" duration="2s"><adjust-transform>{params}</adjust-transform></asset-clip>')
        before = raw(editor)
        assert not editor.list_keyframes()["clips"][0]["properties"]["position"]["supported"]
        with pytest.raises(ValueError):
            editor.set_keyframes(path_for(editor), "position", [{"time": "0s", "value": [2, 3]}])
        assert raw(editor) == before


@pytest.mark.parametrize("prop,tag,name,value", [
    ("position", "adjust-transform", "position", [1, 2]),
    ("scale", "adjust-transform", "scale", [1.1, 1.2]),
    ("rotation", "adjust-transform", "rotation", 15),
    ("opacity", "adjust-blend", "amount", 0.5),
    ("volume", "adjust-volume", "amount", -12),
])
def test_intrinsic_matching_ignores_key_and_preserves_it(make_editor, prop, tag, name, value):
    editor = make_editor(
        f'<asset-clip ref="r2" start="0s" duration="2s"><{tag}>'
        f'<param key="localized/123" name="{name}"/></{tag}></asset-clip>'
    )
    editor.set_keyframes(path_for(editor), prop, [{"time": "0s", "value": value}])
    params = editor.modifier.root.findall(f".//{tag}/param")
    assert len(params) == 1
    assert params[0].get("name") == name
    assert params[0].get("key") == "localized/123"
    assert len(params[0].findall("keyframeAnimation/keyframe")) == 1


@pytest.mark.parametrize("existing_name", ["位置", "Position"])
def test_intrinsic_name_is_case_sensitive_and_key_never_substitutes(make_editor, existing_name):
    editor = make_editor(
        '<asset-clip ref="r2" start="0s" duration="2s"><adjust-transform>'
        f'<param key="position" name="{existing_name}" value="preserved"/>'
        '</adjust-transform></asset-clip>'
    )
    editor.set_keyframes(path_for(editor), "position", [{"time": "0s", "value": [1, 2]}])
    params = editor.modifier.root.findall(".//adjust-transform/param")
    assert len(params) == 2
    assert params[0].attrib == {"key": "position", "name": existing_name, "value": "preserved"}
    assert params[0].find("keyframeAnimation") is None
    assert params[1].get("name") == "position"


@pytest.mark.parametrize("version", ["1.10", "1.14"])
def test_five_properties_validate_against_real_apple_dtd(make_editor, tmp_path, version):
    editor = make_editor(version=version)
    path = path_for(editor)
    for prop, value in [("volume", -12), ("opacity", 0.5), ("rotation", 10), ("scale", [1.1, 1.2]), ("position", [3, -4])]:
        editor.set_keyframes(path, prop, [{"time": "0s", "value": value}, {"time": "1s", "value": value}])
    destination = str(tmp_path / f"keyframes-{version}.fcpxml")
    editor.modifier.save(destination)
    ok, detail = validate_against_dtd(destination)
    if ok is None:
        pytest.skip(detail)
    assert ok is True, detail
    reparsed = KeyframeEditor(FCPXMLModifier(destination))
    for prop in editor.list_keyframes()["properties"]:
        assert reparsed.list_keyframes()["clips"][0]["properties"][prop]["keyframes"] == editor.list_keyframes()["clips"][0]["properties"][prop]["keyframes"]


def test_modern_intrinsic_and_video_param_ordering_with_real_dtd(make_editor, tmp_path):
    editor = make_editor(
        '<video ref="r2" start="0s" offset="0s" duration="2s">'
        '<param name="existing" value="keep"/><note>keep</note>'
        '<adjust-cinematic enabled="0"/><adjust-colorConform peakNitsOfPQSource="1000" peakNitsOfSDRToPQSource="100"/>'
        '<adjust-stereo-3D enabled="0"/><reserved>keep</reserved></video>'
    )
    editor.set_keyframes(path_for(editor), "position", [{"time": "0s", "value": [1, 2]}])
    editor.set_keyframes(path_for(editor), "opacity", [{"time": "0s", "value": 0.5}])
    tags = [child.tag for child in editor.modifier.root.find(".//spine/video")]
    assert tags == ["param", "note", "adjust-transform", "adjust-blend", "adjust-cinematic", "adjust-colorConform", "adjust-stereo-3D", "reserved"]
    destination = str(tmp_path / "modern.fcpxml")
    editor.modifier.save(destination)
    ok, detail = validate_against_dtd(destination)
    if ok is None:
        pytest.skip(detail)
    assert ok is True, detail


@pytest.mark.parametrize("value", [0, -1, [1, 0], [-1, 1], [True, 1], [1], "1 1"])
def test_invalid_scale_is_atomic(make_editor, value):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_keyframes(path_for(editor), "scale", [{"time": "0s", "value": value}])
    assert raw(editor) == before


def test_audio_volume_dtd_and_static_after_clear(make_editor, tmp_path):
    editor = make_editor('<audio ref="r2" start="0s" offset="0s" duration="2s"><adjust-volume amount="-3dB"/></audio>', video="0")
    path = path_for(editor)
    editor.set_keyframes(path, "volume", [{"time": "0s", "value": -12}, {"time": "1s", "value": -6}])
    destination = str(tmp_path / "audio.fcpxml")
    editor.modifier.save(destination)
    ok, detail = validate_against_dtd(destination)
    if ok is not None:
        assert ok is True, detail
    editor.delete_keyframes(path, "volume")
    assert editor.modifier.root.find(".//adjust-volume").get("amount") == "-3dB"
    assert editor.modifier.root.find(".//keyframeAnimation") is None


def test_single_property_failure_does_not_hide_other_capabilities(make_editor):
    editor = make_editor('<asset-clip ref="r2" start="0s" duration="2s"><adjust-transform tracking="tracker"/></asset-clip>')
    clip = editor.list_keyframes()["clips"][0]
    assert clip["supported"]
    assert not clip["properties"]["position"]["supported"]
    assert clip["properties"]["volume"]["supported"]


def test_invalid_mode_property_and_delete_duplicates(make_editor):
    editor = make_editor()
    path = path_for(editor)
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_keyframes(path, "opacity", [{"time": "0s", "value": 0.5}], mode="unknown")
    with pytest.raises(ValueError):
        editor.set_keyframes(path, [], [{"time": "0s", "value": 0.5}])
    with pytest.raises(ValueError):
        editor.delete_keyframes(path, "opacity", ["1s", "24/24s"])
    assert raw(editor) == before


# Minimal native clip from a synthetic FCP 12.4 import/re-export on 2026-10-07.
# This captures FCP's parameter-specific interpolation serialization; DTD alone
# would accept the interp attributes that FCP actually warned about and removed.
NATIVE_FCP_12_4_CLIP = """
<asset-clip ref="r2" offset="0s" name="Synthetic Only" start="5s" duration="6s">
  <adjust-transform>
    <param name="position"><keyframeAnimation>
      <keyframe time="5s" value="0 0" curve="linear"/>
      <keyframe time="10s" value="10 5" curve="linear"/>
    </keyframeAnimation></param>
    <param name="rotation"><keyframeAnimation>
      <keyframe time="5s" value="0"/>
      <keyframe time="10s" value="15"/>
    </keyframeAnimation></param>
    <param name="scale"><keyframeAnimation>
      <keyframe time="5s" value="1 1" curve="linear"/>
      <keyframe time="10s" value="1.2 1.2" curve="linear"/>
    </keyframeAnimation></param>
  </adjust-transform>
  <adjust-blend><param name="amount"><keyframeAnimation>
    <keyframe time="5s" value="0.25" curve="linear"/>
    <keyframe time="10s" value="1" curve="linear"/>
  </keyframeAnimation></param></adjust-blend>
  <adjust-colorConform enabled="1" autoOrManual="manual" conformType="conformNone" peakNitsOfPQSource="1000" peakNitsOfSDRToPQSource="203"/>
  <adjust-volume><param name="amount"><keyframeAnimation>
    <keyframe time="5s" value="-24dB"/>
    <keyframe time="10s" value="0dB"/>
  </keyframeAnimation></param></adjust-volume>
</asset-clip>
"""


@pytest.mark.parametrize("prop,value", [
    ("position", [5, 2.5]), ("scale", [1.1, 1.1]), ("rotation", 7.5),
    ("opacity", 0.625), ("volume", -12),
])
def test_edit_fcp_native_curve_preserves_serialization_and_color_conform(make_editor, prop, value):
    editor = make_editor(NATIVE_FCP_12_4_CLIP, rate="1/30s")
    path = path_for(editor)
    before = editor.list_keyframes(path)["clips"][0]["properties"]
    assert [p["time"] for p in before[prop]["keyframes"]] == ["0s", "5s"]
    color = ET.tostring(editor.modifier.root.find(".//adjust-colorConform"))
    result = editor.set_keyframes(path, prop, [{"time": "5/2s", "value": value, "interp": "linear"}])
    assert [p["time"] for p in result["keyframes"]] == ["0s", "5/2s", "5s"]
    assert all(p["interp"] is None for p in result["keyframes"])
    expected_curve = "linear" if prop in ("position", "scale", "opacity") else None
    assert all(p["curve"] == expected_curve for p in result["keyframes"])
    assert ET.tostring(editor.modifier.root.find(".//adjust-colorConform")) == color
    after = editor.list_keyframes(path)["clips"][0]["properties"]
    for other in set(before) - {prop}:
        assert before[other] == after[other]


@pytest.mark.parametrize("field", ["interp", "curve"])
@pytest.mark.parametrize("prop,expected_curve", [
    ("position", "linear"), ("scale", "linear"), ("rotation", None), ("opacity", "linear"), ("volume", None),
])
def test_explicit_linear_normalizes_existing_attributes(make_editor, prop, expected_curve, field):
    tag, name, value = {
        "position": ("adjust-transform", "position", "0 0"),
        "scale": ("adjust-transform", "scale", "1 1"),
        "rotation": ("adjust-transform", "rotation", "0"),
        "opacity": ("adjust-blend", "amount", "0.5"),
        "volume": ("adjust-volume", "amount", "-6dB"),
    }[prop]
    editor = make_editor(
        f'<asset-clip ref="r2" start="0s" duration="2s"><{tag}><param name="{name}">'
        f'<keyframeAnimation><keyframe time="0s" value="{value}" interp="ease" curve="smooth" auxValue="preserved"/>'
        f'</keyframeAnimation></param></{tag}></asset-clip>'
    )
    value = [1, 2] if prop == "position" else 0.5
    point = editor.set_keyframes(path_for(editor), prop, [{"time": "0s", "value": value, field: "linear"}])["keyframes"][0]
    assert point["interp"] is None
    assert point["curve"] == expected_curve
    assert point["attributes"]["auxValue"] == "preserved"


@pytest.mark.parametrize("prop,mode", [
    (prop, mode) for prop, options in KEYFRAME_CURVE_OPTIONS.items() for mode in options["curve"]
])
def test_every_accepted_curve_mode_is_written_on_a_new_point(make_editor, prop, mode):
    # Guards the writer against a curve mode added to the options table alone.
    editor = make_editor()
    value = [1, 2] if prop == "position" else 0.5
    point = editor.set_keyframes(path_for(editor), prop, [{"time": "0s", "value": value, "curve": mode}])["keyframes"][0]
    # FCP 12.4 exports no curve attribute on rotation or volume.
    assert point["curve"] == (mode if prop in ("position", "scale", "opacity") else None)


@pytest.mark.parametrize("disabled_element", ["adjustment", "param", "both"])
def test_disabled_target_is_inspectable_but_not_editable(make_editor, disabled_element):
    adjust_enabled = ' enabled="0"' if disabled_element in ("adjustment", "both") else ''
    param_enabled = ' enabled="0"' if disabled_element in ("param", "both") else ''
    editor = make_editor(
        '<asset-clip ref="r2" start="0s" duration="2s">'
        f'<adjust-transform{adjust_enabled}><param name="position"{param_enabled}>'
        '<keyframeAnimation><keyframe time="0s" value="1 2" curve="linear"/></keyframeAnimation>'
        '</param></adjust-transform></asset-clip>'
    )
    path = path_for(editor)
    before = raw(editor)
    info = editor.list_keyframes(path)["clips"][0]["properties"]
    assert not info["position"]["supported"]
    assert any("disabled" in reason for reason in info["position"]["unsupported_reasons"])
    assert info["position"]["keyframes"][0]["value"] == [1, 2]
    assert info["volume"]["supported"]
    assert info["rotation"]["supported"] == (disabled_element == "param")
    with pytest.raises(ValueError, match="disabled"):
        editor.set_keyframes(path, "position", [{"time": "0s", "value": [3, 4]}])
    with pytest.raises(ValueError, match="disabled"):
        editor.delete_keyframes(path, "position")
    assert raw(editor) == before


def test_disabled_non_target_parameter_does_not_block_edit(make_editor):
    editor = make_editor(
        '<asset-clip ref="r2" start="0s" duration="2s"><adjust-transform>'
        '<param name="rotation" enabled="0" value="15"/>'
        '</adjust-transform></asset-clip>'
    )
    editor.set_keyframes(path_for(editor), "position", [{"time": "0s", "value": [3, 4]}])
    rotation = editor.modifier.root.find(".//param[@name='rotation']")
    assert rotation.attrib == {"name": "rotation", "enabled": "0", "value": "15"}


def test_param_static_value_overrides_adjustment_and_survives_curve_deletion(make_editor):
    editor = make_editor(
        '<asset-clip ref="r2" start="0s" duration="2s">'
        '<adjust-blend amount="0.5"><param name="amount" value="0.8">'
        '<keyframeAnimation><keyframe time="0s" value="0.2" curve="linear"/></keyframeAnimation>'
        '</param></adjust-blend></asset-clip>'
    )
    path = path_for(editor)
    info = editor.list_keyframes(path)["clips"][0]["properties"]["opacity"]
    assert info["static_value"] == "0.8"
    assert info["keyframes"][0]["value"] == 0.2
    editor.delete_keyframes(path, "opacity")
    info = editor.list_keyframes(path)["clips"][0]["properties"]["opacity"]
    assert info["static_value"] == "0.8"
    assert not info["keyframes"]
    adjustment = editor.modifier.root.find(".//adjust-blend")
    assert adjustment.get("amount") == "0.5"
    assert adjustment.find("param").get("value") == "0.8"


LINEAR_RETIME_CLIP = '''
<asset-clip ref="r2" start="4s" offset="0s" duration="6s">
  <timeMap preservesPitch="1">
    <timept time="4s" value="5s" interp="linear"/>
    <timept time="6s" value="7s" interp="linear"/>
    <timept time="8s" value="11s" interp="linear"/>
    <timept time="10s" value="12s" interp="linear"/>
  </timeMap>
</asset-clip>
'''


@pytest.mark.parametrize("prop,values", [
    ("position", [[0, 0], [10, 5], [-3, 4]]),
    ("scale", [1, 1.2, 0.8]), ("rotation", [0, 15, -30]),
    ("opacity", [0, 0.5, 1]), ("volume", [-24, -6, 0]),
])
def test_retimed_keyframes_keep_local_output_clock_and_report_source_mapping(make_editor, prop, values):
    editor = make_editor(LINEAR_RETIME_CLIP, rate="1/30s")
    clip = editor.list_keyframes()["clips"][0]
    assert clip["supported"]
    assert clip["time_mapping"] == "linear"
    path = clip["clip_path"]
    curve = editor.modifier.root.find(".//timeMap")
    untouched_map = ET.tostring(curve)
    result = editor.set_keyframes(path, prop, [
        {"time": "0s", "value": values[0]},
        {"frame": 75, "value": values[1]},
        {"time": "5s", "value": values[2]},
    ])
    assert [point["local_time"] for point in result["keyframes"]] == ["4s", "13/2s", "9s"]
    assert [point["source_time"] for point in result["keyframes"]] == ["4s", "13/2s", "9s"]
    assert [point["mapped_source_time"] for point in result["keyframes"]] == ["5s", "8s", "23/2s"]
    assert [point["time"] for point in result["keyframes"]] == ["0s", "5/2s", "5s"]
    assert all(point["in_range"] for point in result["keyframes"])
    assert editor.list_keyframes(path)["clips"][0]["properties"][prop]["keyframes"] == result["keyframes"]
    updated = editor.set_keyframes(path, prop, [{"time": "75/30s", "value": values[2]}])
    assert len(updated["keyframes"]) == 3
    deleted = editor.delete_keyframes(path, prop, ["5/2s"])
    assert deleted["removed"] == 1
    assert [point["local_time"] for point in deleted["keyframes"]] == ["4s", "9s"]
    assert ET.tostring(curve) == untouched_map


def test_retimed_ntsc_frame_conversion_is_exact(make_editor):
    editor = make_editor(
        '<asset-clip ref="r2" start="4s" duration="6s"><timeMap>'
        '<timept time="4s" value="5s" interp="linear"/>'
        '<timept time="10s" value="17s" interp="linear"/>'
        '</timeMap></asset-clip>', rate="1001/30000s",
    )
    result = editor.set_keyframes(path_for(editor), "position", [{"frame": 29, "value": [1, 2]}])
    point = result["keyframes"][0]
    assert point["time"] == "29029/30000s"
    assert parse_seconds(point["local_time"]) == 4 + Fraction(29029, 30000)
    assert parse_seconds(point["mapped_source_time"]) == 5 + Fraction(29029, 15000)
    assert editor.delete_keyframes(path_for(editor), "position", [point["time"]])["removed"] == 1


def test_retained_local_keys_without_map_coverage_have_no_invented_source_time(make_editor):
    editor = make_editor(LINEAR_RETIME_CLIP.replace('</timeMap>', '''</timeMap>
      <adjust-blend><param name="amount"><keyframeAnimation>
        <keyframe time="3s" value="0" curve="linear"/>
        <keyframe time="7s" value="0.5" curve="linear" auxValue="keep"/>
        <keyframe time="13s" value="1" curve="linear"/>
      </keyframeAnimation></param></adjust-blend>'''))
    path = path_for(editor)
    points = editor.list_keyframes(path)["clips"][0]["properties"]["opacity"]["keyframes"]
    assert [point["time"] for point in points] == ["-1s", "3s", "9s"]
    assert [point["mapped_source_time"] for point in points] == [None, "9s", None]
    assert [point["in_range"] for point in points] == [False, True, False]
    assert [point["local_time"] for point in points] == ["3s", "7s", "13s"]
    result = editor.set_keyframes(path, "opacity", [{"time": "3s", "value": 0.7}])
    assert len(result["keyframes"]) == 3
    assert result["keyframes"][1]["attributes"]["auxValue"] == "keep"
    result = editor.delete_keyframes(path, "opacity", ["3s"])
    assert [point["local_time"] for point in result["keyframes"]] == ["3s", "13s"]
    assert editor.delete_keyframes(path, "opacity")["removed"] == 2


def test_keys_outside_visible_trim_but_inside_map_keep_exact_relative_time(make_editor):
    editor = make_editor(LINEAR_RETIME_CLIP.replace('start="4s" offset="0s" duration="6s"', 'start="6s" offset="0s" duration="2s"').replace('</timeMap>', '''</timeMap>
      <adjust-volume><param name="amount"><keyframeAnimation>
        <keyframe time="5s" value="-24dB"/>
        <keyframe time="7s" value="-12dB"/>
        <keyframe time="12s" value="0dB"/>
      </keyframeAnimation></param></adjust-volume>'''))
    points = editor.list_keyframes()["clips"][0]["properties"]["volume"]["keyframes"]
    assert [point["time"] for point in points] == ["-1s", "1s", "6s"]
    assert [point["mapped_source_time"] for point in points] == ["6s", "9s", None]
    assert [point["in_range"] for point in points] == [False, True, False]


@pytest.mark.parametrize("time_map", [
    '<timeMap><timept time="0s" value="0s" interp="linear"/></timeMap>',
    '<timeMap><timept time="0s" value="0s" interp="smooth2"/><timept time="2s" value="4s" interp="linear"/></timeMap>',
    '<timeMap><timept time="0s" value="4s" interp="linear"/><timept time="2s" value="0s" interp="linear"/></timeMap>',
    '<timeMap><timept time="0s" value="0s" interp="linear"/><timept time="2s" value="0s" interp="linear"/></timeMap>',
    '<timeMap><timept time="1s" value="0s" interp="linear"/><timept time="2s" value="4s" interp="linear"/></timeMap>',
    '<timeMap><timept time="0s" value="0s" interp="linear"/><timept time="1s" value="4s" interp="linear"/></timeMap>',
])
def test_unsupported_retime_keeps_local_keys_inspectable_without_guessed_media_times(make_editor, time_map):
    editor = make_editor(
        f'<asset-clip ref="r2" start="0s" duration="2s">{time_map}'
        '<adjust-volume><param name="amount"><keyframeAnimation>'
        '<keyframe time="1s" value="-12dB" auxValue="kept"/>'
        '</keyframeAnimation></param></adjust-volume></asset-clip>'
    )
    before = raw(editor)
    clip = editor.list_keyframes()["clips"][0]
    assert not clip["supported"]
    assert clip["time_mapping"] == "unsupported"
    point = clip["properties"]["volume"]["keyframes"][0]
    assert point["time"] == "1s"
    assert point["local_time"] == "1s"
    assert point["source_time"] == "1s"
    assert point["mapped_source_time"] is None
    assert point["value"] == -12
    assert point["in_range"] is True
    with pytest.raises(ValueError, match="timeMap/conform-rate"):
        editor.set_keyframes(clip["clip_path"], "volume", [{"time": "0s", "value": -6}])
    with pytest.raises(ValueError, match="timeMap/conform-rate"):
        editor.delete_keyframes(clip["clip_path"], "volume")
    assert raw(editor) == before


def test_connected_clip_can_use_own_linear_map_only_with_unretimed_host(make_editor):
    connected = LINEAR_RETIME_CLIP.replace('offset="0s"', 'offset="3603s" lane="1"')
    editor = make_editor(f'<gap start="3600s" offset="0s" duration="10s">{connected}</gap>')
    result = editor.set_keyframes(path_for(editor), "rotation", [{"time": "3s", "value": 30}])
    assert result["keyframes"][0]["local_time"] == "7s"
    assert result["keyframes"][0]["mapped_source_time"] == "9s"
    editor = make_editor(
        '<asset-clip ref="r2" start="0s" duration="10s"><timeMap>'
        '<timept time="0s" value="0s" interp="linear"/>'
        '<timept time="10s" value="20s" interp="linear"/></timeMap>'
        f'{connected}</asset-clip>'
    )
    path = path_for(editor, 1)
    clip = editor.list_keyframes(path)["clips"][0]
    assert not clip["supported"]
    with pytest.raises(ValueError, match="connected host is retimed"):
        editor.set_keyframes(path, "rotation", [{"time": "3s", "value": 30}])


def test_retimed_five_properties_validate_against_apple_dtd(make_editor, tmp_path):
    editor = make_editor(LINEAR_RETIME_CLIP, rate="1/30s")
    path = path_for(editor)
    for prop, value in [("volume", -12), ("opacity", 0.5), ("rotation", 10), ("scale", [1.1, 1.2]), ("position", [3, -4])]:
        editor.set_keyframes(path, prop, [{"time": "0s", "value": value}, {"time": "3s", "value": value}])
    destination = str(tmp_path / "retimed-keyframes.fcpxml")
    editor.modifier.save(destination)
    ok, detail = validate_against_dtd(destination)
    if ok is None:
        pytest.skip(detail)
    assert ok is True, detail
    reparsed = KeyframeEditor(FCPXMLModifier(destination))
    assert reparsed.list_keyframes() == editor.list_keyframes()


# Synthetic input verified in FCP 12.4 on 2026-10-07: at output 1.5 s,
# position is (21.6, 10.8) px in 720p, scale 106%, rotation 4.5 degrees,
# opacity 47.5%, volume -9.3 dB. These are 30% along the original local
# 5..10 s keyframes, not 60% as a mistaken source-time lookup would give.
NATIVE_FCP_12_4_RETIMED_CLIP = NATIVE_FCP_12_4_CLIP.replace(
    'duration="6s">', 'duration="3s"><timeMap preservesPitch="1">'
    '<timept time="5s" value="5s" interp="linear"/>'
    '<timept time="8s" value="11s" interp="linear"/></timeMap>',
)


@pytest.mark.parametrize("prop,value", [
    ("position", [3, 1.5]), ("scale", 1.06), ("rotation", 4.5),
    ("opacity", 0.475), ("volume", -9.3),
])
def test_fcp_verified_retime_keeps_all_intrinsic_keys_on_output_clock(make_editor, prop, value):
    editor = make_editor(NATIVE_FCP_12_4_RETIMED_CLIP, rate="1/30s")
    listing = editor.list_keyframes()
    assert listing["keyframe_clock"] == "clip-local-output"
    path = listing["clips"][0]["clip_path"]
    before = listing["clips"][0]["properties"][prop]["keyframes"]
    assert [point["time"] for point in before] == ["0s", "5s"]
    assert [point["mapped_source_time"] for point in before] == ["5s", None]
    assert [point["in_range"] for point in before] == [True, False]
    editor.set_keyframes(path, prop, [{"frame": 45, "value": value}])
    points = editor.list_keyframes(path)["clips"][0]["properties"][prop]["keyframes"]
    middle = points[1]
    assert middle["time"] == "3/2s"
    assert middle["local_time"] == "13/2s"
    assert middle["source_time"] == "13/2s"  # legacy raw-XML alias
    assert middle["mapped_source_time"] == "8s"
    assert editor.delete_keyframes(path, prop, ["3/2s"])["keyframes"] == before


@pytest.mark.parametrize("conform", [
    '<conform-rate/>', '<conform-rate scaleEnabled="1"/>',
    '<conform-rate scaleEnabled="0"/><conform-rate scaleEnabled="0"/>',
])
def test_retimed_keyframes_reject_active_or_ambiguous_conform(make_editor, conform):
    editor = make_editor(LINEAR_RETIME_CLIP.replace('<timeMap', conform + '<timeMap', 1))
    path = path_for(editor)
    before = raw(editor)
    with pytest.raises(ValueError, match="active or ambiguous rate conform"):
        editor.set_keyframes(path, "position", [{"time": "0s", "value": [1, 2]}])
    assert raw(editor) == before


def test_native_disabled_conform_is_allowed_only_with_matching_frame_rates(make_editor):
    clip = LINEAR_RETIME_CLIP.replace('<timeMap', '<conform-rate scaleEnabled="0"/><timeMap', 1)
    editor = make_editor(clip, rate="1/30s")
    result = editor.set_keyframes(path_for(editor), "opacity", [{"time": "1s", "value": 0.5}])
    assert result["keyframes"][0]["local_time"] == "5s"
    assert result["keyframes"][0]["mapped_source_time"] == "6s"
    editor = make_editor(clip, rate="1/30s", source_rate="1/24s")
    assert editor.list_keyframes()["clips"][0]["time_mapping"] == "unsupported"
    with pytest.raises(ValueError, match="frame rates differ"):
        editor.set_keyframes(path_for(editor), "opacity", [{"time": "1s", "value": 0.5}])


def test_asset_wide_time_map_on_trimmed_clip_keeps_animation_on_local_clock(make_editor):
    # Native FCP exports can extend a map beyond the visible trim to asset
    # boundaries. This 2x map is not anchored at the visible clip start.
    editor = make_editor(
        '<asset-clip ref="r2" start="5s" duration="3s">'
        '<conform-rate scaleEnabled="0"/><timeMap preservesPitch="1">'
        '<timept time="0s" value="0s" interp="linear"/>'
        '<timept time="100s" value="200s" interp="linear"/>'
        '</timeMap></asset-clip>', rate="1/30s",
    )
    point = editor.set_keyframes(path_for(editor), "opacity", [{"frame": 45, "value": 0.475}])["keyframes"][0]
    assert point["local_time"] == "13/2s"
    assert point["mapped_source_time"] == "13s"
    assert point["time"] == "3/2s"


@pytest.mark.parametrize("interp", ["ease", "easeIn", "easeOut"])
def test_sparse_native_opacity_easing_roundtrip_without_baking(make_editor, tmp_path, interp):
    editor = make_editor()
    path = path_for(editor)
    result = editor.set_keyframes(path, "opacity", [
        {"time": time, "value": value, "interp": interp}
        for time, value in zip(("0s", "1s", "3/2s"), (0.2, 1, 0.2))
    ])
    assert len(editor.modifier.root.findall(".//keyframe")) == 3
    assert [point["local_time"] for point in result["keyframes"]] == ["100s", "101s", "203/2s"]
    assert [point["interp"] for point in result["keyframes"]] == [interp] * 3
    assert all(point["curve"] is None for point in result["keyframes"])
    destination = tmp_path / "sparse-opacity.fcpxml"
    editor.modifier.save(str(destination))
    reparsed = KeyframeEditor(FCPXMLModifier(str(destination)))
    assert reparsed.list_keyframes(path)["clips"][0]["properties"]["opacity"]["keyframes"] == result["keyframes"]


@pytest.mark.parametrize("prop,value", [("position", [1, 2]), ("scale", 1), ("rotation", 45), ("volume", -6)])
@pytest.mark.parametrize("interp", ["ease", "easeIn", "easeOut"])
def test_unverified_intrinsic_easing_rejects_without_partial_mutation(make_editor, prop, value, interp):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError, match=f"{prop} interp"):
        editor.set_keyframes(path_for(editor), prop, [
            {"time": "0s", "value": value}, {"time": "1s", "value": value, "interp": interp},
        ])
    assert raw(editor) == before


@pytest.mark.parametrize("prop,value", [
    ("position", [1, 2]), ("scale", 1), ("rotation", 45), ("opacity", 0.5), ("volume", -6),
])
def test_native_smooth_mode_is_not_exposed_until_it_controls_playback(make_editor, prop, value):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError, match=f"{prop} curve"):
        editor.set_keyframes(path_for(editor), prop, [{"time": "0s", "value": value, "curve": "smooth"}])
    assert raw(editor) == before


@pytest.mark.parametrize("options", [
    {"interp": None}, {"interp": []}, {"curve": {"x": 1}},
    {"curve": "smooth2"}, {"auxValue": "arbitrary XML"}, {"curve": True},
])
def test_invalid_curve_options_reject_without_partial_mutation(make_editor, options):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError):
        editor.set_keyframes(path_for(editor), "opacity", [
            {"time": "0s", "value": 0.2}, {"time": "1s", "value": 1, **options},
        ])
    assert raw(editor) == before


def test_sparse_opacity_merge_delete_and_replace_preserve_existing_metadata(make_editor):
    editor = make_editor(
        '<asset-clip ref="r2" start="100s" duration="2s"><adjust-blend>'
        '<param name="amount"><keyframeAnimation>'
        '<keyframe time="100s" value="0.2" interp="easeIn" auxValue="left"/>'
        '<keyframe time="101s" value="1" interp="ease" auxValue="middle"/>'
        '<keyframe time="203/2s" value="0.2" interp="easeOut" auxValue="right"/>'
        '</keyframeAnimation></param></adjust-blend></asset-clip>'
    )
    path = path_for(editor)
    original = editor.list_keyframes(path)["clips"][0]["properties"]["opacity"]["keyframes"]
    merged = editor.set_keyframes(path, "opacity", [{"time": "1s", "value": 0.8}])["keyframes"]
    assert merged[0] == original[0]
    assert merged[2] == original[2]
    assert merged[1]["attributes"] == {**original[1]["attributes"], "value": "0.8"}
    assert editor.delete_keyframes(path, "opacity", ["1s"])["keyframes"] == [original[0], original[2]]
    replaced = editor.set_keyframes(path, "opacity", [{"time": "0s", "value": 0.3}], mode="replace")["keyframes"]
    assert len(replaced) == 1
    assert replaced[0]["attributes"] == {**original[0]["attributes"], "value": "0.3"}


def test_explicit_opacity_easing_switches_preserve_linear_normalization_contract(make_editor):
    editor = make_editor()
    path = path_for(editor)

    def update(**options):
        return editor.set_keyframes(path, "opacity", [{"time": "0s", "value": 0.5, **options}])["keyframes"][0]

    assert update()["attributes"] == {"time": "100s", "value": "0.5", "curve": "linear"}
    assert update(interp="ease")["attributes"] == {"time": "100s", "value": "0.5", "interp": "ease"}
    assert update()["interp"] == "ease"
    assert update(interp="easeIn")["interp"] == "easeIn"
    assert update(interp="linear")["attributes"] == {"time": "100s", "value": "0.5", "curve": "linear"}
    assert update(interp="easeOut")["interp"] == "easeOut"
    assert update(curve="linear")["interp"] is None


def test_sparse_opacity_easing_ntsc_frames_use_nonzero_local_output_clock(make_editor):
    editor = make_editor(rate="1001/30000s")
    points = editor.set_keyframes(path_for(editor), "opacity", [
        {"frame": frame, "value": value, "interp": "ease"}
        for frame, value in zip((0, 29, 58), (0.2, 1, 0.2))
    ])["keyframes"]
    assert len(points) == 3
    for point, frame in zip(points, (0, 29, 58)):
        assert parse_seconds(point["time"]) == frame * Fraction(1001, 30000)
        assert parse_seconds(point["local_time"]) == 100 + frame * Fraction(1001, 30000)


def test_sparse_opacity_easing_keeps_local_output_clock_on_retimed_clip(make_editor):
    editor = make_editor(LINEAR_RETIME_CLIP, rate="1/30s")
    path = path_for(editor)
    time_map = editor.modifier.root.find(".//timeMap")
    before = ET.tostring(time_map)
    points = editor.set_keyframes(path, "opacity", [
        {"time": time, "value": value, "interp": "ease"}
        for time, value in zip(("0s", "5/2s", "5s"), (0.2, 1, 0.2))
    ])["keyframes"]
    assert len(points) == 3
    assert [point["local_time"] for point in points] == ["4s", "13/2s", "9s"]
    assert [point["mapped_source_time"] for point in points] == ["5s", "8s", "23/2s"]
    assert ET.tostring(time_map) == before


@pytest.mark.parametrize("version", ["1.10", "1.11", "1.12", "1.13", "1.14"])
def test_sparse_opacity_easing_conforms_to_apple_dtd(make_editor, tmp_path, version):
    editor = make_editor(version=version)
    editor.set_keyframes(path_for(editor), "opacity", [
        {"time": time, "value": value, "interp": interp}
        for time, value, interp in zip(("0s", "1s", "3/2s"), (0.2, 1, 0.2), ("ease", "easeIn", "easeOut"))
    ])
    destination = tmp_path / f"sparse-{version}.fcpxml"
    editor.modifier.save(str(destination))
    valid, detail = validate_against_dtd(str(destination))
    if valid is None:
        pytest.skip(detail)
    assert valid, detail


@pytest.mark.parametrize("interp", ["ease", "easeIn", "easeOut"])
def test_opacity_easing_cannot_mix_explicit_curve_fields(make_editor, interp):
    editor = make_editor()
    before = raw(editor)
    with pytest.raises(ValueError, match="cannot be combined"):
        editor.set_keyframes(path_for(editor), "opacity", [
            {"time": "0s", "value": 0.2},
            {"time": "1s", "value": 1, "interp": interp, "curve": "linear"},
        ])
    assert raw(editor) == before
