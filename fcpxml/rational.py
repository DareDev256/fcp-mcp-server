"""Exact rational time arithmetic for FCPXML attributes.

Why this exists instead of :class:`~fcpxml.models.TimeValue`
------------------------------------------------------------

``TimeValue.from_timecode`` quantises through ``int(fps)``. That is lossless
for the integer frame rates the original fixtures used, and catastrophic for
the fractional ones real projects ship::

    TimeValue.from_timecode("3604s", 24000/1001).to_seconds()  -> 3756.96

``int(23.976...)`` is 23, so 3604 seconds is stored as ``86410/23s``. Any
connected-clip edit built on that arithmetic would move clips by minutes.

FCPXML times are exact rationals on the page (``86496410/24000s``). This
module keeps them exact — :class:`fractions.Fraction` in, frame-aligned
rational string out — and never involves a frame rate until the moment a
value has to be written back, where alignment to the frame grid is the
whole point.
"""

from fractions import Fraction

__all__ = [
    "parse_seconds",
    "format_seconds",
    "frame_duration_from_attr",
    "to_frames",
]


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
