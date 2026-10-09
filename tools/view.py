"""The view tool group — see the cut, inline, in the host.

Why a group of its own rather than a sixth ``preview`` action: MCP Apps binds
the UI to the TOOL. ``_meta.ui.resourceUri`` on ``preview`` would make a host
open the iframe for ``preview_render`` and ``preview_check`` too, whose
results carry no payload, and the app would sit there saying so. A tool
whose every result is renderable keeps the contract simple: call ``view``,
see the timeline.

Two actions, both read-only. ``view_timeline`` returns the payload the app
draws from (``fcpxml/timeline_app.py``); ``view_clip`` is the "inspect"
button. Edits never go through here — the app calls ``edit``/``mark``
directly, so the journal, the project guard and the review gate apply
exactly as they do from chat.
"""

from __future__ import annotations

import hashlib
import os
from fractions import Fraction
from pathlib import Path
from typing import Optional

import tools
from fcpxml import journal, render, timeline_app
from fcpxml.media_intel import media_src_to_path
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
        key = hashlib.sha256(
            f"{path}|{st.st_mtime_ns}|{st.st_size}|{at}".encode("utf-8")
        ).hexdigest()[:20]
        out_dir = render.cache_dir() / "thumbs"
        out_dir.mkdir(exist_ok=True)
        target = out_dir / f"{key}.jpg"
        if not target.is_file():
            written = render.render_frame(
                path, at, str(target), max_short_side=timeline_app.THUMBNAIL_SHORT_SIDE
            )
            if not written:
                return None
        return timeline_app.data_uri_jpeg(str(target))
    except OSError:
        return None


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


async def handle_view_timeline(args: dict):
    validated, timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    want_thumbs = args.get("thumbnails", True) is not False
    want_words = args.get("words", True) is not False
    sha = journal.file_hash(validated)
    payload = timeline_app.build_payload(
        timeline,
        filepath=validated,
        sha256=sha,
        thumbnail=_thumbnail if want_thumbs else None,
        transcript=_transcript if want_words else None,
    )
    thumbs = sum(1 for c in payload["clips"] if c["thumb"])
    words = sum(len(c["words"]) for c in payload["clips"])
    summary = (
        f"**{timeline.name}** — {len(payload['clips'])} clips across "
        f"{len(payload['lanes'])} lanes, {float(Fraction(payload['duration'])):.2f}s, "
        f"{len(payload['markers'])} timeline markers, {len(payload['gaps'])} gaps; "
        f"{thumbs} thumbnails, {words} transcript words.\n"
        f"sha256: {sha}\n"
        "A host with MCP Apps renders this inline (`ui://fcp/timeline`); "
        "otherwise the JSON below is the timeline, every time an exact "
        "`numerator/denominator` in seconds."
    )
    return text_result(timeline_app.wrap_payload(summary, payload))


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
