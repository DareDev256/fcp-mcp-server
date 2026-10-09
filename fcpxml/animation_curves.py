"""Bounded, frame-checked approximations of authored cubic animation curves.

The result contains editable linear FCPXML keyframes, not native Bezier handles.
Tolerance applies at project frame times between the first and last authored
points; sub-frame positions and the application's rendering are not certified.
"""

import math
from fractions import Fraction

from .rational import format_exact_seconds, parse_strict_seconds

# tools/keyframes.py builds its schema from these names; keep limits here.
MAX_AUTHORED_POINTS = 100
MAX_SAMPLED_FRAMES = 18000
MAX_GENERATED_KEYFRAMES = 1000
MAX_ABS_VALUE = 1e12
DEFAULT_TOLERANCE = {"position": 0.05, "scale": 0.001, "rotation": 0.1, "opacity": 0.001}
CURVE_PROPERTIES = tuple(DEFAULT_TOLERANCE)
EASINGS = ("linear", "ease", "easeIn", "easeOut")
_POINT_FIELDS = {"time", "frame", "value", "control_in", "control_out", "easing"}


def check_curve_property(prop):
    """Reject anything but the properties this sampler can author."""
    if not isinstance(prop, str) or prop not in DEFAULT_TOLERANCE:
        raise ValueError(
            f"animation curves support {', '.join(CURVE_PROPERTIES)}; "
            "volume is unsupported, use set_keyframes for audio levels"
        )


def _number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number, not a boolean or string")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} exceeds the supported numeric range") from exc
    if not math.isfinite(result) or abs(result) > MAX_ABS_VALUE:
        raise ValueError(f"{label} must be finite with absolute value <= {MAX_ABS_VALUE:g}")
    return result


def _value(prop, value, dimensions, label):
    if dimensions == 2:
        if prop == "scale" and type(value) in (int, float):
            value = [value, value]
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{label} must be a two-number [x, y] vector")
        result = tuple(_number(v, label) for v in value)
    else:
        result = (_number(value, label),)
    if prop == "scale" and any(v <= 0 for v in result):
        raise ValueError("scale values and controls must be positive multipliers")
    if prop == "opacity" and any(not 0 <= v <= 1 for v in result):
        raise ValueError("opacity values and controls must be between 0 and 1")
    return result


def _frame(point, frame_duration, duration):
    if ("frame" in point) == ("time" in point):
        raise ValueError("each curve point must specify exactly one of time or frame")
    if "frame" in point:
        index = point["frame"]
        if type(index) is not int or index < 0:
            raise ValueError("frame must be a nonnegative integer")
        time = index * frame_duration
    else:
        text = point["time"]
        if isinstance(text, str) and len(text) > 64:
            raise ValueError("time must be at most 64 characters")
        time = parse_strict_seconds(text)
        frames = time / frame_duration
        if frames.denominator != 1:
            raise ValueError("curve point times must align to project frames")
        index = frames.numerator
    if not 0 <= time < duration:
        raise ValueError("curve point times must satisfy 0 <= time < clip duration")
    if time.numerator > 2**63 - 1 or time.denominator > 2**32 - 1:
        raise ValueError("curve point time exceeds FCPXML rational limits")
    return index


def _lerp(a, b, u):
    return tuple((1 - u) * x + u * y for x, y in zip(a, b))


def _ease(u, easing):
    if easing == "ease":
        return u * u * (3 - 2 * u)
    if easing == "easeIn":
        return u * u
    if easing == "easeOut":
        return u * (2 - u)
    return u


def _sample(left, right, u):
    u = _ease(u, left["easing"])
    a, d = left["value"], right["value"]
    if "control_out" not in left and "control_in" not in right:
        return _lerp(a, d, u)
    b = left.get("control_out", _lerp(a, d, 1 / 3))
    c = right.get("control_in", _lerp(a, d, 2 / 3))
    # De Casteljau keeps all intermediates in the control-point convex hull.
    ab, bc, cd = _lerp(a, b, u), _lerp(b, c, u), _lerp(c, d, u)
    return _lerp(_lerp(ab, bc, u), _lerp(bc, cd, u), u)


def _distance(a, b):
    if len(a) == 1:
        return abs(a[0] - b[0])
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _worst(samples, first, last):
    """Largest same-time distance from the chord first..last, and its frame."""
    largest, worst = 0.0, None
    width = last - first
    for index in range(first + 1, last):
        error = _distance(samples[index], _lerp(samples[first], samples[last], (index - first) / width))
        if not math.isfinite(error):
            raise ValueError("curve interpolation produced a nonfinite error")
        if error > largest:
            largest, worst = error, index
    return largest, worst


def _forward_reach(samples, knots, tolerance):
    """From each key, double then bisect toward the farthest chord that fits.

    O(L log L) per key. Error is not monotone in the reach, so this can stop
    short of the farthest valid frame. None once the key limit is exceeded.
    """
    keys = [knots[0]]
    for end in knots[1:]:
        start = keys[-1]
        while start < end:
            reach, step = start + 1, 1
            while reach + step <= end and _worst(samples, start, reach + step)[0] <= tolerance:
                reach, step = reach + step, step * 2
            beyond = min(reach + step, end + 1)
            while beyond - reach > 1:
                middle = (reach + beyond) // 2
                if _worst(samples, start, middle)[0] > tolerance:
                    beyond = middle
                else:
                    reach = middle
            keys.append(reach)
            if len(keys) > MAX_GENERATED_KEYFRAMES:
                return None
            start = reach
    return keys


def _worst_point_split(samples, knots, tolerance):
    """Keep adding each chord's worst frame until every chord fits.

    None once the key limit is exceeded.
    """
    selected = set(knots)
    pending = list(zip(knots, knots[1:]))
    while pending:
        first, last = pending.pop()
        error, worst = _worst(samples, first, last)
        if error > tolerance:
            selected.add(worst)
            if len(selected) > MAX_GENERATED_KEYFRAMES:
                return None
            pending.extend(((worst, last), (first, worst)))
    return sorted(selected)


def _simplify(samples, knots, tolerance):
    """Keep authored knots and the smaller of two selections between them.

    Forward reach is usually at or near the minimum, but on oscillating
    curves it can trail the worst-point split (1003 vs 988 keys on a
    19-point rotation at tolerance 0.01), so neither is used alone.
    """
    candidates = [keys for keys in (_forward_reach(samples, knots, tolerance),
                                    _worst_point_split(samples, knots, tolerance)) if keys is not None]
    if not candidates:
        raise ValueError(f"tolerance requires more than {MAX_GENERATED_KEYFRAMES} generated keyframes; increase tolerance or shorten the curve")
    keys = min(candidates, key=len)
    return keys, max(_worst(samples, a, b)[0] for a, b in zip(keys, keys[1:]))


def sample_animation_curve(prop, points, frame_duration: Fraction, duration: Fraction, tolerance=None):
    """Sample and simplify a clip-relative cubic curve without mutating inputs.

    Points have ``time`` (rational seconds) OR ``frame``, ``value``, optional
    absolute ``control_in``/``control_out`` property values, and an outgoing
    ``easing``: linear, ease (smoothstep), easeIn (u squared), or easeOut.
    Easing changes the cubic parameter; it is not FCP's native interpolation.
    No point may be at clip out. Unused first-in/last-out/last-easing fail.

    Scalar scale stays scalar unless any point/control uses a vector, in which
    case scalar scale values are expanded to [value, value]. Error is absolute
    for scalars and Euclidean for vectors, in the property's native units.
    """
    check_curve_property(prop)
    for name, value in (("frame_duration", frame_duration), ("duration", duration)):
        if not isinstance(value, Fraction) or value <= 0:
            raise ValueError(f"{name} must be a positive Fraction")
    if not isinstance(points, list) or not 2 <= len(points) <= MAX_AUTHORED_POINTS:
        raise ValueError(f"curve points must be a list of 2..{MAX_AUTHORED_POINTS} items")
    tolerance = _number(DEFAULT_TOLERANCE[prop] if tolerance is None else tolerance, "tolerance")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("each curve point must be an object")
        if point.keys() - _POINT_FIELDS or "value" not in point:
            raise ValueError("curve points require value and accept only time, frame, value, control_in, control_out, easing")
    if "control_in" in points[0]:
        raise ValueError("control_in on the first curve point is unused")
    if "control_out" in points[-1] or "easing" in points[-1]:
        raise ValueError("control_out and easing on the last curve point are unused")
    vector_scale = prop == "scale" and any(
        isinstance(point[field], (list, tuple))
        for point in points for field in ("value", "control_in", "control_out") if field in point
    )
    dimensions = 2 if prop == "position" or vector_scale else 1
    authored = []
    for point in points:
        index = _frame(point, frame_duration, duration)
        if authored and index <= authored[-1]["frame"]:
            raise ValueError("curve point times must be strictly increasing")
        easing = point.get("easing", "linear")
        if not isinstance(easing, str) or easing not in EASINGS:
            raise ValueError(f"easing must be one of: {', '.join(EASINGS)}")
        node = {"frame": index, "easing": easing}
        for field in ("value", "control_in", "control_out"):
            if field in point:
                node[field] = _value(prop, point[field], dimensions, field)
        authored.append(node)
    first_frame, last_frame = authored[0]["frame"], authored[-1]["frame"]
    sample_count = last_frame - first_frame + 1
    if sample_count > MAX_SAMPLED_FRAMES:
        raise ValueError(f"curve spans more than {MAX_SAMPLED_FRAMES} evaluated project frames")
    samples = [authored[0]["value"]]
    for left, right in zip(authored, authored[1:]):
        span = right["frame"] - left["frame"]
        for step in range(1, span):
            value = _sample(left, right, step / span)
            if not all(math.isfinite(v) for v in value):
                raise ValueError("curve interpolation produced a nonfinite value")
            samples.append(value)
        samples.append(right["value"])
    indices, max_error = _simplify(samples, [node["frame"] - first_frame for node in authored], tolerance)
    keyframes = []
    for index in indices:
        time = (first_frame + index) * frame_duration
        if time.numerator > 2**63 - 1 or time.denominator > 2**32 - 1:
            raise ValueError("sampled time exceeds FCPXML rational limits")
        value = samples[index]
        keyframes.append({"time": format_exact_seconds(time), "value": list(value) if dimensions == 2 else value[0], "interp": "linear"})
    return {
        "keyframes": keyframes,
        "authored_point_count": len(authored),
        "sampled_frame_count": sample_count,
        "generated_keyframe_count": len(keyframes),
        "tolerance": tolerance,
        "max_error": max_error,
        "error_metric": "euclidean" if dimensions == 2 else "absolute",
        "representation": "adaptive_linear",
    }
