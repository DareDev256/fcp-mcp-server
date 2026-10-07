"""Conservative intrinsic keyframe editing on the original FCPXML tree.

Times in the public API are clip-relative rational seconds (or project frames).
The XML curve uses the clip's source clock. Only a provable 1:1 time mapping is
editable; unsupported structures remain inspectable and are never guessed.
This module does not render or claim Final Cut import/playback verification.
"""

import copy
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from fractions import Fraction

from .rational import parse_seconds
from .writer import FCPXMLModifier

# Intrinsic adjustment params match the case-sensitive attribute name in their
# name attribute. Apple's Adjustment Attributes reference says key is ignored
# for these built-ins (unlike generic effect/plugin parameters).
_PROPERTIES = {
    "position": ("adjust-transform", "position", "0 0"),
    "scale": ("adjust-transform", "scale", "1 1"),
    "rotation": ("adjust-transform", "rotation", "0"),
    "opacity": ("adjust-blend", "amount", "1"),
    "volume": ("adjust-volume", "amount", "0dB"),
}
_CLIP_TAGS = {"asset-clip", "video", "audio", "clip", "ref-clip", "mc-clip", "sync-clip", "title"}
_ORDINARY = {"asset-clip", "video", "audio"}
# Apple 1.10-1.14 DTD order. Video's leading param* and the newer intrinsic
# nodes matter: writer._dtd_insert's legacy ordering omits them.
_PREFIX_ORDER = (
    "param", "note", "conform-rate", "timeMap", "object-tracker",
    "adjust-crop", "adjust-corners", "adjust-conform", "adjust-transform",
    "adjust-blend", "adjust-stabilization", "adjust-rollingShutter",
    "adjust-360-transform", "adjust-reorient", "adjust-orientation",
    "adjust-cinematic", "adjust-colorConform", "adjust-stereo-3D",
    "adjust-volume", "adjust-panner",
)
_TIME = re.compile(r"\d+(?:/\d+)?s\Z")


def _format_seconds(value: Fraction) -> str:
    """Serialize exactly, without the frame snapping of rational.format_seconds."""
    return f"{value.numerator}s" if value.denominator == 1 else f"{value.numerator}/{value.denominator}s"


def _seconds(value: str) -> Fraction:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("time must be nonnegative rational seconds, e.g. '1s' or '1001/30000s'")
    return parse_seconds(value)


def _number(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("keyframe values must be finite numbers (not strings or booleans)")
    try:
        if not math.isfinite(value):
            raise ValueError("keyframe values must be finite numbers")
    except OverflowError as exc:
        raise ValueError("keyframe value is too large") from exc
    return str(value)


def _encode_value(prop: str, value) -> str:
    if prop in ("position", "scale"):
        if prop == "scale" and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = [value, value]
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{prop} value must be a two-number [x, y] vector")
        encoded = [_number(component) for component in value]
        if prop == "scale" and any(component <= 0 for component in value):
            raise ValueError("scale values must be positive multipliers")
        return " ".join(encoded)
    text = _number(value)
    if prop == "opacity" and not 0 <= value <= 1:
        raise ValueError("opacity must be between 0 and 1")
    return text + "dB" if prop == "volume" else text


def _decode_value(prop: str, value: str):
    try:
        if prop in ("position", "scale"):
            parts = value.split()
            if len(parts) != 2:
                raise ValueError("expected a two-number vector")
            result = [float(part) for part in parts]
        else:
            if prop == "volume":
                if not value.endswith("dB"):
                    raise ValueError("expected a dB value")
                value = value[:-2]
            result = float(value)
        _encode_value(prop, result)
        return result
    except (TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"invalid existing {prop} value: {value!r}") from exc


def _insert_adjustment(clip: ET.Element, adjustment: ET.Element) -> None:
    priority = _PREFIX_ORDER.index(adjustment.tag)
    for index, child in enumerate(clip):
        rank = _PREFIX_ORDER.index(child.tag) if child.tag in _PREFIX_ORDER else len(_PREFIX_ORDER)
        if rank > priority:
            clip.insert(index, adjustment)
            return
    clip.append(adjustment)


class KeyframeEditor:
    """Inspect and edit native transform, opacity and volume curves in place."""

    def __init__(self, modifier: FCPXMLModifier):
        self.modifier = modifier

    def _inventory(self):
        root = self.modifier.root
        paths, parents = {}, {}

        def walk(element, path):
            paths[path] = element
            counts = defaultdict(int)
            for child in element:
                counts[child.tag] += 1
                parents[child] = element
                walk(child, f"{path}/{child.tag}[{counts[child.tag]}]")

        walk(root, f"/{root.tag}[1]")
        return paths, parents

    def _context(self, clip, parents):
        ancestors = []
        node = parents.get(clip)
        while node is not None:
            ancestors.append(node)
            node = parents.get(node)
        sequence = next((node for node in ancestors if node.tag == "sequence"), None)
        project = parents.get(sequence)
        reasons = []
        if sequence is None or project is None or project.tag != "project":
            reasons.append("Only clips in a project sequence are supported")
        if clip.tag not in _ORDINARY:
            reasons.append(f"<{clip.tag}> is unsupported; compound, multicam, sync and title clips require separate mapping")
        parent = parents.get(clip)
        direct = parent is not None and parent.tag == "spine" and parents.get(parent) is sequence
        host = None
        if not direct:
            host = parent
            host_parent = parents.get(host)
            connected = (
                host is not None and host.tag in _ORDINARY | {"gap"}
                and host_parent is not None and host_parent.tag == "spine"
                and parents.get(host_parent) is sequence
                and clip.get("lane") not in (None, "0")
            )
            if not connected:
                reasons.append("Only primary-storyline clips and one ordinary connected layer are supported")
        related = [clip] + ancestors[:ancestors.index(sequence)] if sequence in ancestors else [clip]
        if any(node.find("timeMap") is not None or node.find("conform-rate") is not None for node in related):
            reasons.append("timeMap/conform-rate on the clip or its host is unsupported")
        resources = self.modifier.root.find("resources")
        by_id = {node.get("id"): node for node in resources} if resources is not None else {}
        frame_duration = None
        try:
            fmt = by_id.get(sequence.get("format")) if sequence is not None else None
            if fmt is None or not fmt.get("frameDuration"):
                raise ValueError("missing sequence frameDuration")
            frame_duration = parse_seconds(fmt.get("frameDuration"))
            if frame_duration <= 0:
                raise ValueError("invalid sequence frameDuration")
        except ValueError as exc:
            reasons.append(str(exc))
        asset = by_id.get(clip.get("ref"))
        if asset is None or asset.tag != "asset":
            reasons.append("Clip must reference a source asset, not a generator/effect or compound media")
        for node in (clip, host):
            if node is None or node.tag not in _ORDINARY:
                continue
            source = by_id.get(node.get("ref"))
            if source is None or source.tag != "asset":
                if node is host:
                    reasons.append("Connected host must reference an ordinary asset")
                continue
            # Audio-only elements have no video rate to conform.
            if node.tag != "audio" and node.get("srcEnable") != "audio" and source.get("hasVideo") == "1":
                format_ids = {source.get("format")}
                if node.get("format"):
                    format_ids.add(node.get("format"))
                for format_id in format_ids:
                    fmt = by_id.get(format_id)
                    if fmt is None or not fmt.get("frameDuration"):
                        reasons.append("Source video frame rate is unknown; 1:1 time mapping cannot be proven")
                        break
                    try:
                        if parse_seconds(fmt.get("frameDuration")) != frame_duration:
                            reasons.append("Source/clip and project frame rates differ; implicit rate conform is unsupported")
                            break
                    except ValueError:
                        reasons.append("Source video frameDuration is invalid")
                        break
        start = duration = None
        try:
            start = parse_seconds(clip.get("start", "0s"))
            if clip.get("duration") is None:
                raise ValueError("Explicit clip duration is required")
            duration = parse_seconds(clip.get("duration"))
            if duration <= 0:
                raise ValueError("Clip duration must be positive")
        except ValueError as exc:
            reasons.append(str(exc))
        return {
            "reasons": list(dict.fromkeys(reasons)), "sequence": sequence,
            "frame_duration": frame_duration, "asset": asset, "start": start, "duration": duration,
        }

    @staticmethod
    def _property_reasons(clip, context, prop):
        reasons = list(context["reasons"])
        asset = context["asset"]
        if prop == "volume":
            if clip.tag == "video" or clip.get("srcEnable") == "video" or asset is None or asset.get("hasAudio") != "1":
                reasons.append("This clip has no enabled audio component")
            if clip.get("audioStart") is not None or clip.get("audioDuration") is not None:
                reasons.append("Split audio/video edit ranges are unsupported for volume keyframes")
        elif clip.tag == "audio" or clip.get("srcEnable") == "audio" or asset is None or asset.get("hasVideo") != "1":
            reasons.append("This clip has no enabled video component")
        tag, name, _default = _PROPERTIES[prop]
        for adjustment in clip.findall(tag):
            if adjustment.get("enabled") == "0":
                reasons.append(f"<{tag}> is disabled; enable it in Final Cut before editing keyframes")
            for param in adjustment.findall("param"):
                if param.get("name") == name and param.get("enabled") == "0":
                    reasons.append(f"{name} parameter is disabled; enable it in Final Cut before editing keyframes")
        return reasons

    @staticmethod
    def _curve(clip, prop):
        tag, name, _default = _PROPERTIES[prop]
        adjustments = clip.findall(tag)
        if len(adjustments) > 1:
            raise ValueError(f"Multiple <{tag}> elements are ambiguous")
        adjustment = adjustments[0] if adjustments else None
        if adjustment is None:
            return None, None, None
        if adjustment.get("tracking") is not None:
            raise ValueError("Object-tracked transforms are unsupported")
        params = [p for p in adjustment.findall("param") if p.get("name") == name]
        if len(params) > 1:
            raise ValueError(f"Multiple {name} parameters are ambiguous")
        param = params[0] if params else None
        if param is None:
            return adjustment, None, None
        if param.find("param") is not None:
            raise ValueError("Nested component parameters are unsupported for editing this curve")
        animations = param.findall("keyframeAnimation")
        if len(animations) > 1:
            raise ValueError("Multiple keyframeAnimation elements are ambiguous")
        return adjustment, param, animations[0] if animations else None

    @staticmethod
    def _points(animation, prop, start, duration):
        if animation is None:
            return []
        points, seen = [], set()
        for point in animation:
            if point.tag != "keyframe":
                raise ValueError("Unexpected child in keyframeAnimation")
            if point.get("time") is None or point.get("value") is None:
                raise ValueError("Existing keyframe is missing time or value")
            source_time = parse_seconds(point.get("time"))
            if source_time in seen:
                raise ValueError("Existing curve has duplicate keyframe times")
            seen.add(source_time)
            relative = source_time - start
            points.append({
                "time": _format_seconds(relative), "source_time": point.get("time"),
                "value": _decode_value(prop, point.get("value")),
                "interp": point.get("interp"), "curve": point.get("curve"),
                "in_range": 0 <= relative < duration,
                "attributes": dict(point.attrib),
            })
        return sorted(points, key=lambda point: parse_seconds(point["time"]))

    def _describe(self, path, clip, parents):
        context = self._context(clip, parents)
        properties = {}
        for prop in _PROPERTIES:
            reasons = self._property_reasons(clip, context, prop)
            points, static = [], _PROPERTIES[prop][2]
            try:
                adjustment, param, animation = self._curve(clip, prop)
                if adjustment is not None:
                    static = adjustment.get(_PROPERTIES[prop][1], static)
                if param is not None and param.get("value") is not None:
                    static = param.get("value")
                if context["start"] is not None and context["duration"] is not None:
                    points = self._points(animation, prop, context["start"], context["duration"])
            except ValueError as exc:
                reasons.append(str(exc))
            properties[prop] = {
                "supported": not reasons, "unsupported_reasons": reasons,
                "static_value": static, "keyframes": points,
            }
        return {
            "clip_path": path, "name": clip.get("name", ""), "tag": clip.tag,
            "start": clip.get("start", "0s"), "duration": clip.get("duration"),
            "frame_duration": _format_seconds(context["frame_duration"]) if context["frame_duration"] else None,
            "supported": any(p["supported"] for p in properties.values()),
            "unsupported_reasons": context["reasons"], "properties": properties,
        }

    def list_keyframes(self, clip_path: str | None = None) -> dict:
        """Return unique paths, capability reasons and existing native curves."""
        paths, parents = self._inventory()
        if clip_path is not None:
            if not isinstance(clip_path, str) or clip_path not in paths or paths[clip_path].tag not in _CLIP_TAGS:
                raise ValueError("clip_path must exactly match a clip_path returned by list_keyframes")
            items = [(clip_path, paths[clip_path])]
        else:
            items = []
            for path, element in paths.items():
                if element.tag not in _CLIP_TAGS:
                    continue
                node = parents.get(element)
                while node is not None and node.tag != "project":
                    node = parents.get(node)
                if node is not None:
                    items.append((path, element))
        return {
            "time_basis": "clip-relative", "properties": list(_PROPERTIES),
            "clips": [self._describe(path, element, parents) for path, element in items],
        }

    def _editable(self, clip_path, prop):
        if not isinstance(prop, str) or prop not in _PROPERTIES:
            raise ValueError(f"Unknown property {prop!r}; expected one of {', '.join(_PROPERTIES)}")
        paths, parents = self._inventory()
        if not isinstance(clip_path, str) or clip_path not in paths or paths[clip_path].tag not in _CLIP_TAGS:
            raise ValueError("clip_path must exactly match a clip_path returned by list_keyframes")
        clip = paths[clip_path]
        context = self._context(clip, parents)
        reasons = self._property_reasons(clip, context, prop)
        if reasons:
            raise ValueError("Unsupported keyframe target: " + "; ".join(reasons))
        self._points(self._curve(clip, prop)[2], prop, context["start"], context["duration"])
        return clip, context

    @staticmethod
    def _input_time(point, context):
        if ("time" in point) == ("frame" in point):
            raise ValueError("Each keyframe requires exactly one of time or frame")
        if "frame" in point:
            frame = point["frame"]
            if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
                raise ValueError("frame must be a nonnegative integer in the project frame rate")
            relative = frame * context["frame_duration"]
        else:
            relative = _seconds(point["time"])
        if not 0 <= relative < context["duration"]:
            raise ValueError("Keyframe time must satisfy 0 <= time < clip duration")
        return relative

    @staticmethod
    def _linear_attributes(point, prop):
        # Verified with a Final Cut Pro 12.4 import/re-export. The general DTD
        # allows interp on every point, but FCP warns and discards it for these
        # intrinsic curves. Rotation/volume export neither interpolation field.
        # Existing points keep their attributes unless linear is explicitly asked.
        point.attrib.pop("interp", None)
        point.attrib.pop("curve", None)
        if prop in ("position", "scale", "opacity"):
            point.set("curve", "linear")

    def set_keyframes(self, clip_path: str, property: str, keyframes: list[dict], mode="merge") -> dict:
        """Merge points or replace one curve; validate everything before mutation."""
        clip, context = self._editable(clip_path, property)
        if mode not in ("merge", "replace"):
            raise ValueError("mode must be 'merge' or 'replace'")
        if not isinstance(keyframes, list) or not keyframes:
            raise ValueError("keyframes must be a nonempty list; use delete_keyframes to clear a curve")
        incoming, seen = [], set()
        for point in keyframes:
            if not isinstance(point, dict) or set(point) - {"time", "frame", "value", "interp", "curve"}:
                raise ValueError("Each keyframe accepts only time/frame, value, interp and curve")
            if "value" not in point:
                raise ValueError("Each keyframe requires a value")
            relative = self._input_time(point, context)
            if relative in seen:
                raise ValueError("Duplicate keyframe time in input")
            seen.add(relative)
            for field in ("interp", "curve"):
                if field in point and point[field] != "linear":
                    raise ValueError(f"Only linear {field} is supported for new edits")
            incoming.append((relative + context["start"], _encode_value(property, point["value"]), point))

        original_adjustment, _original_param, _original_animation = self._curve(clip, property)
        adjustment = copy.deepcopy(original_adjustment) if original_adjustment is not None else ET.Element(_PROPERTIES[property][0])
        # Work on an isolated adjustment so validation failures cannot partially edit the tree.
        holder = ET.Element(clip.tag)
        holder.append(adjustment)
        _, param, animation = self._curve(holder, property)
        if param is None:
            param = ET.Element("param", name=_PROPERTIES[property][1])
            # adjust-blend's reserved? is required to come after param*.
            reserved = adjustment.find("reserved")
            adjustment.insert(list(adjustment).index(reserved) if reserved is not None else len(adjustment), param)
        if animation is None:
            animation = ET.Element("keyframeAnimation")
            index = next((i for i, child in enumerate(param) if child.tag not in ("fadeIn", "fadeOut")), len(param))
            param.insert(index, animation)
        existing = {parse_seconds(point.get("time")): point for point in animation}
        retained = existing if mode == "merge" else {}
        for source_time, value, incoming_point in incoming:
            if source_time in existing:
                point = copy.deepcopy(existing[source_time])
            else:
                point = ET.Element("keyframe")
                self._linear_attributes(point, property)
            point.set("time", _format_seconds(source_time))
            point.set("value", value)
            if "interp" in incoming_point or "curve" in incoming_point:
                self._linear_attributes(point, property)
            retained[source_time] = point
        animation[:] = [retained[time] for time in sorted(retained)]
        result = self._points(animation, property, context["start"], context["duration"])
        if original_adjustment is None:
            _insert_adjustment(clip, adjustment)
        else:
            index = list(clip).index(original_adjustment)
            clip.remove(original_adjustment)
            clip.insert(index, adjustment)
        return {"clip_path": clip_path, "property": property, "mode": mode, "updated": len(incoming), "keyframes": result}

    def delete_keyframes(self, clip_path: str, property: str, times: list[str] | None = None) -> dict:
        """Delete selected relative times or clear the curve, preserving static values."""
        clip, context = self._editable(clip_path, property)
        selected = None
        if times is not None:
            if not isinstance(times, list) or not times:
                raise ValueError("times must be a nonempty list, or null to delete the entire curve")
            relative = [self._input_time({"time": time}, context) for time in times]
            if len(set(relative)) != len(relative):
                raise ValueError("Duplicate keyframe time in input")
            selected = {time + context["start"] for time in relative}
        _adjustment, param, animation = self._curve(clip, property)
        if animation is None:
            return {"clip_path": clip_path, "property": property, "removed": 0, "keyframes": []}
        remaining = [point for point in animation if selected is not None and parse_seconds(point.get("time")) not in selected]
        removed = len(animation) - len(remaining)
        if remaining:
            animation[:] = remaining
        else:
            param.remove(animation)
            animation = None
        return {"clip_path": clip_path, "property": property, "removed": removed, "keyframes": self._points(animation, property, context["start"], context["duration"])}
