"""Check frame-wise curve accuracy independently of the sampler implementation."""

import copy
import math
from bisect import bisect_right
from fractions import Fraction

import pytest

from fcpxml.animation_curves import sample_animation_curve

RATE = Fraction(1, 30)


def sample(prop, points, **kwargs):
    return sample_animation_curve(prop, points, kwargs.pop("frame_duration", RATE), kwargs.pop("duration", Fraction(6)), **kwargs)


def at_frame(result, frame, rate=RATE):
    """Decode output and interpolate it independently at the requested frame."""
    keys = result["keyframes"]
    times = [Fraction(key["time"][:-1]) / rate for key in keys]
    right = min(bisect_right(times, frame), len(keys) - 1)
    if times[right] == frame:
        return keys[right]["value"]
    left = right - 1
    alpha = float((frame - times[left]) / (times[right] - times[left]))
    a, b = keys[left]["value"], keys[right]["value"]
    if isinstance(a, list):
        return [x + alpha * (y - x) for x, y in zip(a, b)]
    return a + alpha * (b - a)


def bernstein(a, b, c, d, u):
    return (1-u)**3*a + 3*(1-u)**2*u*b + 3*(1-u)*u*u*c + u**3*d


def test_straight_line_needs_only_endpoints_and_is_deterministic():
    points = [{"frame": 0, "value": [0, 0]}, {"frame": 150, "value": [40, 20]}]
    before = copy.deepcopy(points)
    result = sample("position", points)
    assert result == sample("position", points)
    assert points == before
    assert result["generated_keyframe_count"] == 2
    assert result["sampled_frame_count"] == 151
    assert result["authored_point_count"] == 2
    assert result["representation"] == "adaptive_linear"
    assert result["error_metric"] == "euclidean"
    assert result["tolerance"] == 0.05
    assert result["max_error"] <= 1e-12
    assert all(key["interp"] == "linear" for key in result["keyframes"])


def test_bezier_arc_is_compressed_with_independent_dense_error_bound():
    result = sample("position", [
        {"frame": 0, "value": [0, 0], "control_out": [0, 40]},
        {"frame": 150, "value": [40, 0], "control_in": [40, 40]},
    ])
    errors = []
    for frame in range(151):
        u = frame / 150
        expected = [bernstein(0, 0, 40, 40, u), bernstein(0, 40, 40, 0, u)]
        errors.append(math.dist(expected, at_frame(result, frame)))
    assert 2 < result["generated_keyframe_count"] < 50
    assert max(errors) <= 0.05 + 1e-12
    assert result["max_error"] == pytest.approx(max(errors), abs=1e-12)
    assert at_frame(result, 75)[1] == pytest.approx(30, abs=0.05)


def _ease(u):
    return 3*u*u - 2*u*u*u


def _ease_out(u):
    return 1 - (1-u)**2


def _chord_fits(reference, first, last, tolerance):
    for index in range(first + 1, last):
        alpha = (index - first) / (last - first)
        chord = [a + alpha * (b - a) for a, b in zip(reference[first], reference[last])]
        if math.dist(reference[index], chord) > tolerance:
            return False
    return True


def _minimum_key_count(reference, tolerance):
    """Exhaustive minimum over every frame subset, under the same bound."""
    best = [math.inf] * len(reference)
    best[0] = 1
    for last in range(1, len(reference)):
        for first in range(last - 1, -1, -1):
            if best[first] + 1 < best[last] and _chord_fits(reference, first, last, tolerance):
                best[last] = best[first] + 1
    return best[-1]


# The recursive worst-point split this replaced emitted 57, 51, 27 and 28
# keys here, all more than one above the exhaustive minimum.
@pytest.mark.parametrize("prop,points,curve,tolerance", [
    ("position", [{"frame": 0, "value": [-40, 0], "control_out": [-20, 40], "easing": "ease"},
                  {"frame": 150, "value": [40, 0], "control_in": [20, 40]}],
     lambda u: [bernstein(-40, -20, 20, 40, _ease(u)), bernstein(0, 40, 40, 0, _ease(u))], 0.05),
    ("position", [{"frame": 0, "value": [0, 0], "control_out": [0, 40], "easing": "ease"},
                  {"frame": 100, "value": [40, 0], "control_in": [40, 40]}],
     lambda u: [bernstein(0, 0, 40, 40, _ease(u)), bernstein(0, 40, 40, 0, _ease(u))], 0.05),
    ("opacity", [{"frame": 0, "value": 0, "easing": "ease"}, {"frame": 75, "value": 1}],
     lambda u: [_ease(u)], 0.001),
    ("scale", [{"frame": 0, "value": 1, "control_out": 1.6, "easing": "easeOut"},
               {"frame": 60, "value": 1.2, "control_in": 1.25}],
     lambda u: [bernstein(1, 1.6, 1.25, 1.2, _ease_out(u))], 0.001),
], ids=["arch", "arc", "opacity-ease", "scale-overshoot"])
def test_key_count_is_within_one_of_the_exhaustive_minimum(prop, points, curve, tolerance):
    last = points[-1]["frame"]
    reference = [curve(frame / last) for frame in range(last + 1)]
    result = sample(prop, points, tolerance=tolerance)
    errors = []
    for frame in range(last + 1):
        actual = at_frame(result, frame)
        errors.append(math.dist(reference[frame], actual if isinstance(actual, list) else [actual]))
    assert max(errors) <= tolerance + 1e-12
    assert result["max_error"] == pytest.approx(max(errors), abs=1e-12)
    assert result["generated_keyframe_count"] <= _minimum_key_count(reference, tolerance) + 1


def test_long_curve_stays_sparse_with_every_frame_bounded():
    last = 17999
    result = sample("position", [
        {"frame": 0, "value": [-40, 0], "control_out": [-20, 40], "easing": "ease"},
        {"frame": last, "value": [40, 0], "control_in": [20, 40]},
    ], duration=18000*RATE)
    assert result["sampled_frame_count"] == 18000
    # The recursive split emitted 65 keys for this curve.
    assert result["generated_keyframe_count"] <= 40
    worst = 0.0
    for frame in range(last + 1):
        u = _ease(frame / last)
        expected = [bernstein(-40, -20, 20, 40, u), bernstein(0, 40, 40, 0, u)]
        worst = max(worst, math.dist(expected, at_frame(result, frame)))
    assert worst <= 0.05 + 1e-12
    assert result["max_error"] == pytest.approx(worst, abs=1e-12)


def test_oscillating_curve_keeps_the_smaller_selection_under_the_key_limit():
    # Forward reach alone needs 1003 keys here and would exceed the limit;
    # the worst-point split alone fits in 988. Neither may be used alone.
    points = []
    for i in range(18):
        point = {"frame": 60 * i, "value": 38 - 74 * i}
        if i < 17:
            point.update(control_out=53 - 74 * i, easing="ease")
        if i:
            point["control_in"] = -57 - 74 * (i - 1)
        points.append(point)
    last = points[-1]["value"]
    points[-1].update(control_out=last + 1.5, easing="ease")
    points.append({"frame": 1080, "value": last - 7.4, "control_in": last - 9.5})
    result = sample("rotation", points, duration=Fraction(37), tolerance=0.01)
    assert result["generated_keyframe_count"] <= 988
    worst = 0.0
    for left, right in zip(points, points[1:]):
        span = right["frame"] - left["frame"]
        for step in range(span + 1):
            u = _ease(step / span)
            expected = bernstein(left["value"], left["control_out"], right["control_in"], right["value"], u)
            worst = max(worst, abs(at_frame(result, left["frame"] + step) - expected))
    assert worst <= 0.01 + 1e-12
    assert result["max_error"] == pytest.approx(worst, abs=1e-12)


def test_collinear_eased_motion_is_not_collapsed_geometrically():
    result = sample("position", [
        {"frame": 0, "value": [0, 0], "easing": "easeIn"},
        {"frame": 150, "value": [30, 0]},
    ])
    assert result["generated_keyframe_count"] > 2
    for frame in range(151):
        assert at_frame(result, frame)[0] == pytest.approx(30 * (frame / 150)**2, abs=0.05)
        assert at_frame(result, frame)[1] == 0
    assert at_frame(result, 75)[0] == pytest.approx(7.5, abs=0.05)


@pytest.mark.parametrize("easing,reference", [
    ("linear", lambda u: u),
    ("ease", lambda u: 3*u*u-2*u*u*u),
    ("easeIn", lambda u: u*u),
    ("easeOut", lambda u: 1-(1-u)**2),
])
def test_easing_is_applied_to_cubic_parameter(easing, reference):
    result = sample("rotation", [
        {"frame": 0, "value": 0, "control_out": 100, "easing": easing},
        {"frame": 150, "value": 90, "control_in": -30},
    ])
    errors = [abs(at_frame(result, frame) - bernstein(0, 100, -30, 90, reference(frame/150))) for frame in range(151)]
    assert max(errors) <= 0.1 + 1e-12
    assert result["max_error"] == pytest.approx(max(errors), abs=1e-12)
    assert result["error_metric"] == "absolute"


def test_missing_handle_defaults_to_chord_thirds():
    result = sample("rotation", [
        {"frame": 0, "value": 0, "control_out": 80},
        {"frame": 150, "value": 90},
    ])
    for frame in range(151):
        assert at_frame(result, frame) == pytest.approx(bernstein(0, 80, 60, 90, frame/150), abs=0.1)


def test_authored_knots_preserved_even_on_straight_or_held_spans():
    result = sample("rotation", [
        {"frame": 7, "value": 10}, {"frame": 31, "value": 10},
        {"frame": 62, "value": 10}, {"frame": 100, "value": 30},
    ])
    assert [Fraction(key["time"][:-1]) / RATE for key in result["keyframes"]] == [7, 31, 62, 100]
    assert result["sampled_frame_count"] == 94
    assert result["authored_point_count"] == 4


def test_ntsc_nonzero_start_uses_exact_global_frame_grid():
    rate = Fraction(1001, 30000)
    result = sample("position", [
        {"time": "29029/30000s", "value": [0, 0], "easing": "ease"},
        {"frame": 179, "value": [40, 20]},
    ], frame_duration=rate, duration=200*rate)
    assert result["keyframes"][0]["time"] == "29029/30000s"
    assert Fraction(result["keyframes"][-1]["time"][:-1]) == 179*rate
    assert result["sampled_frame_count"] == 151
    for key in result["keyframes"]:
        assert (Fraction(key["time"][:-1]) / rate).denominator == 1
    for frame in range(29, 180):
        u = (frame-29)/150
        smooth = 3*u*u-2*u*u*u
        assert math.dist(at_frame(result, frame, rate), [40*smooth, 20*smooth]) <= 0.05 + 1e-12


@pytest.mark.parametrize("prop,start,end,metric,tol", [
    ("scale", 1, 2, "absolute", 0.001),
    ("scale", [1, 2], [2, 1], "euclidean", 0.001),
    ("scale", 1, [2, 3], "euclidean", 0.001),
    ("opacity", 0, 1, "absolute", 0.001),
    ("rotation", 0, 360, "absolute", 0.1),
])
def test_property_representations_and_default_tolerances(prop, start, end, metric, tol):
    result = sample(prop, [{"frame": 0, "value": start, "easing": "ease"}, {"frame": 150, "value": end}])
    assert result["error_metric"] == metric
    assert result["tolerance"] == tol
    assert result["max_error"] <= tol
    assert isinstance(result["keyframes"][0]["value"], list) == (metric == "euclidean")


def test_vector_scale_control_promotes_scalar_endpoints():
    result = sample("scale", [
        {"frame": 0, "value": 1, "control_out": [1, 3]},
        {"frame": 150, "value": 2},
    ])
    assert result["keyframes"][0]["value"] == [1, 1]
    assert result["keyframes"][-1]["value"] == [2, 2]
    assert result["error_metric"] == "euclidean"


@pytest.mark.parametrize("prop,bad_value", [
    ("position", [True, 0]), ("position", [math.nan, 0]),
    ("position", [math.inf, 0]), ("position", [1e13, 0]),
    ("position", [10**1000, 0]), ("position", [0]),
    ("position", [0, 0, 0]), ("position", 0),
    ("scale", 0), ("scale", -1), ("scale", [1, 0]),
    ("opacity", -0.01), ("opacity", 1.01), ("opacity", "0.5"),
    ("rotation", True), ("rotation", [0, 0]),
])
def test_rejects_invalid_property_values_and_controls(prop, bad_value):
    good = [1, 1] if prop == "position" else 1
    for field in ("value", "control_out"):
        first = {"frame": 0, "value": good, field: bad_value}
        with pytest.raises(ValueError):
            sample(prop, [first, {"frame": 150, "value": good}])


@pytest.mark.parametrize("tolerance", [0, -0.1, math.nan, math.inf, True, "0.1", 1e13])
def test_rejects_invalid_tolerance(tolerance):
    with pytest.raises(ValueError):
        sample("rotation", [{"frame": 0, "value": 0}, {"frame": 150, "value": 90}], tolerance=tolerance)


@pytest.mark.parametrize("location", [
    {}, {"frame": 0, "time": "0s"}, {"frame": True}, {"frame": 0.0},
    {"frame": -1}, {"time": "-1s"}, {"time": "1/0s"},
    {"time": "1/31s"}, {"time": 0}, {"time": "0.0s"},
    {"time": "1"}, {"time": "0"*65+"s"},
])
def test_rejects_invalid_point_time(location):
    with pytest.raises(ValueError):
        sample("rotation", [{**location, "value": 0}, {"frame": 150, "value": 90}])


@pytest.mark.parametrize("end", [0, 180, 181])
def test_point_times_increase_and_exclude_clip_out(end):
    with pytest.raises(ValueError):
        sample("rotation", [{"frame": 0, "value": 0}, {"frame": end, "value": 90}])


@pytest.mark.parametrize("which,extra", [
    (0, {"control_in": 0}), (1, {"control_out": 0}), (1, {"easing": "linear"}),
    (0, {"unexpected": 1}), (0, {"easing": "smooth"}), (0, {"easing": []}),
])
def test_rejects_unknown_or_unused_point_fields(which, extra):
    points = [{"frame": 0, "value": 0}, {"frame": 150, "value": 90}]
    points[which].update(extra)
    with pytest.raises(ValueError):
        sample("rotation", points)


@pytest.mark.parametrize("points", [None, [], [{"frame": 0, "value": 0}], [None, None], [{"frame": 0}, {"frame": 1, "value": 1}], [{"frame": i, "value": 0} for i in range(101)]])
def test_point_list_validation(points):
    with pytest.raises(ValueError):
        sample("rotation", points)


@pytest.mark.parametrize("prop", ["volume", "retime", "x", None, []])
def test_unsupported_property(prop):
    with pytest.raises(ValueError):
        sample(prop, [{"frame": 0, "value": 0}, {"frame": 150, "value": 1}])


@pytest.mark.parametrize("field,bad", [
    ("frame_duration", Fraction(0)), ("frame_duration", Fraction(-1)),
    ("frame_duration", 1/30), ("duration", Fraction(0)), ("duration", 6),
])
def test_requires_positive_rational_clock(field, bad):
    with pytest.raises(ValueError):
        sample("rotation", [{"frame": 0, "value": 0}, {"frame": 1, "value": 1}], **{field: bad})


def test_evaluated_frame_limit_is_total_across_all_authored_spans():
    points = [{"frame": frame, "value": 0} for frame in (0, 9000, 18000)]
    with pytest.raises(ValueError, match="18000 evaluated"):
        sample("rotation", points, duration=20000*RATE)
    points[-1]["frame"] = 17999
    result = sample("rotation", points, duration=20000*RATE)
    assert result["sampled_frame_count"] == 18000
    assert result["generated_keyframe_count"] == 3


def test_generated_keyframe_limit_rejects_unachievable_tolerance():
    with pytest.raises(ValueError, match="1000 generated"):
        sample("rotation", [
            {"frame": 0, "value": 0, "control_out": 10000},
            {"frame": 1100, "value": 0, "control_in": 10000},
        ], duration=40*Fraction(1), tolerance=1e-12)


def test_fraction_time_limits_checked():
    rate = Fraction(1, 2**32+1)
    with pytest.raises(ValueError, match="rational limits"):
        sample("rotation", [{"frame": 0, "value": 0}, {"frame": 1, "value": 1}], frame_duration=rate)
    with pytest.raises(ValueError, match="rational limits"):
        sample("rotation", [{"frame": 0, "value": 0}, {"frame": 2**63, "value": 1}], frame_duration=Fraction(1), duration=Fraction(2**63+1))
