"""MCP contract and publication tests for adaptive animation curves."""

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import server
from fcpxml import animation_curves, journal
from fcpxml.mcp_compat import tool_input_schema
from fcpxml.rational import parse_seconds
from tools import keyframes as kt


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "input.fcpxml"
    path.write_text('''<fcpxml version="1.14"><resources>
      <format id="r1" frameDuration="1/25s" width="1920" height="1080"/>
      <asset id="r2" start="0s" duration="100s" format="r1" hasVideo="1" hasAudio="1" audioSources="1" audioChannels="2">
        <media-rep kind="original-media" src="file:///synthetic.mov"/>
      </asset></resources><library><event name="Test"><project name="Bezier">
      <sequence format="r1" duration="10s" tcStart="0s"><spine>
        <asset-clip name="Duplicate" ref="r2" offset="0s" start="10s" duration="5s"/>
        <asset-clip name="Duplicate" ref="r2" offset="5s" start="20s" duration="5s"/>
      </spine></sequence></project></event></library></fcpxml>''')
    monkeypatch.setattr(server, "READ_ROOTS", [str(tmp_path)])
    monkeypatch.delenv("FCP_MCP_AUTOPUSH", raising=False)
    return path


async def call(action, arguments, *, flat=False):
    if flat:
        return (await server.call_tool(action, arguments))[0].text
    return (await server.call_tool("edit", {"action": action, "args": arguments}))[0].text


async def listing(path):
    result = await server.call_tool("inspect", {"action": "list_keyframes", "args": {"filepath": str(path)}})
    return json.loads(result[0].text)


def canonical(element):
    return ET.canonicalize(ET.tostring(element, encoding="unicode"), strip_text=True)


async def curve_args(path, **changes):
    selection = (await listing(path))["clips"][1]["clip_path"]
    return {
        "filepath": str(path), "clip_path": selection, "property": "position", "tolerance": 0.2,
        "points": [
            {"frame": 0, "value": [0, 0], "control_out": [0, 40], "easing": "ease"},
            {"frame": 100, "value": [40, 0], "control_in": [40, 40]},
        ], **changes,
    }


def test_animation_curve_flat_and_grouped_contracts_are_identical_draft7():
    validator = pytest.importorskip("jsonschema").Draft7Validator
    flat = {tool.name: tool_input_schema(tool) for tool in server._legacy_tool_list()}["set_animation_curve"]
    group = tool_input_schema(server._group_tool("edit"))
    branch = next(clause for clause in group["allOf"]
                  if clause["if"]["properties"]["action"]["const"] == "set_animation_curve")
    assert branch["then"]["properties"]["args"] == flat == kt.ACTION_SCHEMAS["set_animation_curve"]
    validator.check_schema(flat)
    validator.check_schema(group)
    assert flat["additionalProperties"] is False
    point = flat["properties"]["points"]["items"]
    assert point["additionalProperties"] is False
    assert set(point["properties"]) == {"time", "frame", "value", "control_in", "control_out", "easing"}
    args = {"filepath": "/tmp/a.fcpxml", "clip_path": "/clip", "property": "position",
            "points": [{"frame": 0, "value": [0, 0]}, {"time": "1s", "value": [1, 1]}]}
    for schema, payload in [(flat, args), (group, {"action": "set_animation_curve", "args": args})]:
        assert validator(schema).is_valid(payload)
    for bad in ({**args, "property": "volume"}, {**args, "mode": "merge"},
                {**args, "tolerance": 0},
                {**args, "points": [{"frame": 0, "time": "0s", "value": [0, 0]}, args["points"][1]]}):
        assert not validator(flat).is_valid(bad)
        assert not validator(group).is_valid({"action": "set_animation_curve", "args": bad})


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True])
async def test_curve_call_emits_few_points_with_independently_bounded_error(project, flat):
    original = project.read_bytes()
    original_first = canonical(ET.fromstring(original).findall(".//spine/asset-clip")[0])
    args = await curve_args(project)
    result = json.loads(await call("set_animation_curve", args, flat=flat))
    operation = result["operations"][0]
    output = Path(result["output_path"])
    output_clips = ET.parse(output).findall(".//spine/asset-clip")
    assert canonical(output_clips[0]) == original_first
    points = (await listing(output))["clips"][1]["properties"]["position"]["keyframes"]
    assert 2 < len(points) < 50 < operation["sampled_frame_count"]
    assert len(points) == operation["generated_keyframe_count"]
    assert operation["authored_point_count"] == 2
    assert operation["representation"] == "adaptive_linear"
    assert operation["max_error"] <= args["tolerance"]
    assert all(point["curve"] == "linear" and point["interp"] is None for point in points)
    samples = [(int(parse_seconds(point["time"]) * 25), point["value"]) for point in points]
    maximum = 0
    segment = 0
    for frame in range(101):
        while segment + 1 < len(samples) - 1 and frame > samples[segment + 1][0]:
            segment += 1
        left_frame, left = samples[segment]
        right_frame, right = samples[segment + 1]
        weight = (frame - left_frame) / (right_frame - left_frame)
        actual = [a + weight * (b - a) for a, b in zip(left, right)]
        t = frame / 100
        u = 3 * t * t - 2 * t * t * t
        # Independent polynomial evaluation of (0,0),(0,40),(40,40),(40,0).
        expected = [120 * (1 - u) * u * u + 40 * u**3, 120 * u * (1 - u)]
        maximum = max(maximum, math.dist(actual, expected))
    assert maximum <= args["tolerance"] + 1e-12
    assert operation["max_error"] == pytest.approx(maximum, abs=1e-12)
    assert project.read_bytes() == original
    rows = journal.records(str(project))
    assert len(rows) == 1 and rows[0]["action"] == "set_animation_curve"
    assert rows[0]["output"]["path"] == str(output)
    assert "no native Bezier handles" in result["verification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["input", "existing", "symlink"])
async def test_animation_curve_never_overwrites_an_existing_path(project, target):
    output = project if target == "input" else project.with_name("already.fcpxml")
    if target == "existing":
        output.write_bytes(b"preserve existing work")
    if target == "symlink":
        output.symlink_to(project)
    original = project.read_bytes()
    protected = output.read_bytes()
    response = await call("set_animation_curve", await curve_args(project, output_path=str(output)))
    assert "Validation error" in response
    assert output.read_bytes() == protected
    assert project.read_bytes() == original
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"property": "volume"}, {"tolerance": 0}, {"tolerance": float("nan")}, {"unexpected": True},
    {"points": [{"frame": 0, "value": [0, 0]}]},
    {"points": [{"frame": 0, "value": [0, 0]}, {"frame": 125, "value": [1, 1]}]},
    {"points": [{"time": "1/30s", "value": [0, 0]}, {"frame": 100, "value": [1, 1]}]},
    {"points": [{"frame": 0, "value": [0, 0]}, {"frame": 100, "value": [1, 1], "easing": "ease"}]},
    {"points": [{"frame": 0, "value": [0, 0], "control_out": [float("inf"), 0]}, {"frame": 100, "value": [1, 1]}]},
])
async def test_invalid_animation_arguments_publish_nothing(project, changes):
    before = project.read_bytes()
    output = project.with_name("invalid.fcpxml")
    response = await call("set_animation_curve", await curve_args(project, output_path=str(output), **changes))
    assert "Validation error" in response
    assert not output.exists()
    assert project.read_bytes() == before
    assert journal.records(str(project)) == []
    assert not list(project.parent.glob(".keyframes-*"))


def test_curve_schema_takes_its_limits_from_the_sampler():
    properties = kt.ACTION_SCHEMAS["set_animation_curve"]["properties"]
    points = properties["points"]
    assert points["maxItems"] == animation_curves.MAX_AUTHORED_POINTS
    assert points["items"]["properties"]["easing"]["enum"] == list(animation_curves.EASINGS)
    assert properties["property"]["enum"] == list(animation_curves.CURVE_PROPERTIES)
    assert "dB" not in properties["property"]["description"]
    tolerance = properties["tolerance"]["description"]
    for prop, value in animation_curves.DEFAULT_TOLERANCE.items():
        assert f"{prop} {value:g}" in tolerance
    assert f"{animation_curves.MAX_SAMPLED_FRAMES} evaluated frames" in tolerance
    assert f"{animation_curves.MAX_GENERATED_KEYFRAMES} output keys" in tolerance


@pytest.mark.asyncio
async def test_volume_on_a_video_only_clip_reports_the_curve_limit(project):
    project.write_text(project.read_text().replace('hasAudio="1"', 'hasAudio="0"'))
    before = project.read_bytes()
    output = project.with_name("volume.fcpxml")
    response = await call("set_animation_curve", await curve_args(
        project, property="volume", output_path=str(output),
        points=[{"frame": 0, "value": 0}, {"frame": 10, "value": -6}],
    ))
    assert "volume is unsupported" in response
    assert "audio component" not in response
    assert not output.exists()
    assert project.read_bytes() == before
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,value,reason", [
    ("MAX_SAMPLED_FRAMES", 10, "evaluated project frames"),
    ("MAX_GENERATED_KEYFRAMES", 2, "generated keyframes"),
])
async def test_adaptive_limits_fail_without_outputs_or_journal(project, monkeypatch, limit, value, reason):
    monkeypatch.setattr(animation_curves, limit, value)
    original = project.read_bytes()
    output = project.with_name("limited.fcpxml")
    response = await call("set_animation_curve", await curve_args(project, output_path=str(output)))
    assert reason in response
    assert not output.exists()
    assert project.read_bytes() == original
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_fresh_curve_drops_aux_but_legacy_point_edit_still_preserves_it(project):
    root = ET.parse(project).getroot()
    clip = root.findall(".//spine/asset-clip")[1]
    transform = ET.SubElement(clip, "adjust-transform", position="5 6")
    param = ET.SubElement(transform, "param", name="position", value="5 6")
    animation = ET.SubElement(param, "keyframeAnimation")
    ET.SubElement(animation, "keyframe", time="20s", value="0 0", curve="linear", auxValue="keep in legacy edit")
    ET.SubElement(animation, "keyframe", time="24s", value="40 0", curve="linear", auxValue="old tangent")
    project.write_bytes(ET.tostring(root))
    before = project.read_bytes()
    args = await curve_args(project)
    legacy = json.loads(await call("set_keyframes", {
        "filepath": str(project), "clip_path": args["clip_path"], "property": "position",
        "keyframes": [{"time": "0s", "value": [1, 2]}],
    }))
    assert ET.parse(legacy["output_path"]).find(".//keyframe").get("auxValue") == "keep in legacy edit"
    output = json.loads(await call("set_animation_curve", args))["output_path"]
    generated = ET.parse(output).findall(".//keyframe")
    assert generated and all(point.get("auxValue") is None for point in generated)
    assert ET.parse(output).find(".//adjust-transform/param").get("value") == "5 6"
    assert project.read_bytes() == before


@pytest.mark.asyncio
async def test_adaptive_curves_use_retimed_local_output_clock(project):
    root = ET.parse(project).getroot()
    clip = root.findall(".//spine/asset-clip")[1]
    time_map = ET.SubElement(clip, "timeMap", preservesPitch="1")
    ET.SubElement(time_map, "timept", time="20s", value="30s", interp="linear")
    ET.SubElement(time_map, "timept", time="25s", value="40s", interp="linear")
    project.write_bytes(ET.tostring(root))
    original_map = canonical(time_map)
    result = json.loads(await call("set_animation_curve", await curve_args(project)))
    points = (await listing(result["output_path"]))["clips"][1]["properties"]["position"]["keyframes"]
    assert points[0]["local_time"] == "20s" and points[-1]["local_time"] == "24s"
    for point in points:
        relative = parse_seconds(point["time"])
        assert parse_seconds(point["local_time"]) == 20 + relative
        assert parse_seconds(point["mapped_source_time"]) == 30 + 2 * relative
    assert canonical(ET.parse(result["output_path"]).find(".//timeMap")) == original_map


@pytest.mark.asyncio
async def test_animation_curve_bundle_retains_sidecars_and_source(project):
    bundle = project.with_suffix(".fcpxmld")
    bundle.mkdir()
    (bundle / "Info.fcpxml").write_bytes(project.read_bytes())
    sidecar = bundle / "tracking" / "payload.bin"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"preserved sidecar\x00\xff")
    result = json.loads(await call("set_animation_curve", await curve_args(bundle)))
    output = Path(result["output_path"])
    assert output.suffix == ".fcpxmld"
    assert (output / "tracking" / "payload.bin").read_bytes() == sidecar.read_bytes()
    assert (bundle / "Info.fcpxml").read_bytes() == project.read_bytes()
    assert len(ET.parse(output / "Info.fcpxml").findall(".//keyframe")) == result["operations"][0]["generated_keyframe_count"]
