"""Exact, conservative speed curves for ordinary primary-storyline clips.

A speed curve is integrated into source time. Linear speed ramps are sampled
at every project-frame boundary and written as native linear timeMap segments;
this deliberately does not invent Final Cut's undocumented smooth2 handles.
"""

import math
import xml.etree.ElementTree as ET
from bisect import bisect_right
from collections import defaultdict
from fractions import Fraction

from .rational import format_exact_seconds, parse_seconds, parse_strict_seconds
from .writer import FCPXMLModifier

MAX_MAP_POINTS = 10_000
MAX_SPEED_POINTS = 1_000
_ORDINARY = {"asset-clip", "video", "audio"}
_CLIPS = _ORDINARY | {"clip", "ref-clip", "mc-clip", "sync-clip", "title"}
_TIMED = _CLIPS | {"gap", "spine", "transition", "caption"}
_ANIMATED_INTRINSICS = {"adjust-transform", "adjust-blend", "adjust-volume"}
_STATIC_INTRINSICS = {
    "adjust-crop", "adjust-corners", "adjust-conform", "adjust-panner",
    "adjust-colorConform", "adjust-360-transform", "adjust-reorient",
    "adjust-orientation", "adjust-stereo-3D",
}


def _native_time(value: Fraction) -> Fraction:
    # FCPXML's native CMTime representation is signed Int64 / UInt32.
    # Keeping an exact fraction that cannot be represented is not valid XML
    # interoperability; reject rather than silently rounding the requested ramp.
    if not -(2**63) <= value.numerator < 2**63 or value.denominator >= 2**32:
        raise ValueError("Exact time exceeds FCPXML's 64-bit numerator / 32-bit denominator; use simpler speeds or shorter times")
    return value


def _speed(value) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("speed must be a finite numeric multiplier, not a string or boolean")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError("speed must satisfy 0 < speed <= 100") from exc
    if not finite or not 0 < value <= 100:
        raise ValueError("speed must satisfy 0 < speed <= 100")
    return Fraction(str(value))


class LinearTimeMap:
    """A strictly positive, piecewise-linear map with exact bounded inversion."""

    def __init__(self, points):
        self.points = tuple((_native_time(Fraction(time)), _native_time(Fraction(value))) for time, value in points)
        if not 2 <= len(self.points) <= MAX_MAP_POINTS:
            raise ValueError(f"timeMap requires 2 to {MAX_MAP_POINTS} points")
        if any(t1 <= t0 or v1 <= v0 for (t0, v0), (t1, v1) in zip(self.points, self.points[1:])):
            raise ValueError("timeMap must have strictly increasing time and source values; reverse/freeze is unsupported")
        self._times = tuple(point[0] for point in self.points)
        self._values = tuple(point[1] for point in self.points)

    @classmethod
    def from_element(cls, element: ET.Element):
        if element.tag != "timeMap":
            raise ValueError("Expected a timeMap element")
        points = []
        for point in element:
            if point.tag != "timept" or set(point.attrib) - {"time", "value", "interp"}:
                raise ValueError("Unsupported timeMap point or smooth interpolation handles")
            # Apple's default is smooth2. Missing interp must not become linear.
            if point.get("interp") != "linear":
                raise ValueError("Only explicit linear timeMap interpolation is supported")
            if point.get("time") is None or point.get("value") is None:
                raise ValueError("timeMap points require time and value")
            points.append((parse_seconds(point.get("time")), parse_seconds(point.get("value"))))
        return cls(points)

    def _at(self, value: Fraction, *, inverse=False) -> Fraction:
        domain, codomain = (self._values, self._times) if inverse else (self._times, self._values)
        value = Fraction(value)
        if not domain[0] <= value <= domain[-1]:
            raise ValueError("Time lies outside the explicit timeMap range; extrapolation is unsupported")
        index = min(bisect_right(domain, value) - 1, len(domain) - 2)
        ratio = (value - domain[index]) / (domain[index + 1] - domain[index])
        return codomain[index] + ratio * (codomain[index + 1] - codomain[index])

    def source_at(self, local_absolute: Fraction) -> Fraction:
        return self._at(local_absolute)

    def local_at(self, source_absolute: Fraction) -> Fraction:
        return self._at(source_absolute, inverse=True)


def _inventory(root):
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


class RetimeEditor:
    """Inspect/replace/reset speed curves; errors never partially mutate XML."""

    def __init__(self, modifier: FCPXMLModifier):
        self.modifier = modifier

    def _context(self, clip, parents):
        reasons = []
        spine = parents.get(clip)
        sequence = parents.get(spine)
        project = parents.get(sequence)
        if (spine is None or spine.tag != "spine" or sequence is None
                or sequence.tag != "sequence" or project is None or project.tag != "project"
                or clip.get("lane") not in (None, "0")):
            reasons.append("Only direct primary-storyline clips in a project sequence are supported")
        if clip.tag not in _ORDINARY:
            reasons.append("Only ordinary asset-clip, video and audio elements are supported")
        resources = self.modifier.root.find("resources")
        by_id = {node.get("id"): node for node in resources} if resources is not None else {}
        asset = by_id.get(clip.get("ref"))
        if asset is None or asset.tag != "asset":
            reasons.append("Clip must reference an ordinary source asset")
        result = {"reasons": reasons, "spine": spine, "sequence": sequence, "asset": asset,
                  "start": None, "duration": None, "frame_duration": None, "source_start": None,
                  "source_end": None, "time_map": None}
        try:
            fmt = by_id.get(sequence.get("format")) if sequence is not None else None
            if fmt is None or not fmt.get("frameDuration"):
                raise ValueError("Project frameDuration is required")
            frame = _native_time(parse_seconds(fmt.get("frameDuration")))
            if frame <= 0:
                raise ValueError("Project frameDuration must be positive")
            result["frame_duration"] = frame
            start = _native_time(parse_seconds(clip.get("start", "0s")))
            if clip.get("duration") is None:
                raise ValueError("Explicit clip duration is required")
            duration = _native_time(parse_seconds(clip.get("duration")))
            if duration <= 0:
                raise ValueError("Clip duration must be positive")
            result.update(start=start, duration=duration)
            if asset is not None and asset.tag == "asset":
                if asset.get("duration") is None:
                    raise ValueError("Explicit source asset duration is required")
                asset_start = _native_time(parse_seconds(asset.get("start", "0s")))
                asset_duration = _native_time(parse_seconds(asset.get("duration")))
                if asset_duration <= 0:
                    raise ValueError("Source asset duration must be positive")
                result.update(asset_start=asset_start, asset_end=asset_start + asset_duration)
                if clip.tag != "audio" and clip.get("srcEnable") != "audio" and asset.get("hasVideo") == "1":
                    formats = {asset.get("format")}
                    if clip.get("format"):
                        formats.add(clip.get("format"))
                    for format_id in formats:
                        source_fmt = by_id.get(format_id)
                        if source_fmt is None or not source_fmt.get("frameDuration"):
                            raise ValueError("Source video frame rate is unknown")
                        if parse_seconds(source_fmt.get("frameDuration")) != frame:
                            raise ValueError("Source and project frame rates differ; implicit rate conform is unsupported")
            if any(clip.get(attr) is not None for attr in ("audioStart", "audioDuration")):
                reasons.append("Split audio/video edit ranges are unsupported")
            conforms = clip.findall("conform-rate")
            if len(conforms) > 1 or any(node.get("scaleEnabled") != "0" for node in conforms):
                reasons.append("Enabled or ambiguous conform-rate is unsupported")
            maps = clip.findall("timeMap")
            if len(maps) > 1:
                raise ValueError("Multiple timeMap elements are ambiguous")
            mapping = LinearTimeMap.from_element(maps[0]) if maps else None
            result["time_map"] = mapping
            source_start = mapping.source_at(start) if mapping else start
            source_end = mapping.source_at(start + duration) if mapping else start + duration
            result.update(source_start=source_start, source_end=source_end)
            if "asset_start" in result and not result["asset_start"] <= source_start < source_end <= result["asset_end"]:
                reasons.append("Existing visible source range is outside the asset bounds")
        except (ValueError, ZeroDivisionError) as exc:
            reasons.append(str(exc))
        for child in clip:
            if child.tag in {"timeMap", "conform-rate", "note", "metadata"}:
                continue
            if child.tag in _ANIMATED_INTRINSICS:
                if child.get("tracking") is not None:
                    reasons.append("Object-tracked intrinsic adjustments are unsupported")
                if child.find(".//fadeIn") is not None or child.find(".//fadeOut") is not None:
                    reasons.append("Intrinsic fade timing is unsupported during retiming")
                continue
            if child.tag in _STATIC_INTRINSICS and child.find(".//keyframeAnimation") is None:
                continue
            reasons.append(f"<{child.tag}> has unsupported child/effect/anchor timing for retiming")
        result["reasons"] = list(dict.fromkeys(reasons))
        return result

    def _target(self, clip_path, *, editable=True):
        paths, parents = _inventory(self.modifier.root)
        if not isinstance(clip_path, str) or clip_path not in paths or paths[clip_path].tag not in _CLIPS:
            raise ValueError("clip_path must exactly match a clip_path returned by list_speed_points")
        clip = paths[clip_path]
        context = self._context(clip, parents)
        if editable and context["reasons"]:
            raise ValueError("Unsupported speed target: " + "; ".join(context["reasons"]))
        return clip, context

    def _describe(self, path, clip, context):
        mapping = context["time_map"]
        points = []
        if mapping is not None and context["start"] is not None:
            for index, (time, value) in enumerate(mapping.points):
                speed = None
                if index + 1 < len(mapping.points):
                    next_time, next_value = mapping.points[index + 1]
                    speed = (next_value - value) / (next_time - time)
                points.append({"time": format_exact_seconds(time - context["start"]), "local_time": format_exact_seconds(time),
                               "source_time": format_exact_seconds(value), "speed": float(speed) if speed is not None else None,
                               "speed_ratio": str(speed) if speed is not None else None})
        raw_maps = [{"attributes": dict(node.attrib), "points": [dict(point.attrib) for point in node]}
                    for node in clip.findall("timeMap")]
        return {"clip_path": path, "name": clip.get("name", ""), "tag": clip.tag,
                "start": clip.get("start", "0s"), "duration": clip.get("duration"),
                "frame_duration": format_exact_seconds(context["frame_duration"]) if context["frame_duration"] else None,
                "supported": not context["reasons"], "unsupported_reasons": context["reasons"],
                "has_time_map": bool(raw_maps), "speed_points": points, "raw_time_maps": raw_maps,
                "source_start": format_exact_seconds(context["source_start"]) if context["source_start"] is not None else None,
                "source_end": format_exact_seconds(context["source_end"]) if context["source_end"] is not None else None,
                "preserve_pitch": clip.find("timeMap").get("preservesPitch", "1") != "0" if raw_maps else True}

    def list_speed_points(self, clip_path=None):
        if clip_path is not None:
            clip, context = self._target(clip_path, editable=False)
            clips = [self._describe(clip_path, clip, context)]
        else:
            paths, parents = _inventory(self.modifier.root)
            clips = []
            for path, clip in paths.items():
                if clip.tag not in _CLIPS:
                    continue
                ancestor = parents.get(clip)
                while ancestor is not None and ancestor.tag != "project":
                    ancestor = parents.get(ancestor)
                if ancestor is not None:
                    clips.append(self._describe(path, clip, self._context(clip, parents)))
        return {"time_basis": "clip-relative", "interpolation": "native piecewise-linear source time", "clips": clips}

    @staticmethod
    def _inputs(speed_keyframes, frame):
        if not isinstance(speed_keyframes, list) or not 2 <= len(speed_keyframes) <= MAX_SPEED_POINTS:
            raise ValueError(f"speed_keyframes must contain 2 to {MAX_SPEED_POINTS} points")
        points = []
        for point in speed_keyframes:
            if not isinstance(point, dict) or set(point) - {"time", "frame", "speed", "transition"}:
                raise ValueError("Each speed keyframe accepts only time/frame, speed and transition")
            if ("time" in point) == ("frame" in point):
                raise ValueError("Each speed keyframe requires exactly one of time or frame")
            if "frame" in point:
                index = point["frame"]
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise ValueError("frame must be a nonnegative integer")
                time = index * frame
            else:
                time = parse_strict_seconds(point["time"])
            time = _native_time(time)
            if (time / frame).denominator != 1:
                raise ValueError("Speed keyframe times must align to project-frame boundaries; use frame")
            transition = point.get("transition", "linear")
            if transition not in ("hold", "linear"):
                raise ValueError("transition must be 'hold' or 'linear'")
            points.append((time, _speed(point.get("speed")), transition))
        if points[0][0] != 0 or any(right[0] <= left[0] for left, right in zip(points, points[1:])):
            raise ValueError("Speed keyframes must begin at zero and have strictly increasing times")
        return points

    @staticmethod
    def _bake(points, frame, local_start, source_start):
        values = [(local_start, source_start)]
        consumed = Fraction(0)
        for (left, speed, transition), (right, next_speed, _next_transition) in zip(points, points[1:]):
            width = right - left
            ramp = transition == "linear" and speed != next_speed
            count = int(width / frame) if ramp else 1
            if len(values) + count > MAX_MAP_POINTS:
                raise ValueError(f"Speed curve would exceed {MAX_MAP_POINTS} emitted timeMap points")
            for index in range(1, count + 1):
                elapsed = index * frame if ramp else width
                integral = speed * elapsed
                if ramp:
                    integral += (next_speed - speed) * elapsed * elapsed / (2 * width)
                values.append((local_start + left + elapsed, source_start + consumed + integral))
            consumed = values[-1][1] - source_start
        return LinearTimeMap(values)

    @staticmethod
    def _ripple_plan(clip, context, new_duration, ripple):
        if not isinstance(ripple, bool):
            raise ValueError("ripple must be a boolean")
        delta = new_duration - context["duration"]
        if delta == 0:
            return [], None, delta
        if not ripple:
            raise ValueError("ripple=false requires the new duration to equal the current clip duration")
        spine, sequence = context["spine"], context["sequence"]
        if sequence.get("duration") is None:
            raise ValueError("Explicit sequence duration is required for ripple edits")
        sequence_duration = _native_time(parse_seconds(sequence.get("duration")) + delta)
        if sequence_duration <= 0:
            raise ValueError("Ripple would produce a nonpositive sequence duration")
        for child in sequence:
            if child is not spine and (child.get("start") is not None or child.get("offset") is not None):
                raise ValueError("Sequence-level timed annotations are unsupported for ripple edits")
        siblings = list(spine)
        target_index = siblings.index(clip)
        plan = []
        previous_end = None
        for index, sibling in enumerate(siblings):
            if sibling.tag not in _CLIPS | {"gap"} or sibling.get("lane") not in (None, "0"):
                raise ValueError("Transitions or non-primary spine elements are unsupported for ripple edits")
            if index < target_index and any(node.tag in _TIMED for node in sibling.iter() if node is not sibling):
                raise ValueError("Earlier connected clip extents are ambiguous for duration-changing ripple edits")
            if sibling.get("offset") is None or sibling.get("duration") is None:
                raise ValueError("Explicit sibling offsets and durations are required for ripple edits")
            offset = parse_seconds(sibling.get("offset"))
            duration = parse_seconds(sibling.get("duration"))
            if duration <= 0 or (previous_end is not None and offset < previous_end):
                raise ValueError("Overlapping or out-of-order primary-storyline clips are unsupported for ripple edits")
            previous_end = offset + duration
            if index > target_index:
                plan.append((sibling, _native_time(offset + delta)))
        return plan, sequence_duration, delta

    def set_speed_curve(self, clip_path, speed_keyframes, ripple=True, preserve_pitch=True, frame_sampling="floor"):
        clip, context = self._target(clip_path)
        if not isinstance(preserve_pitch, bool):
            raise ValueError("preserve_pitch must be a boolean")
        if frame_sampling not in ("floor", "nearest-neighbor", "frame-blending", "optical-flow-classic", "optical-flow"):
            raise ValueError("Unsupported frame_sampling; use floor, nearest-neighbor, frame-blending, optical-flow-classic or optical-flow")
        points = self._inputs(speed_keyframes, context["frame_duration"])
        mapping = self._bake(points, context["frame_duration"], context["start"], context["source_start"])
        source_end = mapping.points[-1][1]
        if source_end > context["asset_end"]:
            raise ValueError("Speed curve consumes source media beyond the available asset bounds")
        new_duration = points[-1][0]
        plan, sequence_duration, delta = self._ripple_plan(clip, context, new_duration, ripple)
        time_map = ET.Element("timeMap", preservesPitch="1" if preserve_pitch else "0", frameSampling=frame_sampling)
        for time, value in mapping.points:
            ET.SubElement(time_map, "timept", time=format_exact_seconds(time), value=format_exact_seconds(value), interp="linear")
        # All input, map, source-range and ripple validation completes before mutation.
        for old in clip.findall("timeMap"):
            clip.remove(old)
        index = next((i for i, child in enumerate(clip) if child.tag not in ("param", "note", "conform-rate")), len(clip))
        clip.insert(index, time_map)
        clip.set("duration", format_exact_seconds(new_duration))
        for sibling, offset in plan:
            sibling.set("offset", format_exact_seconds(offset))
        if sequence_duration is not None:
            context["sequence"].set("duration", format_exact_seconds(sequence_duration))
        return {"clip_path": clip_path, "duration": format_exact_seconds(new_duration), "previous_duration": format_exact_seconds(context["duration"]),
                "duration_change": format_exact_seconds(delta), "source_start": format_exact_seconds(context["source_start"]),
                "source_end": format_exact_seconds(source_end), "source_consumed": format_exact_seconds(source_end - context["source_start"]),
                "source_trim_change": format_exact_seconds(source_end - context["source_end"]),
                "rippled_clips": len(plan), "preserve_pitch": preserve_pitch, "frame_sampling": frame_sampling,
                "emitted_points": len(mapping.points), "ramp_sampling": "frame-baked linear ramp at project-frame boundaries",
                "animation_policy": "preserve clip-relative keyframe times; shortening can hide retained keys",
                "speed_keyframes": [{"time": format_exact_seconds(time), "speed": float(speed), "transition": transition}
                                    for time, speed, transition in points]}

    def reset_speed(self, clip_path, ripple=True):
        """Reset to 1x, explicitly snapping native-export rounding to source frames.

        FCP re-exports time maps on its internal tick grid, which can move the
        exact inferred trim by a fraction of a frame. Both ends snap to the
        nearest source frame (same known project rate), never more than half a
        frame; the response reports both adjustments rather than hiding them.
        """
        clip, context = self._target(clip_path)
        changed = context["time_map"] is not None
        source_start, source_end = context["source_start"], context["source_end"]
        if changed:
            frame, origin = context["frame_duration"], context["asset_start"]
            source_start = origin + round((source_start - origin) / frame) * frame
            source_end = origin + round((source_end - origin) / frame) * frame
        _native_time(source_start)
        _native_time(source_end)
        if not context["asset_start"] <= source_start < source_end <= context["asset_end"]:
            raise ValueError("Reset source-frame snapping would produce an empty range or exceed asset bounds")
        consumed = _native_time(source_end - source_start)
        start_adjustment = source_start - context["source_start"]
        end_adjustment = source_end - context["source_end"]
        plan, sequence_duration, delta = self._ripple_plan(clip, context, consumed, ripple)
        # FCP's intrinsic animations use the adjusted local clock, not source
        # time. Normalizing start while removing the map must retain each
        # point's position relative to the visible clip's beginning.
        shift = source_start - context["start"]
        animation_plan = []
        if shift:
            for child in clip:
                if child.tag in _ANIMATED_INTRINSICS:
                    for key in child.iter("keyframe"):
                        if key.get("time") is None:
                            raise ValueError("Existing intrinsic keyframe is missing its local time")
                        animation_plan.append((key, _native_time(parse_seconds(key.get("time")) + shift)))
        for old in clip.findall("timeMap"):
            clip.remove(old)
        for key, time in animation_plan:
            key.set("time", format_exact_seconds(time))
        clip.set("start", format_exact_seconds(source_start))
        clip.set("duration", format_exact_seconds(consumed))
        for sibling, offset in plan:
            sibling.set("offset", format_exact_seconds(offset))
        if sequence_duration is not None:
            context["sequence"].set("duration", format_exact_seconds(sequence_duration))
        return {"clip_path": clip_path, "changed": changed, "duration": format_exact_seconds(consumed),
                "previous_duration": format_exact_seconds(context["duration"]), "duration_change": format_exact_seconds(delta),
                "source_start": format_exact_seconds(source_start), "source_end": format_exact_seconds(source_end),
                "source_consumed": format_exact_seconds(consumed), "rippled_clips": len(plan),
                "source_start_adjustment": format_exact_seconds(start_adjustment),
                "source_end_adjustment": format_exact_seconds(end_adjustment),
                "source_trim_change": format_exact_seconds(consumed - (context["source_end"] - context["source_start"])),
                "source_snap_policy": "nearest source frame, at most half a frame per endpoint",
                "warnings": (["Source in/out were snapped to the nearest source-frame boundaries; see adjustment fields"]
                             if start_adjustment or end_adjustment else [])}
