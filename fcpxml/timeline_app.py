"""The timeline app — an MCP Apps view of the cut, rendered by the host.

One ``ui://`` resource (the app shell, served once as
``text/html;profile=mcp-app``) and a JSON payload the ``view`` tool returns on
every call. Since 0.32 ``preview://`` serves the same shell with the payload
inlined (:func:`render_page_html`); the static renderer it used to serve
(``fcpxml/preview.py``) is gone, so there is one timeline picture. The host renders the
shell in a sandboxed iframe, posts each tool result into it, and the app
draws lanes, clips, markers, gaps, transcript words and thumbnails from the
payload alone. The app calls back into the server through the host
(``tools/call``) for reads and for edits, so every edit still goes through
the groups, the journal and the review gate — the app owns no write path.

Two rules this file keeps:

* **Time is rational on the wire.** Every position and duration in the
  payload is an exact ``"numerator/denominator"`` string straight from the
  model's Fractions. A float at 23.976 drifts visibly over a two-minute
  timeline; a rational does not, and it round-trips the fixtures exactly,
  which is what the tests assert.
* **The shell is self-contained.** No CDN, no fetch, no external URL of any
  kind. The host's CSP for an app resource is strict, and a shell that
  reaches out would render blank with nothing to say why.

Thumbnails and transcripts are injected as callables rather than computed
here, so the builder is pure: the tests round-trip the fixtures with no
ffmpeg and no cache, and the handler in ``tools/view.py`` supplies the real
renderers.
"""

from __future__ import annotations

import base64
import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Optional

from . import palette
from .media_intel import file_in_point
from .rational import fcp_frame_rate_name, nominal_fps, rational_fps

APP_URI = "ui://fcp/timeline"
APP_MIME_TYPE = "text/html;profile=mcp-app"
PAYLOAD_SCHEMA = "fcp-mcp/timeline-app/1"

# Bounds on what one payload may carry. The payload travels inside a tool
# result, which the host also shows as chat text to a client without Apps
# support, so it has to stay readable and bounded rather than exhaustive.
MAX_THUMBNAILS = 48
THUMBNAIL_SHORT_SIDE = 108  # 2x the 54px a spine tile shows: sharp on a 2x display
MAX_THUMBNAIL_BYTES = 400_000
MAX_WORDS = 3000

# The tool result carries the payload between these fences. The app looks
# for them in every text block it is handed; a client without Apps support
# sees a summary followed by a JSON block, which is still an honest answer.
PAYLOAD_OPEN = "```json fcp-timeline-app"
PAYLOAD_CLOSE = "```"

ThumbnailFn = Callable[[str, Fraction], Optional[str]]
TranscriptFn = Callable[[str], Optional[dict]]
WaveformFn = Callable[[str, Fraction, Fraction], Optional[list]]
MAX_WAVE_POINTS = 600  # levels 0-100 per audio clip, evenly over its length


def rational(value: Any) -> Optional[str]:
    """Exact ``"n/d"`` for a Fraction or a model Timecode; None for None."""
    if value is None:
        return None
    if hasattr(value, "_exact_seconds"):
        value = value._exact_seconds
    f = Fraction(value)
    return f"{f.numerator}/{f.denominator}"


def _frac(value: Any) -> Fraction:
    if hasattr(value, "_exact_seconds"):
        return Fraction(value._exact_seconds)
    return Fraction(value)


def file_sha256(path: Optional[str]) -> Optional[str]:
    """sha256 of the file (``Info.fcpxml`` inside a bundle); None if absent."""
    if not path:
        return None
    p = Path(path)
    if p.is_dir():
        p = p / "Info.fcpxml"
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _origin(timeline) -> Fraction:
    """The timeline's zero in absolute seconds.

    Spine clips carry their absolute ``offset`` (3600s on a broadcast-style
    export). A connected clip whose ``timeline_start`` the parser resolved is
    already on the sequence clock and must not vote; a connected clip with
    only a raw ``offset`` votes like a spine clip. Same rule as
    ``preview._timeline_origin``.
    """
    starts = [_frac(c.start) for c in timeline.clips if c.start]
    starts += [
        _frac(c.offset) for c in timeline.connected_clips
        if c.offset and getattr(c, "timeline_start", None) is None
    ]
    return min(starts) if starts else Fraction(0)


def _position(item, origin: Fraction, *, connected: bool) -> Fraction:
    """Sequence-clock position (0 = first frame of the edit)."""
    if connected:
        tls = getattr(item, "timeline_start", None)
        if tls is not None:
            return _frac(tls)
        return (_frac(item.offset) if item.offset else Fraction(0)) - origin
    return _frac(item.start) - origin


def _kind(item, lane: int, connected: bool) -> str:
    if not connected:
        return "spine"
    ctype = getattr(item, "clip_type", "") or ""
    if ctype in ("audio", "audio-clip") or lane < 0:
        return "audio"
    if ctype in ("title", "caption"):
        return "title"
    return "video"


def _marker_rows(markers, shift: Fraction):
    """Markers on the sequence clock.

    The parser resolves a marker into its HOST's clock: a spine clip's clock
    is absolute (3600s on a broadcast-origin export), a connected clip's is
    already the sequence clock. The caller passes the origin for the former
    and zero for the latter; getting this wrong puts every marker an hour
    off the right edge.
    """
    rows = []
    for m in markers:
        pos = _frac(m.position) - shift
        rows.append({
            "name": m.name,
            "start": rational(pos),
            "type": m.marker_type.value if hasattr(m.marker_type, "value") else str(m.marker_type),
            "note": m.note or "",
        })
    return rows


def _words_for(clip_pos: Fraction, src_start: Fraction, duration: Fraction, transcript: Optional[dict]):
    """Transcript words that fall inside the clip's source range, on the timeline."""
    if not transcript:
        return []
    out = []
    src_end = src_start + duration
    for w in transcript.get("words", []) or []:
        try:
            s = Fraction(str(w["start"])).limit_denominator(100000)
            e = Fraction(str(w["end"])).limit_denominator(100000)
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            continue
        if e <= src_start or s >= src_end:
            continue
        out.append({
            "w": str(w.get("word", "")),
            "s": rational(clip_pos + (max(s, src_start) - src_start)),
            "e": rational(clip_pos + (min(e, src_end) - src_start)),
        })
    return out


def build_payload(
    timeline,
    *,
    filepath: Optional[str] = None,
    sha256: Optional[str] = None,
    thumbnail: Optional[ThumbnailFn] = None,
    transcript: Optional[TranscriptFn] = None,
    waveform: Optional[WaveformFn] = None,
) -> dict:
    """The JSON the app draws from. Pure: every side effect is injected.

    ``thumbnail(media_path, source_seconds)`` returns a data URI or None;
    ``transcript(media_path)`` returns a cached transcript dict or None;
    ``waveform(media_path, file_start, duration)`` returns levels 0-100 for
    an audio clip (0.33 directions draw it), or None. None is ever called
    more than the bounds above allow.
    """
    origin = _origin(timeline)
    duration = _frac(timeline.duration)
    fps = rational_fps(timeline.frame_rate)
    transcripts: dict[str, Optional[dict]] = {}
    thumbs_left, thumb_bytes, words_left = MAX_THUMBNAILS, 0, MAX_WORDS

    def _transcript(media: str):
        if transcript is None or not media:
            return None
        if media not in transcripts:
            transcripts[media] = transcript(media)
        return transcripts[media]

    def _thumb(media: str, at: Fraction):
        nonlocal thumbs_left, thumb_bytes
        if thumbnail is None or not media or thumbs_left <= 0 or thumb_bytes >= MAX_THUMBNAIL_BYTES:
            return None
        uri = thumbnail(media, at)
        if uri:
            thumbs_left -= 1
            thumb_bytes += len(uri)
        return uri

    clips, lanes_seen = [], {}

    def _add(item, index: int, *, connected: bool):
        nonlocal words_left
        lane = int(getattr(item, "lane", 0) or 0) if connected else 0
        pos = _position(item, origin, connected=connected)
        dur = _frac(item.duration)
        src = _frac(item.source_start) if getattr(item, "source_start", None) else (
            _frac(item.start) if connected and item.start else Fraction(0)
        )
        media = getattr(item, "media_path", "") or ""
        # Words and thumbnails come from the FILE; ``src`` above is on the
        # asset clock and stays what the payload shows.
        file_src = file_in_point(item) if getattr(item, "source_start", None) else src
        role = (getattr(item, "role", "") or getattr(item, "video_role", "")
                or getattr(item, "audio_role", "") or "")
        words = []
        if words_left > 0:
            words = _words_for(pos, file_src, dur, _transcript(media))[:words_left]
            words_left -= len(words)
        row = {
            "id": f"{'c' if connected else 's'}{index}",
            "name": item.name,
            "lane": lane,
            "kind": _kind(item, lane, connected),
            "type": getattr(item, "clip_type", "") or ("clip" if not connected else "asset-clip"),
            "start": rational(pos),
            "duration": rational(dur),
            "end": rational(pos + dur),
            "source_start": rational(src),
            "file_start": rational(file_src),
            "media": media,
            "role": role,
            "keywords": [k.value for k in getattr(item, "keywords", []) or []],
            "markers": _marker_rows(
                getattr(item, "markers", []) or [], Fraction(0) if connected else origin
            ),
            "words": words,
            "thumb": _thumb(media, file_src + dur / 2) if media else None,
        }
        if waveform is not None and media and row["kind"] == "audio":
            levels = waveform(media, file_src, dur)
            if levels:
                row["wave"] = [max(0, min(100, int(v))) for v in levels[:MAX_WAVE_POINTS]]
        clips.append(row)
        lanes_seen.setdefault(lane, []).append(row["id"])

    for i, clip in enumerate(timeline.clips):
        _add(clip, i, connected=False)
    for i, cc in enumerate(timeline.connected_clips):
        _add(cc, i, connected=True)

    # Gaps: the parts of the primary storyline no spine clip covers. On a
    # gap-based storyline the whole spine is one gap by construction, and the
    # flag says so rather than leaving the app to infer it from "no spine".
    gaps, cursor = [], Fraction(0)
    spine = sorted(
        (c for c in clips if c["kind"] == "spine"), key=lambda c: Fraction(c["start"])
    )
    for c in spine:
        s, e = Fraction(c["start"]), Fraction(c["end"])
        if s > cursor:
            gaps.append({"start": rational(cursor), "duration": rational(s - cursor)})
        cursor = max(cursor, e)
    if duration > cursor:
        gaps.append({"start": rational(cursor), "duration": rational(duration - cursor)})

    ordered = sorted((lane for lane in lanes_seen if lane > 0), reverse=True)
    ordered += [0] if 0 in lanes_seen or not timeline.connected_clips else []
    ordered += sorted(lane for lane in lanes_seen if lane < 0)

    return {
        "schema": PAYLOAD_SCHEMA,
        "file": {"path": filepath or "", "sha256": sha256},
        "name": timeline.name,
        "fps": rational(fps),
        "fps_label": fcp_frame_rate_name(timeline.frame_rate),
        "nominal_fps": nominal_fps(timeline.frame_rate),
        "width": timeline.width,
        "height": timeline.height,
        "origin": rational(origin),
        "duration": rational(duration),
        "gap_based": bool(timeline.is_gap_based),
        "lanes": [
            {
                "lane": lane,
                "kind": "spine" if lane == 0 and not timeline.is_gap_based else (
                    "audio" if lane < 0 else "video"),
                "clips": lanes_seen.get(lane, []),
            }
            for lane in ordered
        ],
        "clips": clips,
        "gaps": gaps,
        "markers": _marker_rows(timeline.markers, origin),
        "transitions": [
            {"name": t.name, "start": rational(_frac(t.start) - origin), "duration": rational(t.duration)}
            for t in timeline.transitions
        ],
    }


def wrap_payload(summary: str, payload: dict) -> str:
    """The tool result text: a readable summary, then the fenced payload."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"{summary}\n\n{PAYLOAD_OPEN}\n{body}\n{PAYLOAD_CLOSE}"


def extract_payload(text: str) -> Optional[dict]:
    """Inverse of :func:`wrap_payload`; None when the text carries no payload."""
    start = text.find(PAYLOAD_OPEN)
    if start < 0:
        return None
    start += len(PAYLOAD_OPEN)
    end = text.find(PAYLOAD_CLOSE, start)
    if end < 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def data_uri_jpeg(path: str) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------

DIRECTIONS = ("a", "b")


def render_page_html(payload: dict, direction: Optional[str] = None) -> str:
    """The ``preview://`` page (0.32): the same shell with ``payload`` inlined
    as a JSON script block, so a browser or a host that only shows HTML draws
    the timeline at once. It replaces the legacy static renderer
    (fcpxml/preview.py, deleted). ``<`` is escaped inside the JSON, so a clip
    named ``</script>`` stays data."""
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    block = f'<script type="application/json" id="fcp-inline-payload">{data}</script>\n'
    marker = "<script>"
    i = _APP_HTML.rindex(marker)
    html = _APP_HTML[:i] + block + _APP_HTML[i:]
    if direction is not None:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}, got {direction!r}")
        # 0.33: two looks to pick from; the attribute switches CSS and the
        # layout code, nothing else (no new request, no new resource)
        html = html.replace('<html lang="en">', f'<html lang="en" data-direction="{direction}">', 1)
    return html


def render_app_html() -> str:
    """The ``ui://fcp/timeline`` resource. Self-contained; takes no payload.

    The postMessage protocol it speaks is the MCP Apps draft (SEP-1865):
    ``ui/initialize`` → ``ui/notifications/initialized``, then the host posts
    ``ui/notifications/tool-input`` / ``ui/notifications/tool-result`` and
    the app calls ``tools/call``. Those method names are taken from the spec
    text and the ``@modelcontextprotocol/ext-apps`` client; the Python SDK
    only ships the server half, so the in-iframe half could not be checked
    against installed source. Outside an iframe the shell waits with a
    message instead of pretending.
    """
    return _APP_HTML


_APP_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FCP Timeline</title>
<style>
  /*PALETTE*/
  :root { --gap: repeating-linear-gradient(135deg, var(--ground) 0 6px, var(--panel) 6px 12px); }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--ground); color: var(--ink);
    font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  body { padding: 12px 16px 16px; min-height: 100vh; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; margin-bottom: 10px; }
  header h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: .01em; }
  header .meta { color: var(--mute); font-variant-numeric: tabular-nums; }
  header .tc { margin-left: auto; font: 600 14px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--accent); letter-spacing: .04em; }
  #empty { color: var(--mute); padding: 40px 0; text-align: center; }
  #scroller { position: relative; overflow-x: auto; overflow-y: hidden; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel); }
  #canvas { position: relative; min-width: 100%; padding-bottom: 8px; }  /* the last lane never meets the border */
  .ruler { position: relative; height: 48px; border-bottom: 1px solid var(--line); cursor: crosshair;
    user-select: none; }
  .tick { position: absolute; top: 30px; bottom: 0; border-left: 1px solid var(--line-strong); padding-left: 9px;
    font: 10px/18px ui-monospace, Menlo, monospace; color: var(--mute); white-space: nowrap; }
  .mlabel { position: absolute; top: 2px; font-size: 10px; color: var(--accent); white-space: nowrap;
    padding: 0 3px 0 5px; border-left: 2px solid var(--accent); line-height: 13px; }
  .mlabel.row1 { top: 15px; }
  /* A label that does not fit is hidden, never cut: its flag or clip stays, and the
     name is in the title tooltip. fitLabels() decides (tests/test_app_labels.py). A
     ruler tick is its label: one whose time does not fit goes whole, line and all. */
  .mlabel.unfit, .gap.unfit { color: transparent; }
  .tick.unfit { visibility: hidden; }
  /* A ruler time the playhead would cut through is hidden while it is there. */
  .tick.under-playhead { visibility: hidden; }
  .clip .label.unfit { visibility: hidden; }
  .lane { position: relative; height: 44px; border-bottom: 1px solid var(--line); }
  .lane.spine { height: 58px; }
  .lane.audio { height: 30px; }
  .lane-tag { position: sticky; left: 0; z-index: 3; display: block; width: 48px; height: 100%;
    padding: 4px 0 0 6px; font-size: 10px; color: var(--mute); background: var(--panel);
    border-right: 1px solid var(--line); pointer-events: none; }
  .clip { position: absolute; top: 3px; bottom: 3px; border-radius: 4px; overflow: hidden;
    background: var(--raised); border: 1px solid var(--line-strong); cursor: pointer; display: flex; flex-direction: column;
    justify-content: flex-end; }
  .lane.spine .clip { background: var(--raised-2); }
  .lane.audio .clip { background: var(--panel); }
  .clip.title { background: var(--raised-2); border-style: dashed; }
  /* Thumbnails tile along the clip at their own aspect, like Final Cut's filmstrip:
     never stretched, never one frame blown up to fill a long clip; centred, so a
     clip narrower than one frame shows the frame's middle. */
  .clip .thumbs { position: absolute; inset: 0; opacity: .85; background-repeat: repeat-x;
    background-size: auto 100%; background-position: center; }
  .clip.ghost { pointer-events: none; }
  .clip .label { position: relative; z-index: 1; padding: 2px 6px; font-size: 11px; white-space: nowrap;
    overflow: hidden; color: var(--ink); }
  /* The scrim is FULL shade (75% black) from 10px down, and the text starts at 12px: a
     label over a white frame still reads at 10:1 (tests/test_palette.py, test_app_labels.py). */
  .clip.has-thumb .label { color: var(--on-media); padding-top: 12px;
    background: linear-gradient(to bottom, transparent 0, var(--shade) 10px); }
  .clip .role { font-size: 12px; color: var(--mute); margin-left: 6px; text-transform: uppercase; letter-spacing: .04em; }
  .clip.has-thumb .role { color: var(--on-media); }
  /* Every clip carries its number: inside, top right, when it fits clear of
     the name; else just outside the box on a leader, above it (below it for
     lanes under the spine), in a strip the lane opens for it. */
  .clip .num { position: absolute; right: 2px; top: 2px; z-index: 1; padding: 0 3px; border-radius: 3px;
    font-size: 11px; font-weight: 600; color: var(--ink); background: var(--ground); visibility: hidden; white-space: nowrap; }
  .clip .num.on { visibility: visible; }
  .lane.has-out { height: 62px; }
  .lane.spine.has-out { height: 76px; }
  .lane.audio.has-out { height: 48px; }
  .lane.has-out:not(.below) .clip, .lane.has-out:not(.below) .gap { top: 21px; }
  .lane.has-out.below .clip { bottom: 21px; }
  .onum { position: absolute; top: 1px; z-index: 3; padding: 0 3px; border-radius: 3px; font-size: 11px; font-weight: 600;
    line-height: 14px; color: var(--ink); background: var(--ground); border: 1px solid var(--line-strong); white-space: nowrap; }
  .lane.below .onum { top: auto; bottom: 1px; }
  .onum .lead { position: absolute; left: 50%; top: 100%; width: 0; height: 5px; border-left: 1px solid var(--line-strong); }
  .lane.below .onum .lead { top: auto; bottom: 100%; }
  #key { margin-top: 6px; font-size: 12px; color: var(--mute); line-height: 1.6; }
  #key b { color: var(--ink); font-weight: 600; margin-right: 4px; }
  #key span { margin-right: 14px; white-space: nowrap; }
  .clip.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
  .clip .mk { position: absolute; top: 0; width: 2px; height: 8px; background: var(--accent); z-index: 2; }
  .gap { position: absolute; top: 3px; bottom: 3px; border: 1px dashed var(--line-strong); border-radius: 4px; background: var(--gap);
    color: var(--mute); font-size: 10px; padding: 2px 6px; pointer-events: none; overflow: hidden; white-space: nowrap; }
  .words { position: relative; height: 22px; border-bottom: 1px solid var(--line); overflow: hidden; }
  .word { position: absolute; top: 3px; font-size: 10px; color: var(--mute); white-space: nowrap;
    padding: 0 2px; border-left: 1px solid var(--line); }
  .word.hit { color: var(--ink); }
  .markers { position: absolute; left: 0; right: 0; top: 48px; bottom: 0; pointer-events: none; }
  .tmark { position: absolute; top: 0; width: 0; height: 100%; border-left: 1px solid var(--accent);
    opacity: .55; }
  /* The playhead starts under the marker rows, so a marker at the playhead keeps
     its flag and its name, with a 2 px gap under the second row's flag (at 28 px
     the head touched it: critic round 6); its head clears the tick text
     (padding-left 9px). */
  .playhead { position: absolute; top: 30px; bottom: 0; width: 0; border-left: 2px solid var(--accent);
    pointer-events: none; z-index: 4; }
  .playhead .phead { position: absolute; top: 0; left: -7px; width: 0; height: 0; border: 6px solid transparent;
    border-top: 8px solid var(--accent); }
  .range { position: absolute; top: 0; bottom: 0; background: var(--accent-wash);
    border-left: 1px solid var(--accent); border-right: 1px solid var(--accent); pointer-events: none; z-index: 2; }
  #panel { margin-top: 12px; display: grid; grid-template-columns: 1fr; gap: 10px; }
  @media (min-width: 760px) { #panel { grid-template-columns: 1.3fr 1fr; } }
  .card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 10px 12px; min-height: 72px;
    min-width: 0; overflow: hidden; }
  .card h2 { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: var(--mute); text-transform: uppercase; letter-spacing: .08em; }
  dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 2px 12px; margin: 0; font-variant-numeric: tabular-nums; }
  dt { color: var(--mute); } dd { margin: 0; overflow-wrap: anywhere; }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  button { font: inherit; font-size: 12px; padding: 5px 10px; border-radius: 6px; border: 1px solid var(--line-strong);
    background: var(--raised); color: var(--ink); cursor: pointer; }
  button.primary { background: var(--accent-fill); color: var(--accent-ink); border-color: var(--accent-fill); font-weight: 600; }
  /* No opacity: a 45% button read at 4.1:1. Disabled text is the mute token (palette.TEXT_PAIRS). */
  button:disabled { color: var(--mute); cursor: default; }
  button.primary:disabled { background: var(--raised); color: var(--mute); border-color: var(--line-strong); font-weight: 400; }
  /* Read only (preview://, no host): the controls are OFF, not merely unwired. */
  .readonly button, .readonly input[type=text] { background: transparent; border-style: dashed; color: var(--mute);
    cursor: not-allowed; font-weight: 400; }
  .readonly-note { width: 100%; font-size: 12px; color: var(--mute); }
  input[type=text] { font: inherit; font-size: 12px; padding: 5px 8px; border-radius: 6px; border: 1px solid var(--line-strong);
    background: var(--ground); color: var(--ink); flex: 1 1 120px; min-width: 0; max-width: 100%; }
  /* The browser's own placeholder grey read 4.07:1 on the panel; the mute token reads 5.6:1. */
  input::placeholder { color: var(--mute); opacity: 1; }
  #log { white-space: pre-wrap; overflow-wrap: anywhere; font: 11px/1.45 ui-monospace, Menlo, monospace; color: var(--mute);
    max-height: 180px; overflow: auto; margin: 0; }
  .zoom { display: inline-flex; gap: 4px; align-items: center; color: var(--mute); font-size: 11px; }
  /* ---- 0.33 directions: data-direction="a" (dense, like Resolve's edit page)
     or "b" (open, like Final Cut's timeline). Nothing below applies without
     the attribute. Both: an overview bar, fit with the median clip >= 44 px,
     no label under 4 characters, a label rail for narrow connected clips,
     a glyph per marker kind, cyan text for record time only. */
  #overview { position: relative; margin-bottom: 6px; border: 1px solid var(--line); border-radius: 6px;
    background: var(--panel); overflow: hidden; cursor: pointer; }
  #overview .ob { position: absolute; border-radius: 1px; background: var(--line-strong); }
  #overview .ob.k-audio { background: var(--mute); }
  #overview .om { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--accent); }
  #overview .ov { position: absolute; top: 1px; bottom: 1px; border: 1px solid var(--accent); background: var(--accent-wash);
    border-radius: 3px; pointer-events: none; }
  [data-direction] .meter { position: absolute; left: -9999px; top: 0; visibility: hidden; white-space: nowrap; }
  [data-direction] #key { display: none; }
  [data-direction] .clip .num { display: none; }
  [data-direction] .mlabel { color: var(--ink); border-left: 0; padding: 0 3px 0 11px; font-size: 11px; line-height: 14px; }
  [data-direction] .mlabel .mg { position: absolute; left: -4px; top: 3px; width: 8px; height: 8px; background: var(--accent); }
  [data-direction] .mlabel .mg.standard { transform: rotate(45deg) scale(.82); }
  [data-direction] .mlabel .mg.chapter { height: 11px; top: 2px; clip-path: polygon(0 0, 100% 0, 100% 62%, 50% 100%, 0 62%); }
  [data-direction] .mlabel .mg.todo { background: transparent; border: 2px solid var(--accent); }
  [data-direction] .mlabel .mg.completed { border: 2px solid var(--accent); }
  [data-direction] .tick { top: auto; bottom: 0; height: 15px; padding-left: 4px; font: 11px/15px ui-monospace, SFMono-Regular, Menlo, monospace; }
  [data-direction] .ftick { position: absolute; bottom: 0; width: 0; height: 4px; border-left: 1px solid var(--line-strong); }
  [data-direction] .ftick.sec { height: 7px; }
  [data-direction] .clip { justify-content: flex-start; }
  [data-direction] .clip .stripe { position: absolute; left: 0; right: 0; z-index: 1; }
  [data-direction] .clip .wave { position: absolute; left: 0; right: 0; bottom: 0; width: 100%; }
  [data-direction] .clip .wave path { fill: var(--mute); }
  [data-direction] .rail { position: relative; height: 16px; }
  [data-direction] .rail.rows2 { height: 31px; }
  /* A rail name sits on the panel colour, the row nearer its lane on top: a
     leader from the farther row passes BEHIND a nearer name, never through it. */
  [data-direction] .rail .rl { position: absolute; top: 0; bottom: 0; padding: 0 4px; border-left: 1px solid var(--line-strong);
    color: var(--ink); white-space: nowrap; line-height: 15px; background: var(--panel); cursor: pointer; }
  [data-direction] .rail .rl.r1 { top: 15px; z-index: 1; }
  [data-direction] .rail.below .rl { top: 0; bottom: auto; height: 16px; }
  [data-direction] .rail.below .rl.r1 { top: 0; height: 31px; padding-top: 15px; }
  [data-direction] .gap { color: var(--mute); }

  /* A: dense. Thin lanes, small mono labels, a timecode ruler with frame
     ticks, a 3 px role stripe on the clip's top edge (palette greys: picture
     ink, sound mute), the waveform under audio clips, a compact panel. */
  [data-direction="a"] #overview { height: 26px; }
  [data-direction="a"] .ruler { height: 50px; }
  [data-direction="a"] .markers { top: 50px; }
  [data-direction="a"] .playhead { top: 34px; }
  [data-direction="a"] .lane { height: 52px; }
  [data-direction="a"] .lane.audio { height: 60px; }
  [data-direction="a"] .lane-tag { width: 40px; padding: 6px 0 0 8px; font: 600 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; }
  [data-direction="a"] .clip { top: 2px; bottom: 2px; border-radius: 2px; }
  [data-direction="a"] .clip .label, [data-direction="a"] .meter.label, [data-direction="a"] .rail .rl, [data-direction="a"] .meter.rl {
    font: 11px/14px ui-monospace, SFMono-Regular, Menlo, monospace; }
  [data-direction="a"] .clip .label { padding: 5px 4px 0; color: var(--ink); background: none; }
  [data-direction="a"] .clip .stripe { top: 0; height: 3px; background: var(--ink); }
  [data-direction="a"] .clip.k-audio .stripe { background: var(--mute); }
  [data-direction="a"] .clip .wave { height: 34px; }
  [data-direction="a"] #panel { margin-top: 8px; gap: 8px; }
  [data-direction="a"] .card { padding: 8px 10px; min-height: 0; }
  @media (min-width: 760px) { [data-direction="a"] dl { grid-template-columns: max-content minmax(0, 1fr) max-content minmax(0, 1fr); } }
  [data-direction="a"] #log { max-height: 96px; }

  /* B: open. Tall clips with a filmstrip, the name top-left on a scrim,
     rounded corners, connected clips on their own lanes with the connection
     line to the storyline, Final Cut's role colours as the stripe. */
  [data-direction="b"] #overview { height: 34px; border-radius: 8px; }
  [data-direction="b"] .ruler { height: 52px; }
  [data-direction="b"] .markers { top: 52px; }
  [data-direction="b"] .playhead { top: 36px; }
  [data-direction="b"] .lane { height: 84px; border-bottom: 0; z-index: 1; }
  [data-direction="b"] .lane.spine { height: 40px; }
  [data-direction="b"] .lane.audio { height: 64px; }
  [data-direction="b"] .lane-tag { visibility: hidden; width: 16px; padding: 0; border-right: 0; background: transparent; }
  [data-direction="b"] .clip { top: 4px; bottom: 4px; border-radius: 7px; }
  [data-direction="b"] .clip .label, [data-direction="b"] .meter.label, [data-direction="b"] .rail .rl, [data-direction="b"] .meter.rl {
    font: 600 12px/15px ui-sans-serif, system-ui, -apple-system, sans-serif; }
  [data-direction="b"] .clip .label { position: absolute; top: 0; left: 0; max-width: 100%; padding: 4px 7px 5px;
    color: var(--on-media); background: var(--shade); border-bottom-right-radius: 7px; }
  [data-direction="b"] .clip .stripe { bottom: 0; height: 4px; }
  [data-direction="b"] .clip .stripe.role-video { background: var(--role-video); }
  [data-direction="b"] .clip .stripe.role-titles { background: var(--role-titles); }
  [data-direction="b"] .clip .stripe.role-dialogue { background: var(--role-dialogue); }
  [data-direction="b"] .clip .stripe.role-music { background: var(--role-music); }
  [data-direction="b"] .clip .stripe.role-effects { background: var(--role-effects); }
  [data-direction="b"] .clip .wave { height: 40px; bottom: 4px; }
  [data-direction="b"] .lane.spine .gap { top: 8px; bottom: 8px; border: 1px solid var(--line-strong); border-radius: 7px;
    background: var(--raised); padding: 4px 8px; font-size: 11px; }
  [data-direction="b"] .rail { z-index: 1; }  /* a connection line passes behind a rail name */
  [data-direction="b"] .conn { position: absolute; width: 0; border-left: 1px solid var(--mute); z-index: 0; pointer-events: none; }
  [data-direction="b"] .conn::after { content: ""; position: absolute; left: -3px; width: 5px; height: 5px; border-radius: 50%;
    background: var(--mute); }
  [data-direction="b"] .conn.up::after { bottom: -3px; }
  [data-direction="b"] .conn.down::after { top: -3px; }
  @media (max-width: 600px) {
    [data-direction="b"] .lane { height: 64px; }
    [data-direction="b"] .lane.audio { height: 56px; }
  }
</style>
</head>
<body>
<header>
  <h1 id="title">FCP timeline</h1>
  <span class="meta" id="meta"></span>
  <span class="zoom">zoom <button id="zout" title="zoom out">&minus;</button><button id="zin" title="zoom in">+</button></span>
  <span class="tc" id="tc">00:00:00:00</span>
</header>
<div id="empty">Waiting for a timeline. Call the <b>view</b> tool with <code>view_timeline</code> and a filepath.</div>
<div id="scroller" hidden><div id="canvas"></div></div>
<div id="key" hidden></div>
<div id="panel" hidden>
  <div class="card">
    <h2>Selection</h2>
    <div id="sel">Click a clip. Drag on the ruler to select a range.</div>
    <div class="actions" id="acts"></div>
  </div>
  <div class="card">
    <h2>Server</h2>
    <pre id="log">idle</pre>
  </div>
</div>
<script>
(function () {
  "use strict";
  var PAYLOAD_OPEN = "```json fcp-timeline-app", PAYLOAD_CLOSE = "```";
  var state = { payload: null, pps: 24, playhead: 0, sel: null, range: null, pending: {}, nextId: 1, inHost: false, hostContext: null };
  var $ = function (id) { return document.getElementById(id); };
  // 0.33: data-direction="a" | "b" picks a look; without it the app draws
  // exactly as 0.32 shipped it (every function below checks DIR first).
  var DIR = document.documentElement.getAttribute("data-direction") || "";
  var MEDIAN_MIN = 44, LABEL_MIN = 28, KEEP_MIN = 4;

  // ---- rational time ------------------------------------------------------
  function frac(s) { if (s == null) return 0; var p = String(s).split("/"); return p.length === 2 ? Number(p[0]) / Number(p[1]) : Number(p[0]); }
  function tcOf(sec) {
    var p = state.payload, fps = p ? frac(p.fps) : 24, nom = p ? p.nominal_fps : 24;
    var total = Math.round(sec * fps), f = total % nom, s = Math.floor(total / nom);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60) + ":" + pad(f);
  }
  function secOfSeconds(sec) { return sec.toFixed(3) + "s"; }

  // ---- host protocol (MCP Apps draft) ------------------------------------
  function send(msg) { if (state.inHost) window.parent.postMessage(msg, "*"); }
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = state.nextId++;
      state.pending[id] = { resolve: resolve, reject: reject };
      send({ jsonrpc: "2.0", id: id, method: method, params: params });
      if (!state.inHost) reject(new Error("not inside an MCP Apps host"));
    });
  }
  function notify(method, params) { send({ jsonrpc: "2.0", method: method, params: params || {} }); }
  window.addEventListener("message", function (ev) {
    var m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && state.pending[m.id] && !m.method) {
      var p = state.pending[m.id]; delete state.pending[m.id];
      if (m.error) p.reject(new Error(m.error.message || "host error")); else p.resolve(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") onToolResult(m.params || {});
    else if (m.method === "ui/notifications/tool-input") log("tool input: " + JSON.stringify(m.params || {}).slice(0, 300));
    else if (m.method === "ui/notifications/host-context-changed") applyHostContext(m.params || state.hostContext);
    else if (m.id != null && m.method) send({ jsonrpc: "2.0", id: m.id, result: {} });
  });
  // The host's theme wins; without one the OS preference does (CSS media query).
  function applyHostContext(ctx) {
    state.hostContext = ctx;
    var theme = ctx && ctx.theme;
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
  }
  function onToolResult(params) {
    var content = (params.result && params.result.content) || params.content || [];
    var found = null;
    content.forEach(function (c) { if (c && c.type === "text" && found == null) found = extract(c.text); });
    if (found) { setPayload(found); log("timeline loaded: " + found.name + " (" + found.clips.length + " clips)"); }
    else { log("tool result carried no timeline payload:\n" + content.map(function (c) { return c.text || ""; }).join("\n").slice(0, 600)); }
  }
  function extract(text) {
    if (typeof text !== "string") return null;
    var a = text.indexOf(PAYLOAD_OPEN); if (a < 0) return null; a += PAYLOAD_OPEN.length;
    var b = text.indexOf(PAYLOAD_CLOSE, a); if (b < 0) return null;
    try { return JSON.parse(text.slice(a, b)); } catch (e) { return null; }
  }
  function callTool(name, args) {
    log("→ " + name + " " + JSON.stringify(args).slice(0, 200));
    return request("tools/call", { name: name, arguments: args }).then(function (res) {
      var text = ((res && res.content) || []).map(function (c) { return c.text || ""; }).join("\n");
      log("← " + text.slice(0, 1200));
      return text;
    }, function (err) { log("✗ " + err.message); throw err; });
  }

  // ---- render --------------------------------------------------------------
  function setPayload(p) {
    var before = state.payload ? snapshot() : null;
    state.payload = p; state.sel = null; state.range = null; state.playhead = 0;
    $("empty").hidden = true; $("scroller").hidden = false; $("panel").hidden = false;
    $("title").textContent = p.name || "timeline";
    var spineN = p.clips.filter(function (c) { return !c.lane; }).length, connN = p.clips.length - spineN;
    // the clip count worded as every other output words it (proofs.counts_label)
    $("meta").textContent = spineN + (spineN === 1 ? " clip" : " clips") + " + " + connN + " connected · " + secOfSeconds(frac(p.duration)) + " · " + p.width + "×" + p.height + " @ " + p.fps_label + "fps" + (p.gap_based ? " · gap-based storyline" : "");
    fit(); draw(); updateSel();
    if (before) animateFrom(before);
  }

  // ---- motion between payloads ------------------------------------------------
  // A re-render after an edit MOVES things, the way Final Cut closes a gap: a
  // clip that was split shows its pieces where that footage sat and slides them
  // together, the clips after it slide left, a clip that is new is laid in from
  // its left edge, and new markers land one after another, left to right.
  // Undoing it moves back: the pieces stretch back over the footage between
  // them until they ARE the clip again, a clip with no successor shrinks out,
  // and a marker with none leaves in turn, right to left. Several clips laid
  // in, or leaving, at once go in turn too (left to right in, right to left
  // out), so the clip count never jumps. Only positions, sizes, colours and (in
  // steps) visibility animate, never opacity, so no frame shows the old layout
  // and the new one at once. A clip's scrim is drawn the whole time; its name
  // stays transparent while the clip is too narrow for all of it and fades in
  // over 120 ms once it fits, and fades out before a shrinking clip gets too
  // narrow, so no frame shows part of a name (tests/test_app_motion.py). The
  // curve is ease-in-out, so a split is seen apart before it closes. Web
  // Animations, so a recorder can pause and seek them (document.getAnimations()).
  var CURVE = [0.65, 0, 0.35, 1];
  var MOTION = { duration: 520, easing: "cubic-bezier(" + CURVE.join(", ") + ")" }, REVEAL_MS = 120, TURN_MS = 90;
  var SWAP = 0.8;  // when merging pieces hand over to the clip they came from (fraction of MOTION's time)
  function snapshot() {
    var s = { pps: state.pps, clips: [], keyed: {}, els: {} };
    document.querySelectorAll("#canvas .clip:not(.ghost)").forEach(function (e) {
      if (e.__c) s.clips.push({ c: e.__c, left: parseFloat(e.style.left), width: parseFloat(e.style.width), el: e,
                                row: e.parentNode && e.parentNode.dataset.lane });
    });
    document.querySelectorAll("#canvas .tick, #canvas .mlabel:not(.ghost), #canvas .tmark:not(.ghost), #canvas .mk").forEach(function (e) {
      if (e.__key != null) { s.keyed[e.__key] = parseFloat(e.style.left); s.els[e.__key] = e; }
    });
    return s;
  }
  function sameSource(c, o) { return !!c.media && o.c.media === c.media && o.c.lane === c.lane; }
  // The clip this one came from: same media, same lane, and its source in-point
  // inside the old clip's source range (a piece of a split clip, or the clip itself).
  function predecessor(c, olds) {
    var src = frac(c.source_start), hit = null;
    olds.forEach(function (o) {
      if (hit || !sameSource(c, o)) return;
      var os = frac(o.c.source_start), od = frac(o.c.duration);
      if (src >= os - 1e-6 && src < os + od - 1e-6) hit = o;
    });
    return hit;
  }
  // The old clips this one swallowed (an undone split: its pieces merge back).
  function merged(c, olds) {
    var src = frac(c.source_start), end = src + frac(c.duration);
    return olds.filter(function (o) {
      var os = frac(o.c.source_start);
      return sameSource(c, o) && os >= src - 1e-6 && os < end - 1e-6;
    }).sort(function (a, b) { return a.left - b.left; });
  }
  function overlapsAny(o, clips) {
    var os = frac(o.c.source_start), oe = os + frac(o.c.duration);
    return clips.some(function (c) {
      var cs = frac(c.source_start), ce = cs + frac(c.duration);
      return sameSource(c, o) && cs < oe - 1e-6 && os < ce - 1e-6;
    });
  }
  // The fraction of MOTION's time at which its eased progress first reaches p
  // (y is monotonic for CURVE).
  function timeForProgress(p) {
    if (p <= 0) return 0; if (p >= 1) return 1;
    var b = function (u, a, c) { return 3 * (1 - u) * (1 - u) * u * a + 3 * (1 - u) * u * u * c + u * u * u; };
    var lo = 0, hi = 1;
    for (var i = 0; i < 40; i++) { var mid = (lo + hi) / 2; if (b(mid, CURVE[1], CURVE[3]) < p) lo = mid; else hi = mid; }
    return b(hi, CURVE[0], CURVE[2]);
  }
  function colours(el, fill) {
    var cs = getComputedStyle(el), shut = { color: "transparent" }, open = { color: cs.color };
    if (fill) { shut.backgroundColor = "transparent"; open.backgroundColor = cs.backgroundColor; }
    return [shut, open];
  }
  // Hold el's colours (text, and fill if asked) transparent until its clip,
  // growing from `from` to `to` px over MOTION (after `delay` ms), is `need`
  // px wide; then fade them in.
  function revealWhenWide(el, from, to, need, fill, delay) {
    if (need <= from + 0.5) return null;
    var at = to - from > 0.5 ? timeForProgress((need - from) / (to - from)) * MOTION.duration : MOTION.duration;
    var total = Math.max(MOTION.duration, at + REVEAL_MS), sc = colours(el, fill), shut = sc[0], open = sc[1];
    var k = [Object.assign({ offset: 0 }, shut), Object.assign({ offset: at / total }, shut), Object.assign({ offset: Math.min(1, (at + REVEAL_MS) / total) }, open)];
    if (k[2].offset < 1) k.push(Object.assign({ offset: 1 }, open));
    return el.animate(k, { duration: total, easing: "linear", delay: delay || 0, fill: "backwards" });
  }
  // The reverse, for a clip shrinking from `from` to `to` px: el fades out over
  // the 120 ms before the clip gets narrower than `need`, and stays out.
  function hideWhenNarrow(el, from, to, need, fill, delay) {
    var sc = colours(el, fill), shut = sc[0], open = sc[1], T = MOTION.duration, opt = { duration: T, easing: "linear", delay: delay || 0, fill: "both" };
    if (need >= from - 0.5) return el.animate([shut, shut], opt);
    var at = timeForProgress((from - need) / (from - to)) * T, start = Math.max(0, at - REVEAL_MS);
    return el.animate([Object.assign({ offset: 0 }, open), Object.assign({ offset: start / T }, open),
                       Object.assign({ offset: Math.max(start / T + 1e-3, at / T) }, shut), Object.assign({ offset: 1 }, shut)], opt);
  }
  function labelNeed(lab) { var rg = document.createRange(); rg.selectNodeContents(lab); return rg.getBoundingClientRect().width + 14; }  // padding 12 + borders 2
  function revealLabel(e, from, to, delay) {
    var lab = e.querySelector(".label");
    if (!lab || lab.classList.contains("unfit")) return;
    var need = labelNeed(lab);
    revealWhenWide(lab, from, to, need, false, delay);
    lab.querySelectorAll(".role").forEach(function (r) { revealWhenWide(r, from, to, need, false, delay); });
  }
  function hideLabel(e, from, to, delay) {
    var lab = e.querySelector(".label");
    if (!lab || lab.classList.contains("unfit")) return;
    var need = labelNeed(lab);
    hideWhenNarrow(lab, from, to, need, false, delay);
    lab.querySelectorAll(".role").forEach(function (r) { hideWhenNarrow(r, from, to, need, false, delay); });
  }
  // The turn each element takes, by its left edge (or by the x given for it):
  // dir 1 is left to right.
  function turns(els, dir, given) {
    var xs = given || els.map(function (e) { return e.getBoundingClientRect().left; });
    var order = xs.slice().sort(function (a, b) { return dir * (a - b); })
      .filter(function (v, i, a) { return i === 0 || Math.abs(v - a[i - 1]) > 1; });
    return xs.map(function (v) { return order.findIndex(function (w) { return Math.abs(w - v) <= 1; }); });
  }
  function hiddenFor(e, ms) { if (ms > 0) e.animate([{ visibility: "hidden" }, { visibility: "hidden" }], { duration: ms }); }
  // An undone split: each old piece stretches over its share of the clip it
  // came from (its own footage and the footage after it, up to the next piece)
  // while the clip grows from their span to its own; at SWAP, with the gaps
  // all but closed, the clip takes over from its pieces.
  function mergeBack(e, m, L, W) {
    var c = e.__c, src = frac(c.source_start), row = e.parentNode, T = MOTION.duration;
    var spanL = m[0].left, spanR = m[m.length - 1].left + m[m.length - 1].width;
    var at = function (sec) { return L + (sec - src) * state.pps; };
    e.__motion = { from: spanR - spanL, to: W, delay: 0 };
    e.animate([{ left: spanL + "px", width: (spanR - spanL) + "px" }, { left: L + "px", width: W + "px" }], MOTION);
    e.animate([{ visibility: "hidden", offset: 0 }, { visibility: "hidden", offset: SWAP }, { visibility: "visible", offset: SWAP }, { visibility: "visible", offset: 1 }],
              { duration: T, easing: "linear" });
    m.forEach(function (o, i) {
      var g = o.el, tl = at(frac(o.c.source_start)), tr = i + 1 < m.length ? at(frac(m[i + 1].c.source_start)) - 1 : L + W;
      g.classList.add("ghost"); g.classList.remove("selected");
      row.appendChild(g);
      g.animate([{ left: o.left + "px", width: o.width + "px" }, { left: tl + "px", width: Math.max(3, tr - tl) + "px" }], Object.assign({ fill: "forwards" }, MOTION));
      var a = g.animate([{ visibility: "visible", offset: 0 }, { visibility: "visible", offset: SWAP }, { visibility: "hidden", offset: SWAP }, { visibility: "hidden", offset: 1 }],
                        { duration: T, easing: "linear", fill: "forwards" });
      a.onfinish = function () { if (g.parentNode) g.parentNode.removeChild(g); };
    });
  }
  function animateFrom(s) {
    if (!document.body.animate || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)) return;
    var news = [], laid = [];
    document.querySelectorAll("#canvas .clip").forEach(function (e) {
      var c = e.__c, L = parseFloat(e.style.left), W = parseFloat(e.style.width), fl = L, from = 0;
      if (!c) return;
      news.push(c);
      var m = merged(c, s.clips), o = m.length > 1 ? null : predecessor(c, s.clips);
      if (m.length > 1) { mergeBack(e, m, L, W); return; }
      if (!o) { laid.push(e); return; }
      // where this piece of footage sat before the edit, at the old scale
      fl = o.left + (frac(c.source_start) - frac(o.c.source_start)) * s.pps;
      from = Math.max(3, frac(c.duration) * s.pps - 1);
      if (Math.abs(fl - L) < 0.5 && Math.abs(from - W) < 0.5) return;
      e.__motion = { from: from, to: W, delay: 0 };
      e.animate([{ left: fl + "px", width: from + "px" }, { left: L + "px", width: W + "px" }], MOTION);
      revealLabel(e, from, W, 0);
    });
    // New clips are laid in from their left edges, in turn, left to right.
    var inClip = turns(laid, 1);
    laid.forEach(function (e, i) {
      var L = parseFloat(e.style.left), W = parseFloat(e.style.width), d = inClip[i] * TURN_MS;
      e.__motion = { from: 0, to: W, delay: d };
      hiddenFor(e, d);
      e.animate([{ left: L + "px", width: "0px" }, { left: L + "px", width: W + "px" }], Object.assign({ delay: d, fill: "backwards" }, MOTION));
      revealLabel(e, 0, W, d);
    });
    // A clip with no successor shrinks out where it was, its name fading out
    // before it gets too narrow; several leave in turn, right to left.
    var gone = s.clips.filter(function (o) { return !overlapsAny(o, news) && document.querySelector('#canvas .lane[data-lane="' + o.row + '"]'); });
    var outClip = turns(gone, -1, gone.map(function (o) { return o.left; }));  // (detached: by their old x)
    gone.forEach(function (o, i) {
      var row = document.querySelector('#canvas .lane[data-lane="' + o.row + '"]'), g = o.el, d = outClip[i] * TURN_MS;
      g.classList.add("ghost"); g.classList.remove("selected");
      g.querySelectorAll(".mk").forEach(function (x) { x.style.visibility = "hidden"; });
      row.appendChild(g);
      g.__motion = { from: o.width, to: 0, delay: d };
      // (hidden at the end even if never removed: a paused recorder never fires finish)
      var a = g.animate([{ width: o.width + "px" }, { width: "0px", offset: 0.999 }, { width: "0px", visibility: "hidden" }],
                        Object.assign({ fill: "both", delay: d }, MOTION));
      hideLabel(g, o.width, 0, d);
      a.onfinish = function () { if (g.parentNode) g.parentNode.removeChild(g); };
    });
    var arrive = [], leave = [];
    document.querySelectorAll("#canvas .tick, #canvas .mlabel, #canvas .tmark, #canvas .mk").forEach(function (e) {
      var was = s.keyed[e.__key], L = parseFloat(e.style.left);
      if (was != null) {
        if (Math.abs(was - L) > 0.5 && !e.classList.contains("mk")) e.animate([{ left: was + "px" }, { left: L + "px" }], MOTION);
      } else if (!e.classList.contains("tick")) arrive.push(e);
    });
    var still = {};
    document.querySelectorAll("#canvas .mlabel, #canvas .tmark").forEach(function (e) { if (e.__key != null) still[e.__key] = true; });
    Object.keys(s.els).forEach(function (k) {
      var e = s.els[k];
      if (still[k] || !(e.classList.contains("mlabel") || e.classList.contains("tmark"))) return;
      var host = document.querySelector(e.classList.contains("mlabel") ? "#canvas .ruler" : "#canvas .markers");
      if (host) { e.classList.add("ghost"); host.appendChild(e); leave.push(e); }
    });
    // Markers arrive whole, one after another, left to right (a flag and its
    // clip tick together), and leave the same way, right to left.
    var inTurn = turns(arrive, 1), outTurn = turns(leave, -1);
    arrive.forEach(function (e, i) { hiddenFor(e, inTurn[i] * TURN_MS); });
    leave.forEach(function (e, i) {
      var a = e.animate([{ visibility: "visible" }, { visibility: "hidden" }], { duration: outTurn[i] * TURN_MS + 1, fill: "forwards" });
      a.onfinish = function () { if (e.parentNode) e.parentNode.removeChild(e); };
    });
  }
  var GUTTER = DIR === "a" ? 40 : DIR === "b" ? 16 : 48, MIN_PPS = 0.25;
  function extent() {
    // Fit the whole sequence, like Final Cut's zoom to fit. Until 0.31 this
    // fitted the clips only, because a ripple left the sequence's declared
    // duration stale and the tail was empty; the writer now keeps them in
    // step, and a tail that IS in the file is drawn rather than cut at the edge.
    var p = state.payload, end = 0;
    p.clips.forEach(function (c) { end = Math.max(end, frac(c.end)); });
    return Math.max(end, frac(p.duration)) || 1;
  }
  function fit() {
    // The whole sequence in the width, however long (0.32: an 8 px/s floor
    // ran a 164 s cut 14 s past the edge with no sign of it).
    var w = $("scroller").clientWidth - GUTTER - 24;
    state.pps = Math.max(MIN_PPS, w / extent());
    // 0.33: fitting a long bed under short shots made every shot a sliver;
    // the median clip gets at least MEDIAN_MIN px and the overview bar shows
    // the whole cut with the part in view boxed
    if (DIR) state.pps = Math.max(state.pps, (MEDIAN_MIN + 1) / medianDuration());  // + the 1 px between clips
  }
  function medianDuration() {
    var d = state.payload.clips.map(function (c) { return frac(c.duration); }).filter(function (v) { return v > 0; })
      .sort(function (a, b) { return a - b; });
    if (!d.length) return 1;
    var m = d.length >> 1;
    return d.length % 2 ? d[m] : (d[m - 1] + d[m]) / 2;
  }
  function x(sec) { return GUTTER + sec * state.pps; }   // a position
  function w(sec) { return sec * state.pps; }            // a width: no gutter
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function draw() {
    if (DIR) return drawDir();
    var p = state.payload, canvas = $("canvas"); canvas.textContent = "";
    var dur = frac(p.duration), width = Math.max(x(dur) + 24, $("scroller").clientWidth - 2);
    canvas.style.width = width + "px";
    // ruler: ticks on the lower row, timeline marker names on the upper row
    var ruler = el("div", "ruler");
    // Ticks sit on whole seconds of the timecode they print: at 23.98 a timecode
    // second lasts 1.001 real seconds, so a tick at 30 real seconds read 00:00:29:23.
    var step = niceStep(dur, width - GUTTER), perSec = (p.nominal_fps || 24) / (frac(p.fps) || 24);
    for (var S = 0; S * perSec <= dur + 1e-9; S += step) {
      var t = S * perSec, k = el("div", "tick", tcOf(t)); k.style.left = x(t) + "px"; k.__key = "t:" + S; ruler.appendChild(k);
    }
    // Marker names on the upper row: sequence markers AND clip markers (a
    // chapter marker lives on its clip in FCPXML; every m.start is already
    // on the sequence clock), left to right, a name dropped when it would
    // overprint the previous one.
    canvas.appendChild(ruler);  // attached first, so label widths below are measured, not guessed
    var rowEnd = [-1, -1], named = p.markers.slice();
    p.clips.forEach(function (c) { (c.markers || []).forEach(function (m) { named.push(m); }); });
    named.sort(function (a, b) { return frac(a.start) - frac(b.start); });
    named.forEach(function (m) {
      var left = x(frac(m.start));
      var lab = el("div", "mlabel", m.name); lab.style.left = left + "px"; lab.title = m.name; lab.__key = "m:" + m.name + "@" + m.start;
      ruler.appendChild(lab);
      // The first of two rows the name fits on; on neither, the flag stays
      // and the name is hidden (fitLabels), never printed over its neighbour.
      var row = left > rowEnd[0] + 4 ? 0 : (left > rowEnd[1] + 4 ? 1 : -1);
      if (row === 1) lab.classList.add("row1");
      if (row < 0) { lab.classList.add("unfit", "crowded"); return; }
      rowEnd[row] = left + lab.offsetWidth;
    });
    ruler.addEventListener("mousedown", rulerDown);
    var byId = {}; p.clips.forEach(function (c) { byId[c.id] = c; });
    state.num = {};
    // record order, as the sheet and the text timeline number them: by start,
    // the spine first, then lanes from the lowest
    p.clips.slice().sort(function (a, b) {
      return frac(a.start) - frac(b.start) || (!!a.lane - !!b.lane) || (a.lane || 0) - (b.lane || 0); })
      .forEach(function (c, i) { state.num[c.id] = i + 1; });
    // Room for a connected clip: a spine with nothing above it keeps one empty
    // lane-1 row, so a clip laid on lane 1 (gen_broll) appears in place instead
    // of pushing the spine, and everything under it, down.
    var lanes = p.lanes.slice();
    if (lanes.some(function (l) { return l.kind === "spine"; }) && !lanes.some(function (l) { return l.lane > 0; }))
      lanes.unshift({ lane: 1, kind: "video reserved", clips: [] });
    lanes.forEach(function (lane) {
      var row = el("div", "lane " + lane.kind + (lane.lane < 0 ? " below" : "")); row.dataset.lane = String(lane.lane);
      row.appendChild(el("span", "lane-tag", lane.kind === "spine" ? "spine" : "lane " + lane.lane));
      if (lane.kind === "spine") p.gaps.forEach(function (g) {
        var ge = el("div", "gap", "gap " + secOfSeconds(frac(g.duration)));
        ge.style.left = x(frac(g.start)) + "px"; ge.style.width = Math.max(2, w(frac(g.duration))) + "px"; row.appendChild(ge);
      });
      lane.clips.forEach(function (id) { row.appendChild(clipEl(byId[id])); });
      canvas.appendChild(row);
      if (lane.kind === "spine" || lane.lane > 0) {
        var words = lane.clips.reduce(function (acc, id) { return acc.concat(byId[id].words || []); }, []);
        if (words.length) {
          // Words are placed where they are said; one that would overprint
          // the previous word is dropped rather than drawn on top of it.
          var wr = el("div", "words"), lastEnd = -1;
          words.sort(function (a, b) { return frac(a.s) - frac(b.s); }).forEach(function (w) {
            var left = x(frac(w.s)); if (left < lastEnd) return;
            var we = el("span", "word", w.w); we.style.left = left + "px"; wr.appendChild(we);
            lastEnd = left + w.w.length * 5.6 + 8;
          });
          canvas.appendChild(wr);
        }
      }
    });
    var marks = el("div", "markers");
    p.markers.forEach(function (m) { var me = el("div", "tmark"); me.style.left = x(frac(m.start)) + "px"; me.title = m.name; me.__key = "M:" + m.name + "@" + m.start; marks.appendChild(me); });
    canvas.appendChild(marks);
    var ph = el("div", "playhead"); ph.id = "playhead"; ph.appendChild(el("div", "phead")); canvas.appendChild(ph);
    var rg = el("div", "range"); rg.id = "range"; rg.hidden = true; canvas.appendChild(rg);
    placePlayhead();
    fitLabels();
  }
  // Every label is either whole and inside the visible track area, or hidden.
  // A clip label wider than its clip loses its role, then itself; a tick,
  // marker or gap label that crosses the visible edge (the scroller's right
  // side, or the sticky lane gutter on the left) hides its text until a
  // scroll brings it fully into view. Its line, flag or clip stays drawn.
  function fitLabels() {
    var sc = $("scroller"); if (!sc || sc.hidden) return;
    var r = sc.getBoundingClientRect(), left = r.left + GUTTER, right = r.right - 1;
    sc.querySelectorAll(".clip .label").forEach(function (l) {
      var role = l.querySelector(".role");
      if (role && l.scrollWidth > l.clientWidth + 0.5) role.remove();
      l.classList.toggle("unfit", l.scrollWidth > l.clientWidth + 0.5);
    });
    sc.querySelectorAll(".tick, .mlabel:not(.crowded), .gap, .clip .label:not(.unfit)").forEach(function (e) {
      // The TEXT's extent, not the element's: a clip label's block is as wide
      // as its clip, which may run past the view while the name itself is in it.
      var range = document.createRange(); range.selectNodeContents(e);
      var b = range.getBoundingClientRect(), own = e.scrollWidth > e.clientWidth + 0.5 && !e.classList.contains("tick") && !e.classList.contains("mlabel");
      e.classList.toggle("unfit", own || b.left < left - 0.5 || b.right > right + 0.5);
    });
    placeNumbers(left, right);
  }
  // Every clip is numbered (record order: start, spine first, then lanes
  // from the lowest) and the key under the timeline names every number. The
  // number sits inside the box, top right, when it fits there clear of the
  // name; otherwise just outside it on a leader, in a strip its lane opens:
  // above the box, or below it for a lane under the spine. Outside numbers
  // never overlap each other. A number outside the visible area is hidden
  // there (never cut), and is still in the key.
  function placeNumbers(left, right) {
    var sc = $("scroller");
    if (DIR) { var k0 = $("key"); if (k0) k0.hidden = true; return; }  // the directions name clips inline or on a rail
    sc.querySelectorAll(".onum").forEach(function (e) { e.remove(); });
    var outside = [];
    sc.querySelectorAll(".clip").forEach(function (c) {
      var num = c.querySelector(".num"); if (!num) return;
      var lab = c.querySelector(".label"), named = lab && !lab.classList.contains("unfit");
      var nb = num.getBoundingClientRect(), cb = c.getBoundingClientRect();
      var inside = nb.left >= cb.left + 1 && nb.right <= cb.right - 1;
      if (inside && named) {
        var rg = document.createRange(); rg.selectNodeContents(lab); var tb = rg.getBoundingClientRect();
        if (tb.right > nb.left - 3 && tb.left < nb.right && tb.bottom > nb.top && tb.top < nb.bottom) inside = false;
      }
      num.classList.toggle("on", inside && nb.left >= left - 0.5 && nb.right <= right + 0.5);
      if (!inside) outside.push(c);
    });
    var rows = new Set();
    outside.forEach(function (c) { rows.add(c.parentNode); });
    sc.querySelectorAll(".lane").forEach(function (row) { row.classList.toggle("has-out", rows.has(row)); });
    var lastRight = {};
    outside.sort(function (a, b) { return a.offsetLeft - b.offsetLeft; }).forEach(function (c) {
      var row = c.parentNode, n = c.querySelector(".num").textContent;
      var badge = el("div", "onum", n), lead = el("div", "lead");
      badge.appendChild(lead); badge.dataset.for = n; row.appendChild(badge);
      var centre = c.offsetLeft + c.offsetWidth / 2, bw = badge.offsetWidth;
      // never under the sticky lane tags at the left, never on the previous number
      var at = Math.max(centre - bw / 2, GUTTER + 2, (lastRight[row.dataset.lane] || -1e9) + 3);
      badge.style.left = at + "px";
      lead.style.left = (centre - at) + "px";
      lastRight[row.dataset.lane] = at + bw;
      var bb = badge.getBoundingClientRect();
      badge.style.visibility = bb.left >= left - 0.5 && bb.right <= right + 0.5 ? "visible" : "hidden";
    });
    var key = $("key");
    if (key) {
      key.textContent = "";
      var all = [];
      sc.querySelectorAll(".clip").forEach(function (c) {
        var num = c.querySelector(".num"); if (num && c.__c) all.push([Number(num.textContent), c.__c.name]);
      });
      all.sort(function (a, b) { return a[0] - b[0]; }).forEach(function (k) {
        var sp = el("span"); sp.appendChild(el("b", null, String(k[0]))); sp.appendChild(document.createTextNode(k[1])); key.appendChild(sp);
      });
      key.hidden = all.length === 0;
    }
  }

  // ---- 0.33 directions ------------------------------------------------------
  // A label is measured in the font it is drawn in (a hidden meter span with
  // the same class), never guessed from a character count.
  function textW(s, kind) {
    var m = $("meter"); if (!m) { m = el("span"); m.id = "meter"; document.body.appendChild(m); }
    m.className = "meter " + kind; m.textContent = s; return m.getBoundingClientRect().width;
  }
  var PAD = { a: 10, b: 16 };
  // The name, whole, or cut in the middle so its ending survives ("SHOT…A 1"),
  // keeping at least KEEP_MIN of its characters; null when even that won't fit.
  function middleFit(name, room, kind) {
    if (textW(name, kind) <= room) return { text: name, kept: name.length };
    for (var k = name.length - 1; k >= KEEP_MIN; k--) {
      var tail = Math.ceil(k / 2), head = name.slice(0, k - tail).replace(/\s+$/, ""), end = name.slice(name.length - tail).replace(/^\s+/, "");
      var t = head + "…" + end;
      if (head.length + end.length >= KEEP_MIN && textW(t, kind) <= room) return { text: t, kept: head.length + end.length };
    }
    return null;
  }
  // A connected clip is named inside only when its WHOLE name fits; else on
  // its lane's rail. A storyline clip is named inside, cut in the middle.
  function insideName(c) {
    var wpx = w(frac(c.duration)) - 1;
    if (wpx < LABEL_MIN) return null;
    var room = wpx - PAD[DIR];
    if (c.lane) return textW(c.name, "label") <= room ? { text: c.name, kept: c.name.length } : null;
    return middleFit(c.name, room, "label");
  }
  function roleOf(c) {
    var r = String(c.role || "").split(".")[0].toLowerCase();
    if (["video", "titles", "dialogue", "music", "effects"].indexOf(r) >= 0) return r;
    return c.kind === "audio" ? "dialogue" : c.kind === "title" ? "titles" : "video";
  }
  function glyphOf(type) {
    var t = String(type || "").toLowerCase();
    return t === "chapter" ? "chapter" : (t === "todo" || t === "incomplete") ? "todo" : t === "completed" ? "completed" : "standard";
  }
  function waveEl(levels) {
    var n = levels.length, top = [], bottom = [];
    levels.forEach(function (v, i) {
      if (DIR === "a") { top.push(i + "," + (50 - v / 2)); bottom.unshift(i + "," + (50 + v / 2)); }  // centred, as Resolve draws it
      else { top.push(i + "," + (100 - v)); }                                                         // up from the bottom, as Final Cut does
    });
    var d = "M" + (DIR === "a" ? top.concat(bottom) : ["0,100"].concat(top, [(n - 1) + ",100"])).join(" L") + " Z";
    // built by the HTML parser (numbers only), which gives it the SVG namespace
    var holder = el("div");
    holder.innerHTML = '<svg class="wave" viewBox="0 0 ' + n + ' 100" preserveAspectRatio="none"><path d="' + d + '"></path></svg>';
    return holder.firstChild;
  }
  function clipElDir(c) {
    var thumbed = DIR === "b" && !!(c.thumb && c.thumb.indexOf("data:image/") === 0);
    var e = el("div", "clip k-" + c.kind + (thumbed ? " has-thumb" : "") + (state.sel === c.id ? " selected" : ""));
    e.style.left = x(frac(c.start)) + "px"; e.style.width = Math.max(3, w(frac(c.duration)) - 1) + "px";
    e.title = c.name + " · " + secOfSeconds(frac(c.duration)); e.__c = c;
    if (thumbed) e.appendChild(thumbsOf(c));
    if (c.wave && c.wave.length) e.appendChild(waveEl(c.wave));
    e.appendChild(el("div", "stripe role-" + roleOf(c)));
    (c.markers || []).forEach(function (m) { var mk = el("div", "mk"); mk.style.left = w(frac(m.start) - frac(c.start)) + "px"; mk.title = m.name; e.appendChild(mk); });
    var name = insideName(c);
    if (name) { var lab = el("div", "label", name.text); lab.dataset.kept = String(name.kept); e.appendChild(lab); }
    e.addEventListener("click", function (ev) { ev.stopPropagation(); select(c.id); state.playhead = frac(c.start); placePlayhead(); });
    return e;
  }
  // Names of the clips on one lane that are not named inside: whole, on two
  // rows at most, each at its clip's start with a leader down (or up) to it.
  // A name with no room on either row is left to the tap sheet (the
  // selection panel), never squeezed under KEEP_MIN characters.
  function railFor(clips, lane) {
    var rail = el("div", "rail" + (lane < 0 ? " below" : "")), rowEnd = [-1e9, -1e9], used = 0;
    rail.dataset.lane = "rail " + lane;
    clips.slice().sort(function (a, b) { return frac(a.start) - frac(b.start); }).forEach(function (c) {
      var cx = x(frac(c.start)), need = textW(c.name, "rl") + 9;
      var row = cx > rowEnd[0] + 4 ? 0 : cx > rowEnd[1] + 4 ? 1 : -1;
      if (row < 0) return;
      var r = el("div", "rl" + (row ? " r1" : ""), c.name); r.style.left = cx + "px"; r.dataset.kept = String(c.name.length); r.title = c.name;
      r.addEventListener("click", function (ev) { ev.stopPropagation(); select(c.id); });
      rail.appendChild(r); rowEnd[row] = cx + need; used = Math.max(used, row + 1);
    });
    if (used > 1) rail.classList.add("rows2");
    return used ? rail : null;
  }
  function drawDir() {
    var p = state.payload, canvas = $("canvas"); canvas.textContent = "";
    var dur = extent(), width = Math.max(x(dur) + 24, $("scroller").clientWidth - 2);
    canvas.style.width = width + "px";
    var ruler = el("div", "ruler");
    canvas.appendChild(ruler);
    // Labelled ticks on whole timecode seconds; between them, ticks on frame
    // multiples (the finest at least 4 px apart in A, 6 in B), a second taller.
    var step = niceStep(dur, width - GUTTER), nom = p.nominal_fps || 24, perSec = nom / (frac(p.fps) || 24);
    for (var S = 0; S * perSec <= dur + 1e-9; S += step) {
      var t = S * perSec, k = el("div", "tick", tcOf(t)); k.style.left = x(t) + "px"; k.__key = "t:" + S; ruler.appendChild(k);
    }
    var fpx = state.pps * perSec / nom, gapPx = DIR === "a" ? 4 : 6;
    var sub = [1, 2, 3, 4, 6, 8, 12, 24, 48, 120, 240, 600, 1440].filter(function (n) { return n * fpx >= gapPx; })[0] || 1440;
    for (var F = sub; F / nom * perSec <= dur + 1e-9; F += sub) {
      if (F % (step * nom) === 0) continue;
      var ft = el("div", "ftick" + (F % nom === 0 ? " sec" : "")); ft.style.left = x(F / nom * perSec) + "px"; ruler.appendChild(ft);
    }
    ruler.dataset.frames = String(sub);
    var rowEnd = [-1, -1], named = p.markers.slice();
    p.clips.forEach(function (c) { (c.markers || []).forEach(function (m) { named.push(m); }); });
    named.sort(function (a, b) { return frac(a.start) - frac(b.start); });
    named.forEach(function (m) {
      var left = x(frac(m.start));
      var lab = el("div", "mlabel", m.name); lab.style.left = left + "px"; lab.title = m.name + " (" + glyphOf(m.type) + ")";
      lab.insertBefore(el("span", "mg " + glyphOf(m.type)), lab.firstChild); lab.dataset.kind = glyphOf(m.type);
      ruler.appendChild(lab);
      var row = left > rowEnd[0] + 4 ? 0 : (left > rowEnd[1] + 4 ? 1 : -1);
      if (row === 1) lab.classList.add("row1");
      if (row < 0) { lab.classList.add("unfit", "crowded"); return; }
      rowEnd[row] = left + lab.offsetWidth;
    });
    ruler.addEventListener("mousedown", rulerDown);
    var byId = {}; p.clips.forEach(function (c) { byId[c.id] = c; });
    var lanes = p.lanes.slice();
    // B draws the primary storyline even when it is one gap, so every
    // connected clip has a line to what it hangs from
    if (DIR === "b" && !lanes.some(function (l) { return l.lane === 0; })) {
      var at = lanes.findIndex(function (l) { return l.lane < 0; });
      lanes.splice(at < 0 ? lanes.length : at, 0, { lane: 0, kind: "spine", clips: [] });
    }
    var rows = {};
    lanes.forEach(function (lane) {
      var here = lane.clips.map(function (id) { return byId[id]; });
      var rail = lane.lane ? railFor(here.filter(function (c) { return !insideName(c); }), lane.lane) : null;
      if (rail && lane.lane > 0) canvas.appendChild(rail);
      var row = el("div", "lane " + lane.kind + (lane.lane < 0 ? " below" : "")); row.dataset.lane = String(lane.lane);
      var tag = lane.lane > 0 ? "V" + lane.lane : lane.lane < 0 ? "A" + (-lane.lane) : "";
      row.appendChild(el("span", "lane-tag", DIR === "a" ? tag : ""));
      if (lane.kind === "spine") p.gaps.forEach(function (g) {
        var ge = el("div", "gap", (DIR === "b" ? "Gap " : "gap ") + secOfSeconds(frac(g.duration)));
        ge.style.left = x(frac(g.start)) + "px"; ge.style.width = Math.max(2, w(frac(g.duration))) + "px"; row.appendChild(ge);
      });
      here.forEach(function (c) { row.appendChild(clipElDir(c)); });
      canvas.appendChild(row); rows[lane.lane] = row;
      if (rail && lane.lane < 0) canvas.appendChild(rail);
    });
    var marks = el("div", "markers");
    p.markers.forEach(function (m) { var me = el("div", "tmark"); me.style.left = x(frac(m.start)) + "px"; me.title = m.name; marks.appendChild(me); });
    canvas.appendChild(marks);
    if (DIR === "b" && rows[0]) {
      // the connection line: from each connected clip's start to the storyline
      var s0 = rows[0].offsetTop, gapTop = s0 + 8, gapBottom = s0 + rows[0].offsetHeight - 8;
      canvas.querySelectorAll(".lane .clip").forEach(function (ce) {
        var lane = Number(ce.parentNode.dataset.lane); if (!lane) return;
        var top = ce.parentNode.offsetTop + ce.offsetTop, bottom = top + ce.offsetHeight, line = el("div", "conn");
        line.style.left = (ce.offsetLeft + 4) + "px";
        if (lane > 0) { line.classList.add("down"); line.style.top = bottom + "px"; line.style.height = Math.max(0, gapTop - bottom) + "px"; }
        else { line.classList.add("up"); line.style.top = gapBottom + "px"; line.style.height = Math.max(0, top - gapBottom) + "px"; }
        canvas.appendChild(line);
      });
    }
    var ph = el("div", "playhead"); ph.id = "playhead"; ph.appendChild(el("div", "phead")); canvas.appendChild(ph);
    var rg = el("div", "range"); rg.id = "range"; rg.hidden = true; canvas.appendChild(rg);
    placePlayhead();
    fitLabels();
    drawOverview();
  }
  // The whole cut, always: every lane as a thin row, the markers, and a box
  // round the part the tracks show. A click there scrolls the tracks to it.
  function drawOverview() {
    var p = state.payload, sc = $("scroller"), ov = $("overview");
    if (!ov) {
      ov = el("div"); ov.id = "overview"; sc.parentNode.insertBefore(ov, sc);
      ov.addEventListener("click", function (ev) {
        var r = ov.getBoundingClientRect(), t = (ev.clientX - r.left - 4) / (r.width - 8) * extent();
        sc.scrollLeft = Math.max(0, x(t) - GUTTER - (sc.clientWidth - GUTTER) / 2);
      });
    }
    ov.textContent = "";
    var W = ov.clientWidth, H = ov.clientHeight, dur = extent(), lanes = p.lanes;
    var sx = function (t) { return 4 + t / dur * (W - 8); }, rowH = (H - 6) / Math.max(1, lanes.length);
    var byId = {}; p.clips.forEach(function (c) { byId[c.id] = c; });
    lanes.forEach(function (lane, i) {
      lane.clips.forEach(function (id) {
        var c = byId[id], b = el("div", "ob k-" + c.kind);
        b.style.left = sx(frac(c.start)) + "px"; b.style.width = Math.max(1.5, sx(frac(c.end)) - sx(frac(c.start)) - 0.5) + "px";
        b.style.top = (3 + i * rowH) + "px"; b.style.height = Math.max(2, rowH - 2) + "px"; ov.appendChild(b);
      });
    });
    var named = p.markers.slice(); p.clips.forEach(function (c) { (c.markers || []).forEach(function (m) { named.push(m); }); });
    named.forEach(function (m) { var o = el("div", "om"); o.style.left = sx(frac(m.start)) + "px"; ov.appendChild(o); });
    var box = el("div", "ov"); box.id = "ovbox"; ov.appendChild(box);
    syncOverview();
  }
  function syncOverview() {
    var box = $("ovbox"), sc = $("scroller"), ov = $("overview"); if (!DIR || !box || !state.payload) return;
    var W = ov.clientWidth, dur = extent(), sx = function (t) { return 4 + t / dur * (W - 8); };
    var t0 = sc.scrollLeft / state.pps, t1 = Math.min(dur, (sc.scrollLeft + sc.clientWidth - GUTTER) / state.pps);
    box.style.left = sx(t0) + "px"; box.style.width = Math.max(6, sx(t1) - sx(t0)) + "px";
  }
  // The only CSS image the shell sets: a data: thumbnail (the caller checks it is one).
  function thumbsOf(c) { var th = el("div", "thumbs"); th.style.backgroundImage = 'url("' + c.thumb + '")'; return th; }
  function clipEl(c) {
    var thumbed = !!(c.thumb && c.thumb.indexOf("data:image/") === 0);
    var e = el("div", "clip" + (c.kind === "title" ? " title" : "") + (thumbed ? " has-thumb" : "") + (state.sel === c.id ? " selected" : ""));
    e.style.left = x(frac(c.start)) + "px"; e.style.width = Math.max(3, w(frac(c.duration)) - 1) + "px";
    e.title = c.name + " · " + secOfSeconds(frac(c.duration)); e.__c = c;
    if (thumbed) e.appendChild(thumbsOf(c));
    (c.markers || []).forEach(function (m) { var mk = el("div", "mk"); mk.style.left = w(frac(m.start) - frac(c.start)) + "px"; mk.title = m.name; mk.__key = "k:" + (c.media || c.name) + "@" + m.start; e.appendChild(mk); });
    var wpx = w(frac(c.duration));
    if (wpx >= 20) { var lab = el("div", "label", c.name); if (c.role && wpx >= 90) lab.appendChild(el("span", "role", c.role)); e.appendChild(lab); }
    if (state.num && state.num[c.id]) e.appendChild(el("div", "num", String(state.num[c.id])));
    e.addEventListener("click", function (ev) { ev.stopPropagation(); select(c.id); state.playhead = frac(c.start); placePlayhead(); });
    return e;
  }
  function niceStep(dur, width) {
    var target = 110 / (width / dur), steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    for (var i = 0; i < steps.length; i++) if (steps[i] >= target) return steps[i];
    return 600;
  }
  function coverTicks() {
    var ph = $("playhead"); if (!ph) return;
    var parts = [ph.getBoundingClientRect()], head = ph.querySelector(".phead");
    if (head) parts.push(head.getBoundingClientRect());
    document.querySelectorAll("#canvas .tick").forEach(function (t) {
      var r = document.createRange(); r.selectNodeContents(t); var b = r.getBoundingClientRect();
      t.classList.toggle("under-playhead", b.width > 0 && parts.some(function (q) {
        return q.left < b.right + 1 && b.left < q.right + 1 && q.top < b.bottom && b.top < q.bottom; }));
    });
  }
  function placePlayhead() { var ph = $("playhead"); if (ph) ph.style.left = x(state.playhead) + "px"; $("tc").textContent = tcOf(state.playhead); coverTicks();
    var rg = $("range"); if (rg) { if (state.range) { rg.hidden = false; rg.style.left = x(state.range[0]) + "px"; rg.style.width = w(state.range[1] - state.range[0]) + "px"; } else rg.hidden = true; } }
  function rulerDown(ev) {
    var rect = $("canvas").getBoundingClientRect(), start = clamp((ev.clientX - rect.left - GUTTER) / state.pps), moved = false;
    state.playhead = start; state.range = null; placePlayhead();
    function move(e) { var now = clamp((e.clientX - rect.left - GUTTER) / state.pps); if (Math.abs(now - start) > 0.05) { moved = true; state.range = [Math.min(start, now), Math.max(start, now)]; state.playhead = now; placePlayhead(); } }
    function up() { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); if (!moved) state.range = null; updateSel(); }
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
  }
  function clamp(s) { return Math.max(0, Math.min(frac(state.payload.duration), s)); }
  function select(id) { state.sel = id; draw(); updateSel(); }
  function clipById(id) { var out = null; state.payload.clips.forEach(function (c) { if (c.id === id) out = c; }); return out; }

  // ---- selection panel + actions -----------------------------------------
  function updateSel() {
    var sel = $("sel"), acts = $("acts"); sel.textContent = ""; acts.textContent = "";
    var p = state.payload, file = p.file.path, c = state.sel ? clipById(state.sel) : null;
    if (c) {
      var dl = document.createElement("dl");
      [["clip", c.name], ["lane", c.kind === "spine" ? "spine" : String(c.lane)], ["in", tcOf(frac(c.start))], ["out", tcOf(frac(c.end))],
       ["duration", secOfSeconds(frac(c.duration)) + " (" + c.duration + ")"], ["source in", c.source_start], ["role", c.role || "—"],
       ["media", c.media || "—"], ["markers", String((c.markers || []).length)], ["words", String((c.words || []).length)]]
        .forEach(function (r) { dl.appendChild(el("dt", null, r[0])); dl.appendChild(el("dd", null, r[1])); });
      sel.appendChild(dl);
      if ((c.words || []).length) sel.appendChild(el("div", null, "“" + c.words.map(function (w) { return w.w; }).join(" ").slice(0, 240) + "”"));
    } else if (state.range) {
      sel.appendChild(el("div", null, "Range " + tcOf(state.range[0]) + " → " + tcOf(state.range[1]) + " (" + secOfSeconds(state.range[1] - state.range[0]) + ")"));
    } else {
      sel.appendChild(el("div", null, "Click a clip. Drag on the ruler to select a range. Playhead " + tcOf(state.playhead) + "."));
    }
    var ok = state.inHost;
    var inspect = btn("Inspect clip", function () { callTool("view", { action: "view_clip", args: { filepath: file, clip_name: c.name } }); }, !!c && ok);
    var similar = btn("Find similar shots", function () {
      var q = (c.words || []).map(function (w) { return w.w; }).join(" ").slice(0, 120) || c.name;
      callTool("find", { action: "find_shots", args: { filepath: file, query: q, limit: 8 } });
    }, !!c && ok);
    var name = document.createElement("input"); name.type = "text"; name.placeholder = "marker name"; name.value = c ? c.name : "";
    var mark = btn("Add marker at playhead", function () {
      edit("mark", { action: "add_marker", args: { filepath: file, timecode: tcOf(state.playhead), name: name.value || "marker", expected_sha256: p.file.sha256 } });
    }, ok, true);
    var del = btn("Delete clip", function () {
      edit("edit", { action: "delete_clips", args: { filepath: file, clip_ids: [c.name], expected_sha256: p.file.sha256 } });
    }, !!c && c.kind === "spine" && ok);
    [inspect, similar, name, mark, del].forEach(function (b) { acts.appendChild(b); });
    acts.classList.toggle("readonly", !ok);
    if (!ok) {
      [inspect, similar, mark, del].forEach(function (b) { b.setAttribute("aria-disabled", "true"); b.title = "read only here"; });
      name.disabled = true; name.setAttribute("aria-disabled", "true");
      acts.appendChild(el("div", "readonly-note", "Read only: these edits run when the view tool's timeline is open in an MCP Apps host."));
    }
  }
  function btn(label, fn, enabled, primary) { var b = el("button", primary ? "primary" : null, label); b.disabled = !enabled; b.addEventListener("click", fn); return b; }
  function edit(tool, args) {
    callTool(tool, args).then(function (text) {
      var m = /Saved to:\s*`?([^`\n]+)`?/.exec(text);
      if (!m) { log("edit returned no output path; not re-rendering"); return; }
      return callTool("view", { action: "view_timeline", args: { filepath: m[1].trim() } }).then(function (t2) {
        var pl = extract(t2); if (pl) { setPayload(pl); log("re-rendered from " + pl.file.path); }
      });
    }).catch(function () {});
  }
  function log(s) { var l = $("log"); l.textContent = (s + "\n" + l.textContent).slice(0, 6000); }
  $("zin").addEventListener("click", function () { state.pps *= 1.5; draw(); });
  $("zout").addEventListener("click", function () { state.pps = Math.max(2, state.pps / 1.5); draw(); });
  // Redraw on a WIDTH change only: hosts resize the frame to its content's
  // height after every render, and a redraw then would cut short the motion.
  var drawnWidth = window.innerWidth;
  window.addEventListener("resize", function () {
    if (!state.payload || window.innerWidth === drawnWidth) return;
    drawnWidth = window.innerWidth; draw();
  });
  $("scroller").addEventListener("scroll", function () { fitLabels(); syncOverview(); });
  window.__fcpApp = { setPayload: setPayload, direction: DIR, extract: extract, state: state, fitLabels: fitLabels, motion: MOTION,
                      revealWhenWide: revealWhenWide, hideWhenNarrow: hideWhenNarrow, timeForProgress: timeForProgress,
                      placePlayhead: placePlayhead };

  // ---- boot --------------------------------------------------------------
  state.inHost = window.parent && window.parent !== window;
  // preview:// serves this same shell with the timeline inlined (0.32): drawn
  // at once, with no host and no fetch.
  var inline = document.getElementById("fcp-inline-payload");
  if (state.inHost) {
    request("ui/initialize", { appInfo: { name: "fcp-mcp-server timeline", version: "1" }, appCapabilities: {}, protocolVersion: "2025-11-21" })
      .then(function (res) { applyHostContext((res && res.hostContext) || null); notify("ui/notifications/initialized"); log("host initialized"); },
            function (err) { log("ui/initialize failed: " + err.message); });
  } else if (!inline) {
    log("not inside an MCP Apps host; waiting for a payload via window.__fcpApp.setPayload()");
  }
  if (inline) {
    try {
      setPayload(JSON.parse(inline.textContent));
      log(state.inHost ? "timeline inlined by preview://"
                       : "timeline inlined by preview://; read only here (edits run inside an MCP Apps host)");
    }
    catch (err) { log("inlined timeline could not be read: " + err.message); }
  }
})();
</script>
</body>
</html>
"""

# Colours come from fcpxml/palette.py and nowhere else (tests/test_palette.py).
_APP_HTML = _APP_TEMPLATE.replace("/*PALETTE*/", palette.css_root())
