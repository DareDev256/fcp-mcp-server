"""MCP adapters for intrinsic keyframes; validate before publishing a new file."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from mcp.types import Tool

from fcpxml import dtd
from fcpxml.keyframes import KeyframeEditor
from tools import _common

MAX_OPERATIONS = 100
MAX_KEYFRAMES = 1000
MAX_BATCH_KEYFRAMES = 10000
PROPERTIES = ["position", "scale", "rotation", "opacity", "volume"]

_FILE = {"type": "string", "description": "Input FCPXML or .fcpxmld bundle."}
_CLIP = {
    "type": "string",
    "description": "Exact clip_path returned by inspect.list_keyframes; never a clip name.",
}
_PROPERTY = {
    "type": "string", "enum": PROPERTIES,
    "description": (
        "position uses native FCPXML [x,y] coordinates; scale uses positive multipliers "
        "(one number or [x,y]); rotation degrees; opacity 0..1; volume numeric dB "
        "(Final Cut interpolates native gain, not a linear dB ramp)."
    ),
}
_TIME = {
    "type": "string", "pattern": r"^\d+(?:/[1-9]\d*)?s$",
    "description": "Rational seconds relative to the visible clip start, e.g. 24/25s.",
}
_KEYFRAMES = {
    "type": "array", "minItems": 1, "maxItems": MAX_KEYFRAMES,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "time": _TIME,
            "frame": {"type": "integer", "minimum": 0, "description": "Clip-relative frame index."},
            "value": {"oneOf": [{"type": "number"}, {
                "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
            }]},
            "interp": {"type": "string", "enum": ["linear"], "default": "linear",
                       "description": "Native linear interpolation; XML attributes are normalized per property."},
            "curve": {"type": "string", "enum": ["linear"], "default": "linear",
                      "description": "Native linear curve; XML attributes are normalized per property."},
        },
        "required": ["value"],
        "oneOf": [{"required": ["time"]}, {"required": ["frame"]}],
    },
}
_TIMES = {
    "type": ["array", "null"], "minItems": 1, "maxItems": MAX_KEYFRAMES,
    "items": _TIME,
    "description": "Remove these relative times; omit or null to remove the property's animation.",
}
_MODE = {
    "type": "string", "enum": ["merge", "replace"], "default": "merge",
    "description": "Merge updates matching times; replace replaces this property's curve.",
}
_OUTPUT = {
    "type": "string",
    "description": "New output under the source directory; existing paths are refused. Defaults to a numbered _keyframes sibling.",
}


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_SET = {"clip_path": _CLIP, "property": _PROPERTY, "keyframes": _KEYFRAMES, "mode": _MODE}
_DELETE = {"clip_path": _CLIP, "property": _PROPERTY, "times": _TIMES}
_OPERATION = {"oneOf": [
    _schema({"action": {"const": "set"}, **_SET}, ["action", "clip_path", "property", "keyframes"]),
    _schema({"action": {"const": "delete"}, **_DELETE}, ["action", "clip_path", "property"]),
]}
ACTION_SCHEMAS = {
    "list_keyframes": _schema({"filepath": _FILE, "clip_path": _CLIP}, ["filepath"]),
    "set_keyframes": _schema({"filepath": _FILE, **_SET, "output_path": _OUTPUT},
                             ["filepath", "clip_path", "property", "keyframes"]),
    "delete_keyframes": _schema({"filepath": _FILE, **_DELETE, "output_path": _OUTPUT},
                                ["filepath", "clip_path", "property"]),
    "batch_keyframes": _schema({
        "filepath": _FILE, "output_path": _OUTPUT,
        "operations": {"type": "array", "minItems": 1, "maxItems": MAX_OPERATIONS,
                       "items": _OPERATION},
    }, ["filepath", "operations"]),
}
_DESCRIPTIONS = {
    "list_keyframes": "Read intrinsic animation and unique clip paths, including unsupported structures; no writes.",
    "set_keyframes": "Set native linear position, scale, rotation, opacity or volume keyframes on one uniquely selected clip. Volume values are dB; FCP interpolates gain. Writes a new file after available Apple DTD validation.",
    "delete_keyframes": "Delete selected keyframes or one property's animation, preserving other XML. Writes a new file.",
    "batch_keyframes": "Apply up to 100 set/delete operations to one input, then save once. Any invalid operation aborts without an output. Proxy previews do not verify animation; inspect the imported result in FCP.",
}


def tool_schemas() -> list[Tool]:
    return [Tool(name=name, description=_DESCRIPTIONS[name], inputSchema=schema)
            for name, schema in ACTION_SCHEMAS.items()]


def group_schema_extensions(actions: list[str]) -> dict:
    """Expose keyframe arguments in their existing inspect/edit group schemas."""
    clauses = [{
        "if": {"properties": {"action": {"const": action}}, "required": ["action"]},
        "then": {"properties": {"args": schema}, "required": ["args"]},
    } for action, schema in ACTION_SCHEMAS.items() if action in actions]
    return {"allOf": clauses} if clauses else {}


def _check_fields(arguments: dict, schema: dict) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be an object")
    unknown = set(arguments) - set(schema["properties"])
    if unknown:
        raise ValueError(f"Unknown argument(s): {', '.join(sorted(unknown))}")
    for key in schema["required"]:
        if key not in arguments:
            raise KeyError(key)
    for key in ("filepath", "output_path", "clip_path", "property"):
        if key in arguments and (not isinstance(arguments[key], str) or not arguments[key]):
            raise ValueError(f"{key} must be a nonempty string")


def _editor(arguments: dict) -> tuple[str, KeyframeEditor]:
    srv = _common.tools.server_module()
    filepath = srv._validate_filepath(arguments["filepath"], (".fcpxml", ".fcpxmld"))
    path = Path(filepath)
    if any(p.suffix.lower() == ".fcpxmld" for p in path.parents):
        raise ValueError("Pass the .fcpxmld bundle root, not a file inside it, to preserve sidecars")
    return filepath, KeyframeEditor(srv.FCPXMLModifier(filepath))


def _check_operation(operation: dict) -> None:
    if not isinstance(operation, dict):
        raise ValueError("Each operation must be an object")
    action = operation.get("action")
    if action not in ("set", "delete"):
        raise ValueError("Operation action must be set or delete")
    schema = _OPERATION["oneOf"][0 if action == "set" else 1]
    _check_fields(operation, schema)
    if action == "set":
        points = operation["keyframes"]
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_KEYFRAMES:
            raise ValueError(f"keyframes must contain 1..{MAX_KEYFRAMES} points")
    else:
        times = operation.get("times")
        if times is not None and (
            not isinstance(times, list) or not 1 <= len(times) <= MAX_KEYFRAMES
            or any(not isinstance(t, str) for t in times)
        ):
            raise ValueError(f"times must be null or contain 1..{MAX_KEYFRAMES} rational time strings")


def _apply(editor: KeyframeEditor, operation: dict) -> dict:
    if operation["action"] == "set":
        return editor.set_keyframes(operation["clip_path"], operation["property"],
                                    operation["keyframes"], operation.get("mode", "merge"))
    return editor.delete_keyframes(operation["clip_path"], operation["property"],
                                   operation.get("times"))


def _output_path(filepath: str, requested: str | None, *, suffix: str = "_keyframes") -> Path:
    srv = _common.tools.server_module()
    source = Path(filepath)
    if requested is None:
        stem = source.stem + suffix
        candidate = source.with_name(stem + source.suffix)
        number = 2
        while os.path.lexists(candidate):
            candidate = source.with_name(f"{stem}_{number}{source.suffix}")
            number += 1
    else:
        if "\x00" in requested:
            raise ValueError("Invalid output path: null byte detected")
        candidate = Path(requested)
    # Check the lexical destination too: a dangling symlink must not be followed.
    if os.path.lexists(candidate):
        raise ValueError("Output already exists; choose a new output_path")
    candidate = candidate.resolve()
    if candidate == source or any(p.suffix.lower() == ".fcpxmld" for p in candidate.parents):
        raise ValueError("Output must not replace the input or write inside an existing bundle")
    expected = ".fcpxmld" if source.is_dir() else ".fcpxml"
    if candidate.suffix.lower() != expected:
        raise ValueError(f"Output must use {expected} to preserve the input format and sidecars")
    # Reuse the same source-directory sandbox and journal seam as other writers.
    return Path(srv._validate_output_path(
        str(candidate), anchor_dir=str(source.parent), note_output=False,
    ))


def _publish(editor: KeyframeEditor, filepath: str, output: Path) -> dict:
    """Save once, DTD-check off to the side, then publish without replacement."""
    with tempfile.TemporaryDirectory(prefix=".keyframes-", dir=Path(filepath).parent) as staging:
        staged = Path(staging) / output.name
        editor.modifier.save(str(staged))
        ok, detail = dtd.validate_against_dtd(str(staged))
        if ok is False:
            raise ValueError(f"Apple DTD validation failed; no output published: {detail}")
        if staged.is_dir():
            # mkdir is exclusive even if another request claimed this path since
            # _output_path. Only clean up the directory THIS request created.
            output.mkdir()
            try:
                shutil.copytree(staged, output, dirs_exist_ok=True)
            except BaseException:
                shutil.rmtree(output)
                raise
        else:
            # Atomic no-replace publication: link fails for existing files and
            # symlinks rather than silently overwriting someone else's work.
            os.link(staged, output)
    _common.tools.server_module()._journal.note_output(str(output))
    return {"apple_dtd": "passed" if ok else "unavailable", "detail": detail}


def _result(result: dict) -> list:
    return _common.text_result(json.dumps(result, ensure_ascii=False, allow_nan=False))


async def handle_list_keyframes(arguments: dict) -> list:
    _check_fields(arguments, ACTION_SCHEMAS["list_keyframes"])
    _, editor = _editor(arguments)
    return _result(editor.list_keyframes(arguments.get("clip_path")))


async def _write(arguments: dict, action: str) -> list:
    _check_fields(arguments, ACTION_SCHEMAS[action])
    if action == "batch_keyframes":
        operations = arguments["operations"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
            raise ValueError(f"operations must contain 1..{MAX_OPERATIONS} operations")
    else:
        operations = [{"action": "set" if action == "set_keyframes" else "delete",
                       **{k: v for k, v in arguments.items() if k not in ("filepath", "output_path")}}]
    for operation in operations:
        _check_operation(operation)
    if sum(len(op.get("keyframes") or op.get("times") or []) for op in operations) > MAX_BATCH_KEYFRAMES:
        raise ValueError(f"A batch may contain at most {MAX_BATCH_KEYFRAMES} keyframes/times")
    filepath, editor = _editor(arguments)
    # No destination is registered until every operation succeeds, so a failed
    # batch cannot create an output or a misleading journal entry.
    results = [_apply(editor, op) for op in operations]
    output = _output_path(filepath, arguments.get("output_path"))
    validation = _publish(editor, filepath, output)
    return _result({"output_path": str(output), "operations": results, "validation": validation,
                    "verification": "FCP import and animation playback remain unverified. Proxy previews do not evaluate keyframes."})


async def handle_set_keyframes(arguments: dict) -> list:
    return await _write(arguments, "set_keyframes")


async def handle_delete_keyframes(arguments: dict) -> list:
    return await _write(arguments, "delete_keyframes")


async def handle_batch_keyframes(arguments: dict) -> list:
    return await _write(arguments, "batch_keyframes")
