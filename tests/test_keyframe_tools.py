"""Exercise the MCP surface against real XML, including rejected writes."""

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import server
from fcpxml import journal
from fcpxml.dtd import available_dtd_versions
from fcpxml.mcp_compat import tool_input_schema
from tools import keyframes as kt

VALID_XML = '''<fcpxml version="1.13"><resources>
<format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s" width="1920" height="1080"/>
<asset id="r2" name="A" start="0s" duration="100s" hasVideo="1" hasAudio="1" audioSources="1" audioChannels="2" format="r1">
<media-rep kind="original-media" src="file:///a.mov"/></asset></resources>
<library><event name="E"><project name="Keyframe tool test">
<sequence format="r1" duration="10s" tcStart="0s"><spine>
<asset-clip name="Duplicate" ref="r2" offset="0s" start="10s" duration="5s"/>
<asset-clip name="Duplicate" ref="r2" offset="5s" start="20s" duration="5s"/>
</spine></sequence></project></event></library></fcpxml>'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "input.fcpxml"
    path.write_text(VALID_XML)
    monkeypatch.setattr(server, "READ_ROOTS", [str(tmp_path)])
    monkeypatch.delenv("FCP_MCP_AUTOPUSH", raising=False)
    return path


async def call(group, action, args):
    result = await server.call_tool(group, {"action": action, "args": args})
    return result[0].text


async def listing(path):
    return json.loads(await call("inspect", "list_keyframes", {"filepath": str(path)}))


async def selection(path, index=1):
    return (await listing(path))["clips"][index]["clip_path"]


async def set_args(path, **changes):
    return {"filepath": str(path), "clip_path": await selection(path),
            "property": "rotation", "keyframes": [
                {"time": "0s", "value": 0}, {"frame": 25, "value": 30, "interp": "linear"},
            ], **changes}


def test_existing_groups_and_flat_schema_expose_precise_arguments():
    assert "list_keyframes" in server.TOOL_GROUPS["inspect"]["actions"]
    for name in ("set_keyframes", "delete_keyframes", "batch_keyframes"):
        assert name in server.TOOL_GROUPS["edit"]["actions"]
        assert name in server.TOOL_HANDLERS
        assert "filepath" in server._action_param_help(name)
    flat = {t.name: tool_input_schema(t) for t in server._legacy_tool_list()}
    assert flat["set_keyframes"]["additionalProperties"] is False
    assert flat["batch_keyframes"]["properties"]["operations"]["maxItems"] == 100
    schema = tool_input_schema(server._group_tool("edit"))
    conditions = {c["if"]["properties"]["action"]["const"]: c for c in schema["allOf"]}
    assert conditions["set_keyframes"]["then"]["properties"]["args"] == flat["set_keyframes"]


@pytest.mark.asyncio
async def test_list_is_read_only_and_resolves_duplicate_names(project):
    before = project.read_bytes()
    data = await listing(project)
    assert len(data["clips"]) == 2
    assert data["clips"][0]["clip_path"] != data["clips"][1]["clip_path"]
    assert project.read_bytes() == before
    assert journal.records(str(project)) == []
    assert not list(project.parent.glob("*keyframes*"))


@pytest.mark.asyncio
async def test_set_roundtrips_only_selected_instance_and_journals(project):
    original = project.read_bytes()
    result = json.loads(await call("edit", "set_keyframes", await set_args(project)))
    out = Path(result["output_path"])
    assert out.is_file() and out != project
    assert project.read_bytes() == original
    roots = ET.parse(out).findall(".//spine/asset-clip")
    assert roots[0].find(".//keyframe") is None
    assert len(roots[1].findall(".//keyframe")) == 2
    points = (await listing(out))["clips"][1]["properties"]["rotation"]["keyframes"]
    assert [(p["time"], p["value"]) for p in points] == [("0s", 0), ("1s", 30)]
    assert "unverified" in result["verification"].lower()
    rows = journal.records(str(project))
    assert len(rows) == 1 and rows[0]["action"] == "set_keyframes"
    assert rows[0]["output"]["path"] == str(out)
    assert rows[0]["input"]["sha256"] == journal.file_hash(str(project))


@pytest.mark.asyncio
async def test_default_outputs_number_without_overwriting(project):
    args = await set_args(project)
    first = json.loads(await call("edit", "set_keyframes", args))
    before = Path(first["output_path"]).read_bytes()
    second = json.loads(await call("edit", "set_keyframes", args))
    assert first["output_path"] != second["output_path"]
    assert Path(first["output_path"]).read_bytes() == before
    assert Path(second["output_path"]).name == "input_keyframes_2.fcpxml"


@pytest.mark.asyncio
async def test_flat_dispatch_works_and_delete_selected_time(project):
    response = await server.call_tool("set_keyframes", await set_args(project))
    source = Path(json.loads(response[0].text)["output_path"])
    result = json.loads(await call("edit", "delete_keyframes", {
        "filepath": str(source), "clip_path": await selection(source),
        "property": "rotation", "times": ["1s"],
    }))
    points = (await listing(result["output_path"]))["clips"][1]["properties"]["rotation"]["keyframes"]
    assert len(points) == 1 and points[0]["time"] == "0s"
    assert len((await listing(source))["clips"][1]["properties"]["rotation"]["keyframes"]) == 2


@pytest.mark.asyncio
async def test_batch_applies_set_and_delete_then_saves_once(project, monkeypatch):
    clip = await selection(project)
    original_save = server.FCPXMLModifier.save
    saves = []

    def counted_save(self, path):
        saves.append(path)
        return original_save(self, path)

    monkeypatch.setattr(server.FCPXMLModifier, "save", counted_save)
    result = json.loads(await call("edit", "batch_keyframes", {
        "filepath": str(project), "operations": [
            {"action": "set", "clip_path": clip, "property": "rotation",
             "keyframes": [{"time": "0s", "value": 0}, {"time": "1s", "value": 30}]},
            {"action": "set", "clip_path": clip, "property": "opacity",
             "keyframes": [{"time": "0s", "value": 0}, {"time": "1s", "value": 1}]},
            {"action": "delete", "clip_path": clip, "property": "rotation", "times": ["1s"]},
        ],
    }))
    assert len(saves) == 1
    assert len(result["operations"]) == 3
    properties = (await listing(result["output_path"]))["clips"][1]["properties"]
    assert len(properties["rotation"]["keyframes"]) == 1
    assert len(properties["opacity"]["keyframes"]) == 2
    assert len(journal.records(str(project))) == 1


@pytest.mark.asyncio
async def test_batch_failure_after_valid_operation_creates_nothing(project):
    before = project.read_bytes()
    clip = await selection(project)
    out = project.with_name("failure.fcpxml")
    result = await call("edit", "batch_keyframes", {
        "filepath": str(project), "output_path": str(out), "operations": [
            {"action": "set", "clip_path": clip, "property": "rotation",
             "keyframes": [{"time": "0s", "value": 15}]},
            {"action": "set", "clip_path": clip, "property": "opacity",
             "keyframes": [{"time": "1s", "value": 2}]},
        ],
    })
    assert "Validation error" in result
    assert not out.exists() and project.read_bytes() == before
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["input", "existing", "symlink", "hardlink", "outside", "wrong_extension"])
async def test_output_refusals_preserve_all_files(project, destination, tmp_path):
    out = project
    protected = project.read_bytes()
    if destination == "existing":
        out = tmp_path / "existing.fcpxml"
        out.write_text("keep this work")
        protected = out.read_bytes()
    elif destination == "symlink":
        out = tmp_path / "link.fcpxml"
        out.symlink_to(project)
    elif destination == "hardlink":
        out = tmp_path / "link.fcpxml"
        out.hardlink_to(project)
    elif destination == "outside":
        out = tmp_path.parent / "outside.fcpxml"
    elif destination == "wrong_extension":
        out = tmp_path / "output.txt"
    args = await set_args(project, output_path=str(out))
    result = await call("edit", "set_keyframes", args)
    assert "Validation error" in result
    if out.exists():
        assert out.read_bytes() == protected
    else:
        assert not out.exists()
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_read_sandbox_applies_to_new_actions(project, monkeypatch):
    args = await set_args(project)
    monkeypatch.setattr(server, "READ_ROOTS", [str(project.parent / "other")])
    result = await call("edit", "set_keyframes", args)
    assert "Validation error" in result
    assert not project.with_name("input_keyframes.fcpxml").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,extra", [
    ("list_keyframes", {"unexpected": True}),
    ("set_keyframes", {"unexpected": True}),
    ("delete_keyframes", {"unexpected": True}),
    ("batch_keyframes", {"operations": [{"action": "delete", "clip_path": "x", "property": "rotation", "keyframes": []}]}),
])
async def test_unknown_fields_are_rejected(project, action, extra):
    args = {"filepath": str(project), **extra}
    if action in ("set_keyframes", "delete_keyframes"):
        args.update({"clip_path": await selection(project), "property": "rotation"})
        if action == "set_keyframes":
            args["keyframes"] = [{"time": "0s", "value": 1}]
    result = await call("inspect" if action == "list_keyframes" else "edit", action, args)
    assert "Unknown argument" in result
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operations", [[], [{}] * 101, "not an array"])
async def test_batch_bounds(project, operations):
    result = await call("edit", "batch_keyframes", {"filepath": str(project), "operations": operations})
    assert "operations must contain" in result
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_point_bound(project):
    args = await set_args(project, keyframes=[{"time": "0s", "value": 0}] * 1001)
    result = await call("edit", "set_keyframes", args)
    assert "keyframes must contain" in result
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_dtd_rejection_never_publishes_or_journals(project, monkeypatch):
    staged_files = []

    def reject(path):
        staged_files.append(Path(path))
        assert Path(path).exists()
        assert not project.with_name("input_keyframes.fcpxml").exists()
        return False, "controlled DTD failure"

    monkeypatch.setattr(kt.dtd, "validate_against_dtd", reject)
    result = await call("edit", "set_keyframes", await set_args(project))
    assert "controlled DTD failure" in result and "no output published" in result
    assert not project.with_name("input_keyframes.fcpxml").exists()
    assert staged_files and not staged_files[0].exists()
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_missing_dtd_is_reported_as_unavailable(project, monkeypatch):
    monkeypatch.setattr(kt.dtd, "validate_against_dtd", lambda path: (None, "no Apple DTD installed"))
    result = json.loads(await call("edit", "set_keyframes", await set_args(project)))
    assert result["validation"]["apple_dtd"] == "unavailable"
    assert "no Apple DTD" in result["validation"]["detail"]
    assert Path(result["output_path"]).is_file()


@pytest.mark.asyncio
@pytest.mark.skipif("1.13" not in available_dtd_versions() or not shutil.which("xmllint"),
                    reason="Apple DTD and xmllint unavailable")
async def test_real_apple_dtd_validates_published_keyframes(project):
    result = json.loads(await call("edit", "set_keyframes", await set_args(project)))
    assert result["validation"]["apple_dtd"] == "passed"


@pytest.mark.asyncio
async def test_bundle_sidecars_survive_and_input_bundle_is_protected(project):
    bundle = project.with_suffix(".fcpxmld")
    bundle.mkdir()
    (bundle / "Info.fcpxml").write_bytes(project.read_bytes())
    (bundle / "tracking").mkdir()
    payload = b"exact tracking sidecar\x00\xff"
    (bundle / "tracking" / "data.bin").write_bytes(payload)
    result = json.loads(await call("edit", "set_keyframes", await set_args(bundle)))
    output = Path(result["output_path"])
    assert output.suffix == ".fcpxmld"
    assert (output / "tracking" / "data.bin").read_bytes() == payload
    assert (bundle / "Info.fcpxml").read_bytes() == project.read_bytes()
    for path in (bundle, bundle / "Info.fcpxml", bundle / "extra.fcpxmld"):
        rejected = await call("edit", "set_keyframes", await set_args(bundle, output_path=str(path)))
        assert "Validation error" in rejected
    inner = await call("inspect", "list_keyframes", {"filepath": str(bundle / "Info.fcpxml")})
    assert "bundle root" in inner


@pytest.mark.asyncio
async def test_publication_collision_preserves_new_work_without_journaling_it(project, monkeypatch):
    output = project.with_name("input_keyframes.fcpxml")

    def concurrent_output(path):
        output.write_text("another editor claimed this path")
        return None, "DTD unavailable in this controlled collision"

    monkeypatch.setattr(kt.dtd, "validate_against_dtd", concurrent_output)
    result = await call("edit", "set_keyframes", await set_args(project))
    assert "FileExistsError" in result
    assert output.read_text() == "another editor claimed this path"
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_bundle_publication_failure_removes_only_our_incomplete_output(project, monkeypatch):
    bundle = project.with_suffix(".fcpxmld")
    bundle.mkdir()
    (bundle / "Info.fcpxml").write_bytes(project.read_bytes())
    sidecar = bundle / "sidecar.bin"
    sidecar.write_bytes(b"preserve me")
    output = bundle.with_name("input_keyframes.fcpxmld")
    original_copy = kt.shutil.copytree

    def fail_publication(src, dst, **kwargs):
        if Path(dst) == output:
            (output / "partial").write_bytes(b"partial")
            raise OSError("controlled copy failure")
        return original_copy(src, dst, **kwargs)

    monkeypatch.setattr(kt.shutil, "copytree", fail_publication)
    result = await call("edit", "set_keyframes", await set_args(bundle))
    assert "OSError" in result
    assert not output.exists()
    assert sidecar.read_bytes() == b"preserve me"
    assert (bundle / "Info.fcpxml").read_bytes() == project.read_bytes()
    assert journal.records(str(bundle)) == []
