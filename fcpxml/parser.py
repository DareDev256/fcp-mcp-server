"""
FCPXML Parser - Reads Final Cut Pro XML files into Python objects.
"""

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional

from .models import (
    MARKER_XML_TAGS,
    Clip,
    ConnectedClip,
    ConnectedStoryline,
    Keyword,
    Marker,
    MarkerType,
    MediaRef,
    Project,
    Timecode,
    Timeline,
    TimeValue,
    Transform,
    Transition,
)
from .rational import parse_seconds, rational_fps
from .safe_xml import safe_fromstring, safe_parse

# Maximum FCPXML file size (50 MB) — prevents memory exhaustion from crafted files
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# Tags that represent connected clip elements. Every %anchor_item; the DTD
# allows under a clip or a gap that carries content: 'title' for text
# overlays, 'mc-clip' / 'sync-clip' because a connected-clip multicam edit
# (the FCP 12.3 export shape, issue #23) hangs every picture off a gap, and
# 'caption' because it has no ref and no media but is still on a lane.
_CONNECTED_CLIP_TAGS = (
    'asset-clip', 'clip', 'video', 'audio', 'title', 'ref-clip',
    'mc-clip', 'sync-clip', 'caption',
)

# A connected storyline. Apple's DTD spells it <spine lane="N" offset="...">
# (spine is an anchor item, so it hangs off a clip or a gap); <storyline> is
# not in any FCPXML DTD and is read only so files shaped for it by 0.32 and
# earlier keep their reading.
_CONNECTED_STORYLINE_TAGS = ('spine', 'storyline')

# Tags whose media path comes from a <media> resource rather than an <asset>.
_MEDIA_REF_TAGS = ('mc-clip', 'ref-clip')

# Spine elements that occupy primary-storyline time without being a clip the
# timeline model carries. Each still pushes every later element along by its
# duration; skipping one moved every clip after a spine title early by the
# title's length.
_SPINE_TIME_ONLY_TAGS = ('title', 'audio', 'audition', 'live-drawing', 'caption')

# Image files Final Cut imports as stills.
_STILL_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.heic', '.heif', '.tif', '.tiff', '.bmp',
    '.gif', '.psd', '.exr', '.webp', '.dpx', '.tga',
)

# Child elements that change a clip's picture and that no preview here draws.
# adjust-transform and adjust-conform are read (see _picture_of); the
# colour-conform, stabilisation, rolling-shutter and audio adjustments are
# left out on purpose: they do not change what the cut looks like.
_UNDRAWN_VISUAL_TAGS = (
    'adjust-crop', 'adjust-corners', 'adjust-blend', 'adjust-360-transform',
    'adjust-reorient', 'adjust-orientation', 'filter-video', 'filter-video-mask',
)


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
        self.media: Dict[str, Dict[str, Any]] = {}
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
                # A still's format often declares no size; the 1920x1080
                # defaults above would then read as a real measurement.
                'declaredSize': (
                    (int(fmt.get('width')), int(fmt.get('height')))
                    if fmt.get('width') and fmt.get('height') else None
                ),
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
                'format': asset.get('format', ''),
                'hasVideo': asset.get('hasVideo', '1') == '1',
                'hasAudio': asset.get('hasAudio', '1') == '1',
                # The DTD leaves hasAudio #IMPLIED. Final Cut writes
                # hasAudio="1" whenever an asset has sound, so absence is read
                # as none here; a probe of the real file overrides it anyway.
                'declaresAudio': asset.get('hasAudio') == '1',
            }

        # <media> resources: a multicam (angles, each a small storyline) or a
        # compound clip (a nested sequence). An mc-clip / ref-clip refs one of
        # THESE, not an asset, so before this pass such a clip had no media
        # path even when it sat directly on the spine (issue #23). Only the
        # angle -> first asset ref is recorded; the asset's src is looked up
        # at clip-parse time because resource order in the file is free.
        for media in resources.findall('media'):
            media_id = media.get('id', '')
            multicam = media.find('multicam')
            if multicam is not None:
                angles = []
                for angle in multicam.findall('mc-angle'):
                    first = angle.find('asset-clip')
                    angles.append((angle.get('angleID', ''), first.get('ref', '') if first is not None else ''))
                self.media[media_id] = {'id': media_id, 'name': media.get('name', ''),
                                        'kind': 'multicam', 'angles': angles}
                continue
            sequence = media.find('sequence')
            if sequence is not None:
                first = sequence.find('.//asset-clip')
                self.media[media_id] = {'id': media_id, 'name': media.get('name', ''),
                                        'kind': 'sequence',
                                        'angles': [('', first.get('ref', '') if first is not None else '')]}

    def _name_for(self, elem: ET.Element, fallback: str = 'Untitled') -> str:
        """An element's own name, else the name of the resource it refs.

        FCP leaves ``name`` off an mc-clip that still shows the multicam's
        name in the timeline; showing "Untitled" for it hides the one label
        the editor knows the clip by.
        """
        name = elem.get('name')
        if name:
            return name
        ref = elem.get('ref', '')
        source = self.media.get(ref) or self.resources.get(ref) or {}
        return source.get('name') or fallback

    def _media_path_for(self, elem: ET.Element) -> str:
        """Resolve the source media an element refers to.

        asset-clip / clip / video / audio ref an <asset>. mc-clip and ref-clip
        ref a <media>; for a multicam the enabled angle (``<mc-source
        srcEnable="all|video">``) decides which asset, and with no mc-source
        the first angle is what FCP shows. A caption or title has no media.
        """
        ref = elem.get('ref', '')
        if not ref:
            return ''
        if elem.tag in _MEDIA_REF_TAGS and ref in self.media:
            angles = self.media[ref]['angles']
            if not angles:
                return ''
            chosen = angles[0][1]
            for source in elem.findall('mc-source'):
                if source.get('srcEnable', 'all') in ('all', 'video'):
                    wanted = source.get('angleID', '')
                    for angle_id, asset_ref in angles:
                        if angle_id == wanted:
                            chosen = asset_ref
                            break
                    break
            return self.resources.get(chosen, {}).get('src', '')
        return self.resources.get(ref, {}).get('src', '')

    def _media_ref_for(self, elem: ET.Element) -> Optional[MediaRef]:
        """The asset an element plays, as the renderer needs it: clock origin,
        still or not, declared size.

        Only for elements that ref an ``<asset>`` directly. An mc-clip or
        ref-clip plays a ``<media>`` whose own clock is not the angle file's,
        so it gets None and keeps the old zero-origin reading rather than a
        guessed one.
        """
        ref = elem.get('ref', '')
        if not ref or elem.tag in _MEDIA_REF_TAGS:
            return None
        asset = self.resources.get(ref)
        if asset is None:
            return None
        try:
            origin = parse_seconds(asset.get('start'))
            duration = parse_seconds(asset.get('duration'))
        except ValueError:
            return None
        size = self.formats.get(asset.get('format', ''), {}).get('declaredSize')
        src = asset.get('src', '').split('?', 1)[0].lower()
        still = asset.get('hasVideo', True) and (
            duration == 0 or src.endswith(_STILL_EXTENSIONS)
        )
        # <video> and <audio> each play ONE component of the asset: a
        # <video> element's sound is not heard (its audio, when used, is a
        # sibling <audio> element), and an <audio> element has no picture.
        return MediaRef(
            origin=origin,
            duration=None if still else duration,
            width=size[0] if size else None,
            height=size[1] if size else None,
            still=bool(still),
            has_video=bool(asset.get('hasVideo', True)) and elem.tag != 'audio',
            has_audio=bool(asset.get('declaresAudio')) and not still and elem.tag != 'video',
        )

    @staticmethod
    def _volume_of(elem: ET.Element) -> Optional[float]:
        """<adjust-volume amount="-6dB"> in dB, None when absent or unreadable."""
        v = elem.find('adjust-volume')
        if v is None:
            return None
        text = (v.get('amount') or '').strip().lower().removesuffix('db').strip()
        try:
            return float(text) if text else None
        except ValueError:
            return None

    @staticmethod
    def _pair(text: Optional[str], default: tuple) -> tuple:
        try:
            values = [float(v) for v in (text or '').split()]
        except ValueError:
            return default
        if len(values) == 1:
            return (values[0], values[0])
        return (values[0], values[1]) if len(values) >= 2 else default

    def _picture_of(self, elem: ET.Element) -> tuple:
        """(conform, transform, undrawn effects) for a clip element."""
        conform_elem = elem.find('adjust-conform')
        conform = (conform_elem.get('type', 'fit') if conform_elem is not None else 'fit')
        transform = None
        t = elem.find('adjust-transform')
        if t is not None and t.get('enabled', '1') != '0':
            try:
                rotation = float(t.get('rotation', '0') or 0)
            except ValueError:
                rotation = 0.0
            transform = Transform(
                position=self._pair(t.get('position'), (0.0, 0.0)),
                scale=self._pair(t.get('scale'), (1.0, 1.0)),
                rotation=rotation,
                anchor=self._pair(t.get('anchor'), (0.0, 0.0)),
                animated=any(p.find('keyframeAnimation') is not None for p in t.findall('param')),
            )
        effects = tuple(child.tag for child in elem if child.tag in _UNDRAWN_VISUAL_TAGS)
        return conform, transform, effects

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
        # Where the previous element ended, on the spine's OWN clock (its
        # offset attributes, which may begin at tcStart rather than 0). A
        # transition's offset is on that clock too, so the two subtract to
        # how much of it runs before the cut.
        raw_cut = None
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
                self._parse_gap_connected_clips(
                    elem, current_offset, self._tc(elem, 'start').frames, timeline)
                # A gap's own markers are timeline markers. The mark tool writes
                # them here on a gap-based storyline; until 0.33 they were
                # written and never read back.
                timeline.markers.extend(self._collect_gap_markers(elem, current_offset))
                current_offset += gap_frames
            elif tag == 'transition':
                transition = self._parse_transition(elem, current_offset, raw_cut)
                if transition:
                    timeline.transitions.append(transition)
                continue
            elif tag in _SPINE_TIME_ONLY_TAGS:
                current_offset += self._tc(elem, 'duration').frames
            else:
                continue
            try:
                raw_cut = parse_seconds(elem.get('offset')) + parse_seconds(elem.get('duration'))
            except ValueError:
                raw_cut = None

    def _parse_clip(self, elem: ET.Element, offset: int) -> Optional[Clip]:
        """Parse a clip element."""
        name = self._name_for(elem, 'Untitled Clip')
        duration = self._tc(elem, 'duration')
        source_start = self._tc(elem, 'start')
        media_path = self._media_path_for(elem)

        conform, transform, effects = self._picture_of(elem)
        clip = Clip(
            name=name,
            start=Timecode(frames=offset, frame_rate=self.frame_rate),
            duration=duration,
            source_start=source_start,
            media_path=media_path,
            audio_role=elem.get('audioRole', ''),
            video_role=elem.get('videoRole', ''),
            media=self._media_ref_for(elem),
            conform=conform,
            transform=transform,
            effects=effects,
            volume_db=self._volume_of(elem),
            enabled=elem.get('enabled', '1') != '0',
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

    def _collect_gap_markers(self, gap: ET.Element, gap_offset_frames: int) -> list:
        """A gap's markers, each placed on the timeline: where the gap sits
        plus how far into the gap's clock (which begins at its ``start``) the
        marker is, measured in exact seconds and rounded to the nearest
        frame once. A broadcast gap starts at 3600s, which is not on the
        23.976 frame grid; subtracting two separately truncated frame counts
        put every marker one frame late."""
        try:
            gap_start = parse_seconds(gap.get('start', '0s'))
        except ValueError:
            gap_start = Fraction(0)
        frame = Fraction(1) / rational_fps(self.frame_rate)
        out = []
        for child in gap:
            if child.tag not in MARKER_XML_TAGS:
                continue
            marker = self._parse_marker_element(child)
            if marker is None:
                continue
            try:
                into = parse_seconds(child.get('start', '0s')) - gap_start
            except ValueError:
                into = Fraction(0)
            marker.timeline_start = Timecode(
                frames=gap_offset_frames + int((into / frame + Fraction(1, 2)) // 1),
                frame_rate=self.frame_rate,
            )
            out.append(marker)
        return out

    def _parse_keyword(self, elem: ET.Element) -> Optional[Keyword]:
        """Parse a keyword element."""
        return Keyword(
            value=elem.get('value', ''),
            start=self._tc(elem, 'start') if elem.get('start') else None,
            duration=self._tc(elem, 'duration') if elem.get('duration') else None,
        )

    def _parse_transition(self, elem: ET.Element, offset: int,
                          raw_cut=None) -> Optional[Transition]:
        """Parse a transition element.

        ``start`` stays the cut it sits on. ``lead`` is how much of it runs
        before that cut, from its own ``offset``; Final Cut centres a new
        transition, but an edit can leave it anywhere across the cut.
        ``transition_type`` is left empty: the document says what the
        transition is by ``name``, and the model's "cross-dissolve" default
        would otherwise vouch for every unrecognised one.
        """
        duration = self._tc(elem, 'duration', '1s')
        lead = None
        if raw_cut is not None and elem.get('offset') is not None:
            try:
                before = raw_cut - parse_seconds(elem.get('offset'))
                length = parse_seconds(elem.get('duration', '1s'))
            except ValueError:
                before = None
            if before is not None and 0 <= before <= length:
                lead = Timecode(frames=round(Fraction(before) * rational_fps(self.frame_rate)),
                                frame_rate=self.frame_rate)
        return Transition(
            name=elem.get('name', 'Cross Dissolve'),
            duration=duration,
            start=Timecode(frames=offset, frame_rate=self.frame_rate),
            transition_type='',
            lead=lead,
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

    def _iter_connected_elements(self, parent_elem: ET.Element, parent_name: str,
                                 parent_offset: int, parent_start: int,
                                 storylines: Optional[list] = None):
        """Yield parsed :class:`ConnectedClip` objects hanging off ``parent_elem``.

        Shared iteration logic for both spine-clip and gap-attached connected
        clips — walks direct children with a ``lane`` attribute and connected
        storylines (``<spine lane>``, and the legacy ``<storyline>``), without
        prescribing where the results get stored. ``parent_offset`` is where
        the parent sits on the timeline (frames) and ``parent_start`` is
        where its local clock begins; a child's ``offset`` is written in that
        local clock, so its timeline position is
        ``parent_offset + (offset - parent_start)``. A storyline's items are
        on the storyline's own clock instead (see
        :meth:`_parse_connected_storyline`); the storyline itself is appended
        to ``storylines`` when given.
        """
        for child in parent_elem:
            lane = child.get('lane')
            if lane is not None and child.tag in _CONNECTED_CLIP_TAGS:
                connected = self._parse_one_connected_clip(
                    child, int(lane), parent_name, parent_offset, parent_start)
                if connected:
                    yield connected
            elif child.tag in _CONNECTED_STORYLINE_TAGS:
                storyline = self._parse_connected_storyline(
                    child, parent_elem, parent_name, parent_offset)
                if storylines is not None:
                    storylines.append(storyline)
                yield from storyline.clips

    def _parse_connected_storyline(self, storyline: ET.Element, parent_elem: ET.Element,
                                   parent_name: str, parent_offset: int) -> ConnectedStoryline:
        """A connected storyline and every item in it, placed on the timeline.

        Its ``offset`` is on the PARENT's clock (which starts at the parent's
        ``start``); its items' offsets are on its OWN clock, which starts at
        0. So an item plays at
        ``parent position + (storyline offset - parent start) + item offset``,
        measured in exact seconds and rounded to a frame once. Items follow
        one another like the primary storyline's: clips and gaps take time, a
        transition takes none and sits on the cut before it (the storyline's
        first frame when it comes first). A clip's ``offset`` is kept on the
        parent's clock (storyline offset + item offset), as it is for every
        connected clip. The legacy ``<storyline>`` carries no offset: its
        clock is the parent's, which is how 0.32 and earlier read it.
        """
        lane = int(storyline.get('lane', '1'))
        frame = Fraction(1) / rational_fps(self.frame_rate)
        try:
            own_offset = parse_seconds(storyline.get('offset', '0s'))
            base = own_offset - parse_seconds(parent_elem.get('start', '0s'))
        except ValueError:
            own_offset = base = Fraction(0)

        def frames_at(local: Fraction) -> int:
            return parent_offset + int(((base + local) / frame + Fraction(1, 2)) // 1)

        def seconds(item: ET.Element, attr: str, default: Fraction) -> Fraction:
            try:
                return parse_seconds(item.get(attr)) if item.get(attr) is not None else default
            except ValueError:
                return default

        record = ConnectedStoryline(
            lane=lane,
            start=Timecode(frames=frames_at(Fraction(0)), frame_rate=self.frame_rate),
            duration=Timecode(frames=0, frame_rate=self.frame_rate),
            parent_clip_name=parent_name,
        )
        running = Fraction(0)  # where the next item starts, on the storyline's clock
        end = Fraction(0)
        for item in storyline:
            local = seconds(item, 'offset', running)
            if item.tag == 'transition':
                transition = self._parse_transition(item, frames_at(running), running)
                if transition:
                    transition.lane = lane
                    record.transitions.append(transition)
                continue
            length = seconds(item, 'duration', Fraction(0))
            if item.tag in _CONNECTED_CLIP_TAGS:
                clip = self._parse_one_connected_clip(
                    item, lane, parent_name,
                    timeline_frames=frames_at(local),
                    parent_clock_offset=own_offset + local)
                if clip:
                    record.clips.append(clip)
            elif item.tag == 'gap':
                record.gaps.append((
                    Timecode(frames=frames_at(local), frame_rate=self.frame_rate),
                    Timecode(frames=int((length / frame + Fraction(1, 2)) // 1), frame_rate=self.frame_rate),
                ))
            else:
                record.skipped.append(item.tag)
            running = local + length
            end = max(end, running)
        record.duration = Timecode(frames=int((end / frame + Fraction(1, 2)) // 1), frame_rate=self.frame_rate)
        return record

    def _parse_connected_clips(self, parent_elem: ET.Element,
                                parent_clip: Clip, timeline: Timeline):
        """Parse connected clips attached to a primary storyline clip."""
        for connected in self._iter_connected_elements(
                parent_elem, parent_clip.name,
                parent_clip.start.frames, parent_clip.source_start.frames,
                timeline.storylines):
            parent_clip.connected_clips.append(connected)
            timeline.connected_clips.append(connected)

    def _parse_gap_connected_clips(self, gap_elem: ET.Element, gap_offset: int,
                                    gap_start: int, timeline: Timeline):
        """Parse connected clips attached to gap elements."""
        for connected in self._iter_connected_elements(
                gap_elem, f"gap@{gap_offset}", gap_offset, gap_start, timeline.storylines):
            timeline.connected_clips.append(connected)

    def _parse_one_connected_clip(self, elem: ET.Element, lane: int, parent_name: str,
                                   parent_offset: int = 0, parent_start: int = 0,
                                   timeline_frames: Optional[int] = None,
                                   parent_clock_offset: Optional[Fraction] = None,
                                   ) -> Optional[ConnectedClip]:
        """Parse a single connected clip element.

        ``timeline_frames`` and ``parent_clock_offset`` are given for an item
        of a connected storyline, whose own ``offset`` is on the storyline's
        clock: where it plays, and its offset restated on the parent's clock.
        """
        duration = self._tc(elem, 'duration')
        start = self._tc(elem, 'start')
        offset = self._tc(elem, 'offset')
        if parent_clock_offset is not None:
            offset = Timecode(
                frames=int(parent_clock_offset * rational_fps(self.frame_rate)),
                frame_rate=self.frame_rate)
        ref = elem.get('ref', '')
        media_path = self._media_path_for(elem)
        role = elem.get('audioRole', '') or elem.get('videoRole', '')
        text = ''
        if elem.tag == 'caption':
            text = ''.join(t.text or '' for t in elem.iter('text')).strip()
        name = self._name_for(elem, text or 'Untitled')
        timeline_start = Timecode(
            frames=(timeline_frames if timeline_frames is not None
                    else parent_offset + offset.frames - parent_start),
            frame_rate=self.frame_rate,
        )

        conform, transform, effects = self._picture_of(elem)
        connected = ConnectedClip(
            name=name, start=start, duration=duration,
            lane=lane, offset=offset, source_start=start,
            media_path=media_path, clip_type=elem.tag, role=role,
            ref_id=ref, parent_clip_name=parent_name,
            timeline_start=timeline_start, text=text,
            media=self._media_ref_for(elem), conform=conform,
            transform=transform, effects=effects, volume_db=self._volume_of(elem),
            enabled=elem.get('enabled', '1') != '0',
        )

        connected.markers.extend(
            self._collect_markers(elem, timeline_start.frames, start.frames))

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
