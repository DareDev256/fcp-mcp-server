"""Exact rational time arithmetic for FCPXML attributes.

What this is
------------

FCPXML times are exact rationals on the page (``86496410/24000s``). This
module keeps them exact — :class:`fractions.Fraction` in, frame-aligned
rational string out — and never involves a frame rate until the moment a
value has to be written back, where alignment to the frame grid is the
whole point.

It is also the single source of truth for what a frame rate *is*. Every
timebase in the codebase is built from :func:`rational_fps`, because the
broadcast rates are exact rationals and their decimal spellings are not::

    23.976 -> 24000/1001    29.97 -> 30000/1001    59.94 -> 60000/1001

``TimeValue`` used to build denominators with ``int(fps)``. ``int(23.976)``
is 23, so 3604 seconds was stored as ``86410/23s`` and read back as
3756.96 — a 152-second error on a value that is exact on the page, and
enough to move a connected clip by minutes. That is #17, and it is why
nothing here is allowed to see a rounded rate.
"""

from fractions import Fraction

__all__ = [
    "parse_seconds",
    "format_seconds",
    "frame_duration_from_attr",
    "to_frames",
    "rational_fps",
    "frame_duration_seconds",
    "frame_duration_attr",
    "nominal_fps",
    "fcp_frame_rate_name",
    "is_ntsc_rate",
    "tick_timebase",
]


# The NTSC-fractional family, exact. A caller passing 23.976 (or 23.98, which
# is what Final Cut prints in its own UI) means 24000/1001 — a float that
# never divides evenly into a second and truncates to 23 under ``int()``.
_NTSC_RATES = (
    Fraction(24000, 1001),   # 23.976 / 23.98
    Fraction(30000, 1001),   # 29.97
    Fraction(48000, 1001),   # 47.952
    Fraction(60000, 1001),   # 59.94
    Fraction(120000, 1001),  # 119.88
)

# Half a hundredth of a frame per second is wide enough to catch every way a
# caller writes an NTSC rate (23.976, 23.98, 23.976023976...) and far too
# narrow to reach the neighbouring integer rate: 24 - 24000/1001 is 0.024.
_RATE_SNAP_TOLERANCE = 0.01

# ``<conform-rate srcFrameRate>`` is an enumeration, not a number. int(23.976)
# produced "23", which is not a member of it.
_FCP_RATE_NAMES = {
    Fraction(24000, 1001): "23.98",
    Fraction(30000, 1001): "29.97",
    Fraction(60000, 1001): "59.94",
}


def rational_fps(fps: "float | int | Fraction | str") -> Fraction:
    """Resolve a frame rate to the exact rational it stands in for.

    ``23.976`` is not a frame rate, it is a rounded printout of ``24000/1001``.
    Every timebase in this codebase is built from the return of this function
    so that no arithmetic ever runs on the rounded form.

    Raises:
        ValueError: on a non-positive or unparseable rate. A zero frame rate
            would divide by zero downstream; falling back to a default would
            silently place clips on the wrong grid.
    """
    if isinstance(fps, Fraction):
        exact = fps
    else:
        try:
            exact = Fraction(str(fps).strip())
        except (ValueError, ZeroDivisionError, ArithmeticError) as exc:
            raise ValueError(f"Unparseable frame rate: {fps!r}") from exc
    if exact <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")

    # Already one of the exact rationals (or an integer) — leave it alone.
    if exact in _NTSC_RATES or exact.denominator == 1:
        return exact

    as_float = float(exact)
    for candidate in _NTSC_RATES:
        if abs(float(candidate) - as_float) < _RATE_SNAP_TOLERANCE:
            return candidate
    if abs(as_float - round(as_float)) < _RATE_SNAP_TOLERANCE:
        return Fraction(round(as_float))
    return exact.limit_denominator(100000)


def frame_duration_seconds(fps: "float | int | Fraction | str") -> Fraction:
    """Exact seconds per frame — the reciprocal of :func:`rational_fps`."""
    return 1 / rational_fps(fps)


def frame_duration_attr(fps: "float | int | Fraction | str") -> str:
    """One frame as an FCPXML time attribute: ``"1001/24000s"`` at 23.98."""
    duration = frame_duration_seconds(fps)
    if duration.denominator == 1:
        return f"{duration.numerator}s"
    return f"{duration.numerator}/{duration.denominator}s"


def nominal_fps(fps: "float | int | Fraction | str") -> int:
    """Frames per labelled second of non-drop timecode.

    23.98 timecode counts 0..23 and drifts against the wall clock; that is
    what non-drop means. The count is ``round()``, never ``int()`` — the
    latter gave 23 frames per second and made frame 23 unrepresentable.
    """
    exact = rational_fps(fps)
    return max(1, round(float(exact)))


def fcp_frame_rate_name(fps: "float | int | Fraction | str") -> str:
    """The enumerated string Final Cut accepts for ``srcFrameRate``."""
    exact = rational_fps(fps)
    if exact in _FCP_RATE_NAMES:
        return _FCP_RATE_NAMES[exact]
    if exact.denominator == 1:
        return str(exact.numerator)
    return f"{float(exact):.2f}"


def is_ntsc_rate(fps: "float | int | Fraction | str") -> bool:
    """True for the /1001 family, which NLEs flag rather than name.

    Written as an exact-rational check, not float membership: a rate parsed
    off a real file is 23.976023976023978, which ``in (23.976, ...)`` never
    matched.
    """
    return rational_fps(fps) in _NTSC_RATES


def tick_timebase(fps: "float | int | Fraction | str") -> tuple[int, int]:
    """``(ticks_per_second, ticks_per_frame)`` for frame snapping.

    Integer rates keep the 2400-tick timebase this codebase already used, so
    their output is unchanged. NTSC rates cannot: a 23.98 frame is 100.1
    ticks of 2400, and ``2400 // int(23.976)`` gave 104 ticks — a unit that
    is not a frame of anything. Those get the format's own timebase, where a
    frame is exactly ``1001`` ticks of ``24000``.
    """
    exact = rational_fps(fps)
    if exact.denominator == 1 and 2400 % exact.numerator == 0:
        return 2400, 2400 // exact.numerator
    duration = 1 / exact
    return duration.denominator, duration.numerator


def parse_seconds(value: str | None) -> Fraction:
    """Exact seconds from an FCPXML time attribute.

    Accepts the two forms Final Cut writes — ``"86496410/24000s"`` and
    ``"3604s"`` — plus a missing/empty attribute, which FCPXML treats as 0.

    Raises:
        ValueError: on a malformed value or a zero denominator. Silently
            returning 0 here would place a clip at the start of the
            timeline, which is worse than failing.
    """
    if value is None:
        return Fraction(0)
    text = str(value).strip()
    if not text:
        return Fraction(0)
    if text.endswith("s"):
        text = text[:-1]
    if "/" in text:
        num, _, den = text.partition("/")
        denominator = int(den)
        if denominator == 0:
            raise ValueError(f"Zero denominator in FCPXML time: {value!r}")
        return Fraction(int(num), denominator)
    return Fraction(text)


def frame_duration_from_attr(frame_duration: str | None) -> Fraction:
    """Exact seconds-per-frame from a ``<format frameDuration>`` attribute.

    ``"1001/24000s"`` -> ``Fraction(1001, 24000)``, i.e. 23.976 fps held
    exactly rather than as a float that never divides evenly into a second.
    """
    if not frame_duration:
        return Fraction(1, 30)
    value = parse_seconds(frame_duration)
    if value <= 0:
        return Fraction(1, 30)
    return value


def to_frames(seconds: Fraction, frame_duration: Fraction) -> int:
    """Nearest whole frame to *seconds*, rounding halves away from zero.

    ``round()`` on a ``Fraction`` uses banker's rounding, which would send
    two cuts an equal distance from a beat in opposite directions.
    """
    quotient = seconds / frame_duration
    floor = quotient.numerator // quotient.denominator
    remainder = quotient - floor
    return floor + 1 if remainder >= Fraction(1, 2) else floor


def format_seconds(seconds: Fraction, frame_duration: Fraction) -> str:
    """Frame-align *seconds* and render it as an FCPXML time attribute.

    Final Cut rejects offsets that do not land on a frame boundary, so the
    value is snapped to the nearest frame before formatting. Whole seconds
    collapse to ``"12s"``; everything else keeps the format's own timebase
    as the denominator (``"86496410/24000s"``) rather than being reduced to
    a non-standard one.
    """
    frames = to_frames(seconds, frame_duration)
    exact = frames * frame_duration
    if exact.denominator == 1:
        return f"{exact.numerator}s"
    numerator = frames * frame_duration.numerator
    denominator = frame_duration.denominator
    return f"{numerator}/{denominator}s"
