"""Export FCPXML to other NLE formats.

Supports:
- Simplified FCPXML v1.9 for DaVinci Resolve compatibility
- FCP7 XML (XMEML) for Premiere Pro / Resolve / Avid compatibility
"""

import copy
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import Any, Dict, List

from .parser import FCPXMLParser
from .rational import is_ntsc_rate, nominal_fps, rational_fps
from .safe_xml import serialize_xml
from .writer import _sanitize_xml_value


def _to_frames(seconds: float, frame_rate: float) -> int:
    """Whole frames for *seconds* at the exact rate, rounded not truncated.

    ``int(2.0 * 23.976023976)`` is 47, not 48 — truncation silently lost a
    frame off the end of every clip exported at a broadcast rate.
    """
    exact = Fraction(seconds).limit_denominator(1000000) * rational_fps(frame_rate)
    return round(exact.numerator / exact.denominator)


class DaVinciExporter:
    """Export FCPXML in formats compatible with other NLEs."""

    def __init__(self, source_path: str):
        """Load the source FCPXML file.

        Args:
            source_path: Path to the source FCPXML file
        """
        self.source_path = source_path
        from .safe_xml import safe_parse
        self.tree = safe_parse(source_path)
        self.root = self.tree.getroot()
        self.parser = FCPXMLParser()
        self.project = self.parser.parse_file(source_path)

    def export_simplified_fcpxml(self, output_path: str,
                                  flatten_compounds: bool = True) -> str:
        """Generate simplified FCPXML v1.9 for DaVinci Resolve.

        Strips features that cause Resolve import issues:
        - Downgrades version to 1.9
        - Optionally flattens compound clips
        - Removes unsupported attributes

        Args:
            output_path: Destination file path
            flatten_compounds: Whether to flatten compound clips

        Returns:
            Path to the generated file
        """
        root = copy.deepcopy(self.root)
        root.set('version', '1.9')

        # Strip attributes that cause Resolve issues
        unsupported_attrs = ['mcClipAngle', 'modDate', 'colorProcessing']
        for elem in root.iter():
            for attr in unsupported_attrs:
                if attr in elem.attrib:
                    del elem.attrib[attr]

        # Flatten compound clips (ref-clips pointing to media sequences)
        if flatten_compounds:
            for spine in root.findall('.//spine'):
                for ref_clip in spine.findall('ref-clip'):
                    # Convert ref-clip to simple asset-clip
                    ref_clip.tag = 'asset-clip'
                    # Keep core attributes, strip compound-specific ones
                    for attr in list(ref_clip.attrib.keys()):
                        if attr not in ('ref', 'offset', 'duration', 'start', 'name', 'format'):
                            del ref_clip.attrib[attr]

        return serialize_xml(root, output_path, '<!DOCTYPE fcpxml>')

    def export_xmeml(self, output_path: str) -> str:
        """Generate FCP7 XML (XMEML) for maximum NLE compatibility.

        Converts FCPXML's spine-based model to XMEML's track-based model:
        - Primary storyline -> Video Track 1
        - Connected clips lane +N -> Video Track N+1
        - Connected clips lane -N -> Audio Track N+1

        Args:
            output_path: Destination file path

        Returns:
            Path to the generated file
        """
        tl = self.project.primary_timeline
        if tl is None:
            raise ValueError("No timeline found in source FCPXML")

        # Build track model from spine
        video_tracks, audio_tracks = self._spine_to_tracks()

        # Build XMEML document
        xmeml = ET.Element('xmeml')
        xmeml.set('version', '5')

        sequence = ET.SubElement(xmeml, 'sequence')
        ET.SubElement(sequence, 'name').text = _sanitize_xml_value(tl.name, 512)

        total_frames = _to_frames(tl.duration.seconds, tl.frame_rate)
        ET.SubElement(sequence, 'duration').text = str(total_frames)

        # XMEML expresses a broadcast rate as the nominal integer timebase
        # plus an <ntsc> flag — timebase 24 + ntsc TRUE *is* 23.98. Writing
        # int(23.976) = 23 with ntsc FALSE described a rate that does not
        # exist, and the exact-float membership test that set the flag never
        # matched the 23.976023976... a real file parses to.
        timebase = str(nominal_fps(tl.frame_rate))
        is_ntsc = is_ntsc_rate(tl.frame_rate)

        rate = ET.SubElement(sequence, 'rate')
        ET.SubElement(rate, 'timebase').text = timebase
        ET.SubElement(rate, 'ntsc').text = 'TRUE' if is_ntsc else 'FALSE'

        # Timecode
        tc = ET.SubElement(sequence, 'timecode')
        tc_rate = ET.SubElement(tc, 'rate')
        ET.SubElement(tc_rate, 'timebase').text = timebase
        ET.SubElement(tc_rate, 'ntsc').text = 'TRUE' if is_ntsc else 'FALSE'
        ET.SubElement(tc, 'string').text = '00:00:00:00'
        ET.SubElement(tc, 'frame').text = '0'

        media = ET.SubElement(sequence, 'media')

        # Video tracks
        video = ET.SubElement(media, 'video')
        format_elem = ET.SubElement(video, 'format')
        sample = ET.SubElement(format_elem, 'samplecharacteristics')
        ET.SubElement(sample, 'width').text = str(tl.width)
        ET.SubElement(sample, 'height').text = str(tl.height)

        for track_num in sorted(video_tracks.keys()):
            track = ET.SubElement(video, 'track')
            for clip_data in video_tracks[track_num]:
                self._add_xmeml_clipitem(track, clip_data, tl.frame_rate)

        # Audio tracks
        audio = ET.SubElement(media, 'audio')
        for track_num in sorted(audio_tracks.keys()):
            track = ET.SubElement(audio, 'track')
            for clip_data in audio_tracks[track_num]:
                self._add_xmeml_clipitem(track, clip_data, tl.frame_rate)

        # If no explicit audio tracks, create one from primary storyline
        if not audio_tracks and video_tracks.get(0):
            track = ET.SubElement(audio, 'track')
            for clip_data in video_tracks[0]:
                if clip_data.get('has_audio', True):
                    self._add_xmeml_clipitem(track, clip_data, tl.frame_rate)

        return serialize_xml(xmeml, output_path, '<!DOCTYPE xmeml>')

    def _spine_to_tracks(self) -> tuple:
        """Convert spine + connected clips to track-based model.

        Returns:
            Tuple of (video_tracks, audio_tracks) where each is a
            dict mapping track number to list of clip data dicts.
        """
        tl = self.project.primary_timeline
        video_tracks: Dict[int, List[Dict[str, Any]]] = {0: []}
        audio_tracks: Dict[int, List[Dict[str, Any]]] = {}

        # Primary storyline -> track 0
        for clip in tl.clips:
            video_tracks[0].append({
                'name': clip.name,
                'start_seconds': clip.start.seconds,
                'duration_seconds': clip.duration_seconds,
                'source_start_seconds': clip.source_start.seconds if clip.source_start else 0,
                'media_path': clip.media_path,
                'has_audio': True,
            })

        # Connected clips -> higher tracks
        for cc in tl.connected_clips:
            if cc.lane > 0:
                track_num = cc.lane
                if track_num not in video_tracks:
                    video_tracks[track_num] = []
                video_tracks[track_num].append({
                    'name': cc.name,
                    'start_seconds': cc.offset.seconds if cc.offset else 0,
                    'duration_seconds': cc.duration_seconds,
                    'source_start_seconds': cc.source_start.seconds if cc.source_start else 0,
                    'media_path': cc.media_path,
                    'has_audio': cc.clip_type not in ('title', 'video'),
                })
            elif cc.lane < 0:
                track_num = abs(cc.lane) - 1
                if track_num not in audio_tracks:
                    audio_tracks[track_num] = []
                audio_tracks[track_num].append({
                    'name': cc.name,
                    'start_seconds': cc.offset.seconds if cc.offset else 0,
                    'duration_seconds': cc.duration_seconds,
                    'source_start_seconds': cc.source_start.seconds if cc.source_start else 0,
                    'media_path': cc.media_path,
                    'has_audio': True,
                })

        return video_tracks, audio_tracks

    def _add_xmeml_clipitem(self, track: ET.Element,
                             clip_data: Dict[str, Any],
                             frame_rate: float):
        """Add a clipitem element to an XMEML track."""
        clipitem = ET.SubElement(track, 'clipitem')
        ET.SubElement(clipitem, 'name').text = _sanitize_xml_value(
            clip_data['name'], 512
        )

        duration_frames = _to_frames(clip_data['duration_seconds'], frame_rate)
        ET.SubElement(clipitem, 'duration').text = str(duration_frames)

        rate = ET.SubElement(clipitem, 'rate')
        ET.SubElement(rate, 'timebase').text = str(nominal_fps(frame_rate))
        ET.SubElement(rate, 'ntsc').text = (
            'TRUE' if is_ntsc_rate(frame_rate) else 'FALSE'
        )

        start_frame = _to_frames(clip_data['start_seconds'], frame_rate)
        source_in = _to_frames(clip_data['source_start_seconds'], frame_rate)
        source_out = source_in + duration_frames

        ET.SubElement(clipitem, 'start').text = str(start_frame)
        ET.SubElement(clipitem, 'end').text = str(start_frame + duration_frames)
        ET.SubElement(clipitem, 'in').text = str(source_in)
        ET.SubElement(clipitem, 'out').text = str(source_out)

        if clip_data.get('media_path'):
            file_elem = ET.SubElement(clipitem, 'file')
            pathurl = ET.SubElement(file_elem, 'pathurl')
            pathurl.text = _sanitize_xml_value(clip_data['media_path'], 2048)
