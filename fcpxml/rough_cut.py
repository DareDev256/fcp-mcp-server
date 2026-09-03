"""
Auto Rough Cut - AI-powered timeline assembly from source clips.

The flagship feature: select clips by keywords, set a target duration
and pacing style, and get a complete rough cut assembled automatically.
"""

import random
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import diversity
from .models import (
    MontageConfig,
    PacingConfig,
    PacingCurve,
    RoughCutResult,
    SegmentSpec,
    TimeValue,
)
from .writer import _create_asset_element, write_fcpxml


class RoughCutGenerator:
    """
    Generates rough cuts from source FCPXML files.

    Usage:
        generator = RoughCutGenerator("source.fcpxml")
        result = generator.generate(
            output_path="rough_cut.fcpxml",
            target_duration="00:03:00:00",
            pacing="medium",
            segments=[
                SegmentSpec(name="Intro", keywords=["intro"], duration_seconds=30),
                SegmentSpec(name="Interview", keywords=["interview", "talking"]),
                SegmentSpec(name="Outro", keywords=["outro"], duration_seconds=15),
            ]
        )
    """

    def __init__(self, source_fcpxml: str):
        """Load source FCPXML for clip selection."""
        self.source_path = Path(source_fcpxml)
        from .safe_xml import safe_parse
        self.tree = safe_parse(source_fcpxml)
        self.root = self.tree.getroot()
        self.fps = self._detect_fps()
        self._index_clips()
        self._index_resources()

    def _detect_fps(self) -> float:
        """Extract frame rate from format resource."""
        for fmt in self.root.findall('.//format'):
            frame_dur = fmt.get('frameDuration', '1/30s')
            if '/' in frame_dur:
                num, denom = frame_dur.replace('s', '').split('/')
                return int(denom) / int(num)
        return 30.0

    def _index_clips(self) -> None:
        """Index all clips with their metadata."""
        self.clips: List[Dict[str, Any]] = []

        for clip in self.root.findall('.//asset-clip'):
            clip_data = self._extract_clip_data(clip, 'asset-clip')
            if clip_data:
                self.clips.append(clip_data)

        for clip in self.root.findall('.//clip'):
            clip_data = self._extract_clip_data(clip, 'clip')
            if clip_data:
                self.clips.append(clip_data)

        for video in self.root.findall('.//video'):
            clip_data = self._extract_clip_data(video, 'video')
            if clip_data:
                self.clips.append(clip_data)

    def _extract_clip_data(self, elem: ET.Element, clip_type: str) -> Optional[Dict[str, Any]]:
        """Extract clip metadata into a dictionary."""
        name = elem.get('name', 'Untitled')
        duration_str = elem.get('duration', '0s')
        start_str = elem.get('start', '0s')
        ref = elem.get('ref', '')

        duration = TimeValue.from_timecode(duration_str, self.fps)

        # Skip very short clips
        if duration.to_seconds() < 0.1:
            return None

        # Extract keywords
        keywords = []
        for kw in elem.findall('keyword'):
            keywords.append(kw.get('value', ''))

        # Check if favorited
        is_favorite = elem.get('rating', '') == '1' or elem.get('isFavorite', '') == '1'
        is_rejected = elem.get('rating', '') == '-1' or elem.get('isRejected', '') == '1'

        return {
            'element': elem,
            'name': name,
            'type': clip_type,
            'ref': ref,
            'duration': duration,
            'start': TimeValue.from_timecode(start_str, self.fps),
            'keywords': keywords,
            'is_favorite': is_favorite,
            'is_rejected': is_rejected,
            'used': False,  # Track if already used in rough cut
        }

    def _index_resources(self) -> None:
        """Index all resources (assets, formats)."""
        self.resources = {}
        self.formats = {}

        for asset in self.root.findall('.//asset'):
            self.resources[asset.get('id', '')] = asset

        for fmt in self.root.findall('.//format'):
            self.formats[fmt.get('id', '')] = fmt

    def generate(
        self,
        output_path: str,
        target_duration: str = "00:03:00:00",
        pacing: str = "medium",
        segments: Optional[List[SegmentSpec]] = None,
        keywords: Optional[List[str]] = None,
        priority: str = "best",
        exclude_rejected: bool = True,
        favorites_only: bool = False,
        add_transitions: bool = False,
        transition_duration: str = "00:00:00:15",
        min_source_separation: int = 0,
    ) -> RoughCutResult:
        """
        Generate a rough cut.

        Args:
            output_path: Where to save the rough cut FCPXML
            target_duration: Target timeline length (timecode or "3m30s")
            pacing: "slow", "medium", "fast", or "dynamic"
            segments: Optional segment breakdown with keywords per section
            keywords: Simple mode - just filter by these keywords
            priority: How to select clips - "best", "favorites", "longest", "shortest", "random"
            exclude_rejected: Skip rejected clips
            favorites_only: Only use favorited clips
            add_transitions: Add cross-dissolves between clips
            transition_duration: Length of transitions
            min_source_separation: Other shots required between two uses of
                the same source clip; 0 = no constraint

        Returns:
            RoughCutResult with output details
        """
        target_time = self._parse_duration(target_duration)
        pacing_config = PacingConfig(pacing=pacing)

        # Filter available clips
        available_clips = self._filter_clips(
            keywords=keywords,
            exclude_rejected=exclude_rejected,
            favorites_only=favorites_only
        )

        if not available_clips:
            raise ValueError("No clips match the filter criteria")

        # Build the sequence
        if segments:
            selected_clips = self._select_clips_by_segments(
                available_clips, segments, target_time, pacing_config
            )
        else:
            selected_clips = self._select_clips_simple(
                available_clips, target_time, pacing_config, priority
            )

        if min_source_separation > 0:
            selected_clips = diversity.apply(
                [{**c, "source": c.get("ref")} for c in selected_clips],
                min_separation=min_source_separation,
            )
        diversity_score = diversity.score([{"source": c.get("ref")} for c in selected_clips])

        # Generate output FCPXML
        actual_duration = self._build_output(
            selected_clips, output_path, add_transitions, transition_duration
        )

        return RoughCutResult(
            output_path=output_path,
            clips_used=len(selected_clips),
            clips_available=len(available_clips),
            target_duration=target_time.to_seconds(),
            actual_duration=actual_duration,
            segments=len(segments) if segments else 1,
            average_clip_duration=actual_duration / len(selected_clips) if selected_clips else 0,
            diversity_score=diversity_score,
        )

    def _parse_duration(self, duration: str) -> TimeValue:
        """Parse various duration formats."""
        # Handle shorthand like "3m30s" or "2m"
        if 'm' in duration and ':' not in duration:
            parts = duration.lower().replace('s', '').split('m')
            minutes = int(parts[0]) if parts[0] else 0
            seconds = float(parts[1]) if len(parts) > 1 and parts[1] else 0
            total_seconds = minutes * 60 + seconds
            return TimeValue.from_seconds(total_seconds, self.fps)

        return TimeValue.from_timecode(duration, self.fps)

    def _filter_clips(
        self,
        keywords: Optional[List[str]] = None,
        exclude_rejected: bool = True,
        favorites_only: bool = False
    ) -> List[Dict[str, Any]]:
        """Filter clips by criteria."""
        result = []

        for clip in self.clips:
            # Skip rejected
            if exclude_rejected and clip['is_rejected']:
                continue

            # Filter to favorites only
            if favorites_only and not clip['is_favorite']:
                continue

            # Filter by keywords
            if keywords:
                clip_kws = set(k.lower() for k in clip['keywords'])
                filter_kws = set(k.lower() for k in keywords)
                if not clip_kws & filter_kws:
                    continue

            result.append(clip)

        return result

    def _select_clips_simple(
        self,
        clips: List[Dict[str, Any]],
        target_duration: TimeValue,
        pacing: PacingConfig,
        priority: str
    ) -> List[Dict[str, Any]]:
        """Select clips to fill target duration."""
        selected = []
        current_duration = TimeValue.zero()
        target_seconds = target_duration.to_seconds()
        min_clip, max_clip = pacing.get_duration_range()

        # Sort clips by priority
        if priority == 'favorites':
            clips = sorted(clips, key=lambda c: (not c['is_favorite'], -c['duration'].to_seconds()))
        elif priority == 'longest':
            clips = sorted(clips, key=lambda c: -c['duration'].to_seconds())
        elif priority == 'shortest':
            clips = sorted(clips, key=lambda c: c['duration'].to_seconds())
        elif priority == 'random':
            random.shuffle(clips)
        else:  # 'best' - mix of favorites first, then by duration
            clips = sorted(clips, key=lambda c: (not c['is_favorite'], -c['duration'].to_seconds()))

        for clip in clips:
            if current_duration.to_seconds() >= target_seconds:
                break

            remaining = target_seconds - current_duration.to_seconds()
            clip_dur = clip['duration'].to_seconds()

            # Calculate usable duration for this clip
            if clip_dur > max_clip:
                use_duration = min(max_clip, remaining)
            elif clip_dur < min_clip:
                use_duration = clip_dur  # Use short clips as-is
            else:
                use_duration = min(clip_dur, remaining)

            if use_duration <= 0:
                continue

            # Create selection with in/out points
            selection = {
                **clip,
                'use_duration': TimeValue.from_seconds(use_duration, self.fps),
                'in_point': clip['start'],
                'out_point': clip['start'] + TimeValue.from_seconds(use_duration, self.fps),
            }
            selected.append(selection)
            current_duration = current_duration + TimeValue.from_seconds(use_duration, self.fps)

        return selected

    def _select_clips_by_segments(
        self,
        clips: List[Dict[str, Any]],
        segments: List[SegmentSpec],
        target_duration: TimeValue,
        pacing: PacingConfig
    ) -> List[Dict[str, Any]]:
        """Select clips organized by segment structure."""
        selected = []

        # Calculate duration per segment
        total_specified = sum(s.duration_seconds for s in segments if s.duration_seconds > 0)
        unspecified_count = sum(1 for s in segments if s.duration_seconds <= 0)

        if unspecified_count > 0:
            remaining_duration = target_duration.to_seconds() - total_specified
            per_segment = max(0, remaining_duration / unspecified_count)
        else:
            per_segment = 0

        for segment in segments:
            seg_duration = segment.duration_seconds if segment.duration_seconds > 0 else per_segment

            # Filter clips for this segment
            if segment.keywords:
                seg_clips = [c for c in clips if
                    set(k.lower() for k in c['keywords']) &
                    set(k.lower() for k in segment.keywords)]
            else:
                seg_clips = clips.copy()

            # Remove already used clips
            seg_clips = [c for c in seg_clips if not c.get('used_in_rough', False)]

            # Select clips for segment
            seg_target = TimeValue.from_seconds(seg_duration, self.fps)
            seg_selected = self._select_clips_simple(
                seg_clips, seg_target, pacing, segment.priority
            )

            # Mark as used on originals so they're excluded from later segments
            used_names = {sel['name'] for sel in seg_selected}
            for c in clips:
                if c['name'] in used_names:
                    c['used_in_rough'] = True
            for sel in seg_selected:
                sel['segment'] = segment.name

            selected.extend(seg_selected)

        return selected

    def _build_output(
        self,
        clips: List[Dict[str, Any]],
        output_path: str,
        add_transitions: bool,
        transition_duration: str
    ) -> float:
        """Build the output FCPXML."""
        # Create new FCPXML structure
        root = ET.Element('fcpxml', version='1.13')
        resources = ET.SubElement(root, 'resources')

        # Copy relevant resources
        format_id = None
        for fmt_id, fmt in self.formats.items():
            resources.append(fmt)
            format_id = fmt_id
            break

        # Copy needed assets — use media-rep form for new assets
        used_refs = set(c.get('ref') for c in clips if c.get('ref'))
        for ref in used_refs:
            if ref in self.resources:
                orig = self.resources[ref]
                # Extract src from either attribute or media-rep child
                src = orig.get('src', '')
                if not src:
                    mr = orig.find('media-rep')
                    if mr is not None:
                        src = mr.get('src', '')
                _create_asset_element(
                    resources,
                    asset_id=orig.get('id', ref),
                    name=orig.get('name', ''),
                    src=src,
                    duration=orig.get('duration', '0s'),
                    start=orig.get('start', '0s'),
                    has_video=orig.get('hasVideo', '1'),
                    has_audio=orig.get('hasAudio', '1'),
                    uid=orig.get('uid'),
                )

        # Create library structure
        library = ET.SubElement(root, 'library',
            location="file:///Users/editor/Movies/RoughCut.fcpbundle/")
        event = ET.SubElement(library, 'event',
            name="Rough Cut", uid=str(uuid.uuid4()).upper())
        project = ET.SubElement(event, 'project',
            name="Rough Cut", uid=str(uuid.uuid4()).upper(),
            modDate=datetime.now().strftime("%Y-%m-%d %H:%M:%S -0500"))

        # Calculate total duration
        total_duration = TimeValue.zero()
        for clip in clips:
            total_duration = total_duration + clip['use_duration']

        # Create sequence
        sequence = ET.SubElement(project, 'sequence',
            format=format_id or "r1",
            duration=total_duration.to_fcpxml(),
            tcStart="0s", tcFormat="NDF",
            audioLayout="stereo", audioRate="48k")

        spine = ET.SubElement(sequence, 'spine')

        # Add clips to spine
        current_offset = TimeValue.zero()
        trans_dur = TimeValue.from_timecode(transition_duration, self.fps) if add_transitions else None

        for i, clip in enumerate(clips):
            ET.SubElement(spine, 'asset-clip',
                ref=clip.get('ref', 'r1'),
                offset=current_offset.to_fcpxml(),
                name=clip['name'],
                start=clip['in_point'].to_fcpxml(),
                duration=clip['use_duration'].to_fcpxml(),
                format=format_id or "r1",
                tcFormat="NDF")

            # Add transition before clip (except first)
            if add_transitions and i > 0 and trans_dur:
                half_trans = trans_dur * 0.5
                trans_offset = current_offset - half_trans

                transition = ET.Element('transition',
                    name="Cross Dissolve",
                    offset=trans_offset.to_fcpxml(),
                    duration=trans_dur.to_fcpxml())
                ET.SubElement(transition, 'filter-video',
                    name="Cross Dissolve")
                spine.insert(-1, transition)

            current_offset = current_offset + clip['use_duration']

        # Write output
        write_fcpxml(root, output_path)

        return total_duration.to_seconds()

    # ========================================================================
    # MONTAGE GENERATION (v0.3.0)
    # ========================================================================

    def generate_montage(
        self,
        output_path: str,
        target_duration: str,
        pacing_curve: str = "accelerating",
        start_duration: float = 2.0,
        end_duration: float = 0.5,
        keywords: Optional[List[str]] = None,
        exclude_rejected: bool = True,
        add_transitions: bool = False
    ) -> Dict[str, Any]:
        """
        Generate a rapid-fire montage with dynamic pacing curves.

        Creates montages where clip duration varies over time:
        - accelerating: Starts slow (2s), ends fast (0.5s) - builds energy
        - decelerating: Starts fast, ends slow - winds down
        - pyramid: Slow → fast → slow - dramatic arc
        - constant: Same duration throughout

        Args:
            output_path: Where to save the montage FCPXML
            target_duration: Total montage length (e.g., "30s", "00:00:30:00")
            pacing_curve: "accelerating", "decelerating", "pyramid", or "constant"
            start_duration: Clip duration at start (seconds)
            end_duration: Clip duration at end (seconds)
            keywords: Filter clips by keywords
            exclude_rejected: Skip rejected clips
            add_transitions: Add quick dissolves between clips

        Returns:
            Dict with output_path, clips_used, actual_duration, pacing_curve
        """
        # Parse pacing curve
        curve_map = {
            'accelerating': PacingCurve.ACCELERATING,
            'decelerating': PacingCurve.DECELERATING,
            'pyramid': PacingCurve.PYRAMID,
            'constant': PacingCurve.CONSTANT
        }
        curve = curve_map.get(pacing_curve, PacingCurve.ACCELERATING)

        config = MontageConfig(
            target_duration=self._parse_duration(target_duration).to_seconds(),
            pacing_curve=curve,
            start_duration=start_duration,
            end_duration=end_duration,
            min_duration=0.2,
            max_duration=max(start_duration, end_duration) + 1.0
        )

        # Filter available clips
        available_clips = self._filter_clips(
            keywords=keywords,
            exclude_rejected=exclude_rejected
        )

        if not available_clips:
            raise ValueError("No clips match the filter criteria")

        # Select clips with pacing curve
        selected_clips = self._select_clips_for_montage(available_clips, config)

        # Build output
        actual_duration = self._build_output(
            selected_clips, output_path, add_transitions, "00:00:00:06"
        )

        return {
            'output_path': output_path,
            'clips_used': len(selected_clips),
            'clips_available': len(available_clips),
            'target_duration': config.target_duration,
            'actual_duration': actual_duration,
            'pacing_curve': pacing_curve,
            'start_clip_duration': selected_clips[0]['use_duration'].to_seconds() if selected_clips else 0,
            'end_clip_duration': selected_clips[-1]['use_duration'].to_seconds() if selected_clips else 0
        }

    def _select_clips_for_montage(
        self,
        clips: List[Dict[str, Any]],
        config: MontageConfig
    ) -> List[Dict[str, Any]]:
        """Select clips with dynamic pacing based on montage config."""
        selected = []
        current_duration = 0.0
        target = config.target_duration
        clip_index = 0

        # Shuffle clips for variety
        available = clips.copy()
        random.shuffle(available)

        while current_duration < target and clip_index < len(available):
            # Calculate position in montage (0.0 to 1.0)
            position = current_duration / target if target > 0 else 0

            # Get target clip duration for this position
            target_clip_duration = config.get_duration_at_position(position)

            # Find best matching clip
            clip = available[clip_index]
            clip_dur = clip['duration'].to_seconds()

            # Determine actual duration to use
            remaining = target - current_duration
            use_duration = min(target_clip_duration, clip_dur, remaining)
            use_duration = max(use_duration, config.min_duration)

            if use_duration <= 0:
                clip_index += 1
                continue

            # Create selection
            selection = {
                **clip,
                'use_duration': TimeValue.from_seconds(use_duration, self.fps),
                'in_point': clip['start'],
                'out_point': clip['start'] + TimeValue.from_seconds(use_duration, self.fps),
                'montage_position': position
            }
            selected.append(selection)
            current_duration += use_duration
            clip_index += 1

        return selected

    # ========================================================================
    # A/B ROLL GENERATION (v0.3.0)
    # ========================================================================

    def generate_ab_roll(
        self,
        output_path: str,
        target_duration: str,
        a_keywords: List[str],
        b_keywords: List[str],
        a_duration: str = "5s",
        b_duration: str = "3s",
        start_with: str = "a",
        exclude_rejected: bool = True,
        add_transitions: bool = True
    ) -> Dict[str, Any]:
        """
        Generate classic documentary-style A/B roll edit.

        Alternates between A-roll (main content, usually interviews) and
        B-roll (cutaway footage, usually visuals).

        Args:
            output_path: Where to save the FCPXML
            target_duration: Total duration (e.g., "3m", "00:03:00:00")
            a_keywords: Keywords for A-roll clips (e.g., ["interview", "talking"])
            b_keywords: Keywords for B-roll clips (e.g., ["broll", "cutaway"])
            a_duration: How long each A-roll segment should be
            b_duration: How long each B-roll cutaway should be
            start_with: Start with "a" or "b" roll
            exclude_rejected: Skip rejected clips
            add_transitions: Add cross-dissolves between clips

        Returns:
            Dict with output details including a_segments and b_segments counts
        """
        target_time = self._parse_duration(target_duration)
        a_dur = self._parse_duration(a_duration)
        b_dur = self._parse_duration(b_duration)

        # Filter A and B clips
        a_clips = self._filter_clips(keywords=a_keywords, exclude_rejected=exclude_rejected)
        b_clips = self._filter_clips(keywords=b_keywords, exclude_rejected=exclude_rejected)

        if not a_clips:
            raise ValueError(f"No A-roll clips found with keywords: {a_keywords}")
        if not b_clips:
            raise ValueError(f"No B-roll clips found with keywords: {b_keywords}")

        # Build alternating sequence
        selected_clips = self._build_ab_sequence(
            a_clips, b_clips, target_time, a_dur, b_dur, start_with
        )

        # Build output
        actual_duration = self._build_output(
            selected_clips, output_path, add_transitions, "00:00:00:12"
        )

        a_count = sum(1 for c in selected_clips if c.get('roll_type') == 'A')
        b_count = sum(1 for c in selected_clips if c.get('roll_type') == 'B')

        return {
            'output_path': output_path,
            'clips_used': len(selected_clips),
            'a_clips_available': len(a_clips),
            'b_clips_available': len(b_clips),
            'a_segments': a_count,
            'b_segments': b_count,
            'target_duration': target_time.to_seconds(),
            'actual_duration': actual_duration,
            'a_duration_setting': a_duration,
            'b_duration_setting': b_duration
        }

    def _build_ab_sequence(
        self,
        a_clips: List[Dict[str, Any]],
        b_clips: List[Dict[str, Any]],
        target_duration: TimeValue,
        a_dur: TimeValue,
        b_dur: TimeValue,
        start_with: str
    ) -> List[Dict[str, Any]]:
        """Build alternating A/B sequence."""
        selected = []
        current_duration = TimeValue.zero()
        target_seconds = target_duration.to_seconds()

        # Track which clips we've used
        a_index = 0
        b_index = 0
        current_roll = start_with.lower()

        while current_duration.to_seconds() < target_seconds:
            remaining = target_seconds - current_duration.to_seconds()

            if current_roll == 'a':
                if a_index >= len(a_clips):
                    a_index = 0  # Loop if needed

                clip = a_clips[a_index]
                target_dur = min(a_dur.to_seconds(), remaining)
                clip_dur = clip['duration'].to_seconds()
                use_dur = min(target_dur, clip_dur)

                selection = {
                    **clip,
                    'use_duration': TimeValue.from_seconds(use_dur, self.fps),
                    'in_point': clip['start'],
                    'out_point': clip['start'] + TimeValue.from_seconds(use_dur, self.fps),
                    'roll_type': 'A'
                }
                selected.append(selection)
                current_duration = current_duration + TimeValue.from_seconds(use_dur, self.fps)
                a_index += 1
                current_roll = 'b'

            else:  # B-roll
                if b_index >= len(b_clips):
                    b_index = 0  # Loop if needed

                clip = b_clips[b_index]
                target_dur = min(b_dur.to_seconds(), remaining)
                clip_dur = clip['duration'].to_seconds()
                use_dur = min(target_dur, clip_dur)

                selection = {
                    **clip,
                    'use_duration': TimeValue.from_seconds(use_dur, self.fps),
                    'in_point': clip['start'],
                    'out_point': clip['start'] + TimeValue.from_seconds(use_dur, self.fps),
                    'roll_type': 'B'
                }
                selected.append(selection)
                current_duration = current_duration + TimeValue.from_seconds(use_dur, self.fps)
                b_index += 1
                current_roll = 'a'

            # Safety check to prevent infinite loop
            if len(selected) > 1000:
                break

        return selected


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def generate_rough_cut(
    source_fcpxml: str,
    output_path: str,
    target_duration: str = "3m",
    pacing: str = "medium",
    keywords: Optional[List[str]] = None,
    priority: str = "best"
) -> RoughCutResult:
    """
    One-liner rough cut generation.

    Example:
        result = generate_rough_cut(
            "source_clips.fcpxml",
            "rough_cut.fcpxml",
            target_duration="3m",
            pacing="fast",
            keywords=["broll", "action"]
        )
    """
    generator = RoughCutGenerator(source_fcpxml)
    return generator.generate(
        output_path=output_path,
        target_duration=target_duration,
        pacing=pacing,
        keywords=keywords,
        priority=priority
    )


def generate_segmented_rough_cut(
    source_fcpxml: str,
    output_path: str,
    segments: List[Dict[str, Any]],
    pacing: str = "medium",
    add_transitions: bool = True
) -> RoughCutResult:
    """
    Generate rough cut with defined segments.

    Example:
        result = generate_segmented_rough_cut(
            "interview.fcpxml",
            "rough_cut.fcpxml",
            segments=[
                {"name": "Intro", "keywords": ["intro"], "duration": 30},
                {"name": "Main", "keywords": ["interview"], "duration": 180},
                {"name": "Outro", "keywords": ["outro"], "duration": 20},
            ],
            pacing="medium",
            add_transitions=True
        )
    """
    segment_specs = []
    for seg in segments:
        segment_specs.append(SegmentSpec(
            name=seg.get('name', 'Segment'),
            keywords=seg.get('keywords', []),
            duration_seconds=seg.get('duration', 0),
            priority=seg.get('priority', 'best')
        ))

    # Calculate total duration
    total = sum(seg.get('duration', 0) for seg in segments)

    generator = RoughCutGenerator(source_fcpxml)
    return generator.generate(
        output_path=output_path,
        target_duration=f"{total}s",
        pacing=pacing,
        segments=segment_specs,
        add_transitions=add_transitions
    )
