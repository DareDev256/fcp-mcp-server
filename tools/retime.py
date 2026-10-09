"""Speed curve MCP adapters, using the same guarded publisher as keyframes."""

from pathlib import Path

from mcp.types import Tool

from fcpxml.retime import RetimeEditor
from tools import _common
from tools.keyframes import (
    _CLIP,
    _FILE,
    _TIME,
    _check_fields,
    _output_path,
    _publish,
    _result,
    _schema,
)

_POINT = _schema({
    "time": {**_TIME, "description": "Output clip-relative rational time, on a project frame boundary."},
    "frame": {"type": "integer", "minimum": 0, "description": "Output clip-relative project frame index."},
    "speed": {"type": "number", "exclusiveMinimum": 0, "maximum": 100,
              "description": "Playback multiplier: 0.5 half speed, 2 double speed; forward only."},
    "transition": {"type": "string", "enum": ["hold", "linear"], "default": "linear",
                   "description": "Outgoing segment: hold this speed or linearly ramp to the next. Final point ends the clip."},
}, ["speed"])
_POINT["oneOf"] = [{"required": ["time"]}, {"required": ["frame"]}]
_RIPPLE = {"type": "boolean", "default": True,
           "description": "Shift following spine clips by the duration change. False requires unchanged duration."}
_OUTPUT = {"type": "string", "description": "New file/bundle under the input directory; never overwrite. Defaults to numbered _retimed sibling."}
ACTION_SCHEMAS = {
    "list_speed_points": _schema({"filepath": _FILE, "clip_path": _CLIP}, ["filepath"]),
    "set_speed_curve": _schema({
        "filepath": _FILE, "clip_path": _CLIP, "output_path": _OUTPUT,
        "speed_keyframes": {"type": "array", "minItems": 2, "maxItems": 1000,
                            "items": _POINT, "description": "Ordered points, starting at zero. Last time is output duration. Integrating speed determines source consumed, which may extend the old trim within the asset."},
        "ripple": _RIPPLE,
        "preserve_pitch": {"type": "boolean", "default": True,
                           "description": "Write FCP native preservesPitch for retimed audio."},
        "frame_sampling": {"type": "string", "enum": ["floor", "nearest-neighbor", "frame-blending", "optical-flow-classic", "optical-flow"],
                           "default": "floor", "description": "FCP frame sampling mode; optical flow is evaluated by FCP, not this server."},
    }, ["filepath", "clip_path", "speed_keyframes"]),
    "reset_speed": _schema({"filepath": _FILE, "clip_path": _CLIP,
                            "output_path": _OUTPUT, "ripple": _RIPPLE}, ["filepath", "clip_path"]),
}
_DESCRIPTIONS = {
    "list_speed_points": "Read native speed/time maps, exact rational source positions, and unsupported reasons using unique clip paths. No writes.",
    "set_speed_curve": "Set forward speed control points on an ordinary primary-storyline clip. Holds and linear speed ramps (integrated at every project frame, max 10000 map points). Existing intrinsic animations retain clip-relative timing; shorter durations can hide retained keys. Writes a new DTD-checked file; verify playback in FCP.",
    "reset_speed": "Restore 1x over the current source range, snapping each endpoint to the nearest source frame and reporting exact adjustments. Ripple following clips; preserve clip-relative intrinsic animation timing. This removes retiming; it is not undo of an earlier source trim.",
}


def tool_schemas() -> list[Tool]:
    return [Tool(name=name, description=_DESCRIPTIONS[name], inputSchema=schema)
            for name, schema in ACTION_SCHEMAS.items()]


def group_schema_extensions(actions: list[str]) -> dict:
    clauses = [{"if": {"properties": {"action": {"const": name}}, "required": ["action"]},
                "then": {"properties": {"args": schema}, "required": ["args"]}}
               for name, schema in ACTION_SCHEMAS.items() if name in actions]
    return {"allOf": clauses} if clauses else {}


def _editor(arguments: dict) -> tuple[str, RetimeEditor]:
    srv = _common.tools.server_module()
    filepath = srv._validate_filepath(arguments["filepath"], (".fcpxml", ".fcpxmld"))
    if any(p.suffix.lower() == ".fcpxmld" for p in Path(filepath).parents):
        raise ValueError("Pass the .fcpxmld bundle root to preserve sidecars")
    return filepath, RetimeEditor(srv.FCPXMLModifier(filepath))


async def handle_list_speed_points(arguments: dict) -> list:
    _check_fields(arguments, ACTION_SCHEMAS["list_speed_points"])
    _, editor = _editor(arguments)
    return _result(editor.list_speed_points(arguments.get("clip_path")))


async def _write(arguments: dict, action: str) -> list:
    _check_fields(arguments, ACTION_SCHEMAS[action])
    filepath, editor = _editor(arguments)
    if action == "set_speed_curve":
        result = editor.set_speed_curve(
            arguments["clip_path"], arguments["speed_keyframes"],
            ripple=arguments.get("ripple", True),
            preserve_pitch=arguments.get("preserve_pitch", True),
            frame_sampling=arguments.get("frame_sampling", "floor"),
        )
    else:
        result = editor.reset_speed(arguments["clip_path"], ripple=arguments.get("ripple", True))
    output = _output_path(filepath, arguments.get("output_path"), suffix="_retimed")
    validation = _publish(editor, filepath, output)
    return _result({"output_path": str(output), "operation": result, "validation": validation,
                    "verification": "This output's FCP import/playback is unverified. Proxy previews do not evaluate variable retiming or intrinsic animation."})


async def handle_set_speed_curve(arguments: dict) -> list:
    return await _write(arguments, "set_speed_curve")


async def handle_reset_speed(arguments: dict) -> list:
    return await _write(arguments, "reset_speed")
