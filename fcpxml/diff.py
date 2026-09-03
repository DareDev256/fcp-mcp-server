"""Timeline comparison engine for FCPXML files.

Compares two FCPXML timelines and reports differences in clips,
markers, transitions, and format settings.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .parser import FCPXMLParser


@dataclass
class ClipDiff:
    """Difference record for a single clip."""
    action: str  # "added", "removed", "moved", "trimmed", "unchanged"
    clip_name: str
    details: str = ""
    old_start: Optional[float] = None
    new_start: Optional[float] = None
    old_duration: Optional[float] = None
    new_duration: Optional[float] = None


@dataclass
class MarkerDiff:
    """Difference record for a marker."""
    action: str  # "added", "removed", "moved"
    marker_name: str
    details: str = ""
    old_position: Optional[float] = None
    new_position: Optional[float] = None


@dataclass
class TimelineDiff:
    """Complete diff between two timelines."""
    timeline_a_name: str
    timeline_b_name: str
    clip_diffs: List[ClipDiff] = field(default_factory=list)
    marker_diffs: List[MarkerDiff] = field(default_factory=list)
    transition_diffs: List[str] = field(default_factory=list)
    format_changes: List[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        changes = [d for d in self.clip_diffs if d.action != "unchanged"]
        return len(changes) + len(self.marker_diffs) + len(self.transition_diffs) + len(self.format_changes)

    @property
    def has_changes(self) -> bool:
        return self.total_changes > 0


def format_diff(diff: TimelineDiff) -> str:
    """Render a TimelineDiff as operator-readable markdown.

    Lives here rather than in a handler because two callers need it: the
    diff_timelines tool and watch_pull, which diffs each detected export
    against the last one seen.
    """
    if not diff.has_changes:
        return (
            f"# Timeline Diff: No Changes\n\n"
            f"**{diff.timeline_a_name}** vs **{diff.timeline_b_name}** are identical."
        )

    result = (
        f"# Timeline Diff\n\n"
        f"**Baseline**: {diff.timeline_a_name}\n"
        f"**Comparison**: {diff.timeline_b_name}\n"
        f"**Total changes**: {diff.total_changes}\n\n"
    )

    if diff.format_changes:
        result += "## Format Changes\n\n"
        for change in diff.format_changes:
            result += f"- {change}\n"
        result += "\n"

    clip_changes = [d for d in diff.clip_diffs if d.action != "unchanged"]
    if clip_changes:
        result += "## Clip Changes\n\n| Action | Clip | Details |\n|--------|------|--------|\n"
        for d in clip_changes:
            result += f"| {d.action.upper()} | {d.clip_name} | {d.details} |\n"
        result += "\n"

    if diff.marker_diffs:
        result += "## Marker Changes\n\n| Action | Marker | Details |\n|--------|--------|--------|\n"
        for d in diff.marker_diffs:
            result += f"| {d.action.upper()} | {d.marker_name} | {d.details} |\n"
        result += "\n"

    if diff.transition_diffs:
        result += "## Transition Changes\n\n"
        for change in diff.transition_diffs:
            result += f"- {change}\n"

    return result


def _clip_identity(clip) -> Tuple[str, float]:
    """Build identity key for a clip: (name, source_start rounded to 0.01s)."""
    source_start = round(clip.source_start.seconds, 2) if clip.source_start else 0.0
    return (clip.name, source_start)


def compare_timelines(filepath_a: str, filepath_b: str) -> TimelineDiff:
    """Compare two FCPXML files and return structured diff.

    Uses clip identity (name + source in-point) to match clips between
    timelines, then detects moved, trimmed, added, and removed clips.

    Args:
        filepath_a: Path to baseline FCPXML
        filepath_b: Path to comparison FCPXML

    Returns:
        TimelineDiff with all detected changes
    """
    parser_a = FCPXMLParser()
    parser_b = FCPXMLParser()
    project_a = parser_a.parse_file(filepath_a)
    project_b = parser_b.parse_file(filepath_b)

    tl_a = project_a.primary_timeline
    tl_b = project_b.primary_timeline

    if tl_a is None or tl_b is None:
        return TimelineDiff(
            timeline_a_name=tl_a.name if tl_a else "No timeline",
            timeline_b_name=tl_b.name if tl_b else "No timeline",
        )

    diff = TimelineDiff(
        timeline_a_name=tl_a.name,
        timeline_b_name=tl_b.name,
    )

    # Compare clips
    _compare_clips(tl_a.clips, tl_b.clips, diff)

    # Compare markers
    _compare_markers(tl_a, tl_b, diff)

    # Compare transitions
    _compare_transitions(tl_a.transitions, tl_b.transitions, diff)

    # Compare format
    _compare_format(tl_a, tl_b, diff)

    return diff


def _compare_clips(clips_a, clips_b, diff: TimelineDiff):
    """Compare clip sequences between two timelines."""
    # Build identity maps
    map_a: Dict[Tuple[str, float], list] = {}
    for clip in clips_a:
        key = _clip_identity(clip)
        map_a.setdefault(key, []).append(clip)

    map_b: Dict[Tuple[str, float], list] = {}
    for clip in clips_b:
        key = _clip_identity(clip)
        map_b.setdefault(key, []).append(clip)

    all_keys = set(map_a.keys()) | set(map_b.keys())

    for key in sorted(all_keys, key=lambda k: k[1]):
        a_clips = map_a.get(key, [])
        b_clips = map_b.get(key, [])

        if not a_clips and b_clips:
            for clip in b_clips:
                diff.clip_diffs.append(ClipDiff(
                    action="added",
                    clip_name=clip.name,
                    details=f"Added at {clip.start.to_smpte()}",
                    new_start=clip.start.seconds,
                    new_duration=clip.duration_seconds,
                ))
        elif a_clips and not b_clips:
            for clip in a_clips:
                diff.clip_diffs.append(ClipDiff(
                    action="removed",
                    clip_name=clip.name,
                    details=f"Removed from {clip.start.to_smpte()}",
                    old_start=clip.start.seconds,
                    old_duration=clip.duration_seconds,
                ))
        else:
            # Match clips pairwise
            for i in range(max(len(a_clips), len(b_clips))):
                if i >= len(a_clips):
                    diff.clip_diffs.append(ClipDiff(
                        action="added", clip_name=b_clips[i].name,
                        new_start=b_clips[i].start.seconds,
                        new_duration=b_clips[i].duration_seconds,
                    ))
                elif i >= len(b_clips):
                    diff.clip_diffs.append(ClipDiff(
                        action="removed", clip_name=a_clips[i].name,
                        old_start=a_clips[i].start.seconds,
                        old_duration=a_clips[i].duration_seconds,
                    ))
                else:
                    a, b = a_clips[i], b_clips[i]
                    moved = abs(a.start.seconds - b.start.seconds) > 0.04
                    trimmed = abs(a.duration_seconds - b.duration_seconds) > 0.04

                    if moved and trimmed:
                        diff.clip_diffs.append(ClipDiff(
                            action="moved",
                            clip_name=a.name,
                            details=(
                                f"Moved {a.start.to_smpte()} -> {b.start.to_smpte()}, "
                                f"trimmed {a.duration_seconds:.2f}s -> {b.duration_seconds:.2f}s"
                            ),
                            old_start=a.start.seconds,
                            new_start=b.start.seconds,
                            old_duration=a.duration_seconds,
                            new_duration=b.duration_seconds,
                        ))
                    elif moved:
                        diff.clip_diffs.append(ClipDiff(
                            action="moved",
                            clip_name=a.name,
                            details=f"Moved {a.start.to_smpte()} -> {b.start.to_smpte()}",
                            old_start=a.start.seconds,
                            new_start=b.start.seconds,
                            old_duration=a.duration_seconds,
                            new_duration=b.duration_seconds,
                        ))
                    elif trimmed:
                        diff.clip_diffs.append(ClipDiff(
                            action="trimmed",
                            clip_name=a.name,
                            details=f"Duration {a.duration_seconds:.2f}s -> {b.duration_seconds:.2f}s",
                            old_start=a.start.seconds,
                            new_start=b.start.seconds,
                            old_duration=a.duration_seconds,
                            new_duration=b.duration_seconds,
                        ))


def _compare_markers(tl_a, tl_b, diff: TimelineDiff):
    """Compare markers between two timelines."""
    # Collect all markers including clip-level markers
    markers_a = {}
    for m in tl_a.markers:
        markers_a[m.name] = m.position.seconds
    for clip in tl_a.clips:
        for m in clip.markers:
            markers_a[m.name] = m.position.seconds

    markers_b = {}
    for m in tl_b.markers:
        markers_b[m.name] = m.position.seconds
    for clip in tl_b.clips:
        for m in clip.markers:
            markers_b[m.name] = m.position.seconds

    all_names = set(markers_a.keys()) | set(markers_b.keys())

    for name in sorted(all_names):
        if name in markers_a and name not in markers_b:
            diff.marker_diffs.append(MarkerDiff(
                action="removed", marker_name=name,
                old_position=markers_a[name],
            ))
        elif name not in markers_a and name in markers_b:
            diff.marker_diffs.append(MarkerDiff(
                action="added", marker_name=name,
                new_position=markers_b[name],
            ))
        elif abs(markers_a[name] - markers_b[name]) > 1.0:
            diff.marker_diffs.append(MarkerDiff(
                action="moved", marker_name=name,
                details=f"Position {markers_a[name]:.2f}s -> {markers_b[name]:.2f}s",
                old_position=markers_a[name],
                new_position=markers_b[name],
            ))


def _compare_transitions(trans_a, trans_b, diff: TimelineDiff):
    """Compare transitions between timelines."""
    count_a = len(trans_a)
    count_b = len(trans_b)
    if count_a != count_b:
        diff.transition_diffs.append(
            f"Transition count changed: {count_a} -> {count_b}"
        )

    for i in range(min(count_a, count_b)):
        a, b = trans_a[i], trans_b[i]
        if a.name != b.name:
            diff.transition_diffs.append(
                f"Transition {i + 1}: {a.name} -> {b.name}"
            )
        if abs(a.duration.seconds - b.duration.seconds) > 0.04:
            diff.transition_diffs.append(
                f"Transition {i + 1} duration: {a.duration.seconds:.2f}s -> {b.duration.seconds:.2f}s"
            )


def _compare_format(tl_a, tl_b, diff: TimelineDiff):
    """Compare format settings (resolution, frame rate)."""
    if tl_a.width != tl_b.width or tl_a.height != tl_b.height:
        diff.format_changes.append(
            f"Resolution: {tl_a.width}x{tl_a.height} -> {tl_b.width}x{tl_b.height}"
        )
    if abs(tl_a.frame_rate - tl_b.frame_rate) > 0.01:
        diff.format_changes.append(
            f"Frame rate: {tl_a.frame_rate:.3f} -> {tl_b.frame_rate:.3f}"
        )
