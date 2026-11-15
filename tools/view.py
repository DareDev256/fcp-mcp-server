"""The view tool group — see the cut, inline, in the host.

Why a group of its own rather than a sixth ``preview`` action: MCP Apps binds
the UI to the TOOL. ``_meta.ui.resourceUri`` on ``preview`` would make a host
open the iframe for ``preview_render`` and ``preview_check`` too, whose
results carry no payload, and the app would sit there saying so. A tool
whose every result is renderable keeps the contract simple: call ``view``,
see the timeline.

Two actions, both read-only. ``view_timeline`` returns the payload the app
draws from (``fcpxml/timeline_app.py``) as the result's ``structuredContent``
and a short text a client without Apps can read (0.34: the thumbnails left
the text); ``view_clip`` is the "inspect" button. Edits never go through
here — the app calls ``edit``/``mark`` directly, so the journal, the project
guard and the review gate apply exactly as they do from chat.

Why the thumbnails ride in ``structuredContent`` and are not fetched by the
app afterwards: the ext-apps spec lets an app call a tool the model does not
see (``_meta.ui.visibility: ["app"]``; the host "MUST NOT include" it in the
agent's tool list, specification/draft/apps.mdx, Visibility), and the 2.x
SDK stamps that field (mcp/server/apps.py, ``Apps.tool(visibility=...)``),
but 1.28.1 has no such module and a client WITHOUT Apps lists every tool to
its model: a thumbnails tool would grow the advertised schema
(tests/test_schema_budget.py) and be one more tool the model can call. The
structured payload reaches the app in the same ``ui/notifications/tool-result``
and the spec keeps it out of the model's context, so nothing is fetched.
"""

from __future__ import annotations

import hashlib
import os
from fractions import Fraction
from pathlib import Path
from typing import Optional

import tools
from fcpxml import index as _index
from fcpxml import journal, render, timeline_app
from fcpxml.mcp_compat import ToolReply
from fcpxml.media_intel import media_src_to_path
from fcpxml.words import plural
from tools import find as _find
from tools._common import parse_project, text_result

UI_META = {"ui": {"resourceUri": timeline_app.APP_URI}}


def _thumbnail(media_src: str, at: Fraction) -> Optional[str]:
    """A tiny JPEG of the source at *at* seconds as a data URI, or None.

    Cached under the private preview cache keyed on the file's identity and
    the position, so redrawing a timeline does not re-run ffmpeg per clip.
    Degrades to None (a coloured block in the app) when ffmpeg or the media
    is absent; it never raises into the payload builder.
    """
    path = media_src_to_path(media_src or "")
    if not path or not Path(path).is_file():
        return None
    try:
        st = os.stat(path)
        # the size and quality are in the key: 0.34 draws thumbnails at 180 px,
        # and a 108 px one cached by 0.33 would otherwise come back upscaled
        key = hashlib.sha256(
            f"{path}|{st.st_mtime_ns}|{st.st_size}|{at}|{timeline_app.THUMBNAIL_SHORT_SIDE}"
            f"|q{timeline_app.THUMBNAIL_QUALITY}".encode("utf-8")
        ).hexdigest()[:20]
        out_dir = render.cache_dir() / "thumbs"
        out_dir.mkdir(exist_ok=True)
        target = out_dir / f"{key}.jpg"
        if not target.is_file():
            written = render.render_frame(
                path, at, str(target), max_short_side=timeline_app.THUMBNAIL_SHORT_SIDE,
                quality=timeline_app.THUMBNAIL_QUALITY,
            )
            if not written:
                return None
        return timeline_app.data_uri_jpeg(str(target))
    except OSError:
        return None


# The waveform (0.34): one peak envelope per FILE, PEAK_RATE levels a second
# (0-100 over -60..0 dBFS: the app draws them on that absolute scale, round
# 4), measured once with ffmpeg and cached in the index as analysis kind
# "peaks" (the floor is in the kind, so a cache written on the old -48 dB
# scale is never read as this one); each clip then takes its own range of
# it, down to timeline_app.WAVE_POINTS_PER_SECOND points (the loudest in
# each), so a short array travels. FCP_MCP_INDEX=off measures every time.
PEAK_RATE = 10
PEAK_AUDIO_RATE = 4000
PEAK_FLOOR_DB = -60.0
PEAKS_KIND = f"peaks@{PEAK_RATE}/s,{int(PEAK_FLOOR_DB)}dB"


def _measure_peaks(path: str) -> Optional[list]:
    """The file's peak envelope, or [] for a file with no sound, or None
    when ffmpeg is missing or cannot read it."""
    import array
    import math
    import subprocess

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:a:0", "-ac", "1",
           "-ar", str(PEAK_AUDIO_RATE), "-f", "s16le", "pipe:1"]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        # no audio stream is an answer (no waveform), not a failure to retry
        return [] if b"matches no streams" in run.stderr or b"does not contain any stream" in run.stderr else None
    samples = array.array("h")
    samples.frombytes(run.stdout[: len(run.stdout) // 2 * 2])
    per = PEAK_AUDIO_RATE // PEAK_RATE
    out = []
    for i in range(0, len(samples), per):
        chunk = samples[i:i + per]
        peak = max(max(chunk), -min(chunk)) / 32768 if chunk else 0
        db = 20 * math.log10(peak) if peak > 0 else PEAK_FLOOR_DB
        out.append(int(round(max(0.0, min(1.0, (db - PEAK_FLOOR_DB) / -PEAK_FLOOR_DB)) * 100)))
    return out


def _file_peaks(path: str) -> Optional[list]:
    ix = _index.Index.open()
    if ix is None:
        return _measure_peaks(path)
    with ix:
        rows = ix.get_analysis(path, PEAKS_KIND)
        if rows is not None:
            return rows[0]["payload"]["levels"] if rows else []
        levels = _measure_peaks(path)
        if levels is not None:
            ix.put_analysis(path, PEAKS_KIND, [{"start": 0, "end": Fraction(len(levels), PEAK_RATE),
                                                "payload": {"levels": levels}}] if levels else [])
        return levels


def _waveform(media_src: str, file_start: Fraction, duration: Fraction) -> Optional[list]:
    """Levels 0-100 for ``duration`` seconds of the file from ``file_start``:
    the loudest of the file's envelope in each of the clip's points."""
    path = media_src_to_path(media_src or "")
    if not path or not Path(path).is_file() or duration <= 0:
        return None
    levels = _file_peaks(path)
    if not levels:
        return None
    n = max(2, min(timeline_app.MAX_WAVE_POINTS, round(duration * timeline_app.WAVE_POINTS_PER_SECOND)))
    out = []
    for i in range(n):
        a = int((file_start + duration * i / n) * PEAK_RATE)
        b = max(a + 1, int((file_start + duration * (i + 1) / n) * PEAK_RATE + Fraction(999, 1000)))
        window = levels[max(0, a):max(0, b)]
        out.append(max(window) if window else 0)
    return out


def _transcript(media_src: str) -> Optional[dict]:
    """index -> sidecar -> None. Never transcribes (same rule as find)."""
    path = media_src_to_path(media_src or "")
    if not path:
        return None
    return _find._transcript_for(path)


def _timeline_or_message(filepath):
    if not filepath:
        return None, None, "This action requires 'filepath'."
    try:
        validated = tools.server_module()._validate_filepath(filepath, ('.fcpxml', '.fcpxmld'))
        _project, timeline = parse_project(validated)
    except (ValueError, OSError) as exc:
        return None, None, f"Could not read {filepath}: {exc}"
    if timeline is None:
        return None, None, "No timelines found"
    return validated, timeline, None


def _plural(n: int, word: str) -> str:
    return plural(n, word)


def _marker_phrase(payload: dict) -> str:
    """Every marker the app draws: on the sequence AND on clips.

    Until 0.31 this counted sequence markers only, so a cut whose chapter
    markers sit on their clips (how Final Cut and ``mark`` write them) read
    "0 timeline markers" under a ruler showing three flags.
    """
    on_clips = sum(len(c["markers"]) for c in payload["clips"])
    total = len(payload["markers"]) + on_clips
    return f"{_plural(total, 'marker')} ({on_clips} on clips)" if on_clips else _plural(total, "marker")


def _gap_phrase(timeline) -> str:
    """Gaps counted exactly as ``diagnose`` counts them: one definition.

    The payload's ``gaps`` list is what the app HATCHES (leading and trailing
    space, and on a gap-based storyline the whole spine), which is right to
    draw and wrong to count: ``view`` said "1 gaps" where
    ``validate_timeline`` said 0 on the same music video. The count is the
    server's ``_detect_gaps``, the function ``diagnose`` reports.
    """
    count = len(tools.server_module()._detect_gaps(timeline))
    phrase = _plural(count, "gap")
    return phrase + " (gap-based storyline)" if timeline.is_gap_based else phrase


async def handle_view_timeline(args: dict):
    validated, timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    want_thumbs = args.get("thumbnails", True) is not False
    want_words = args.get("words", True) is not False
    want_wave = args.get("waveforms", True) is not False
    sha = journal.file_hash(validated)
    payload = timeline_app.build_payload(
        timeline,
        filepath=validated,
        sha256=sha,
        thumbnail=_thumbnail if want_thumbs else None,
        transcript=_transcript if want_words else None,
        waveform=_waveform if want_wave else None,
    )
    thumbs = sum(1 for c in payload["clips"] if c["thumb"])
    words = sum(len(c["words"]) for c in payload["clips"])
    waves = sum(1 for c in payload["clips"] if c.get("wave"))
    summary = (
        f"**{timeline.name}** — {_plural(len(payload['clips']), 'clip')} across "
        f"{_plural(len(payload['lanes']), 'lane')}, {float(Fraction(payload['duration'])):.2f}s, "
        f"{_marker_phrase(payload)}, {_gap_phrase(timeline)}; "
        f"{plural(thumbs, 'thumbnail')}, {plural(waves, 'waveform')}, {plural(words, 'transcript word')}.\n"
        f"sha256: {sha}\n"
        "A host with MCP Apps draws this inline (`ui://fcp/timeline`) from the "
        "result's structured payload, every time an exact `numerator/denominator` "
        "in seconds; view_clip reads one clip in full."
    )
    return ToolReply(text_result(timeline_app.fallback_text(summary, payload)), payload)


def _fmt(value: Fraction) -> str:
    return f"{float(value):.3f}s ({value.numerator}/{value.denominator})"


async def handle_view_clip(args: dict):
    validated, timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    name = args.get("clip_name")
    if not name:
        return text_result("view_clip requires 'clip_name'.")
    payload = timeline_app.build_payload(
        timeline, filepath=validated, sha256=journal.file_hash(validated),
        transcript=_transcript,
    )
    hits = [c for c in payload["clips"] if c["name"] == name]
    if not hits:
        names = ", ".join(sorted({c["name"] for c in payload["clips"]})[:20])
        return text_result(f"No clip named '{name}'. Clips: {names}")
    lines = [f"# {name}", f"File: {validated}", f"sha256: {payload['file']['sha256']}", ""]
    for c in hits:
        start, dur = Fraction(c["start"]), Fraction(c["duration"])
        media_path = media_src_to_path(c["media"] or "")
        present = "present" if media_path and Path(media_path).is_file() else "MISSING"
        where = "spine" if c["kind"] == "spine" else f"lane {c['lane']}"
        lines += [
            f"## {c['id']} — {where} ({c['kind']}, {c['type']})",
            f"- timeline: {_fmt(start)} → {_fmt(start + dur)}",
            f"- duration: {_fmt(dur)}",
            f"- source in: {_fmt(Fraction(c['source_start']))}",
            f"- role: {c['role'] or '—'}",
            f"- media: {c['media'] or '—'} ({present})" if c["media"] else "- media: — (no media)",
            f"- keywords: {', '.join(c['keywords']) or '—'}",
        ]
        if c["markers"]:
            lines.append("- markers: " + "; ".join(
                f"{m['name']} @ {float(Fraction(m['start'])):.3f}s" for m in c["markers"]
            ))
        if c["words"]:
            lines.append("- said: " + " ".join(w["w"] for w in c["words"])[:600])
        else:
            lines.append("- transcript: none cached (transcript_media makes one; never done implicitly)")
        lines.append("")
    return text_result("\n".join(lines))


ACTIONS = {
    "view_timeline": handle_view_timeline,
    "view_clip": handle_view_clip,
}

DESCRIPTION = (
    "See the cut inline. view_timeline(filepath, thumbnails=true, words=true) "
    "returns the timeline — lanes, clips, markers, gaps, cached transcript "
    "words and filmstrip thumbnails — as an exact rational-time JSON payload "
    "that hosts with MCP Apps render as an interactive timeline (clip "
    "details, range selection, inspect / find-similar / marker / delete "
    "buttons that call the other tools). view_clip(filepath, clip_name) is "
    "the inspect button. Read-only; edits go through edit and mark."
)
