"""The operation journal — an append-only ledger under ``~/.fcp-mcp/journal/``.

Every write operation appends one JSON line: when, which tool and action,
the arguments, the input file and its hash, the output file and its hash.
Because every operation writes a NEW suffixed file and never mutates its
input, ``undo`` is a pointer move: the recorded output is moved aside and
the input is, by construction, still there.

The project is the FOLDER. Every derived file is anchored to its input's
directory by ``_validate_output_path``, so one ledger per directory follows
the work wherever the suffix chain goes.

Records hold paths and hashes, never content. Set ``FCP_MCP_JOURNAL`` to a
directory to relocate the ledger, or to ``off`` to disable it — the review
gate then refuses to certify anything, and says so.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

_OFF = {"off", "0", "false", "no"}
_LEDGER: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "fcp_journal_ledger", default=None
)


def journal_dir() -> Optional[Path]:
    """The ledger directory (created, mode 700), or ``None`` when disabled."""
    raw = os.environ.get("FCP_MCP_JOURNAL", "").strip()
    if raw.lower() in _OFF:
        return None
    if raw:
        path = Path(raw).expanduser()
        private = (path,)
    else:
        path = Path.home() / ".fcp-mcp" / "journal"
        private = (path.parent, path)
    path.mkdir(parents=True, exist_ok=True)
    for p in private:
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    return path


def enabled() -> bool:
    return journal_dir() is not None


def file_hash(path: Optional[str]) -> Optional[str]:
    """sha256 of a file; of ``Info.fcpxml`` for a bundle; ``None`` if missing."""
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


def project_key(path: str) -> str:
    parent = str(Path(path).expanduser().resolve().parent)
    return hashlib.sha256(parent.encode("utf-8")).hexdigest()[:16]


def _ledger_file(path: str) -> Optional[Path]:
    d = journal_dir()
    return None if d is None else d / f"{project_key(path)}.jsonl"


def record(entry: dict) -> None:
    anchor = (entry.get("input") or {}).get("path") or (entry.get("output") or {}).get("path")
    f = _ledger_file(anchor) if anchor else None
    if f is None:
        return
    row = {"ts": time.time(), **entry}
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def records(path: str, limit: Optional[int] = None) -> list[dict]:
    """Every record for *path*'s folder, oldest first."""
    f = _ledger_file(path)
    if f is None or not f.is_file():
        return []
    rows = []
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn line from a crash mid-write; the rest is intact
    return rows[-limit:] if limit else rows


def reviewed(path: str) -> Optional[dict]:
    """The latest preview_render of *path* AS IT IS NOW, or ``None``.

    A render of an earlier state proves nothing about this one, so the match
    is on the input hash, not the path.
    """
    current = file_hash(path)
    if current is None:
        return None
    for row in reversed(records(path)):
        if row.get("action") == "preview_render" and (row.get("input") or {}).get("sha256") == current:
            return row
    return None


# -- request ledger -----------------------------------------------------------
# One ledger per tool call. Handlers do not know it exists: the seam is
# _validate_output_path, which notes every path it approves, and finish()
# keeps the ones that were actually written.

def begin(tool: str, action: str, args: dict, input_path: Optional[str]):
    ledger = {
        "tool": tool,
        "action": action,
        "args": _json_safe(args),
        "input": {"path": input_path, "sha256": file_hash(input_path)},
        "candidates": [],
        "started": time.time(),
    }
    return _LEDGER.set(ledger)


def note_output(path: str) -> None:
    ledger = _LEDGER.get()
    if ledger is not None and path not in ledger["candidates"]:
        ledger["candidates"].append(path)


def finish(token) -> list[str]:
    """Close the request's ledger and return the outputs it actually wrote.

    An output counts when the candidate exists on disk and is no older than
    the request. The list comes back whether or not the journal is enabled —
    autopush reads it too — but only an enabled journal records the rows.
    """
    ledger = _LEDGER.get()
    _LEDGER.reset(token)
    if ledger is None:
        return []
    written: list[str] = []
    for cand in ledger["candidates"]:
        p = Path(cand)
        target = p / "Info.fcpxml" if p.is_dir() else p
        if not target.is_file() or target.stat().st_mtime < ledger["started"] - 1:
            continue
        written.append(cand)
        if enabled():
            record({
                "tool": ledger["tool"],
                "action": ledger["action"],
                "args": ledger["args"],
                "input": ledger["input"],
                "output": {"path": cand, "sha256": file_hash(cand)},
            })
    return written


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


# -- undo ---------------------------------------------------------------------

def undo(path: str, n: int = 1) -> list[dict]:
    """Park the outputs of the last *n* write operations. Never deletes.

    Raises ``ValueError`` naming the file when an output's hash no longer
    matches what was recorded — someone edited it since, and moving their
    work aside on the strength of an old record is not this tool's call.
    """
    d = journal_dir()
    if d is None:
        return []
    rows = records(path)
    already = {(r.get("output") or {}).get("path") for r in rows if r.get("action") == "undo"}
    targets = [
        r for r in reversed(rows)
        if r.get("action") not in ("undo", "preview_render")
        and (r.get("output") or {}).get("path") not in already
    ][:n]
    moved = []
    for row in targets:
        out = row["output"]["path"]
        current = file_hash(out)
        if current is None:
            continue  # already gone; nothing to move
        if current != row["output"]["sha256"]:
            raise ValueError(
                f"{Path(out).name} changed since it was written (hash differs); "
                "refusing to move it. Undo it by hand if that is what you want."
            )
        dest_dir = d / "undone" / project_key(path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{int(time.time())}-{Path(out).name}"
        shutil.move(out, dest)
        entry = {
            "tool": "organize", "action": "undo", "args": {"filepath": path, "n": n},
            "input": row["input"], "output": {"path": out, "sha256": row["output"]["sha256"]},
            "moved_to": str(dest), "undid": row.get("action"),
        }
        record(entry)
        moved.append(entry)
    return moved
