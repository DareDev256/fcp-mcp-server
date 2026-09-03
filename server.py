#!/usr/bin/env python3
"""
FCPXML MCP Server — Batch operations and analysis for Final Cut Pro XML files.

Provides 53 tools, MCP resources for file discovery, and pre-built prompt
workflows for common editing tasks.

Author: DareDev256 (https://github.com/DareDev256)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, unquote

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

from fcpxml import diversity as _diversity
from fcpxml import index as _index
from fcpxml import journal as _journal
from fcpxml import live
from fcpxml import progress as _progress
from fcpxml import transcript_pack as _tpack
from fcpxml.diff import compare_timelines, format_diff
from fcpxml.mcp_compat import register_handlers, tool_input_schema
from fcpxml.media_intel import (
    detect_beats,
    detect_silence,
    map_silence_to_timeline,
    media_src_to_path,
)
from fcpxml.models import (
    DuplicateGroup,
    FlashFrame,
    FlashFrameSeverity,
    GapInfo,
    MarkerType,
    SegmentSpec,
    Timecode,
    TimeValue,
)
from fcpxml.parser import FCPXMLParser
from fcpxml.preview import render_timeline_html
from fcpxml.rational import fcp_frame_rate_name
from fcpxml.rough_cut import RoughCutGenerator
from fcpxml.transcribe import (
    BACKENDS,
    DEFAULT_FILLERS,
    SCRIBE_KEY_ENV,
    SCRIBE_MODEL,
    find_filler_spans,
    find_phrase_spans,
    invert_ranges,
    is_diarized,
    merge_ranges,
    segments_to_srt,
    transcribe,
)
from fcpxml.writer import FCPXMLModifier

__version__ = "0.21.2"

server = Server("fcp-mcp-server", version=__version__)


def _parse_allowed_roots(
    env: Mapping[str, str], *, include_legacy: bool = True
) -> list[str]:
    """Build a sandbox root allowlist from the environment.

    ``FCP_PROJECTS_DIRS`` accepts several roots separated by ``os.pathsep``
    (``:`` on macOS/Linux, ``;`` on Windows), like ``PATH``.

    *include_legacy* controls whether the original single-root
    ``FCP_PROJECTS_DIR`` is folded in.  It is True for the **listing**
    allowlist — that is exactly what the variable has always done — and False
    for the **read** allowlist, because confining reads is new behaviour and
    must not be switched on under someone by a variable they already set.

    An empty list means *no confinement*.  Roots that do not resolve (typo,
    unmounted drive) are dropped rather than raising, so a stale entry cannot
    take the whole server down at import time.
    """
    raw_parts: list[str] = []
    multi = env.get("FCP_PROJECTS_DIRS", "")
    raw_parts.extend(multi.split(os.pathsep))
    if include_legacy:
        single = env.get("FCP_PROJECTS_DIR")
        if single is not None:
            raw_parts.append(single)

    roots: list[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        try:
            resolved = str(Path(os.path.expanduser(part)).resolve())
        except (OSError, ValueError):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _read_roots_from_env(env: Mapping[str, str]) -> list[str]:
    """Roots that confine READS — ``FCP_PROJECTS_DIRS`` only.

    The legacy ``FCP_PROJECTS_DIR`` deliberately does NOT feed this list. The
    README has always told users to run
    ``claude mcp add fcpxml -e FCP_PROJECTS_DIR=~/Movies``, and it has always
    meant "where to look for projects". Quietly promoting it to "the only place
    you may open a file from" breaks every installation that followed the docs
    the moment they upgrade.
    """
    return _parse_allowed_roots(env, include_legacy=False)


def _list_roots_from_env(env: Mapping[str, str]) -> list[str]:
    """Roots that confine LISTING — unchanged from 0.15.0.

    ``FCP_PROJECTS_DIR`` confines ``list_projects`` exactly as it always has;
    ``FCP_PROJECTS_DIRS`` adds more roots to that same allowlist.
    """
    return _parse_allowed_roots(env, include_legacy=True)


READ_ROOTS = _read_roots_from_env(os.environ)
LIST_ROOTS = _list_roots_from_env(os.environ)

PROJECTS_DIR = os.environ.get(
    "FCP_PROJECTS_DIR",
    LIST_ROOTS[0] if LIST_ROOTS else os.path.expanduser("~/Movies"),
)
# When roots are configured explicitly, confine discovery.
_SANDBOX_ENABLED = bool(LIST_ROOTS)

# Maximum file size for parsing (100 MB).
MAX_FILE_SIZE = 100 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    """Read a positive integer cap from the environment, falling back cleanly."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return value if value > 0 else default


# Maximum number of files a single directory walk may collect.  An unbounded
# rglob driven by a caller-supplied directory (``list_projects`` on ``/``) walks
# the entire filesystem; this stops the walk, and callers report the truncation
# instead of presenting a partial list as if it were complete.
MAX_DISCOVERY_FILES = _env_int("FCP_MAX_DISCOVERY_FILES", 10000)

# Maximum markers written by one batch/import operation.
MAX_BATCH_MARKERS = _env_int("FCP_MAX_BATCH_MARKERS", 10000)

# Maximum length of inline transcript text accepted by import_transcript_markers.
MAX_INLINE_TRANSCRIPT_CHARS = _env_int("FCP_MAX_TRANSCRIPT_CHARS", 1024 * 1024)


# ============================================================================
# SECURITY UTILITIES
# ============================================================================

# Maximum nesting depth for JSON deserialization (beat markers, configs).
# Prevents stack overflow / memory exhaustion from deeply nested payloads.
_MAX_JSON_DEPTH = 50


def _check_json_depth(obj: object, _depth: int = 0) -> None:
    """Reject JSON structures nested beyond _MAX_JSON_DEPTH.

    Prevents denial-of-service via deeply nested objects that exhaust the
    call stack or memory during downstream processing.  Called after
    json.load() since Python's json module has no built-in depth limit.
    """
    if _depth > _MAX_JSON_DEPTH:
        raise ValueError(
            f"JSON nesting depth exceeds {_MAX_JSON_DEPTH} — "
            "file may be malformed or adversarial"
        )
    if isinstance(obj, dict):
        for v in obj.values():
            _check_json_depth(v, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _check_json_depth(item, _depth + 1)


def _is_within_roots(resolved: Path, roots: Sequence[str]) -> bool:
    """True when *resolved* is a descendant of (or equal to) any allowed root.

    String comparison first, then an inode-identity fallback.  The fallback is
    not belt-and-braces: macOS filesystems are case-insensitive but
    ``Path.resolve()`` does *not* normalise case, so a root written
    ``/users/me/Movies`` never string-matches a file resolved as
    ``/Users/me/Movies`` and the user is locked out of their own library.
    Comparing ``os.stat`` results answers "same directory?" correctly on a
    case-insensitive filesystem without weakening a case-sensitive one, where
    two differently-cased directories genuinely have different inodes.
    """
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        return True

    root_stats = []
    for root in roots:
        try:
            root_stats.append(os.stat(root))
        except OSError:
            continue
    if not root_stats:
        return False

    # The path itself may not exist yet (output paths); its parents will.
    for candidate in (resolved, *resolved.parents):
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if any(os.path.samestat(st, rs) for rs in root_stats):
            return True
    return False


def _lexical_path(filepath: str) -> Path:
    """Absolute path with ``..`` collapsed but symlinks NOT followed.

    ``os.path.abspath`` applies ``normpath``, so traversal still normalises
    away before any containment check — ``root/../../etc`` becomes ``/etc``
    and is judged on where it actually points.
    """
    return Path(os.path.abspath(filepath))


def _within_any_root(filepath: str, roots: Sequence[str]) -> bool:
    """True when the path *as given* or its symlink target is inside a root.

    Checking the given path first is what makes a Final Cut library usable.
    FCP imports media "leave files in place" by default, so
    ``~/Movies/X.fcpbundle/.../Original Media/`` is full of symlinks pointing
    at wherever the footage actually lives — an external drive, a NAS, a
    scratch volume.  Judging only the resolved target rejects the file Final
    Cut itself put inside the root, which is the normal case for every real
    library, not an edge case.

    Traversal protection survives: ``..`` collapses lexically before the check,
    and a symlink that points *into* a root from outside still passes on its
    target, judged on where it lands.
    """
    if _is_within_roots(_lexical_path(filepath), roots):
        return True
    try:
        return _is_within_roots(Path(filepath).resolve(), roots)
    except (OSError, ValueError):
        return False


def _enforce_read_roots(filepath: str, what: str) -> None:
    """Confine a path to ``READ_ROOTS`` when the operator opted in.

    With no roots configured this is a no-op — the default, and what every
    installation that only sets the legacy ``FCP_PROJECTS_DIR`` gets.
    """
    roots = READ_ROOTS
    if not roots:
        return
    if _within_any_root(filepath, roots):
        return
    raise ValueError(
        f"{what} escapes the allowed roots: {_lexical_path(filepath)} is not "
        f"under any of {os.pathsep.join(roots)}. "
        f"Add its directory to FCP_PROJECTS_DIRS to allow it."
    )


def _validate_filepath(filepath: str, allowed_extensions: tuple[str, ...] | None = None) -> str:
    """Validate a user-provided file path against traversal and size attacks.

    Resolves symlinks, blocks null bytes, confines the path to ``READ_ROOTS``
    (only when ``FCP_PROJECTS_DIRS`` is set), enforces the extension whitelist,
    and checks file size before any parsing takes place.

    The extension whitelist is still applied to the *resolved* suffix, so a
    symlink named ``innocent.fcpxml`` pointing at ``/etc/passwd`` is rejected
    on its target's suffix regardless of any sandbox setting.

    Raises:
        ValueError: For invalid paths (null bytes, bad extensions, oversized,
            outside the configured sandbox roots).
        FileNotFoundError: When the resolved path does not exist.
    """
    if '\x00' in filepath:
        raise ValueError("Invalid file path: null byte detected")

    _enforce_read_roots(filepath, "File path")

    resolved = Path(filepath).resolve()

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    # .fcpxmld bundles are directories (a package wrapping Info.fcpxml plus
    # sidecar data files for object tracking / Cinematic mode).  The size
    # check applies to the inner Info.fcpxml, which is what gets parsed.
    if resolved.is_dir():
        if resolved.suffix.lower() != '.fcpxmld':
            raise ValueError(f"Not a regular file: {filepath}")
        inner = resolved / 'Info.fcpxml'
        if not inner.is_file():
            raise ValueError(f"Invalid bundle (no Info.fcpxml): {filepath}")
        size_target = inner
    elif not resolved.is_file():
        raise ValueError(f"Not a regular file: {filepath}")
    else:
        size_target = resolved

    if allowed_extensions and resolved.suffix.lower() not in allowed_extensions:
        raise ValueError(
            f"Invalid file type '{resolved.suffix}'. "
            f"Allowed: {', '.join(allowed_extensions)}"
        )

    if size_target.stat().st_size > MAX_FILE_SIZE:
        size_mb = size_target.stat().st_size / (1024 * 1024)
        raise ValueError(f"File too large ({size_mb:.1f} MB). Maximum: {MAX_FILE_SIZE // (1024 * 1024)} MB")

    return str(resolved)


def _validate_output_path(output_path: str, *, anchor_dir: str | None = None) -> str:
    """Validate an output path with optional sandbox enforcement.

    Resolves traversal, blocks null bytes, ensures parent exists, and — when
    *anchor_dir* is provided — verifies the resolved output lives under that
    directory.  This prevents LLM-generated tool calls from writing to
    arbitrary filesystem locations (e.g. ``/etc/cron.d/backdoor``).

    Args:
        output_path: The raw output path to validate.
        anchor_dir: If set, the resolved output must be a child of this
            directory.  Typically the parent directory of the input file so
            outputs stay co-located with their sources.

    Raises:
        ValueError: For null bytes, missing parent, or sandbox escape.
    """
    if '\x00' in output_path:
        raise ValueError("Invalid output path: null byte detected")

    resolved = Path(output_path).resolve()

    if not resolved.parent.exists():
        raise ValueError(f"Output directory does not exist: {resolved.parent}")

    if anchor_dir is not None:
        anchor = Path(anchor_dir).resolve()
        try:
            resolved.relative_to(anchor)
        except ValueError:
            raise ValueError(
                f"Output path escapes allowed directory: "
                f"{resolved} is not under {anchor}"
            )

    # The journal seam: every write passes through here, so noting the
    # approved path is enough for the ledger to record it once it exists.
    _journal.note_output(str(resolved))
    return str(resolved)


def _validate_directory(
    directory: str,
    *,
    allowed_root: str | None = None,
    allowed_roots: Sequence[str] | None = None,
) -> str:
    """Validate a user-provided directory path against traversal and injection.

    Resolves symlinks, blocks null bytes, and verifies the path is a real
    directory. When *allowed_root* (single) or *allowed_roots* (several) is
    given, the resolved path must be a descendant of (or equal to) one of them
    — preventing filesystem enumeration beyond the project workspace.

    Raises:
        ValueError: For invalid paths (null bytes, not a directory, sandbox escape).
    """
    if '\x00' in directory:
        raise ValueError("Invalid directory path: null byte detected")

    resolved = Path(directory).resolve()

    if not resolved.is_dir():
        raise ValueError(f"Not a valid directory: {directory}")

    roots: list[str] = []
    if allowed_root is not None:
        roots.append(allowed_root)
    if allowed_roots:
        roots.extend(allowed_roots)

    if roots and not _within_any_root(directory, roots):
        raise ValueError(
            f"Directory escapes allowed root: "
            f"{resolved} is not under {os.pathsep.join(str(Path(r).resolve()) for r in roots)}"
        )

    return str(resolved)


# ============================================================================
# UTILITIES
# ============================================================================

def find_fcpxml_files_capped(
    directory: str, cap: int | None = None
) -> tuple[list[str], bool]:
    """Find FCPXML files under *directory*, stopping the walk at *cap* files.

    ``rglob`` is a generator, so the cap is enforced by breaking out of the
    iteration — the walk itself stops rather than collecting everything and
    slicing afterwards.  That is the difference between bounded work and
    walking the whole filesystem when a caller names ``/``.

    Returns:
        ``(sorted_files, truncated)``.  *truncated* is True when the cap cut
        the walk short, so callers can say so instead of presenting a partial
        list as complete.
    """
    limit = MAX_DISCOVERY_FILES if cap is None else cap
    path = Path(directory)
    files: list[str] = []
    truncated = False
    for pattern in ("*.fcpxml", "*.fcpxmld"):
        for f in path.rglob(pattern):
            if len(files) >= limit:
                truncated = True
                break
            files.append(str(f))
        if truncated:
            break
    return sorted(files), truncated


def find_fcpxml_files(directory: str, cap: int | None = None) -> list[str]:
    """Find FCPXML files in a directory (capped — see find_fcpxml_files_capped)."""
    files, _ = find_fcpxml_files_capped(directory, cap)
    return files


def format_timecode(tc) -> str:
    """Format a Timecode object to SMPTE string."""
    return tc.to_smpte() if tc else "00:00:00:00"


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.2f}s"
    return f"{int(seconds // 60)}m {seconds % 60:.1f}s"


def _format_clip_table(clips: list, header: str) -> str:
    """Render a list of clips as a markdown table with timecodes and durations.

    Shared by handlers that filter clips by duration threshold
    (find_short_cuts, find_long_clips).
    """
    result = f"{header}\n\n| Name | TC | Duration |\n|------|----|---------|\n"
    result += "\n".join(
        f"| {c.name} | {format_timecode(c.start)} | {format_duration(c.duration_seconds)} |"
        for c in clips
    )
    return result


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a markdown table from headers and rows.

    Returns header row, separator row, and data rows as a single string.
    Callers avoid repeating the ``| H1 | H2 |\\n|---|---|`` boilerplate
    that appears in 15+ handlers.
    """
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "|" + "|".join("------" for _ in headers) + "|"
    data_lines = "\n".join(
        "| " + " | ".join(str(c) for c in row) + " |" for row in rows
    )
    return f"{header_line}\n{sep_line}\n{data_lines}"


def _format_batch_result(
    title: str,
    summary: dict[str, str],
    headers: list[str],
    rows: list[list[str]],
    output_path: str,
) -> str:
    """Build a standard batch-operation result with summary, table, and save footer.

    Used by batch fix handlers (flash frames, rapid trim, fill gaps) that all
    share the same markdown structure: ``# Title → ## Summary → ## Details table
    → Saved to`` footer.
    """
    summary_lines = "\n".join(f"- **{k}**: {v}" for k, v in summary.items())
    table = _markdown_table(headers, rows)
    return (
        f"# {title}\n\n"
        f"## Summary\n{summary_lines}\n\n"
        f"## Details\n{table}\n\n"
        f"Saved to: `{output_path}`"
    )


def _fmt_suggestions(suggestions: list[str]) -> str:
    """Format pacing suggestions as markdown list (Python 3.10 compatible)."""
    if not suggestions:
        return "- Pacing looks good!"
    nl = "\n"
    return nl.join(f"- {s}" for s in suggestions)


def generate_output_path(input_path: str, suffix: str = "_modified") -> str:
    """Generate output path from input path.

    The suffix is sanitized to prevent path-component injection — only
    alphanumeric, hyphen, underscore, and dot characters survive.
    """
    # Strip anything that could inject path separators or traversal sequences
    clean_suffix = re.sub(r'[^a-zA-Z0-9._-]', '', suffix)
    if not clean_suffix:
        clean_suffix = "_modified"
    p = Path(input_path)
    return str(p.parent / f"{p.stem}{clean_suffix}{p.suffix}")


def _parse_project(filepath: str):
    """Parse an FCPXML file and return the project with its primary timeline."""
    filepath = _validate_filepath(filepath, ('.fcpxml', '.fcpxmld'))
    project = FCPXMLParser().parse_file(filepath)
    if not project.timelines:
        return None, None
    return project, project.primary_timeline


_CONFIRM_UNREVIEWED_SCHEMA = {
    "type": "boolean",
    "description": (
        "Ship without a rendered preview of the file's current state. Off by "
        "default: the gate refuses and names the preview_render call that "
        "would satisfy it."
    ),
}


def _text_result(text: str) -> list[TextContent]:
    """Wrap a string in the MCP TextContent list that every tool handler returns."""
    return [TextContent(type="text", text=text)]


def _no_timeline():
    """Standard response when no timelines are found."""
    return _text_result("No timelines found")


def _require_timeline(filepath: str):
    """Parse FCPXML and return (project, timeline), raising if no timeline exists.

    Centralises the repeated _parse_project + _no_timeline guard that
    appears in every read-only timeline handler.  Returns a tuple so
    callers can destructure directly::

        project, tl = _require_timeline(arguments["filepath"])
    """
    project, tl = _parse_project(filepath)
    if not tl:
        raise _NoTimelineError()
    return project, tl


class _NoTimelineError(Exception):
    """Sentinel raised by _require_timeline when no timelines exist."""


def _resolve_io_paths(
    arguments: dict,
    suffix: str = "_modified",
) -> tuple[str, str]:
    """Validate input filepath and resolve the output path.

    Shared foundation for every handler that reads an FCPXML and writes
    a derived file.  Validates the input, falls back to a suffixed
    output name when ``output_path`` is not supplied, and sandbox-checks
    the result.

    Args:
        arguments: Tool arguments dict (must contain ``filepath``; may
            contain ``output_path``).
        suffix: Default output filename suffix when ``output_path`` is
            not provided (e.g. ``"_modified"``, ``"_beats"``).

    Returns:
        ``(filepath, output_path)`` tuple with both paths validated.
    """
    filepath = _validate_filepath(arguments["filepath"], ('.fcpxml', '.fcpxmld'))
    # Anchor write operations to the input file's directory so LLM-generated
    # tool calls cannot write to arbitrary filesystem locations (e.g.
    # /etc/cron.d/backdoor).  When the explicit sandbox is off, the anchor
    # still prevents writes outside the source directory tree.
    anchor = str(Path(filepath).resolve().parent)
    output_path = _validate_output_path(
        arguments.get("output_path") or generate_output_path(filepath, suffix),
        anchor_dir=anchor,
    )
    return filepath, output_path


def _setup_modifier(
    arguments: dict,
    suffix: str = "_modified",
) -> tuple[str, str, "FCPXMLModifier"]:
    """Common setup for write handlers: validate paths and create modifier.

    Consolidates the repeated validate-filepath → resolve-output-path →
    create-modifier boilerplate shared by 18+ write handlers.

    Args:
        arguments: Tool arguments dict (must contain ``filepath``; may
            contain ``output_path``).
        suffix: Default output filename suffix when ``output_path`` is
            not provided (e.g. ``"_modified"``, ``"_flash_fixed"``).

    Returns:
        ``(filepath, output_path, modifier)`` tuple ready for the
        handler's domain-specific operation.
    """
    filepath, output_path = _resolve_io_paths(arguments, suffix)
    modifier = FCPXMLModifier(filepath)
    return filepath, output_path, modifier


def _setup_generator(
    arguments: dict,
    suffix: str = "_roughcut",
) -> tuple[str, str, "RoughCutGenerator"]:
    """Common setup for generation handlers: validate paths and create generator.

    Args:
        arguments: Tool arguments dict (must contain ``filepath`` and
            ``output_path``).
        suffix: Default output filename suffix.

    Returns:
        ``(filepath, output_path, generator)`` tuple.
    """
    filepath, output_path = _resolve_io_paths(arguments, suffix)
    generator = RoughCutGenerator(filepath)
    return filepath, output_path, generator


def _parse_timestamp_parts(
    parts: list[str], *, frame_rate: float = 24.0
) -> float | None:
    """Convert colon-separated timestamp parts to total seconds.

    Handles 2-part (M:SS), 3-part (H:MM:SS / HH:MM:SS.ms), and
    4-part (HH:MM:SS:FF SMPTE) formats.  Returns ``None`` when the
    part count is unrecognised so callers can skip.

    Args:
        parts: Colon-split timestamp components.
        frame_rate: FPS used to convert the frame component of SMPTE
            timecodes into fractional seconds (default 24.0).
    """
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 4:
        # SMPTE: HH:MM:SS:FF — convert frames to fractional seconds
        base = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        frames = int(parts[3])
        return base + (frames / frame_rate) if frame_rate > 0 else base
    return None


def _cap_markers(markers: list) -> tuple[list, int]:
    """Trim a marker batch to ``MAX_BATCH_MARKERS``.

    Returns ``(kept, dropped)``.  Callers must surface *dropped* in their
    result text — a partial write reported as a complete one is the failure
    mode this cap exists to prevent.
    """
    if len(markers) <= MAX_BATCH_MARKERS:
        return markers, 0
    return markers[:MAX_BATCH_MARKERS], len(markers) - MAX_BATCH_MARKERS


def _cap_transcript_text(text: str) -> tuple[str, int]:
    """Trim transcript text to ``MAX_INLINE_TRANSCRIPT_CHARS``.

    Cuts on the last newline inside the cap so a timestamp line is never split
    in half and silently reinterpreted.  Returns ``(kept, chars_dropped)``.
    """
    if len(text) <= MAX_INLINE_TRANSCRIPT_CHARS:
        return text, 0
    head = text[:MAX_INLINE_TRANSCRIPT_CHARS]
    boundary = head.rfind("\n")
    if boundary > 0:
        head = head[:boundary]
    return head, len(text) - len(head)


def _transcript_cap_notice(dropped: int) -> str:
    """Loud, single-line notice for transcript text dropped by the length cap."""
    if not dropped:
        return ""
    return (
        f"\n\n⚠️ TRUNCATED: {dropped} character(s) of transcript text were NOT "
        f"read — input exceeded the {MAX_INLINE_TRANSCRIPT_CHARS}-character cap. "
        f"Raise FCP_MAX_TRANSCRIPT_CHARS or split the transcript."
    )


def _marker_cap_notice(dropped: int) -> str:
    """Loud, single-line notice for markers dropped by the batch cap."""
    if not dropped:
        return ""
    return (
        f"\n\n⚠️ TRUNCATED: {dropped} marker(s) were NOT written — the batch hit "
        f"the {MAX_BATCH_MARKERS}-marker cap. Raise FCP_MAX_BATCH_MARKERS or "
        f"split the import."
    )


def _raw_markers_to_batch(
    raw_markers: list[dict],
    marker_type: str = "chapter",
    max_label: int | None = None,
) -> list[dict]:
    """Convert raw {seconds, text} marker dicts to batch_add_markers format.

    Shared by import_srt_markers and import_transcript_markers.
    """
    batch = []
    for m in raw_markers:
        label = m["text"]
        if max_label and len(label) > max_label:
            label = label[:max_label]
        batch.append({
            "timecode": f"{m['seconds']}s",
            "name": label,
            "marker_type": marker_type.upper(),
        })
    return batch


def _extract_subtitle_blocks(text: str, *, strip_vtt_tags: bool = False) -> list[dict]:
    """Extract timestamp/text pairs from subtitle cue blocks (SRT or VTT).

    Both SRT and VTT use the same ``start --> end`` cue syntax with
    text lines underneath; only header stripping and tag cleaning differ.
    """
    markers = []
    blocks = re.split(r'\n\s*\n', text.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        ts_line = None
        text_lines = []
        for line in lines:
            if '-->' in line:
                ts_line = line
            elif ts_line is not None:
                if strip_vtt_tags:
                    line = re.sub(r'<[^>]+>', '', line)
                cleaned = line.strip()
                if cleaned:
                    text_lines.append(cleaned)
        if not ts_line or not text_lines:
            continue
        start_str = ts_line.split('-->')[0].strip().replace(',', '.')
        seconds = _parse_timestamp_parts(start_str.split(':'))
        if seconds is not None:
            markers.append({'seconds': seconds, 'text': ' '.join(text_lines)})
    return markers


def parse_srt(text: str) -> list[dict]:
    """Parse SRT subtitle format into timestamp/text pairs."""
    return _extract_subtitle_blocks(text)


def parse_vtt(text: str) -> list[dict]:
    """Parse WebVTT subtitle format into timestamp/text pairs."""
    text = re.sub(r'^WEBVTT.*?\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'NOTE\n.*?\n\n', '', text, flags=re.DOTALL)
    return _extract_subtitle_blocks(text, strip_vtt_tags=True)


def parse_transcript_timestamps(text: str) -> list[dict]:
    """Parse timestamped text (YouTube description format) into markers.

    Supports formats like:
      0:00 Introduction
      00:01:30 Main Topic
      1:05:30 Conclusion
      00:00:00:00 SMPTE timecode
    """
    markers = []
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        match = re.match(r'^(\d{1,2}:\d{2}(?::\d{2}){0,2})\s+(.+)$', line)
        if match:
            seconds = _parse_timestamp_parts(match.group(1).split(':'))
            if seconds is not None:
                markers.append({'seconds': seconds, 'text': match.group(2).strip()})
    return markers


# ============================================================================
# MCP RESOURCES — File discovery
# ============================================================================

async def list_resources() -> list[Resource]:
    """Expose discovered FCPXML files as MCP resources."""
    files = find_fcpxml_files(PROJECTS_DIR)
    resources = []
    for f in files:
        p = Path(f)
        encoded = quote(f)
        resources.append(Resource(
            uri=f"file://{encoded}",
            name=p.stem,
            description=f"FCPXML project: {p.name} ({format_duration(0)})",
            mimeType="application/xml",
        ))
        resources.append(Resource(
            uri=f"preview://{encoded}",
            name=f"{p.stem} (visual preview)",
            description=f"HTML timeline preview: {p.name}",
            mimeType="text/html",
        ))
    return resources


def _uri_to_path(uri: str, scheme: str) -> str:
    """Turn an MCP resource URI into a filesystem path.

    Two things go wrong if this is done with `str(uri).replace(scheme, "")`:

    1. `replace` is global, so a path that happens to contain the literal
       scheme string (``preview://a/preview:///b.fcpxml``) gets mangled
       mid-path. Only the leading scheme should be removed.
    2. `list_resources` emits pydantic-normalized URIs, so ``My Project.fcpxml``
       comes back as ``My%20Project.fcpxml``. Without unquoting, every file
       whose name contains a space — the norm in ``~/Movies`` — fails with
       "File not found".

    The returned path is still untrusted and must go through
    `_validate_filepath` before use.
    """
    return unquote(uri.removeprefix(scheme))


async def read_resource(uri: str) -> str | list[ReadResourceContents]:
    """Read an FCPXML file and return a summary."""
    raw = str(uri)
    if raw.startswith("preview://"):
        try:
            filepath = _validate_filepath(
                _uri_to_path(raw, "preview://"), ('.fcpxml', '.fcpxmld')
            )
        except (ValueError, FileNotFoundError) as e:
            return str(e)
        _project, tl = _parse_project(filepath)
        if not tl:
            return f"No timelines found in {filepath}"
        return [ReadResourceContents(content=render_timeline_html(tl), mime_type="text/html")]

    filepath = _uri_to_path(raw, "file://")
    try:
        filepath = _validate_filepath(filepath, ('.fcpxml', '.fcpxmld'))
    except (ValueError, FileNotFoundError) as e:
        return str(e)

    project, tl = _parse_project(filepath)
    if not tl:
        return f"No timelines found in {filepath}"

    return f"""FCPXML Project: {tl.name}
Duration: {format_duration(tl.duration.seconds)}
Resolution: {tl.width}x{tl.height} @ {fcp_frame_rate_name(tl.frame_rate)}fps
Clips: {tl.total_clips}
Markers: {len(tl.markers)}
Cuts/min: {tl.cuts_per_minute:.1f}
Path: {filepath}"""


# ============================================================================
# MCP PROMPTS — Pre-built workflows
# ============================================================================

async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="qc-check",
            description="Run a full quality control check on your timeline — flash frames, gaps, duplicates, and health score",
            arguments=[
                PromptArgument(name="filepath", description="Path to FCPXML file", required=True),
            ],
        ),
        Prompt(
            name="youtube-chapters",
            description="Extract chapter markers formatted for YouTube descriptions",
            arguments=[
                PromptArgument(name="filepath", description="Path to FCPXML file", required=True),
            ],
        ),
        Prompt(
            name="rough-cut",
            description="Guided rough cut generation — choose keywords, pacing, and duration",
            arguments=[
                PromptArgument(name="filepath", description="Path to source FCPXML with clips", required=True),
                PromptArgument(name="duration", description="Target duration (e.g., '3m', '90s')", required=True),
            ],
        ),
        Prompt(
            name="timeline-summary",
            description="Quick overview of a timeline — stats, pacing, and potential issues",
            arguments=[
                PromptArgument(name="filepath", description="Path to FCPXML file", required=True),
            ],
        ),
        Prompt(
            name="cleanup",
            description="Find and fix common timeline issues — flash frames, gaps, and duplicates",
            arguments=[
                PromptArgument(name="filepath", description="Path to FCPXML file", required=True),
            ],
        ),
    ]


# Prompts must name tools the model can actually SEE. Since v0.14.0 list_tools
# advertises 7 grouped verbs, not the 62 flat names, so every step below is
# written as "`<group>` with action `<action>`". The flat names still dispatch,
# but instructing the model to call one it was never shown teaches it to guess.
# tests/test_tool_groups.py::TestPromptsUseGroupedToolNames enforces this shape.
_PROMPT_CALLING_CONVENTION = """
Every tool call takes the grouped form:
  tool = the group name, arguments = {"action": "<action>", "args": {...}}
For example: `inspect` with action `analyze_timeline` and args {"filepath": "..."}.
"""


async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> GetPromptResult:
    args = arguments or {}
    filepath = args.get("filepath", "<path to your .fcpxml file>")

    if name == "qc-check":
        return GetPromptResult(
            description="Full QC check on timeline",
            messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Run a complete quality control check on my timeline.

File: {filepath}
{_PROMPT_CALLING_CONVENTION}
Please:
1. Call `diagnose` with action `validate_timeline` to get the health score
2. Call `diagnose` with action `detect_flash_frames` to find any ultra-short clips
3. Call `diagnose` with action `detect_gaps` to find unintentional gaps
4. Call `diagnose` with action `detect_duplicates` to find repeated source clips
5. Summarize all issues and recommend fixes

If there are critical issues, offer to fix them automatically: `edit` with action
`fix_flash_frames`, then `edit` with action `fill_gaps`."""
                ),
            )],
        )

    elif name == "youtube-chapters":
        return GetPromptResult(
            description="Export YouTube chapter markers",
            messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Extract chapter markers from my timeline and format them for YouTube.

File: {filepath}
{_PROMPT_CALLING_CONVENTION}
Please:
1. Call `inspect` with action `list_markers`, passing format "youtube" in args, to get chapter timestamps
2. Format the output so I can copy-paste directly into a YouTube description
3. If there are no chapter markers, suggest good chapter points based on the timeline structure using `inspect` with action `analyze_pacing`"""
                ),
            )],
        )

    elif name == "rough-cut":
        duration = args.get("duration", "3m")
        return GetPromptResult(
            description="Guided rough cut generation",
            messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Help me create a rough cut from my source clips.

File: {filepath}
Target duration: {duration}
{_PROMPT_CALLING_CONVENTION}
Please:
1. Call `inspect` with action `list_library_clips` to show me what clips are available
2. Call `inspect` with action `list_keywords` to show me the tags I can filter by
3. Suggest a structure (segments, pacing) based on what's available
4. Generate the rough cut with `generate` with action `auto_rough_cut` using my preferences
5. Show me a summary of what was created"""
                ),
            )],
        )

    elif name == "timeline-summary":
        return GetPromptResult(
            description="Quick timeline overview",
            messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Give me a quick overview of my timeline.

File: {filepath}
{_PROMPT_CALLING_CONVENTION}
Please:
1. Call `inspect` with action `analyze_timeline` for stats (duration, resolution, clip count)
2. Call `inspect` with action `analyze_pacing` for pacing metrics and suggestions
3. Call `inspect` with action `list_keywords` to show what tags are in use
4. Call `inspect` with action `list_markers` to show any markers
5. Give me a brief assessment of the edit"""
                ),
            )],
        )

    elif name == "cleanup":
        return GetPromptResult(
            description="Find and fix timeline issues",
            messages=[PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"""Help me clean up my timeline by finding and fixing common issues.

File: {filepath}
{_PROMPT_CALLING_CONVENTION}
Please:
1. Call `diagnose` with action `validate_timeline` to get the health score
2. If there are flash frames, call `edit` with action `fix_flash_frames` to remove them
3. If there are gaps, call `edit` with action `fill_gaps` to close them
4. Report what was fixed and the new health score"""
                ),
            )],
        )

    raise ValueError(f"Unknown prompt: {name}")


# ============================================================================
# TOOL DEFINITIONS
# ============================================================================

def _legacy_tool_list() -> list[Tool]:
    """The flat tool schemas (the original 62 plus transcript_pack). Still
    dispatchable; advertised only on opt-in. Operations born as group actions
    (preview, watch, index, scenes) have no flat schema."""
    return [
        # ===== READ TOOLS =====
        Tool(
            name="list_projects",
            description="List all FCPXML projects in directory",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory to search (default: ~/Movies)"}
                }
            }
        ),
        Tool(
            name="analyze_timeline",
            description="Get comprehensive timeline statistics including duration, resolution, clip count, pacing metrics",
            inputSchema={
                "type": "object",
                "properties": {"filepath": {"type": "string", "description": "Path to FCPXML file"}},
                "required": ["filepath"]
            }
        ),
        Tool(
            name="list_clips",
            description="List all clips with timecodes, durations, and metadata",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max clips to return"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="list_markers",
            description="Extract markers (chapter, todo, standard) with timestamps",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "marker_type": {"type": "string", "enum": ["all", "chapter", "todo", "standard", "completed"]},
                    "format": {"type": "string", "enum": ["detailed", "youtube", "simple"]}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="find_short_cuts",
            description="Find clips shorter than threshold (flash frame detection)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "threshold_seconds": {"type": "number", "default": 0.5}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="find_long_clips",
            description="Find clips longer than threshold",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "threshold_seconds": {"type": "number", "default": 10.0}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="list_keywords",
            description="Extract all keywords/tags from project",
            inputSchema={
                "type": "object",
                "properties": {"filepath": {"type": "string"}},
                "required": ["filepath"]
            }
        ),
        Tool(
            name="export_edl",
            description="Generate EDL (Edit Decision List) from timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm_unreviewed": _CONFIRM_UNREVIEWED_SCHEMA,"filepath": {"type": "string"}},
                "required": ["filepath"]
            }
        ),
        Tool(
            name="export_csv",
            description="Export timeline data to CSV format",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm_unreviewed": _CONFIRM_UNREVIEWED_SCHEMA,
                    "filepath": {"type": "string"},
                    "include": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="analyze_pacing",
            description="Analyze edit pacing with suggestions for improvements",
            inputSchema={
                "type": "object",
                "properties": {"filepath": {"type": "string"}},
                "required": ["filepath"]
            }
        ),
        Tool(
            name="list_library_clips",
            description="List all available clips in the library (source media, not yet on timeline)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Filter by keywords"},
                    "limit": {"type": "integer", "description": "Max clips to return"}
                },
                "required": ["filepath"]
            }
        ),

        # ===== QC / VALIDATION TOOLS =====
        Tool(
            name="detect_flash_frames",
            description="Find ultra-short clips (flash frames) that are likely errors, with severity categorization",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "critical_threshold_frames": {"type": "integer", "default": 2, "description": "Frames below this = critical (default: 2)"},
                    "warning_threshold_frames": {"type": "integer", "default": 6, "description": "Frames below this = warning (default: 6)"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="detect_duplicates",
            description="Find clips using the same source media (potential duplicates)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "mode": {"type": "string", "enum": ["same_source", "overlapping_ranges", "identical"], "default": "same_source", "description": "Detection mode"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="detect_gaps",
            description="Find unintentional gaps in the timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "min_gap_frames": {"type": "integer", "default": 1, "description": "Minimum gap size to detect (default: 1 frame)"}
                },
                "required": ["filepath"]
            }
        ),

        # ===== WRITE TOOLS =====
        Tool(
            name="add_marker",
            description="Add a marker at a specific timecode",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "timecode": {"type": "string", "description": "Position (00:00:10:00 or 10s)"},
                    "name": {"type": "string", "description": "Marker label"},
                    "marker_type": {"type": "string", "enum": ["standard", "chapter", "todo", "completed"], "default": "standard"},
                    "note": {"type": "string", "description": "Optional note"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath", "timecode", "name"]
            }
        ),
        Tool(
            name="batch_add_markers",
            description="Add multiple markers at once, or auto-generate at cuts/intervals",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "markers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "timecode": {"type": "string"},
                                "name": {"type": "string"},
                                "marker_type": {"type": "string"},
                                "note": {"type": "string"}
                            }
                        },
                        "description": "List of markers to add"
                    },
                    "auto_at_cuts": {"type": "boolean", "description": "Add marker at every cut"},
                    "auto_at_intervals": {"type": "string", "description": "Add markers every N seconds (e.g., '30s')"},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="trim_clip",
            description="Trim a clip's in-point and/or out-point",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "clip_id": {"type": "string", "description": "Clip name or ID"},
                    "trim_start": {"type": "string", "description": "New in-point or delta (+1s, -10f)"},
                    "trim_end": {"type": "string", "description": "New out-point or delta"},
                    "ripple": {"type": "boolean", "default": True, "description": "Shift subsequent clips"},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath", "clip_id"]
            }
        ),
        Tool(
            name="reorder_clips",
            description="Move clips to a new position in the timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "clip_ids": {"type": "array", "items": {"type": "string"}, "description": "Clips to move"},
                    "target_position": {"type": "string", "description": "'start', 'end', timecode, or 'after:clip_id'"},
                    "ripple": {"type": "boolean", "default": True},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath", "clip_ids", "target_position"]
            }
        ),
        Tool(
            name="add_transition",
            description="Add a transition between clips",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "clip_id": {"type": "string", "description": "Clip to add transition to"},
                    "position": {"type": "string", "enum": ["start", "end", "both"], "default": "end"},
                    "transition_type": {"type": "string", "enum": ["cross-dissolve", "fade-to-black", "fade-from-black", "wipe"], "default": "cross-dissolve"},
                    "duration": {"type": "string", "default": "00:00:00:15"},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath", "clip_id"]
            }
        ),
        Tool(
            name="change_speed",
            description="Change clip playback speed (slow motion or speed up)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "clip_id": {"type": "string"},
                    "speed": {"type": "number", "description": "Speed multiplier (0.5 = half, 2.0 = double)"},
                    "preserve_pitch": {"type": "boolean", "default": True},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath", "clip_id", "speed"]
            }
        ),
        Tool(
            name="delete_clips",
            description="Delete clips from timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "clip_ids": {"type": "array", "items": {"type": "string"}},
                    "ripple": {"type": "boolean", "default": True, "description": "Close gaps after deletion"},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath", "clip_ids"]
            }
        ),
        Tool(
            name="split_clip",
            description="Split a clip at specified timecodes",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "clip_id": {"type": "string"},
                    "split_points": {"type": "array", "items": {"type": "string"}, "description": "Timecodes to split at"},
                    "output_path": {"type": "string"}
                },
                "required": ["filepath", "clip_id", "split_points"]
            }
        ),
        Tool(
            name="insert_clip",
            description="Insert a library clip onto the timeline at a specific position",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "asset_id": {"type": "string", "description": "Asset reference ID (e.g., 'r3')"},
                    "asset_name": {"type": "string", "description": "Asset name (alternative to asset_id)"},
                    "position": {"type": "string", "description": "'start', 'end', timecode, or 'after:clip_name'"},
                    "duration": {"type": "string", "description": "Clip duration (if not using in/out points)"},
                    "in_point": {"type": "string", "description": "Source in-point for subclip"},
                    "out_point": {"type": "string", "description": "Source out-point for subclip"},
                    "ripple": {"type": "boolean", "default": True, "description": "Shift subsequent clips"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath", "position"]
            }
        ),

        # ===== BATCH FIX TOOLS =====
        Tool(
            name="fix_flash_frames",
            description="Automatically fix detected flash frames by extending neighbors or deleting",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "mode": {"type": "string", "enum": ["extend_previous", "extend_next", "delete", "auto"], "default": "auto", "description": "How to fix: extend previous/next clip, delete, or auto"},
                    "threshold_frames": {"type": "integer", "default": 6, "description": "Frames below this threshold are flash frames"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="rapid_trim",
            description="Batch trim clips to a maximum duration for fast-paced montages",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "max_duration": {"type": "string", "description": "Maximum clip duration (e.g., '2s', '00:00:02:00')"},
                    "min_duration": {"type": "string", "description": "Minimum clip duration (optional)"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Only trim clips with these keywords"},
                    "trim_from": {"type": "string", "enum": ["start", "end", "center"], "default": "end", "description": "Where to trim from"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath", "max_duration"]
            }
        ),
        Tool(
            name="fill_gaps",
            description="Automatically fill gaps in the timeline by extending adjacent clips",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "mode": {"type": "string", "enum": ["extend_previous", "extend_next", "delete"], "default": "extend_previous", "description": "How to fill gaps"},
                    "max_gap": {"type": "string", "description": "Only fill gaps smaller than this (e.g., '1s')"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="validate_timeline",
            description="Comprehensive timeline health check for flash frames, gaps, duplicates, and issues",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "checks": {"type": "array", "items": {"type": "string", "enum": ["all", "flash_frames", "gaps", "duplicates", "offsets"]}, "default": ["all"], "description": "Which checks to run"}
                },
                "required": ["filepath"]
            }
        ),

        # ===== GENERATION TOOLS =====
        Tool(
            name="auto_rough_cut",
            description="Generate a rough cut from source clips based on keywords, duration, and pacing",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Source FCPXML with clips"},
                    "output_path": {"type": "string", "description": "Where to save rough cut"},
                    "target_duration": {"type": "string", "description": "Target length (3m, 00:03:00:00)"},
                    "pacing": {"type": "string", "enum": ["slow", "medium", "fast", "dynamic"], "default": "medium"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Filter clips by keywords"},
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "keywords": {"type": "array", "items": {"type": "string"}},
                                "duration": {"type": "number"}
                            }
                        },
                        "description": "Segment structure [{name, keywords, duration_seconds}]"
                    },
                    "priority": {"type": "string", "enum": ["best", "favorites", "longest", "shortest", "random"], "default": "best"},
                    "favorites_only": {"type": "boolean", "default": False},
                    "add_transitions": {"type": "boolean", "default": False},
                    "min_source_separation": {
                        "type": "integer", "minimum": 0, "maximum": 20, "default": 0,
                        "description": "Minimum number of other shots between two uses of the same source; 0 = off"
                    }
                },
                "required": ["filepath", "output_path", "target_duration"]
            }
        ),
        Tool(
            name="generate_montage",
            description="Create rapid-fire montages with pacing curves (accelerating, decelerating, pyramid)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Source FCPXML with clips"},
                    "output_path": {"type": "string", "description": "Where to save montage"},
                    "target_duration": {"type": "string", "description": "Total montage length (e.g., '30s', '00:00:30:00')"},
                    "pacing_curve": {"type": "string", "enum": ["accelerating", "decelerating", "pyramid", "constant"], "default": "accelerating", "description": "How clip duration changes over time"},
                    "start_duration": {"type": "number", "default": 2.0, "description": "Clip duration at start (seconds)"},
                    "end_duration": {"type": "number", "default": 0.5, "description": "Clip duration at end (seconds)"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Filter clips by keywords"},
                    "add_transitions": {"type": "boolean", "default": False, "description": "Add quick dissolves"}
                },
                "required": ["filepath", "output_path", "target_duration"]
            }
        ),
        Tool(
            name="generate_ab_roll",
            description="Create documentary-style A/B roll edits alternating between main content and cutaways",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Source FCPXML with clips"},
                    "output_path": {"type": "string", "description": "Where to save A/B roll edit"},
                    "target_duration": {"type": "string", "description": "Total duration (e.g., '3m', '00:03:00:00')"},
                    "a_keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords for A-roll (main content, interviews)"},
                    "b_keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords for B-roll (cutaways, visuals)"},
                    "a_duration": {"type": "string", "default": "5s", "description": "Duration of each A-roll segment"},
                    "b_duration": {"type": "string", "default": "3s", "description": "Duration of each B-roll cutaway"},
                    "start_with": {"type": "string", "enum": ["a", "b"], "default": "a", "description": "Which roll to start with"},
                    "add_transitions": {"type": "boolean", "default": True, "description": "Add cross-dissolves"}
                },
                "required": ["filepath", "output_path", "target_duration", "a_keywords", "b_keywords"]
            }
        ),

        # ===== BEAT SYNC TOOLS =====
        Tool(
            name="import_beat_markers",
            description="Import beat markers from external audio analysis (JSON format)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "beats_path": {"type": "string", "description": "Path to beats JSON file"},
                    "marker_type": {"type": "string", "enum": ["standard", "chapter"], "default": "standard"},
                    "beat_filter": {"type": "string", "enum": ["all", "downbeat", "measure"], "default": "all", "description": "Which beats to import"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _beats suffix)"}
                },
                "required": ["filepath", "beats_path"]
            }
        ),
        Tool(
            name="snap_to_beats",
            description="Align cuts to nearest beat markers for music-synced edits. Works on spine cuts and on connected clips lane by lane (a music video keeps its whole edit on lanes). Moving a connected clip does not ripple the clips after it; a move that would collide with a neighbour in the same lane is skipped and reported.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file with beat markers"},
                    "max_shift_frames": {"type": "integer", "default": 6, "description": "Maximum frames to shift a cut"},
                    "prefer": {"type": "string", "enum": ["earlier", "later", "nearest"], "default": "nearest", "description": "Which beat to prefer when equidistant"},
                    "include_audio_lanes": {"type": "boolean", "default": False, "description": "Also snap clips on negative (audio) lanes. Off by default: on a music video that is the track the beats came from, and moving it desyncs the whole edit."},
                    "output_path": {"type": "string", "description": "Output path (default: adds _synced suffix)"}
                },
                "required": ["filepath"]
            }
        ),

        # ===== SUBTITLE / TRANSCRIPT TOOLS =====
        Tool(
            name="import_srt_markers",
            description="Import SRT or VTT subtitles as chapter markers on the timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "srt_path": {"type": "string", "description": "Path to SRT or VTT subtitle file"},
                    "mode": {"type": "string", "enum": ["all", "first_per_minute", "scene_changes"], "default": "first_per_minute", "description": "How to create markers: every subtitle, first per minute, or on text changes"},
                    "marker_type": {"type": "string", "enum": ["standard", "chapter"], "default": "chapter"},
                    "max_label_length": {"type": "integer", "default": 50, "description": "Truncate marker labels to this length"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _subtitled suffix)"}
                },
                "required": ["filepath", "srt_path"]
            }
        ),
        Tool(
            name="import_transcript_markers",
            description="Import timestamped transcript (YouTube chapter format) as markers. Supports '0:00 Title' and 'HH:MM:SS Title' formats",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "transcript": {"type": "string", "description": "Timestamped text (one per line: '0:00 Introduction')"},
                    "transcript_path": {"type": "string", "description": "Path to text file with timestamps (alternative to inline transcript)"},
                    "marker_type": {"type": "string", "enum": ["standard", "chapter"], "default": "chapter"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _chapters suffix)"}
                },
                "required": ["filepath"]
            }
        ),

        # ===== CONNECTED CLIPS & COMPOUND CLIPS (v0.5.0) =====
        Tool(
            name="list_connected_clips",
            description="List all connected clips (B-roll, titles, audio) with their lanes and parent clips",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "lane": {"type": "integer", "description": "Filter by lane number (positive=above, negative=below)"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="add_connected_clip",
            description="Connect a library clip to an existing timeline clip (B-roll overlay, audio, title)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "parent_clip_id": {"type": "string", "description": "Name/ID of the clip to attach to"},
                    "asset_id": {"type": "string", "description": "Asset reference ID"},
                    "asset_name": {"type": "string", "description": "Asset name (alternative to asset_id)"},
                    "offset": {"type": "string", "default": "0s", "description": "Position relative to parent clip start"},
                    "duration": {"type": "string", "description": "Duration (default: full asset)"},
                    "lane": {"type": "integer", "default": 1, "description": "Lane number (positive=above, negative=below)"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath", "parent_clip_id"]
            }
        ),
        Tool(
            name="list_compound_clips",
            description="List compound clips (ref-clips) and their nested content",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                },
                "required": ["filepath"]
            }
        ),

        # ===== ROLES MANAGEMENT (v0.5.0) =====
        Tool(
            name="list_roles",
            description="List all audio/video roles used in the timeline with clip counts",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="assign_role",
            description="Set the audio or video role on a clip (dialogue, music, effects, titles, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "clip_id": {"type": "string", "description": "Clip name or ID"},
                    "audio_role": {"type": "string", "description": "Audio role (e.g., dialogue, music, effects)"},
                    "video_role": {"type": "string", "description": "Video role (e.g., video, titles)"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _modified suffix)"}
                },
                "required": ["filepath", "clip_id"]
            }
        ),
        Tool(
            name="filter_by_role",
            description="List all clips matching a specific audio or video role",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "role": {"type": "string", "description": "Role name to filter by"},
                    "role_type": {"type": "string", "enum": ["audio", "video", "any"], "default": "any", "description": "Which role type to search"},
                },
                "required": ["filepath", "role"]
            }
        ),
        Tool(
            name="export_role_stems",
            description="Export clip list grouped by role for audio mixing stem planning",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm_unreviewed": _CONFIRM_UNREVIEWED_SCHEMA,
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                },
                "required": ["filepath"]
            }
        ),

        # ===== TIMELINE DIFF (v0.5.0) =====
        Tool(
            name="diff_timelines",
            description="Compare two FCPXML files and report differences in clips, markers, transitions, and format",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath_a": {"type": "string", "description": "Path to first FCPXML file (baseline)"},
                    "filepath_b": {"type": "string", "description": "Path to second FCPXML file (comparison)"},
                },
                "required": ["filepath_a", "filepath_b"]
            }
        ),

        # ===== SOCIAL MEDIA REFORMAT (v0.5.0) =====
        Tool(
            name="reformat_timeline",
            description="Create new FCPXML with different resolution/aspect ratio (9:16 for TikTok, 1:1 for Instagram, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "format": {"type": "string", "enum": ["9:16", "1:1", "4:5", "16:9", "4:3", "custom"], "description": "Target format preset"},
                    "width": {"type": "integer", "description": "Custom width (only with format='custom')"},
                    "height": {"type": "integer", "description": "Custom height (only with format='custom')"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _reformatted suffix)"}
                },
                "required": ["filepath", "format"]
            }
        ),

        # ===== MEDIA INTELLIGENCE (v0.10.0) =====
        Tool(
            name="detect_media_silence",
            description="Detect REAL silence by analyzing each clip's source audio with ffmpeg silencedetect, mapped into timeline time. Unlike detect_silence_candidates (XML-only heuristics), this reads the actual media files referenced by the timeline. Requires ffmpeg; clips whose media is missing or unreadable are reported, not failed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "noise_db": {"type": "number", "default": -30.0, "description": "Silence threshold in dBFS, -120 to 0 (default -30)"},
                    "min_silence": {"type": "number", "default": 0.5, "description": "Minimum silence duration in seconds to report (default 0.5)"},
                    "clip_name": {"type": "string", "description": "Only analyze the clip with this name"},
                },
                "required": ["filepath"]
            }
        ),

        Tool(
            name="detect_beats",
            description="Detect musical beats and tempo in an audio/video file (librosa beat tracker). Writes a beats JSON next to the media file that plugs directly into import_beat_markers + snap_to_beats for beat-synced editing. Requires the optional [intelligence] extra (librosa); degrades to an install hint without it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "media_path": {"type": "string", "description": "Path to audio/video file (.wav, .mp3, .m4a, .aac, .aif, .flac, .mov, .mp4)"},
                },
                "required": ["media_path"]
            }
        ),
        Tool(
            name="remove_media_silence",
            description="Detect REAL silence in each clip's source audio (ffmpeg) and CUT it out of the timeline with ripple. Clips are split around silence; the silent middles are removed and everything after shifts earlier. Non-destructive: writes a _silence_removed copy. Preview with detect_media_silence first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "noise_db": {"type": "number", "default": -30.0, "description": "Silence threshold in dBFS, -120 to 0 (default -30)"},
                    "min_silence": {"type": "number", "default": 0.5, "description": "Minimum silence duration in seconds to cut (default 0.5)"},
                    "padding": {"type": "number", "default": 0.05, "description": "Seconds of silence to keep on each side of a cut so edits breathe (default 0.05, max 5)"},
                    "clip_name": {"type": "string", "description": "Only cut silence in the clip with this name"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _silence_removed suffix)"},
                },
                "required": ["filepath"]
            }
        ),

        # ===== TRANSCRIPT INTELLIGENCE (v0.13.1) =====
        Tool(
            name="transcribe_media",
            description="Transcribe each clip's source media locally with word-level timestamps (faster-whisper). Writes a _transcript.json next to each media file (reused by edit_by_transcript / remove_filler_words so media is only transcribed once) and optionally an SRT for captions. Requires the optional [transcribe] extra; degrades to an install hint without it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "clip_name": {"type": "string", "description": "Only transcribe the clip with this name"},
                    "model": {"type": "string", "default": "base", "description": "Whisper model size: tiny, base, small, medium, large-v3 (default base; larger = slower + more accurate)"},
                    "language": {"type": "string", "description": "ISO language code hint (e.g. 'en'); auto-detected if omitted"},
                    "backend": {"type": "string", "enum": ["local", "elevenlabs"], "default": "local", "description": "local = faster-whisper on this machine, nothing leaves it. elevenlabs = upload the media to ElevenLabs Scribe for speaker labels and audio events (needs ELEVENLABS_API_KEY; opt-in, audio leaves the machine)"},
                    "write_srt": {"type": "boolean", "default": False, "description": "Also write a _transcript.srt next to each media file (plugs into import_srt_markers)"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="edit_by_transcript",
            description="Text-based editing: cut timeline content by what was SAID. mode=remove cuts every occurrence of the given phrases (with ripple); mode=keep_only keeps only the matched phrases and cuts everything else in each matched clip (clips with no matches are left untouched). Uses each media file's _transcript.json (auto-transcribes if missing). Non-destructive: writes a _transcript_edit copy.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "phrases": {"type": "array", "items": {"type": "string"}, "description": "Spoken phrases to match (case/punctuation-insensitive)"},
                    "mode": {"type": "string", "enum": ["remove", "keep_only"], "default": "remove", "description": "remove=cut matches out; keep_only=keep only matches"},
                    "clip_name": {"type": "string", "description": "Only edit the clip with this name"},
                    "model": {"type": "string", "default": "base", "description": "Whisper model size if transcription is needed"},
                    "backend": {"type": "string", "enum": ["local", "elevenlabs"], "default": "local", "description": "local = faster-whisper on this machine, nothing leaves it. elevenlabs = upload the media to ElevenLabs Scribe for speaker labels and audio events (needs ELEVENLABS_API_KEY; opt-in, audio leaves the machine)"},
                    "padding": {"type": "number", "default": 0.0, "description": "Seconds to widen each cut on both sides (0-2, default 0)"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _transcript_edit suffix)"},
                },
                "required": ["filepath", "phrases"]
            }
        ),
        Tool(
            name="remove_filler_words",
            description="Cut filler words (um, uh, erm...) out of the timeline with ripple, using word-level transcripts of the real source audio. Conservative default filler list — words like 'like' and 'so' are only cut if you pass them explicitly. Uses each media file's _transcript.json (auto-transcribes if missing). Non-destructive: writes a _defillered copy.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "fillers": {"type": "array", "items": {"type": "string"}, "description": "Filler words/phrases to cut (default: um, uh, uhh, umm, erm, ehm, mmm, hmm, mhm)"},
                    "clip_name": {"type": "string", "description": "Only clean the clip with this name"},
                    "model": {"type": "string", "default": "base", "description": "Whisper model size if transcription is needed"},
                    "backend": {"type": "string", "enum": ["local", "elevenlabs"], "default": "local", "description": "local = faster-whisper on this machine, nothing leaves it. elevenlabs = upload the media to ElevenLabs Scribe for speaker labels and audio events (needs ELEVENLABS_API_KEY; opt-in, audio leaves the machine)"},
                    "padding": {"type": "number", "default": 0.02, "description": "Seconds to widen each cut on both sides (0-2, default 0.02)"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _defillered suffix)"},
                },
                "required": ["filepath"]
            }
        ),

        Tool(
            name="transcript_pack",
            description="The whole shoot on one page: every clip's transcript packed into one document — a header per source, one line per utterance, broken on silence or a speaker change, audio events inline. Built for planning an edit from what was said. Uses each media file's _transcript.json / the index (auto-transcribes if missing). Truncated at 60KB; write=true saves the full pack as <project>_pack.md beside the FCPXML.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "clip_name": {"type": "string", "description": "Only pack the clip with this name"},
                    "model": {"type": "string", "default": "base", "description": "Whisper model size if transcription is needed"},
                    "language": {"type": "string", "description": "ISO language code hint if transcription is needed"},
                    "backend": {"type": "string", "enum": ["local", "elevenlabs"], "default": "local", "description": "local = faster-whisper on this machine, nothing leaves it. elevenlabs = upload the media to ElevenLabs Scribe for speaker labels and audio events (needs ELEVENLABS_API_KEY; opt-in, audio leaves the machine)"},
                    "gap": {"type": "number", "default": 0.5, "description": "Seconds of silence that end an utterance (0.1-5, default 0.5)"},
                    "write": {"type": "boolean", "default": False, "description": "Also write the full, untruncated pack to <project>_pack.md next to the FCPXML"},
                },
                "required": ["filepath"]
            }
        ),

        # ===== SILENCE DETECTION (v0.5.0) =====
        Tool(
            name="detect_silence_candidates",
            description="Detect potential silence/dead air using timeline heuristics (gaps, ultra-short clips, name patterns, duration anomalies)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "min_gap_seconds": {"type": "number", "default": 0.5, "description": "Minimum gap duration to flag"},
                    "patterns": {"type": "array", "items": {"type": "string"}, "description": "Name patterns to match (default: gap, silence, room tone)"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="remove_silence_candidates",
            description="Remove or mark detected silence candidates from timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "mode": {"type": "string", "enum": ["delete", "mark"], "default": "mark", "description": "delete=remove clips/gaps, mark=add red markers"},
                    "min_gap_seconds": {"type": "number", "default": 0.5},
                    "min_confidence": {"type": "number", "default": 0.7, "description": "Only act on candidates above this confidence"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _silence_cleaned suffix)"}
                },
                "required": ["filepath"]
            }
        ),

        # ===== NLE EXPORT (v0.5.0) =====
        Tool(
            name="export_resolve_xml",
            description="Export timeline as DaVinci Resolve compatible FCPXML (simplified v1.9)",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm_unreviewed": _CONFIRM_UNREVIEWED_SCHEMA,
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "flatten_compounds": {"type": "boolean", "default": True, "description": "Flatten compound clips for compatibility"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _resolve suffix)"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="export_fcp7_xml",
            description="Export timeline as FCP7 XML (XMEML) for Premiere Pro, DaVinci Resolve, and Avid compatibility",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm_unreviewed": _CONFIRM_UNREVIEWED_SCHEMA,
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "output_path": {"type": "string", "description": "Output path (default: adds _fcp7.xml suffix)"},
                },
                "required": ["filepath"]
            }
        ),

        # ===== v0.6.0 TOOLS =====
        Tool(
            name="list_effects",
            description="List all available FCP transition effects with slugs and UUIDs",
            inputSchema={
                "type": "object",
                "properties": {},
            }
        ),
        Tool(
            name="add_audio",
            description="Add an audio clip or music bed to the timeline",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "parent_clip_id": {"type": "string", "description": "Clip to attach audio to (omit for music bed spanning full timeline)"},
                    "asset_id": {"type": "string", "description": "Existing asset reference ID"},
                    "src": {"type": "string", "description": "Path to audio file (creates new asset)"},
                    "offset": {"type": "string", "description": "Position relative to parent clip start", "default": "0s"},
                    "duration": {"type": "string", "description": "Duration of audio clip"},
                    "role": {"type": "string", "description": "Audio role (dialogue, music, effects, etc.)", "default": "dialogue"},
                    "lane": {"type": "integer", "description": "Lane number (negative = below)", "default": -1},
                    "output_path": {"type": "string", "description": "Output path"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="create_compound_clip",
            description="Group spine clips into a compound clip",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "clip_ids": {"type": "array", "items": {"type": "string"}, "description": "Clip IDs to group"},
                    "name": {"type": "string", "description": "Name for the compound clip", "default": "Compound Clip"},
                    "output_path": {"type": "string", "description": "Output path"},
                },
                "required": ["filepath", "clip_ids"]
            }
        ),
        Tool(
            name="flatten_compound_clip",
            description="Flatten a compound clip back into individual clips in the spine",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file"},
                    "ref_clip_id": {"type": "string", "description": "ID of the ref-clip to flatten"},
                    "output_path": {"type": "string", "description": "Output path"},
                },
                "required": ["filepath", "ref_clip_id"]
            }
        ),
        Tool(
            name="list_templates",
            description="List available timeline templates with slot definitions",
            inputSchema={
                "type": "object",
                "properties": {},
            }
        ),
        Tool(
            name="apply_template",
            description="Fill a timeline template with clips and generate FCPXML",
            inputSchema={
                "type": "object",
                "properties": {
                    "template_name": {"type": "string", "description": "Template name (intro_outro, lower_thirds, music_video)"},
                    "clips": {"type": "object", "description": "Map of slot_name -> {src, name, duration} or {asset_id, name, duration}"},
                    "output_path": {"type": "string", "description": "Output FCPXML path"},
                    "fps": {"type": "number", "description": "Frame rate", "default": 24},
                },
                "required": ["template_name", "clips", "output_path"]
            }
        ),

        # ===== v0.8.0 TOOLS =====
        Tool(
            name="relink_media",
            description="Bulk-rewrite media source paths (asset/media-rep src URLs) to relink moved or renamed media folders without opening FCP. Prefix-based: find='/Volumes/OldDrive/Media' replace='/Volumes/NewDrive/Media'. Use dry_run to preview.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to FCPXML file or .fcpxmld bundle"},
                    "find": {"type": "string", "description": "Old path prefix to match (plain path or file:// URL)"},
                    "replace": {"type": "string", "description": "New path prefix to substitute"},
                    "dry_run": {"type": "boolean", "description": "Preview changes without writing", "default": False},
                    "output_path": {"type": "string", "description": "Output path (default: adds _relinked suffix)"},
                },
                "required": ["filepath", "find", "replace"]
            }
        ),

        # ===== v0.9.0 LIVE MODE (macOS + Final Cut Pro required) =====
        Tool(
            name="push_to_fcp",
            description="LIVE: send an FCPXML file into the running Final Cut Pro with zero clicks (official Open Document Apple event). Creates/targets a library via import-options. Launches FCP if needed. macOS-only; first use triggers an Automation permission prompt. For true zero-click, pass a library_location ending in .fcpbundle (a new path is auto-created); omitting it makes FCP show a modal library picker.",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm_unreviewed": _CONFIRM_UNREVIEWED_SCHEMA,
                    "filepath": {"type": "string", "description": "Path to FCPXML file or .fcpxmld bundle to import"},
                    "library_location": {"type": "string", "description": "Target .fcpbundle library path (auto-created if it doesn't exist; the extension is normalized to .fcpbundle). Omit to import into the active library, but note FCP then shows a modal 'Open Library' picker that blocks until answered"},
                    "suppress_warnings": {"type": "boolean", "description": "Suppress non-fatal import warning dialogs", "default": True},
                    "copy_assets": {"type": "boolean", "description": "Copy media into the library (true) or link in place (false). Omit for FCP default"},
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="list_fcp_libraries",
            description="LIVE: enumerate the running Final Cut Pro's open libraries, events, and projects via Apple's read-only scripting dictionary. Refuses to launch FCP unless allow_launch is true. macOS-only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "allow_launch": {"type": "boolean", "description": "Launch FCP if it isn't running", "default": False},
                },
            }
        ),
    ]


def _legacy_tools_enabled() -> bool:
    """Advertise the flat tool schemas alongside the groups.

    Off by default so new users pay only the small schema cost. Existing configs
    that call flat tool names keep working either way, because call_tool
    dispatches from TOOL_HANDLERS and never consults this list.
    """
    return os.environ.get("FCP_MCP_LEGACY_TOOLS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def list_tools() -> list[Tool]:
    tools = [_group_tool(name) for name in TOOL_GROUPS]
    if _legacy_tools_enabled():
        tools.extend(_legacy_tool_list())
    return tools


# ============================================================================
# QC DETECTION HELPERS — Pure detection logic, reusable across handlers
# ============================================================================


def _detect_flash_frames(
    tl: Any, *, critical_threshold: int = 2, warning_threshold: int = 6,
) -> list:
    """Find clips shorter than *warning_threshold* frames.

    Returns a list of ``FlashFrame`` objects sorted by severity.  Shared by
    ``handle_detect_flash_frames`` and ``handle_validate_timeline`` so the
    detection logic lives in exactly one place.

    Connected clips are scanned alongside spine clips.  A two-frame B-roll
    shot is a flash frame whether it sits on the primary storyline or on
    lane 4, and on a music video every clip is on a lane — spine-only
    detection returned a clean bill of health for a timeline holding 129 of
    them (issue #16).  Their reported position is measured from the timeline
    origin, so a sequence starting at 01:00:00:00 does not report every cut
    an hour late.
    """
    fps = tl.frame_rate
    origin = tl.origin_seconds
    flash_frames: list[FlashFrame] = []

    def consider(name: str, duration_seconds: float, start: Timecode) -> None:
        duration_frames = int(duration_seconds * fps)
        if duration_frames >= warning_threshold:
            return
        severity = (
            FlashFrameSeverity.CRITICAL
            if duration_frames < critical_threshold
            else FlashFrameSeverity.WARNING
        )
        flash_frames.append(FlashFrame(
            clip_name=name, clip_id=name, start=start,
            duration_frames=duration_frames,
            duration_seconds=duration_seconds, severity=severity,
        ))

    def relative(position: Timecode) -> Timecode:
        """Re-base a position onto the timeline origin, exactly when needed.

        A 0-origin timeline (every existing fixture) hands back the same
        Timecode object rather than round-tripping it through seconds, so
        the reported frame numbers cannot drift by float error.
        """
        if not origin:
            return position
        return Timecode(
            frames=position.frames - int(round(origin * fps)),
            frame_rate=fps,
        )

    for clip in tl.clips:
        consider(clip.name, clip.duration_seconds, relative(clip.start))
    for connected in getattr(tl, 'connected_clips', []) or []:
        position = connected.offset or Timecode(frames=0, frame_rate=fps)
        consider(connected.name, connected.duration_seconds, relative(position))

    return flash_frames


def _detect_gaps(tl: Any, *, min_gap_frames: int = 1) -> list:
    """Find inter-clip gaps of at least *min_gap_frames* length.

    Returns a list of ``GapInfo`` objects.  Shared by ``handle_detect_gaps``
    and ``handle_validate_timeline``.
    """
    fps = tl.frame_rate
    min_gap_seconds = min_gap_frames / fps
    gaps: list[GapInfo] = []
    sorted_clips = sorted(tl.clips, key=lambda c: c.start.seconds)
    for i in range(len(sorted_clips) - 1):
        current_end = sorted_clips[i].end.seconds
        next_start = sorted_clips[i + 1].start.seconds
        gap_duration = next_start - current_end
        if gap_duration >= min_gap_seconds:
            gaps.append(GapInfo(
                start=Timecode(frames=int(current_end * fps), frame_rate=fps),
                duration_frames=int(gap_duration * fps),
                duration_seconds=gap_duration,
                previous_clip=sorted_clips[i].name,
                next_clip=sorted_clips[i + 1].name,
            ))
    return gaps


def _detect_duplicate_groups(tl: Any, *, mode: str = "same_source") -> list:
    """Group clips that share a source media reference.

    Returns a list of ``DuplicateGroup`` objects.  Shared by
    ``handle_detect_duplicates`` and ``handle_validate_timeline``.
    """
    source_groups: dict[str, list[dict]] = {}
    for clip in tl.clips:
        source_key = clip.media_path or clip.name
        if source_key not in source_groups:
            source_groups[source_key] = []
        source_groups[source_key].append({
            'name': clip.name,
            'start': clip.start.seconds,
            'duration': clip.duration_seconds,
            'source_start': clip.source_start.seconds if clip.source_start else 0,
            'source_duration': clip.duration_seconds,
            'timecode': format_timecode(clip.start),
        })

    duplicates: list[DuplicateGroup] = []
    for source_key, clips in source_groups.items():
        if len(clips) <= 1:
            continue
        group = DuplicateGroup(
            source_ref=source_key,
            source_name=source_key.split('/')[-1] if '/' in source_key else source_key,
            clips=clips,
        )
        if mode == "same_source":
            duplicates.append(group)
        elif mode == "overlapping_ranges" and group.has_overlapping_ranges:
            duplicates.append(group)
        elif mode == "identical":
            seen_ranges: set[tuple] = set()
            identical_clips = []
            for c in clips:
                range_key = (c['source_start'], c['source_duration'])
                if range_key in seen_ranges:
                    identical_clips.append(c)
                seen_ranges.add(range_key)
            if identical_clips:
                group.clips = identical_clips
                duplicates.append(group)
    return duplicates


# ============================================================================
# TOOL HANDLERS — Each tool gets its own function
# ============================================================================

# ----- READ HANDLERS -----

async def handle_list_projects(arguments: dict) -> Sequence[TextContent]:
    directory = arguments.get("directory", PROJECTS_DIR)
    resolved_dir = _validate_directory(
        directory, allowed_roots=LIST_ROOTS if _SANDBOX_ENABLED else None
    )
    files, truncated = find_fcpxml_files_capped(resolved_dir)
    if not files:
        return _text_result(f"No FCPXML files found in {directory}")
    notice = (
        f"\n\n⚠️ TRUNCATED: the walk stopped at the {MAX_DISCOVERY_FILES}-file cap. "
        f"This list is incomplete — narrow the directory or raise "
        f"FCP_MAX_DISCOVERY_FILES." if truncated else ""
    )
    return _text_result(
        f"Found {len(files)} FCPXML file(s):\n"
        + "\n".join(f"  - {f}" for f in files)
        + notice
    )


async def handle_analyze_timeline(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    durs = [c.duration_seconds for c in tl.clips]
    avg, med, mn, mx = (0, 0, 0, 0) if not durs else (
        sum(durs)/len(durs), sorted(durs)[len(durs)//2], min(durs), max(durs))
    return _text_result(f"""# Timeline Analysis: {tl.name}

## Overview
- **Duration**: {format_duration(tl.duration.seconds)}
- **Resolution**: {tl.width}x{tl.height} @ {fcp_frame_rate_name(tl.frame_rate)}fps

## Clip Statistics
- **Total Clips**: {tl.total_clips}
- **Total Cuts**: {tl.total_cuts}
- **Transitions**: {len(tl.transitions)}

## Pacing
- **Average**: {format_duration(avg)}
- **Median**: {format_duration(med)}
- **Shortest**: {format_duration(mn)}
- **Longest**: {format_duration(mx)}
- **Cuts/Minute**: {tl.cuts_per_minute:.1f}

## Markers
- **Total**: {len(tl.markers)}
- **Chapters**: {len([m for m in tl.markers if m.marker_type == MarkerType.CHAPTER])}
""")


async def handle_list_clips(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    limit = arguments.get("limit")
    clips = tl.clips[:limit] if limit else tl.clips
    result = f"# Clips in {tl.name}\n\n| # | Name | Start | Duration | Keywords |\n|---|------|-------|----------|----------|\n"
    for i, c in enumerate(clips, 1):
        kws = ", ".join(k.value for k in c.keywords) if c.keywords else "-"
        result += f"| {i} | {c.name} | {format_timecode(c.start)} | {format_duration(c.duration_seconds)} | {kws} |\n"
    return _text_result(result)


async def handle_list_markers(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    markers = list(tl.markers)
    for clip in tl.clips:
        markers.extend(clip.markers)
    marker_type = arguments.get("marker_type", "all")
    if marker_type != "all":
        markers = [m for m in markers if m.marker_type == MarkerType.from_string(marker_type)]
    markers.sort(key=lambda m: m.position.frames)
    fmt = arguments.get("format", "detailed")
    if fmt == "youtube":
        result = "# YouTube Chapters\n\n" + "\n".join(f"{m.to_youtube_timestamp()} {m.name}" for m in markers)
    elif fmt == "simple":
        result = "\n".join(f"{format_timecode(m.position)} - {m.name}" for m in markers)
    else:
        result = f"# Markers ({len(markers)})\n\n| TC | Name | Type |\n|---|------|------|\n"
        result += "\n".join(f"| {format_timecode(m.position)} | {m.name} | {m.marker_type.value} |" for m in markers)
    return _text_result(result)


async def handle_find_short_cuts(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    threshold = arguments.get("threshold_seconds", 0.5)
    short = tl.get_clips_shorter_than(threshold)
    if not short:
        return _text_result(f"No clips shorter than {threshold}s")
    return _text_result(_format_clip_table(
        short, f"# Short Clips (< {threshold}s) - {len(short)} found",
    ))


async def handle_find_long_clips(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    threshold = arguments.get("threshold_seconds", 10.0)
    long = tl.get_clips_longer_than(threshold)
    if not long:
        return _text_result(f"No clips longer than {threshold}s")
    return _text_result(_format_clip_table(
        long, f"# Long Clips (> {threshold}s) - {len(long)} found",
    ))


async def handle_list_keywords(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    keywords = {}
    for clip in tl.clips:
        for kw in clip.keywords:
            keywords.setdefault(kw.value, []).append(clip.name)
    if not keywords:
        return _text_result("No keywords found")
    result = f"# Keywords ({len(keywords)})\n\n"
    for kw, clips in sorted(keywords.items()):
        result += f"**{kw}** ({len(clips)} clips)\n"
    return _text_result(result)


async def handle_export_edl(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    edl = f"TITLE: {tl.name}\nFCM: NON-DROP FRAME\n\n"
    for i, c in enumerate(tl.clips, 1):
        edl += f"{i:03d}  AX       V     C        {format_timecode(c.source_start)} {format_timecode(c.end)} {format_timecode(c.start)} {format_timecode(c.end)}\n"
        edl += f"* FROM CLIP NAME: {c.name}\n\n"
    return _text_result(f"```edl\n{edl}```")


async def handle_export_csv(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    csv = "Name,Start,End,Duration,Keywords\n"
    for c in tl.clips:
        kws = "|".join(k.value for k in c.keywords)
        csv += f'"{c.name}",{format_timecode(c.start)},{format_timecode(c.end)},{c.duration_seconds:.3f},"{kws}"\n'
    return _text_result(f"```csv\n{csv}```")


async def handle_analyze_pacing(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    if not tl.clips:
        return _text_result("No clips to analyze")
    durs = [c.duration_seconds for c in tl.clips]
    avg = sum(durs) / len(durs)
    q_len = len(durs) // 4 or 1
    segments = [durs[i:i+q_len] for i in range(0, len(durs), q_len)][:4]
    seg_avgs = [sum(s)/len(s) if s else 0 for s in segments]
    suggestions = []
    flash = [c for c in tl.clips if c.duration_seconds < 0.2]
    if flash:
        suggestions.append(f"  {len(flash)} potential flash frames (< 0.2s)")
    long = [c for c in tl.clips if c.duration_seconds > 30]
    if long:
        suggestions.append(f"  {len(long)} long takes (> 30s) - consider trimming")
    if len(seg_avgs) >= 4 and seg_avgs[3] < seg_avgs[0] * 0.7:
        suggestions.append("  Pacing accelerates toward end - good for building energy")
    elif len(seg_avgs) >= 4 and seg_avgs[3] > seg_avgs[0] * 1.3:
        suggestions.append("  Pacing slows toward end - consider tightening")
    return _text_result(f"""# Pacing Analysis: {tl.name}

## Overall
- **Avg Cut**: {format_duration(avg)}
- **Cuts/Min**: {tl.cuts_per_minute:.1f}

## By Section
| Q1 | Q2 | Q3 | Q4 |
|----|----|----|----|
| {format_duration(seg_avgs[0]) if len(seg_avgs) > 0 else 'N/A'} | {format_duration(seg_avgs[1]) if len(seg_avgs) > 1 else 'N/A'} | {format_duration(seg_avgs[2]) if len(seg_avgs) > 2 else 'N/A'} | {format_duration(seg_avgs[3]) if len(seg_avgs) > 3 else 'N/A'} |

## Suggestions
{_fmt_suggestions(suggestions)}
""")


async def handle_list_library_clips(arguments: dict) -> Sequence[TextContent]:
    filepath = _validate_filepath(arguments["filepath"], ('.fcpxml', '.fcpxmld'))
    parser = FCPXMLParser()
    parser.parse_file(filepath)
    keywords = arguments.get("keywords")
    library_clips = parser.get_library_clips(keywords=keywords)
    limit = arguments.get("limit")
    if limit:
        library_clips = library_clips[:limit]
    if not library_clips:
        return _text_result("No library clips found")
    result = f"# Library Clips ({len(library_clips)} available)\n\n"
    result += "| ID | Name | Duration | Has Video | Has Audio |\n"
    result += "|----|------|----------|-----------|----------|\n"
    for c in library_clips:
        result += f"| {c['asset_id']} | {c['name']} | {format_duration(c['duration_seconds'])} | {'Y' if c['has_video'] else 'N'} | {'Y' if c['has_audio'] else 'N'} |\n"
    result += "\n*Use `insert_clip` to add these to your timeline.*"
    return _text_result(result)


# ----- QC / VALIDATION HANDLERS -----

async def handle_detect_flash_frames(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    critical_threshold = arguments.get("critical_threshold_frames", 2)
    warning_threshold = arguments.get("warning_threshold_frames", 6)

    flash_frames = _detect_flash_frames(
        tl, critical_threshold=critical_threshold, warning_threshold=warning_threshold,
    )

    if not flash_frames:
        return _text_result(f"No flash frames detected (threshold: {warning_threshold} frames)")

    critical = [f for f in flash_frames if f.severity == FlashFrameSeverity.CRITICAL]
    warnings = [f for f in flash_frames if f.severity == FlashFrameSeverity.WARNING]

    result = f"""# Flash Frame Detection

## Summary
- **Critical** (< {critical_threshold} frames): {len(critical)} found
- **Warning** (< {warning_threshold} frames): {len(warnings)} found
- **Total**: {len(flash_frames)} flash frames

## Critical Flash Frames
"""
    flash_headers = ["Clip", "Timecode", "Frames", "Duration"]
    if critical:
        result += _markdown_table(flash_headers, [
            [f.clip_name, format_timecode(f.start), f"{f.duration_frames}f", format_duration(f.duration_seconds)]
            for f in critical
        ]) + "\n"
    else:
        result += "_None_\n"

    result += "\n## Warning Flash Frames\n"
    if warnings:
        result += _markdown_table(flash_headers, [
            [f.clip_name, format_timecode(f.start), f"{f.duration_frames}f", format_duration(f.duration_seconds)]
            for f in warnings
        ]) + "\n"
    else:
        result += "_None_\n"

    result += "\n*Use `fix_flash_frames` to automatically resolve these issues.*"
    return _text_result(result)


async def handle_detect_duplicates(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    mode = arguments.get("mode", "same_source")

    duplicates = _detect_duplicate_groups(tl, mode=mode)

    if not duplicates:
        return _text_result(f"No duplicate clips found (mode: {mode})")

    result = f"""# Duplicate Clip Detection

## Summary
- **Mode**: {mode}
- **Duplicate Groups**: {len(duplicates)}
- **Total Duplicate Clips**: {sum(g.count for g in duplicates)}

## Duplicate Groups
"""
    for group in duplicates:
        result += f"\n### {group.source_name} ({group.count} uses)\n"
        result += "| Clip Name | Timeline Position | Duration |\n|-----------|-------------------|----------|\n"
        for c in group.clips:
            result += f"| {c['name']} | {c['timecode']} | {format_duration(c['duration'])} |\n"

    return _text_result(result)


async def handle_detect_gaps(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    min_gap_frames = arguments.get("min_gap_frames", 1)

    gaps = _detect_gaps(tl, min_gap_frames=min_gap_frames)

    # Gap detection is a primary-storyline concept. Space between connected
    # clips on a lane is not a gap — B-roll and titles are meant to be sparse,
    # so flagging every hole in lane 3 would be noise. State the scope instead
    # of implying the whole timeline was checked, because on a music video the
    # spine is one <gap> and this check inspects nothing at all (issue #16).
    connected_count = len(getattr(tl, 'connected_clips', []) or [])
    scope_note = ""
    if connected_count:
        lanes = len({c.lane for c in tl.connected_clips})
        scope_note = (
            f"\n\n_Scope: the primary storyline only ({len(tl.clips)} spine "
            f"clip(s)). {connected_count} connected clip(s) across {lanes} "
            "lane(s) were not checked — space between connected clips is "
            "normal, not a gap._"
        )

    if not gaps:
        return _text_result(
            f"No gaps detected on the primary storyline "
            f"(minimum: {min_gap_frames} frame(s)){scope_note}"
        )

    result = f"""# Gap Detection

## Summary
- **Gaps Found**: {len(gaps)}
- **Total Gap Duration**: {format_duration(sum(g.duration_seconds for g in gaps))}
- **Minimum Detection**: {min_gap_frames} frame(s)

## Gaps
"""
    result += _markdown_table(
        ["Position", "Duration", "Between"],
        [[gap.timecode, f"{gap.duration_frames}f ({format_duration(gap.duration_seconds)})",
          f"{gap.previous_clip} -> {gap.next_clip}"] for gap in gaps],
    ) + "\n"

    result += "\n*Use `fill_gaps` to automatically close these gaps.*"
    result += scope_note
    return _text_result(result)


# ----- WRITE HANDLERS -----

async def handle_add_marker(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    marker_type = MarkerType.from_string(arguments.get("marker_type", "standard"))
    modifier.add_marker_at_timeline(
        timecode=arguments["timecode"], name=arguments["name"],
        marker_type=marker_type, note=arguments.get("note"),
    )
    modifier.save(output_path)
    return _text_result(
        f"Added marker '{arguments['name']}' at {arguments['timecode']}\n\n"
        f"Saved to: {output_path}"
    )


async def handle_batch_add_markers(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    markers, dropped = _cap_markers(arguments.get("markers", []) or [])
    markers_added = modifier.batch_add_markers(
        markers=markers,
        auto_at_cuts=arguments.get("auto_at_cuts", False),
        auto_at_intervals=arguments.get("auto_at_intervals"),
    )
    modifier.save(output_path)
    return _text_result(
        f"Added {len(markers_added)} markers\n\nSaved to: {output_path}"
        + _marker_cap_notice(dropped)
    )


async def handle_trim_clip(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    modifier.trim_clip(
        clip_id=arguments["clip_id"],
        trim_start=arguments.get("trim_start"),
        trim_end=arguments.get("trim_end"),
        ripple=arguments.get("ripple", True),
    )
    modifier.save(output_path)
    return _text_result(f"Trimmed clip '{arguments['clip_id']}'\n\nSaved to: {output_path}")


async def handle_reorder_clips(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    modifier.reorder_clips(
        clip_ids=arguments["clip_ids"],
        target_position=arguments["target_position"],
        ripple=arguments.get("ripple", True),
    )
    modifier.save(output_path)
    clips_moved = ", ".join(arguments["clip_ids"])
    return _text_result(f"Moved clips [{clips_moved}] to {arguments['target_position']}\n\nSaved to: {output_path}")


async def handle_add_transition(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    modifier.add_transition(
        clip_id=arguments["clip_id"],
        position=arguments.get("position", "end"),
        transition_type=arguments.get("transition_type", "cross-dissolve"),
        duration=arguments.get("duration", "00:00:00:15"),
    )
    modifier.save(output_path)
    return _text_result(f"Added {arguments.get('transition_type', 'cross-dissolve')} to '{arguments['clip_id']}'\n\nSaved to: {output_path}")


async def handle_change_speed(arguments: dict) -> Sequence[TextContent]:
    speed = arguments["speed"]
    if not isinstance(speed, (int, float)) or speed <= 0 or speed > 100:
        raise ValueError(
            f"Speed must be a positive number between 0 (exclusive) and 100, got {speed!r}"
        )
    filepath, output_path, modifier = _setup_modifier(arguments)
    modifier.change_speed(
        clip_id=arguments["clip_id"],
        speed=speed,
        preserve_pitch=arguments.get("preserve_pitch", True),
    )
    modifier.save(output_path)
    speed_desc = f"{speed}x" if speed >= 1 else f"{int(1/speed)}x slow motion"
    return _text_result(f"Changed speed of '{arguments['clip_id']}' to {speed_desc}\n\nSaved to: {output_path}")


async def handle_delete_clips(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    requested = arguments["clip_ids"]
    deleted = modifier.delete_clip(
        clip_ids=requested,
        ripple=arguments.get("ripple", True),
    )
    modifier.save(output_path)
    # Report what was deleted, never what was asked for. Counting the request
    # made this answer "Deleted 2 clip(s)" to a request that matched nothing.
    missed = [c for c in requested if c not in deleted]
    text = f"Deleted {len(deleted)} clip(s)"
    if missed:
        text += (
            f"\n\nNo clip matched: {', '.join(repr(m) for m in missed)} — "
            f"an id here is a clip NAME, and names are matched exactly."
        )
    return _text_result(f"{text}\n\nSaved to: {output_path}")


async def handle_split_clip(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    new_clips = modifier.split_clip(
        clip_id=arguments["clip_id"],
        split_points=arguments["split_points"],
    )
    modifier.save(output_path)
    return _text_result(f"Split '{arguments['clip_id']}' into {len(new_clips)} clips\n\nSaved to: {output_path}")


async def handle_insert_clip(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    new_clip = modifier.insert_clip(
        asset_id=arguments.get("asset_id"),
        asset_name=arguments.get("asset_name"),
        position=arguments["position"],
        duration=arguments.get("duration"),
        in_point=arguments.get("in_point"),
        out_point=arguments.get("out_point"),
        ripple=arguments.get("ripple", True),
    )
    modifier.save(output_path)
    clip_name = new_clip.get('name', 'Unknown')
    pos = arguments["position"]
    return _text_result(f"Inserted '{clip_name}' at position '{pos}'\n\nSaved to: {output_path}")


# ----- BATCH FIX HANDLERS -----

async def handle_fix_flash_frames(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments, "_flash_fixed")
    fixed = modifier.fix_flash_frames(
        mode=arguments.get("mode", "auto"),
        threshold_frames=arguments.get("threshold_frames", 6),
    )
    modifier.save(output_path)

    if not fixed:
        return _text_result("No flash frames found to fix.")

    result = _format_batch_result(
        title="Flash Frames Fixed",
        summary={"Fixed": f"{len(fixed)} flash frames", "Mode": arguments.get('mode', 'auto')},
        headers=["Clip", "Frames", "Action", "Result"],
        rows=[
            [f['clip_name'], f"{f['duration_frames']}f", f['action'], f"Extended: {f.get('extended_clip', 'N/A')}"]
            for f in fixed
        ],
        output_path=output_path,
    )
    return _text_result(result)


async def handle_rapid_trim(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments, "_rapid_trim")
    trimmed = modifier.rapid_trim(
        max_duration=arguments["max_duration"],
        min_duration=arguments.get("min_duration"),
        keywords=arguments.get("keywords"),
        trim_from=arguments.get("trim_from", "end"),
    )
    modifier.save(output_path)

    if not trimmed:
        return _text_result(f"No clips exceeded {arguments['max_duration']} - nothing trimmed.")

    total_before = sum(t['original_duration'] for t in trimmed)
    total_after = sum(t['new_duration'] for t in trimmed)

    result = _format_batch_result(
        title="Rapid Trim Complete",
        summary={
            "Clips Trimmed": str(len(trimmed)),
            "Max Duration": str(arguments['max_duration']),
            "Trim From": arguments.get('trim_from', 'end'),
            "Time Saved": format_duration(total_before - total_after),
        },
        headers=["Clip", "Before", "After"],
        rows=[
            [t['clip_name'], format_duration(t['original_duration']), format_duration(t['new_duration'])]
            for t in trimmed
        ],
        output_path=output_path,
    )
    return _text_result(result)


async def handle_fill_gaps(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments, "_gaps_filled")
    filled = modifier.fill_gaps(
        mode=arguments.get("mode", "extend_previous"),
        max_gap=arguments.get("max_gap"),
    )
    modifier.save(output_path)

    if not filled:
        return _text_result("No gaps found to fill.")

    result = _format_batch_result(
        title="Gaps Filled",
        summary={"Gaps Filled": str(len(filled)), "Mode": arguments.get('mode', 'extend_previous')},
        headers=["Position", "Duration", "Action"],
        rows=[[g['timecode'], f"{g['duration_frames']}f", g['action']] for g in filled],
        output_path=output_path,
    )
    return _text_result(result)


async def handle_validate_timeline(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])
    checks = arguments.get("checks", ["all"])
    run_all = "all" in checks

    issues: list[str] = []
    flash_count = 0
    gap_count = 0
    duplicate_count = 0

    if run_all or "flash_frames" in checks:
        flashes = _detect_flash_frames(tl)
        flash_count = len(flashes)
        for f in flashes:
            severity = "error" if f.severity == FlashFrameSeverity.CRITICAL else "warning"
            issues.append(
                f"- [{severity.upper()}] Flash frame: {f.clip_name} "
                f"({f.duration_frames}f) at {format_timecode(f.start)}"
            )

    if run_all or "gaps" in checks:
        detected_gaps = _detect_gaps(tl)
        gap_count = len(detected_gaps)
        for g in detected_gaps:
            issues.append(f"- [WARNING] Gap: {g.duration_frames}f at {g.timecode}")

    if run_all or "duplicates" in checks:
        dup_groups = _detect_duplicate_groups(tl)
        for group in dup_groups:
            duplicate_count += group.count
            issues.append(
                f"- [INFO] Duplicate source: {group.source_name} ({group.count} uses)"
            )

    error_weight = 10
    warning_weight = 3
    info_weight = 1
    errors = len([i for i in issues if "[ERROR]" in i])
    warnings = len([i for i in issues if "[WARNING]" in i])
    infos = len([i for i in issues if "[INFO]" in i])
    penalty = (errors * error_weight) + (warnings * warning_weight) + (infos * info_weight)
    health_score = max(0, 100 - penalty)

    result = f"""# Timeline Validation: {tl.name}

## Health Score: {health_score}%

## Summary
| Check | Count | Status |
|-------|-------|--------|
| Flash Frames | {flash_count} | {'PASS' if flash_count == 0 else 'FAIL'} |
| Gaps | {gap_count} | {'PASS' if gap_count == 0 else 'WARN'} |
| Duplicate Sources | {duplicate_count} | {'PASS' if duplicate_count == 0 else 'INFO'} |

## Issues ({len(issues)})
"""
    if issues:
        result += "\n".join(issues[:20])
        if len(issues) > 20:
            result += f"\n... and {len(issues) - 20} more issues"
    else:
        result += "_No issues found!_"

    result += "\n\n*Use `fix_flash_frames` and `fill_gaps` to automatically resolve issues.*"
    return _text_result(result)


# ----- GENERATION HANDLERS -----

async def handle_auto_rough_cut(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, generator = _setup_generator(arguments, "_roughcut")

    segments = None
    if arguments.get("segments"):
        segments = [
            SegmentSpec(
                name=s.get("name", "Segment"),
                keywords=s.get("keywords", []),
                duration_seconds=s.get("duration", 0),
                priority=s.get("priority", "best"),
            )
            for s in arguments["segments"]
        ]
    result = generator.generate(
        output_path=output_path,
        target_duration=arguments["target_duration"],
        pacing=arguments.get("pacing", "medium"),
        keywords=arguments.get("keywords"),
        segments=segments,
        priority=arguments.get("priority", "best"),
        favorites_only=arguments.get("favorites_only", False),
        add_transitions=arguments.get("add_transitions", False),
        min_source_separation=int(arguments.get("min_source_separation", 0)),
    )

    return _text_result(f"""# Rough Cut Generated

## Summary
- **Clips Used**: {result.clips_used} of {result.clips_available} available
- **Target Duration**: {format_duration(result.target_duration)}
- **Actual Duration**: {format_duration(result.actual_duration)}
- **Average Clip**: {format_duration(result.average_clip_duration)}
- **{_diversity.describe(result.diversity_score)}**

## Output
Saved to: `{result.output_path}`

**Next step**: Import this FCPXML into Final Cut Pro (File > Import > XML)
""")


async def handle_generate_montage(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, generator = _setup_generator(arguments, "_montage")
    result = generator.generate_montage(
        output_path=output_path,
        target_duration=arguments["target_duration"],
        pacing_curve=arguments.get("pacing_curve", "accelerating"),
        start_duration=arguments.get("start_duration", 2.0),
        end_duration=arguments.get("end_duration", 0.5),
        keywords=arguments.get("keywords"),
        add_transitions=arguments.get("add_transitions", False),
    )

    curve_desc = {
        'accelerating': 'slow to fast (builds energy)',
        'decelerating': 'fast to slow (winds down)',
        'pyramid': 'slow to fast to slow (dramatic arc)',
        'constant': 'same duration throughout',
    }

    return _text_result(f"""# Montage Generated

## Summary
- **Clips Used**: {result['clips_used']} of {result['clips_available']} available
- **Target Duration**: {format_duration(result['target_duration'])}
- **Actual Duration**: {format_duration(result['actual_duration'])}
- **Pacing Curve**: {result['pacing_curve']} - {curve_desc.get(result['pacing_curve'], '')}

## Pacing
- **Start Clip Duration**: {format_duration(result['start_clip_duration'])}
- **End Clip Duration**: {format_duration(result['end_clip_duration'])}

## Output
Saved to: `{result['output_path']}`
""")


async def handle_generate_ab_roll(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, generator = _setup_generator(arguments, "_ab_roll")
    result = generator.generate_ab_roll(
        output_path=output_path,
        target_duration=arguments["target_duration"],
        a_keywords=arguments["a_keywords"],
        b_keywords=arguments["b_keywords"],
        a_duration=arguments.get("a_duration", "5s"),
        b_duration=arguments.get("b_duration", "3s"),
        start_with=arguments.get("start_with", "a"),
        add_transitions=arguments.get("add_transitions", True),
    )

    return _text_result(f"""# A/B Roll Edit Generated

## Summary
- **A-Roll Segments**: {result['a_segments']} (from {result['a_clips_available']} available)
- **B-Roll Segments**: {result['b_segments']} (from {result['b_clips_available']} available)
- **Total Clips**: {result['clips_used']}

## Timing
- **Target Duration**: {format_duration(result['target_duration'])}
- **Actual Duration**: {format_duration(result['actual_duration'])}
- **A-Roll Duration**: {result['a_duration_setting']} per segment
- **B-Roll Duration**: {result['b_duration_setting']} per cutaway

## Output
Saved to: `{result['output_path']}`

**Next step**: Import this FCPXML into Final Cut Pro (File > Import > XML)
""")


# ----- BEAT SYNC HANDLERS -----

async def handle_import_beat_markers(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path = _resolve_io_paths(arguments, "_beats")
    beats_path = _validate_filepath(arguments["beats_path"], ('.json',))

    with open(beats_path, 'r') as f:
        beats_data = json.load(f)
    _check_json_depth(beats_data)

    beat_times = []
    if isinstance(beats_data, list):
        beat_times = beats_data
    elif isinstance(beats_data, dict):
        beat_times = beats_data.get('beats', beats_data.get('times', beats_data.get('markers', [])))

    beat_filter = arguments.get("beat_filter", "all")
    if beat_filter == "downbeat" and isinstance(beats_data, dict):
        beat_times = beats_data.get('downbeats', beat_times[::4])
    elif beat_filter == "measure" and isinstance(beats_data, dict):
        beat_times = beats_data.get('measures', beat_times[::4])

    markers = []
    marker_type = arguments.get("marker_type", "standard")
    for i, beat_time in enumerate(beat_times):
        if isinstance(beat_time, (int, float)):
            markers.append({
                'timecode': f"{beat_time}s",
                'name': f"Beat {i+1}",
                'marker_type': marker_type.upper(),
            })
        elif isinstance(beat_time, dict):
            markers.append({
                'timecode': f"{beat_time.get('time', beat_time.get('position', 0))}s",
                'name': beat_time.get('label', f"Beat {i+1}"),
                'marker_type': marker_type.upper(),
            })

    modifier = FCPXMLModifier(filepath)

    # Songs routinely run longer than the edit — beats past the timeline's
    # end are skipped (add_marker_at_timeline would raise on them).
    timeline_end = modifier._timeline_duration().to_seconds()
    in_range = [m for m in markers if float(m['timecode'].rstrip('s')) < timeline_end]
    skipped_count = len(markers) - len(in_range)
    in_range, dropped = _cap_markers(in_range)

    added = modifier.batch_add_markers(markers=in_range)
    modifier.save(output_path)

    skipped_note = (
        f"- **Skipped**: {skipped_count} beat(s) beyond the timeline end "
        f"({format_duration(timeline_end)})\n" if skipped_count else ""
    )
    return _text_result(f"""# Beat Markers Imported

## Summary
- **Beats Found**: {len(beat_times)}
- **Markers Added**: {len(added)}
{skipped_note}- **Filter**: {beat_filter}
- **Marker Type**: {marker_type}

## Output
Saved to: `{output_path}`

*Use `snap_to_beats` to align your cuts to these markers.*
""" + _marker_cap_notice(dropped))


async def handle_snap_to_beats(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path = _resolve_io_paths(arguments, "_synced")
    max_shift = arguments.get("max_shift_frames", 6)
    prefer = arguments.get("prefer", "nearest")

    parser = FCPXMLParser()
    project = parser.parse_file(filepath)
    if not project.timelines:
        return _no_timeline()

    tl = project.primary_timeline
    fps = tl.frame_rate

    markers = list(tl.markers)
    for clip in tl.clips:
        markers.extend(clip.markers)

    modifier = FCPXMLModifier(filepath)
    # The connected path reads markers straight off the XML in exact
    # rationals and normalises them to timeline-relative seconds, because a
    # connected timeline's markers live on the gap or on the clips
    # themselves and its offsets start at 3600s.  The spine path keeps the
    # parser-derived list it has always used, unchanged.
    connected_marker_times = modifier.timeline_marker_seconds()

    if not markers and not connected_marker_times:
        return _text_result("No markers found. Use `import_beat_markers` first.")

    marker_times = sorted([m.position.seconds for m in markers])

    spine = modifier._get_spine()
    adjusted_count = 0
    total_shift = 0

    clips_list = [c for c in spine if c.tag in ('clip', 'asset-clip', 'video', 'ref-clip')]

    for i, clip in enumerate(clips_list[1:], 1):
        cut_offset = modifier._parse_time(clip.get('offset', '0s'))
        cut_seconds = cut_offset.to_seconds()

        best_marker = None
        best_distance = float('inf')

        for marker_time in marker_times:
            distance = abs(marker_time - cut_seconds)
            distance_frames = distance * fps

            if distance_frames <= max_shift:
                if prefer == "earlier" and marker_time <= cut_seconds:
                    if distance < best_distance:
                        best_distance = distance
                        best_marker = marker_time
                elif prefer == "later" and marker_time >= cut_seconds:
                    if distance < best_distance:
                        best_distance = distance
                        best_marker = marker_time
                elif prefer == "nearest":
                    if distance < best_distance:
                        best_distance = distance
                        best_marker = marker_time

        if best_marker is not None and best_distance > 0.001:
            shift = best_marker - cut_seconds
            shift_frames = int(shift * fps)

            prev_clip = clips_list[i - 1]
            prev_dur = modifier._parse_time(prev_clip.get('duration', '0s'))
            new_prev_dur = prev_dur + modifier._parse_time(f"{shift}s")
            prev_clip.set('duration', new_prev_dur.to_fcpxml())

            new_offset = modifier._parse_time(f"{best_marker}s")
            clip.set('offset', new_offset.to_fcpxml())

            adjusted_count += 1
            total_shift += abs(shift_frames)

    # Connected clips (lanes) — the shape a music video actually has, where
    # the spine loop above sees nothing at all.
    connected = modifier.snap_connected_clips(
        marker_seconds=connected_marker_times,
        max_shift_frames=max_shift,
        prefer=prefer,
        include_audio_lanes=arguments.get("include_audio_lanes", False),
    )

    modifier.save(output_path)

    spine_considered = max(0, len(clips_list) - 1)
    considered = spine_considered + connected['considered']
    moved = adjusted_count + len(connected['moved'])
    total_shift += sum(abs(m['shift_frames']) for m in connected['moved'])
    avg_shift = total_shift / moved if moved else 0

    return _text_result(
        _format_snap_report(
            output_path=output_path,
            max_shift=max_shift,
            prefer=prefer,
            considered=considered,
            moved=moved,
            avg_shift=avg_shift,
            spine_considered=spine_considered,
            spine_moved=adjusted_count,
            connected=connected,
            # The XML-derived list is the complete one; marker_times is the
            # parser-derived list the spine path has always used and covers
            # the same markers, so adding them would double-count.
            marker_count=len(connected_marker_times) or len(marker_times),
        )
    )


def _format_snap_report(
    *, output_path: str, max_shift: int, prefer: str, considered: int,
    moved: int, avg_shift: float, spine_considered: int, spine_moved: int,
    connected: dict, marker_count: int,
) -> str:
    """Render the snap_to_beats result, including everything that did NOT move.

    The bug this reports around (issue #16) was not that snapping was wrong,
    it was that snapping did nothing and said "Your edits are now synced to
    the beat!" while doing it.  A cut that could not move is as much of a
    result as one that did, so every considered cut is accounted for in
    exactly one bucket below and the headline states the count either way.
    """
    aligned = connected['already_aligned']
    out_of_range = connected['out_of_range']
    skipped = connected['skipped']

    if considered == 0:
        headline = (
            "**No cut points found.** Nothing on this timeline's spine or "
            "lanes could be moved."
        )
    elif moved == 0:
        headline = f"**0 of {considered} cuts moved.** Nothing was changed."
    else:
        headline = f"**{moved} of {considered} cuts moved.**"

    lines = [
        "# Cuts Snapped to Beats",
        "",
        headline,
        "",
        "## Summary",
        f"- **Cuts Considered**: {considered}"
        + (f" ({spine_considered} on the spine, {connected['considered']} connected"
           f" across lanes {', '.join(str(x) for x in connected['lanes'])})"
           if connected['considered'] else ""),
        f"- **Cuts Moved**: {moved}"
        + (f" ({spine_moved} on the spine, {len(connected['moved'])} connected)"
           if connected['considered'] else ""),
        f"- **Already On A Beat**: {len(aligned)}",
        f"- **No Marker Within {max_shift} Frames**: {len(out_of_range)}",
        f"- **Skipped (would collide)**: {len(skipped)}",
        f"- **Markers Available**: {marker_count}",
        f"- **Preference**: {prefer}",
        f"- **Average Shift**: {avg_shift:.1f} frames",
    ]

    if connected['audio_lane_clips']:
        lines += [
            "",
            f"_{connected['audio_lane_clips']} clip(s) on negative (audio) lanes "
            "were left alone — that is the track the beat grid came from. "
            "Pass `include_audio_lanes` to snap them too._",
        ]

    if connected['moved']:
        lines += ["", "## Moved", ""]
        lines.append(_markdown_table(
            ["Clip", "Lane", "From", "To", "Shift"],
            [[m['name'], str(m['lane']), f"{m['at_seconds']:.3f}s",
              f"{m['to_seconds']:.3f}s", f"{m['shift_frames']:+d}f"]
             for m in connected['moved']],
        ))

    if skipped:
        lines += ["", "## Skipped", ""]
        lines.append(_markdown_table(
            ["Clip", "Lane", "At", "Reason"],
            [[s['name'], str(s['lane']), f"{s['at_seconds']:.3f}s", s['reason']]
             for s in skipped],
        ))

    if out_of_range:
        lines += ["", f"## No Marker Within {max_shift} Frames", ""]
        lines.append(_markdown_table(
            ["Clip", "Lane", "At", "Nearest Marker"],
            [[o['name'], str(o['lane']), f"{o['at_seconds']:.3f}s",
              "none" if o['nearest_marker_frames'] is None
              else f"{o['nearest_marker_frames']:.1f}f away"]
             for o in out_of_range],
        ))
        lines += [
            "",
            "_Raise `max_shift_frames` to reach these, or add markers where "
            "the cuts already are._",
        ]

    lines += ["", "## Output", f"Saved to: `{output_path}`", ""]
    return "\n".join(lines)


# ----- SUBTITLE / TRANSCRIPT HANDLERS -----

async def handle_import_srt_markers(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path = _resolve_io_paths(arguments, "_subtitled")
    srt_path = _validate_filepath(arguments["srt_path"], ('.srt', '.vtt'))
    mode = arguments.get("mode", "first_per_minute")
    marker_type = arguments.get("marker_type", "chapter")
    max_label = arguments.get("max_label_length", 50)

    text = Path(srt_path).read_text(encoding='utf-8')

    # Detect format and parse
    if srt_path.endswith('.vtt') or text.strip().startswith('WEBVTT'):
        raw_markers = parse_vtt(text)
        fmt_name = "WebVTT"
    else:
        raw_markers = parse_srt(text)
        fmt_name = "SRT"

    if not raw_markers:
        return _text_result(f"No subtitles found in {srt_path}")

    # Apply mode filtering
    filtered = []
    if mode == "all":
        filtered = raw_markers
    elif mode == "first_per_minute":
        seen_minutes = set()
        for m in raw_markers:
            minute = int(m['seconds'] // 60)
            if minute not in seen_minutes:
                seen_minutes.add(minute)
                filtered.append(m)
    elif mode == "scene_changes":
        # Group by similar text, take first occurrence of each unique line
        seen_texts = set()
        for m in raw_markers:
            # Normalize: lowercase, strip punctuation
            normalized = re.sub(r'[^\w\s]', '', m['text'].lower()).strip()
            words = normalized.split()[:3]  # First 3 words as key
            key = ' '.join(words)
            if key and key not in seen_texts:
                seen_texts.add(key)
                filtered.append(m)

    markers = _raw_markers_to_batch(filtered, marker_type, max_label=max_label)
    markers, dropped = _cap_markers(markers)

    modifier = FCPXMLModifier(filepath)
    added = modifier.batch_add_markers(markers=markers)
    modifier.save(output_path)

    return _text_result(f"""# Subtitle Markers Imported

## Summary
- **Format**: {fmt_name}
- **Subtitles Parsed**: {len(raw_markers)}
- **Mode**: {mode}
- **Markers Added**: {len(added)}
- **Marker Type**: {marker_type}

## Output
Saved to: `{output_path}`
""" + _marker_cap_notice(dropped))


async def handle_import_transcript_markers(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path = _resolve_io_paths(arguments, "_chapters")
    marker_type = arguments.get("marker_type", "chapter")

    # Get transcript text from inline or file
    transcript = arguments.get("transcript")
    transcript_path = arguments.get("transcript_path")

    if not transcript and not transcript_path:
        return _text_result("Provide either 'transcript' (inline text) or 'transcript_path' (path to file)")

    if transcript_path:
        transcript_path = _validate_filepath(transcript_path, ('.txt', '.srt', '.vtt'))
        transcript = Path(transcript_path).read_text(encoding='utf-8')

    transcript, transcript_dropped = _cap_transcript_text(transcript or "")

    raw_markers = parse_transcript_timestamps(transcript)

    if not raw_markers:
        return _text_result("No timestamps found. Expected format: '0:00 Title' or 'HH:MM:SS Title', one per line.")

    markers = _raw_markers_to_batch(raw_markers, marker_type)
    markers, dropped = _cap_markers(markers)

    modifier = FCPXMLModifier(filepath)
    added = modifier.batch_add_markers(markers=markers)
    modifier.save(output_path)

    return _text_result(f"""# Transcript Markers Imported

## Summary
- **Timestamps Found**: {len(raw_markers)}
- **Markers Added**: {len(added)}
- **Marker Type**: {marker_type}

## Markers
""" + "\n".join(f"- `{m['timecode']}` {m['name']}" for m in markers) + f"""

## Output
Saved to: `{output_path}`
""" + _transcript_cap_notice(transcript_dropped) + _marker_cap_notice(dropped))


# ----- CONNECTED CLIPS & COMPOUND CLIPS HANDLERS (v0.5.0) -----

async def handle_list_connected_clips(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])

    lane_filter = arguments.get("lane")
    clips = tl.connected_clips
    if lane_filter is not None:
        clips = [c for c in clips if c.lane == lane_filter]

    if not clips:
        return _text_result("No connected clips found in timeline.")

    result = f"# Connected Clips in {tl.name}\n\n**Total**: {len(clips)}\n\n"
    result += "| # | Name | Lane | Type | Duration | Parent | Role |\n"
    result += "|---|------|------|------|----------|--------|------|\n"
    for i, c in enumerate(clips, 1):
        result += (
            f"| {i} | {c.name} | {c.lane} | {c.clip_type} | "
            f"{format_duration(c.duration_seconds)} | {c.parent_clip_name} | "
            f"{c.role or '-'} |\n"
        )
    return _text_result(result)


async def handle_add_connected_clip(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    modifier.add_connected_clip(
        parent_clip_id=arguments["parent_clip_id"],
        asset_id=arguments.get("asset_id"),
        asset_name=arguments.get("asset_name"),
        offset=arguments.get("offset", "0s"),
        duration=arguments.get("duration"),
        lane=arguments.get("lane", 1),
    )
    modifier.save(output_path)
    return _text_result((
        f"Connected clip added to '{arguments['parent_clip_id']}' on lane {arguments.get('lane', 1)}\n\n"
        f"Saved to: `{output_path}`"
    ))


async def handle_list_compound_clips(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])

    if not tl.compound_clips:
        return _text_result("No compound clips found in timeline.")

    result = f"# Compound Clips in {tl.name}\n\n"
    for i, cc in enumerate(tl.compound_clips, 1):
        result += f"### {i}. {cc.name}\n"
        result += f"- **Ref ID**: {cc.ref_id}\n"
        result += f"- **Duration**: {format_duration(cc.duration_seconds)}\n"
        result += f"- **Clips inside**: {len(cc.clips)}\n\n"
    return _text_result(result)


# ----- ROLES HANDLERS (v0.5.0) -----

async def handle_list_roles(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])

    audio_roles: dict[str, int] = {}
    video_roles: dict[str, int] = {}

    for clip in tl.clips:
        if clip.audio_role:
            audio_roles[clip.audio_role] = audio_roles.get(clip.audio_role, 0) + 1
        if clip.video_role:
            video_roles[clip.video_role] = video_roles.get(clip.video_role, 0) + 1

    for cc in tl.connected_clips:
        if cc.role:
            # Determine type from clip_type
            if cc.clip_type in ('audio', 'audio-clip'):
                audio_roles[cc.role] = audio_roles.get(cc.role, 0) + 1
            else:
                video_roles[cc.role] = video_roles.get(cc.role, 0) + 1

    result = f"# Roles in {tl.name}\n\n"
    if audio_roles:
        result += "## Audio Roles\n\n| Role | Clips |\n|------|-------|\n"
        for role, count in sorted(audio_roles.items()):
            result += f"| {role} | {count} |\n"
    else:
        result += "## Audio Roles\n\nNo audio roles assigned.\n"

    result += "\n"
    if video_roles:
        result += "## Video Roles\n\n| Role | Clips |\n|------|-------|\n"
        for role, count in sorted(video_roles.items()):
            result += f"| {role} | {count} |\n"
    else:
        result += "## Video Roles\n\nNo video roles assigned.\n"

    return _text_result(result)


async def handle_assign_role(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments)
    modifier.assign_role(
        clip_id=arguments["clip_id"],
        audio_role=arguments.get("audio_role"),
        video_role=arguments.get("video_role"),
    )
    modifier.save(output_path)

    roles_set = []
    if arguments.get("audio_role"):
        roles_set.append(f"audioRole={arguments['audio_role']}")
    if arguments.get("video_role"):
        roles_set.append(f"videoRole={arguments['video_role']}")

    return _text_result((
        f"Set {', '.join(roles_set)} on '{arguments['clip_id']}'\n\n"
        f"Saved to: `{output_path}`"
    ))


async def handle_filter_by_role(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])

    role = arguments["role"].lower()
    role_type = arguments.get("role_type", "any")
    matches = []

    for clip in tl.clips:
        if role_type in ("audio", "any") and clip.audio_role.lower() == role:
            matches.append((clip.name, "audio", clip.audio_role, format_duration(clip.duration_seconds)))
        if role_type in ("video", "any") and clip.video_role.lower() == role:
            matches.append((clip.name, "video", clip.video_role, format_duration(clip.duration_seconds)))

    if not matches:
        return _text_result(f"No clips found with role '{role}'.")

    result = f"# Clips with role '{role}'\n\n"
    result += "| Clip | Type | Role | Duration |\n|------|------|------|----------|\n"
    for name, rtype, rval, dur in matches:
        result += f"| {name} | {rtype} | {rval} | {dur} |\n"
    return _text_result(result)


async def handle_export_role_stems(arguments: dict) -> Sequence[TextContent]:
    project, tl = _require_timeline(arguments["filepath"])

    stems: dict[str, list] = {}
    for clip in tl.clips:
        role = clip.audio_role or "unassigned"
        stems.setdefault(role, []).append(clip)

    for cc in tl.connected_clips:
        role = cc.role or "unassigned"
        stems.setdefault(role, []).append(cc)

    result = f"# Audio Stem Plan for {tl.name}\n\n"
    for role, clips in sorted(stems.items()):
        total_dur = sum(c.duration_seconds for c in clips)
        plural = "clip" if len(clips) == 1 else "clips"
        result += f"## {role.title()} ({len(clips)} {plural}, {format_duration(total_dur)})\n\n"
        for c in clips:
            result += f"- {c.name} ({format_duration(c.duration_seconds)})\n"
        result += "\n"

    return _text_result(result)


# ----- TIMELINE DIFF HANDLER (v0.5.0) -----

async def handle_diff_timelines(arguments: dict) -> Sequence[TextContent]:
    filepath_a = _validate_filepath(arguments["filepath_a"], ('.fcpxml', '.fcpxmld'))
    filepath_b = _validate_filepath(arguments["filepath_b"], ('.fcpxml', '.fcpxmld'))

    # Formatting lives in fcpxml.diff so watch_pull renders diffs identically.
    return _text_result(format_diff(compare_timelines(filepath_a, filepath_b)))


# ----- SOCIAL MEDIA REFORMAT HANDLER (v0.5.0) -----

async def handle_reformat_timeline(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path = _resolve_io_paths(arguments, "_reformatted")

    fmt = arguments["format"]
    if fmt == "custom":
        width = arguments.get("width")
        height = arguments.get("height")
        if not width or not height:
            return _text_result("Custom format requires both 'width' and 'height' parameters.")
    else:
        formats = FCPXMLModifier.SOCIAL_FORMATS
        if fmt not in formats:
            return _text_result(f"Unknown format: {fmt}. Valid: {', '.join(formats.keys())}")
        width, height = formats[fmt]

    modifier = FCPXMLModifier(filepath)
    modifier.reformat_resolution(width, height)
    modifier.save(output_path)

    return _text_result((
        f"# Timeline Reformatted\n\n"
        f"- **Format**: {fmt} ({width}x{height})\n"
        f"- **Aspect ratio**: {width}:{height}\n\n"
        f"Saved to: `{output_path}`\n\n"
        f"**Next step**: Import into FCP (File > Import > XML). "
        f"FCP will handle spatial conforming automatically."
    ))


# ----- SILENCE DETECTION HANDLERS (v0.5.0) -----

def _silence_cached(media_path: str, noise_db: float, min_silence: float):
    """``detect_silence`` through the index. Same answer with the index off."""
    kind = f"silence@{noise_db}dB/{min_silence}s"
    ix = _index.Index.open()
    if ix is None:
        return detect_silence(media_path, noise_db=noise_db, min_duration=min_silence)
    with ix:
        rows = ix.get_analysis(media_path, kind)
        if rows is not None:
            return [(float(r["start"]), float(r["end"])) for r in rows]
        silences = detect_silence(media_path, noise_db=noise_db, min_duration=min_silence)
        if silences is not None:
            ix.put_analysis(
                media_path, kind, [{"start": a, "end": b, "payload": None} for a, b in silences]
            )
        return silences


def _beats_cached(media_path: str):
    """``detect_beats`` through the index. Same answer with the index off."""
    ix = _index.Index.open()
    if ix is None:
        return detect_beats(media_path)
    with ix:
        rows = ix.get_analysis(media_path, "beat")
        if rows is not None and rows:
            return {
                "bpm": float(rows[0]["payload"]["bpm"]),
                "beats": [float(r["start"]) for r in rows],
            }
        result = detect_beats(media_path)
        if result is not None:
            bpm = result["bpm"]
            ix.put_analysis(
                media_path, "beat",
                [{"start": b, "end": b, "payload": {"bpm": bpm}} for b in result["beats"]],
            )
        return result


async def handle_detect_media_silence(arguments: dict) -> Sequence[TextContent]:
    noise_db = float(arguments.get("noise_db", -30.0))
    min_silence = float(arguments.get("min_silence", 0.5))
    # Same bounds detect_silence() enforces — validated here so a bad request
    # fails before any media file is opened.
    if not (-120.0 <= noise_db <= 0.0):
        raise ValueError(f"noise_db must be between -120 and 0 dB, got {noise_db}")
    if not (0 < min_silence <= 3600):
        raise ValueError(f"min_silence must be between 0 and 3600 seconds, got {min_silence}")

    _, tl = _require_timeline(arguments["filepath"])
    clip_filter = arguments.get("clip_name")

    max_media_probes = 100
    findings: list[tuple[str, float, float]] = []
    skipped: list[tuple[str, str]] = []
    probe_cache: dict[str, list | None] = {}
    prog = _progress.start(total=len(tl.clips))
    for clip in tl.clips:
        await prog.step(f"silence: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path not in probe_cache:
            if len(probe_cache) >= max_media_probes:
                skipped.append((clip.name, f"probe cap reached ({max_media_probes} media files)"))
                continue
            probe_cache[media_path] = _silence_cached(media_path, noise_db, min_silence)
        silences = probe_cache[media_path]
        if silences is None:
            skipped.append((clip.name, "unanalyzable (ffmpeg missing or media unreadable)"))
            continue
        source_start = clip.source_start.seconds if clip.source_start else 0.0
        mapped = map_silence_to_timeline(
            silences, source_start, clip.duration.seconds, clip.start.seconds
        )
        findings.extend((clip.name, start, end) for start, end in mapped)

    total_silence = sum(end - start for _, start, end in findings)
    result = f"""# Media Silence Detection (real audio analysis)

## Summary
- **Threshold**: {noise_db} dB for >= {min_silence}s
- **Media Files Probed**: {len(probe_cache)}
- **Silence Spans Found**: {len(findings)} ({format_duration(total_silence)} total)
"""
    if findings:
        result += "\n## Silence Spans (timeline time)\n"
        result += _markdown_table(
            ["Clip", "Start", "End", "Duration"],
            [[name, f"{start:.2f}s", f"{end:.2f}s", f"{end - start:.2f}s"]
             for name, start, end in findings],
        ) + "\n"
        result += "\n*To remove: `split_clip` at each boundary, then `delete_clips` with ripple.*"
    if skipped:
        result += "\n## Skipped Clips\n"
        result += _markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    if not findings and not skipped:
        result += "\nNo silence detected in any clip's source audio."
    return _text_result(result)


async def handle_remove_media_silence(arguments: dict) -> Sequence[TextContent]:
    noise_db = float(arguments.get("noise_db", -30.0))
    min_silence = float(arguments.get("min_silence", 0.5))
    padding = float(arguments.get("padding", 0.05))
    if not (-120.0 <= noise_db <= 0.0):
        raise ValueError(f"noise_db must be between -120 and 0 dB, got {noise_db}")
    if not (0 < min_silence <= 3600):
        raise ValueError(f"min_silence must be between 0 and 3600 seconds, got {min_silence}")
    if not (0 <= padding <= 5):
        raise ValueError(f"padding must be between 0 and 5 seconds, got {padding}")

    filepath, output_path, modifier = _setup_modifier(arguments, "_silence_removed")
    clip_filter = arguments.get("clip_name")
    fps = modifier._detect_fps()

    def to_frame_timevalue(seconds: float) -> TimeValue:
        # Snap cut boundaries to the frame grid in the 2400-tick timebase so
        # output stays frame-aligned and DTD-friendly.
        return TimeValue(int(round(seconds * fps) * round(2400 / fps)), 2400)

    max_media_probes = 100
    cuts_made: list[tuple[str, int, float]] = []
    skipped: list[tuple[str, str]] = []
    probe_cache: dict[str, list | None] = {}
    spine_clips = [el for _, el in modifier._iter_spine_clips()]
    for el in spine_clips:
        name = el.get("name", "")
        if clip_filter and name != clip_filter:
            continue
        src = modifier.resources.get(el.get("ref", ""), {}).get("src", "")
        media_path = media_src_to_path(src)
        if not media_path or not Path(media_path).is_file():
            skipped.append((name, "media file missing"))
            continue
        if media_path not in probe_cache:
            if len(probe_cache) >= max_media_probes:
                skipped.append((name, f"probe cap reached ({max_media_probes} media files)"))
                continue
            probe_cache[media_path] = _silence_cached(media_path, noise_db, min_silence)
        silences = probe_cache[media_path]
        if silences is None:
            skipped.append((name, "unanalyzable (ffmpeg missing or media unreadable)"))
            continue

        clip_source_start = modifier._parse_time(el.get("start", "0s")).to_seconds()
        clip_duration = modifier._parse_time(el.get("duration", "0s")).to_seconds()
        cut_ranges = []
        for sil_start, sil_end in silences:
            # Source time -> clip-relative, padded so cuts breathe.
            cut_start = max(sil_start, clip_source_start) - clip_source_start + padding
            cut_end = min(sil_end, clip_source_start + clip_duration) - clip_source_start - padding
            if cut_end > cut_start:
                cut_ranges.append((to_frame_timevalue(cut_start), to_frame_timevalue(cut_end)))
        if not cut_ranges:
            continue
        removed = modifier.cut_clip_ranges(el, cut_ranges)
        if removed > TimeValue.zero():
            cuts_made.append((name, len(cut_ranges), removed.to_seconds()))

    if not cuts_made:
        text = "# Media Silence Removal\n\nNo silence found to remove — file unchanged (nothing saved)."
        if skipped:
            text += "\n\n## Skipped Clips\n" + _markdown_table(
                ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
            )
        return _text_result(text)

    modifier.save(output_path)
    total_removed = sum(seconds for _, _, seconds in cuts_made)
    result = f"""# Media Silence Removal (real audio analysis)

## Summary
- **Threshold**: {noise_db} dB for >= {min_silence}s, padding {padding}s
- **Clips Cut**: {len(cuts_made)}
- **Total Removed**: {format_duration(total_removed)}

## Cuts
"""
    result += _markdown_table(
        ["Clip", "Silence Spans Cut", "Removed"],
        [[name, str(count), f"{seconds:.2f}s"] for name, count, seconds in cuts_made],
    ) + "\n"
    if skipped:
        result += "\n## Skipped Clips\n" + _markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    result += f"\nSaved to: {output_path}\n\n*Preview first next time with `detect_media_silence`. Original file untouched.*"
    return _text_result(result)


AUDIO_MEDIA_EXTENSIONS = (
    '.wav', '.aif', '.aiff', '.mp3', '.m4a', '.aac', '.flac', '.mov', '.mp4',
)


async def handle_detect_beats(arguments: dict) -> Sequence[TextContent]:
    media_path = _validate_filepath(arguments["media_path"], AUDIO_MEDIA_EXTENSIONS)

    result = _beats_cached(media_path)
    if result is None:
        return _text_result(
            "Beat detection unavailable — librosa is not installed or the file "
            "could not be analyzed.\n\nInstall the optional media-intelligence "
            "extra:\n\n    pip install 'fcp-mcp-server[intelligence]'"
        )

    bpm, beats = result["bpm"], result["beats"]
    beats_data = {
        "source": str(Path(media_path).name),
        "bpm": round(bpm, 2),
        "beats": [round(b, 4) for b in beats],
        "downbeats": [round(b, 4) for b in beats[::4]],
    }
    json_path = _validate_output_path(
        str(Path(media_path).with_name(Path(media_path).stem + "_beats.json")),
        anchor_dir=str(Path(media_path).parent),
    )
    with open(json_path, "w") as f:
        json.dump(beats_data, f, indent=2)

    preview = beats[:16]
    result_text = f"""# Beat Detection

## Summary
- **Source**: {Path(media_path).name}
- **Estimated Tempo**: {bpm:.1f} BPM
- **Beats Detected**: {len(beats)} ({format_duration(beats[-1]) if beats else '0s'} span)
- **Beats JSON**: {json_path}

## First Beats
"""
    result_text += _markdown_table(
        ["#", "Time"],
        [[str(i + 1), f"{b:.3f}s"] for i, b in enumerate(preview)],
    ) + "\n"
    result_text += (
        f"\n*Next: `import_beat_markers` with beats_path=\"{json_path}\" to place "
        "markers, then `snap_to_beats` to align your cuts.*"
    )
    return _text_result(result_text)


# ===== TRANSCRIPT INTELLIGENCE (v0.13.1) =====

TRANSCRIBE_MAX_MEDIA = 10
TRANSCRIBE_BACKENDS = BACKENDS
_EGRESS_NOTE = (
    "- **Audio left this machine**: sent to api.elevenlabs.io (Scribe) for "
    "speakers and audio events. The local backend never does this.\n"
)

_TRANSCRIBE_INSTALL_HINT = (
    "\n\nInstall the optional transcription extra:\n\n"
    "    pip install 'fcp-mcp-server[transcribe]'\n\n"
    "or run via uvx:\n\n"
    "    uvx --from \"fcp-mcp-server[transcribe]\" fcp-mcp-server"
)


def _transcript_json_path(media_path: str) -> Path:
    p = Path(media_path)
    return p.with_name(p.stem + "_transcript.json")


def _backend_arg(arguments: dict) -> str:
    backend = arguments.get("backend", "local")
    if backend not in TRANSCRIBE_BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(TRANSCRIBE_BACKENDS)}, got {backend!r}")
    return backend


def _cache_satisfies(data: dict, backend: str) -> bool:
    """A local transcript answers a local request. A request for speakers is
    only answered by a transcript that has them."""
    return backend == "local" or is_diarized(data)


def _load_or_transcribe(
    media_path: str, model: str, language: str | None, backend: str = "local",
) -> tuple[dict | None, str]:
    """Load a cached ``_transcript.json`` for a media file, else transcribe and cache it.

    Returns ``(transcript, "")`` or ``(None, reason)``. The cache makes
    transcription a one-time cost per media file across all transcript tools.
    """
    json_path = _transcript_json_path(media_path)
    if json_path.is_file():
        try:
            with open(json_path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("words"), list) \
                    and _cache_satisfies(data, backend):
                return data, ""
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass  # unreadable cache falls through to re-transcribe
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            data = ix.get_transcript(media_path)
        if data is not None and _cache_satisfies(data, backend):
            return data, ""
    result = transcribe(media_path, model_size=model, language=language, backend=backend)
    if result is None:
        if backend == "elevenlabs":
            if not os.environ.get(SCRIBE_KEY_ENV, "").strip():
                return None, f"elevenlabs backend needs {SCRIBE_KEY_ENV} set in the server's environment"
            return None, "untranscribable (api.elevenlabs.io request failed or media unreadable)"
        return None, "untranscribable (faster-whisper not installed or media unreadable)"
    out_path = _validate_output_path(str(json_path), anchor_dir=str(Path(media_path).parent))
    with open(out_path, "w") as f:
        json.dump({"source": Path(media_path).name, **result}, f, indent=2)
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            ix.put_transcript(media_path, result)
    return result, ""


def _cut_transcript_spans(modifier, clip_filter, model, language, padding, spans_fn, keep_only=False,
                          backend="local"):
    """Shared cut engine for transcript-driven editing.

    ``spans_fn(words) -> [(start, end), ...]`` in source seconds. Spans are
    padded, clamped to each clip's used source window, optionally inverted
    (keep_only), snapped to the frame grid, and cut with ripple.
    """
    fps = modifier._detect_fps()
    tick = round(2400 / fps)

    def to_frame(seconds: float) -> TimeValue:
        return TimeValue(int(round(seconds * fps) * tick), 2400)

    cache: dict[str, tuple] = {}
    cuts_made: list[tuple[str, int, float]] = []
    skipped: list[tuple[str, str]] = []
    spine_clips = [el for _, el in modifier._iter_spine_clips()]
    for el in spine_clips:
        name = el.get("name", "")
        if clip_filter and name != clip_filter:
            continue
        src = modifier.resources.get(el.get("ref", ""), {}).get("src", "")
        media_path = media_src_to_path(src)
        if not media_path or not Path(media_path).is_file():
            skipped.append((name, "media file missing"))
            continue
        if media_path not in cache:
            if len(cache) >= TRANSCRIBE_MAX_MEDIA:
                skipped.append((name, f"transcription cap reached ({TRANSCRIBE_MAX_MEDIA} media files)"))
                continue
            cache[media_path] = _load_or_transcribe(media_path, model, language, backend)
        data, reason = cache[media_path]
        if data is None:
            skipped.append((name, reason))
            continue

        clip_source_start = modifier._parse_time(el.get("start", "0s")).to_seconds()
        clip_duration = modifier._parse_time(el.get("duration", "0s")).to_seconds()
        window_start = clip_source_start
        window_end = clip_source_start + clip_duration

        spans = spans_fn(data.get("words", []))
        padded = merge_ranges([(s - padding, e + padding) for s, e in spans])
        clamped = [
            (max(s, window_start), min(e, window_end))
            for s, e in padded
            if min(e, window_end) > max(s, window_start)
        ]
        if keep_only:
            if not clamped:
                # Never delete a whole clip just because nothing matched in it.
                skipped.append((name, "no phrase matches — left untouched (keep_only)"))
                continue
            cut_source = invert_ranges(clamped, window_start, window_end)
        else:
            cut_source = clamped
        cut_ranges = [
            (to_frame(s - clip_source_start), to_frame(e - clip_source_start))
            for s, e in cut_source
        ]
        cut_ranges = [(a, b) for a, b in cut_ranges if b > a]
        if not cut_ranges:
            continue
        removed = modifier.cut_clip_ranges(el, cut_ranges)
        if removed > TimeValue.zero():
            cuts_made.append((name, len(cut_ranges), removed.to_seconds()))
    return cuts_made, skipped


def _transcript_cut_report(title, summary_lines, cuts_made, skipped, output_path, footer):
    if not cuts_made:
        text = f"# {title}\n\nNo cuts to make — file unchanged (nothing saved)."
        if skipped:
            text += "\n\n## Skipped Clips\n" + _markdown_table(
                ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
            )
        if any("faster-whisper" in reason for _, reason in skipped):
            text += _TRANSCRIBE_INSTALL_HINT
        return _text_result(text)
    total_removed = sum(seconds for _, _, seconds in cuts_made)
    result = f"# {title}\n\n## Summary\n"
    result += "\n".join(summary_lines) + "\n"
    result += f"- **Clips Cut**: {len(cuts_made)}\n- **Total Removed**: {format_duration(total_removed)}\n"
    result += "\n## Cuts\n"
    result += _markdown_table(
        ["Clip", "Ranges Cut", "Removed"],
        [[name, str(count), f"{seconds:.2f}s"] for name, count, seconds in cuts_made],
    ) + "\n"
    if skipped:
        result += "\n## Skipped Clips\n" + _markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    result += f"\nSaved to: {output_path}\n\n{footer}"
    return _text_result(result)


async def handle_transcribe_media(arguments: dict) -> Sequence[TextContent]:
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)
    write_srt = bool(arguments.get("write_srt", False))
    _, tl = _require_timeline(arguments["filepath"])
    clip_filter = arguments.get("clip_name")

    done: dict[str, dict | None] = {}
    skipped: list[tuple[str, str]] = []
    rows: list[list[str]] = []
    srt_paths: list[str] = []
    prog = _progress.start(total=len(tl.clips))
    for clip in tl.clips:
        await prog.step(f"transcribe: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path in done:
            continue
        if len(done) >= TRANSCRIBE_MAX_MEDIA:
            skipped.append((clip.name, f"transcription cap reached ({TRANSCRIBE_MAX_MEDIA} media files)"))
            continue
        data, reason = _load_or_transcribe(media_path, model, language, backend)
        done[media_path] = data
        if data is None:
            skipped.append((clip.name, reason))
            continue
        if write_srt and data.get("segments"):
            srt_path = _validate_output_path(
                str(Path(media_path).with_name(Path(media_path).stem + "_transcript.srt")),
                anchor_dir=str(Path(media_path).parent),
            )
            with open(srt_path, "w") as f:
                f.write(segments_to_srt(data["segments"]))
            srt_paths.append(srt_path)
        preview = data.get("text", "")[:160]
        rows.append([
            Path(media_path).name,
            data.get("language", "?"),
            str(len(data.get("words", []))),
            format_duration(float(data.get("duration", 0.0))),
            preview + ("…" if len(data.get("text", "")) > 160 else ""),
        ])

    title = "ElevenLabs Scribe" if backend == "elevenlabs" else "local Whisper"
    result = f"""# Media Transcription ({title})

## Summary
- **Model**: {SCRIBE_MODEL if backend == "elevenlabs" else model}
- **Media Files Transcribed**: {len(rows)}
"""
    if backend == "elevenlabs" and rows:
        result += _EGRESS_NOTE
    if rows:
        result += "\n## Transcripts (saved as _transcript.json next to each media file)\n"
        result += _markdown_table(
            ["Media", "Language", "Words", "Duration", "Preview"], rows
        ) + "\n"
        result += (
            "\n*Next: `edit_by_transcript` to cut by what was said, or "
            "`remove_filler_words` to clean ums/uhs. Transcripts are cached — "
            "media is only transcribed once.*"
        )
    if srt_paths:
        result += "\n\n## SRT Files\n" + "\n".join(f"- {p}" for p in srt_paths)
    if skipped:
        result += "\n## Skipped Clips\n" + _markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    if not rows and any("faster-whisper" in reason for _, reason in skipped):
        result += _TRANSCRIBE_INSTALL_HINT
    return _text_result(result)


async def handle_edit_by_transcript(arguments: dict) -> Sequence[TextContent]:
    phrases = arguments.get("phrases") or []
    if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
        raise ValueError("phrases must be a list of strings")
    phrases = [p for p in phrases if p.strip()]
    if not phrases:
        raise ValueError("phrases must contain at least one non-empty string")
    mode = arguments.get("mode", "remove")
    if mode not in ("remove", "keep_only"):
        raise ValueError(f"mode must be 'remove' or 'keep_only', got {mode!r}")
    padding = float(arguments.get("padding", 0.0))
    if not (0 <= padding <= 2):
        raise ValueError(f"padding must be between 0 and 2 seconds, got {padding}")
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)

    filepath, output_path, modifier = _setup_modifier(arguments, "_transcript_edit")

    def spans_fn(words):
        return merge_ranges(
            [span for phrase in phrases for span in find_phrase_spans(words, phrase)]
        )

    cuts_made, skipped = _cut_transcript_spans(
        modifier, arguments.get("clip_name"), model, language, padding,
        spans_fn, keep_only=(mode == "keep_only"), backend=backend,
    )
    if cuts_made:
        modifier.save(output_path)
    verb = "kept only" if mode == "keep_only" else "removed"
    return _transcript_cut_report(
        "Transcript Edit",
        [f"- **Mode**: {mode} ({verb} the matched phrases)",
         f"- **Phrases**: {', '.join(repr(p) for p in phrases)}",
         f"- **Padding**: {padding}s"],
        cuts_made, skipped, output_path,
        "*Transcripts are cached as _transcript.json. Original file untouched.*",
    )


async def handle_transcript_pack(arguments: dict) -> Sequence[TextContent]:
    filepath = arguments["filepath"]
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)
    gap = float(arguments.get("gap", _tpack.DEFAULT_GAP))
    if not 0.1 <= gap <= 5.0:
        raise ValueError("gap must be between 0.1 and 5 seconds")
    write = bool(arguments.get("write", False))
    _, tl = _require_timeline(filepath)
    clip_filter = arguments.get("clip_name")

    sources: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []
    prog = _progress.start(total=len(tl.clips))
    for clip in tl.clips:
        await prog.step(f"pack: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path in sources:
            continue
        if len(sources) >= TRANSCRIBE_MAX_MEDIA:
            skipped.append((clip.name, f"transcription cap reached ({TRANSCRIBE_MAX_MEDIA} media files)"))
            continue
        data, reason = _load_or_transcribe(media_path, model, language, backend)
        if data is None:
            skipped.append((clip.name, reason))
            continue
        sources[media_path] = {
            "name": Path(media_path).name,
            "words": data.get("words", []),
            "events": data.get("events", []),
        }

    full = _tpack.pack(list(sources.values()), gap=gap)
    size = _tpack.pack_size(full)
    written = ""
    if write and sources:
        fp = Path(_validate_filepath(filepath, ('.fcpxml', '.fcpxmld')))
        out = _validate_output_path(
            str(fp.with_name(fp.stem + "_pack.md")), anchor_dir=str(fp.parent),
        )
        with open(out, "w", encoding="utf-8") as f:
            f.write(full)
        written = out

    result = (
        "# Transcript Pack\n\n"
        f"- **Sources**: {len(sources)}\n"
        f"- **Utterances**: {sum(1 for ln in full.splitlines() if ln.startswith('['))}\n"
        f"- **Size**: {size} bytes"
        + (f" (shown truncated to {_tpack.PACK_LIMIT_BYTES})" if size > _tpack.PACK_LIMIT_BYTES else "")
        + "\n"
    )
    if written:
        result += f"- **Written**: {written}\n"
    if backend == "elevenlabs" and sources:
        result += _EGRESS_NOTE
    if sources:
        result += "\n" + _tpack.truncate(full, limit=_tpack.PACK_LIMIT_BYTES)
    if skipped:
        result += "\n## Skipped Clips\n" + _markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    if not sources and any("faster-whisper" in reason for _, reason in skipped):
        result += _TRANSCRIBE_INSTALL_HINT
    return _text_result(result)


async def handle_remove_filler_words(arguments: dict) -> Sequence[TextContent]:
    fillers = arguments.get("fillers") or list(DEFAULT_FILLERS)
    if not isinstance(fillers, list) or not all(isinstance(f, str) for f in fillers):
        raise ValueError("fillers must be a list of strings")
    padding = float(arguments.get("padding", 0.02))
    if not (0 <= padding <= 2):
        raise ValueError(f"padding must be between 0 and 2 seconds, got {padding}")
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)

    filepath, output_path, modifier = _setup_modifier(arguments, "_defillered")

    cuts_made, skipped = _cut_transcript_spans(
        modifier, arguments.get("clip_name"), model, language, padding,
        lambda words: merge_ranges(find_filler_spans(words, fillers)),
        backend=backend,
    )
    if cuts_made:
        modifier.save(output_path)
    return _transcript_cut_report(
        "Filler Word Removal",
        [f"- **Fillers**: {', '.join(fillers)}", f"- **Padding**: {padding}s"],
        cuts_made, skipped, output_path,
        "*Transcripts are cached as _transcript.json. Original file untouched.*",
    )


async def handle_detect_silence_candidates(arguments: dict) -> Sequence[TextContent]:
    filepath = _validate_filepath(arguments["filepath"], ('.fcpxml', '.fcpxmld'))
    modifier = FCPXMLModifier(filepath)
    candidates = modifier.detect_silence_candidates(
        min_gap_seconds=arguments.get("min_gap_seconds", 0.5),
        patterns=arguments.get("patterns"),
    )

    if not candidates:
        return _text_result("No silence candidates detected.")

    result = f"# Silence Candidates Detected\n\n**Found**: {len(candidates)}\n\n"
    result += "| # | Timecode | Duration | Reason | Confidence | Clip |\n"
    result += "|---|----------|----------|--------|------------|------|\n"
    for i, c in enumerate(candidates, 1):
        result += (
            f"| {i} | {c['start_timecode']} | {format_duration(c['duration_seconds'])} | "
            f"{c['reason']} | {c['confidence']:.0%} | {c.get('clip_name') or '-'} |\n"
        )
    result += (
        "\n**Note**: Detection uses timeline heuristics (gaps, ultra-short clips, name patterns). "
        "Review candidates before removing — some may be intentional."
    )
    return _text_result(result)


async def handle_remove_silence_candidates(arguments: dict) -> Sequence[TextContent]:
    filepath, output_path, modifier = _setup_modifier(arguments, "_silence_cleaned")
    actions = modifier.remove_silence_candidates(
        mode=arguments.get("mode", "mark"),
        min_gap_seconds=arguments.get("min_gap_seconds", 0.5),
        min_confidence=arguments.get("min_confidence", 0.7),
    )
    modifier.save(output_path)

    if not actions:
        return _text_result("No silence candidates met the confidence threshold.")

    mode = arguments.get("mode", "mark")
    result = f"# Silence Candidates {'Marked' if mode == 'mark' else 'Removed'}\n\n"
    result += f"**Actions taken**: {len(actions)}\n\n"
    for a in actions:
        result += f"- **{a['action']}** {a.get('clip_name', 'gap')} ({a['reason']})\n"
    result += f"\nSaved to: `{output_path}`"
    return _text_result(result)


# ----- NLE EXPORT / EFFECTS / TEMPLATES / RELINK -----
#
# Moved to tools/nle.py. Re-exported here under the original names so
# TOOL_HANDLERS, the flat tool list and existing callers keep resolving one
# definition rather than two that can drift apart.
from tools.nle import (  # noqa: E402
    handle_add_audio,
    handle_apply_template,
    handle_create_compound_clip,
    handle_export_fcp7_xml,
    handle_export_resolve_xml,
    handle_flatten_compound_clip,
    handle_list_effects,
    handle_list_templates,
    handle_relink_media,
)


async def handle_push_to_fcp(arguments: dict) -> Sequence[TextContent]:
    from fcpxml.live import push_to_fcp

    filepath = _validate_filepath(arguments["filepath"], ('.fcpxml', '.fcpxmld'))

    # Flat files get an options-injected sibling copy (never touch the
    # original); the copy path goes through the same write sandbox as
    # every other derived output.
    import_copy = None
    if Path(filepath).suffix.lower() == '.fcpxml':
        anchor = str(Path(filepath).resolve().parent)
        import_copy = _validate_output_path(
            generate_output_path(filepath, "_import"), anchor_dir=anchor
        )

    result = push_to_fcp(
        filepath,
        library_location=arguments.get("library_location"),
        suppress_warnings=arguments.get("suppress_warnings", True),
        copy_assets=arguments.get("copy_assets"),
        import_copy_path=import_copy,
    )
    lines = [
        f"Sent to Final Cut Pro: {result['sent']}",
        f"FCP {'was launched' if result['launched_fcp'] else 'was already running'} — "
        f"import happens in-app (libraries/events are created or merged per import-options).",
    ]
    if arguments.get("library_location"):
        lines.append(f"Target library: {arguments['library_location']}")
    lines.append(
        "Note: Apple offers no programmatic export — to round-trip edits "
        "back, use File > Export XML in FCP."
    )
    return _text_result("\n".join(lines))


async def handle_list_fcp_libraries(arguments: dict) -> Sequence[TextContent]:
    from fcpxml.live import list_fcp_libraries

    try:
        libraries = list_fcp_libraries(
            allow_launch=arguments.get("allow_launch", False)
        )
    except RuntimeError as exc:
        return _text_result(str(exc))

    if not libraries:
        return _text_result("Final Cut Pro is running but reports no open libraries.")

    lines = [f"Open libraries in Final Cut Pro ({len(libraries)}):", ""]
    for lib in libraries:
        lines.append(f"📚 {lib['name']}")
        for event in lib["events"]:
            lines.append(f"  └─ {event['name']}")
            for proj in event["projects"]:
                lines.append(f"      • {proj}")
    return _text_result("\n".join(lines))


# ============================================================================
# TOOL DISPATCH
# ============================================================================

# Anything not in this set turns autopush ON. An UNSET variable reads as "",
# which is in the set, so the default is OFF — repeated imports accumulate
# library churn in Final Cut Pro, and that is the operator's call to make
# rather than a default to inflict on them.
_AUTOPUSH_OFF = {"", "0", "false", "no", "off"}


def _autopush_enabled() -> bool:
    """Whether every write should also land in the running Final Cut Pro."""
    return os.environ.get("FCP_MCP_AUTOPUSH", "").strip().lower() not in _AUTOPUSH_OFF


def _maybe_autopush(output_path: str) -> str:
    """Push *output_path* into FCP when autopush is on. Returns a report line.

    Never raises. The edit already succeeded and is on disk; a failed push is a
    note about the PUSH, not a failure of the edit, and turning it into an
    exception would lose the operator a file they already have.
    """
    if not _autopush_enabled():
        return ""
    try:
        result = live.push_to_fcp(output_path)
    except (RuntimeError, OSError) as exc:
        return f"\nAutopush: not pushed — {exc}"
    launched = " (launched Final Cut Pro)" if result.get("launched_fcp") else ""
    return f"\nPushed to Final Cut Pro: {result.get('sent', output_path)}{launched}"


async def handle_import_edl_json(arguments: dict) -> Sequence[TextContent]:
    """Author an FCPXML timeline from a video-use style edl.json cut list.

    The bridge that lets an agentic editing pipeline finish in Final Cut Pro
    instead of dead-ending at a flat mp4.
    """
    import json

    from fcpxml.edl import EDLValidationError, edl_to_fcpxml

    filepath = arguments.get("filepath")
    if not filepath:
        return _text_result("import_edl_json requires 'filepath' (an edl.json).")
    path = _validate_filepath(filepath, ('.json',))

    output_path = arguments.get("output_path") or str(
        Path(path).with_suffix('.fcpxml')
    )
    output_path = _validate_output_path(output_path, anchor_dir=str(Path(path).parent))

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return _text_result(f"Could not read {path}: {exc}")

    try:
        result = edl_to_fcpxml(
            data, output_path,
            base_dir=str(Path(path).parent),
            fps=float(arguments.get("fps", 24.0)),
            name=arguments.get("name", "EDL Import"),
        )
    except EDLValidationError as exc:
        return _text_result(f"Invalid EDL: {exc}")

    lines = [
        f"Authored {result['path']} from {result['clips']} ranges.",
    ]
    for note in result["ignored"]:
        lines.append(f"Ignored: {note}")
    if result["missing"]:
        lines.append(
            "Media not found on this machine (the timeline still references "
            "it; relink in FCP or with deliver.relink_media): "
            + ", ".join(result["missing"])
        )
    lines.append(
        "Preview it with preview.preview_render, or import with "
        "deliver.push_to_fcp."
    )
    return _text_result("\n".join(lines))


TOOL_HANDLERS = {
    # Read
    "list_projects": handle_list_projects,
    "analyze_timeline": handle_analyze_timeline,
    "list_clips": handle_list_clips,
    "list_markers": handle_list_markers,
    "find_short_cuts": handle_find_short_cuts,
    "find_long_clips": handle_find_long_clips,
    "list_keywords": handle_list_keywords,
    "export_edl": handle_export_edl,
    "export_csv": handle_export_csv,
    "analyze_pacing": handle_analyze_pacing,
    "list_library_clips": handle_list_library_clips,
    # QC
    "detect_flash_frames": handle_detect_flash_frames,
    "detect_duplicates": handle_detect_duplicates,
    "detect_gaps": handle_detect_gaps,
    # Write
    "add_marker": handle_add_marker,
    "batch_add_markers": handle_batch_add_markers,
    "trim_clip": handle_trim_clip,
    "reorder_clips": handle_reorder_clips,
    "add_transition": handle_add_transition,
    "change_speed": handle_change_speed,
    "delete_clips": handle_delete_clips,
    "split_clip": handle_split_clip,
    "insert_clip": handle_insert_clip,
    # Batch Fix
    "fix_flash_frames": handle_fix_flash_frames,
    "rapid_trim": handle_rapid_trim,
    "fill_gaps": handle_fill_gaps,
    "validate_timeline": handle_validate_timeline,
    # Generation
    "auto_rough_cut": handle_auto_rough_cut,
    "import_edl_json": handle_import_edl_json,
    "generate_montage": handle_generate_montage,
    "generate_ab_roll": handle_generate_ab_roll,
    # Beat Sync
    "import_beat_markers": handle_import_beat_markers,
    "snap_to_beats": handle_snap_to_beats,
    # SRT / Transcript
    "import_srt_markers": handle_import_srt_markers,
    "import_transcript_markers": handle_import_transcript_markers,
    # Connected Clips & Compound Clips (v0.5.0)
    "list_connected_clips": handle_list_connected_clips,
    "add_connected_clip": handle_add_connected_clip,
    "list_compound_clips": handle_list_compound_clips,
    # Roles (v0.5.0)
    "list_roles": handle_list_roles,
    "assign_role": handle_assign_role,
    "filter_by_role": handle_filter_by_role,
    "export_role_stems": handle_export_role_stems,
    # Timeline Diff (v0.5.0)
    "diff_timelines": handle_diff_timelines,
    # Social Media Reformat (v0.5.0)
    "reformat_timeline": handle_reformat_timeline,
    # Silence Detection (v0.5.0)
    "detect_media_silence": handle_detect_media_silence,
    "remove_media_silence": handle_remove_media_silence,
    "transcribe_media": handle_transcribe_media,
    "edit_by_transcript": handle_edit_by_transcript,
    "remove_filler_words": handle_remove_filler_words,
    "transcript_pack": handle_transcript_pack,
    "detect_beats": handle_detect_beats,
    "detect_silence_candidates": handle_detect_silence_candidates,
    "remove_silence_candidates": handle_remove_silence_candidates,
    # NLE Export (v0.5.0)
    "export_resolve_xml": handle_export_resolve_xml,
    "export_fcp7_xml": handle_export_fcp7_xml,
    # v0.6.0
    "list_effects": handle_list_effects,
    "add_audio": handle_add_audio,
    "create_compound_clip": handle_create_compound_clip,
    "flatten_compound_clip": handle_flatten_compound_clip,
    "list_templates": handle_list_templates,
    "apply_template": handle_apply_template,
    # v0.8.0
    "relink_media": handle_relink_media,
    # v0.9.0 — Live mode
    "push_to_fcp": handle_push_to_fcp,
    "list_fcp_libraries": handle_list_fcp_libraries,
}


# ============================================================================
# GROUPED TOOL FACADE
#
# 62 flat tools cost ~9,000 tokens of schema in every conversation before the
# user types anything. These groups (inspect, diagnose, edit, mark, generate,
# transcript, deliver here; preview, watch, index, scenes registered from
# tools/) advertise the same capability for a
# fraction of that. They dispatch straight into TOOL_HANDLERS, so behaviour is
# identical and every legacy tool name keeps working whether or not it is
# advertised in list_tools.
# ============================================================================

TOOL_GROUPS: dict[str, dict] = {
    "inspect": {
        "description": (
            "Read a timeline or project without changing it. Use this first to "
            "understand what you are working with."
        ),
        "actions": [
            "list_projects", "analyze_timeline", "analyze_pacing", "list_clips",
            "list_markers", "list_roles", "list_keywords", "list_effects",
            "list_templates", "list_library_clips", "list_compound_clips",
            "list_connected_clips", "filter_by_role",
        ],
    },
    "diagnose": {
        "description": (
            "Find problems in a timeline: gaps, flash frames, duplicates, dead "
            "air, and beat structure. Read-only. Run before editing."
        ),
        "actions": [
            "validate_timeline", "detect_gaps", "detect_duplicates",
            "detect_flash_frames", "detect_silence_candidates",
            "detect_media_silence", "detect_beats", "find_short_cuts",
            "find_long_clips", "diff_timelines",
        ],
    },
    "edit": {
        "description": (
            "Change clips on the timeline: insert, delete, trim, split, reorder, "
            "retime, remove silence, and attach audio, B-roll or transitions. "
            "Writes a new file."
        ),
        "actions": [
            "insert_clip", "delete_clips", "trim_clip", "split_clip",
            "reorder_clips", "change_speed", "rapid_trim", "add_transition",
            "add_audio", "add_connected_clip", "assign_role", "fill_gaps",
            "fix_flash_frames", "remove_silence_candidates", "remove_media_silence",
        ],
    },
    "mark": {
        "description": (
            "Add or import markers and chapters, including from SRT/VTT "
            "subtitles, transcripts, and beat analysis."
        ),
        "actions": [
            "add_marker", "batch_add_markers", "import_srt_markers",
            "import_transcript_markers", "import_beat_markers", "snap_to_beats",
        ],
    },
    "generate": {
        "description": (
            "Build new timeline structure from source clips: rough cuts, "
            "montages, A/B roll, templates and compound clips."
        ),
        "actions": [
            "auto_rough_cut", "generate_ab_roll", "generate_montage",
            "apply_template", "create_compound_clip", "flatten_compound_clip",
            "import_edl_json",
        ],
    },
    "transcript": {
        "description": (
            "Transcribe source media locally and edit the timeline by what was "
            "SAID rather than by timecode. Also removes filler words."
        ),
        "actions": [
            "transcribe_media", "edit_by_transcript", "remove_filler_words",
            "transcript_pack",
        ],
    },
    "deliver": {
        "description": (
            "Get the edit out: export to other NLEs, CSV, EDL and stems, "
            "reformat, relink media, or push straight into a running Final Cut Pro. "
            "Exports and push refuse a cut with no rendered preview of its current "
            "state (pass confirm_unreviewed=true to ship anyway)."
        ),
        "actions": [
            "export_csv", "export_edl", "export_fcp7_xml", "export_resolve_xml",
            "export_role_stems", "reformat_timeline", "relink_media",
            "push_to_fcp", "list_fcp_libraries",
        ],
    },
}


# ---------------------------------------------------------------------------
# tools/ package merge
# ---------------------------------------------------------------------------
# Groups defined in tools/ are folded into TOOL_GROUPS and TOOL_HANDLERS here,
# rather than kept in a second registry that every call site would have to
# consult. One source of truth means list_tools, handle_group, call_tool,
# _action_param_help and the "valid actions" error messages all keep working
# unchanged, and test_every_handler_belongs_to_exactly_one_group keeps its
# meaning.
#
# The import is deliberately down here, not at the top of the file: tools/
# group modules import fcpxml.* and tools._common, never server, but keeping
# the import adjacent to the merge makes the dependency direction obvious.
import sys  # noqa: E402

import tools as _extra_tools  # noqa: E402

# Hand tools/ the live module object rather than letting it `import server`.
# In production this file runs as __main__, so that import would execute a
# second copy under a different name, with its own handler registry and its own
# sandbox state.
_extra_tools.bind_server(sys.modules[__name__])

def _merge_extra_tools(
    groups: dict, handlers: dict, into_groups: dict, into_handlers: dict
) -> None:
    """Fold tools/ groups and handlers into the server's registries.

    A function rather than inline statements so the shadowing guard can be
    tested. A guard nothing exercises is a guard nobody knows is broken.
    """
    for name, spec in groups.items():
        if name in into_groups:
            raise RuntimeError(f"tools/ group shadows a builtin group: {name}")
    for action in handlers:
        if action in into_handlers:
            raise RuntimeError(f"tools/ action shadows a builtin action: {action}")
    into_groups.update(groups)
    into_handlers.update(handlers)


_merge_extra_tools(
    _extra_tools.EXTRA_GROUPS, _extra_tools.EXTRA_HANDLERS,
    TOOL_GROUPS, TOOL_HANDLERS,
)


def _group_action_error(group: str, action: str | None) -> list[TextContent]:
    """Reject an action while telling the caller what it should have used."""
    if group not in TOOL_GROUPS:
        return _text_result(
            f"Unknown tool group: {group}. "
            f"Valid groups: {', '.join(sorted(TOOL_GROUPS))}"
        )
    valid = ", ".join(TOOL_GROUPS[group]["actions"])
    if not action:
        return _text_result(
            f"The '{group}' tool requires an 'action'. Valid actions: {valid}"
        )
    return _text_result(
        f"Unknown action '{action}' for '{group}'. Valid actions: {valid}"
    )


async def handle_group(group: str, arguments: dict) -> list[TextContent]:
    """Dispatch a grouped tool call into the flat TOOL_HANDLERS registry."""
    if group not in TOOL_GROUPS:
        return _group_action_error(group, None)

    action = arguments.get("action")
    if not action or action not in TOOL_GROUPS[group]["actions"]:
        return _group_action_error(group, action)

    handler = TOOL_HANDLERS.get(action)
    if handler is None:
        # Only reachable if TOOL_GROUPS names an action with no handler, which
        # test_every_group_action_resolves_to_a_real_handler exists to prevent.
        return _text_result(f"No handler registered for action: {action}")

    if "args" in arguments:
        call_args = arguments.get("args") or {}
        if not isinstance(call_args, dict):
            return _text_result(
                f"Invalid 'args' for '{group}.{action}': expected an object, "
                f"got {type(call_args).__name__}."
            )
    else:
        # The schema says arguments live under "args", and a caller that sends
        # them flat used to get "Missing required argument: filepath" while
        # looking at a call that plainly passed filepath — the one error text
        # guaranteed to send someone hunting in the wrong place. Models make
        # this mistake constantly against grouped tools. Take the flat form
        # rather than teaching it a lesson; the advertised schema is unchanged,
        # so a correct caller is unaffected.
        call_args = {k: v for k, v in arguments.items() if k != "action"}

    return await _journaled(group, action, call_args, handler)


def _group_tool(name: str) -> Tool:
    """Build the advertised Tool schema for one group."""
    spec = TOOL_GROUPS[name]
    return Tool(
        name=name,
        description=(
            f"{spec['description']} "
            f"Actions: {', '.join(spec['actions'])}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": spec["actions"],
                    "description": "Which operation to run.",
                },
                "args": {
                    "type": "object",
                    "description": (
                        "Arguments for the chosen action, e.g. "
                        "{\"filepath\": \"/path/to/project.fcpxml\"}."
                    ),
                },
            },
            "required": ["action"],
        },
    )


_ACTION_SCHEMAS: dict[str, dict] | None = None


def _action_param_help(action: str | None) -> str:
    """Describe one action's accepted parameters, from its original tool schema.

    Grouped calls nest arguments under `args`, so the per-action required
    fields are no longer visible in the advertised schema — the caller is
    guessing parameter names. Most handlers take `filepath`, but a few take
    something else (`media_path` on the beat tools, for one), and a bare
    `KeyError` gives the caller nothing to correct. This turns that dead end
    into a recoverable one.
    """
    global _ACTION_SCHEMAS
    if not action:
        return ""
    if _ACTION_SCHEMAS is None:
        _ACTION_SCHEMAS = {t.name: tool_input_schema(t) for t in _legacy_tool_list()}
    schema = _ACTION_SCHEMAS.get(action)
    if schema is None:
        return ""

    props = schema.get("properties") or {}
    if not props:
        return f"'{action}' takes no arguments."

    required = set(schema.get("required") or [])
    lines = []
    for key, spec in props.items():
        flag = "required" if key in required else "optional"
        desc = (spec or {}).get("description", "")
        lines.append(f"  {key} ({flag})" + (f": {desc}" if desc else ""))
    return f"'{action}' accepts:\n" + "\n".join(lines)


GATED_ACTIONS = frozenset({
    "export_csv", "export_edl", "export_fcp7_xml", "export_resolve_xml",
    "export_role_stems", "push_to_fcp",
})


_UNREVIEWED_NOTE = (
    "\n\n*Shipped UNREVIEWED (confirm_unreviewed=true): no rendered preview "
    "matches this file's current state.*"
)


def _review_gate(action: str, arguments: dict) -> list[TextContent] | None:
    """The editors' checkpoint: watch it in full before it leaves.

    Refuses a gated action when the journal holds no preview_render whose
    input hash equals the file as it is NOW. A render of an earlier state
    proves nothing about this one. Returns the refusal, or None to proceed.
    """
    if action not in GATED_ACTIONS or arguments.get("confirm_unreviewed") is True:
        return None
    filepath = arguments.get("filepath")
    if not isinstance(filepath, str):
        return None  # the handler reports the missing argument itself
    call = json.dumps({"action": "preview_render", "args": {"filepath": filepath}})
    if not _journal.enabled():
        return _text_result(
            f"Refused: {action} cannot verify a review because the journal is off "
            f"(FCP_MCP_JOURNAL). Turn it on and render first: preview {call} — "
            "or pass confirm_unreviewed=true to ship without watching it."
        )
    if _journal.reviewed(filepath) is None:
        return _text_result(
            f"Refused: {filepath} has no rendered preview for its current state. "
            f"Watch it first: preview {call} — then run {action} again. "
            "To ship unreviewed anyway, pass confirm_unreviewed=true."
        )
    return None


async def _journaled(tool: str, action: str, arguments: dict, handler) -> Sequence[TextContent]:
    """Run *handler* inside a journal ledger, behind the review gate.

    Any path the handler validates as an output and then writes is recorded
    against the input. Handlers change nothing for this: the seam is
    _validate_output_path, which every write already passes through.
    """
    refusal = _review_gate(action, arguments)
    if refusal is not None:
        return refusal
    filepath = arguments.get("filepath")
    input_path = filepath if isinstance(filepath, str) else None
    token = _journal.begin(tool, action, arguments, input_path)
    try:
        result = await handler(arguments)
    finally:
        written = _journal.finish(token)
    # Autopush lives HERE, on the same seam, so every write handler gets it
    # without knowing: whatever this request wrote as FCPXML is pushed.
    # push_to_fcp is the push; it is not pushed again.
    if action != "push_to_fcp" and _autopush_enabled():
        pushed = "".join(
            _maybe_autopush(p) for p in written if p.endswith((".fcpxml", ".fcpxmld"))
        )
        if pushed:
            result = _text_result(result[0].text + pushed)
    if action in GATED_ACTIONS and arguments.get("confirm_unreviewed") is True:
        result = _text_result(result[0].text + _UNREVIEWED_NOTE)
    return result


async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
    handler = TOOL_HANDLERS.get(name)
    if not handler and name not in TOOL_GROUPS:
        return _text_result(f"Unknown tool: {name}")
    try:
        if name in TOOL_GROUPS:
            return await handle_group(name, arguments)
        return await _journaled(name, name, arguments, handler)
    except _NoTimelineError:
        return _no_timeline()
    except FileNotFoundError as e:
        return _text_result(f"File not found: {e}")
    except ValueError as e:
        return _text_result(f"Validation error: {e}")
    except KeyError as e:
        action = arguments.get("action") if name in TOOL_GROUPS else name
        missing = str(e).strip("'\"")
        help_text = _action_param_help(action)
        msg = f"Missing required argument: {missing}"
        return _text_result(f"{msg}\n\n{help_text}" if help_text else msg)
    except Exception as e:
        return _text_result(f"Error: {type(e).__name__}")


# ============================================================================
# HANDLER REGISTRATION
# ============================================================================

# Registered here rather than by decorator: mcp 2.0 removed the decorator API
# entirely (issue #9). register_handlers detects which API the installed SDK
# exposes and wires the same six functions either way — see fcpxml/mcp_compat.
register_handlers(
    server,
    list_resources=list_resources,
    read_resource=read_resource,
    list_prompts=list_prompts,
    get_prompt=get_prompt,
    list_tools=list_tools,
    call_tool=call_tool,
)


# ============================================================================
# MAIN
# ============================================================================

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for use as a console script."""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
