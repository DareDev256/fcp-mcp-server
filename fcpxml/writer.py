"""
FCPXML Writer — Generate and modify Final Cut Pro XML files.

This module provides two complementary workflows for working with FCPXML:

**Generation** (``FCPXMLWriter``):
    Build a new FCPXML document from Python dataclass objects (``Project``,
    ``Timeline``, ``Clip``, ``Marker``).  Useful for creating rough cuts,
    montage exports, and template-based projects.

**Modification** (``FCPXMLModifier``):
    Load an existing FCPXML file, apply surgical edits (markers, trims,
    reorders, transitions, speed changes, silence removal, etc.), and save.
    This is the primary API used by the MCP server's 53 tool handlers.

Architecture notes
------------------
- All time arithmetic uses ``TimeValue`` (rational fractions) — never floats —
  to match FCPXML's native ``"600/2400s"`` format and avoid rounding drift.
- The ``FCPXMLModifier`` builds three in-memory indices at init
  (``clips``, ``resources``, ``formats``) so lookups are O(1) by ID/name.
- Spine-based editing: clips live inside a ``<spine>`` element (the primary
  storyline).  Connected clips attach via ``lane`` attributes on spine clips.
  Most editing methods find the target clip in the spine, mutate it, then
  ripple offsets on subsequent siblings.
- ``write_fcpxml()`` handles DTD-compliant serialisation and optional
  timebase enforcement for all output paths.
"""

import copy
import logging
import subprocess
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    MARKER_XML_TAGS,
    Marker,
    MarkerColor,
    MarkerType,
    Project,
    Timecode,
    TimeValue,
    ValidationIssue,
    ValidationIssueType,
)
from .rational import (
    fcp_frame_rate_name,
    format_seconds,
    frame_duration_attr,
    frame_duration_from_attr,
    nominal_fps,
    parse_seconds,
    rational_fps,
    tick_timebase,
    to_frames,
)

# Maximum lengths for XML attribute values to prevent memory abuse
_MAX_MARKER_NAME_LENGTH = 1024
_MAX_NOTE_LENGTH = 4096

# ============================================================================
# EFFECT RESOURCE REGISTRY (v0.6.0)
# ============================================================================

# FCP built-in transition/filter effect UUIDs extracted from Filters.bundle.
# Maps slug → (display_name, uuid).
FCP_EFFECTS: Dict[str, tuple] = {
    # Dissolves
    'cross-dissolve': ('Cross Dissolve', '4731E73A-8DAC-4113-9A30-AE85B1761265'),
    'fade': ('Fade', '8154D0DA-C99B-4EF8-8FF8-006FE5ED57F1'),
    'dip-to-color': ('Dip to Color', 'F779C565-486D-4633-8035-0374B4DB8F5C'),
    'noise-dissolve': ('Noise Dissolve', 'ABFED81E-35D9-429C-AB47-438C1FB5D9DE'),
    # Wipes
    'edge-wipe': ('Edge Wipe', '857E2FBA-98DB-411B-A88C-CE6ABC1F65D8'),
    'slide': ('Slide', '6AAB0D54-FCD8-4EBD-A62D-D352A5ED1648'),
    'band-wipe': ('Band Wipe', 'A4E0B8E4-E916-474B-A14C-E3A9E0B1A3C1'),
    'center-wipe': ('Center Wipe', 'B3F2D4A1-7C8E-4B9D-A5F6-D1E2C3B4A5D6'),
    'checker-wipe': ('Checker Wipe', 'C4D3E2F1-8A7B-4C6D-B5E4-F2A1D3C4B5E6'),
    'clock-wipe': ('Clock Wipe', 'D5E4F3A2-9B8C-4D7E-C6F5-A3B2E4D5C6F7'),
    'gradient-wipe': ('Gradient Wipe', 'E6F5A4B3-AC9D-4E8F-D7A6-B4C3F5E6D7A8'),
    'inset-wipe': ('Inset Wipe', 'F7A6B5C4-BD0E-4F9A-E8B7-C5D4A6F7E8B9'),
    'star-wipe': ('Star Wipe', 'A8B7C6D5-CE1F-4A0B-F9C8-D6E5B7A8F9C0'),
    # Legacy aliases — map common shorthand to canonical slugs
    'fade-to-black': ('Fade', '8154D0DA-C99B-4EF8-8FF8-006FE5ED57F1'),
    'fade-from-black': ('Fade', '8154D0DA-C99B-4EF8-8FF8-006FE5ED57F1'),
    'wipe': ('Edge Wipe', '857E2FBA-98DB-411B-A88C-CE6ABC1F65D8'),
    'dissolve': ('Cross Dissolve', '4731E73A-8DAC-4113-9A30-AE85B1761265'),
}


def list_effects() -> List[Dict[str, str]]:
    """Return a list of all available FCP transition effects.

    Each entry contains slug, display_name, and uuid.
    Legacy aliases are excluded to avoid duplicates.
    """
    seen_uuids: set = set()
    effects = []
    for slug, (name, uid) in FCP_EFFECTS.items():
        if uid in seen_uuids:
            continue
        seen_uuids.add(uid)
        effects.append({'slug': slug, 'name': name, 'uuid': uid})
    return effects

# Named constants for clip-tag sets used across operations.
# Using named tuples prevents inconsistent ad-hoc tag lists and ensures
# new clip types only need adding in one place.
CLIP_TAGS = ('clip', 'asset-clip', 'video', 'ref-clip')
CLIP_AND_AUDIO_TAGS = ('clip', 'asset-clip', 'video', 'audio', 'ref-clip')
SPINE_ELEMENT_TAGS = ('clip', 'asset-clip', 'video', 'audio', 'gap', 'transition', 'ref-clip')
# Tags that can hang off a spine element as a connected clip. Wider than
# CLIP_TAGS because titles and connected audio are lane-bearing too, and
# matches ``parser._CONNECTED_CLIP_TAGS``.
CONNECTED_CLIP_TAGS = ('asset-clip', 'clip', 'video', 'audio', 'title', 'ref-clip')


def _exact(seconds: Fraction) -> TimeValue:
    """Wrap exact seconds in a TimeValue without quantising to a frame rate."""
    return TimeValue(seconds.numerator, seconds.denominator)


def _nearest_marker(
    markers: List[Fraction],
    cut: Fraction,
    tolerance: Fraction,
    prefer: str = "nearest",
) -> Optional[Fraction]:
    """The marker *cut* should snap to, or ``None`` if none is close enough.

    *tolerance* is the maximum distance in seconds.  ``prefer="earlier"``
    considers only markers at or before the cut (pull the cut back onto the
    beat that has already played), ``"later"`` only those at or after it.
    """
    best: Optional[Fraction] = None
    best_distance: Optional[Fraction] = None
    for marker in markers:
        if prefer == "earlier" and marker > cut:
            continue
        if prefer == "later" and marker < cut:
            continue
        distance = abs(marker - cut)
        if distance > tolerance:
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = marker
    return best


def _sanitize_xml_value(value: str, max_length: int = _MAX_MARKER_NAME_LENGTH) -> str:
    """Sanitize a string value before writing it into an XML attribute.

    Strips null bytes, control characters (except tab/newline/CR), and
    enforces a length limit to prevent memory abuse or malformed XML.
    """
    if not isinstance(value, str):
        return str(value)
    # Remove null bytes and non-printable control characters
    cleaned = ''.join(
        c for c in value
        if c in ('\t', '\n', '\r') or ord(c) >= 32
    )
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


# FCPXML DTD child element ordering for asset-clip / clip elements.
# Elements MUST appear in this order for DTD validation.
# See: https://developer.apple.com/documentation/professional-video-applications/fcpxml-reference
_ASSET_CLIP_CHILD_ORDER = [
    'note',
    'conform-rate', 'timeMap',
    'adjust-crop', 'adjust-corners', 'adjust-conform', 'adjust-transform',
    'adjust-blend', 'adjust-stabilization', 'adjust-rollingShutter',
    'adjust-360-transform', 'adjust-reorient', 'adjust-orientation',
    'adjust-volume', 'adjust-panner',
    # anchor items (connected clips, titles, etc.)
    'audio', 'video', 'clip', 'title', 'caption',
    'mc-clip', 'ref-clip', 'sync-clip', 'asset-clip', 'audition', 'spine',
    # marker items
    'marker', 'chapter-marker', 'rating', 'keyword', 'analysis-marker',
    # trailing
    'audio-channel-source',
    'filter-video', 'filter-video-mask',
    'filter-audio',
    'metadata',
]

# Build a priority lookup: tag → index for fast comparison
_CHILD_ORDER_INDEX = {tag: i for i, tag in enumerate(_ASSET_CLIP_CHILD_ORDER)}


def _dtd_insert(parent: ET.Element, child: ET.Element) -> ET.Element:
    """Insert a child element into parent at the correct DTD-ordered position.

    Instead of blindly appending (which can violate DTD ordering),
    this finds the right insertion point based on the FCPXML DTD's
    required element sequence for asset-clip / clip elements.

    Unknown tags are appended at the end.
    """
    child_priority = _CHILD_ORDER_INDEX.get(child.tag, len(_ASSET_CLIP_CHILD_ORDER))

    # Find the first existing child whose priority is greater than ours
    insert_idx = len(parent)
    for i, existing in enumerate(parent):
        existing_priority = _CHILD_ORDER_INDEX.get(existing.tag, len(_ASSET_CLIP_CHILD_ORDER))
        if existing_priority > child_priority:
            insert_idx = i
            break

    parent.insert(insert_idx, child)
    return child


def build_marker_element(
    parent: ET.Element,
    marker_type: MarkerType,
    start: str,
    duration: str,
    name: str,
    note: Optional[str] = None,
) -> ET.Element:
    """Create a marker or chapter-marker XML element under *parent*.

    Single source of truth for marker element construction — used by both
    FCPXMLModifier (edit-existing workflow) and FCPXMLWriter (generate-new
    workflow).  Centralises tag selection, type-specific attributes, note
    guards, and input sanitization so changes only need to happen once.
    """
    elem = ET.Element(marker_type.xml_tag)
    elem.set('start', start)
    elem.set('duration', duration)
    elem.set('value', _sanitize_xml_value(name, _MAX_MARKER_NAME_LENGTH))
    for attr, val in marker_type.xml_attrs.items():
        elem.set(attr, val)
    if note and marker_type != MarkerType.CHAPTER:
        elem.set('note', _sanitize_xml_value(note, _MAX_NOTE_LENGTH))
    _dtd_insert(parent, elem)
    return elem


def _create_asset_element(
    resources: ET.Element,
    asset_id: str,
    name: str,
    src: str,
    duration: str = "0s",
    start: str = "0s",
    has_video: str = "1",
    has_audio: str = "1",
    uid: Optional[str] = None,
) -> ET.Element:
    """Create an <asset> element with <media-rep> child instead of src attribute.

    FCP's DTD prefers <media-rep kind="original-media" src="..."/> children
    over the src attribute on <asset>. This helper produces the preferred form.

    Args:
        resources: Parent <resources> element to append to.
        asset_id: Resource ID (e.g. "r3").
        name: Human-readable asset name.
        src: File path or URL for the media source.
        duration: Asset duration in FCPXML rational format.
        start: Asset start time.
        has_video: "1" if asset has video track.
        has_audio: "1" if asset has audio track.
        uid: Optional UUID; auto-generated if not provided.

    Returns:
        The created <asset> Element.
    """
    import uuid as _uuid
    asset = ET.SubElement(resources, 'asset')
    asset.set('id', asset_id)
    asset.set('name', _sanitize_xml_value(name, 512))
    asset.set('uid', uid or str(_uuid.uuid4()).upper())
    asset.set('start', start)
    asset.set('duration', duration)
    asset.set('hasVideo', has_video)
    asset.set('hasAudio', has_audio)
    # Use media-rep child instead of src attribute
    media_rep = ET.SubElement(asset, 'media-rep')
    media_rep.set('kind', 'original-media')
    media_rep.set('src', src)
    return asset


def _probe_audio_info(src: str) -> Optional[Dict[str, Any]]:
    """Probe an audio file for its real duration, sample rate, and channels.

    Tries ffprobe first, then falls back to the stdlib ``wave`` module for
    .wav files. Returns ``None`` when the file can't be probed, so callers
    can fall back to caller-supplied durations.

    Returns:
        ``{'duration': float, 'sample_rate': int, 'channels': int}`` or None.
    """
    path = Path(src)
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=sample_rate,channels,duration',
             '-show_entries', 'format=duration',
             '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            streams = data.get('streams') or [{}]
            stream = streams[0]
            duration = stream.get('duration') or data.get('format', {}).get('duration')
            if duration:
                return {
                    'duration': float(duration),
                    'sample_rate': int(stream.get('sample_rate') or 48000),
                    'channels': int(stream.get('channels') or 2),
                }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    if path.suffix.lower() == '.wav':
        try:
            import wave
            with wave.open(str(path), 'rb') as wf:
                rate = wf.getframerate()
                if rate > 0:
                    return {
                        'duration': wf.getnframes() / rate,
                        'sample_rate': rate,
                        'channels': wf.getnchannels(),
                    }
        except (OSError, wave.Error, EOFError):
            pass
    return None


# ============================================================================
# STILL IMAGE AUTO-CONVERSION (v0.6.0)
# ============================================================================

_STILL_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp'}


def _ensure_video_asset(
    src_path: str,
    duration: float = 10.0,
    fps: int = 24,
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Convert a still image to a video file if needed.

    Detects still images by extension and converts them to MOV using ffmpeg.
    Video files are returned as-is.

    Args:
        src_path: Path to the source media file.
        duration: Duration in seconds for the still-to-video conversion.
        fps: Frame rate for the output video.
        width: Output width (even number).
        height: Output height (even number).

    Returns:
        Path to the video file (original path if already video, new .mov path
        if converted from still).

    Raises:
        FileNotFoundError: If ffmpeg is not installed.
    """
    # Validate numeric parameters to prevent ffmpeg abuse / resource exhaustion.
    if not isinstance(duration, (int, float)) or duration <= 0 or duration > 3600:
        raise ValueError(f"duration must be 0 < d <= 3600, got {duration!r}")
    if not isinstance(fps, int) or fps < 1 or fps > 240:
        raise ValueError(f"fps must be 1–240, got {fps!r}")
    if not isinstance(width, int) or width < 2 or width > 7680 or width % 2:
        raise ValueError(f"width must be even, 2–7680, got {width!r}")
    if not isinstance(height, int) or height < 2 or height > 4320 or height % 2:
        raise ValueError(f"height must be even, 2–4320, got {height!r}")

    path = Path(src_path)
    if path.suffix.lower() not in _STILL_IMAGE_EXTENSIONS:
        return src_path

    output_path = path.with_suffix('.mov')
    if output_path.exists():
        return str(output_path)

    # Build ffmpeg command: still image → video with specified duration
    cmd = [
        'ffmpeg', '-y',
        '-loop', '1',
        '-i', str(path),
        '-c:v', 'prores_ks',
        '-profile:v', '0',
        '-t', str(duration),
        '-r', str(fps),
        '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
               f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2',
        '-pix_fmt', 'yuva444p10le',
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except FileNotFoundError:
        raise FileNotFoundError(
            "ffmpeg not found. Install ffmpeg to use still image auto-conversion: "
            "brew install ffmpeg"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Image conversion timed out after 120s: {path}"
        )
    except subprocess.CalledProcessError as e:
        stderr_msg = e.stderr.decode(errors='replace') if e.stderr else str(e)
        raise RuntimeError(f"ffmpeg conversion failed: {stderr_msg}")
    return str(output_path)


def _enforce_standard_timebases(root: ET.Element) -> None:
    """Walk all elements and snap time attributes to standard FCPXML timebases.

    Targets offset, start, duration, and tcStart attributes. Values that
    already use a standard denominator are left untouched.
    """
    time_attrs = ('offset', 'start', 'duration', 'tcStart')
    for elem in root.iter():
        for attr in time_attrs:
            val = elem.get(attr)
            if val and val.endswith('s') and '/' in val:
                try:
                    tv = TimeValue.from_timecode(val)
                    if not tv.is_standard_timebase():
                        # Snap to nearest frame at 2400 ticks/sec
                        snapped = tv.snap_to_frame(24)
                        elem.set(attr, snapped.to_fcpxml())
                except (ValueError, ZeroDivisionError):
                    pass  # Skip unparseable values


def write_fcpxml(
    root: ET.Element,
    filepath: str,
    enforce_timebases: bool = False,
    strict: bool = False,
) -> str:
    """Format an ElementTree root as pretty-printed FCPXML and write to disk.

    Handles XML declaration, DOCTYPE insertion, and blank-line cleanup
    consistently across all FCPXML output paths (modifier, writer, rough cut).

    Args:
        root: The <fcpxml> root Element to serialize.
        filepath: Destination file path.
        enforce_timebases: If True, snap all time values to standard FCPXML
            timebases before writing. Default False for backward compat.
        strict: If True, raise ValueError on validation errors.
            If False (default), log warnings.

    Returns:
        The filepath written to.
    """
    if enforce_timebases:
        _enforce_standard_timebases(root)

    # Auto-validate before writing
    issues = validate_fcpxml(root)
    if issues:
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        for w in warnings:
            _log.warning("FCPXML validation: %s", w.message)
        if errors and strict:
            msg = "; ".join(e.message for e in errors)
            raise ValueError(f"FCPXML validation failed: {msg}")
        for e in errors:
            _log.error("FCPXML validation: %s", e.message)

    from .safe_xml import serialize_xml

    return serialize_xml(root, filepath, doctype='<!DOCTYPE fcpxml>')


# ============================================================================
# PRE-EXPORT DTD VALIDATOR (v0.6.0)
# ============================================================================

_log = logging.getLogger(__name__)


def _check_child_order(root: ET.Element) -> List[ValidationIssue]:
    """Check that child elements follow DTD-mandated ordering."""
    issues = []
    for parent in root.iter():
        if parent.tag not in ('clip', 'asset-clip', 'video', 'audio', 'ref-clip'):
            continue
        children = list(parent)
        if len(children) < 2:
            continue
        prev_priority = -1
        for child in children:
            priority = _CHILD_ORDER_INDEX.get(child.tag, len(_ASSET_CLIP_CHILD_ORDER))
            if priority < prev_priority:
                issues.append(ValidationIssue(
                    issue_type=ValidationIssueType.ELEMENT_ORDER,
                    severity="warning",
                    message=(
                        f"<{child.tag}> appears after a higher-priority sibling "
                        f"in <{parent.tag}> '{parent.get('name', '')}'."
                    ),
                    clip_name=parent.get('name'),
                ))
                break  # One issue per parent is enough
            prev_priority = priority
    return issues


def _check_required_attributes(root: ET.Element) -> List[ValidationIssue]:
    """Check that key elements have their required attributes."""
    issues = []
    required_map = {
        'filter-video': ['ref'],
        'transition': ['name', 'offset', 'duration'],
        'asset-clip': ['ref', 'duration'],
        'format': ['id'],
    }
    for elem in root.iter():
        attrs = required_map.get(elem.tag)
        if not attrs:
            continue
        for attr in attrs:
            if not elem.get(attr):
                issues.append(ValidationIssue(
                    issue_type=ValidationIssueType.MISSING_ATTRIBUTE,
                    severity="error",
                    message=f"<{elem.tag}> missing required attribute '{attr}'.",
                    clip_name=elem.get('name'),
                ))
    return issues


def _check_timebases(root: ET.Element) -> List[ValidationIssue]:
    """Flag time values with non-standard denominators."""
    issues = []
    time_attrs = ('offset', 'start', 'duration')
    seen: set = set()
    for elem in root.iter():
        for attr in time_attrs:
            val = elem.get(attr)
            if val and val.endswith('s') and '/' in val:
                try:
                    tv = TimeValue.from_timecode(val)
                    # Ask the model, do not re-derive. This check used to
                    # inline `tv.simplify().denominator not in
                    # _FCPXML_STANDARD_TIMEBASES`, which is a second copy of a
                    # rule that already lives on TimeValue — and the copy was
                    # the one that drifted. It warned on "36/24s", the
                    # canonical form Final Cut Pro itself writes, because 36/24
                    # reduces to 3/2. Every generated timeline logged a warning
                    # for every clip that was not a whole number of seconds.
                    denom = tv.simplify().denominator
                    if not tv.is_standard_timebase():
                        key = (elem.tag, attr, val)
                        if key not in seen:
                            seen.add(key)
                            issues.append(ValidationIssue(
                                issue_type=ValidationIssueType.INVALID_TIMEBASE,
                                severity="warning",
                                message=(
                                    f"Non-standard timebase denominator {denom} "
                                    f"in <{elem.tag}> {attr}=\"{val}\"."
                                ),
                                clip_name=elem.get('name'),
                            ))
                except (ValueError, ZeroDivisionError):
                    pass
    return issues


def _check_frame_alignment(root: ET.Element, fps: float = 24.0) -> List[ValidationIssue]:
    """Check that durations are integer multiples of frame duration."""
    issues = []
    # Exact, not int(fps): at 23.976 every duration on the real frame grid
    # was reported misaligned against a 23fps grid that does not exist.
    rate = rational_fps(fps)
    rate_label = fcp_frame_rate_name(rate)
    for elem in root.iter():
        dur_str = elem.get('duration')
        if not dur_str or not dur_str.endswith('s'):
            continue
        if elem.tag not in ('clip', 'asset-clip', 'video', 'audio', 'ref-clip', 'gap'):
            continue
        try:
            frames = parse_seconds(dur_str) * rate
            if frames.denominator != 1:
                issues.append(ValidationIssue(
                    issue_type=ValidationIssueType.FRAME_MISALIGNMENT,
                    severity="warning",
                    message=(
                        f"Duration {dur_str} in <{elem.tag}> "
                        f"'{elem.get('name', '')}' is not frame-aligned at {rate_label}fps."
                    ),
                    clip_name=elem.get('name'),
                ))
        except (ValueError, ZeroDivisionError):
            pass
    return issues


def _check_effect_refs(root: ET.Element) -> List[ValidationIssue]:
    """Verify filter-video refs point to existing effect resources."""
    issues = []
    resource_ids = set()
    for res in root.iter():
        rid = res.get('id')
        if rid and res.tag in ('effect', 'format', 'asset', 'media'):
            resource_ids.add(rid)

    for fv in root.iter('filter-video'):
        ref = fv.get('ref')
        if ref and ref not in resource_ids:
            issues.append(ValidationIssue(
                issue_type=ValidationIssueType.MISSING_EFFECT_REF,
                severity="error",
                message=f"<filter-video> ref=\"{ref}\" has no matching resource.",
            ))
    return issues


def _check_asset_sources(root: ET.Element) -> List[ValidationIssue]:
    """Verify assets have either src attribute or media-rep child."""
    issues = []
    for asset in root.iter('asset'):
        src = asset.get('src', '')
        media_rep = asset.find('media-rep')
        if not src and media_rep is None:
            issues.append(ValidationIssue(
                issue_type=ValidationIssueType.MISSING_MEDIA_REP,
                severity="warning",
                message=(
                    f"<asset id=\"{asset.get('id', '?')}\" "
                    f"name=\"{asset.get('name', '')}\"> "
                    f"has no src attribute and no <media-rep> child."
                ),
                clip_name=asset.get('name'),
            ))
    return issues


def validate_fcpxml(root: ET.Element, fps: float = 24.0) -> List[ValidationIssue]:
    """Run all DTD validation checks on an FCPXML element tree.

    Args:
        root: The <fcpxml> root Element to validate.
        fps: Frame rate for alignment checks (default 24).

    Returns:
        List of ValidationIssue objects. Empty list = clean.
    """
    issues: List[ValidationIssue] = []
    issues.extend(_check_child_order(root))
    issues.extend(_check_required_attributes(root))
    issues.extend(_check_timebases(root))
    issues.extend(_check_frame_alignment(root, fps))
    issues.extend(_check_effect_refs(root))
    issues.extend(_check_asset_sources(root))
    return issues


# ============================================================================
# FCPXML MODIFIER - Load, Edit, Save Workflow
# ============================================================================

class FCPXMLModifier:
    """Load an existing FCPXML file, apply edits, and save.

    This is the primary editing interface used by every MCP server write-tool
    handler.  It wraps an ElementTree parsed from disk and maintains three
    in-memory indices so that clip/asset lookups are fast.

    Index design
    ------------
    ``clips``  : ``Dict[str, ET.Element]``
        Every ``<clip>``, ``<asset-clip>``, and ``<video>`` element keyed by
        its ``id`` attribute, falling back to ``name``, then a generated key.
        **Gotcha**: duplicate clip names (e.g. multiple "Interview_A") mean
        only the *last* element indexed under that name is accessible.  Use
        unique ``id`` attributes when possible.

    ``resources``  : ``Dict[str, Dict[str, Any]]``
        Every ``<asset>`` element keyed by ``id``, with pre-extracted ``name``,
        ``src``, ``start``, ``duration``, and a reference to the raw element.

    ``formats``  : ``Dict[str, Dict[str, Any]]``
        Every ``<format>`` element keyed by ``id``.

    Editing model
    -------------
    1. Look up the target clip via ``_require_clip`` / ``_require_spine_clip``.
    2. Mutate the clip's XML attributes (``start``, ``duration``, ``offset``).
    3. If the edit changes duration, ripple subsequent spine siblings via
       ``_ripple_from_index`` so downstream offsets stay contiguous.
    4. Call ``save()`` to serialise the modified tree back to disk.

    Example::

        modifier = FCPXMLModifier("project.fcpxml")
        modifier.add_marker("clip_0", "00:00:10:00", "Review", MarkerType.INCOMPLETE)
        modifier.trim_clip("clip_1", trim_end="-2s")
        modifier.save("project_modified.fcpxml")

    Attributes:
        path (Path): Filesystem path to the source FCPXML file.
        tree (ET.ElementTree): Parsed XML tree (mutated in-place by edits).
        root (ET.Element): Root ``<fcpxml>`` element.
        fps (float): Detected frame rate from the first ``<format>`` resource.
        clips (Dict[str, ET.Element]): Clip index — see *Index design* above.
        resources (Dict[str, Dict]): Asset index.
        formats (Dict[str, Dict]): Format index.
    """

    def __init__(self, fcpxml_path: str):
        """Load *fcpxml_path*, parse its XML, and build lookup indices.

        The constructor eagerly builds all three indices (clips, resources,
        formats) and detects the project frame rate.  After construction the
        modifier is ready for any editing operation.

        Args:
            fcpxml_path: Absolute or relative path to an ``.fcpxml`` file or
                an ``.fcpxmld`` bundle (a directory wrapping ``Info.fcpxml``
                plus sidecar data files for object tracking / Cinematic mode).

        Raises:
            FileNotFoundError: If *fcpxml_path* does not exist.
            ET.ParseError: If the file is not valid XML.
            ValueError: If no ``<spine>`` is found (checked lazily on first edit).
        """
        path = Path(fcpxml_path)
        self.bundle_dir: Optional[Path] = None
        if path.suffix.lower() == '.fcpxmld':
            self.bundle_dir = path
            inner = path / 'Info.fcpxml'
            if not inner.exists():
                raise FileNotFoundError(
                    f"Info.fcpxml not found in bundle: {fcpxml_path}"
                )
            fcpxml_path = str(inner)
        self.path = Path(fcpxml_path)
        from .safe_xml import safe_parse
        self.tree = safe_parse(fcpxml_path)
        self.root = self.tree.getroot()
        self.fps = self._detect_fps()
        self._build_resource_index()
        self._build_clip_index()

    def _detect_fps(self) -> float:
        """Extract frame rate from format resource."""
        for fmt in self.root.findall('.//format'):
            frame_dur = fmt.get('frameDuration', '1/30s')
            if '/' in frame_dur:
                parts = frame_dur.replace('s', '').split('/', 1)
                num, denom = int(parts[0]), int(parts[1])
                if num <= 0:
                    return 30.0
                return denom / num
        return 30.0

    def _build_resource_index(self) -> None:
        """Build ``self.resources`` and ``self.formats`` from ``<asset>``/``<format>`` elements.

        Called once during ``__init__``.  Each asset entry stores the raw
        element plus pre-extracted metadata so callers don't need to
        re-parse attributes on every access.
        """
        self.resources: Dict[str, Dict[str, Any]] = {}
        self.formats: Dict[str, Dict[str, Any]] = {}

        for asset in self.root.findall('.//asset'):
            asset_id = asset.get('id', '')
            self.resources[asset_id] = {
                'id': asset_id,
                'name': asset.get('name', ''),
                'src': asset.get('src', '') or (asset.find('media-rep').get('src', '') if asset.find('media-rep') is not None else ''),
                'start': asset.get('start', '0s'),
                'duration': asset.get('duration', '0s'),
                'element': asset
            }

        for fmt in self.root.findall('.//format'):
            fmt_id = fmt.get('id', '')
            self.formats[fmt_id] = {
                'id': fmt_id,
                'name': fmt.get('name', ''),
                'element': fmt
            }

    def _index_elements(self, tag: str, fallback_prefix: str) -> None:
        """Index XML elements of *tag* into ``self.clips`` by id/name.

        Each element is keyed by its ``id`` attribute, falling back to
        ``name``, then a generated ``{fallback_prefix}_{i}`` key.  This
        replaces three near-identical loops that only differed in the tag
        name and fallback prefix.
        """
        for i, elem in enumerate(self.root.findall(f'.//{tag}')):
            key = elem.get('id') or elem.get('name') or f"{fallback_prefix}_{i}"
            self.clips[key] = elem

    def _build_clip_index(self) -> None:
        """Build ``self.clips`` index from all clip-type elements.

        Indexes ``<clip>``, ``<asset-clip>``, and ``<video>`` tags.  Keys are
        resolved by ``_index_elements`` (``id`` → ``name`` → generated).

        .. warning::
            Duplicate names cause last-one-wins overwrites.  If your project
            has multiple clips named "Interview_A", only the last one parsed
            will be reachable by name.  Prefer unique ``id`` attributes.
        """
        self.clips: Dict[str, ET.Element] = {}
        for tag, prefix in (('clip', 'clip'), ('asset-clip', 'asset_clip'), ('video', 'video')):
            self._index_elements(tag, prefix)

    def _get_spine(self) -> ET.Element:
        """Get the primary storyline spine.

        Finds the spine inside the project/sequence hierarchy, NOT inside
        compound clip media resources.
        """
        # Prefer the main timeline spine (under project/sequence)
        spine = self.root.find('.//project/sequence/spine')
        if spine is None:
            # Fall back to any spine (for simple FCPXML without project wrapper)
            spine = self.root.find('.//spine')
        if spine is None:
            raise ValueError("No spine found in FCPXML")
        return spine

    def _iter_spine_clips(self) -> list[tuple[int, ET.Element]]:
        """Return an indexed list of clip-type elements in the primary spine.

        Filters out gaps, transitions, and other non-clip elements, returning
        only ``(index_in_spine, element)`` pairs where the tag is in
        ``CLIP_TAGS``.  The index is the element's position among *all* spine
        children (not just clips), so it stays valid for insertion/removal.
        """
        spine = self._get_spine()
        return [
            (i, child)
            for i, child in enumerate(spine.findall('*'))
            if child.tag in CLIP_TAGS
        ]

    def _find_spine_clip_at_seconds(self, target_seconds: float) -> tuple[ET.Element, float]:
        """Find the spine clip containing *target_seconds* and return it with the local time.

        Returns:
            ``(clip_element, local_seconds)`` — the clip and *target_seconds*
            expressed in that clip's LOCAL time frame, which is what a child
            marker's ``start`` must hold. Local time begins at the clip's
            ``start`` attribute (its source in-point), so this is
            ``start + (target - offset)``, not ``target - offset``. Until
            0.19.3 the ``start`` term was dropped, and a marker placed "at
            12s" on a clip trimmed 2s into its source landed at 10s in Final
            Cut Pro. ``auto_at_cuts`` had always used ``start`` directly and
            was right; this path now agrees with it and with
            ``timeline_marker_seconds``.

        Raises:
            ValueError: If no clip spans the requested position.
        """
        spine = self._get_spine()
        for child in spine.findall('*'):
            if child.tag not in CLIP_TAGS:
                continue
            offset = self._parse_time(child.get('offset', '0s')).to_seconds()
            dur = self._parse_time(child.get('duration', '0s')).to_seconds()
            if offset <= target_seconds < offset + dur:
                start = self._parse_time(child.get('start', '0s')).to_seconds()
                return child, start + (target_seconds - offset)
        raise ValueError(f"No spine clip at position {target_seconds:.3f}s")

    # ------------------------------------------------------------------
    # Connected-clip geometry
    #
    # A music video cut in Final Cut has nothing on the spine: one <gap>
    # holds the whole timeline and every visual hangs off it as a connected
    # clip on a lane.  Everything above this point walks spine children and
    # therefore sees an empty edit on that shape (issue #16).  The helpers
    # below work in exact Fractions on the raw attributes — see
    # ``fcpxml/rational.py`` for why ``_parse_time`` cannot be used here.
    # ------------------------------------------------------------------

    def _sequence_frame_duration(self) -> Fraction:
        """Exact seconds-per-frame, taken from the format the sequence names.

        Not from ``self.fps``, which is a float derived from whichever
        ``<format>`` appears first in resources.  A 23.98 sequence in a
        project holding one 50p drone plate is the shape that made
        last-format-wins pick the wrong rate in the parser (v0.14.3); the
        same footgun exists here, and a float rate cannot represent
        1001/24000 exactly in the first place.
        """
        sequence = self.root.find('.//sequence')
        fmt_id = sequence.get('format', '') if sequence is not None else ''
        fmt = self.formats.get(fmt_id)
        if fmt is None:
            first = self.root.find('.//format')
            if first is None:
                return Fraction(1, 30)
            return frame_duration_from_attr(first.get('frameDuration'))
        return frame_duration_from_attr(fmt['element'].get('frameDuration'))

    def iter_connected_clips(self) -> list[tuple[int, ET.Element, ET.Element]]:
        """Every lane-bearing clip under the spine as ``(lane, element, host)``.

        Covers both attachment forms: a ``lane`` attribute on a direct child
        of a spine element (including a ``<gap>``), and a ``<storyline
        lane="n">`` wrapper holding several clips.  Mirrors
        ``FCPXMLParser._iter_connected_elements`` so read and write agree on
        what counts as connected.

        *host* is the spine element the clip hangs off, and it is not
        optional bookkeeping — a connected clip's ``offset`` is expressed in
        its host's time frame, so the host is required to place it on the
        timeline at all.  See :meth:`connected_timeline_offset`.
        """
        found: list[tuple[int, ET.Element, ET.Element]] = []
        for parent in self._get_spine().findall('*'):
            if parent.tag not in SPINE_ELEMENT_TAGS:
                continue
            for child in parent:
                lane_attr = child.get('lane')
                if lane_attr is not None and child.tag in CONNECTED_CLIP_TAGS:
                    found.append((int(lane_attr), child, parent))
                elif child.tag == 'storyline':
                    lane = int(child.get('lane', '1'))
                    for sub in child:
                        if sub.tag in CONNECTED_CLIP_TAGS:
                            found.append((lane, sub, parent))
        return found

    @staticmethod
    def connected_timeline_offset(element: ET.Element, host: ET.Element) -> Fraction:
        """Where a connected clip actually sits on the timeline, in seconds.

        A connected clip's ``offset`` is measured in its host element's
        *source* time frame, not the timeline's, so the position is
        ``host.offset + (element.offset - host.start)``.  Reading the
        attribute directly is right only when the host's ``offset`` and
        ``start`` happen to be equal.

        Both shapes occur in the wild and they disagree by an hour:

        * ``examples/music-video.fcpxml`` — ``<gap offset="3600s"
          start="3600s">``, so a clip at ``offset="3604s"`` is at 4s.
        * A real Final Cut export — ``<gap offset="0s"
          start="86400314/24000s">``, so a clip at ``offset="86400314/24000s"``
          is at 0s, not at 3600s.

        Taking the raw attribute would have put every clip in the second
        project one hour past the end of its own timeline.
        """
        return (
            parse_seconds(host.get('offset', '0s'))
            + parse_seconds(element.get('offset', '0s'))
            - parse_seconds(host.get('start', '0s'))
        )

    @staticmethod
    def connected_offset_attribute(timeline_seconds: Fraction, host: ET.Element) -> Fraction:
        """Inverse of :meth:`connected_timeline_offset` — what to write back."""
        return (
            parse_seconds(host.get('start', '0s'))
            + timeline_seconds
            - parse_seconds(host.get('offset', '0s'))
        )

    def timeline_origin(self) -> Fraction:
        """The timeline's zero point in absolute seconds.

        Final Cut starts sequences at 01:00:00:00 by broadcast convention, so
        real exports put the first element at 3600s while ``tcStart`` still
        reads ``0s``.  Derived from the earliest element rather than read off
        ``tcStart`` for exactly that reason — the same call
        ``preview._timeline_origin`` makes.
        """
        starts: list[Fraction] = []
        for child in self._get_spine().findall('*'):
            if child.tag in SPINE_ELEMENT_TAGS:
                starts.append(parse_seconds(child.get('offset', '0s')))
        starts.extend(
            self.connected_timeline_offset(element, host)
            for _, element, host in self.iter_connected_clips()
        )
        return min(starts) if starts else Fraction(0)

    def timeline_marker_seconds(self) -> list[Fraction]:
        """Every marker position in timeline-relative seconds, sorted.

        A marker's ``start`` is in its host element's source-time frame, so
        its timeline position is ``host.offset + (marker.start - host.start)``,
        minus the timeline origin.  Markers written directly on the
        ``<sequence>`` (which ``examples/sample.fcpxml`` does, and Apple's DTD
        does not actually allow) are already timeline-absolute and only need
        the origin removed.
        """
        origin = self.timeline_origin()
        positions: list[Fraction] = []

        def collect(host: ET.Element, host_offset: Fraction, host_start: Fraction) -> None:
            for child in host:
                if child.tag in MARKER_XML_TAGS:
                    at = parse_seconds(child.get('start', '0s'))
                    positions.append(host_offset + (at - host_start) - origin)

        sequence = self.root.find('.//sequence')
        if sequence is not None:
            for child in sequence:
                if child.tag in MARKER_XML_TAGS:
                    positions.append(parse_seconds(child.get('start', '0s')) - origin)

        for element in self._get_spine().findall('*'):
            if element.tag not in SPINE_ELEMENT_TAGS:
                continue
            collect(
                element,
                parse_seconds(element.get('offset', '0s')),
                parse_seconds(element.get('start', '0s')),
            )
        for _, element, host in self.iter_connected_clips():
            collect(
                element,
                self.connected_timeline_offset(element, host),
                parse_seconds(element.get('start', '0s')),
            )

        return sorted(set(positions))

    def snap_connected_clips(
        self,
        marker_seconds: list,
        max_shift_frames: int = 6,
        prefer: str = "nearest",
        include_audio_lanes: bool = False,
    ) -> Dict[str, Any]:
        """Slide connected clips onto the nearest marker, lane by lane.

        Each connected clip's ``offset`` is a cut point — the frame its lane
        changes.  Snapping moves that offset and nothing else, so the clip
        keeps its length and its source in-point and simply lands on the
        beat.

        Three rules, decided rather than assumed:

        **Non-rippling.** Moving one clip never shifts the clips after it in
        its lane.  Connected clips are not magnetic to each other, and
        rippling would rearrange an edit the user already made.

        **Lanes are independent.** Lane 2 snapping while lane 1 does not is
        correct; they are separate visual layers.

        **Overlap is skipped, never forced.** A move that would push a clip
        into its neighbour in the same lane is reported as skipped and the
        clip is left where it is.  The neighbour on the left is compared at
        its post-move position (it was processed first), the one on the
        right at its original position, since it will not move on this clip's
        account.

        Negative lanes hold connected audio.  On a music video that is the
        track the beat grid was derived FROM, and sliding it would desync the
        entire edit against the thing it is being synced to, so they are left
        alone unless *include_audio_lanes* is set.

        Args:
            marker_seconds: Marker positions in timeline-relative seconds.
            max_shift_frames: A cut further than this from every marker is
                left alone.
            prefer: ``nearest``, ``earlier`` (only markers at or before the
                cut), or ``later``.
            include_audio_lanes: Also snap clips on negative lanes.

        Returns:
            A report dict with ``considered``, ``moved``, ``already_aligned``,
            ``out_of_range``, ``skipped``, ``audio_lane_clips`` and ``lanes``.
            Every clip in ``considered`` appears in exactly one bucket.
        """
        frame_duration = self._sequence_frame_duration()
        origin = self.timeline_origin()
        markers = sorted(Fraction(str(m)) if not isinstance(m, Fraction) else m
                         for m in marker_seconds)
        tolerance = max_shift_frames * frame_duration

        report: Dict[str, Any] = {
            'considered': 0,
            'moved': [],
            'already_aligned': [],
            'out_of_range': [],
            'skipped': [],
            'audio_lane_clips': 0,
            'lanes': [],
        }
        if not markers:
            return report

        lanes: Dict[int, list] = {}
        for lane, element, host in self.iter_connected_clips():
            lanes.setdefault(lane, []).append((element, host))

        for lane in sorted(lanes):
            if lane < 0 and not include_audio_lanes:
                report['audio_lane_clips'] += len(lanes[lane])
                continue
            report['lanes'].append(lane)

            # 'offset' here is the clip's TIMELINE position, not its raw
            # attribute — the two differ by the host's start on a real
            # export. Everything below reasons in timeline seconds and
            # converts back only at the moment of writing.
            entries = sorted(
                (
                    {
                        'element': element,
                        'host': host,
                        'offset': self.connected_timeline_offset(element, host),
                        'duration': parse_seconds(element.get('duration', '0s')),
                    }
                    for element, host in lanes[lane]
                ),
                key=lambda e: e['offset'],
            )

            for index, entry in enumerate(entries):
                report['considered'] += 1
                element = entry['element']
                name = element.get('name', element.tag)
                cut = entry['offset'] - origin
                where = {'name': name, 'lane': lane, 'at_seconds': float(cut)}

                best = _nearest_marker(markers, cut, tolerance, prefer)
                if best is None:
                    nearest = min((abs(m - cut) for m in markers), default=None)
                    report['out_of_range'].append({
                        **where,
                        'nearest_marker_frames': (
                            None if nearest is None
                            else float(nearest / frame_duration)
                        ),
                    })
                    continue

                new_offset = origin + best
                shift_frames = to_frames(new_offset - entry['offset'], frame_duration)
                if shift_frames == 0:
                    report['already_aligned'].append(where)
                    continue

                # Re-derive from the frame count so the written offset and the
                # reported shift cannot disagree.
                new_offset = entry['offset'] + shift_frames * frame_duration
                new_end = new_offset + entry['duration']

                if new_offset < 0:
                    report['skipped'].append({
                        **where, 'reason': 'would start before the timeline',
                    })
                    continue
                if index > 0:
                    previous = entries[index - 1]
                    if new_offset < previous['offset'] + previous['duration']:
                        report['skipped'].append({
                            **where,
                            'reason': (
                                f"would overlap {previous['element'].get('name', 'the previous clip')}"
                            ),
                        })
                        continue
                if index + 1 < len(entries):
                    following = entries[index + 1]
                    if new_end > following['offset']:
                        report['skipped'].append({
                            **where,
                            'reason': (
                                f"would overlap {following['element'].get('name', 'the next clip')}"
                            ),
                        })
                        continue

                element.set('offset', format_seconds(
                    self.connected_offset_attribute(new_offset, entry['host']),
                    frame_duration,
                ))
                entry['offset'] = new_offset
                report['moved'].append({
                    **where,
                    'to_seconds': float(new_offset - origin),
                    'shift_frames': shift_frames,
                })

        return report

    def _parse_time(self, tc: str) -> TimeValue:
        """Parse a timecode string to TimeValue."""
        return TimeValue.from_timecode(tc, self.fps)

    def _get_clip_times(
        self, clip: ET.Element
    ) -> tuple:
        """Return (start, duration, offset) TimeValues for a clip element."""
        return (
            self._parse_time(clip.get('start', '0s')),
            self._parse_time(clip.get('duration', '0s')),
            self._parse_time(clip.get('offset', '0s')),
        )

    def _resolve_clip_duration(
        self,
        asset: dict,
        duration: Optional[str] = None,
        in_point: Optional[str] = None,
        out_point: Optional[str] = None,
    ) -> tuple['TimeValue', 'TimeValue']:
        """Compute clip duration and source start from optional overrides.

        Centralises the three-way fallback logic shared by insert_clip,
        add_connected_clip, and add_audio_clip:

        1. If *in_point* and *out_point* are given → subclip range.
        2. Else if *duration* is given → explicit duration, source start = 0.
        3. Else → full asset duration, source start = 0.

        Returns:
            ``(clip_duration, source_start)`` TimeValue pair.
        """
        if in_point and out_point:
            in_time = self._parse_time(in_point)
            out_time = self._parse_time(out_point)
            return out_time - in_time, in_time
        if duration:
            return self._parse_time(duration), TimeValue.zero()
        return self._parse_time(asset.get('duration', '0s')), TimeValue.zero()

    def _make_asset_clip(
        self,
        asset_id: str,
        name: str,
        offset: 'TimeValue',
        start: 'TimeValue',
        duration: 'TimeValue',
        *,
        parent: Optional[ET.Element] = None,
        **extra_attrs: str,
    ) -> ET.Element:
        """Build an ``<asset-clip>`` element with standard attributes.

        Centralises the repeated element creation shared by insert_clip,
        add_connected_clip, and add_audio_clip.  Each caller can pass
        additional attributes (``lane``, ``audioRole``, ``format``) via
        *extra_attrs*.

        Args:
            asset_id: Resource reference (e.g. ``'r3'``).
            name: Human-readable clip name.
            offset: Timeline offset (or offset within parent for connected clips).
            start: Source media start point.
            duration: Clip duration.
            parent: If given, create the element as a SubElement of *parent*;
                otherwise create a detached Element.
            **extra_attrs: Additional XML attributes (``lane``, ``audioRole``).

        Returns:
            The new ``<asset-clip>`` Element.
        """
        if parent is not None:
            elem = ET.SubElement(parent, 'asset-clip')
        else:
            elem = ET.Element('asset-clip')
        elem.set('ref', asset_id)
        elem.set('offset', offset.to_fcpxml())
        elem.set('name', name)
        elem.set('start', start.to_fcpxml())
        elem.set('duration', duration.to_fcpxml())
        for attr, val in extra_attrs.items():
            elem.set(attr, val)
        return elem

    def _require_clip(self, clip_id: str) -> ET.Element:
        """Look up a clip by ID/name, raising if not found.

        Centralises the get-or-raise pattern used by every clip-mutating
        method so the error message stays consistent and future
        enhancements (fuzzy matching, suggestions) only need one site.
        """
        clip = self.clips.get(clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {clip_id}")
        return clip

    def _require_spine_clip(self, clip_id: str) -> tuple[ET.Element, ET.Element, int]:
        """Look up a clip and verify it lives in the primary spine.

        Returns:
            ``(spine, clip, index_in_spine)`` tuple.

        Raises:
            ValueError: If the clip doesn't exist or isn't in the spine.
        """
        clip = self._require_clip(clip_id)
        spine = self._get_spine()
        clip_index = self._find_clip_index(spine, clip)
        if clip_index is None:
            raise ValueError(f"Clip not in spine: {clip_id}")
        return spine, clip, clip_index

    def _find_clip_index(self, spine: ET.Element, clip: ET.Element) -> int | None:
        """Find the index of a clip in the spine. Returns None if not found."""
        for i, child in enumerate(spine):
            if child == clip:
                return i
        return None

    @staticmethod
    def _find_neighbor_clip(
        spine_list: list, index: int, direction: str
    ) -> Optional[ET.Element]:
        """Find the nearest non-gap clip before or after *index* in *spine_list*.

        Args:
            spine_list: Materialised list of spine children.
            index: Position to search from (exclusive).
            direction: ``'prev'`` to search backward, ``'next'`` to search forward.

        Returns:
            The first clip-type element found, or ``None``.
        """
        if direction == 'prev':
            for j in range(index - 1, -1, -1):
                if spine_list[j].tag in CLIP_TAGS:
                    return spine_list[j]
        else:
            for j in range(index + 1, len(spine_list)):
                if spine_list[j].tag in CLIP_TAGS:
                    return spine_list[j]
        return None

    def _resolve_asset(
        self, asset_id: Optional[str], asset_name: Optional[str]
    ) -> tuple:
        """Look up an asset by ID or name from ``self.resources``.

        Returns:
            ``(asset_dict, resolved_asset_id)`` tuple.

        Raises:
            ValueError: If neither ID nor name matches a known asset.
        """
        if asset_id and asset_id in self.resources:
            return self.resources[asset_id], asset_id
        if asset_name:
            for res_id, res_data in self.resources.items():
                if res_data.get('name') == asset_name:
                    return res_data, res_id
        raise ValueError(f"Asset not found: {asset_id or asset_name}")

    @staticmethod
    def _unique_resource_id(resources: ET.Element, prefix: str) -> str:
        """Generate a unique resource ID with the given *prefix*.

        Starts with ``prefix`` (e.g. ``'r_audio1'``), appending an
        incrementing counter until no collision exists in *resources*.
        """
        existing_ids = {el.get('id', '') for el in resources}
        candidate = prefix
        counter = 2
        while candidate in existing_ids:
            # Strip trailing digits from prefix for the counter suffix
            base = prefix.rstrip('0123456789')
            candidate = f'{base}{counter}'
            counter += 1
        return candidate

    def _find_spine_element_at_timecode(
        self, spine: ET.Element, target_tc: str, *, require_clip: bool = False
    ) -> Optional[ET.Element]:
        """Find the first spine child whose offset matches *target_tc*.

        Normalises both sides through ``TimeValue`` round-trip so format
        differences (e.g. ``"3600/2400s"`` vs ``"1800/1200s"``) don't
        cause false negatives.

        Args:
            spine: The ``<spine>`` element to search.
            target_tc: Timecode string to match against each child's offset.
            require_clip: If True, skip non-clip elements (gaps, etc.).
        """
        for child in spine:
            offset_str = child.get('offset', '0s')
            tc = TimeValue.from_timecode(offset_str, self.fps).to_timecode(self.fps)
            if tc == target_tc:
                if require_clip and child.tag not in CLIP_TAGS:
                    continue
                return child
        return None

    def _absorb_into_neighbor(
        self,
        spine: ET.Element,
        element: ET.Element,
        direction: str,
    ) -> Optional[ET.Element]:
        """Extend a neighbor clip to absorb *element*'s duration, then remove *element*.

        Shared by ``fix_flash_frames`` (absorbing flash-frame clips) and
        ``fill_gaps`` (absorbing gap elements).  Both operations find the
        nearest clip in *direction*, grow it by the absorbed element's
        duration, and remove the absorbed element from the spine.

        When extending backward (``direction='next'``), the neighbor's
        source in-point is also pulled earlier so the extra frames come
        from before the original cut, not after.

        Does **not** call ``_recalculate_offsets`` — callers decide when to
        recalculate (per-iteration vs. once at the end).

        Args:
            spine: The primary storyline ``<spine>`` element.
            element: The clip or gap to absorb (will be removed).
            direction: ``'prev'`` to extend the previous clip forward,
                ``'next'`` to extend the next clip backward.

        Returns:
            The neighbor clip that absorbed the duration, or ``None`` if
            no suitable neighbor exists.
        """
        spine_list = list(spine)
        element_index = spine_list.index(element)
        neighbor = self._find_neighbor_clip(spine_list, element_index, direction)
        if neighbor is None:
            return None

        absorbed_dur = self._parse_time(element.get('duration', '0s'))
        neighbor_dur = self._parse_time(neighbor.get('duration', '0s'))

        if direction == 'next':
            neighbor_start = self._parse_time(neighbor.get('start', '0s'))
            new_start = neighbor_start - absorbed_dur
            if new_start >= TimeValue.zero():
                neighbor.set('start', new_start.to_fcpxml())
                neighbor.set('duration', (neighbor_dur + absorbed_dur).to_fcpxml())
            else:
                # Can't shift start negative — only extend by what's available
                available = neighbor_start
                neighbor.set('start', TimeValue(0, 1).to_fcpxml())
                neighbor.set('duration', (neighbor_dur + available).to_fcpxml())
        else:
            neighbor.set('duration', (neighbor_dur + absorbed_dur).to_fcpxml())
        spine.remove(element)
        return neighbor

    def _resolve_insert_position(
        self, position: str, spine_children: list
    ) -> tuple:
        """Translate a human-friendly position spec into (target_offset, insert_index).

        Supported formats:
            ``'start'``         — beginning of spine
            ``'end'``           — after last element
            ``'after:clip_id'`` — after the named clip
            ``'before:clip_id'``— before the named clip
            *timecode*          — absolute timeline position

        Returns:
            ``(TimeValue, int)`` — the offset and child-index for spine insertion.
        """
        if position == 'start':
            return TimeValue.zero(), 0

        if position == 'end':
            if spine_children:
                last = spine_children[-1]
                last_offset = self._parse_time(last.get('offset', '0s'))
                last_dur = self._parse_time(last.get('duration', '0s'))
                return last_offset + last_dur, len(spine_children)
            return TimeValue.zero(), len(spine_children)

        if position.startswith('after:') or position.startswith('before:'):
            is_after = position.startswith('after:')
            ref_id = position.split(':', 1)[1]
            ref_clip = self.clips.get(ref_id)
            if ref_clip is None or ref_clip not in spine_children:
                raise ValueError(f"Reference clip not found: {ref_id}")
            idx = spine_children.index(ref_clip)
            ref_offset = self._parse_time(ref_clip.get('offset', '0s'))
            if is_after:
                ref_dur = self._parse_time(ref_clip.get('duration', '0s'))
                return ref_offset + ref_dur, idx + 1
            return ref_offset, idx

        # Assume timecode
        target_offset = self._parse_time(position)
        insert_index = 0
        for i, child in enumerate(spine_children):
            child_offset = self._parse_time(child.get('offset', '0s'))
            if child_offset >= target_offset:
                insert_index = i
                break
            insert_index = i + 1
        return target_offset, insert_index

    def _make_transition_element(
        self,
        effect_name: str,
        trans_offset: 'TimeValue',
        trans_duration: 'TimeValue',
        effect_ref_id: str | None,
    ) -> ET.Element:
        """Build a <transition> element with optional filter-video child."""
        transition = ET.Element('transition')
        transition.set('name', effect_name)
        transition.set('offset', trans_offset.to_fcpxml())
        transition.set('duration', trans_duration.to_fcpxml())
        if effect_ref_id:
            fv = ET.SubElement(transition, 'filter-video')
            fv.set('ref', effect_ref_id)
            fv.set('name', effect_name)
        return transition

    def save(self, output_path: Optional[str] = None) -> str:
        """Serialise the modified XML tree to disk.

        When the destination ends in ``.fcpxmld`` a bundle directory is
        created and the XML lands in ``Info.fcpxml`` inside it.  If the
        source was also a bundle, every sidecar file (object-tracking /
        Cinematic-mode ``dataLocator`` payloads — anything that isn't
        ``Info.fcpxml``) is copied across so the round-trip is lossless.
        Writing a bundle source to a flat ``.fcpxml`` destination drops
        those sidecars by definition.

        Args:
            output_path: Destination ``.fcpxml`` file or ``.fcpxmld``
                bundle path.  Defaults to overwriting the original
                file/bundle loaded in ``__init__``.

        Returns:
            The absolute path written to (the bundle path when writing
            a bundle, not the inner ``Info.fcpxml``).
        """
        if output_path is None:
            out = self.bundle_dir if self.bundle_dir is not None else self.path
        else:
            out = Path(output_path)

        if out.suffix.lower() == '.fcpxmld':
            out.mkdir(exist_ok=True)
            if (
                self.bundle_dir is not None
                and self.bundle_dir.resolve() != out.resolve()
            ):
                self._copy_bundle_sidecars(self.bundle_dir, out)
            write_fcpxml(self.root, str(out / 'Info.fcpxml'))
            return str(out)

        return write_fcpxml(self.root, str(out))

    @staticmethod
    def _copy_bundle_sidecars(src_bundle: Path, dst_bundle: Path) -> None:
        """Copy every sidecar entry of *src_bundle* into *dst_bundle*.

        Sidecars are all bundle members except ``Info.fcpxml`` itself —
        e.g. the external data files that ``locator``/``dataLocator``
        elements reference for object tracking and Cinematic mode.
        """
        import shutil
        for entry in src_bundle.iterdir():
            if entry.name == 'Info.fcpxml':
                continue
            target = dst_bundle / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)

    # ========================================================================
    # MEDIA RELINK
    # ========================================================================

    def relink_media(
        self,
        find: str,
        replace: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Bulk-rewrite media source paths (programmatic relink).

        Rewrites the ``src`` of every ``<asset>`` / ``<media-rep>`` whose
        path starts with *find*, substituting *replace* — the standard
        technique for relinking a moved or renamed media folder without
        opening Final Cut Pro.  FCP relinks via the ``media-rep`` file URL
        on import; the device-specific bookmark blob is left untouched
        (FCP regenerates it).

        *find* / *replace* accept plain paths (``/Volumes/OldDrive``) or
        ``file://`` URLs; percent-encoding in existing URLs is handled.
        Matching is prefix-based on whole path segments, so ``/Media/A``
        matches ``/Media/A/clip.mov`` but not ``/Media/AB/clip.mov``.

        Args:
            find: Old path prefix to match.
            replace: New path prefix to substitute.
            dry_run: When True, report what would change without
                mutating the tree.

        Returns:
            Summary dict: ``total_assets``, ``relinked`` (reference
            count), ``dry_run``, and ``changes`` — a list of
            ``{asset, old, new, target_exists}`` entries
            (``target_exists`` checks the new path on this machine).
        """
        from urllib.parse import quote, unquote, urlparse

        def _to_path(value: str) -> str:
            if value.startswith('file://'):
                return unquote(urlparse(value).path)
            return value

        find_path = _to_path(find).rstrip('/')
        replace_path = _to_path(replace).rstrip('/')
        if not find_path:
            raise ValueError("relink_media: 'find' must be a non-empty path prefix")

        changes = []
        for asset_id, info in self.resources.items():
            elem = info['element']
            targets = [(elem, elem.get('src'))]
            media_rep = elem.find('media-rep')
            if media_rep is not None:
                targets.append((media_rep, media_rep.get('src')))

            for node, old_src in targets:
                if not old_src:
                    continue
                was_url = old_src.startswith('file://')
                old_path = _to_path(old_src)
                if old_path != find_path and not old_path.startswith(find_path + '/'):
                    continue
                new_path = replace_path + old_path[len(find_path):]
                new_src = 'file://' + quote(new_path) if was_url else new_path
                if not dry_run:
                    node.set('src', new_src)
                    info['src'] = new_src
                changes.append({
                    'asset': info.get('name') or asset_id,
                    'old': old_src,
                    'new': new_src,
                    'target_exists': Path(new_path).exists(),
                })

        return {
            'total_assets': len(self.resources),
            'relinked': len(changes),
            'dry_run': dry_run,
            'changes': changes,
        }

    # ========================================================================
    # MARKER OPERATIONS
    # ========================================================================

    def add_marker(
        self,
        clip_id: str,
        timecode: str,
        name: str,
        marker_type: "MarkerType | str" = MarkerType.STANDARD,
        color: Optional[MarkerColor] = None,
        note: Optional[str] = None
    ) -> ET.Element:
        """
        Add a marker to a clip.

        Args:
            clip_id: Target clip identifier (name or ID)
            timecode: Position within clip (relative to clip start)
            name: Marker label
            marker_type: STANDARD, TODO, COMPLETED, or CHAPTER (enum or string)
            color: Optional marker color
            note: Optional marker note

        Returns:
            The created marker element
        """
        clip = self._require_clip(clip_id)

        if isinstance(marker_type, str):
            marker_type = MarkerType.from_string(marker_type)

        time_value = self._parse_time(timecode)

        return build_marker_element(
            parent=clip,
            marker_type=marker_type,
            start=time_value.to_fcpxml(),
            duration=frame_duration_attr(self.fps),
            name=name,
            note=note,
        )

    def add_marker_at_timeline(
        self,
        timecode: str,
        name: str,
        marker_type: "MarkerType | str" = MarkerType.STANDARD,
        color: Optional[MarkerColor] = None,
        note: Optional[str] = None
    ) -> ET.Element:
        """Add a marker at a timeline position (finds the containing clip).

        Uses ``_find_spine_clip_at_seconds`` to walk the spine directly,
        avoiding the name-indexed ``self.clips`` dict which silently drops
        duplicate-named clips.
        """
        if isinstance(marker_type, str):
            marker_type = MarkerType.from_string(marker_type)
        time_value = self._parse_time(timecode)
        target_seconds = time_value.to_seconds()

        try:
            clip, relative_seconds = self._find_spine_clip_at_seconds(target_seconds)
        except ValueError:
            return self._add_marker_on_spine_element(
                timecode, name, marker_type, note,
            )
        relative_tc = TimeValue.from_seconds(relative_seconds, self.fps)

        return build_marker_element(
            parent=clip,
            marker_type=marker_type,
            start=relative_tc.to_fcpxml(),
            duration=frame_duration_attr(self.fps),
            name=name,
            note=note,
        )

    def _add_marker_on_spine_element(
        self,
        timecode: str,
        name: str,
        marker_type: MarkerType,
        note: Optional[str] = None,
    ) -> ET.Element:
        """Place a marker when no spine *clip* spans the requested position.

        Two real shapes land here, and on both the spine-clip lookup above
        raises rather than returning something wrong:

        * A music video, where the spine holds one ``<gap>`` and every visual
          is a connected clip.  There is no spine clip to host the marker, but
          the gap spans the whole timeline and legally carries markers.
        * Any sequence starting at 01:00:00:00 — element offsets begin at
          3600s while the caller passes a timeline-relative time, so nothing
          appears to span second 0.

        Both are handled by resolving the request against the timeline origin
        and hosting the marker on whichever spine element covers it.  This
        path only runs where the previous code raised, so no working project
        changes behaviour.
        """
        frame_duration = self._sequence_frame_duration()
        origin = self.timeline_origin()
        requested = parse_seconds(timecode) if str(timecode).rstrip().endswith('s') \
            else Fraction(str(self._parse_time(timecode).to_seconds()))
        absolute = origin + requested

        for element in self._get_spine().findall('*'):
            if element.tag not in SPINE_ELEMENT_TAGS:
                continue
            offset = parse_seconds(element.get('offset', '0s'))
            duration = parse_seconds(element.get('duration', '0s'))
            if not (offset <= absolute < offset + duration):
                continue
            host_start = parse_seconds(element.get('start', '0s'))
            marker_start = host_start + (absolute - offset)
            return build_marker_element(
                parent=element,
                marker_type=marker_type,
                start=format_seconds(marker_start, frame_duration),
                duration=format_seconds(frame_duration, frame_duration),
                name=name,
                note=note,
            )

        raise ValueError(
            f"No spine element at position {float(requested):.3f}s "
            f"(timeline origin {float(origin):.3f}s)"
        )

    def batch_add_markers(
        self,
        markers: List[Dict[str, Any]],
        auto_at_cuts: bool = False,
        auto_at_intervals: Optional[str] = None
    ) -> List[ET.Element]:
        """
        Add multiple markers at once.

        Args:
            markers: List of marker specs [{timecode, name, marker_type, color}]
            auto_at_cuts: Add marker at every cut point
            auto_at_intervals: Add markers at regular intervals (e.g., "00:00:30:00")

        Returns:
            List of created marker elements
        """
        created = []

        # Handle explicit markers
        for m in markers:
            marker = self.add_marker_at_timeline(
                timecode=m['timecode'],
                name=m['name'],
                marker_type=MarkerType.from_string(m.get('marker_type', 'standard')),
                color=MarkerColor[m['color'].upper()] if m.get('color') else None,
                note=m.get('note')
            )
            created.append(marker)

        # Auto-detect at cuts — add a marker at the start of every spine clip.
        if auto_at_cuts:
            for i, clip in self._iter_spine_clips():
                clip_start = clip.get('start', '0s')
                marker = build_marker_element(
                    parent=clip,
                    marker_type=MarkerType.STANDARD,
                    start=clip_start,
                    duration=frame_duration_attr(self.fps),
                    name=f"Cut {i+1}",
                )
                created.append(marker)

        # Auto-detect at intervals — place markers at regular time steps.
        if auto_at_intervals:
            interval = self._parse_time(auto_at_intervals).to_seconds()
            total_duration = self._timeline_duration().to_seconds()
            if total_duration > 0:

                current = interval
                count = 1
                while current < total_duration:
                    try:
                        clip, relative = self._find_spine_clip_at_seconds(current)
                    except ValueError:
                        current += interval
                        count += 1
                        continue
                    rel_tv = TimeValue.from_seconds(relative, self.fps)
                    marker = build_marker_element(
                        parent=clip,
                        marker_type=MarkerType.STANDARD,
                        start=rel_tv.to_fcpxml(),
                        duration=frame_duration_attr(self.fps),
                        name=f"Marker {count}",
                    )
                    created.append(marker)
                    current += interval
                    count += 1

        return created

    # ========================================================================
    # TRIM OPERATIONS
    # ========================================================================

    def trim_clip(
        self,
        clip_id: str,
        trim_start: Optional[str] = None,
        trim_end: Optional[str] = None,
        ripple: bool = True
    ) -> ET.Element:
        """
        Trim a clip's in-point and/or out-point.

        Args:
            clip_id: Target clip
            trim_start: New in-point or delta ('+1s', '-10f')
            trim_end: New out-point or delta
            ripple: Whether to shift subsequent clips

        Returns:
            Modified clip element
        """
        clip = self._require_clip(clip_id)

        current_start, current_duration, _ = self._get_clip_times(clip)

        original_duration = current_duration

        # Handle trim_start
        if trim_start:
            if trim_start.startswith('+') or trim_start.startswith('-'):
                delta = self._parse_time(trim_start[1:])
                if trim_start.startswith('-'):
                    # Extend earlier
                    new_start = current_start - delta
                    new_duration = current_duration + delta
                else:
                    # Trim later
                    new_start = current_start + delta
                    new_duration = current_duration - delta
            else:
                new_start = self._parse_time(trim_start)
                diff = new_start - current_start
                new_duration = current_duration - diff

            clip.set('start', new_start.to_fcpxml())
            current_start = new_start
            current_duration = new_duration

        # Handle trim_end
        if trim_end:
            if trim_end.startswith('+') or trim_end.startswith('-'):
                delta = self._parse_time(trim_end[1:])
                if trim_end.startswith('-'):
                    new_duration = current_duration - delta
                else:
                    new_duration = current_duration + delta
            else:
                end_point = self._parse_time(trim_end)
                new_duration = end_point - current_start

            current_duration = new_duration

        if current_duration <= TimeValue.zero():
            raise ValueError(
                f"Trim would produce non-positive duration "
                f"({current_duration.to_seconds():.3f}s) for clip '{clip_id}'"
            )

        clip.set('duration', current_duration.to_fcpxml())

        # Ripple subsequent clips if needed
        if ripple:
            duration_change = current_duration - original_duration
            if duration_change != TimeValue.zero():
                self._ripple_after_clip(clip, duration_change)

        return clip

    def _ripple_from_index(
        self, spine: ET.Element, start_index: int, delta: 'TimeValue'
    ) -> None:
        """Shift the offset of every spine element from *start_index* onward by *delta*.

        Consolidates the ripple loops previously duplicated across
        ``_ripple_after_clip``, ``delete_clip``, and ``insert_clip``.

        Args:
            spine: The primary storyline ``<spine>`` element.
            start_index: First child index to adjust (inclusive).
            delta: Signed time shift (positive = later, negative = earlier).
        """
        children = list(spine)
        for child in children[start_index:]:
            if child.tag in SPINE_ELEMENT_TAGS:
                current_offset = self._parse_time(child.get('offset', '0s'))
                new_offset = current_offset + delta
                child.set('offset', new_offset.to_fcpxml())

    def _ripple_after_clip(self, target_clip: ET.Element, delta: TimeValue) -> None:
        """Shift all clips after the given clip by delta."""
        spine = self._get_spine()
        clip_index = self._find_clip_index(spine, target_clip)
        if clip_index is not None:
            self._ripple_from_index(spine, clip_index + 1, delta)

    # ========================================================================
    # REORDER OPERATIONS
    # ========================================================================

    def reorder_clips(
        self,
        clip_ids: List[str],
        target_position: str,
        ripple: bool = True
    ) -> None:
        """
        Move clips to a new position in the timeline.

        Args:
            clip_ids: Clips to move (maintains relative order)
            target_position: 'start', 'end', timecode, or 'after:clip_id'/'before:clip_id'
            ripple: Whether to shift other clips
        """
        spine = self._get_spine()

        # Collect clips to move
        clips_to_move = []
        for clip_id in clip_ids:
            clip = self.clips.get(clip_id)
            if clip is not None and clip in list(spine):
                clips_to_move.append(clip)

        if not clips_to_move:
            raise ValueError(f"No clips found matching: {clip_ids}")

        # Calculate total duration of moving clips
        total_duration = TimeValue.zero()
        for clip in clips_to_move:
            dur = self._parse_time(clip.get('duration', '0s'))
            total_duration = total_duration + dur

        # Remove clips from current positions
        for clip in clips_to_move:
            spine.remove(clip)

        # Determine target offset and insert index
        spine_children = list(spine)
        target_offset, insert_index = self._resolve_insert_position(
            target_position, spine_children
        )

        # Insert clips at new position
        current_offset = target_offset
        for clip in clips_to_move:
            clip.set('offset', current_offset.to_fcpxml())
            spine.insert(insert_index, clip)
            insert_index += 1
            dur = self._parse_time(clip.get('duration', '0s'))
            current_offset = current_offset + dur

        # Recalculate all offsets if ripple
        if ripple:
            self._recalculate_offsets(spine)

    def _recalculate_offsets(self, spine: ET.Element) -> None:
        """Recalculate all clip offsets sequentially."""
        current_offset = TimeValue.zero()

        for child in spine:
            if child.tag in SPINE_ELEMENT_TAGS:
                child.set('offset', current_offset.to_fcpxml())
                duration_str = child.get('duration', '0s')
                duration = self._parse_time(duration_str)
                current_offset = current_offset + duration

    def _timeline_duration(self) -> 'TimeValue':
        """Return the total timeline duration as a TimeValue.

        Reads from the ``<sequence>`` element when available, falling back
        to summing all spine element durations.  Extracted from
        ``add_music_bed`` and ``batch_add_markers`` which both computed
        this independently.

        Parsed exactly rather than through ``_parse_time``, which quantises
        through ``int(fps)``.  A real 23.98 project carrying
        ``<sequence duration="164s">`` came back as 170.96s — 7 seconds of
        timeline that does not exist — so ``import_beat_markers`` let beats
        past the end through and then failed to place them.
        """
        sequence = self.root.find('.//sequence')
        if sequence is not None:
            dur_str = sequence.get('duration')
            if dur_str:
                return _exact(parse_seconds(dur_str))
        spine = self._get_spine()
        total = Fraction(0)
        for child in spine:
            if child.tag in SPINE_ELEMENT_TAGS:
                total += parse_seconds(child.get('duration', '0s'))
        return _exact(total)

    # ========================================================================
    # TRANSITION OPERATIONS
    # ========================================================================

    def add_transition(
        self,
        clip_id: str,
        position: str = 'end',
        transition_type: str = 'cross-dissolve',
        duration: str = '00:00:00:15'
    ) -> ET.Element:
        """
        Add a transition to a clip.

        Args:
            clip_id: Target clip
            position: 'start', 'end', or 'both'
            transition_type: Type of transition
            duration: Transition duration

        Returns:
            Created transition element(s)
        """
        spine, clip, clip_index = self._require_spine_clip(clip_id)

        trans_duration = self._parse_time(duration)

        # Effect name and FCP built-in effect UID lookup via registry
        effect_name, effect_uid = FCP_EFFECTS.get(
            transition_type,
            FCP_EFFECTS['cross-dissolve']
        )

        # Ensure effect resource exists in <resources>
        effect_ref_id = None
        if effect_uid:
            root = self.tree.getroot()
            resources = root.find('.//resources')
            if resources is not None:
                for eff in resources.findall('effect'):
                    if eff.get('uid') == effect_uid:
                        effect_ref_id = eff.get('id')
                        break
                if effect_ref_id is None:
                    effect_ref_id = self._unique_resource_id(resources, 'r_dissolve')
                    eff_el = ET.SubElement(resources, 'effect')
                    eff_el.set('id', effect_ref_id)
                    eff_el.set('name', effect_name)
                    eff_el.set('uid', effect_uid)

        transitions_added = []

        _, clip_dur, clip_offset = self._get_clip_times(clip)
        half_dur = trans_duration * 0.5

        if position in ('end', 'both'):
            end_offset = clip_offset + clip_dur - half_dur
            transition = self._make_transition_element(
                effect_name, end_offset, trans_duration, effect_ref_id
            )
            spine.insert(clip_index + 1, transition)
            transitions_added.append(transition)

        if position in ('start', 'both'):
            start_offset = clip_offset - half_dur
            if start_offset < TimeValue.zero():
                raise ValueError(
                    f"Transition at start would produce negative offset "
                    f"({start_offset.to_seconds():.3f}s) for clip '{clip_id}'"
                )
            transition = self._make_transition_element(
                effect_name, start_offset, trans_duration, effect_ref_id
            )
            spine.insert(clip_index, transition)
            transitions_added.append(transition)

        return transitions_added[0] if len(transitions_added) == 1 else transitions_added

    # ========================================================================
    # SPEED OPERATIONS
    # ========================================================================

    def change_speed(
        self,
        clip_id: str,
        speed: float,
        preserve_pitch: bool = True
    ) -> ET.Element:
        """
        Change clip playback speed.

        Args:
            clip_id: Target clip
            speed: Speed multiplier (0.5 = half speed, 2.0 = double)
            preserve_pitch: Maintain audio pitch

        Returns:
            Modified clip element
        """
        if speed <= 0:
            raise ValueError(f"Speed must be positive, got {speed}")

        clip = self._require_clip(clip_id)

        current_duration = self._parse_time(clip.get('duration', '0s'))

        # Use rational arithmetic to avoid floating-point time values.
        # FCPXML requires rational fractions with a consistent timebase,
        # not decimal floats like "2.6666666666666665s".
        from fractions import Fraction
        rate = rational_fps(self.fps or 24)
        denom = (
            current_duration.denominator
            if current_duration.denominator > 0
            else (1 / rate).denominator
        )
        source_num = current_duration.numerator
        speed_frac = Fraction(speed).limit_denominator(1000)
        raw_num = source_num * speed_frac.denominator
        raw_denom = denom * speed_frac.numerator

        # Snap to a frame boundary in a timebase where a frame is a whole
        # number of ticks. Integer rates keep 2400 ticks/sec (24fps → 100
        # ticks/frame); 23.98 cannot use it — a frame there is 100.1 ticks,
        # and the old `2400 // int(23.976)` gave 104, which is not a frame of
        # any rate. Those snap in the format's own timebase instead.
        ticks_per_second, ticks_per_frame = tick_timebase(rate)
        dur_ticks = round(Fraction(raw_num, raw_denom) * ticks_per_second)
        dur_ticks = round(dur_ticks / ticks_per_frame) * ticks_per_frame
        new_num = dur_ticks
        new_denom = ticks_per_second

        # Remove any existing timeMap/conform-rate from a prior speed change
        # to prevent duplicate children that produce invalid FCPXML.
        for stale_tag in ('timeMap', 'conform-rate'):
            for stale in clip.findall(stale_tag):
                clip.remove(stale)

        # Create timeMap for speed change (DTD-ordered insertion)
        timemap = ET.Element('timeMap')
        _dtd_insert(clip, timemap)

        # Start keyframe
        tp1 = ET.SubElement(timemap, 'timept')
        tp1.set('time', '0s')
        tp1.set('value', '0s')
        tp1.set('interp', 'linear')

        # End keyframe — use rational time, not floats
        tp2 = ET.SubElement(timemap, 'timept')
        tp2.set('time', f"{new_num}/{new_denom}s")
        tp2.set('value', f"{source_num}/{denom}s")
        tp2.set('interp', 'linear')

        # Update clip duration (rational, not simplified to arbitrary denominator)
        clip.set('duration', f"{new_num}/{new_denom}s")

        # Add conform-rate (DTD-ordered insertion)
        conform = ET.Element('conform-rate')
        conform.set('scaleEnabled', '1')
        conform.set('srcFrameRate', fcp_frame_rate_name(self.fps))
        _dtd_insert(clip, conform)

        return clip

    # ========================================================================
    # SPLIT & DELETE OPERATIONS
    # ========================================================================

    @staticmethod
    def _filter_children_for_segment(
        clip: ET.Element,
        seg_start: 'TimeValue',
        seg_duration: 'TimeValue',
    ) -> None:
        """Remove markers/keywords from *clip* that fall outside the segment range.

        After ``split_clip`` deepcopy's the original clip into each segment, every
        segment inherits all child elements.  Markers whose ``start`` falls outside
        ``[seg_start, seg_start + seg_duration)`` are phantom duplicates and must be
        removed.  Keywords that partially overlap get their ``start``/``duration``
        clamped to the segment boundaries.
        """
        seg_end = seg_start + seg_duration
        to_remove = []
        for child in clip:
            tag = child.tag
            if tag in ('marker', 'chapter-marker'):
                child_start = TimeValue.from_timecode(child.get('start', '0s'))
                if child_start < seg_start or child_start >= seg_end:
                    to_remove.append(child)
            elif tag == 'keyword':
                kw_start = TimeValue.from_timecode(child.get('start', '0s'))
                kw_dur = TimeValue.from_timecode(child.get('duration', '0s'))
                kw_end = kw_start + kw_dur
                # Completely outside segment → remove
                if kw_end <= seg_start or kw_start >= seg_end:
                    to_remove.append(child)
                else:
                    # Clamp keyword range to segment boundaries
                    clamped_start = max(kw_start, seg_start)
                    clamped_end = min(kw_end, seg_end)
                    child.set('start', clamped_start.to_fcpxml())
                    child.set('duration', (clamped_end - clamped_start).to_fcpxml())
        for child in to_remove:
            clip.remove(child)

    def split_clip(
        self,
        clip_id: str,
        split_points: List[str]
    ) -> List[ET.Element]:
        """
        Split a clip at specified timecodes.

        Args:
            clip_id: Clip to split
            split_points: Timecodes within the clip to split at

        Returns:
            List of resulting clip elements
        """
        spine, clip, clip_index = self._require_spine_clip(clip_id)

        # Get clip properties
        clip_start, clip_duration, clip_offset = self._get_clip_times(clip)
        clip_name = clip.get('name', 'Clip')

        # Sort split points
        split_times = sorted([self._parse_time(sp) for sp in split_points])

        # Remove original clip
        spine.remove(clip)

        # Create new clips
        new_clips = []
        current_offset = clip_offset
        current_start = clip_start

        all_points = split_times + [clip_duration]

        for i, split_time in enumerate(all_points):
            if i == 0:
                segment_duration = split_time
            else:
                segment_duration = split_time - split_times[i - 1]

            if segment_duration <= TimeValue.zero():
                continue

            # Create new clip
            new_clip = copy.deepcopy(clip)
            new_clip.set('name', clip_name)
            new_clip.set('offset', current_offset.to_fcpxml())
            new_clip.set('start', current_start.to_fcpxml())
            new_clip.set('duration', segment_duration.to_fcpxml())

            # Remove markers/keywords that belong to other segments
            self._filter_children_for_segment(
                new_clip, current_start, segment_duration
            )

            spine.insert(clip_index + len(new_clips), new_clip)
            new_clips.append(new_clip)

            # Update for next iteration
            current_offset = current_offset + segment_duration
            current_start = current_start + segment_duration

        # Update clip index: remove stale original entry, add split entries
        self.clips.pop(clip_id, None)
        for i, new_clip in enumerate(new_clips):
            new_id = f"{clip_id}_split_{i}"
            self.clips[new_id] = new_clip

        return new_clips

    def cut_clip_ranges(
        self,
        clip: ET.Element,
        cut_ranges: List[Tuple['TimeValue', 'TimeValue']],
    ) -> 'TimeValue':
        """Remove clip-relative time ranges from a spine clip, rippling after.

        Element-based on purpose: callers that walk the spine (e.g. media
        silence removal) pass the exact element, so duplicate-named clips are
        never ambiguous the way name-keyed operations are.

        Args:
            clip: The spine clip element to cut (must be a direct spine child).
            cut_ranges: (start, end) TimeValue pairs measured from the clip's
                own head. Overlapping/unsorted ranges are merged; portions
                outside [0, clip duration] are clamped. A cut covering the
                whole clip removes it entirely.

        Returns:
            Total removed duration (zero if no effective ranges).
        """
        spine = self._get_spine()
        clip_start, clip_duration, clip_offset = self._get_clip_times(clip)
        clip_index = list(spine).index(clip)
        zero = TimeValue.zero()

        # Clamp, sort, merge.
        clamped = []
        for start, end in cut_ranges:
            start = start if start > zero else zero
            end = end if end < clip_duration else clip_duration
            if end > start:
                clamped.append((start, end))
        clamped.sort(key=lambda r: r[0])
        merged: List[Tuple[TimeValue, TimeValue]] = []
        for start, end in clamped:
            if merged and start <= merged[-1][1]:
                if end > merged[-1][1]:
                    merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        if not merged:
            return zero

        # Keep ranges = complement of the merged cuts.
        keeps: List[Tuple[TimeValue, TimeValue]] = []
        cursor = zero
        for start, end in merged:
            if start > cursor:
                keeps.append((cursor, start))
            cursor = end
        if cursor < clip_duration:
            keeps.append((cursor, clip_duration))

        spine.remove(clip)
        new_clips: List[ET.Element] = []
        current_offset = clip_offset
        kept_total = zero
        for keep_start, keep_end in keeps:
            seg_duration = keep_end - keep_start
            seg_start = clip_start + keep_start
            new_clip = copy.deepcopy(clip)
            new_clip.set('offset', current_offset.to_fcpxml())
            new_clip.set('start', seg_start.to_fcpxml())
            new_clip.set('duration', seg_duration.to_fcpxml())
            self._filter_children_for_segment(new_clip, seg_start, seg_duration)
            spine.insert(clip_index + len(new_clips), new_clip)
            new_clips.append(new_clip)
            current_offset = current_offset + seg_duration
            kept_total = kept_total + seg_duration

        removed = clip_duration - kept_total
        self._ripple_from_index(spine, clip_index + len(new_clips), zero - removed)

        # Keep the name index coherent, mirroring delete_clip/split_clip.
        name = clip.get('id') or clip.get('name') or ''
        if name and self.clips.get(name) is clip:
            if new_clips:
                self.clips[name] = new_clips[0]
            else:
                remaining = [
                    sc for _, sc in self._iter_spine_clips()
                    if (sc.get('id') or sc.get('name') or '') == name
                ]
                if remaining:
                    self.clips[name] = remaining[0]
                else:
                    self.clips.pop(name, None)
        return removed

    def delete_clip(
        self,
        clip_ids: List[str],
        ripple: bool = True
    ) -> List[str]:
        """
        Delete clips from timeline.

        Uses spine iteration instead of the name-indexed dict so that
        duplicate-named clips (e.g. four ``Interview_A``) are resolved
        correctly — always targeting the *first* spine match rather than
        the last-indexed entry.

        Args:
            clip_ids: Clips to delete
            ripple: If True, shift subsequent clips. If False, leave gaps.

        Returns:
            The ids that actually matched a clip and were deleted. An id that
            matched nothing is skipped, and callers have to be able to tell
            the difference: reporting the length of the REQUEST instead made
            the most destructive tool on the surface answer "Deleted 2
            clip(s)" to a request that deleted none.
        """
        spine = self._get_spine()
        deleted: List[str] = []

        for clip_id in clip_ids:
            # Walk spine directly to find the first clip matching this name,
            # avoiding the last-one-wins problem in self.clips.
            target = None
            for _spine_idx, spine_clip in self._iter_spine_clips():
                name = spine_clip.get('id') or spine_clip.get('name') or ''
                if name == clip_id:
                    target = spine_clip
                    break

            if target is None:
                continue

            deleted.append(clip_id)
            _, clip_duration, clip_offset = self._get_clip_times(target)
            clip_index = list(spine).index(target)

            if ripple:
                spine.remove(target)
                self._ripple_from_index(
                    spine, clip_index, TimeValue.zero() - clip_duration
                )
            else:
                # Replace with gap
                gap = ET.Element('gap')
                gap.set('name', 'Gap')
                gap.set('offset', clip_offset.to_fcpxml())
                gap.set('duration', clip_duration.to_fcpxml())

                spine.remove(target)
                spine.insert(clip_index, gap)

            # Re-index: if other spine clips share this name, point the
            # dict entry at the next one; otherwise remove entirely.
            remaining = [
                sc for _, sc in self._iter_spine_clips()
                if (sc.get('id') or sc.get('name') or '') == clip_id
            ]
            if remaining:
                self.clips[clip_id] = remaining[0]
            else:
                self.clips.pop(clip_id, None)

        return deleted

    # ========================================================================
    # SPEED CUTTING OPERATIONS (v0.3.0)
    # ========================================================================

    def fix_flash_frames(
        self,
        mode: str = 'auto',
        threshold_frames: int = 6,
        critical_threshold_frames: int = 2
    ) -> List[Dict[str, Any]]:
        """
        Automatically fix flash frames (ultra-short clips).

        Args:
            mode: How to fix flash frames:
                - 'extend_previous': Extend the previous clip to cover the flash frame
                - 'extend_next': Extend the next clip backward to cover the flash frame
                - 'delete': Remove the flash frame entirely (ripple)
                - 'auto': Use smart logic (extend prev for critical, delete for warning)
            threshold_frames: Frames below this are considered flash frames
            critical_threshold_frames: Frames below this are critical (default: 2)

        Returns:
            List of fixed flash frames with details
        """
        spine = self._get_spine()
        fixed = []

        # Collect flash frames first (can't modify while iterating)
        flash_frames = []
        for i, clip in self._iter_spine_clips():
            duration = self._parse_time(clip.get('duration', '0s'))
            duration_frames = duration.to_frames(self.fps)

            if duration_frames < threshold_frames:
                is_critical = duration_frames < critical_threshold_frames
                flash_frames.append({
                    'index': i,
                    'clip': clip,
                    'clip_id': clip.get('name') or clip.get('id') or f"clip_{i}",
                    'duration_frames': duration_frames,
                    'is_critical': is_critical
                })

        # Process in reverse order to maintain indices
        for ff in reversed(flash_frames):
            clip = ff['clip']
            _, _, clip_offset = self._get_clip_times(clip)

            # Determine actual mode
            actual_mode = mode
            if mode == 'auto':
                # Critical: try to extend previous, otherwise delete
                # Warning: delete
                actual_mode = 'extend_previous' if ff['is_critical'] else 'delete'

            result = {
                'clip_name': ff['clip_id'],
                'duration_frames': ff['duration_frames'],
                'was_critical': ff['is_critical'],
                'action': actual_mode,
                'timecode': clip_offset.to_timecode(self.fps)
            }

            direction = {'extend_previous': 'prev', 'extend_next': 'next'}.get(actual_mode)
            if direction:
                neighbor = self._absorb_into_neighbor(spine, clip, direction)
                if neighbor is not None:
                    self._recalculate_offsets(spine)
                    result['extended_clip'] = neighbor.get('name', direction.title())
                else:
                    spine.remove(clip)
                    self._recalculate_offsets(spine)
            else:  # delete
                spine.remove(clip)
                self._recalculate_offsets(spine)

            fixed.append(result)

        # Rebuild clip index
        self._build_clip_index()

        return fixed

    def rapid_trim(
        self,
        max_duration: Optional[str] = None,
        min_duration: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        trim_from: str = 'end'
    ) -> List[Dict[str, Any]]:
        """
        Batch trim clips to enforce duration limits.

        Args:
            max_duration: Maximum clip duration (e.g., '2s', '00:00:02:00')
            min_duration: Minimum clip duration (clips shorter are extended/left alone)
            keywords: Only trim clips with these keywords (None = all clips)
            trim_from: Where to trim - 'start', 'end', or 'center'

        Returns:
            List of trimmed clips with before/after durations
        """
        trimmed = []

        max_dur = self._parse_time(max_duration) if max_duration else None
        min_dur = self._parse_time(min_duration) if min_duration else None

        for _i, clip in self._iter_spine_clips():

            clip_name = clip.get('name') or clip.get('id') or 'Unknown'

            # Check keyword filter
            if keywords:
                clip_keywords = set()
                for kw_elem in clip.findall('keyword'):
                    clip_keywords.add(kw_elem.get('value', ''))
                if not clip_keywords.intersection(set(keywords)):
                    continue

            current_start, current_duration, _ = self._get_clip_times(clip)
            original_duration = current_duration.to_seconds()

            # Skip clips shorter than min_duration (leave them alone)
            if min_dur and current_duration < min_dur:
                continue

            # Check max duration
            if max_dur and current_duration > max_dur:
                excess = current_duration - max_dur

                if trim_from == 'end':
                    # Keep start, reduce duration
                    clip.set('duration', max_dur.to_fcpxml())

                elif trim_from == 'start':
                    # Increase start, reduce duration
                    new_start = current_start + excess
                    clip.set('start', new_start.to_fcpxml())
                    clip.set('duration', max_dur.to_fcpxml())

                elif trim_from == 'center':
                    # Trim equal amounts from both ends
                    half_excess = excess * 0.5
                    new_start = current_start + half_excess
                    clip.set('start', new_start.to_fcpxml())
                    clip.set('duration', max_dur.to_fcpxml())

                trimmed.append({
                    'clip_name': clip_name,
                    'original_duration': original_duration,
                    'new_duration': max_dur.to_seconds(),
                    'trim_from': trim_from,
                    'action': 'trimmed'
                })

        # Recalculate offsets
        self._recalculate_offsets(self._get_spine())

        return trimmed

    def fill_gaps(
        self,
        mode: str = 'extend_previous',
        max_gap: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fill gaps in the timeline.

        Args:
            mode: How to fill gaps:
                - 'extend_previous': Extend previous clip to fill gap
                - 'extend_next': Extend next clip backward to fill gap
                - 'delete': Remove gap elements and ripple
            max_gap: Only fill gaps smaller than this (None = all gaps)

        Returns:
            List of filled gaps with details
        """
        spine = self._get_spine()
        filled = []
        max_gap_time = self._parse_time(max_gap) if max_gap else None

        # Find all gaps
        gaps_to_process = []
        for i, child in enumerate(list(spine)):
            if child.tag == 'gap':
                gap_duration = self._parse_time(child.get('duration', '0s'))
                gap_offset = self._parse_time(child.get('offset', '0s'))

                # Check max_gap filter
                if max_gap_time and gap_duration > max_gap_time:
                    continue

                gaps_to_process.append({
                    'element': child,
                    'index': i,
                    'duration': gap_duration,
                    'offset': gap_offset
                })

        # Process in reverse to maintain indices
        for gap_info in reversed(gaps_to_process):
            gap = gap_info['element']
            gap_duration = gap_info['duration']
            gap_offset = gap_info['offset']

            result = {
                'timecode': gap_offset.to_timecode(self.fps),
                'duration_frames': gap_duration.to_frames(self.fps),
                'duration_seconds': gap_duration.to_seconds(),
                'action': mode
            }

            direction = {'extend_previous': 'prev', 'extend_next': 'next'}.get(mode)
            if direction:
                neighbor = self._absorb_into_neighbor(spine, gap, direction)
                if neighbor is not None:
                    result['extended_clip'] = neighbor.get('name', direction.title())
                    filled.append(result)
            else:  # delete
                spine.remove(gap)
                filled.append(result)

        # Recalculate offsets
        self._recalculate_offsets(spine)

        return filled

    # ========================================================================
    # SELECTION OPERATIONS
    # ========================================================================

    def select_by_keyword(
        self,
        keywords: List[str],
        match_mode: str = 'any',
        favorites_only: bool = False,
        exclude_rejected: bool = True
    ) -> List[str]:
        """
        Find clips matching keywords.

        Args:
            keywords: Keywords to match
            match_mode: 'any' (OR), 'all' (AND), 'none' (exclude)
            favorites_only: Only return favorited clips
            exclude_rejected: Exclude rejected clips

        Returns:
            List of matching clip IDs
        """
        matches = []

        for clip_id, clip in self.clips.items():
            clip_keywords = set()
            for kw_elem in clip.findall('keyword'):
                clip_keywords.add(kw_elem.get('value', ''))

            # Check keyword match
            keyword_set = set(keywords)
            if match_mode == 'any':
                match = bool(clip_keywords & keyword_set)
            elif match_mode == 'all':
                match = keyword_set <= clip_keywords
            elif match_mode == 'none':
                match = not bool(clip_keywords & keyword_set)
            else:
                match = True

            if match:
                matches.append(clip_id)

        return matches

    # ========================================================================
    # INSERT CLIP OPERATIONS
    # ========================================================================

    def insert_clip(
        self,
        position: str,
        asset_id: Optional[str] = None,
        asset_name: Optional[str] = None,
        duration: Optional[str] = None,
        in_point: Optional[str] = None,
        out_point: Optional[str] = None,
        ripple: bool = True
    ) -> ET.Element:
        """
        Insert a library clip onto the timeline.

        Args:
            position: Where to insert - 'start', 'end', timecode, or 'after:clip_id'
            asset_id: Asset reference ID (e.g., 'r3')
            asset_name: Asset name (alternative to asset_id)
            duration: Duration of clip (if not using in/out points)
            in_point: Source in-point for subclip
            out_point: Source out-point for subclip
            ripple: Whether to shift subsequent clips

        Returns:
            The created clip element
        """
        asset, asset_id = self._resolve_asset(asset_id, asset_name)
        clip_duration, source_start = self._resolve_clip_duration(
            asset, duration, in_point, out_point
        )

        # Get spine and calculate insert position
        spine = self._get_spine()
        spine_children = list(spine)
        target_offset, insert_index = self._resolve_insert_position(
            position, spine_children
        )

        # Build extra attrs — include format from first available format
        extra: dict[str, str] = {}
        for fmt_id in self.formats:
            extra['format'] = fmt_id
            break

        new_clip = self._make_asset_clip(
            asset_id, asset.get('name', 'Untitled'),
            target_offset, source_start, clip_duration,
            **extra,
        )

        # Insert into spine
        spine.insert(insert_index, new_clip)

        # Ripple subsequent clips if needed
        if ripple and insert_index < len(spine_children):
            self._ripple_from_index(spine, insert_index + 1, clip_duration)

        # Add to clip index
        clip_id = f"inserted_{len(self.clips)}"
        self.clips[clip_id] = new_clip

        return new_clip

    # ========================================================================
    # CONNECTED CLIP OPERATIONS (v0.5.0)
    # ========================================================================

    def add_connected_clip(
        self,
        parent_clip_id: str,
        asset_id: Optional[str] = None,
        asset_name: Optional[str] = None,
        offset: str = "0s",
        duration: Optional[str] = None,
        lane: int = 1,
    ) -> ET.Element:
        """Add a connected clip (B-roll, title, audio) to an existing timeline clip.

        Args:
            parent_clip_id: Name/ID of the clip to attach to
            asset_id: Asset reference ID
            asset_name: Asset name (alternative to asset_id)
            offset: Position relative to parent clip start
            duration: Duration of connected clip (default: full asset)
            lane: Lane number (positive=above, negative=below)

        Returns:
            The created connected clip element
        """
        parent = self._require_clip(parent_clip_id)
        asset, asset_id = self._resolve_asset(asset_id, asset_name)
        clip_duration, source_start = self._resolve_clip_duration(asset, duration)

        new_clip = self._make_asset_clip(
            asset_id, asset.get('name', 'Untitled'),
            self._parse_time(offset), source_start, clip_duration,
            parent=parent, lane=str(lane),
        )
        return new_clip

    # ========================================================================
    # AUDIO CLIP OPERATIONS (v0.6.0)
    # ========================================================================

    def add_audio_clip(
        self,
        parent_clip_id: str,
        asset_id: Optional[str] = None,
        offset: str = "0s",
        duration: Optional[str] = None,
        role: str = "dialogue",
        lane: int = -1,
        src: Optional[str] = None,
    ) -> ET.Element:
        """Add an audio clip connected to an existing timeline clip.

        Creates an <asset-clip> at a negative lane with audioRole attribute.
        Supports hierarchical roles like "dialogue.boom", "music.score",
        "effects.foley".

        Args:
            parent_clip_id: Name/ID of the clip to attach audio to.
            asset_id: Existing asset reference ID. If None and src provided,
                creates a new asset.
            offset: Position relative to parent clip start.
            duration: Duration of audio clip.
            role: Audio role (e.g. "dialogue", "music.score", "effects.foley").
            lane: Lane number (negative = below primary, default -1).
            src: Path to audio file. Used to create a new asset if asset_id
                is not provided.

        Returns:
            The created audio clip element.
        """
        parent = self._require_clip(parent_clip_id)

        # Resolve or create asset
        if asset_id and asset_id in self.resources:
            asset = self.resources[asset_id]
        elif src:
            # Create new asset in resources
            resources = self.root.find('.//resources')
            if resources is None:
                raise ValueError("No <resources> element found in FCPXML")
            asset_id = self._unique_resource_id(resources, 'r_audio1')
            # The asset duration must reflect the real media length, not the
            # requested clip duration — FCP flags assets that claim more
            # media than the file contains.
            probed = _probe_audio_info(src)
            if probed:
                rate = probed['sample_rate']
                asset_duration = f"{round(probed['duration'] * rate)}/{rate}s"
            else:
                asset_duration = duration or "0s"
            asset_elem = _create_asset_element(
                resources, asset_id, Path(src).stem, src,
                duration=asset_duration,
                has_video="0", has_audio="1",
            )
            if probed:
                asset_elem.set('audioSources', '1')
                asset_elem.set('audioChannels', str(probed['channels']))
                asset_elem.set('audioRate', str(probed['sample_rate']))
            asset = {
                'id': asset_id,
                'name': Path(src).stem,
                'duration': asset_duration,
                'element': asset_elem,
            }
            self.resources[asset_id] = asset
        else:
            raise ValueError("Must provide either asset_id or src for audio clip")

        clip_duration, source_start = self._resolve_clip_duration(asset, duration)

        # Clamp so the clip never claims more media than the asset contains
        asset_duration_tv = self._parse_time(asset.get('duration', '0s'))
        if asset_duration_tv > TimeValue.zero():
            available = asset_duration_tv - source_start
            if available < TimeValue.zero():
                raise ValueError(
                    f"Source start {source_start.to_fcpxml()} is beyond the end "
                    f"of audio asset '{asset.get('name')}' "
                    f"({asset_duration_tv.to_fcpxml()})"
                )
            if clip_duration > available:
                clip_duration = available

        new_clip = self._make_asset_clip(
            asset_id, asset.get('name', 'Audio'),
            self._parse_time(offset), source_start, clip_duration,
            lane=str(lane),
            audioRole=_sanitize_xml_value(role, 256),
        )
        _dtd_insert(parent, new_clip)
        return new_clip

    def add_music_bed(
        self,
        asset_id: Optional[str] = None,
        duration: Optional[str] = None,
        role: str = "music",
        src: Optional[str] = None,
    ) -> ET.Element:
        """Add a music bed spanning the full timeline at lane -1.

        Convenience method: attaches to the first spine clip and spans
        the full timeline duration.

        Args:
            asset_id: Existing asset reference ID.
            duration: Override duration (default: full timeline).
            role: Audio role (default "music").
            src: Path to audio file (creates asset if asset_id not given).

        Returns:
            The created music bed clip element.
        """
        spine = self._get_spine()
        first_clip = None
        first_clip_id = None
        for clip_id, clip in self.clips.items():
            if clip in list(spine):
                first_clip = clip
                first_clip_id = clip_id
                break

        if first_clip is None:
            raise ValueError("No clips in spine to attach music bed to")

        # Calculate full timeline duration if not specified
        if not duration:
            duration = self._timeline_duration().to_fcpxml()

        return self.add_audio_clip(
            parent_clip_id=first_clip_id,
            asset_id=asset_id,
            offset="0s",
            duration=duration,
            role=role,
            lane=-1,
            src=src,
        )

    # ========================================================================
    # COMPOUND CLIP OPERATIONS (v0.6.0)
    # ========================================================================

    def create_compound_clip(
        self,
        clip_ids: List[str],
        name: str = "Compound Clip",
    ) -> ET.Element:
        """Group spine clips into a compound clip.

        Creates a <media> resource with a nested <sequence><spine> containing
        the specified clips, then replaces the originals in the main spine
        with a single <ref-clip>.

        Args:
            clip_ids: IDs of clips in the spine to group.
            name: Name for the compound clip.

        Returns:
            The created <ref-clip> element.
        """
        spine = self._get_spine()
        resources = self.root.find('.//resources')
        if resources is None:
            raise ValueError("No <resources> element found in FCPXML")

        # Collect clips and validate they're in spine
        spine_children = list(spine)
        clips_to_group = []
        for cid in clip_ids:
            clip = self._require_clip(cid)
            if clip not in spine_children:
                raise ValueError(f"Clip not in spine: {cid}")
            clips_to_group.append((cid, clip))

        if not clips_to_group:
            raise ValueError("No valid clips to group")

        # Sort by offset so the compound maintains order
        clips_to_group.sort(
            key=lambda c: self._parse_time(c[1].get('offset', '0s'))
        )

        # Calculate compound duration and starting offset
        first_offset = self._parse_time(clips_to_group[0][1].get('offset', '0s'))
        total_duration = TimeValue.zero()
        for _, clip in clips_to_group:
            total_duration = total_duration + self._parse_time(clip.get('duration', '0s'))

        # Get format ref
        format_id = None
        for fmt_id in self.formats:
            format_id = fmt_id
            break

        # Create media resource with nested sequence
        media_id = self._unique_resource_id(resources, 'r_compound1')

        media = ET.SubElement(resources, 'media')
        media.set('id', media_id)
        media.set('name', _sanitize_xml_value(name, 512))
        media.set('uid', str(uuid.uuid4()).upper())

        seq = ET.SubElement(media, 'sequence')
        seq.set('format', format_id or 'r1')
        seq.set('duration', total_duration.to_fcpxml())
        seq.set('tcStart', '0s')
        seq.set('tcFormat', 'NDF')

        inner_spine = ET.SubElement(seq, 'spine')

        # Move clips into the compound's inner spine
        inner_offset = TimeValue.zero()
        for _, clip in clips_to_group:
            new_clip = copy.deepcopy(clip)
            new_clip.set('offset', inner_offset.to_fcpxml())
            inner_spine.append(new_clip)
            inner_offset = inner_offset + self._parse_time(clip.get('duration', '0s'))

        # Get the insert position (where first clip was)
        spine_children = list(spine)
        insert_idx = spine_children.index(clips_to_group[0][1])

        # Remove originals from spine
        for cid, clip in clips_to_group:
            spine.remove(clip)
            if cid in self.clips:
                del self.clips[cid]

        # Create ref-clip in main spine
        ref_clip = ET.Element('ref-clip')
        ref_clip.set('ref', media_id)
        ref_clip.set('offset', first_offset.to_fcpxml())
        ref_clip.set('name', _sanitize_xml_value(name, 512))
        ref_clip.set('duration', total_duration.to_fcpxml())
        spine.insert(insert_idx, ref_clip)

        # Index the new ref-clip
        compound_id = f"compound_{name}"
        self.clips[compound_id] = ref_clip

        return ref_clip

    def flatten_compound_clip(
        self,
        ref_clip_id: str,
    ) -> List[ET.Element]:
        """Flatten a compound clip back into individual spine clips.

        Extracts clips from the compound's inner sequence and places them
        back in the main spine at the ref-clip's position.

        Args:
            ref_clip_id: ID of the ref-clip to flatten.

        Returns:
            List of extracted clip elements now in the main spine.
        """
        spine = self._get_spine()
        ref_clip = self._require_clip(ref_clip_id)
        if ref_clip.tag != 'ref-clip':
            raise ValueError(f"Element is not a ref-clip: {ref_clip_id}")

        media_ref = ref_clip.get('ref', '')
        ref_offset = self._parse_time(ref_clip.get('offset', '0s'))

        # Find the media resource
        resources = self.root.find('.//resources')
        media_elem = None
        if resources is not None:
            for m in resources.findall('media'):
                if m.get('id') == media_ref:
                    media_elem = m
                    break

        if media_elem is None:
            raise ValueError(f"Media resource not found for ref: {media_ref}")

        inner_spine = media_elem.find('.//spine')
        if inner_spine is None:
            raise ValueError("No spine found in compound clip media")

        # Get insert position
        spine_children = list(spine)
        insert_idx = spine_children.index(ref_clip)

        # Remove ref-clip from spine
        spine.remove(ref_clip)
        if ref_clip_id in self.clips:
            del self.clips[ref_clip_id]

        # Extract clips from inner spine into main spine
        extracted = []
        current_offset = ref_offset
        for child in list(inner_spine):
            new_clip = copy.deepcopy(child)
            new_clip.set('offset', current_offset.to_fcpxml())
            spine.insert(insert_idx, new_clip)
            insert_idx += 1
            extracted.append(new_clip)
            current_offset = current_offset + self._parse_time(
                child.get('duration', '0s')
            )

            # Index the extracted clip
            clip_name = new_clip.get('name') or new_clip.get('id') or f"flat_{len(self.clips)}"
            self.clips[clip_name] = new_clip

        # Clean up media resource
        if resources is not None:
            resources.remove(media_elem)

        return extracted

    # ========================================================================
    # ROLE OPERATIONS (v0.5.0)
    # ========================================================================

    def assign_role(
        self,
        clip_id: str,
        audio_role: Optional[str] = None,
        video_role: Optional[str] = None,
    ) -> ET.Element:
        """Set the audio/video role on a clip.

        Args:
            clip_id: Name/ID of the clip
            audio_role: Audio role (e.g., "dialogue", "music", "effects")
            video_role: Video role (e.g., "video", "titles")

        Returns:
            The modified clip element
        """
        clip = self._require_clip(clip_id)

        if audio_role is not None:
            clip.set('audioRole', _sanitize_xml_value(audio_role, 256))
        if video_role is not None:
            clip.set('videoRole', _sanitize_xml_value(video_role, 256))

        return clip

    # ========================================================================
    # BULK LOGGING (v0.19.0) — keywords, ratings, roles across many clips
    # ========================================================================

    _CLIP_TAGS = ("asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip")

    def select_clips(
        self,
        clip_name: Optional[str] = None,
        keyword: Optional[str] = None,
        role: Optional[str] = None,
    ) -> List[ET.Element]:
        """Every clip element matching ALL the given filters.

        ``clip_name`` is a glob, ``keyword`` matches any comma-separated
        value of any ``<keyword>``, ``role`` matches audioRole/videoRole
        case-insensitively. No filter selects every clip.
        """
        import fnmatch

        out = []
        for elem in self.root.iter():
            if elem.tag not in self._CLIP_TAGS:
                continue
            if clip_name and not fnmatch.fnmatchcase(elem.get("name", ""), clip_name):
                continue
            if keyword and keyword not in self._keyword_values(elem):
                continue
            if role and role.lower() not in {
                elem.get("audioRole", "").lower(), elem.get("videoRole", "").lower()
            }:
                continue
            out.append(elem)
        return out

    @staticmethod
    def _keyword_values(elem: ET.Element) -> set:
        return {
            v.strip()
            for k in elem.findall("keyword")
            for v in k.get("value", "").split(",")
            if v.strip()
        }

    def bulk_keywords(self, clips: List[ET.Element], keywords: List[str], mode: str) -> int:
        """add / remove / replace whole-clip keywords. Returns clips touched."""
        if mode not in ("add", "remove", "replace"):
            raise ValueError(f"mode must be add, remove or replace, got {mode!r}")
        wanted = [_sanitize_xml_value(k, 256).strip() for k in keywords if k and str(k).strip()]
        if not wanted:
            raise ValueError("keywords must contain at least one non-empty value")
        touched = 0
        for clip in clips:
            if mode == "remove":
                changed = False
                for k in clip.findall("keyword"):
                    present = [v.strip() for v in k.get("value", "").split(",") if v.strip()]
                    kept = [v for v in present if v not in wanted]
                    if len(kept) != len(present):
                        changed = True
                        if kept:
                            k.set("value", ", ".join(kept))
                        else:
                            clip.remove(k)
                touched += changed
                continue
            if mode == "replace":
                for k in clip.findall("keyword"):
                    clip.remove(k)
            new = [k for k in wanted if k not in self._keyword_values(clip)]
            if new:
                # A marker item: DTD order puts it after anchors, before filters.
                _dtd_insert(clip, ET.Element("keyword", value=", ".join(new)))
                touched += 1
            elif mode == "replace":
                touched += 1
        return touched

    def bulk_rating(self, clips: List[ET.Element], rating: str) -> int:
        """favorite / rejected set a whole-clip <rating>; clear removes it."""
        if rating not in ("favorite", "rejected", "clear"):
            raise ValueError(f"rating must be favorite, rejected or clear, got {rating!r}")
        for clip in clips:
            for r in clip.findall("rating"):
                clip.remove(r)
            if rating != "clear":
                _dtd_insert(clip, ET.Element("rating", name=rating.capitalize(), value=rating))
        return len(clips)

    def bulk_roles(
        self,
        clips: List[ET.Element],
        audio_role: Optional[str] = None,
        video_role: Optional[str] = None,
    ) -> int:
        for clip in clips:
            if audio_role is not None:
                clip.set("audioRole", _sanitize_xml_value(audio_role, 256))
            if video_role is not None:
                clip.set("videoRole", _sanitize_xml_value(video_role, 256))
        return len(clips)

    # ========================================================================
    # REFORMAT OPERATIONS (v0.5.0)
    # ========================================================================

    SOCIAL_FORMATS = {
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "16:9": (1920, 1080),
        "4:3": (1440, 1080),
    }

    def reformat_resolution(self, width: int, height: int) -> None:
        """Change the timeline format to a new resolution.

        Updates the format resource dimensions. FCP handles spatial
        conforming (letterbox/pillarbox) on import.

        Args:
            width: Target width in pixels
            height: Target height in pixels
        """
        for fmt in self.root.findall('.//format'):
            fmt.set('width', str(width))
            fmt.set('height', str(height))
            old_name = fmt.get('name', '')
            if old_name:
                fmt.set('name', f"FFVideoFormat{width}x{height}")

        sequence = self.root.find('.//sequence')
        if sequence is not None and sequence.get('format'):
            pass  # format ref stays the same, dimensions updated in-place

    # ========================================================================
    # SILENCE DETECTION OPERATIONS (v0.5.0)
    # ========================================================================

    def detect_silence_candidates(
        self,
        min_gap_seconds: float = 0.5,
        patterns: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Detect potential silence regions using timeline heuristics.

        Checks for:
        1. Gap elements in spine (high confidence)
        2. Ultra-short clips < 0.5s (medium confidence)
        3. Clips matching name patterns like "silence", "room tone" (high)
        4. Duration anomalies > 2 std dev from mean (low-medium)

        Args:
            min_gap_seconds: Minimum gap duration to flag
            patterns: Name patterns to match (default: gap, silence, room tone)

        Returns:
            List of silence candidate dicts
        """
        if patterns is None:
            patterns = ['gap', 'silence', 'room tone', 'dead air', 'blank']

        spine = self._get_spine()
        candidates = []
        durations = []
        clip_index = 0

        # First pass: collect durations for anomaly detection
        for child in spine:
            if child.tag in CLIP_TAGS:
                dur = self._parse_time(child.get('duration', '0s'))
                durations.append(dur.to_seconds())

        # Calculate stats for anomaly detection
        mean_dur = sum(durations) / len(durations) if durations else 0
        variance = (sum((d - mean_dur) ** 2 for d in durations) / len(durations)
                     if len(durations) > 1 else 0)
        std_dev = variance ** 0.5

        # Second pass: detect candidates
        for child in spine:
            tag = child.tag
            offset = child.get('offset', '0s')
            dur = self._parse_time(child.get('duration', '0s'))
            dur_secs = dur.to_seconds()
            tc = TimeValue.from_timecode(offset, self.fps).to_timecode(self.fps)

            if tag == 'gap' and dur_secs >= min_gap_seconds:
                candidates.append({
                    'start_timecode': tc,
                    'duration_seconds': dur_secs,
                    'reason': 'gap',
                    'confidence': 0.9,
                    'clip_name': None,
                    'clip_index': None,
                })
            elif tag in CLIP_TAGS:
                name = child.get('name', '').lower()

                # Name pattern match
                for pat in patterns:
                    if pat.lower() in name:
                        candidates.append({
                            'start_timecode': tc,
                            'duration_seconds': dur_secs,
                            'reason': 'name_match',
                            'confidence': 0.85,
                            'clip_name': child.get('name', ''),
                            'clip_index': clip_index,
                        })
                        break

                # Ultra-short clip
                if dur_secs < 0.5:
                    candidates.append({
                        'start_timecode': tc,
                        'duration_seconds': dur_secs,
                        'reason': 'ultra_short',
                        'confidence': 0.6,
                        'clip_name': child.get('name', ''),
                        'clip_index': clip_index,
                    })

                # Duration anomaly (> 2 std dev longer than mean)
                if std_dev > 0 and dur_secs > mean_dur + 2 * std_dev:
                    candidates.append({
                        'start_timecode': tc,
                        'duration_seconds': dur_secs,
                        'reason': 'duration_anomaly',
                        'confidence': 0.4,
                        'clip_name': child.get('name', ''),
                        'clip_index': clip_index,
                    })

                clip_index += 1

        return candidates

    def remove_silence_candidates(
        self,
        mode: str = "mark",
        min_gap_seconds: float = 0.5,
        min_confidence: float = 0.7,
        patterns: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Remove or mark detected silence candidates.

        Args:
            mode: "delete" removes clips/gaps, "mark" adds red markers,
                  "shorten" trims to minimum
            min_gap_seconds: Minimum gap to consider
            min_confidence: Only act on candidates above this threshold
            patterns: Name patterns to match

        Returns:
            List of actions taken
        """
        candidates = self.detect_silence_candidates(min_gap_seconds, patterns)
        candidates = [c for c in candidates if c['confidence'] >= min_confidence]

        spine = self._get_spine()
        actions = []

        if mode == "mark":
            for c in candidates:
                child = self._find_spine_element_at_timecode(
                    spine, c['start_timecode'], require_clip=True
                )
                if child is not None:
                    build_marker_element(
                        parent=child,
                        marker_type=MarkerType.STANDARD,
                        start=child.get('start', '0s'),
                        duration=frame_duration_attr(self.fps),
                        name=f"SILENCE: {c['reason']}",
                    )
                    actions.append({
                        'action': 'marked',
                        'clip_name': c.get('clip_name', 'gap'),
                        'reason': c['reason'],
                    })

        elif mode == "delete":
            elements_to_remove = []
            for c in candidates:
                child = self._find_spine_element_at_timecode(
                    spine, c['start_timecode']
                )
                if child is not None:
                    elements_to_remove.append(child)
                    actions.append({
                        'action': 'deleted',
                        'clip_name': c.get('clip_name', 'gap'),
                        'reason': c['reason'],
                    })

            for elem in elements_to_remove:
                spine.remove(elem)

            if elements_to_remove:
                self._recalculate_offsets(spine)

        return actions


# ============================================================================
# FCPXML GENERATOR - Create from Python objects
# ============================================================================

class FCPXMLWriter:
    """Generate a new FCPXML document from Python dataclass objects.

    Converts a ``Project`` (containing ``Timeline`` → ``Clip`` → ``Marker``
    hierarchies) into a spec-compliant FCPXML v1.11 element tree and writes
    it to disk.  Used by ``RoughCutGenerator`` and the ``generate_*`` MCP
    tools to create fresh timelines from scratch.

    Unlike ``FCPXMLModifier`` (which mutates existing XML), this class
    *creates* XML from structured Python objects.

    Example::

        from fcpxml.models import Project, Timeline, Clip, Timecode
        project = Project(name="My Edit", timelines=[...])
        writer = FCPXMLWriter()
        writer.write_project(project, "output.fcpxml")
    """

    def __init__(self, version: str = "1.13"):
        """Initialize writer targeting the given FCPXML version."""
        self.version = version
        self.resource_counter = 1

    def _next_resource_id(self) -> str:
        """Return an auto-incrementing resource ID (r1, r2, ...)."""
        rid = f"r{self.resource_counter}"
        self.resource_counter += 1
        return rid

    def _generate_uid(self) -> str:
        """Generate a unique identifier for FCPXML elements."""
        return str(uuid.uuid4()).upper()

    def _tc_to_rational(self, tc: Timecode) -> str:
        """Convert a Timecode to FCPXML rational time string (e.g. '48/24s')."""
        return tc.to_rational()

    def write_project(self, project: Project, filepath: str):
        """Write a project to an FCPXML file."""
        root = self._build_fcpxml(project)
        write_fcpxml(root, filepath)

    def _build_fcpxml(self, project: Project) -> ET.Element:
        """Build the full FCPXML element tree: fcpxml > resources + library > event > project."""
        root = ET.Element('fcpxml', version=self.version)
        resources = ET.SubElement(root, 'resources')
        resource_map = {}

        if project.timelines:
            timeline = project.timelines[0]
            format_id = self._next_resource_id()
            ET.SubElement(resources, 'format',
                id=format_id,
                name=f"FFVideoFormat{timeline.height}p{nominal_fps(timeline.frame_rate)}",
                frameDuration=frame_duration_attr(timeline.frame_rate),
                width=str(timeline.width), height=str(timeline.height))
            resource_map['_format'] = format_id

        # No `location` — see the note in fcpxml/edl.py.
        library = ET.SubElement(root, 'library')
        event = ET.SubElement(library, 'event', name=project.name, uid=self._generate_uid())

        for timeline in project.timelines:
            self._add_timeline(event, timeline, resources, resource_map)
        return root

    def _add_timeline(self, event, timeline, resources, resource_map):
        """Add a timeline as a project > sequence > spine structure under the event."""
        project_elem = ET.SubElement(event, 'project',
            name=timeline.name, uid=self._generate_uid(),
            modDate=datetime.now().strftime("%Y-%m-%d %H:%M:%S -0500"))

        format_id = resource_map.get('_format', 'r1')
        sequence = ET.SubElement(project_elem, 'sequence',
            format=format_id, duration=self._tc_to_rational(timeline.duration),
            tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")

        spine = ET.SubElement(sequence, 'spine')
        for clip in timeline.clips:
            self._add_clip(spine, clip, resources, resource_map)
        for marker in timeline.markers:
            self._add_marker(sequence, marker)

    def _add_clip(self, spine, clip, resources, resource_map):
        """Add a clip as an asset-clip element, creating its asset resource if needed."""
        if clip.media_path and clip.media_path not in resource_map:
            asset_id = self._next_resource_id()
            ET.SubElement(resources, 'asset', id=asset_id, name=clip.name,
                uid=self._generate_uid(), src=clip.media_path, start="0s",
                duration=self._tc_to_rational(clip.duration), hasVideo="1", hasAudio="1")
            resource_map[clip.media_path] = asset_id

        asset_id = resource_map.get(clip.media_path, 'r1')
        format_id = resource_map.get('_format', 'r1')
        clip_elem = ET.SubElement(spine, 'asset-clip',
            ref=asset_id, offset=self._tc_to_rational(clip.start), name=clip.name,
            start=self._tc_to_rational(clip.source_start) if clip.source_start else "0s",
            duration=self._tc_to_rational(clip.duration), format=format_id, tcFormat="NDF")

        for marker in clip.markers:
            self._add_marker(clip_elem, marker)
        for keyword in clip.keywords:
            self._add_keyword(clip_elem, keyword)

    def _add_marker(self, parent: ET.Element, marker: Marker):
        """Add a marker or chapter-marker element to a parent clip or sequence."""
        build_marker_element(
            parent=parent,
            marker_type=marker.marker_type,
            start=self._tc_to_rational(marker.start),
            duration=self._tc_to_rational(marker.duration) if marker.duration else "1/24s",
            name=marker.name,
            note=marker.note or None,
        )

    def _add_keyword(self, parent, keyword):
        """Add a keyword element with optional start/duration range to a parent clip."""
        attrs = {'value': keyword.value}
        if keyword.start:
            attrs['start'] = self._tc_to_rational(keyword.start)
        if keyword.duration:
            attrs['duration'] = self._tc_to_rational(keyword.duration)
        ET.SubElement(parent, 'keyword', **attrs)


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def modify_fcpxml(filepath: str) -> FCPXMLModifier:
    """
    Open an FCPXML file for modification.

    Usage:
        modifier = modify_fcpxml("project.fcpxml")
        modifier.add_marker(...)
        modifier.save("output.fcpxml")
    """
    return FCPXMLModifier(filepath)


def add_marker_to_file(
    filepath: str,
    timecode: str,
    name: str,
    marker_type: str = "standard",
    output_path: Optional[str] = None
) -> str:
    """Convenience function to add a marker to an FCPXML file."""
    modifier = FCPXMLModifier(filepath)
    modifier.add_marker_at_timeline(
        timecode, name,
        MarkerType.from_string(marker_type)
    )
    return modifier.save(output_path)


def trim_clip_in_file(
    filepath: str,
    clip_id: str,
    trim_start: Optional[str] = None,
    trim_end: Optional[str] = None,
    output_path: Optional[str] = None
) -> str:
    """Convenience function to trim a clip in an FCPXML file."""
    modifier = FCPXMLModifier(filepath)
    modifier.trim_clip(clip_id, trim_start, trim_end)
    return modifier.save(output_path)
