"""The timeline app — an MCP Apps view of the cut, rendered by the host.

``fcpxml/preview.py`` draws a static HTML snapshot and serves it as
``preview://``. This module is the interactive successor: one ``ui://``
resource (the app shell, served once as ``text/html;profile=mcp-app``) and a
JSON payload the ``view`` tool returns on every call. The host renders the
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

from .rational import fcp_frame_rate_name, nominal_fps, rational_fps

APP_URI = "ui://fcp/timeline"
APP_MIME_TYPE = "text/html;profile=mcp-app"
PAYLOAD_SCHEMA = "fcp-mcp/timeline-app/1"

# Bounds on what one payload may carry. The payload travels inside a tool
# result, which the host also shows as chat text to a client without Apps
# support, so it has to stay readable and bounded rather than exhaustive.
MAX_THUMBNAILS = 48
THUMBNAIL_SHORT_SIDE = 54
MAX_THUMBNAIL_BYTES = 400_000
MAX_WORDS = 3000

# The tool result carries the payload between these fences. The app looks
# for them in every text block it is handed; a client without Apps support
# sees a summary followed by a JSON block, which is still an honest answer.
PAYLOAD_OPEN = "```json fcp-timeline-app"
PAYLOAD_CLOSE = "```"

ThumbnailFn = Callable[[str, Fraction], Optional[str]]
TranscriptFn = Callable[[str], Optional[dict]]


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
) -> dict:
    """The JSON the app draws from. Pure: every side effect is injected.

    ``thumbnail(media_path, source_seconds)`` returns a data URI or None;
    ``transcript(media_path)`` returns a cached transcript dict or None.
    Neither is ever called more than the bounds above allow.
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
        role = (getattr(item, "role", "") or getattr(item, "video_role", "")
                or getattr(item, "audio_role", "") or "")
        words = []
        if words_left > 0:
            words = _words_for(pos, src, dur, _transcript(media))[:words_left]
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
            "media": media,
            "role": role,
            "keywords": [k.value for k in getattr(item, "keywords", []) or []],
            "markers": _marker_rows(
                getattr(item, "markers", []) or [], Fraction(0) if connected else origin
            ),
            "words": words,
            "thumb": _thumb(media, src + dur / 2) if media else None,
        }
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


_APP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FCP Timeline</title>
<style>
  :root {
    --bg: #09090b; --panel: #121216; --line: #26262c; --ink: #ededf0;
    --mute: #8c8c96; --accent: #00e5ff; --accent-ink: #062228;
    --spine: #2a2a32; --video: #23232b; --audio: #1c1c23; --title: #2c2a22;
    --gap: repeating-linear-gradient(135deg, #111115 0 6px, #1a1a20 6px 12px);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink);
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
  #canvas { position: relative; min-width: 100%; }
  .ruler { position: relative; height: 40px; border-bottom: 1px solid var(--line); cursor: crosshair;
    user-select: none; }
  .tick { position: absolute; top: 18px; bottom: 0; border-left: 1px solid #33333b; padding-left: 4px;
    font: 10px/22px ui-monospace, Menlo, monospace; color: var(--mute); white-space: nowrap; }
  .mlabel { position: absolute; top: 2px; font-size: 10px; color: var(--accent); white-space: nowrap;
    padding: 0 3px 0 5px; border-left: 2px solid var(--accent); line-height: 14px; }
  .lane { position: relative; height: 44px; border-bottom: 1px solid var(--line); }
  .lane.spine { height: 58px; background: var(--gap); }
  .lane.audio { height: 30px; }
  .lane-tag { position: sticky; left: 0; z-index: 3; display: block; width: 48px; height: 100%;
    padding: 4px 0 0 6px; font-size: 10px; color: var(--mute); background: var(--panel);
    border-right: 1px solid var(--line); pointer-events: none; }
  .clip { position: absolute; top: 3px; bottom: 3px; border-radius: 4px; overflow: hidden;
    background: var(--video); border: 1px solid #3a3a44; cursor: pointer; display: flex; flex-direction: column;
    justify-content: flex-end; }
  .lane.spine .clip { background: var(--spine); }
  .lane.audio .clip { background: var(--audio); }
  .clip.title { background: var(--title); }
  .clip img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: .85; }
  .clip .label { position: relative; z-index: 1; padding: 2px 6px; font-size: 11px; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; background: linear-gradient(transparent, rgba(0,0,0,.75)); }
  .clip .role { font-size: 9px; color: var(--mute); margin-left: 6px; }
  .clip.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
  .clip .mk { position: absolute; top: 0; width: 2px; height: 8px; background: var(--accent); z-index: 2; }
  .gap { position: absolute; top: 3px; bottom: 3px; border: 1px dashed #3a3a44; border-radius: 4px;
    color: var(--mute); font-size: 10px; padding: 2px 6px; pointer-events: none; }
  .words { position: relative; height: 22px; border-bottom: 1px solid var(--line); overflow: hidden; }
  .word { position: absolute; top: 3px; font-size: 10px; color: var(--mute); white-space: nowrap;
    padding: 0 2px; border-left: 1px solid #2e2e36; }
  .word.hit { color: var(--ink); }
  .markers { position: absolute; left: 0; right: 0; top: 40px; bottom: 0; pointer-events: none; }
  .tmark { position: absolute; top: 0; width: 0; height: 100%; border-left: 1px solid var(--accent);
    opacity: .55; }
  .playhead { position: absolute; top: 0; bottom: 0; width: 0; border-left: 2px solid var(--accent);
    pointer-events: none; z-index: 4; }
  .playhead:before { content: ""; position: absolute; top: 0; left: -6px; border: 6px solid transparent;
    border-top: 8px solid var(--accent); }
  .range { position: absolute; top: 0; bottom: 0; background: rgba(0,229,255,.12);
    border-left: 1px solid var(--accent); border-right: 1px solid var(--accent); pointer-events: none; z-index: 2; }
  #panel { margin-top: 12px; display: grid; grid-template-columns: 1fr; gap: 10px; }
  @media (min-width: 760px) { #panel { grid-template-columns: 1.3fr 1fr; } }
  .card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 10px 12px; min-height: 72px;
    min-width: 0; overflow: hidden; }
  .card h2 { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: var(--mute); text-transform: uppercase; letter-spacing: .08em; }
  dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 2px 12px; margin: 0; font-variant-numeric: tabular-nums; }
  dt { color: var(--mute); } dd { margin: 0; overflow-wrap: anywhere; }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  button { font: inherit; font-size: 12px; padding: 5px 10px; border-radius: 6px; border: 1px solid #3a3a44;
    background: #1b1b21; color: var(--ink); cursor: pointer; }
  button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
  button:disabled { opacity: .45; cursor: default; }
  input[type=text] { font: inherit; font-size: 12px; padding: 5px 8px; border-radius: 6px; border: 1px solid #3a3a44;
    background: #0f0f13; color: var(--ink); flex: 1 1 120px; min-width: 0; max-width: 100%; }
  #log { white-space: pre-wrap; overflow-wrap: anywhere; font: 11px/1.45 ui-monospace, Menlo, monospace; color: var(--mute);
    max-height: 180px; overflow: auto; margin: 0; }
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
  var PAYLOAD_OPEN = "```json fcp-timeline-app", PAYLOAD_CLOSE = "```";
  var state = { payload: null, pps: 24, playhead: 0, sel: null, range: null, pending: {}, nextId: 1, inHost: false, hostContext: null };
  var $ = function (id) { return document.getElementById(id); };

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
    else if (m.method === "ui/notifications/host-context-changed") state.hostContext = m.params || state.hostContext;
    else if (m.id != null && m.method) send({ jsonrpc: "2.0", id: m.id, result: {} });
  });
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
    state.payload = p; state.sel = null; state.range = null; state.playhead = 0;
    $("empty").hidden = true; $("scroller").hidden = false; $("panel").hidden = false;
    $("title").textContent = p.name || "timeline";
    $("meta").textContent = p.clips.length + " clips · " + secOfSeconds(frac(p.duration)) + " · " + p.width + "×" + p.height + " @ " + p.fps_label + "fps" + (p.gap_based ? " · gap-based storyline" : "");
    fit(); draw(); updateSel();
  }
  var GUTTER = 48, MIN_PPS = 8;
  function extent() {
    // Fit to what is cut, not to the declared duration: a 120s sequence with
    // 54s of picture would otherwise spend half the width on the empty tail.
    var p = state.payload, end = 0;
    p.clips.forEach(function (c) { end = Math.max(end, frac(c.end)); });
    return end || frac(p.duration) || 1;
  }
  function fit() {
    var w = $("scroller").clientWidth - GUTTER - 24;
    state.pps = Math.max(MIN_PPS, w / extent());
  }
  function x(sec) { return GUTTER + sec * state.pps; }   // a position
  function w(sec) { return sec * state.pps; }            // a width: no gutter
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function draw() {
    var p = state.payload, canvas = $("canvas"); canvas.textContent = "";
    var dur = frac(p.duration), width = Math.max(x(dur) + 24, $("scroller").clientWidth - 2);
    canvas.style.width = width + "px";
    // ruler: ticks on the lower row, timeline marker names on the upper row
    var ruler = el("div", "ruler");
    var step = niceStep(dur, width - GUTTER);
    for (var t = 0; t <= dur; t += step) { var k = el("div", "tick", tcOf(t)); k.style.left = x(t) + "px"; ruler.appendChild(k); }
    var lastLabelEnd = -1;
    p.markers.forEach(function (m) {
      var left = x(frac(m.start));
      var lab = el("div", "mlabel", left - lastLabelEnd > 8 ? m.name : ""); lab.style.left = left + "px"; lab.title = m.name;
      ruler.appendChild(lab); lastLabelEnd = Math.max(lastLabelEnd, left + (lab.textContent ? m.name.length * 6 + 10 : 2));
    });
    ruler.addEventListener("mousedown", rulerDown);
    canvas.appendChild(ruler);
    var byId = {}; p.clips.forEach(function (c) { byId[c.id] = c; });
    p.lanes.forEach(function (lane) {
      var row = el("div", "lane " + lane.kind);
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
    p.markers.forEach(function (m) { var me = el("div", "tmark"); me.style.left = x(frac(m.start)) + "px"; me.title = m.name; marks.appendChild(me); });
    canvas.appendChild(marks);
    var ph = el("div", "playhead"); ph.id = "playhead"; canvas.appendChild(ph);
    var rg = el("div", "range"); rg.id = "range"; rg.hidden = true; canvas.appendChild(rg);
    placePlayhead();
  }
  function clipEl(c) {
    var e = el("div", "clip" + (c.kind === "title" ? " title" : "") + (state.sel === c.id ? " selected" : ""));
    e.style.left = x(frac(c.start)) + "px"; e.style.width = Math.max(3, w(frac(c.duration)) - 1) + "px";
    e.title = c.name + " · " + secOfSeconds(frac(c.duration));
    if (c.thumb && c.thumb.indexOf("data:image/") === 0) { var img = document.createElement("img"); img.src = c.thumb; img.alt = ""; e.appendChild(img); }
    (c.markers || []).forEach(function (m) { var mk = el("div", "mk"); mk.style.left = w(frac(m.start) - frac(c.start)) + "px"; mk.title = m.name; e.appendChild(mk); });
    var wpx = w(frac(c.duration));
    if (wpx >= 28) { var lab = el("div", "label", c.name); if (c.role && wpx >= 90) lab.appendChild(el("span", "role", c.role)); e.appendChild(lab); }
    e.addEventListener("click", function (ev) { ev.stopPropagation(); select(c.id); state.playhead = frac(c.start); placePlayhead(); });
    return e;
  }
  function niceStep(dur, width) {
    var target = 110 / (width / dur), steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    for (var i = 0; i < steps.length; i++) if (steps[i] >= target) return steps[i];
    return 600;
  }
  function placePlayhead() { var ph = $("playhead"); if (ph) ph.style.left = x(state.playhead) + "px"; $("tc").textContent = tcOf(state.playhead);
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
  window.addEventListener("resize", function () { if (state.payload) { draw(); } });
  window.__fcpApp = { setPayload: setPayload, extract: extract, state: state };

  // ---- boot --------------------------------------------------------------
  state.inHost = window.parent && window.parent !== window;
  if (state.inHost) {
    request("ui/initialize", { appInfo: { name: "fcp-mcp-server timeline", version: "1" }, appCapabilities: {}, protocolVersion: "2025-11-21" })
      .then(function (res) { state.hostContext = (res && res.hostContext) || null; notify("ui/notifications/initialized"); log("host initialized"); },
            function (err) { log("ui/initialize failed: " + err.message); });
  } else {
    log("not inside an MCP Apps host; waiting for a payload via window.__fcpApp.setPayload()");
  }
})();
</script>
</body>
</html>
"""
