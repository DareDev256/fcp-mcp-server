"""
Data models for Final Cut Pro FCPXML structures.

Provides a clean Python interface for working with Final Cut Pro
timelines, clips, markers, and other elements.
"""

import operator
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from functools import total_ordering
from math import gcd
from typing import Any, Callable, Dict, List, Optional, Tuple

from .rational import (
    frame_duration_seconds,
    nominal_fps,
    parse_seconds,
    rational_fps,
    tick_timebase,
)

# ============================================================================
# ENUMS
# ============================================================================

# Maximum length for marker type strings to prevent memory abuse
_MAX_MARKER_TYPE_LENGTH = 64


class MarkerType(Enum):
    """Types of markers in Final Cut Pro.

    Members:
        STANDARD   — Default marker with no completion state.
        INCOMPLETE — Task marker (completed="0" in FCPXML).  ← canonical name
        TODO       — Alias for INCOMPLETE. Kept for backward compatibility;
                     resolves to the same object (``MarkerType.TODO is
                     MarkerType.INCOMPLETE``).  Python enums treat the first
                     member with a given value as canonical; all subsequent
                     members sharing that value become aliases.
        CHAPTER    — Chapter marker (``<chapter-marker>`` element).
        COMPLETED  — Task marker with completed="1".

    Serialization helpers:
        ``from_string()``      — Accepts values, names, and legacy aliases
                                 (e.g. ``"todo-marker"``).  Always returns the
                                 canonical member.
        ``from_xml_element()`` — Reads an ``lxml``/``ElementTree`` element and
                                 returns the appropriate type based on the tag
                                 name and ``completed`` attribute.
        ``xml_tag``            — The FCPXML element tag to emit when writing.
        ``xml_attrs``          — Extra attributes required when writing (e.g.
                                 ``completed="0"`` for INCOMPLETE).
    """
    STANDARD = "standard"
    INCOMPLETE = "todo"
    TODO = "todo"  # Backward-compat alias — resolves to INCOMPLETE at runtime
    CHAPTER = "chapter"
    COMPLETED = "completed"

    @classmethod
    def from_string(cls, value: str) -> 'MarkerType':
        """Convert a string to MarkerType, accepting both enum names and values.

        Includes input validation: rejects null bytes, control characters,
        and excessively long strings to prevent injection and memory abuse.

        Examples:
            MarkerType.from_string("todo")      -> MarkerType.INCOMPLETE
            MarkerType.from_string("TODO")       -> MarkerType.INCOMPLETE
            MarkerType.from_string("completed")  -> MarkerType.COMPLETED
        """
        if not isinstance(value, str):
            raise TypeError(f"Expected str, got {type(value).__name__}")
        if '\x00' in value or any(ord(c) < 32 and c not in ('\n', '\r', '\t') for c in value):
            raise ValueError("Marker type contains invalid control characters")
        if len(value) > _MAX_MARKER_TYPE_LENGTH:
            raise ValueError(
                f"Marker type exceeds maximum length ({_MAX_MARKER_TYPE_LENGTH} chars)"
            )
        lowered = value.strip().lower()
        if not lowered:
            raise ValueError("Marker type cannot be empty")
        # Accept legacy aliases from older specs (e.g. "todo-marker" → INCOMPLETE)
        aliases = {
            "todo-marker": "todo",
            "completed-marker": "completed",
            "chapter-marker": "chapter",
        }
        lowered = aliases.get(lowered, lowered)
        try:
            return cls(lowered)
        except ValueError:
            raise ValueError(
                f"Invalid marker type: '{value}'. "
                f"Valid types: {', '.join(m.value for m in cls)}"
            )

    @classmethod
    def from_xml_element(cls, elem) -> 'MarkerType':
        """Determine MarkerType from an XML element's tag and attributes.

        Centralises the parse-side mapping so the parser doesn't need to
        know about completed-attribute semantics.

        Rules (in priority order):
          1. <chapter-marker> tag → CHAPTER (completed attr ignored)
          2. completed='0' (exact) → INCOMPLETE
          3. completed='1' (exact) → COMPLETED
          4. Everything else → STANDARD (including whitespace-padded,
             absent, empty, or non-boolean completed values)

        Matching is intentionally strict — no .strip(), no case folding.
        This prevents whitespace-injected attributes like ' 0 ' from
        being misclassified.
        """
        if elem.tag == 'chapter-marker':
            return cls.CHAPTER
        completed = elem.get('completed')
        if completed == '0':
            return cls.INCOMPLETE
        if completed == '1':
            return cls.COMPLETED
        return cls.STANDARD

    @property
    def xml_tag(self) -> str:
        """Return the FCPXML element tag for this marker type."""
        return 'chapter-marker' if self == MarkerType.CHAPTER else 'marker'

    @property
    def xml_attrs(self) -> dict:
        """Return extra XML attributes this marker type requires when writing.

        Centralises the write-side mapping so both FCPXMLModifier and
        FCPXMLWriter use a single source of truth.
        """
        if self == MarkerType.CHAPTER:
            return {'posterOffset': '0s'}
        if self == MarkerType.INCOMPLETE:
            return {'completed': '0'}
        if self == MarkerType.COMPLETED:
            return {'completed': '1'}
        return {}


# Recognised marker XML tags — used by the parser for single-pass collection
# and by the writer to validate element creation.
MARKER_XML_TAGS = ('marker', 'chapter-marker')


class MarkerColor(Enum):
    """Marker color options (FCP internal values)."""
    BLUE = 0
    CYAN = 1
    GREEN = 2
    YELLOW = 3
    ORANGE = 4
    RED = 5
    PINK = 6
    PURPLE = 7


class TransitionType(Enum):
    """Built-in transition types."""
    CROSS_DISSOLVE = "Cross Dissolve"
    FADE_TO_BLACK = "Fade to Color"
    FADE_FROM_BLACK = "Fade from Color"
    DIP_TO_COLOR = "Dip to Color"
    WIPE = "Wipe"
    SLIDE = "Slide"


class PacingStyle(Enum):
    """Pacing presets for rough cut generation."""
    SLOW = "slow"      # 5-10 second cuts
    MEDIUM = "medium"  # 2-5 second cuts
    FAST = "fast"      # 0.5-2 second cuts
    DYNAMIC = "dynamic"  # Varies throughout


class FlashFrameSeverity(Enum):
    """Severity levels for flash frame detection."""
    CRITICAL = "critical"  # < 2 frames, almost certainly an error
    WARNING = "warning"    # < 6 frames, potentially intentional but suspicious


class PacingCurve(Enum):
    """Pacing curves for montage generation."""
    CONSTANT = "constant"        # Same clip duration throughout
    ACCELERATING = "accelerating"  # Starts slow, gets faster
    DECELERATING = "decelerating"  # Starts fast, gets slower
    PYRAMID = "pyramid"          # Slow → fast → slow


class ValidationIssueType(Enum):
    """Types of timeline validation issues."""
    FLASH_FRAME = "flash_frame"
    GAP = "gap"
    DUPLICATE = "duplicate"
    ORPHAN_REF = "orphan_ref"
    INVALID_OFFSET = "invalid_offset"
    # DTD validation types (v0.6.0)
    ELEMENT_ORDER = "element_order"
    MISSING_ATTRIBUTE = "missing_attribute"
    INVALID_TIMEBASE = "invalid_timebase"
    FRAME_MISALIGNMENT = "frame_misalignment"
    MISSING_EFFECT_REF = "missing_effect_ref"
    MISSING_MEDIA_REP = "missing_media_rep"


# ============================================================================
# TIME VALUE - Rational Time Representation
# ============================================================================

# Standard FCPXML timebase denominators that FCP's DTD validator accepts.
# TimeValue.to_fcpxml() only simplifies fractions when the result uses one
# of these denominators, preventing values like "8/3s" that FCP rejects.
_FCPXML_STANDARD_TIMEBASES = frozenset({
    1, 24, 25, 30, 48, 50, 60, 90, 96, 100, 120,
    240, 600, 2400, 4800, 9600, 48000,
    # The NTSC-fractional family. Final Cut writes frameDuration="1001/24000s"
    # for a 23.98 sequence, so 24000 is not merely accepted — it is what FCP
    # itself emits. Their absence had the validator warning "Non-standard
    # timebase denominator 24000" about Apple's own output.
    24000, 30000, 60000, 120000,
})


@total_ordering
@dataclass
class TimeValue:
    """
    Represents time in FCPXML's rational format.

    FCPXML uses fractions of seconds (e.g., "90/30s" for 3 seconds at 30fps).
    This class handles conversion between timecode, seconds, and FCPXML format.

    Examples:
        TimeValue(90, 30)  # 3 seconds at 30fps
        TimeValue(1, 1)    # 1 second
        TimeValue.from_timecode("00:01:30:15", fps=30)  # 90.5 seconds
    """
    numerator: int
    denominator: int = 1

    def __post_init__(self):
        if self.denominator == 0:
            raise ValueError(
                f"TimeValue denominator cannot be zero (got {self.numerator}/0). "
                "This would corrupt all downstream time calculations."
            )
        # Normalize sign: denominator must always be positive.
        # Cross-multiplication in __lt__/__eq__ assumes positive denominators;
        # __hash__ assumes canonical form.  Without this, TimeValue(1, -2)
        # compares/hashes incorrectly against TimeValue(-1, 2).
        if self.denominator < 0:
            # Use object.__setattr__ because dataclass may be frozen-like
            object.__setattr__(self, 'numerator', -self.numerator)
            object.__setattr__(self, 'denominator', -self.denominator)

    @classmethod
    def _from_frames(cls, frames: int, fps: float) -> 'TimeValue':
        """Build a TimeValue from a whole frame count at an exact rate.

        ``fps = p/q`` means one frame is ``q/p`` seconds, so *n* frames is
        ``n*q/p``. At an integer rate that reduces to the old ``(frames, fps)``
        pair; at 23.98 it is ``(frames*1001, 24000)`` rather than the
        ``(frames, 23)`` that ``int(fps)`` produced.
        """
        duration = frame_duration_seconds(fps)
        return cls(frames * duration.numerator, duration.denominator)

    @classmethod
    def from_timecode(cls, tc: str, fps: float = 30.0) -> 'TimeValue':
        """
        Create TimeValue from various string formats.

        Supported formats:
        - "HH:MM:SS:FF" - Standard timecode
        - "HH:MM:SS;FF" - Drop-frame timecode
        - "30s" - Seconds
        - "90/30s" - FCPXML rational format
        - "15f" - Frames

        Seconds are kept exact. ``"3604s"`` is already an exact rational on
        the FCPXML page, so quantising it to the frame grid on the way in
        only loses information — and at a broadcast rate it lost 152 seconds
        of it (#17). Frame counts and SMPTE strings *are* frame quantities,
        so those resolve through the exact rate.
        """
        if not tc:
            return cls(0, 1)

        tc = str(tc).strip()

        # FCPXML format: "90/30s" or "30s"
        if tc.endswith('s'):
            tc_val = tc[:-1]
            if '/' in tc_val:
                parts = tc_val.split('/', 1)
                num, denom = int(parts[0]), int(parts[1])
                if denom == 0:
                    raise ValueError(f"Zero denominator in timecode: {tc}")
                return cls(num, denom)
            else:
                exact = Fraction(tc_val)
                return cls(exact.numerator, exact.denominator)

        # Frame format: "15f"
        if tc.endswith('f'):
            return cls._from_frames(int(tc[:-1]), fps)

        # Timecode format: "HH:MM:SS:FF" or "HH:MM:SS;FF"
        if ':' in tc or ';' in tc:
            parts = tc.replace(';', ':').split(':')
            # Non-drop timecode counts `nominal` frames per labelled second
            # (24 at 23.98), which is not the same number as the real rate.
            nominal = nominal_fps(fps)
            if len(parts) == 4:
                h, m, s, f = map(int, parts)
                return cls._from_frames((h * 3600 + m * 60 + s) * nominal + f, fps)
            elif len(parts) == 3:
                h, m, s = map(int, parts)
                return cls._from_frames((h * 3600 + m * 60 + s) * nominal, fps)

        # Try as plain number (seconds)
        try:
            exact = Fraction(tc)
        except (ValueError, ZeroDivisionError):
            raise ValueError(f"Invalid timecode format: {tc}")
        return cls(exact.numerator, exact.denominator)

    @classmethod
    def from_seconds(cls, seconds: float, fps: float = 30.0) -> 'TimeValue':
        """Create TimeValue from decimal seconds, snapped to the frame grid."""
        duration = frame_duration_seconds(fps)
        frames = int(round(Fraction(seconds).limit_denominator(1000000) / duration))
        return cls._from_frames(frames, fps)

    @classmethod
    def zero(cls) -> 'TimeValue':
        """Return zero time value."""
        return cls(0, 1)

    def to_fcpxml(self) -> str:
        """Convert to FCPXML time string (e.g., "90/30s").

        Only simplifies when the denominator reduces to 1 (whole seconds)
        or stays a standard FCPXML timebase. Avoids producing denominators
        like 3, 7, etc. that FCP's DTD validator may reject.
        """
        simplified = self.simplify()
        if simplified.denominator == 1:
            return f"{simplified.numerator}s"
        # Keep original denominator if simplification produces a non-standard
        # denominator (not a multiple of common timebases: 24, 30, 25, 2400)
        if simplified.denominator in _FCPXML_STANDARD_TIMEBASES:
            return f"{simplified.numerator}/{simplified.denominator}s"
        # Fall back to unsimplified form
        return f"{self.numerator}/{self.denominator}s"

    def to_seconds(self) -> float:
        """Convert to decimal seconds."""
        return self.numerator / self.denominator

    def to_timecode(self, fps: float = 30.0) -> str:
        """Convert to HH:MM:SS:FF timecode string.

        The frame count comes from the exact rate; the labelled seconds come
        from the nominal one (24 frames per second of 23.98 non-drop). Using
        ``int(fps)`` for both gave 23 frames per second, which both mislabels
        every second and makes frame 23 unrepresentable.
        """
        total_frames = self.to_frames(fps)
        total_secs, frames = divmod(total_frames, nominal_fps(fps))
        total_mins, secs = divmod(total_secs, 60)
        hours, mins = divmod(total_mins, 60)
        return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"

    def to_frames(self, fps: float = 30.0) -> int:
        """Convert to frame count on the exact frame grid."""
        exact = Fraction(self.numerator, self.denominator) * rational_fps(fps)
        return round(exact.numerator / exact.denominator)

    def simplify(self) -> 'TimeValue':
        """Reduce fraction to simplest form."""
        if self.numerator == 0:
            return TimeValue(0, 1)
        divisor = gcd(abs(self.numerator), abs(self.denominator))
        return TimeValue(
            self.numerator // divisor,
            self.denominator // divisor
        )

    @staticmethod
    def _lcm_denom(d1: int, d2: int) -> int:
        """LCM of two denominators for cross-timebase arithmetic."""
        return d1 // gcd(d1, d2) * d2

    def _binop(self, other: 'TimeValue', op: Callable[[int, int], int]) -> 'TimeValue':
        """Shared logic for add/sub: same-denom fast path, then LCM alignment."""
        if self.denominator == other.denominator:
            return TimeValue(op(self.numerator, other.numerator), self.denominator)
        lcd = TimeValue._lcm_denom(self.denominator, other.denominator)
        return TimeValue(
            op(
                self.numerator * (lcd // self.denominator),
                other.numerator * (lcd // other.denominator),
            ),
            lcd,
        )

    def __add__(self, other: 'TimeValue') -> 'TimeValue':
        return self._binop(other, operator.add)

    def __sub__(self, other: 'TimeValue') -> 'TimeValue':
        return self._binop(other, operator.sub)

    def __mul__(self, scalar: float) -> 'TimeValue':
        new_num = round(self.numerator * scalar)
        return TimeValue(new_num, self.denominator)

    def __truediv__(self, scalar: float) -> 'TimeValue':
        if scalar == 0:
            raise ZeroDivisionError("Cannot divide TimeValue by zero")
        new_denom = round(self.denominator * scalar)
        if new_denom == 0:
            raise ZeroDivisionError(
                f"Division by {scalar} rounds denominator {self.denominator} to zero"
            )
        return TimeValue(self.numerator, new_denom)

    def __lt__(self, other: 'TimeValue') -> bool:
        # Cross-multiply to compare without float conversion:
        # a/b < c/d  ↔  a*d < c*b  (denominators are always positive)
        return self.numerator * other.denominator < other.numerator * self.denominator

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TimeValue):
            return False
        # Cross-multiply for exact integer comparison
        return self.numerator * other.denominator == other.numerator * self.denominator

    def __hash__(self) -> int:
        # Delegate to simplify() — single source of truth for canonical form.
        # __post_init__ guarantees denominator > 0, so no zero guard needed.
        s = self.simplify()
        return hash((s.numerator, s.denominator))

    def snap_to_frame(self, fps: float) -> 'TimeValue':
        """Round this time value to the nearest frame boundary at the given fps.

        Integer rates use the 2400-tick timebase (LCM of the common integer
        frame rates) so results land on clean frame boundaries. NTSC rates
        cannot: a 23.98 frame is 100.1 ticks of 2400, so those snap in the
        format's own timebase (1001 ticks of 24000) instead. See
        :func:`fcpxml.rational.tick_timebase`.

        Args:
            fps: Frame rate to snap to (e.g. 24, 30, 23.976)

        Returns:
            New TimeValue on a whole frame of *fps*.

        Raises:
            ValueError: if *fps* is not positive.
        """
        ticks_per_second, ticks_per_frame = tick_timebase(fps)
        exact_ticks = Fraction(self.numerator, self.denominator) * ticks_per_second
        total_ticks = round(exact_ticks.numerator / exact_ticks.denominator)
        snapped_ticks = round(total_ticks / ticks_per_frame) * ticks_per_frame
        return TimeValue(snapped_ticks, ticks_per_second)

    def is_standard_timebase(self) -> bool:
        """Whether this value can be WRITTEN on an FCP-accepted timebase.

        Checks the denominator as written as well as the simplified one,
        because that is exactly what ``to_fcpxml()`` emits: it prefers the
        simplified form and falls back to the original denominator when
        simplifying would produce a non-standard one.

        Inspecting only the simplified form made the predicate disagree with
        the emitter. ``TimeValue(36, 24)`` — 36 frames at 24fps, and the
        canonical form Final Cut Pro itself writes — simplifies to 3/2, so it
        reported non-standard while ``to_fcpxml()`` correctly wrote "36/24s".
        Every generated timeline therefore logged a validation warning for
        every clip that was not a whole number of seconds long, which is most
        of them. An alarm that fires on correct output is not an alarm.
        """
        if self.denominator in _FCPXML_STANDARD_TIMEBASES:
            return True
        return self.simplify().denominator in _FCPXML_STANDARD_TIMEBASES

    def __repr__(self) -> str:
        return f"TimeValue({self.numerator}/{self.denominator}s = {self.to_seconds():.3f}s)"


# ============================================================================
# TIMECODE (Legacy compatibility - wraps TimeValue)
# ============================================================================

@dataclass
class Timecode:
    """
    Represents a timecode value.

    Note: This class exists for backwards compatibility with the parser.
    New code should prefer TimeValue for rational time math.
    """
    frames: int
    frame_rate: float = 24.0
    drop_frame: bool = False

    @property
    def _exact_seconds(self) -> Fraction:
        """Exact seconds for this frame count at the exact frame rate."""
        return self.frames * frame_duration_seconds(self.frame_rate)

    @property
    def seconds(self) -> float:
        if not self.frame_rate:
            raise ZeroDivisionError(
                "Timecode.frame_rate is zero — a frame count has no duration "
                "without a rate."
            )
        return float(self._exact_seconds)

    @property
    def total_frames(self) -> int:
        return self.frames

    def to_smpte(self) -> str:
        """Convert to SMPTE timecode string (HH:MM:SS:FF)."""
        nominal = nominal_fps(self.frame_rate)
        total_seconds, frames = divmod(self.frames, nominal)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        separator = ";" if self.drop_frame else ":"
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{frames:02d}"

    @classmethod
    def from_rational(cls, rational_str: str, frame_rate: float = 24.0) -> "Timecode":
        """Parse FCPXML rational time format (e.g., '3600/24s')."""
        if not rational_str:
            return cls(frames=0, frame_rate=frame_rate)
        seconds = parse_seconds(rational_str)
        frames = int(seconds / frame_duration_seconds(frame_rate))
        return cls(frames=frames, frame_rate=frame_rate)

    def to_rational(self) -> str:
        """Convert to FCPXML rational format.

        Kept unsimplified in the rate's own timebase — ``"48/24s"``, not
        ``"2s"`` — because that is the shape Final Cut writes and what
        downstream consumers of this writer already expect. At 23.98 the
        timebase is 24000, so 24 frames is ``"24024/24000s"``.
        """
        duration = frame_duration_seconds(self.frame_rate)
        return f"{self.frames * duration.numerator}/{duration.denominator}s"

    def to_time_value(self) -> TimeValue:
        """Convert to TimeValue for rational math."""
        return TimeValue._from_frames(self.frames, self.frame_rate)


# ============================================================================
# CORE MODELS
# ============================================================================

@dataclass
class Keyword:
    """Represents a keyword/tag applied to a clip."""
    value: str
    start: Optional[Timecode] = None
    duration: Optional[Timecode] = None


@dataclass
class Marker:
    """Represents a marker in the timeline."""
    name: str
    start: Timecode
    duration: Optional[Timecode] = None
    marker_type: MarkerType = MarkerType.STANDARD
    note: str = ""
    color: Optional[MarkerColor] = None

    def to_youtube_timestamp(self) -> str:
        """Format as YouTube chapter timestamp."""
        total_seconds = int(self.start.seconds)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"


@dataclass
class Clip:
    """Represents a clip in the timeline."""
    name: str
    start: Timecode
    duration: Timecode
    source_start: Optional[Timecode] = None
    source_end: Optional[Timecode] = None
    media_path: str = ""
    markers: List[Marker] = field(default_factory=list)
    keywords: List[Keyword] = field(default_factory=list)

    # Extended metadata
    rating: int = 0  # 0=unrated, 1-5 stars
    is_favorite: bool = False
    is_rejected: bool = False

    # Roles (FCP audio/video role assignments)
    audio_role: str = ""
    video_role: str = ""

    # Connected clips (B-roll, titles, audio attached to this clip)
    connected_clips: List['ConnectedClip'] = field(default_factory=list)

    @property
    def end(self) -> Timecode:
        return Timecode(
            frames=self.start.frames + self.duration.frames,
            frame_rate=self.start.frame_rate
        )

    @property
    def duration_seconds(self) -> float:
        return self.duration.seconds

    @property
    def keyword_values(self) -> List[str]:
        """Get list of keyword strings."""
        return [k.value for k in self.keywords]


@dataclass
class AudioClip(Clip):
    """Audio-specific clip."""
    channels: int = 2
    sample_rate: int = 48000
    role: str = "dialogue"


@dataclass
class VideoClip(Clip):
    """Video-specific clip."""
    width: int = 1920
    height: int = 1080
    has_audio: bool = True


@dataclass
class ConnectedClip:
    """A clip connected to a primary storyline clip (B-roll, titles, audio).

    In FCP's magnetic timeline, connected clips hang off spine clips via lanes.
    Positive lanes are above (video overlays), negative lanes are below (audio).
    """
    name: str
    start: Timecode
    duration: Timecode
    lane: int = 1
    offset: Optional[Timecode] = None
    source_start: Optional[Timecode] = None
    media_path: str = ""
    clip_type: str = "asset-clip"
    role: str = ""
    ref_id: str = ""
    parent_clip_name: str = ""
    markers: List[Marker] = field(default_factory=list)
    keywords: List[Keyword] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return self.duration.seconds


@dataclass
class CompoundClip:
    """A compound clip (ref-clip) containing a nested timeline."""
    name: str
    ref_id: str
    duration: Timecode
    start: Timecode
    clips: List[Clip] = field(default_factory=list)
    connected_clips: List[ConnectedClip] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return self.duration.seconds


@dataclass
class SilenceCandidate:
    """A potential silence region detected by timeline heuristics."""
    start_timecode: str
    duration_seconds: float
    reason: str  # "gap", "ultra_short", "name_match", "duration_anomaly"
    confidence: float = 0.5  # 0.0 to 1.0
    clip_name: Optional[str] = None
    clip_index: Optional[int] = None


@dataclass
class Transition:
    """Represents a transition between clips."""
    name: str
    duration: Timecode
    start: Timecode
    transition_type: str = "cross-dissolve"


@dataclass
class Timeline:
    """Represents a Final Cut Pro timeline/sequence."""
    name: str
    duration: Timecode
    frame_rate: float = 24.0
    width: int = 1920
    height: int = 1080
    clips: List[Clip] = field(default_factory=list)
    audio_clips: List[AudioClip] = field(default_factory=list)
    transitions: List[Transition] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    connected_clips: List[ConnectedClip] = field(default_factory=list)
    compound_clips: List[CompoundClip] = field(default_factory=list)

    @property
    def total_clips(self) -> int:
        return len(self.clips)

    @property
    def total_cuts(self) -> int:
        return max(0, len(self.clips) - 1)

    @property
    def average_clip_duration(self) -> float:
        if not self.clips:
            return 0.0
        return sum(c.duration_seconds for c in self.clips) / len(self.clips)

    @property
    def cuts_per_minute(self) -> float:
        """Average cuts per minute."""
        if self.duration.seconds <= 0:
            return 0.0
        return (self.total_cuts / self.duration.seconds) * 60

    def get_clips_shorter_than(self, seconds: float) -> List[Clip]:
        """Find clips shorter than threshold (flash frame detection)."""
        return [c for c in self.clips if c.duration_seconds < seconds]

    def get_clips_longer_than(self, seconds: float) -> List[Clip]:
        """Find clips longer than threshold."""
        return [c for c in self.clips if c.duration_seconds > seconds]

    def get_clip_at(self, timecode: float) -> Optional[Clip]:
        """Find the clip at a specific timecode (seconds)."""
        for clip in self.clips:
            start_sec = clip.start.seconds
            end_sec = clip.end.seconds
            if start_sec <= timecode < end_sec:
                return clip
        return None

    def get_clips_by_keyword(self, keyword: str) -> List[Clip]:
        """Find all clips with a specific keyword."""
        return [c for c in self.clips if keyword in c.keyword_values]

    @property
    def origin_seconds(self) -> float:
        """The timeline's zero point in absolute seconds.

        Final Cut starts sequences at 01:00:00:00 by broadcast convention, so
        a real export's element offsets begin at 3600s while ``tcStart`` still
        reads ``0s``. Derived from the earliest element rather than trusted
        from that attribute — anything reporting a position to a human has to
        subtract this or the first frame of the edit reads as one hour in.
        """
        starts = [float(c.start.seconds or 0) for c in self.clips if c.start]
        starts += [
            float(c.offset.seconds or 0)
            for c in self.connected_clips if c.offset
        ]
        return min(starts) if starts else 0.0


@dataclass
class Project:
    """Represents a Final Cut Pro project/library."""
    name: str
    timelines: List[Timeline] = field(default_factory=list)
    fcpxml_version: str = "1.13"

    @property
    def primary_timeline(self) -> Optional[Timeline]:
        return self.timelines[0] if self.timelines else None


# ============================================================================
# ROUGH CUT MODELS
# ============================================================================

@dataclass
class SegmentSpec:
    """Specification for a segment in auto rough cut."""
    name: str
    keywords: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    priority: str = "best"  # favorites, longest, shortest, random, best


@dataclass
class PacingConfig:
    """Configuration for rough cut pacing."""
    pacing: str = "medium"  # slow, medium, fast, dynamic
    min_clip_duration: float = 1.0
    max_clip_duration: float = 8.0
    avg_clip_duration: Optional[float] = None
    vary_pacing: bool = True

    def get_duration_range(self) -> Tuple[float, float]:
        """Get min/max based on pacing style."""
        ranges = {
            "slow": (5.0, 10.0),
            "medium": (2.0, 5.0),
            "fast": (0.5, 2.0),
            "dynamic": (1.0, 6.0),
        }
        return ranges.get(self.pacing, (2.0, 5.0))


@dataclass
class RoughCutResult:
    """Result of auto rough cut generation."""
    output_path: str
    clips_used: int
    clips_available: int
    target_duration: float
    actual_duration: float
    segments: int
    average_clip_duration: float


# ============================================================================
# SPEED CUTTING & VALIDATION MODELS (v0.3.0)
# ============================================================================

@dataclass
class FlashFrame:
    """
    Represents a detected flash frame (ultra-short clip).

    Flash frames are typically editing errors - clips that are too short
    to be perceived as intentional cuts.
    """
    clip_name: str
    clip_id: str
    start: Timecode
    duration_frames: int
    duration_seconds: float
    severity: 'FlashFrameSeverity'

    @property
    def is_critical(self) -> bool:
        """Check if this is a critical flash frame."""
        return self.severity == FlashFrameSeverity.CRITICAL


@dataclass
class GapInfo:
    """
    Represents a detected gap in the timeline.

    Gaps can be intentional (black frames) or errors from deleted clips.
    """
    start: Timecode
    duration_frames: int
    duration_seconds: float
    previous_clip: Optional[str] = None  # Clip name before the gap
    next_clip: Optional[str] = None      # Clip name after the gap

    @property
    def timecode(self) -> str:
        """Get timecode string for the gap start."""
        return self.start.to_smpte()


@dataclass
class DuplicateGroup:
    """
    Represents a group of clips using the same source media.

    Useful for detecting duplicate clips that may be unintentional.
    """
    source_ref: str  # The asset/media reference ID
    source_name: str  # Human-readable source name
    clips: List[Dict[str, Any]] = field(default_factory=list)  # List of clip info dicts

    @property
    def count(self) -> int:
        """Number of clips using this source."""
        return len(self.clips)

    @property
    def has_overlapping_ranges(self) -> bool:
        """Check if any clips use overlapping portions of the source."""
        # Sort clips by source_start
        sorted_clips = sorted(self.clips, key=lambda c: c.get('source_start', 0))
        for i in range(len(sorted_clips) - 1):
            curr_end = sorted_clips[i].get('source_start', 0) + sorted_clips[i].get('source_duration', 0)
            next_start = sorted_clips[i + 1].get('source_start', 0)
            if curr_end > next_start:
                return True
        return False


@dataclass
class ValidationIssue:
    """
    Represents a single validation issue found in a timeline.

    Used by validate_timeline to report problems.
    """
    issue_type: 'ValidationIssueType'
    severity: str  # "error", "warning", "info"
    message: str
    timecode: Optional[str] = None
    clip_name: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """
    Result of timeline validation.

    Provides a health score and categorized list of issues.
    """
    is_valid: bool
    health_score: int  # 0-100 percentage
    issues: List[ValidationIssue] = field(default_factory=list)
    flash_frames: List[FlashFrame] = field(default_factory=list)
    gaps: List[GapInfo] = field(default_factory=list)
    duplicates: List[DuplicateGroup] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len([i for i in self.issues if i.severity == "error"])

    @property
    def warning_count(self) -> int:
        return len([i for i in self.issues if i.severity == "warning"])

    def summary(self) -> str:
        """Generate a summary string."""
        return (
            f"Timeline Health: {self.health_score}% | "
            f"Errors: {self.error_count} | Warnings: {self.warning_count} | "
            f"Flash frames: {len(self.flash_frames)} | Gaps: {len(self.gaps)}"
        )


@dataclass
class MontageConfig:
    """Configuration for montage generation with pacing curves."""
    target_duration: float  # Target duration in seconds
    pacing_curve: 'PacingCurve'
    start_duration: float = 2.0  # Clip duration at start
    end_duration: float = 0.5    # Clip duration at end
    min_duration: float = 0.2    # Minimum allowed clip duration
    max_duration: float = 5.0    # Maximum allowed clip duration

    def get_duration_at_position(self, position: float) -> float:
        """
        Calculate clip duration for a given position (0.0 to 1.0).

        Args:
            position: Position in montage (0.0 = start, 1.0 = end)

        Returns:
            Target duration in seconds for a clip at this position
        """
        if self.pacing_curve == PacingCurve.CONSTANT:
            duration = (self.start_duration + self.end_duration) / 2

        elif self.pacing_curve == PacingCurve.ACCELERATING:
            # Linear interpolation from start to end duration
            duration = self.start_duration + (self.end_duration - self.start_duration) * position

        elif self.pacing_curve == PacingCurve.DECELERATING:
            # Reverse: start fast, end slow
            duration = self.end_duration + (self.start_duration - self.end_duration) * position

        elif self.pacing_curve == PacingCurve.PYRAMID:
            # Slow → fast → slow (parabolic curve)
            if position < 0.5:
                # First half: slow to fast
                duration = self.start_duration + (self.end_duration - self.start_duration) * (position * 2)
            else:
                # Second half: fast to slow
                duration = self.end_duration + (self.start_duration - self.end_duration) * ((position - 0.5) * 2)
        else:
            duration = self.start_duration

        # Clamp to min/max
        return max(self.min_duration, min(self.max_duration, duration))
