"""The timeline app — an MCP Apps view of the cut, rendered by the host.

One ``ui://`` resource (the app shell, served once as
``text/html;profile=mcp-app``) and a JSON payload the ``view`` tool returns on
every call. Since 0.32 ``preview://`` serves the same shell with the payload
inlined (:func:`render_page_html`), so there is one timeline picture. Since
0.34 there is one LOOK too: open, like Final Cut's timeline (James picked
direction B on 2026-11-15; direction A and the flag that chose between them
are gone). The host renders the shell in a sandboxed iframe, posts each tool
result into it, and the app draws lanes, clips, markers, gaps, waveforms,
transcript words and thumbnails from the payload alone. The app calls back
into the server through the host (``tools/call``) for reads and for edits, so
every edit still goes through the groups, the journal and the review gate —
the app owns no write path.

Three rules this file keeps:

* **Time is rational on the wire.** Every position and duration in the
  payload is an exact ``"numerator/denominator"`` string straight from the
  model's Fractions. A float at 23.976 drifts visibly over a two-minute
  timeline; a rational does not, and it round-trips the fixtures exactly,
  which is what the tests assert.
* **The shell is self-contained.** No CDN, no fetch, no external URL of any
  kind. The host's CSP for an app resource is strict, and a shell that
  reaches out would render blank with nothing to say why.
* **Pictures never ride in the text (0.34).** The payload travels as the tool
  result's ``structuredContent``, which the ext-apps spec hands to the app and
  keeps out of the model's context; the text block is a short summary a
  client without Apps can read (:func:`fallback_text`). Until 0.33 the whole
  payload, base64 thumbnails and all, was fenced into the text: 11,184
  characters for a five-clip cut.

Thumbnails, transcripts and waveforms are injected as callables rather than
computed here, so the builder is pure: the tests round-trip the fixtures with
no ffmpeg and no cache, and the handler in ``tools/view.py`` supplies the real
readers.
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
# Since 0.34 thumbnails travel in structuredContent, never in the text, so
# the cap is about the host's message size, not the model's context: every
# clip of the 18-minute interview fixture (58) gets its picture.
MAX_THUMBNAILS = 64
THUMBNAIL_SHORT_SIDE = 180  # 2x the 88 px clip a filmstrip tile fills (0.34): sharp on a 2x display
THUMBNAIL_QUALITY = 5       # ffmpeg -q:v: about 8.5 KB a 320x180 frame
MAX_THUMBNAIL_BYTES = 600_000
MAX_WORDS = 3000

# The tool result carries the payload between these fences. The app looks
# for them in every text block it is handed; a client without Apps support
# sees a summary followed by a JSON block, which is still an honest answer.
PAYLOAD_OPEN = "```json fcp-timeline-app"
PAYLOAD_CLOSE = "```"

ThumbnailFn = Callable[[str, Fraction], Optional[str]]
TranscriptFn = Callable[[str], Optional[dict]]
WaveformFn = Callable[[str, Fraction, Fraction], Optional[list]]
# A waveform travels as a short array: levels 0-100 (-60..0 dBFS peak), ten
# a second (round 4: four a second drew a 164 s bed as 18 px blocks once the
# track opens on the picture), at most this many per clip, evenly over its
# length.
MAX_WAVE_POINTS = 1200
WAVE_POINTS_PER_SECOND = 10

# Byte budgets the tests hold (tests/test_app_glance.py), each broken on
# purpose once: the text a client without Apps sees for the five-clip
# fixture, and the structured payload the app draws from (thumbnails are
# capped by MAX_THUMBNAIL_BYTES above; the rest is clip rows and waveforms).
FALLBACK_TEXT_BUDGET = 2000
PAYLOAD_BUDGET = 700_000
FALLBACK_CLIP_LINES = 12  # clips listed in the fallback text; the rest are counted


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
    a clip with sound (drawn under its filmstrip, as Final Cut does), or
    None. None is ever called more than the bounds above allow.
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
            # enabled="0": shown greyed and marked off, never hidden
            "enabled": bool(getattr(item, "enabled", True)),
        }
        sound = row["kind"] == "audio" or bool(getattr(getattr(item, "media", None), "has_audio", False))
        if waveform is not None and media and sound:
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

    # Top to bottom as Final Cut stacks them: the highest lane first, the
    # storyline, then lane -1 directly under it and -2 under that (until 0.34
    # the lanes under the storyline came out -2 before -1).
    ordered = sorted((lane for lane in lanes_seen if lane > 0), reverse=True)
    ordered += [0] if 0 in lanes_seen or not timeline.connected_clips else []
    ordered += sorted((lane for lane in lanes_seen if lane < 0), reverse=True)

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
            {"name": t.name, "start": rational(_frac(t.start) - origin), "duration": rational(t.duration),
             "lane": 0, "lead": rational(t.lead) if getattr(t, "lead", None) is not None else None}
            for t in timeline.transitions
        ] + [
            # inside a connected storyline: already on the sequence clock,
            # like a connected clip's timeline_start; "start" is the cut and
            # "lead" how much of it runs before
            {"name": t.name, "start": rational(_frac(t.start)), "duration": rational(t.duration),
             "lane": t.lane, "lead": rational(t.lead) if t.lead is not None else None}
            for s in getattr(timeline, "storylines", None) or [] for t in s.transitions
        ],
        "flashes": _picture_flashes(timeline),
    }


def _picture_flashes(timeline) -> list:
    """Runs shorter than six frames in the composited PICTURE (connected
    clips included), the reading diagnose fails on: a 1f run is a FAIL (the
    0.33 directions draw it red), up to 5f a WARN. Pure: the filtergraph is
    compiled, nothing is rendered. [] when the timeline cannot be compiled."""
    try:
        from . import filtergraph as _fg
        from . import proofs as _pf

        runs = _pf.picture_runs(_fg.compile_timeline(timeline))
    except Exception:  # noqa: BLE001 - the payload never fails over a QC reading
        return []
    out = []
    for i, (a, b, layers) in enumerate(runs):
        if b - a < 6 and layers and 0 < i < len(runs) - 1:
            out.append({"start": rational(Fraction(a, _fg.FPS)), "duration": rational(Fraction(b - a, _fg.FPS)),
                        "frames": b - a, "verdict": "FAIL" if b - a < 2 else "WARN",
                        "shows": _pf.picture_name(layers),
                        "between": [_pf.picture_name(runs[i - 1][2]), _pf.picture_name(runs[i + 1][2])]})
    return out


def wrap_payload(summary: str, payload: dict) -> str:
    """A summary and the fenced payload in one text (until 0.33 the view
    tool's reply). Kept for a host that hands the app text only: the app
    still reads a fenced payload when there is no ``structuredContent``."""
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


def payload_of(reply) -> Optional[dict]:
    """The payload a ``view_timeline`` reply carries: its structured content
    (0.34), else a payload fenced in its text (a reply from before 0.34)."""
    structured = getattr(reply, "structured", None)
    if structured is not None:
        return structured
    for block in reply or []:
        found = extract_payload(getattr(block, "text", "") or "")
        if found is not None:
            return found
    return None


def payload_bytes(payload: dict) -> int:
    """The payload's size on the wire (compact JSON, UTF-8)."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _timecode(seconds: Fraction, fps: Fraction, nominal: int) -> str:
    """Non-drop timecode of NOMINAL frames, as the app's ruler prints it."""
    total = int(seconds * fps + Fraction(1, 2))
    frames, secs = total % nominal, total // nominal
    return f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}:{frames:02d}"


def fallback_text(summary: str, payload: dict, *, limit: int = FALLBACK_CLIP_LINES) -> str:
    """What a client without MCP Apps reads (0.34): the summary, then the
    clips in record order, one short line each (start, length, lane, name),
    at most ``limit`` of them and the rest counted. No thumbnail, waveform or
    JSON: the app gets those as ``structuredContent``."""
    fps, nominal = Fraction(payload["fps"]), int(payload.get("nominal_fps") or 24)
    rows = sorted(payload["clips"], key=lambda c: (Fraction(c["start"]), bool(c["lane"]), c["lane"]))
    lines = [summary, "", "Clips in record order (start, length, lane):"]
    for i, c in enumerate(rows[:limit], 1):
        where = "spine" if c["kind"] == "spine" else f"lane {c['lane']}"
        off = " (off)" if c.get("enabled") is False else ""
        lines.append(f"{i}. {_timecode(Fraction(c['start']), fps, nominal)} "
                     f"{float(Fraction(c['duration'])):.2f}s {where} {c['name']}{off}")
    if len(rows) > limit:
        lines.append(f"...and {len(rows) - limit} more (view_clip reads one by name).")
    return "\n".join(lines)


def data_uri_jpeg(path: str) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------

def render_page_html(payload: dict) -> str:
    """The ``preview://`` page (0.32): the same shell with ``payload`` inlined
    as a JSON script block, so a browser or a host that only shows HTML draws
    the timeline at once. ``<`` is escaped inside the JSON, so a clip named
    ``</script>`` stays data. It follows ``prefers-color-scheme``; there is no
    host to send a theme."""
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    block = f'<script type="application/json" id="fcp-inline-payload">{data}</script>\n'
    marker = "<script>"
    i = _APP_HTML.rindex(marker)
    return _APP_HTML[:i] + block + _APP_HTML[i:]


def render_app_html() -> str:
    """The ``ui://fcp/timeline`` resource. Self-contained; takes no payload.

    The postMessage protocol it speaks is the MCP Apps spec
    (``modelcontextprotocol/ext-apps``, specification/draft/apps.mdx):
    ``ui/initialize`` → ``ui/notifications/initialized``, then the host posts
    ``ui/notifications/tool-input`` / ``ui/notifications/tool-result`` (the
    CallToolResult: the payload is its ``structuredContent``) and
    ``ui/notifications/host-context-changed`` (the theme), and the app calls
    ``tools/call``. The Python SDK only ships the server half, so the
    in-iframe half is held by tests/test_app_glance.py's stand-in host.
    Outside an iframe the shell waits with a message instead of pretending.
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
  /* Clip heights by lane type (0.34): picture lanes 88 px, titles and sound
     72 px on a desktop; every clip 56 px on a phone. A lane is its clip plus
     4 px above and below. */
  :root { --clip-h: 88px; --clip-h-short: 72px; }
  @media (max-width: 600px) { :root { --clip-h: 56px; --clip-h-short: 56px; } }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--ground); color: var(--ink);
    font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  body { padding: 12px 16px 16px; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; margin-bottom: 8px; }
  header h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: .01em; }
  header .meta { color: var(--mute); font-variant-numeric: tabular-nums; }
  header .tc { margin-left: auto; font: 600 14px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--accent); letter-spacing: .04em; }
  #empty { color: var(--mute); padding: 40px 0; text-align: center; }
  /* The overview: a minimap of the whole cut, always (0.34 round 4). Its top
     band holds a 1 px tick per marker, the band under it the flash, and
     under those every lane is a row of bars in its clips' role colours
     (Final Cut's, 3:1 or better on the panel in both themes), with a window
     round the part the tracks show; a click scrolls the tracks there. */
  #overview { position: relative; height: 48px; margin-bottom: 6px; border: 1px solid var(--line); border-radius: 8px;
    background: var(--panel); overflow: hidden; cursor: pointer; }
  #overview .ob { position: absolute; border-radius: 1px; }
  #overview .ob.role-video { background: var(--role-video); }
  #overview .ob.role-titles { background: var(--role-titles); }
  #overview .ob.role-dialogue { background: var(--role-dialogue); }
  #overview .ob.role-music { background: var(--role-music); }
  #overview .ob.role-effects { background: var(--role-effects); }
  #overview .om { position: absolute; top: 3px; width: 1px; height: 7px; background: var(--accent); }
  #overview .of { position: absolute; top: 12px; width: 2px; height: 7px; background: var(--danger); }
  #overview .ov { position: absolute; top: 21px; bottom: 2px; border: 1px solid var(--accent); background: var(--accent-wash);
    border-radius: 3px; pointer-events: none; }
  #scroller { position: relative; overflow-x: auto; overflow-y: hidden; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel); }
  /* The strip above the ruler: what a tap selected (a clip's name, start and
     length; a marker; a cluster's markers). Nothing floats over the lanes. */
  #strip { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; min-height: 26px; margin-bottom: 6px;
    padding: 4px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); font-size: 12px; color: var(--mute); }
  #strip #sinfo { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; min-width: 0; }
  #strip #count { margin-left: auto; white-space: nowrap; color: var(--mute); }
  #strip b { color: var(--ink); font-weight: 600; }
  .rec { color: var(--accent); font-variant-numeric: tabular-nums; }
  #strip .si { color: var(--ink); font-variant-numeric: tabular-nums; white-space: nowrap; position: relative; }
  #strip .si .mg { position: relative; display: inline-block; margin-right: 6px; left: 0; top: 0; vertical-align: -1px; }
  /* Its own stacking context: a connection line (z -1) sits behind every
     clip; names are 7; the playhead (30) is on top of everything (0.34
     round 4, the Fable ruling). */
  #canvas { position: relative; min-width: 100%; padding-bottom: 8px; isolation: isolate; }  /* the last lane never meets the border */
  /* The ruler, top to bottom: two marker rows (a glyph per kind, the name
     beside it), the timecode row, then the frame ticks in their own 8 px
     band under it: a time never touches a tick (0.34 round 2: the ticks ran
     up through the digits). A time starts 8 px right of its own tick line;
     the playhead's head sits in the tick band, under the times, so it never
     cuts one (00:00:00:00 stays when a tap moves it, round 4). */
  .ruler { position: relative; height: 62px; border-bottom: 1px solid var(--line); cursor: crosshair; user-select: none; }
  .tick { position: absolute; bottom: 0; height: 24px; border-left: 1px solid var(--line-strong); padding: 0 0 9px 8px;
    font: 11px/15px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--mute); white-space: nowrap; }
  .ftick { position: absolute; bottom: 0; width: 0; height: 4px; border-left: 1px solid var(--line-strong); }
  .ftick.sec { height: 6px; }
  .mlabel { position: absolute; top: 2px; font-size: 11px; line-height: 14px; color: var(--ink); white-space: nowrap;
    padding: 0 3px 0 11px; }
  .mlabel.row1, .mchip.row1 { top: 17px; }
  .mlabel { cursor: pointer; }
  /* A run of markers whose names cannot all be named (round 4): a bracket
     from the run's first marker to its last, a tick at each, and a chip
     centred on it ("3 markers"); a tap lists them. */
  .mchip .ct, .meter.chip { font: 600 11px/11px ui-sans-serif, system-ui, -apple-system, sans-serif; }
  .mchip { position: absolute; top: 2px; height: 14px; cursor: pointer; }
  .mchip .ct { position: absolute; top: 0; left: 50%; transform: translateX(-50%); color: var(--ink); white-space: nowrap; }
  .mchip .mb { position: absolute; left: -1px; right: -1px; top: 11px; height: 3px; border: 1px solid var(--accent); border-top: 0; }
  .mchip .mt { position: absolute; top: 11px; width: 1px; height: 3px; background: var(--accent); }
  /* Final Cut's marker glyphs: a standard marker, a chapter marker (a pin),
     a to-do (hollow) and a completed to-do (filled, with its ring); a cluster. */
  .mg { position: absolute; width: 8px; height: 8px; background: var(--accent); }
  .mlabel .mg, .mchip .mg { left: -4px; top: 3px; }
  .mg.standard { transform: rotate(45deg) scale(.82); }
  .mg.chapter { height: 11px; clip-path: polygon(0 0, 100% 0, 100% 62%, 50% 100%, 0 62%); }
  .mlabel .mg.chapter { top: 2px; }
  .mg.todo { background: transparent; border: 2px solid var(--accent); }
  .mg.completed { border: 2px solid var(--accent); box-shadow: inset 0 0 0 1px var(--panel); }
  /* A label that does not fit is hidden, never cut: its tick or clip stays,
     and the name is in the title tooltip and the strip. fitLabels() decides.
     A marker with no room for its name is in a bracketed run (placeMarkers()). */
  .gap .gl.unfit { visibility: hidden; }
  .flag.unfit { visibility: hidden; }
  .mlabel.gone { visibility: hidden; display: none; }  /* in a chip, or out of view: no box, no overflow */
  .tick.unfit { visibility: hidden; }
  .tick.under-flash, .ftick.under-flash { visibility: hidden; }  /* a ruler time or tick a flash line runs through */
  .clip .label.unfit, .trans.unfit .tn, .trans.unfit .bow { visibility: hidden; }
  .lane { position: relative; height: calc(var(--clip-h) + 8px); }
  .lane.title, .lane.audio { height: calc(var(--clip-h-short) + 8px); }
  .lane.title.bars { height: 32px; }
  .lane.spine.gaponly { height: 40px; }
  /* A lane's grey tag in the gutter (desktop): sticky, so it stays as the
     tracks scroll; clips pass under it, and a name that would go under it
     hides (fitLabels() measures from the gutter's edge). */
  .lane .ltag, .meter.ltag { font: 600 11px/14px ui-sans-serif, system-ui, -apple-system, sans-serif; }
  .lane .ltag { position: sticky; left: 0; z-index: 8; display: flex; align-items: center; width: 60px; height: 100%;
    padding-left: 8px; color: var(--mute);
    background: var(--panel); border-right: 1px solid var(--line); white-space: nowrap; pointer-events: none; }
  /* a connected lane with nothing in view: a thin rule */
  .lane.collapsed { height: 8px; border-top: 1px dashed var(--line); }
  .lane.collapsed > * { display: none; }
  .clip { position: absolute; top: 4px; bottom: 4px; border-radius: 7px; overflow: hidden; cursor: pointer;
    background: var(--raised); border: 1px solid var(--line-strong); }
  .lane.spine .clip { background: var(--raised-2); }
  .lane.audio .clip { background: var(--raised); }
  /* Titles: the Titles role's colour as the body, as Final Cut draws a title;
     one too narrow for its name is a 24 px bar (white text on it reads
     5.2:1 in both themes: palette.TEXT_PAIRS). */
  .clip.k-title { background: var(--role-titles); border-color: var(--role-titles); }
  .clip.k-title.bar { top: auto; height: 24px; }
  /* enabled="0": in the cut, not in the picture or the sound. Greyed and
     marked, never hidden: an editor has to see it is there. */
  .clip.off { opacity: .45; border-style: dashed; }
  .clip .off-tag { margin-left: 6px; font-size: 10px; text-transform: uppercase; letter-spacing: .04em; }
  .clip.ghost { pointer-events: none; }
  .clip.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
  /* Final Cut's clip anatomy: the filmstrip on top, tiled at its own aspect
     (never stretched), and the sound in its own band under it, the
     filmstrip ending where the band begins (0.34 round 3), the band on the
     clip's own grey, never the page's (round 4): one bottom-anchored column
     per peak on a -60..0 dBFS scale. A clip with sound and no picture is
     all waveform, under its name when it has one. */
  :root { --wave-h: 32%; }
  .clip .thumbs { position: absolute; inset: 0; background-repeat: repeat-x; background-size: auto 100%;
    background-position: center; }
  .clip.has-thumb.has-wave .thumbs { bottom: calc(3px + var(--wave-h)); }
  .clip .wave { position: absolute; left: 0; right: 0; bottom: 3px; height: calc(100% - 3px); }
  .clip.named:not(.has-thumb) .wave { height: calc(100% - 23px); }
  .clip.has-thumb .wave { height: var(--wave-h); background: var(--raised-2); }
  .clip .wave svg { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
  .clip .wave path { fill: var(--mute); }
  /* The role: a 3 px stripe along the clip's foot in Final Cut 12.4's own
     role colour (fcpxml/palette.py says where each was read). */
  .clip .stripe { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; z-index: 2; }
  .clip .stripe.role-video { background: var(--role-video); }
  .clip .stripe.role-titles { background: var(--role-titles); }
  .clip .stripe.role-dialogue { background: var(--role-dialogue); }
  .clip .stripe.role-music { background: var(--role-music); }
  .clip .stripe.role-effects { background: var(--role-effects); }
  /* The name: inside its clip, whole, or not at all (0.34 round 4, the
     Fable ruling: no name rows, no leaders, no scrim pills, never an
     ellipsis). 11 px, 6 px in from the clip's edges (INSET), so the
     playhead at rest touches none. */
  .clip .label, .meter.label { font: 600 11px/14px ui-sans-serif, system-ui, -apple-system, sans-serif; }
  .clip .label { position: absolute; top: 0; left: 0; max-width: 100%; z-index: 7; padding: 3px 6px;
    white-space: nowrap; overflow: hidden; color: var(--ink); }
  /* Over a picture the name is white with a 2 px black outline and a soft
     shadow (the halo token): it reads over a white frame on any font
     rasteriser (tests/test_app_labels.py measures it; a 1 px outline read
     97% on macOS and 76% on CI's Linux), and covers no picture. */
  .clip.has-thumb .label { color: var(--on-media);
    text-shadow: 1px 0px 0 var(--halo), -1px 0px 0 var(--halo), 0px 1px 0 var(--halo), 0px -1px 0 var(--halo), 1px 1px 0 var(--halo), -1px -1px 0 var(--halo), 1px -1px 0 var(--halo), -1px 1px 0 var(--halo),
      -2px -2px 0 var(--halo), -2px -1px 0 var(--halo), -2px 0px 0 var(--halo), -2px 1px 0 var(--halo), -2px 2px 0 var(--halo), -1px -2px 0 var(--halo), -1px 2px 0 var(--halo), 0px -2px 0 var(--halo), 0px 2px 0 var(--halo), 1px -2px 0 var(--halo), 1px 2px 0 var(--halo), 2px -2px 0 var(--halo), 2px -1px 0 var(--halo), 2px 0px 0 var(--halo), 2px 1px 0 var(--halo), 2px 2px 0 var(--halo),
      0 0 3px var(--halo); }
  .clip.k-title .label { color: var(--on-media); }
  .clip.has-thumb .off-tag { color: var(--on-media); }
  .meter { position: absolute; left: -9999px; top: 0; visibility: hidden; white-space: nowrap; }
  .gap { position: absolute; top: 8px; bottom: 8px; border: 1px solid var(--line-strong); border-radius: 7px;
    background: var(--raised); pointer-events: none; overflow: hidden; white-space: nowrap; }
  .gap .gl { position: relative; z-index: 7; display: inline-block; margin: 4px 0 0 8px; padding: 0 4px;
    color: var(--mute); font-size: 11px; }
  /* The playhead, on top of everything (0.34 round 4): its head in the
     ruler's tick band under the times, so it never cuts a time, its line
     from there down through the tracks. At rest (00:00:00:00) it touches
     nothing: names and stems sit 6 px into their clips. */
  .playhead { position: absolute; top: 54px; bottom: 0; width: 0; border-left: 2px solid var(--accent);
    pointer-events: none; z-index: 30; }
  .playhead .phead { position: absolute; top: 0; left: -7px; width: 0; height: 0; border: 6px solid transparent;
    border-bottom: 0; border-top: 8px solid var(--accent); }
  .range { position: absolute; top: 0; bottom: 0; background: var(--accent-wash);
    border-left: 1px solid var(--accent); border-right: 1px solid var(--accent); pointer-events: none; z-index: 2; }
  /* A transition, as Final Cut draws one: a translucent overlay across both
     clips at the edit, never under TRANS_MIN px, never an opaque slab; its
     bow-tie and its name inside it when the name fits whole, else neither
     (a glyph never shows without its name); a tap names it in the strip. */
  .trans { position: absolute; top: 4px; bottom: 4px; z-index: 5; border: 1px solid var(--on-media); border-radius: 4px;
    overflow: hidden; cursor: pointer; }
  .trans .tbg { position: absolute; inset: 0; background: var(--shade); opacity: .45; }
  .trans .bow { position: absolute; left: 6px; top: 6px; width: 12px; height: 9px; }
  .trans .bow path { fill: var(--on-media); }
  .trans .tn { position: absolute; left: 22px; top: 3px; font: 600 11px/14px ui-sans-serif, system-ui, -apple-system, sans-serif;
    white-space: nowrap; color: var(--on-media);
    text-shadow: 1px 0px 0 var(--halo), -1px 0px 0 var(--halo), 0px 1px 0 var(--halo), 0px -1px 0 var(--halo), 1px 1px 0 var(--halo), -1px -1px 0 var(--halo), 1px -1px 0 var(--halo), -1px 1px 0 var(--halo),
      -2px -2px 0 var(--halo), -2px -1px 0 var(--halo), -2px 0px 0 var(--halo), -2px 1px 0 var(--halo), -2px 2px 0 var(--halo), -1px -2px 0 var(--halo), -1px 2px 0 var(--halo), 0px -2px 0 var(--halo), 0px 2px 0 var(--halo), 1px -2px 0 var(--halo), 1px 2px 0 var(--halo), 2px -2px 0 var(--halo), 2px -1px 0 var(--halo), 2px 0px 0 var(--halo), 2px 1px 0 var(--halo), 2px 2px 0 var(--halo),
      0 0 3px var(--halo); }
  /* A flash in the picture is a failure: red, the only red on the timeline,
     from under the ruler's marker rows (FLASH_TOP) down through the tracks. */
  .flash { position: absolute; top: 32px; z-index: 4; background: var(--danger); pointer-events: none; }
  .flag { position: absolute; top: 2px; z-index: 1; font-size: 11px; line-height: 14px; font-weight: 600; white-space: nowrap;
    color: var(--danger); padding-right: 6px; border-right: 2px solid var(--danger); }
  .flag.row1 { top: 17px; }
  /* The connection: a 2 px stem from a connected clip's edge to the
     storyline's edge, ending in its 6 px anchor dot there (drawConnections()),
     behind every clip and name. */
  .conn { position: absolute; width: 0; border-left: 2px solid var(--mute); z-index: -1; pointer-events: none; }
  .conn::after { content: ""; position: absolute; left: -4px; width: 6px; height: 6px; border-radius: 50%; background: var(--mute); }
  .conn.down::after { bottom: 0; }
  .conn.up::after { top: 0; }
  #panel { margin-top: 10px; display: grid; grid-template-columns: 1fr; gap: 8px; }
  @media (min-width: 760px) { #panel { grid-template-columns: 2fr 1fr; } }
  .card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 8px 12px;
    min-width: 0; overflow: hidden; }
  .card h2 { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: var(--mute); text-transform: uppercase; letter-spacing: .08em; }
  dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 2px 12px; margin: 0; font-variant-numeric: tabular-nums; }
  @media (min-width: 760px) { #sel dl { grid-template-columns: max-content minmax(0, 1fr) max-content minmax(0, 1fr); } }
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
    max-height: 96px; overflow: auto; margin: 0; }
  .zoom { display: inline-flex; gap: 4px; align-items: center; color: var(--mute); font-size: 11px; }
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
<div id="overview" hidden></div>
<div id="strip" aria-live="polite" hidden><div id="sinfo"></div><span id="count"></span></div>
<div id="scroller" hidden><div id="canvas"></div></div>
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
  var PAYLOAD_OPEN = "```json fcp-timeline-app", PAYLOAD_CLOSE = "```", SCHEMA = "fcp-mcp/timeline-app/1";
  var state = { payload: null, pps: 24, playhead: 0, sel: null, range: null, pending: {}, nextId: 1, inHost: false, hostContext: null };
  var $ = function (id) { return document.getElementById(id); };
  // 0.34 rules (tests/test_app_glance.py). A name is drawn inside its clip,
  // whole, INSET px from its edges (BORDER is the clip's own), or not at
  // all; the playhead snaps to a cut within SNAP_PX; a connected clip is
  // never drawn thinner than MIN_W px, a transition overlay than TRANS_MIN.
  // The zoom at open (round 4, the Fable ruling): the fit region (the
  // picture's extent and FIT_PAD of it after, when the picture covers under
  // FIT_SHARE of the running time, else the whole cut), zoomed in from its
  // start to a MEDIAN_ZOOM px median picture clip when the median there is
  // under MEDIAN_MIN px. A run of markers is bracketed over at most SPLIT
  // of the view. A flash line starts FLASH_TOP px down the ruler, under the
  // two marker rows.
  var SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, INSET = 6, BORDER = 1, FLASH_TOP = 32;
  // The lane tags (round 4, item 10): a gutter of grey lane names on a
  // desktop (a track WIDE_AT px or wider), GUTTER_WIDE px or as wide as its
  // widest tag needs in the font it is drawn in (CI's Linux font is wider);
  // a phone keeps 16 px.
  var GUTTER_PHONE = GUTTER, GUTTER_WIDE = 64, WIDE_AT = 1000;
  var MIN_W = 6, TRANS_MIN = 14, FIT_SHARE = 0.25, FIT_PAD = 0.1, MEDIAN_MIN = 16, MEDIAN_ZOOM = 64, SPLIT = 0.25;

  // ---- rational time ------------------------------------------------------
  function frac(s) { if (s == null) return 0; var p = String(s).split("/"); return p.length === 2 ? Number(p[0]) / Number(p[1]) : Number(p[0]); }
  function tcOf(sec) {
    var p = state.payload, fps = p ? frac(p.fps) : 24, nom = p ? p.nominal_fps : 24;
    var total = Math.round(sec * fps), f = total % nom, s = Math.floor(total / nom);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60) + ":" + pad(f);
  }
  function secOfSeconds(sec) { return sec.toFixed(3) + "s"; }

  // ---- host protocol (MCP Apps, ext-apps specification/draft/apps.mdx) ----
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
    else if (m.method === "ui/notifications/host-context-changed") applyHostContext(m.params || {});
    else if (m.id != null && m.method) send({ jsonrpc: "2.0", id: m.id, result: {} });
  });
  // The host's theme wins; without one the OS preference does (the CSS media
  // query in the palette). A change notification carries only what changed.
  function applyHostContext(ctx) {
    state.hostContext = Object.assign({}, state.hostContext || {}, ctx || {});
    var theme = state.hostContext.theme;
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
  }
  function textOf(res) { return ((res && res.content) || []).map(function (c) { return (c && c.text) || ""; }).join("\n"); }
  // The payload is the result's structuredContent (0.34: kept out of the
  // model's context, so the thumbnails cost the conversation nothing); a
  // fenced payload in the text is still read for a host that sends text only.
  function payloadOf(res) {
    var sc = res && res.structuredContent;
    if (sc && sc.schema === SCHEMA) return sc;
    var found = null;
    ((res && res.content) || []).forEach(function (c) { if (c && c.type === "text" && found == null) found = extract(c.text); });
    return found;
  }
  function onToolResult(params) {
    var res = params.result || params, found = payloadOf(res);
    if (found) { setPayload(found); log("timeline loaded: " + found.name + " (" + found.clips.length + " clips)"); }
    else { log("tool result carried no timeline payload:\n" + textOf(res).slice(0, 600)); }
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
      log("← " + textOf(res).slice(0, 1200));
      return res;
    }, function (err) { log("✗ " + err.message); throw err; });
  }

  // ---- render --------------------------------------------------------------
  function setPayload(p) {
    var before = state.payload ? snapshot() : null;
    state.payload = p; state.sel = null; state.range = null; state.playhead = 0;
    $("empty").hidden = true; $("overview").hidden = false; $("strip").hidden = false; $("scroller").hidden = false; $("panel").hidden = false;
    $("title").textContent = p.name || "timeline";
    var spineN = p.clips.filter(function (c) { return !c.lane; }).length, connN = p.clips.length - spineN;
    // the clip count worded as every other output words it (proofs.counts_label);
    // a storyline that is one gap says so; the format never breaks inside
    // "3840×2160 @ 23.98fps" (non-breaking spaces); the length is a timecode
    var lead = spineN === 0 && p.gap_based ? "gap spine" : spineN + (spineN === 1 ? " clip" : " clips");
    $("meta").textContent = lead + " + " + connN + " connected · " + tcLen(frac(p.duration)) + " · " + p.width + "×" + p.height +
      "\u00a0@\u00a0" + p.fps_label + "fps" + (p.gap_based ? " · gap-based storyline" : "");
    $("meta").title = "length " + p.duration + " s";
    fit(); draw(); openAtRegion(); updateSel(); showStrip();
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
    // a name's halo (its outline over a picture) comes and goes with it
    if (cs.textShadow && cs.textShadow !== "none") { open.textShadow = cs.textShadow; shut.textShadow = cs.textShadow.replace(/rgba?\([^)]*\)/g, "transparent"); }
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
  function labelNeed(lab) {  // the name's text and the label's own padding (14 px, or 6 when tight)
    var rg = document.createRange(), cs = getComputedStyle(lab); rg.selectNodeContents(lab);
    return rg.getBoundingClientRect().width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
  }
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
  function extent() {
    // The whole sequence, like Final Cut's zoom to fit: a tail that IS in the
    // file is drawn rather than cut at the edge.
    var p = state.payload, end = 0;
    p.clips.forEach(function (c) { end = Math.max(end, frac(c.end)); });
    return Math.max(end, frac(p.duration)) || 1;
  }
  // The clips with a picture: the storyline's and the connected video clips
  // (a title is graphics, a sound clip has none).
  function pictures() { return state.payload.clips.filter(function (c) { return c.kind === "spine" || c.kind === "video"; }); }
  function median(xs) {
    var s = xs.slice().sort(function (a, b) { return a - b; }), m = s.length >> 1;
    return s.length ? (s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2) : 0;
  }
  // The fit region (0.34 round 4, the Fable ruling): the picture's extent
  // and FIT_PAD of it after, when the picture covers under FIT_SHARE of the
  // running time (a music video's 16 s of picture over a 164 s bed), else
  // the whole cut.
  function fitRegion() {
    var dur = extent(), pics = pictures();
    if (!pics.length) return [0, dur];
    var a = Math.min.apply(null, pics.map(function (c) { return frac(c.start); }));
    var b = Math.max.apply(null, pics.map(function (c) { return frac(c.end); }));
    if ((b - a) / dur >= FIT_SHARE) return [0, dur];
    return [a, Math.min(dur, b + (b - a) * FIT_PAD)];
  }
  // At open the fit region fills the track; when its median picture clip is
  // then under MEDIAN_MIN px (44 cuts on a phone) the track zooms in to a
  // MEDIAN_ZOOM px median from the region's start, and scrolls. The overview
  // always holds the whole cut. Zoom + and - zoom from here.
  function tagRoom() {
    var names = [laneName(0)].concat(state.payload.lanes.map(function (l) { return laneName(l.lane); }));
    return Math.ceil(Math.max.apply(null, names.map(function (n) { return textW(n, "ltag"); }))) + 8 + 8;
  }
  function fit() {
    GUTTER = $("scroller").clientWidth >= WIDE_AT ? Math.max(GUTTER_WIDE, tagRoom()) : GUTTER_PHONE;
    var R = fitRegion(), med = median(pictures().map(function (c) { return frac(c.duration); }));
    var room = $("scroller").clientWidth - GUTTER - 24;
    state.pps = Math.max(MIN_PPS, room / Math.max(1e-6, R[1] - R[0]));
    if (med > 0 && med * state.pps < MEDIAN_MIN) state.pps = MEDIAN_ZOOM / med;
    state.region = R;
  }
  function openAtRegion() {
    var sc = $("scroller"); sc.scrollLeft = Math.max(0, state.region[0] * state.pps);
    fitLabels(); syncOverview();
  }
  function x(sec) { return GUTTER + sec * state.pps; }   // a position
  function w(sec) { return sec * state.pps; }            // a width: no gutter
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  // A duration a person reads is a timecode (00:00:01:23); the exact
  // rational stays in the element's title.
  function tcLen(sec) { return tcOf(sec); }
  function secsLabel(sec) { var s = (Math.round(sec * 10) / 10).toFixed(1); return s.replace(/\.0$/, "") + "s"; }
  // A record (timeline) time is cyan wherever it is written (the colour
  // rule); a length or a source time is not.
  function recEl(sec) { return el("span", "rec", tcOf(sec)); }

  // ---- names ----------------------------------------------------------------
  // A label is measured in the font it is drawn in (a hidden meter span with
  // the same class), never guessed from a character count.
  function textW(s, kind) {
    var m = $("meter"); if (!m) { m = el("span"); m.id = "meter"; document.body.appendChild(m); }
    m.className = "meter " + kind; m.textContent = s; return m.getBoundingClientRect().width;
  }
  // A clip's drawn width: a connected clip is never thinner than MIN_W px
  // (0.34 round 3: a 2 px Whoosh); storyline clips sit edge to edge at their
  // real length.
  function drawnW(c) { var v = w(frac(c.duration)) - 1; return c.lane ? Math.max(MIN_W, v) : Math.max(3, v); }
  // ---- transitions: Final Cut's overlay across the edit ---------------------------
  function transOn(lane) { return (state.payload.transitions || []).filter(function (t) { return (t.lane || 0) === lane; }); }
  function transLead(t) { return t.lead != null ? frac(t.lead) : frac(t.duration) / 2; }
  function transBox(t) {  // px, never under TRANS_MIN so it reads at phone width
    var a = x(frac(t.start) - transLead(t)), b = a + w(frac(t.duration));
    if (b - a < TRANS_MIN) { var m = (a + b) / 2; a = m - TRANS_MIN / 2; b = m + TRANS_MIN / 2; }
    return [a, b];
  }
  function transName(t) { return t.name || "Transition"; }
  // how far a transition overlay reaches into a clip's head, and into its
  // tail: its name keeps clear of both
  function headCover(c) {
    var s = x(frac(c.start)), e = s + drawnW(c), px = 0;
    transOn(c.lane || 0).forEach(function (t) { var b = transBox(t); if (b[0] <= s + 1 && b[1] > s && b[1] < e) px = Math.max(px, b[1] - s); });
    return px;
  }
  function tailCover(c) {
    var s = x(frac(c.start)), e = s + drawnW(c), px = 0;
    transOn(c.lane || 0).forEach(function (t) { var b = transBox(t); if (b[1] >= e - 1 && b[0] < e && b[0] > s) px = Math.max(px, e - b[0]); });
    return px;
  }
  // A clip's name: inside it, whole, INSET px clear of its edges (and of a
  // transition over its head or tail), or not drawn at all (0.34 round 4,
  // the Fable ruling: no name rows, no leaders, no scrim pills, never an
  // ellipsis). A clip with no name is counted in the hint strip, and a tap
  // names it in the strip.
  function insideName(c) {
    var tag = c.enabled === false ? textW(" off", "label") + 6 : 0;
    var room = drawnW(c) - headCover(c) - tailCover(c) - 2 * BORDER - 2 * INSET - tag;
    return Math.ceil(textW(c.name, "label")) <= room ? { text: c.name } : null;
  }
  // Final Cut's five built-in main roles; a sub-role ("dialogue.dialogue-1")
  // takes its main role's colour, anything else its kind's default role.
  function roleOf(c) {
    var r = String(c.role || "").split(".")[0].toLowerCase();
    if (["video", "titles", "dialogue", "music", "effects"].indexOf(r) >= 0) return r;
    return c.kind === "audio" ? "dialogue" : c.kind === "title" ? "titles" : "video";
  }
  function glyphOf(type) {
    var t = String(type || "").toLowerCase();
    return t === "chapter" ? "chapter" : (t === "todo" || t === "incomplete") ? "todo" : t === "completed" ? "completed" : "standard";
  }
  function peakDb(levels) { var m = Math.max.apply(null, levels); return Math.round(m * 0.6 - 60); }
  // The sound in its own band under the filmstrip, as Final Cut draws it:
  // one bottom-anchored column per peak, on an ABSOLUTE scale (levels 0-100
  // are -60..0 dBFS, so a column's height is its level in dB and two clips
  // compare; 0.34 round 4: round 3 scaled each clip to its own loudest peak,
  // which drew a steady tone as a slab). Built into an SVG by the HTML
  // parser (numbers only).
  function waveEl(levels) {
    var n = levels.length, d = "M0,100";
    levels.forEach(function (v, i) {
      var h = (100 - Math.max(0, Math.min(100, v))).toFixed(2);
      d += " L" + i + "," + h + " L" + (i + 1) + "," + h;
    });
    d += " L" + n + ",100 Z";
    var holder = el("div");
    holder.innerHTML = '<div class="wave"><svg viewBox="0 0 ' + Math.max(1, n) + ' 100" preserveAspectRatio="none"><path d="' + d + '"></path></svg></div>';
    return holder.firstChild;
  }
  // The only CSS image the shell sets: a data: thumbnail (the caller checks it is one).
  function thumbsOf(c) { var th = el("div", "thumbs"); th.style.backgroundImage = 'url("' + c.thumb + '")'; return th; }
  function clipEl(c) {
    var thumbed = !!(c.thumb && c.thumb.indexOf("data:image/") === 0);
    var wave = c.wave && c.wave.length ? c.wave : null, off = c.enabled === false, name = insideName(c);
    var e = el("div", "clip k-" + c.kind + (thumbed ? " has-thumb" : "") + (wave ? " has-wave" : "") + (name ? " named" : "") +
                (state.sel === c.id ? " selected" : "") + (off ? " off" : "") + (c.kind === "title" && !name ? " bar" : ""));
    e.style.left = x(frac(c.start)) + "px"; e.style.width = drawnW(c) + "px";
    e.title = c.name + " · " + tcLen(frac(c.duration)) + (wave ? " · peak " + peakDb(wave) + " dBFS" : "") + (off ? " · off (disabled)" : ""); e.__c = c;
    if (thumbed) e.appendChild(thumbsOf(c));
    if (wave) e.appendChild(waveEl(wave));
    e.appendChild(el("div", "stripe role-" + roleOf(c)));
    if (name) {
      var lab = el("div", "label", name.text);
      var cover = headCover(c); if (cover) lab.style.left = cover + "px";  // after a transition into it
      if (off) lab.appendChild(el("span", "off-tag", "off"));
      e.appendChild(lab);
    }
    e.addEventListener("click", function (ev) { ev.stopPropagation(); state.playhead = frac(c.start); select(c.id); });
    return e;
  }
  // A transition, as Final Cut draws one: a translucent overlay across both
  // clips at the edit, never under TRANS_MIN px; its bow-tie and its name
  // inside it when the name fits whole, else neither (a glyph never shows
  // without its name). A tap names it in the strip.
  function transEl(t) {
    var b = transBox(t), tr = el("div", "trans"), name = transName(t);
    tr.style.left = b[0] + "px"; tr.style.width = (b[1] - b[0]) + "px";
    tr.title = name + " " + secsLabel(frac(t.duration)) + " (" + t.duration + " s)";
    tr.appendChild(el("div", "tbg"));
    if (Math.ceil(textW(name, "label")) + 16 + 2 * INSET <= b[1] - b[0]) {
      var holder = el("div");
      holder.innerHTML = '<svg class="bow" viewBox="0 0 14 10"><path d="M0,0 L7,5 L0,10 Z M14,0 L7,5 L14,10 Z"></path></svg>';
      tr.appendChild(holder.firstChild); tr.appendChild(el("div", "tn", name)); tr.classList.add("named");
    }
    tr.addEventListener("click", function (ev) { ev.stopPropagation(); showStrip({ trans: t }); });
    return tr;
  }

  // A lane's tag: the primary storyline, or the lane's number above (+) or
  // under (-) it, as Final Cut numbers connected lanes.
  function laneName(n) { return n === 0 ? "Storyline" : "Lane " + (n > 0 ? n : "\u2212" + (-n)); }

  // ---- the timeline -----------------------------------------------------------
  function draw() {
    var p = state.payload, canvas = $("canvas"); canvas.textContent = "";
    var dur = extent(), width = Math.max(x(dur) + 24, $("scroller").clientWidth - 2);
    canvas.style.width = width + "px";
    var ruler = el("div", "ruler");
    canvas.appendChild(ruler);  // attached first, so label widths below are measured, not guessed
    // Labelled ticks on whole timecode seconds (at 23.98 a timecode second
    // lasts 1.001 real seconds); between them, ticks on frame multiples at
    // least 8 px apart (so the first clears the playhead's head at rest), a
    // second taller.
    var step = niceStep(dur, width - GUTTER), nom = p.nominal_fps || 24, perSec = nom / (frac(p.fps) || 24);
    for (var S = 0; S * perSec <= dur + 1e-9; S += step) {
      var t = S * perSec, k = el("div", "tick", tcOf(t)); k.style.left = x(t) + "px"; k.__key = "t:" + S; ruler.appendChild(k);
      // a time whose label would run past the end is not drawn: no sideways scroll for a label
      if (k.offsetLeft + k.offsetWidth > width - 2) ruler.removeChild(k);
    }
    var fpx = state.pps * perSec / nom;
    var sub = [1, 2, 3, 4, 6, 8, 12, 24, 48, 120, 240, 600, 1440, 3600, 14400].filter(function (n) { return n * fpx >= 8; })[0] || 14400;
    for (var F = sub; F / nom * perSec <= dur + 1e-9; F += sub) {
      if (F % (step * nom) === 0) continue;
      var ft = el("div", "ftick" + (F % nom === 0 ? " sec" : "")); ft.style.left = x(F / nom * perSec) + "px"; ruler.appendChild(ft);
    }
    ruler.dataset.frames = String(sub);
    // Markers: sequence markers AND clip markers (a chapter marker lives on
    // its clip in FCPXML; every m.start is already on the sequence clock),
    // each its glyph and its name in the ruler's two marker rows (nothing in
    // the lanes). placeMarkers() gives each a row, or a run a bracket.
    var named = p.markers.slice();
    p.clips.forEach(function (c) { (c.markers || []).forEach(function (m) { named.push(m); }); });
    named.sort(function (a, b) { return frac(a.start) - frac(b.start); });
    state.marks = named.map(function (m) {
      var left = x(frac(m.start)), g = glyphOf(m.type || m.kind);
      var lab = el("div", "mlabel", m.name); lab.style.left = left + "px"; lab.title = m.name + " (" + g + ")";
      lab.insertBefore(el("span", "mg " + g), lab.firstChild); lab.dataset.kind = g; lab.__key = "m:" + m.name + "@" + m.start;
      ruler.appendChild(lab);
      var wpx = lab.offsetWidth; lab.dataset.w = String(wpx); lab.dataset.t = String(frac(m.start));
      lab.addEventListener("click", function (ev) { ev.stopPropagation(); state.playhead = frac(m.start); placePlayhead(); showStrip({ marks: [m] }); });
      return { m: m, el: lab, x: left, w: wpx, kind: g };
    });
    ruler.addEventListener("mousedown", rulerDown);
    var byId = {}; p.clips.forEach(function (c) { byId[c.id] = c; });
    var lanes = p.lanes.slice();
    // The primary storyline is drawn even when it is one gap, so every
    // connected clip has a line to what it hangs from; and a storyline with
    // nothing above it keeps an empty lane-1 row, so a clip laid on lane 1
    // (gen_broll) appears in place instead of pushing the storyline down.
    if (!lanes.some(function (l) { return l.lane === 0; })) {
      var at = lanes.findIndex(function (l) { return l.lane < 0; });
      lanes.splice(at < 0 ? lanes.length : at, 0, { lane: 0, kind: "spine", clips: [] });
    }
    if (lanes.some(function (l) { return l.lane === 0 && l.clips.length; }) && !lanes.some(function (l) { return l.lane > 0; }))
      lanes.unshift({ lane: 1, kind: "video reserved", clips: [] });
    state.rows = {};
    lanes.forEach(function (lane) {
      var here = lane.clips.map(function (id) { return byId[id]; });
      // lane height by lane type: titles and sound are shorter than picture,
      // and a lane of titles too narrow to name is a row of 24 px bars
      var titles = here.length && here.every(function (c) { return c.kind === "title"; });
      var bars = titles && here.every(function (c) { return !insideName(c); });
      var row = el("div", "lane " + lane.kind + (lane.lane < 0 ? " below" : "") + (titles ? " title" : "") + (bars ? " bars" : "") +
                    (lane.lane === 0 && !lane.clips.length ? " gaponly" : ""));
      row.dataset.lane = String(lane.lane);
      // its grey tag in the gutter, sticky as the tracks scroll (desktop only)
      if (GUTTER > GUTTER_PHONE) { var tag = el("div", "ltag", laneName(lane.lane)); tag.style.width = (GUTTER - 4) + "px"; row.appendChild(tag); }
      if (lane.lane === 0) p.gaps.forEach(function (g) {
        var ge = el("div", "gap"); ge.appendChild(el("span", "gl", "Gap " + tcLen(frac(g.duration)))); ge.title = "gap " + g.duration + " s";
        ge.style.left = x(frac(g.start)) + "px"; ge.style.width = Math.max(2, w(frac(g.duration))) + "px"; row.appendChild(ge);
      });
      here.forEach(function (c) { row.appendChild(clipEl(c)); });
      transOn(lane.lane).forEach(function (t) { row.appendChild(transEl(t)); });
      canvas.appendChild(row); state.rows[lane.lane] = row;
    });
    drawFlashes(ruler, canvas, state.rows[0]);
    var ph = el("div", "playhead"); ph.id = "playhead"; ph.appendChild(el("div", "phead")); canvas.appendChild(ph);
    var rg = el("div", "range"); rg.id = "range"; rg.hidden = true; canvas.appendChild(rg);
    placePlayhead();
    fitLabels();
    drawOverview();
  }
  // A connected lane with no clip in the part in view collapses to a thin
  // rule (0.34 round 3: a 90 px empty sound lane at 390); the storyline
  // never does. Then the connection lines are redrawn.
  function collapseLanes() {
    var sc = $("scroller"), t0 = (sc.scrollLeft) / state.pps, t1 = (sc.scrollLeft + sc.clientWidth - GUTTER) / state.pps;
    var byId = {}; state.payload.clips.forEach(function (c) { byId[c.id] = c; });
    state.payload.lanes.forEach(function (lane) {
      var row = state.rows && state.rows[lane.lane]; if (!row || !lane.lane) return;
      var shown = lane.clips.some(function (id) { var c = byId[id]; return frac(c.end) > t0 && frac(c.start) < t1; });
      row.classList.toggle("collapsed", !shown);
    });
    drawConnections();
  }
  // The connection: a 2 px stem from each connected clip's edge to the
  // storyline's edge, ending in its anchor dot ON that edge, outside the
  // storyline's clip (0.34 round 4: round 3's dot sat 4 px inside the
  // storyline, behind it, and never showed). The stem is INSET px into its
  // clip, so the playhead at rest touches none; a stem that would run
  // within a dot's width of another moves along its clip, so no two meet.
  // Drawn behind every clip and name.
  function drawConnections() {
    var canvas = $("canvas"), s = state.rows && state.rows[0]; if (!s) return;
    canvas.querySelectorAll(".conn").forEach(function (c) { c.remove(); });
    canvas.querySelectorAll(".flash").forEach(function (f) { f.style.height = Math.max(0, s.offsetTop + s.offsetHeight - FLASH_TOP) + "px"; });
    var spineParts = [].slice.call(s.querySelectorAll(".clip:not(.ghost), .gap"));
    // the storyline's edge under x: the clip or gap there, else a clip's
    var edge = function (px) {
      var hit = spineParts.filter(function (e) { return e.offsetLeft <= px + 4 && px - 4 <= e.offsetLeft + e.offsetWidth; })[0];
      var top = hit ? hit.offsetTop : 4, h = hit ? hit.offsetHeight : s.offsetHeight - 8;
      return [s.offsetTop + top, s.offsetTop + top + h];
    };
    var placed = [];
    canvas.querySelectorAll(".lane:not(.collapsed) .clip:not(.ghost)").forEach(function (ce) {
      var lane = Number(ce.parentNode.dataset.lane); if (!lane) return;
      var lx = ce.offsetLeft + INSET, last = ce.offsetLeft + ce.offsetWidth - 4;
      var ct = ce.parentNode.offsetTop + ce.offsetTop, cb = ct + ce.offsetHeight;
      var span = function () { var sp = edge(lx); return lane > 0 ? [cb, sp[0]] : [sp[1], ct]; };
      var ab = span();
      while (placed.some(function (q) { return Math.abs(q.x - lx) < 8 && q.a < ab[1] && ab[0] < q.b; }) && lx + 8 <= last) { lx += 8; ab = span(); }
      placed.push({ x: lx, a: ab[0], b: ab[1] });
      var line = el("div", "conn " + (lane > 0 ? "down" : "up"));
      line.style.left = lx + "px"; line.style.top = ab[0] + "px"; line.style.height = Math.max(6, ab[1] - ab[0]) + "px";
      canvas.appendChild(line);
    });
  }
  // Every label is either whole and inside the visible track area, or
  // hidden. A clip name wider than its clip is hidden; a tick, gap or
  // transition name that crosses the visible edge hides until a scroll
  // brings it fully into view. Markers are placed by placeMarkers(); the
  // hint strip counts the names shown.
  function fitLabels() {
    var sc = $("scroller"); if (!sc || sc.hidden) return;
    collapseLanes();
    var r = sc.getBoundingClientRect(), left = r.left + GUTTER, right = r.right - 1;
    sc.querySelectorAll(".clip .label").forEach(function (l) { l.classList.toggle("unfit", l.scrollWidth > l.clientWidth + 0.5); });
    sc.querySelectorAll(".tick, .gap .gl, .clip .label:not(.unfit)").forEach(function (e) {
      var b = textBox(e);
      e.classList.toggle("unfit", !!(b.left < left - 0.5 || b.right > right + 0.5));
    });
    sc.querySelectorAll(".trans.named").forEach(function (t) {
      var b = textBox(t.querySelector(".tn")), g = t.querySelector(".bow").getBoundingClientRect();
      t.classList.toggle("unfit", Math.min(b.left, g.left) < left - 0.5 || b.right > right + 0.5);
    });
    placeMarkers(r, left, right);
    coverTicks();
    countNames(left, right);
  }
  function textBox(e) {
    var r = document.createRange(), t = [].filter.call(e.childNodes, function (n) { return n.nodeType === 3; });
    if (t.length) { r.setStart(t[0], 0); r.setEnd(t[t.length - 1], t[t.length - 1].length); } else r.selectNodeContents(e);
    return r.getBoundingClientRect();
  }
  // The hint strip's counted line (0.34 round 4): the clip names the part in
  // view shows, of every clip in the cut ("13 of 44 names shown · tap or zoom").
  function countNames(left, right) {
    var n = 0, total = state.payload.clips.length, c = $("count");
    document.querySelectorAll("#canvas .lane:not(.collapsed) .clip:not(.ghost) .label:not(.unfit)").forEach(function (l) {
      var b = textBox(l); if (b.width > 0 && b.left >= left - 0.5 && b.right <= right + 0.5) n++;
    });
    if (!c) return;
    c.textContent = n + " of " + total + " names shown" + (n < total ? " · tap or zoom" : "");
    c.dataset.shown = String(n); c.dataset.total = String(total);
  }
  // The ruler's two marker rows, for the part in view: each flash flag
  // first (a failure; right-aligned to its frame), then the markers left to
  // right, each named right of its own glyph on row 0 or row 1, in view and
  // 6 px clear. A marker with no such place joins the marker (or run) before
  // it, when the run then spans at most SPLIT of the view, else the one
  // after it, in a run drawn as a bracket from its first marker to its last
  // (a tick at each) with a chip centred on it ("3 markers"; a tap lists
  // them); then every row is placed again. So only markers whose names
  // cannot all fit are bracketed, never one far from the rest, and no
  // marker in view is hidden (0.34 round 4).
  function placeMarkers(view, left, right) {
    var ruler = document.querySelector("#canvas .ruler"); if (!ruler || !state.marks) return;
    ruler.querySelectorAll(".mchip").forEach(function (c) { c.remove(); });
    var x0 = ruler.getBoundingClientRect().left, GAP = 6, reachPx = (view.right - left) * SPLIT;
    var inView = function (a, b) { return a >= left - 4.5 && b <= view.right - 1; };
    var flags = [].slice.call(ruler.querySelectorAll(".flag")), live = [];
    state.marks.forEach(function (mk) {
      var at = x0 + mk.x;
      mk.el.classList.remove("row1", "gone", "clustered", "unfit", "crowded");
      // in view when its glyph and its name are, as a ruler time is; until a
      // scroll brings it whole it is not drawn (its overview tick shows it),
      // and a marker before the cut's start never shows
      if (!inView(at - 4, at + mk.w)) { mk.el.classList.add("gone", "unfit"); mk.state = "out"; return; }
      live.push(mk);
    });
    var chipW = function (n) { return Math.ceil(textW(n + (n === 1 ? " marker" : " markers"), "chip")); };
    var boxOf = function (it) {
      var a = x0 + it.marks[0].x, b = x0 + it.marks[it.marks.length - 1].x;
      if (!it.run) return [a - 4, a + it.marks[0].w];
      var mid = (a + b) / 2, half = chipW(it.marks.length) / 2 + 1;
      return [Math.min(a - 1, mid - half), Math.max(b + 1, mid + half)];
    };
    var items = live.map(function (mk) { return { marks: [mk] }; }), rows = [[], []], guard = 0;
    var clear = function (r, bx) { return !rows[r].some(function (q) { return bx[0] < q[1] + GAP && q[0] < bx[1] + GAP; }); };
    for (;;) {
      rows = [[], []];
      flags.forEach(function (f) {
        var b = x0 + f.__right, a = b - f.__w, row = -1;
        for (var r = 0; r < 2 && row < 0; r++) if (a >= view.left && b <= view.right - 1 && clear(r, [a, b])) row = r;
        f.classList.toggle("row1", row === 1); f.classList.toggle("unfit", row < 0);
        if (row >= 0) rows[row].push([a, b]);
      });
      var fail = -1;
      for (var i = 0; i < items.length && fail < 0; i++) {
        var bx = boxOf(items[i]), row = -1;
        for (var r = 0; r < 2 && row < 0; r++) if (inView(bx[0], bx[1]) && clear(r, bx)) row = r;
        if (row < 0) { fail = i; items[i].row = null; } else { items[i].row = row; rows[row].push(bx); }
      }
      if (fail < 0 || ++guard > 80) break;
      var f = items[fail], prev = items[fail - 1], next = items[fail + 1];
      var reach = function (p, q) { return q.marks[q.marks.length - 1].x - p.marks[0].x <= reachPx; };
      if (prev && reach(prev, f)) { prev.marks = prev.marks.concat(f.marks); prev.run = true; items.splice(fail, 1); }
      else if (next && reach(f, next)) { f.marks = f.marks.concat(next.marks); f.run = true; items.splice(fail + 1, 1); }
      else if (!f.run) f.run = true;  // alone, too far from either: its chip ("1 marker")
      // last resort, never met on the fixtures: a run past SPLIT rather than a
      // marker lost (tests/test_app_glance.py reports the run as too wide)
      else if (prev) { prev.marks = prev.marks.concat(f.marks); prev.run = true; items.splice(fail, 1); }
      else if (next) { f.marks = f.marks.concat(next.marks); items.splice(fail + 1, 1); }
      else break;
    }
    items.forEach(function (it) {
      if (it.row == null) { it.marks.forEach(function (mk) { mk.el.classList.add("gone", "unfit"); mk.state = "unplaced"; }); return; }
      if (!it.run) { var one = it.marks[0]; one.el.classList.toggle("row1", it.row === 1); one.state = "named"; return; }
      var a = it.marks[0].x, b = it.marks[it.marks.length - 1].x, n = it.marks.length;
      var chip = el("div", "mchip" + (it.row ? " row1" : ""));
      chip.style.left = a + "px"; chip.style.width = (b - a) + "px";
      chip.appendChild(el("div", "mb"));
      it.marks.forEach(function (mk) { var tk = el("div", "mt"); tk.style.left = (mk.x - a) + "px"; chip.appendChild(tk); });
      chip.appendChild(el("div", "ct", n + (n === 1 ? " marker" : " markers")));
      chip.dataset.count = String(n); chip.dataset.times = it.marks.map(function (mk) { return String(frac(mk.m.start)); }).join(",");
      chip.title = it.marks.map(function (mk) { return mk.m.name + " " + tcOf(frac(mk.m.start)); }).join(" · ");
      it.marks.forEach(function (mk) { mk.el.classList.add("gone", "unfit", "crowded", "clustered"); mk.state = "clustered"; });
      var list = it.marks.map(function (mk) { return mk.m; });
      chip.addEventListener("click", function (ev) { ev.stopPropagation(); showStrip({ marks: list }); });
      ruler.appendChild(chip);
    });
  }
  // A flash in the picture (diagnose's FAIL): a red line from under the
  // ruler's marker rows (FLASH_TOP; 0.34 round 4: from the ruler's top it ran
  // through the Press marker's glyph) down through the picture's tracks to
  // the storyline's foot, never thinner than 2 px, and a red flag in a marker
  // row right-aligned to it (placeMarkers() gives it the first row with
  // room, before any marker). A WARN (2-5f) is grey: red means failure only.
  function drawFlashes(ruler, canvas, spine) {
    var foot = spine ? spine.offsetTop + spine.offsetHeight : null;
    (state.payload.flashes || []).forEach(function (f) {
      var fail = f.verdict === "FAIL", sl = el("div", "flash");
      sl.style.left = x(frac(f.start)) + "px"; sl.style.width = Math.max(2, w(frac(f.duration))) + "px";
      if (foot != null) sl.style.height = Math.max(0, foot - FLASH_TOP) + "px";
      if (!fail) sl.style.background = "var(--mute)";
      sl.title = "flash " + f.frames + "f " + tcOf(frac(f.start)) + ": " + f.shows + " between " + f.between.join(" and ");
      canvas.appendChild(sl);
      var tag = el("div", "flag", "flash " + f.frames + "f"); if (!fail) { tag.style.color = "var(--mute)"; tag.style.borderColor = "var(--mute)"; }
      tag.title = sl.title; ruler.appendChild(tag);
      var right = x(frac(f.start));
      tag.__right = right; tag.__w = tag.offsetWidth; tag.style.left = (right - tag.__w) + "px";
    });
  }
  // The overview: a minimap of the whole cut, always (0.34 round 4). A 1 px
  // tick per marker in its top band, the flash in the band under it, then
  // every lane a row of bars in its clips' role colours, and a window round
  // the part the tracks show. A click there scrolls the tracks to it.
  var OV_TOP = 22;
  function drawOverview() {
    var p = state.payload, ov = $("overview");
    ov.textContent = "";
    var W = ov.clientWidth, H = ov.clientHeight, dur = extent(), lanes = p.lanes;
    var sx = function (t) { return 4 + t / dur * (W - 8); }, rowH = (H - OV_TOP - 3) / Math.max(1, lanes.length);
    var byId = {}; p.clips.forEach(function (c) { byId[c.id] = c; });
    lanes.forEach(function (lane, i) {
      lane.clips.forEach(function (id) {
        var c = byId[id], b = el("div", "ob role-" + roleOf(c));
        b.style.left = sx(frac(c.start)) + "px"; b.style.width = Math.max(1.5, sx(frac(c.end)) - sx(frac(c.start)) - 0.5) + "px";
        b.style.top = (OV_TOP + i * rowH) + "px"; b.style.height = Math.max(2, rowH - 2) + "px"; b.__c = c; ov.appendChild(b);
      });
    });
    (state.marks || []).forEach(function (mk) {
      var o = el("div", "om"); o.style.left = Math.round(sx(frac(mk.m.start))) + "px"; o.title = mk.m.name + " " + tcOf(frac(mk.m.start)); ov.appendChild(o);
    });
    (p.flashes || []).forEach(function (f) {
      if (f.verdict !== "FAIL") return;
      var o = el("div", "of"); o.style.left = sx(frac(f.start)) + "px"; o.title = "flash " + f.frames + "f " + tcOf(frac(f.start)); ov.appendChild(o);
    });
    var box = el("div", "ov"); box.id = "ovbox"; ov.appendChild(box);
    ov.dataset.span = String(dur);  // the time the bar maps across its width (tests read it)
    syncOverview();
  }
  function syncOverview() {
    var box = $("ovbox"), sc = $("scroller"), ov = $("overview"); if (!box || !state.payload) return;
    var W = ov.clientWidth, dur = extent(), sx = function (t) { return 4 + t / dur * (W - 8); };
    var t0 = sc.scrollLeft / state.pps, t1 = Math.min(dur, (sc.scrollLeft + sc.clientWidth - GUTTER) / state.pps);
    box.style.left = sx(t0) + "px"; box.style.width = Math.max(6, sx(t1) - sx(t0)) + "px";
  }
  $("overview").addEventListener("click", function (ev) {
    if (!state.payload) return;
    var ov = $("overview"), sc = $("scroller"), r = ov.getBoundingClientRect(), t = (ev.clientX - r.left - 4) / (r.width - 8) * extent();
    sc.scrollLeft = Math.max(0, x(t) - GUTTER - (sc.clientWidth - GUTTER) / 2);
  });
  function niceStep(dur, width) {
    var target = 110 / (width / dur), steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    for (var i = 0; i < steps.length; i++) if (steps[i] >= target) return steps[i];
    return 3600;
  }
  // A ruler time or frame tick under a flash line is hidden while the line
  // is there. The playhead cuts none: its head sits in the tick band under
  // the times (0.34 round 4: 00:00:00:00 no longer goes on a tap).
  function coverTicks() {
    var parts = [];
    document.querySelectorAll("#canvas .flash").forEach(function (f) { parts.push(f.getBoundingClientRect()); });
    document.querySelectorAll("#canvas .tick").forEach(function (t) {
      var r = document.createRange(); r.selectNodeContents(t); var b = r.getBoundingClientRect();
      t.classList.toggle("under-flash", b.width > 0 && parts.some(function (q) {
        return q.left < b.right + 1 && b.left < q.right + 1 && q.top < b.bottom && b.top < q.bottom; }));
    });
    document.querySelectorAll("#canvas .ftick").forEach(function (t) {
      var b = t.getBoundingClientRect();
      t.classList.toggle("under-flash", parts.some(function (q) { return q.left < b.right + 1 && b.left < q.right + 1; }));
    });
  }
  function placePlayhead() { var ph = $("playhead"); if (ph) ph.style.left = x(state.playhead) + "px"; $("tc").textContent = tcOf(state.playhead);
    var rg = $("range"); if (rg) { if (state.range) { rg.hidden = false; rg.style.left = x(state.range[0]) + "px"; rg.style.width = w(state.range[1] - state.range[0]) + "px"; } else rg.hidden = true; } }
  // The playhead snaps to a cut (a clip's start or end, the sequence's ends)
  // within SNAP_PX, as Final Cut's snapping does.
  function cuts() {
    var out = [0, extent()];
    state.payload.clips.forEach(function (c) { out.push(frac(c.start), frac(c.end)); });
    return out;
  }
  function snap(t) {
    var best = t, bd = SNAP_PX + 1e-6;
    cuts().forEach(function (k) { var d = Math.abs(k - t) * state.pps; if (d <= bd) { bd = d; best = k; } });
    return best;
  }
  function rulerDown(ev) {
    var rect = $("canvas").getBoundingClientRect(), at = function (e) { return snap(clamp((e.clientX - rect.left - GUTTER) / state.pps)); };
    var start = at(ev), moved = false;
    state.playhead = start; state.range = null; placePlayhead();
    function move(e) { var now = at(e); if (Math.abs(now - start) * state.pps > 3) { moved = true; state.range = [Math.min(start, now), Math.max(start, now)]; state.playhead = now; placePlayhead(); } }
    function up() { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); if (!moved) state.range = null; updateSel(); }
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
  }
  function clamp(s) { return Math.max(0, Math.min(extent(), s)); }
  function clipById(id) { var out = null; state.payload.clips.forEach(function (c) { if (c.id === id) out = c; }); return out; }
  function select(id) { state.sel = id; draw(); updateSel(); showStrip(); }

  // ---- the strip above the ruler ---------------------------------------------------
  // A tap never floats a sheet over the lanes (0.34 round 3): the selected
  // clip's name, start and length (or a marker, a run's markers, a
  // transition) show in a strip anchored above the ruler, and the Selection
  // panel updates. Its last line counts the names shown (countNames()).
  // Record times are cyan (round 4), lengths are not.
  function showStrip(what) {
    var s = $("strip"), info = $("sinfo"); if (!s || !info || !state.payload) return; info.textContent = ""; delete s.dataset.for;
    var c = state.sel ? clipById(state.sel) : null;
    var at = function (label, sec) { var it = el("span", "si"); it.appendChild(document.createTextNode(label + " ")); it.appendChild(recEl(sec)); return it; };
    var len = function (sec, exact) { var ln = el("span", "si", "length " + tcLen(sec)); ln.title = exact + " s"; return ln; };
    if (what && what.marks) {
      info.appendChild(el("b", null, what.marks.length > 1 ? what.marks.length + " markers" : what.marks[0].name));
      what.marks.forEach(function (m) {
        var it = el("span", "si"); it.appendChild(el("span", "mg " + glyphOf(m.type || m.kind)));
        if (what.marks.length > 1) it.appendChild(document.createTextNode(m.name + " "));
        it.appendChild(recEl(frac(m.start))); info.appendChild(it);
      });
      s.dataset.for = "markers"; return;
    }
    if (what && what.trans) {
      var t = what.trans;
      info.appendChild(el("b", null, transName(t)));
      info.appendChild(at("start", frac(t.start) - transLead(t))); info.appendChild(len(frac(t.duration), t.duration));
      s.dataset.for = "transition"; return;
    }
    if (c) {
      info.appendChild(el("b", null, c.name));
      info.appendChild(at("start", frac(c.start))); info.appendChild(len(frac(c.duration), c.duration));
      s.dataset.for = c.id; return;
    }
    info.appendChild(el("span", "hint", "Tap a clip or a marker for its name, start and length."));
  }

  // ---- selection panel + actions -----------------------------------------
  function updateSel() {
    var sel = $("sel"), acts = $("acts"); sel.textContent = ""; acts.textContent = "";
    var p = state.payload, file = p.file.path, c = state.sel ? clipById(state.sel) : null;
    var line = function (parts) { var d = el("div"); parts.forEach(function (x) { d.appendChild(typeof x === "string" ? document.createTextNode(x) : x); }); return d; };
    if (c) {
      var dl = document.createElement("dl");
      // every time and length a person reads is a timecode; the exact rational
      // is its title; the record times (start, end) are cyan
      [["clip", c.name], ["start", tcOf(frac(c.start)), c.start + " s", "rec"], ["end", tcOf(frac(c.end)), c.end + " s", "rec"],
       ["length", tcLen(frac(c.duration)), c.duration + " s"], ["lane", c.kind === "spine" ? "spine" : String(c.lane)],
       ["role", (c.role || roleOf(c)) + (c.wave ? " · peak " + peakDb(c.wave) + " dBFS" : "")],
       ["source in", tcOf(frac(c.source_start)), c.source_start + " s"],
       ["media", c.media || "—"], ["markers", String((c.markers || []).length)], ["words", String((c.words || []).length)]]
        .forEach(function (r) { dl.appendChild(el("dt", null, r[0])); var dd = el("dd", r[3] || null, r[1]); if (r[2]) dd.title = r[2]; dl.appendChild(dd); });
      sel.appendChild(dl);
      if ((c.words || []).length) sel.appendChild(el("div", null, "“" + c.words.map(function (w) { return w.w; }).join(" ").slice(0, 240) + "”"));
    } else if (state.range) {
      sel.appendChild(line(["Range ", recEl(state.range[0]), " → ", recEl(state.range[1]), " (" + tcLen(state.range[1] - state.range[0]) + ")"]));
    } else {
      sel.appendChild(line(["Click a clip. Drag on the ruler to select a range. Playhead ", recEl(state.playhead), "."]));
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
    callTool(tool, args).then(function (res) {
      var m = /Saved to:\s*`?([^`\n]+)`?/.exec(textOf(res));
      if (!m) { log("edit returned no output path; not re-rendering"); return; }
      return callTool("view", { action: "view_timeline", args: { filepath: m[1].trim() } }).then(function (r2) {
        var pl = payloadOf(r2); if (pl) { setPayload(pl); log("re-rendered from " + pl.file.path); }
      });
    }).catch(function () {});
  }
  function log(s) { var l = $("log"); l.textContent = (s + "\n" + l.textContent).slice(0, 6000); }
  $("zin").addEventListener("click", function () { if (!state.payload) return; state.pps *= 1.5; draw(); });
  $("zout").addEventListener("click", function () { if (!state.payload) return; state.pps = Math.max(MIN_PPS, state.pps / 1.5); draw(); });
  // Refit and redraw on a WIDTH change only: hosts resize the frame to its
  // content's height after every render, and a redraw then would cut short the motion.
  var drawnWidth = window.innerWidth;
  window.addEventListener("resize", function () {
    if (!state.payload || window.innerWidth === drawnWidth) return;
    drawnWidth = window.innerWidth; fit(); draw(); openAtRegion();
  });
  $("scroller").addEventListener("scroll", function () { fitLabels(); syncOverview(); });
  window.__fcpApp = { setPayload: setPayload, extract: extract, payloadOf: payloadOf, state: state, fitLabels: fitLabels,
                      motion: MOTION, revealWhenWide: revealWhenWide, hideWhenNarrow: hideWhenNarrow, timeForProgress: timeForProgress,
                      placePlayhead: placePlayhead, snap: snap, select: select, get gutter() { return GUTTER; }, fitRegion: fitRegion,
                      rules: { inset: INSET, snap: SNAP_PX, minWidth: MIN_W, transMin: TRANS_MIN, fitShare: FIT_SHARE, fitPad: FIT_PAD,
                               medianMin: MEDIAN_MIN, medianZoom: MEDIAN_ZOOM, split: SPLIT, flashTop: FLASH_TOP } };

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
