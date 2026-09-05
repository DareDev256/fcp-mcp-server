"""
Template System — Pre-built timeline structures for common editing patterns.

Templates define slot-based layouts that can be filled with clips to generate
complete FCPXML timelines. Each template has named slots (video, audio, title,
gap) with duration constraints and lane assignments.
"""

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import TimeValue
from .rational import frame_duration_attr, nominal_fps
from .writer import (
    _create_asset_element,
    _sanitize_xml_value,
    write_fcpxml,
)

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class TemplateSlot:
    """A slot in a template that can be filled with a clip.

    Attributes:
        name: Unique slot name (e.g. "intro", "main", "music_bed").
        slot_type: Type of content: "video", "audio", "title", or "gap".
        min_duration: Minimum duration in seconds (0 = no minimum).
        max_duration: Maximum duration in seconds (0 = no maximum).
        default_duration: Default duration if no clip duration specified.
        lane: Lane assignment (0 = primary spine, positive = above, negative = below).
        role: Audio/video role (e.g. "music", "dialogue", "titles").
        required: Whether this slot must be filled.
    """
    name: str
    slot_type: str = "video"
    min_duration: float = 0.0
    max_duration: float = 0.0
    default_duration: float = 5.0
    lane: int = 0
    role: str = ""
    required: bool = True


@dataclass
class Template:
    """A timeline template with named slots.

    Attributes:
        name: Template identifier (e.g. "intro_outro").
        description: Human-readable description.
        slots: Ordered list of template slots.
    """
    name: str
    description: str
    slots: List[TemplateSlot] = field(default_factory=list)


@dataclass
class ClipSpec:
    """Specification for filling a template slot.

    Provide either asset_id (for existing assets) or src (for new media).

    Attributes:
        asset_id: Reference to an existing asset in the FCPXML.
        src: File path for new media.
        name: Clip display name.
        duration: Override duration in seconds (uses slot default if not set).
    """
    asset_id: Optional[str] = None
    src: Optional[str] = None
    name: str = "Untitled"
    duration: Optional[float] = None


# ============================================================================
# BUILTIN TEMPLATES
# ============================================================================

BUILTIN_TEMPLATES: Dict[str, Template] = {
    "intro_outro": Template(
        name="intro_outro",
        description=(
            "Title card + main content + end card with optional music bed. "
            "Classic YouTube/corporate structure."
        ),
        slots=[
            TemplateSlot(
                name="intro_card", slot_type="video",
                default_duration=5.0, max_duration=15.0,
            ),
            TemplateSlot(
                name="main_content", slot_type="video",
                default_duration=60.0, min_duration=5.0,
            ),
            TemplateSlot(
                name="end_card", slot_type="video",
                default_duration=5.0, max_duration=15.0,
            ),
            TemplateSlot(
                name="music_bed", slot_type="audio",
                default_duration=0.0, lane=-1, role="music",
                required=False,
            ),
        ],
    ),
    "lower_thirds": Template(
        name="lower_thirds",
        description=(
            "Main content with title overlay positions at lane +1. "
            "Useful for interview graphics, name supers."
        ),
        slots=[
            TemplateSlot(
                name="main_content", slot_type="video",
                default_duration=60.0, min_duration=5.0,
            ),
            TemplateSlot(
                name="lower_third_1", slot_type="title",
                default_duration=4.0, max_duration=10.0,
                lane=1, role="titles", required=False,
            ),
            TemplateSlot(
                name="lower_third_2", slot_type="title",
                default_duration=4.0, max_duration=10.0,
                lane=1, role="titles", required=False,
            ),
            TemplateSlot(
                name="lower_third_3", slot_type="title",
                default_duration=4.0, max_duration=10.0,
                lane=1, role="titles", required=False,
            ),
        ],
    ),
    "music_video": Template(
        name="music_video",
        description=(
            "A/B roll structure with music bed. "
            "Alternating performance and cutaway shots over a music track."
        ),
        slots=[
            TemplateSlot(
                name="a_roll_1", slot_type="video",
                default_duration=8.0,
            ),
            TemplateSlot(
                name="b_roll_1", slot_type="video",
                default_duration=4.0,
            ),
            TemplateSlot(
                name="a_roll_2", slot_type="video",
                default_duration=8.0,
            ),
            TemplateSlot(
                name="b_roll_2", slot_type="video",
                default_duration=4.0,
            ),
            TemplateSlot(
                name="a_roll_3", slot_type="video",
                default_duration=8.0,
            ),
            TemplateSlot(
                name="b_roll_3", slot_type="video",
                default_duration=4.0,
            ),
            TemplateSlot(
                name="music_bed", slot_type="audio",
                default_duration=0.0, lane=-1, role="music",
                required=False,
            ),
        ],
    ),
}


# ============================================================================
# PUBLIC API
# ============================================================================

def list_templates() -> List[Dict[str, Any]]:
    """Return all available templates with their slot definitions.

    Returns:
        List of dicts with template name, description, and slot details.
    """
    result = []
    for name, tmpl in BUILTIN_TEMPLATES.items():
        result.append({
            'name': tmpl.name,
            'description': tmpl.description,
            'slots': [
                {
                    'name': s.name,
                    'slot_type': s.slot_type,
                    'default_duration': s.default_duration,
                    'min_duration': s.min_duration,
                    'max_duration': s.max_duration,
                    'lane': s.lane,
                    'role': s.role,
                    'required': s.required,
                }
                for s in tmpl.slots
            ],
        })
    return result


def apply_template(
    template_name: str,
    clips_map: Dict[str, ClipSpec],
    output_path: str,
    fps: float = 24.0,
) -> str:
    """Fill a template with clips and write the resulting FCPXML.

    Args:
        template_name: Name of a builtin template (e.g. "intro_outro").
        clips_map: Dict mapping slot names to ClipSpec objects.
        output_path: Where to write the generated FCPXML.
        fps: Frame rate for the output timeline.

    Returns:
        The output file path.

    Raises:
        ValueError: If template not found or required slots are missing.
    """
    template = BUILTIN_TEMPLATES.get(template_name)
    if template is None:
        available = ', '.join(BUILTIN_TEMPLATES.keys())
        raise ValueError(
            f"Template '{template_name}' not found. Available: {available}"
        )

    # Validate required slots
    for slot in template.slots:
        if slot.required and slot.name not in clips_map:
            raise ValueError(
                f"Required slot '{slot.name}' not filled in template '{template_name}'."
            )

    # Build FCPXML
    fps_int = nominal_fps(fps)
    root = ET.Element('fcpxml', version='1.13')
    resources = ET.SubElement(root, 'resources')

    # Format resource
    format_id = 'r1'
    ET.SubElement(resources, 'format',
                  id=format_id,
                  name=f"FFVideoFormat1080p{fps_int}",
                  frameDuration=frame_duration_attr(fps),
                  width="1920", height="1080")

    # Create assets for each filled slot
    asset_map: Dict[str, str] = {}  # slot_name → asset_id
    asset_counter = 2
    for slot_name, spec in clips_map.items():
        if spec.asset_id:
            asset_map[slot_name] = spec.asset_id
        elif spec.src:
            aid = f"r{asset_counter}"
            asset_counter += 1
            slot = next((s for s in template.slots if s.name == slot_name), None)
            dur = spec.duration or (slot.default_duration if slot else 5.0)
            dur_tv = TimeValue.from_seconds(dur, fps)
            _create_asset_element(
                resources, aid, spec.name, spec.src,
                duration=dur_tv.to_fcpxml(),
                has_video="1" if (not slot or slot.slot_type != "audio") else "0",
                has_audio="1",
            )
            asset_map[slot_name] = aid

    # Library/event/project structure
    # No `location` — see the note in fcpxml/edl.py.
    library = ET.SubElement(root, 'library')
    event = ET.SubElement(library, 'event',
                          name=template_name, uid=str(uuid.uuid4()).upper())
    project_elem = ET.SubElement(event, 'project',
                                 name=template.name,
                                 uid=str(uuid.uuid4()).upper(),
                                 modDate=datetime.now().strftime("%Y-%m-%d %H:%M:%S -0500"))

    # Calculate total spine duration (only lane-0 slots)
    total_duration = TimeValue.zero()
    for slot in template.slots:
        if slot.lane != 0:
            continue
        spec = clips_map.get(slot.name)
        if spec:
            dur = spec.duration or slot.default_duration
        else:
            dur = slot.default_duration
        total_duration = total_duration + TimeValue.from_seconds(dur, fps)

    sequence = ET.SubElement(project_elem, 'sequence',
                             format=format_id,
                             duration=total_duration.to_fcpxml(),
                             tcStart="0s", tcFormat="NDF",
                             audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(sequence, 'spine')

    # Fill spine slots (lane 0) and track connected slots
    current_offset = TimeValue.zero()
    spine_clips: Dict[str, ET.Element] = {}  # slot_name → spine clip element

    for slot in template.slots:
        if slot.lane != 0:
            continue

        spec = clips_map.get(slot.name)
        dur = (spec.duration if spec and spec.duration else slot.default_duration)
        dur_tv = TimeValue.from_seconds(dur, fps)
        aid = asset_map.get(slot.name, format_id)

        if spec:
            clip_elem = ET.SubElement(spine, 'asset-clip',
                                      ref=aid,
                                      offset=current_offset.to_fcpxml(),
                                      name=_sanitize_xml_value(spec.name, 512),
                                      start="0s",
                                      duration=dur_tv.to_fcpxml(),
                                      format=format_id,
                                      tcFormat="NDF")
        else:
            # Gap placeholder for unfilled optional slot
            clip_elem = ET.SubElement(spine, 'gap',
                                      name=slot.name,
                                      offset=current_offset.to_fcpxml(),
                                      duration=dur_tv.to_fcpxml())

        spine_clips[slot.name] = clip_elem
        current_offset = current_offset + dur_tv

    # Fill connected slots (lane != 0) — attach to first spine clip
    first_spine_clip = None
    for s in template.slots:
        if s.lane == 0 and s.name in spine_clips:
            first_spine_clip = spine_clips[s.name]
            break

    for slot in template.slots:
        if slot.lane == 0:
            continue
        spec = clips_map.get(slot.name)
        if not spec:
            continue

        dur = spec.duration or slot.default_duration
        dur_tv = TimeValue.from_seconds(dur, fps)
        aid = asset_map.get(slot.name, format_id)

        parent = first_spine_clip if first_spine_clip is not None else spine

        if slot.slot_type == "audio":
            # Audio: spans total if duration is 0
            if slot.default_duration == 0.0 and spec.duration is None:
                dur_tv = total_duration
            connected = ET.SubElement(parent, 'asset-clip',
                                      ref=aid,
                                      lane=str(slot.lane),
                                      offset="0s",
                                      name=_sanitize_xml_value(spec.name, 512),
                                      start="0s",
                                      duration=dur_tv.to_fcpxml(),
                                      audioRole=slot.role or "music")
        else:
            # Title or video overlay
            connected = ET.SubElement(parent, 'asset-clip',
                                      ref=aid,
                                      lane=str(slot.lane),
                                      offset="0s",
                                      name=_sanitize_xml_value(spec.name, 512),
                                      start="0s",
                                      duration=dur_tv.to_fcpxml())
            if slot.role:
                connected.set('videoRole', slot.role)

    write_fcpxml(root, output_path)
    return output_path
