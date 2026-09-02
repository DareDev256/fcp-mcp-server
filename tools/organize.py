"""The organize tool group — bulk library logging, and the ledger behind it.

``organize_keywords`` / ``organize_rate`` / ``organize_roles`` apply one edit
to every clip a selection matches and write a ``_organized`` copy. ``history``
reads the folder's journal; ``undo`` parks the outputs of the last write
operations (moves, never deletes — see ``fcpxml.journal``).
"""

import json
import time
from collections import Counter
from pathlib import Path

import tools
from fcpxml import find as _find
from fcpxml import index as _index
from fcpxml import journal
from fcpxml.media_intel import media_src_to_path
from fcpxml.writer import FCPXMLModifier
from tools._common import parse_project, text_result

_JOURNAL_OFF = (
    "The journal is off (FCP_MCP_JOURNAL is set to a disabling value), so "
    "nothing was recorded. Unset it, or point it at a directory, to keep a ledger."
)
_SELECTION_KEYS = ("clip_name", "keyword", "role")


def _select(args: dict):
    """(modifier, clips, description of the selection) — or refuses with text."""
    srv = tools.server_module()
    filepath = srv._validate_filepath(args["filepath"])
    m = FCPXMLModifier(filepath)
    filters = {k: args[k] for k in _SELECTION_KEYS if args.get(k)}
    clips = m.select_clips(**filters)
    desc = ", ".join(f"{k}={v!r}" for k, v in filters.items()) or "every clip"
    return srv, filepath, m, clips, desc


def _write(srv, filepath: str, m: FCPXMLModifier, args: dict) -> str:
    out = args.get("output_path") or srv.generate_output_path(filepath, "_organized")
    out = srv._validate_output_path(out, anchor_dir=str(Path(filepath).parent))
    return m.save(out)


def _nothing(desc: str, clips) -> list:
    return text_result(f"No clips matched: {len(clips)} clips for {desc}. Nothing written.")


async def handle_organize_keywords(args: dict) -> list:
    keywords = args.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        raise ValueError("keywords must be a non-empty list of strings")
    mode = args.get("mode", "add")
    srv, filepath, m, clips, desc = _select(args)
    if not clips:
        return _nothing(desc, clips)
    touched = m.bulk_keywords(clips, [str(k) for k in keywords], mode)
    out = _write(srv, filepath, m, args)
    return text_result(
        f"**Keywords {mode}**: {', '.join(str(k) for k in keywords)}\n"
        f"Selection: {desc} — {len(clips)} clips, {touched} changed.\n"
        f"Written: {out}"
    )


async def handle_organize_rate(args: dict) -> list:
    rating = args.get("rating")
    if rating not in ("favorite", "rejected", "clear"):
        raise ValueError(f"rating must be favorite, rejected or clear, got {rating!r}")
    srv, filepath, m, clips, desc = _select(args)
    if not clips:
        return _nothing(desc, clips)
    m.bulk_rating(clips, rating)
    out = _write(srv, filepath, m, args)
    verb = "Rating cleared" if rating == "clear" else f"Rated {rating}"
    return text_result(f"**{verb}**: {desc} — {len(clips)} clips.\nWritten: {out}")


async def handle_organize_roles(args: dict) -> list:
    audio_role = args.get("audio_role")
    video_role = args.get("video_role")
    if not audio_role and not video_role:
        raise ValueError("give audio_role and/or video_role")
    srv, filepath, m, clips, desc = _select(args)
    if not clips:
        return _nothing(desc, clips)
    m.bulk_roles(clips, audio_role=audio_role, video_role=video_role)
    out = _write(srv, filepath, m, args)
    roles = ", ".join(f"{k}={v}" for k, v in (("audio", audio_role), ("video", video_role)) if v)
    return text_result(f"**Roles set**: {roles}\nSelection: {desc} — {len(clips)} clips.\nWritten: {out}")


def _age(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


async def handle_history(args: dict) -> list:
    srv = tools.server_module()
    filepath = srv._validate_filepath(args["filepath"])
    if not journal.enabled():
        return text_result(_JOURNAL_OFF)
    limit = int(args.get("limit", 20))
    if not (1 <= limit <= 500):
        raise ValueError("limit must be between 1 and 500")
    rows = journal.records(filepath, limit=limit)
    if not rows:
        return text_result(f"No operations recorded for the folder of {Path(filepath).name}.")
    lines = [
        f"**History** — {Path(filepath).parent} ({len(rows)} most recent)\n",
        "| When | Tool | Action | Input | Output | Output hash |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        inp = (r.get("input") or {}).get("path") or ""
        out = (r.get("output") or {}).get("path") or ""
        sha = ((r.get("output") or {}).get("sha256") or "")[:12]
        extra = f" → {Path(r['moved_to']).name}" if r.get("moved_to") else ""
        lines.append(
            f"| {_age(r.get('ts', 0))} | {r.get('tool', '')} | {r.get('action', '')} | "
            f"{Path(inp).name if inp else ''} | {Path(out).name if out else ''}{extra} | {sha} |"
        )
    return text_result("\n".join(lines))


async def handle_undo(args: dict) -> list:
    srv = tools.server_module()
    filepath = srv._validate_filepath(args["filepath"])
    if not journal.enabled():
        return text_result(_JOURNAL_OFF)
    n = int(args.get("n", 1))
    if not (1 <= n <= 50):
        raise ValueError("n must be between 1 and 50")
    moved = journal.undo(filepath, n)
    if not moved:
        return text_result("Nothing to undo.")
    lines = [f"**Undid {len(moved)} operation(s)** — nothing was deleted:"]
    for e in moved:
        lines.append(f"- {e['undid']}: {Path(e['output']['path']).name} moved to {e['moved_to']}")
    return text_result("\n".join(lines))


def _derivable_text(media_path: str):
    """(caption text, transcript text) for a source — index, then sidecar. Never computes."""
    captions, transcript = "", ""
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            rows = ix.get_shots(media_path) or []
            data = ix.get_transcript(media_path)
        captions = " ".join(r.get("caption") or "" for r in rows)
        if data is not None:
            transcript = data.get("text", "")
    if not transcript:
        sidecar = tools.server_module()._transcript_json_path(media_path)
        if sidecar.is_file():
            try:
                with open(sidecar) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    transcript = str(data.get("text", ""))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                pass
    return captions, transcript


def _propose(clip, max_keywords: int):
    """(keywords, sources) proposed for one parsed clip."""
    media_path = media_src_to_path(clip.media_path or "")
    if not media_path:
        return [], []
    captions, transcript = _derivable_text(media_path)
    existing = {t for k in clip.keyword_values for t in _find.tokens(k)}
    counts: Counter = Counter()
    sources = []
    for label, text in (("captions", captions), ("transcript", transcript)):
        toks = [t for t in _find.tokens(text) if len(t) >= 4 and t not in existing]
        if toks:
            sources.append(label)
            counts.update(toks)
    return [w for w, _ in counts.most_common(max_keywords)], sources


async def handle_organize_auto(args: dict) -> list:
    max_keywords = int(args.get("max_keywords", 3))
    if not (1 <= max_keywords <= 20):
        raise ValueError("max_keywords must be between 1 and 20")
    srv = tools.server_module()
    filepath = srv._validate_filepath(args["filepath"])
    _, tl = parse_project(filepath)
    clip_filter = args.get("clip_name")
    proposals, bare = [], []
    for clip in tl.clips:
        if clip_filter and clip.name != clip_filter:
            continue
        words, sources = _propose(clip, max_keywords)
        if words:
            proposals.append((clip.name, words, sources))
        else:
            bare.append(clip.name)
    out = "# Auto Keywords\n\n"
    if proposals:
        out += srv._markdown_table(
            ["Clip", "Proposed", "From"],
            [[name, ", ".join(words), " + ".join(sources)] for name, words, sources in proposals],
        ) + "\n"
    if bare:
        out += (f"\n**Nothing to derive from** ({len(bare)}): {', '.join(bare[:10])} — "
                "no captions or transcript yet; run `find_index` (and `transcript_media`) first.\n")
    if not proposals:
        return text_result(out + "\nNothing proposed. Nothing written.")
    if not args.get("apply"):
        call = json.dumps({"action": "organize_auto", "args": {"filepath": filepath, "apply": True,
                                                                "max_keywords": max_keywords}})
        return text_result(out + f"\nProposal only — nothing written. To apply: organize {call}")
    m = FCPXMLModifier(filepath)
    touched = 0
    for name, words, _ in proposals:
        touched += m.bulk_keywords(m.select_clips(clip_name=name), words, "add")
    written = _write(srv, filepath, m, args)
    return text_result(out + f"\nApplied to {touched} clips.\nWritten: {written}")


ACTIONS = {
    "organize_auto": handle_organize_auto,
    "organize_keywords": handle_organize_keywords,
    "organize_rate": handle_organize_rate,
    "organize_roles": handle_organize_roles,
    "history": handle_history,
    "undo": handle_undo,
}

DESCRIPTION = (
    "Bulk library logging and the ledger. organize_auto proposes keywords per clip "
    "from cached captions and the transcript (apply=true writes them). Select clips with clip_name (glob), "
    "keyword and/or role, then organize_keywords (keywords, mode add|remove|replace), "
    "organize_rate (rating favorite|rejected|clear) or organize_roles (audio_role, "
    "video_role); each writes a _organized copy. history lists every recorded "
    "operation for the file's folder (limit); undo (n) moves the outputs of the "
    "last n writes into the journal's undone/ folder — it never deletes and refuses "
    "when a file changed since it was written."
)
