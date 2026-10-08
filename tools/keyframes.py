"""MCP adapters for intrinsic keyframes; validate before publishing a new file."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from mcp.types import Tool

from fcpxml import dtd
from fcpxml.animation_curves import (
    CURVE_PROPERTIES,
    DEFAULT_TOLERANCE,
    EASINGS,
    MAX_AUTHORED_POINTS,
    MAX_GENERATED_KEYFRAMES,
    MAX_SAMPLED_FRAMES,
)
from fcpxml.keyframes import KEYFRAME_CURVE_OPTIONS, KeyframeEditor
from tools import _common

MAX_OPERATIONS = 100
MAX_KEYFRAMES = 1000
MAX_BATCH_KEYFRAMES = 10000
PROPERTIES = list(KEYFRAME_CURVE_OPTIONS)

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


def _curve_modes(field: str) -> list[str]:
    return list(dict.fromkeys(mode for options in KEYFRAME_CURVE_OPTIONS.values() for mode in options[field]))


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
            "interp": {"type": "string", "enum": _curve_modes("interp"),
                       "description": (
                           "Native temporal interpolation: opacity supports ease/easeIn/easeOut; other properties accept linear only. "
                           "Serialized without per-frame baking. "
                           "Omit to preserve an existing point; new points default to linear. "
                           "Explicit linear normalizes interpolation attributes per property. "
                           "For nonlinear opacity interp, omit curve; its native encoding removes that attribute."
                       )},
            "curve": {"type": "string", "enum": _curve_modes("curve"),
                      "description": (
                          "Compatibility input for native linear normalization; only linear is accepted. "
                          "Omit to preserve an existing point; new points default to linear. "
                          "Native spatial smooth/Bezier handles are not exposed by this action."
                      )},
        },
        "required": ["value"],
        "oneOf": [{"required": ["time"]}, {"required": ["frame"]}],
    },
}
_CURVE_POINTS = {
    "type": "array", "minItems": 2, "maxItems": MAX_AUTHORED_POINTS,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "time": _TIME,
            "frame": {"type": "integer", "minimum": 0},
            "value": _KEYFRAMES["items"]["properties"]["value"],
            "control_in": {**_KEYFRAMES["items"]["properties"]["value"],
                           "description": "Incoming cubic Bezier control in absolute property units, not an offset. Omit on the first point."},
            "control_out": {**_KEYFRAMES["items"]["properties"]["value"],
                            "description": "Outgoing cubic Bezier control in absolute property units, not an offset. Omit on the last point."},
            "easing": {"type": "string", "enum": list(EASINGS),
                       "description": "Outgoing segment parameter: linear u; ease 3u^2-2u^3; easeIn u^2; easeOut 2u-u^2. Omit on last point. Defaults to linear."},
        },
        "required": ["value"],
        "oneOf": [{"required": ["time"]}, {"required": ["frame"]}],
    },
}
_ANIMATION_CURVE = {
    "clip_path": _CLIP,
    "property": {
        "type": "string", "enum": list(CURVE_PROPERTIES),
        "description": (
            "position uses native FCPXML [x,y] coordinates; scale uses positive multipliers "
            "(one number or [x,y]); rotation degrees; opacity 0..1. "
            "Volume is not supported; use set_keyframes for audio levels."
        ),
    },
    "points": _CURVE_POINTS,
    "tolerance": {
        "type": "number", "exclusiveMinimum": 0,
        "description": (
            "Maximum Euclidean vector / absolute scalar error at project frames, in property units. "
            f"Defaults: {', '.join(f'{prop} {value:g}' for prop, value in DEFAULT_TOLERANCE.items())}. "
            f"Smaller values emit more keys; limits {MAX_SAMPLED_FRAMES} evaluated frames and "
            f"{MAX_GENERATED_KEYFRAMES} output keys. Not a pixel or between-frame bound."
        ),
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


def _point_mode_schema(options: dict) -> dict:
    schema = {"properties": {field: {"enum": list(modes)} for field, modes in options.items()}}
    nonlinear = [mode for mode in options["interp"] if mode != "linear"]
    if nonlinear:
        schema["not"] = {"properties": {"interp": {"enum": nonlinear}}, "required": ["interp", "curve"]}
    return schema


def _set_schema(properties: dict, required: list[str]) -> dict:
    schema = _schema(properties, required)
    schema["allOf"] = [{
        "if": {"properties": {"property": {"const": prop}}, "required": ["property"]},
        "then": {"properties": {"keyframes": {"items": _point_mode_schema(options)}}},
    } for prop, options in KEYFRAME_CURVE_OPTIONS.items()]
    return schema


_SET = {"clip_path": _CLIP, "property": _PROPERTY, "keyframes": _KEYFRAMES, "mode": _MODE}
_DELETE = {"clip_path": _CLIP, "property": _PROPERTY, "times": _TIMES}
_OPERATION = {"oneOf": [
    _set_schema({"action": {"const": "set"}, **_SET}, ["action", "clip_path", "property", "keyframes"]),
    _schema({"action": {"const": "delete"}, **_DELETE}, ["action", "clip_path", "property"]),
]}
ACTION_SCHEMAS = {
    "set_animation_curve": _schema({"filepath": _FILE, **_ANIMATION_CURVE, "output_path": _OUTPUT},
                                   ["filepath", "clip_path", "property", "points"]),
    "list_keyframes": _schema({"filepath": _FILE, "clip_path": _CLIP}, ["filepath"]),
    "set_keyframes": _set_schema({"filepath": _FILE, **_SET, "output_path": _OUTPUT},
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
    "set_animation_curve": f"Replace one {'/'.join(CURVE_PROPERTIES)} animation using cubic Bezier controls and easing. Emits an adaptive linear approximation within the requested project-frame error, usually fewer keys than frame-by-frame baking. Controls use absolute property values; missing handles follow the straight chord. Not native Bezier handles, audio automation, or a speed/timeMap curve. All authored times must be increasing project-frame positions within the clip. Saves a new file after available Apple DTD validation.",
    "list_keyframes": "Read intrinsic animation and unique clip paths, including unsupported structures; no writes.",
    "set_keyframes": "Set native position, scale, rotation, opacity or volume keyframes on one uniquely selected clip. Only opacity supports native ease/easeIn/easeOut interpolation; it retains sparse points without per-frame baking. Other properties remain linear, and volume interpolates gain with dB values. Writes a new file after available Apple DTD validation.",
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


async def handle_set_animation_curve(arguments: dict) -> list:
    _check_fields(arguments, ACTION_SCHEMAS["set_animation_curve"])
    filepath, editor = _editor(arguments)
    result = editor.set_animation_curve(
        arguments["clip_path"], arguments["property"], arguments["points"],
        arguments.get("tolerance"),
    )
    output = _output_path(filepath, arguments.get("output_path"))
    validation = _publish(editor, filepath, output)
    return _result({"output_path": str(output), "operations": [result], "validation": validation,
                    "verification": "Adaptive linear approximation; no native Bezier handles. Error is bounded at project frames before FCP serialization. Import/playback of this output remains unverified."})
