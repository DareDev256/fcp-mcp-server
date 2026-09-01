"""The index tool group — status, warm-up, and the off switch.

The index is a cache. Nothing here is required for any other tool to give a
correct answer; ``index_build`` only moves the cost of the first question to
a moment the operator chose. ``index_status`` publishes the cache's own age,
because a right answer built on stale analysis is the failure nobody sees.
"""

from pathlib import Path

from fcpxml import index as _index
from fcpxml import progress as _progress
from fcpxml.media_intel import media_src_to_path
from fcpxml.render import probe_duration
from tools import _common
from tools._common import parse_project, text_result

# Same ceiling detect_media_silence applies — an index build is a warm-up,
# not a licence to walk a 2TB shoot on one call.
MAX_BUILD_MEDIA = 100
_DISABLED = "Index disabled (FCP_MCP_INDEX=off). Every tool still works; it just recomputes."


def _age(seconds):
    if seconds is None:
        return "empty"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


async def handle_index_status(args: dict):
    ix = _index.Index.open()
    if ix is None:
        return text_result(_DISABLED)
    with ix:
        s = ix.stats()
        path = ix.path
    return text_result(
        "# Index\n"
        f"- **Path**: {path}\n"
        f"- **Media rows**: {s['media']}\n"
        f"- **Transcript words**: {s['transcript']}\n"
        f"- **Analysis rows**: {s['analysis']}\n"
        f"- **Shots**: {s['shot']}\n"
        f"- **Oldest entry**: {_age(s['oldest_age'])}\n\n"
        "Rows are keyed to (path, mtime, size); a re-exported source is re-analysed "
        "on its next use, not on a schedule."
    )


def _sources(timeline) -> list[str]:
    seen: list[str] = []
    for clip in timeline.clips:
        media_path = media_src_to_path(clip.media_path or "")
        if media_path and media_path not in seen:
            seen.append(media_path)
    return seen


async def handle_index_build(args: dict):
    if _index.Index.open() is None:
        return text_result(_DISABLED)
    _, tl = parse_project(args["filepath"])
    with_transcript = bool(args.get("with_transcript", False))
    srv = _common.tools.server_module()

    sources = _sources(tl)
    capped = sources[MAX_BUILD_MEDIA:]
    sources = sources[:MAX_BUILD_MEDIA]
    prog = _progress.start(total=len(sources))
    rows: list[list[str]] = []
    missing: list[str] = []
    for media_path in sources:
        name = Path(media_path).name
        await prog.step(f"index: {name}")
        if not Path(media_path).is_file():
            missing.append(name)
            continue
        duration = probe_duration(media_path)
        silences = srv._silence_cached(media_path, -30.0, 0.5)
        transcript = None
        if with_transcript:
            transcript, _ = srv._load_or_transcribe(media_path, "base", None)
        rows.append([
            name,
            f"{float(duration):.2f}s" if duration is not None else "unprobed",
            str(len(silences)) if silences is not None else "unanalyzable",
            ("yes" if transcript else "no") if with_transcript else "skipped",
        ])

    out = f"# Index Build\n\n- **Sources**: {len(rows)} indexed, {len(missing)} missing\n\n"
    if rows:
        out += srv._markdown_table(["Media", "Duration", "Silences", "Transcript"], rows) + "\n"
    if missing:
        out += "\n## Missing media\n" + "\n".join(f"- {m}" for m in missing) + "\n"
    if capped:
        out += f"\n*{len(capped)} more sources not indexed (cap {MAX_BUILD_MEDIA} per call).*\n"
    return text_result(out)


async def handle_index_clear(args: dict):
    ix = _index.Index.open()
    if ix is None:
        return text_result(_DISABLED)
    with ix:
        before = ix.stats()["media"]
        ix.clear()
    return text_result(f"Index cleared: {before} media rows dropped. Nothing is lost — it is a cache.")


ACTIONS = {
    "index_status": handle_index_status,
    "index_build": handle_index_build,
    "index_clear": handle_index_clear,
}

DESCRIPTION = (
    "The analysis cache under ~/.fcp-mcp/index.db. index_status shows what is "
    "cached and how old it is; index_build warms it for every source in a "
    "timeline (args: filepath, with_transcript); index_clear drops it. Every "
    "other tool works with the index off (FCP_MCP_INDEX=off) — this only "
    "makes the second question fast."
)
