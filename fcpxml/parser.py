"""
FCPXML Parser - Reads Final Cut Pro XML files into Python objects.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional

from .models import (
    MARKER_XML_TAGS,
    Clip,
    ConnectedClip,
    Keyword,
    Marker,
    MarkerType,
    Project,
    Timecode,
    Timeline,
    TimeValue,
    Transition,
)
from .safe_xml import safe_fromstring, safe_parse

# Maximum FCPXML file size (50 MB) — prevents memory exhaustion from crafted files
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# Tags that represent connected clip elements (includes 'title' for text overlays)
_CONNECTED_CLIP_TAGS = ('asset-clip', 'clip', 'video', 'audio', 'title', 'ref-clip')


class FCPXMLParser:
    """Parser for Final Cut Pro FCPXML files. Supports versions 1.8 - 1.14.

    Unknown elements introduced by newer FCPXML versions (e.g. 1.13's
    ``adjust-stereo-3D`` / ``hidden-clip-marker``, 1.14's smart-collection
    search rules) are ignored on read and preserved untouched by the
    modify path, which operates on the raw ElementTree.
    """

    def __init__(self):
        self.resources: Dict[str, Dict[str, Any]] = {}
        self.formats: Dict[str, Dict[str, Any]] = {}
        self.frame_rate: float = 24.0

    def _tc(self, elem: ET.Element, attr: str, default: str = '0s') -> Timecode:
        """Parse a rational time attribute from an XML element.

        Centralises the ``Timecode.from_rational(elem.get(attr), frame_rate)``
        pattern that repeats across every clip/marker/transition parser.
        """
        return Timecode.from_rational(elem.get(attr, default), self.frame_rate)

    def parse_file(self, filepath: str) -> Project:
        """Parse an FCPXML file and return a Project object.

        Enforces a file size limit to prevent memory exhaustion from
        maliciously large XML files.
        """
        path = Path(filepath)
        if path.suffix == '.fcpxmld':
            fcpxml_path = path / 'Info.fcpxml'
            if not fcpxml_path.exists():
                raise FileNotFoundError(f"Info.fcpxml not found in bundle: {filepath}")
            filepath = str(fcpxml_path)
            path = Path(filepath)
        file_size = path.stat().st_size
        if file_size > _MAX_FILE_SIZE_BYTES:
            raise ValueError(
                f"FCPXML file exceeds maximum size "
                f"({file_size / 1024 / 1024:.1f} MB > "
                f"{_MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB limit)"
            )
        tree = safe_parse(filepath)
        return self._parse_fcpxml(tree.getroot())

    def parse_string(self, xml_string: str) -> Project:
        """Parse FCPXML from a string."""
        return self._parse_fcpxml(safe_fromstring(xml_string))

    def _parse_fcpxml(self, root: ET.Element) -> Project:
        """Parse the root fcpxml element."""
        version = root.get('version', '1.11')
        resources_elem = root.find('resources')
        if resources_elem is not None:
            self._parse_resources(resources_elem)

        timelines = []
        for library in root.findall('.//library'):
            for event in library.findall('event'):
                for project in event.findall('project'):
                    timeline = self._parse_project(project)
                    if timeline:
                        timelines.append(timeline)

        if not timelines:
            for project in root.findall('.//project'):
                timeline = self._parse_project(project)
                if timeline:
                    timelines.append(timeline)

        project_name = timelines[0].name if timelines else "Untitled"
        return Project(name=project_name, timelines=timelines, fcpxml_version=version)

    @staticmethod
    def _rate_from_frame_duration(frame_dur: str) -> Optional[float]:
        """Frames per second from an FCPXML ``frameDuration`` like ``1001/24000s``."""
        if '/' not in frame_dur:
            return None
        parts = frame_dur.rstrip('s').split('/', 1)
        num, denom = int(parts[0]), int(parts[1])
        if num <= 0:
            raise ValueError(f"Invalid frameDuration numerator: {frame_dur}")
        if denom <= 0:
            raise ValueError(f"Invalid frameDuration denominator: {frame_dur}")
        return denom / num

    def _parse_resources(self, resources: ET.Element):
        """Parse the resources section."""
        for index, fmt in enumerate(resources.findall('format')):
            fmt_id = fmt.get('id', '')
            frame_dur = fmt.get('frameDuration', '1/24s')
            rate = self._rate_from_frame_duration(frame_dur)
            self.formats[fmt_id] = {
                'id': fmt_id, 'name': fmt.get('name', ''),
                'width': int(fmt.get('width', 1920)),
                'height': int(fmt.get('height', 1080)),
                'frameDuration': frame_dur,
                'frameRate': rate,
            }
            # Fallback only, for a sequence naming an unresolvable format. The
            # sequence resolves its OWN format in _parse_project, which wins.
            # Previously every format overwrote this, so the last resource in
            # the file decided the rate — a 23.98 sequence in a project holding
            # one 25p drone clip reported 50.0 fps, and every seconds-to-frames
            # conversion downstream was wrong by that factor.
            if index == 0 and rate is not None:
                self.frame_rate = rate

        for asset in resources.findall('asset'):
            asset_id = asset.get('id', '')
            self.resources[asset_id] = {
                'id': asset_id, 'name': asset.get('name', ''),
                'src': asset.get('src', '') or (media_rep.get('src', '') if (media_rep := asset.find('media-rep')) is not None else ''),
                'start': asset.get('start', '0s'),
                'duration': asset.get('duration', '0s'),
                'hasVideo': asset.get('hasVideo', '1') == '1',
                'hasAudio': asset.get('hasAudio', '1') == '1',
            }

    def _parse_project(self, project: ET.Element) -> Optional[Timeline]:
        """Parse a project element into a Timeline."""
        name = project.get('name', 'Untitled')
        sequence = project.find('sequence')
        if sequence is None:
            return None

        format_ref = sequence.get('format', '')
        fmt = self.formats.get(format_ref, {})

        # The sequence's own format decides the timeline's frame rate. This has
        # to land BEFORE _tc() and _parse_spine(), both of which stamp
        # self.frame_rate onto every Timecode they build.
        seq_rate = fmt.get('frameRate')
        if seq_rate is not None:
            self.frame_rate = seq_rate

        timeline = Timeline(
            name=name,
            duration=self._tc(sequence, 'duration'),
            frame_rate=self.frame_rate,
            width=fmt.get('width', 1920),
            height=fmt.get('height', 1080)
        )

        spine = sequence.find('spine')
        if spine is not None:
            self._parse_spine(spine, timeline)

        timeline.markers.extend(self._collect_markers(sequence))

        return timeline

    def _parse_spine(self, spine: ET.Element, timeline: Timeline):
        """Parse the spine (primary storyline) including connected clips."""
        current_offset = 0
        for elem in spine:
            tag = elem.tag
            if tag in ('asset-clip', 'clip', 'video', 'mc-clip', 'sync-clip', 'ref-clip'):
                clip = self._parse_clip(elem, current_offset)
                if clip:
                    timeline.clips.append(clip)
                    self._parse_connected_clips(elem, clip, timeline)
                    current_offset += clip.duration.frames
            elif tag == 'gap':
                gap_frames = self._tc(elem, 'duration').frames
                self._parse_gap_connected_clips(elem, current_offset, timeline)
                current_offset += gap_frames
            elif tag == 'transition':
                transition = self._parse_transition(elem, current_offset)
                if transition:
                    timeline.transitions.append(transition)

    def _parse_clip(self, elem: ET.Element, offset: int) -> Optional[Clip]:
        """Parse a clip element."""
        name = elem.get('name', 'Untitled Clip')
        duration = self._tc(elem, 'duration')
        source_start = self._tc(elem, 'start')
        ref = elem.get('ref', '')
        media_path = self.resources.get(ref, {}).get('src', '')

        clip = Clip(
            name=name,
            start=Timecode(frames=offset, frame_rate=self.frame_rate),
            duration=duration,
            source_start=source_start,
            media_path=media_path,
            audio_role=elem.get('audioRole', ''),
            video_role=elem.get('videoRole', ''),
        )

        clip.markers.extend(self._collect_markers(elem, offset, source_start.frames))

        for keyword_elem in elem.findall('keyword'):
            keyword = self._parse_keyword(keyword_elem)
            if keyword:
                clip.keywords.append(keyword)

        return clip

    def _parse_marker_element(self, elem: ET.Element) -> Optional[Marker]:
        """Parse any marker element (<marker> or <chapter-marker>).

        Type detection is delegated to MarkerType.from_xml_element which
        owns the completed-attribute semantics. This means the parser
        doesn't need separate methods for each tag.
        """
        return Marker(
            name=elem.get('value', ''),
            start=self._tc(elem, 'start'),
            duration=self._tc(elem, 'duration', '1/24s'),
            marker_type=MarkerType.from_xml_element(elem),
            note=elem.get('note', '')
        )

    def _collect_markers(
        self,
        elem: ET.Element,
        host_offset_frames: Optional[int] = None,
        host_start_frames: int = 0,
    ) -> list:
        """Collect all markers (standard + chapter) from an element in a single pass.

        Iterates children once, selecting recognised marker tags via
        MARKER_XML_TAGS rather than making a separate findall per tag.

        A marker's ``start`` is in the host's local time, which begins at the
        host's ``start`` attribute. When the caller knows where the host sits
        on the timeline, each marker's ``timeline_start`` is resolved as
        ``host_offset + (marker.start - host_start)``; the raw ``start`` is
        kept as written.
        """
        markers = [
            marker
            for child in elem
            if child.tag in MARKER_XML_TAGS
            for marker in [self._parse_marker_element(child)]
            if marker is not None
        ]
        if host_offset_frames is not None:
            for marker in markers:
                marker.timeline_start = Timecode(
                    frames=host_offset_frames + marker.start.frames - host_start_frames,
                    frame_rate=self.frame_rate,
                )
        return markers

    def _parse_keyword(self, elem: ET.Element) -> Optional[Keyword]:
        """Parse a keyword element."""
        return Keyword(
            value=elem.get('value', ''),
            start=self._tc(elem, 'start') if elem.get('start') else None,
            duration=self._tc(elem, 'duration') if elem.get('duration') else None,
        )

    def _parse_transition(self, elem: ET.Element, offset: int) -> Optional[Transition]:
        """Parse a transition element."""
        return Transition(
            name=elem.get('name', 'Cross Dissolve'),
            duration=self._tc(elem, 'duration', '1s'),
            start=Timecode(frames=offset, frame_rate=self.frame_rate)
        )

    def get_library_clips(self, keywords: Optional[list] = None) -> list:
        """
        Get all available clips from the library (assets in resources section).

        Args:
            keywords: Optional list of keywords to filter by

        Returns:
            List of dicts with asset metadata: name, asset_id, duration_seconds, src
        """
        result = []
        for asset_id, asset_data in self.resources.items():
            # Parse duration to seconds
            duration_str = asset_data.get('duration', '0s')
            duration_seconds = self._parse_duration_to_seconds(duration_str)

            clip_info = {
                'asset_id': asset_id,
                'name': asset_data.get('name', ''),
                'duration_seconds': duration_seconds,
                'src': asset_data.get('src', ''),
                'has_video': asset_data.get('hasVideo', True),
                'has_audio': asset_data.get('hasAudio', True),
            }
            result.append(clip_info)

        # Filter by keywords if provided
        if keywords:
            # For now, assets don't have keywords directly - return empty if filtering
            # In real FCPXML, keywords are typically on clips in events, not assets
            return []

        return result

    def _iter_connected_elements(self, parent_elem: ET.Element, parent_name: str):
        """Yield ``(element, lane, parent_name)`` tuples for connected clips.

        Shared iteration logic for both spine-clip and gap-attached connected
        clips — walks direct children with a ``lane`` attribute and
        ``<storyline>`` wrappers, yielding parsed :class:`ConnectedClip`
        objects without prescribing where they get stored.
        """
        for child in parent_elem:
            lane = child.get('lane')
            if lane is not None and child.tag in _CONNECTED_CLIP_TAGS:
                connected = self._parse_one_connected_clip(
                    child, int(lane), parent_name)
                if connected:
                    yield connected
            elif child.tag == 'storyline':
                lane_val = int(child.get('lane', '1'))
                for sub_elem in child:
                    if sub_elem.tag in _CONNECTED_CLIP_TAGS:
                        connected = self._parse_one_connected_clip(
                            sub_elem, lane_val, parent_name)
                        if connected:
                            yield connected

    def _parse_connected_clips(self, parent_elem: ET.Element,
                                parent_clip: Clip, timeline: Timeline):
        """Parse connected clips attached to a primary storyline clip."""
        for connected in self._iter_connected_elements(parent_elem, parent_clip.name):
            parent_clip.connected_clips.append(connected)
            timeline.connected_clips.append(connected)

    def _parse_gap_connected_clips(self, gap_elem: ET.Element,
                                    gap_offset: int, timeline: Timeline):
        """Parse connected clips attached to gap elements."""
        for connected in self._iter_connected_elements(gap_elem, f"gap@{gap_offset}"):
            timeline.connected_clips.append(connected)

    def _parse_one_connected_clip(self, elem: ET.Element, lane: int,
                                   parent_name: str) -> Optional[ConnectedClip]:
        """Parse a single connected clip element."""
        name = elem.get('name', 'Untitled')
        duration = self._tc(elem, 'duration')
        start = self._tc(elem, 'start')
        offset = self._tc(elem, 'offset')
        ref = elem.get('ref', '')
        media_path = self.resources.get(ref, {}).get('src', '')
        role = elem.get('audioRole', '') or elem.get('videoRole', '')

        connected = ConnectedClip(
            name=name, start=start, duration=duration,
            lane=lane, offset=offset, source_start=start,
            media_path=media_path, clip_type=elem.tag, role=role,
            ref_id=ref, parent_clip_name=parent_name,
        )

        connected.markers.extend(self._collect_markers(elem, offset.frames, start.frames))

        for keyword_elem in elem.findall('keyword'):
            keyword = self._parse_keyword(keyword_elem)
            if keyword:
                connected.keywords.append(keyword)

        return connected

    def _parse_duration_to_seconds(self, duration_str: str) -> float:
        """Convert FCPXML duration string to seconds.

        Delegates to TimeValue.from_timecode() which handles rational
        format (``"150/30s"``), plain seconds (``"10s"``), timecode
        (``HH:MM:SS:FF``), and frame counts (``"15f"``).
        """
        try:
            return TimeValue.from_timecode(duration_str).to_seconds()
        except (ValueError, ZeroDivisionError):
            # Zero-denominator or unparseable → 0.0 (matches prior behaviour)
            return 0.0


def parse_fcpxml(filepath: str) -> Project:
    """Convenience function to parse an FCPXML file."""
    return FCPXMLParser().parse_file(filepath)
