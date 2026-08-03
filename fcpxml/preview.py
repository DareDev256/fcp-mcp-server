"""Render a Timeline as a self-contained HTML preview.

Editing FCPXML is otherwise blind: you call a tool, you get text back, and you
cannot see the cut until you import into Final Cut Pro. This is the visual
check in between. No external assets, no scripts, so it renders anywhere.

Note on model shapes: ``Clip`` (the primary-storyline clip type returned by
``Timeline.clips``) has no ``offset`` or ``lane`` field — those only exist on
``ConnectedClip``. A clip's timeline position is its ``start`` (the FCPXML
spine is contiguous; clips do not overlap), so ``clip.start.seconds`` is used
as the left offset. Lane coloring only applies to connected clips, which are
out of scope for this render (it draws ``timeline.clips``, the primary
storyline), so all primary clips share one track color and are told apart by
their border + alternating shade instead. ``Marker`` has no ``value`` field —
its label is ``name``.
"""

from html import escape

# Primary track clips alternate between these two shades so adjacent clips
# (which are always the same duration-derived hue family) still read as
# separate blocks without implying a lane that doesn't exist on this model.
_CLIP_COLORS = ["#3b82f6", "#2563eb"]


def _clip_color(index: int) -> str:
    return _CLIP_COLORS[index % len(_CLIP_COLORS)]


def render_timeline_html(timeline) -> str:
    """Return a standalone HTML document visualising one timeline."""
    total = max(float(timeline.duration.seconds or 0), 0.001)

    rows = []
    for i, clip in enumerate(timeline.clips):
        seconds = float(clip.duration.seconds or 0)
        offset = float(clip.start.seconds or 0)
        width = max((seconds / total) * 100, 0.4)
        left = min((offset / total) * 100, 100)
        rows.append(
            f'<div class="clip" style="left:{left:.3f}%;width:{width:.3f}%;'
            f'background:{_clip_color(i)}" '
            f'title="{escape(str(clip.name))} ({seconds:.2f}s)">'
            f'<span>{escape(str(clip.name))}</span></div>'
        )

    marks = []
    for marker in getattr(timeline, "markers", []):
        at = float(marker.start.seconds or 0)
        marks.append(
            f'<div class="marker" style="left:{min((at / total) * 100, 100):.3f}%" '
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
  .track {{ position: relative; height: 56px; background: #16161c;
            border-radius: 6px; overflow: hidden; }}
  .clip {{ position: absolute; top: 0; bottom: 0; border-right: 1px solid #0b0b0f;
           display: flex; align-items: center; overflow: hidden; }}
  .clip span {{ padding: 0 6px; font-size: 11px; color: #fff; white-space: nowrap;
                text-shadow: 0 1px 2px rgba(0,0,0,.6); }}
  .markers {{ position: relative; height: 14px; margin-top: 6px; }}
  .marker {{ position: absolute; width: 2px; height: 14px; background: #f43f5e; }}
</style>
</head>
<body>
<h1>{escape(str(timeline.name))}</h1>
<div class="meta">
  {timeline.total_clips} clips &#183; {total:.2f}s &#183;
  {timeline.width}&#215;{timeline.height} @ {timeline.frame_rate}fps
</div>
<div class="track">{"".join(rows)}</div>
<div class="markers">{"".join(marks)}</div>
</body>
</html>"""
