"""Render a Timeline as a self-contained HTML preview.

Editing FCPXML is otherwise blind: you call a tool, you get text back, and you
cannot see the cut until you import into Final Cut Pro. This is the visual
check in between. No external assets, no scripts, so it renders anywhere.

Note on model shapes: ``Clip`` (the primary-storyline clip type returned by
``Timeline.clips``) has no ``offset`` or ``lane`` field. Its timeline position
is ``start`` (the parser assigns the FCPXML ``offset`` attribute to
``Clip.start`` — the spine is contiguous, clips do not overlap), so
``clip.start.seconds`` is the left offset for spine clips.

``ConnectedClip`` (B-roll, titles, connected audio hanging off the spine via
``Timeline.connected_clips``) DOES have real ``lane`` and ``offset`` fields —
those are rendered as separate lane rows: positive lanes above the spine,
negative lanes below, per FCPXML's magnetic-timeline semantics
(``ConnectedClip`` docstring in ``fcpxml/models.py``).

``Marker`` has no ``value`` field — its label is ``name``.
"""

from html import escape

# Distinct hues per lane so connected clips (B-roll, titles, connected audio)
# read as separate layers from each other and from the spine.
_LANE_COLORS = [
    "#8b5cf6", "#ec4899", "#f59e0b", "#10b981", "#06b6d4", "#eab308",
]

# Primary track (spine) clips alternate between these two shades so adjacent
# clips still read as separate blocks.
_CLIP_COLORS = ["#3b82f6", "#2563eb"]


def _clip_color(index: int) -> str:
    return _CLIP_COLORS[index % len(_CLIP_COLORS)]


def _lane_color(lane: int) -> str:
    return _LANE_COLORS[abs(int(lane or 0)) % len(_LANE_COLORS)]


def _left_width(offset_seconds: float, duration_seconds: float, total: float) -> tuple[float, float]:
    """Compute clamped left/width percentages for a block on the timeline ruler.

    Handles the degenerate cases that would otherwise render nonsense:
    a negative/absurd offset (from a bad or synthetic timeline) clamps to
    the left edge, a clip longer than the timeline clamps to the remaining
    space rather than overflowing the ruler, and a near-zero timeline
    duration (guarded to 0.001s upstream) no longer produces four-digit
    percentages because width is always capped against what's left of the
    ruler after ``left``.
    """
    left = max(min((offset_seconds / total) * 100, 100.0), 0.0)
    raw_width = (duration_seconds / total) * 100
    width = max(min(raw_width, 100.0 - left), 0.4)
    return left, width


def _render_clip_block(name: str, left: float, width: float, color: str, seconds: float, css_class: str) -> str:
    return (
        f'<div class="{css_class}" style="left:{left:.3f}%;width:{width:.3f}%;'
        f'background:{color}" '
        f'title="{escape(str(name))} ({seconds:.2f}s)">'
        f'<span>{escape(str(name))}</span></div>'
    )


def render_timeline_html(timeline) -> str:
    """Return a standalone HTML document visualising one timeline."""
    total = max(float(timeline.duration.seconds or 0), 0.001)

    spine_rows = []
    for i, clip in enumerate(timeline.clips):
        seconds = float(clip.duration.seconds or 0)
        offset = float(clip.start.seconds or 0)
        left, width = _left_width(offset, seconds, total)
        spine_rows.append(_render_clip_block(clip.name, left, width, _clip_color(i), seconds, "clip"))

    # Group connected clips (B-roll, titles, connected audio) by lane.
    # Positive lanes render above the spine, negative lanes below — matching
    # FCPXML's magnetic-timeline semantics (positive = video overlay,
    # negative = audio). Highest positive lane sits topmost; most negative
    # lane sits bottommost.
    connected = list(getattr(timeline, "connected_clips", []) or [])
    lanes: dict[int, list] = {}
    for cc in connected:
        lanes.setdefault(int(cc.lane or 0), []).append(cc)

    def _lane_row_html(lane: int) -> str:
        blocks = []
        for cc in lanes[lane]:
            seconds = float(cc.duration.seconds or 0)
            cc_offset = cc.offset.seconds if cc.offset else 0.0
            left, width = _left_width(float(cc_offset or 0), seconds, total)
            blocks.append(_render_clip_block(cc.name, left, width, _lane_color(lane), seconds, "lane-clip"))
        return (
            f'<div class="lane-label">Lane {lane}</div>'
            f'<div class="track lane-track">{"".join(blocks)}</div>'
        )

    positive_lanes = sorted((lane for lane in lanes if lane > 0), reverse=True)
    negative_lanes = sorted(lane for lane in lanes if lane < 0)
    zero_lanes = sorted(lane for lane in lanes if lane == 0)

    above_html = "".join(_lane_row_html(lane) for lane in positive_lanes)
    below_html = "".join(_lane_row_html(lane) for lane in negative_lanes + zero_lanes)

    marks = []
    for marker in getattr(timeline, "markers", []):
        at = float(marker.start.seconds or 0)
        left = max(min((at / total) * 100, 100.0), 0.0)
        marks.append(
            f'<div class="marker" style="left:{left:.3f}%" '
            f'title="{escape(str(marker.name))}"></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{escape(str(timeline.name))}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px ui-sans-serif, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #0b0b0f; color: #e7e7ea; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .meta {{ color: #9a9aa4; font-size: 13px; margin-bottom: 20px; }}
  .lane-label {{ color: #9a9aa4; font-size: 11px; margin: 10px 0 4px; }}
  .track {{ position: relative; height: 56px; background: #16161c;
            border-radius: 6px; overflow: hidden; }}
  .lane-track {{ height: 36px; }}
  .clip, .lane-clip {{ position: absolute; top: 0; bottom: 0; border-right: 1px solid #0b0b0f;
           display: flex; align-items: center; overflow: hidden; }}
  .clip span, .lane-clip span {{ padding: 0 6px; font-size: 11px; color: #fff; white-space: nowrap;
                text-shadow: 0 1px 2px rgba(0,0,0,.6); }}
  .markers {{ position: relative; height: 14px; margin-top: 6px; }}
  .marker {{ position: absolute; width: 2px; height: 14px; background: #f43f5e; }}
</style>
</head>
<body>
<h1>{escape(str(timeline.name))}</h1>
<div class="meta">
  {timeline.total_clips} clips &#183; {len(connected)} connected &#183; {total:.2f}s &#183;
  {timeline.width}&#215;{timeline.height} @ {timeline.frame_rate}fps
</div>
{above_html}
<div class="track">{"".join(spine_rows)}</div>
{below_html}
<div class="markers">{"".join(marks)}</div>
</body>
</html>"""
