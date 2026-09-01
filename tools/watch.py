"""The watch tool group — the loop's return path.

Every message that can end in a wait names the exact keystroke that ends it.
An operator staring at a stalled prompt with no instruction is the failure mode
this group exists to remove, not to reproduce.
"""

from pathlib import Path
from typing import Any, Dict

from fcpxml import bridges
from fcpxml.diff import compare_timelines, format_diff
from fcpxml.watchfolder import Watcher, default_watch_dir
from tools._common import text_result

# Module-level because a watcher must survive between tool calls. The MCP server
# is one long-lived process serving one operator; a per-call watcher would
# re-baseline every time and could never detect anything.
_STATE: Dict[str, Any] = {}


async def handle_watch_start(args: dict):
    directory = args.get("directory") or default_watch_dir()
    if not directory:
        return text_result(
            "watch_start needs a directory. Pass 'directory', or set "
            "FCP_WATCH_DIR to your Final Cut Pro export destination."
        )
    watcher = Watcher(str(directory))
    try:
        watcher.baseline()
    except ValueError as exc:
        return text_result(f"Cannot watch: {exc}")

    _STATE["watcher"] = watcher
    _STATE["directory"] = str(directory)
    _STATE["last"] = None
    return text_result(
        f"Watching {directory} for .fcpxml and .fcpxmld exports.\n"
        f"{bridges.describe()}"
    )


async def handle_watch_status(args: dict):
    watcher = _STATE.get("watcher")
    if watcher is None:
        return text_result("Not watching. Run watch_start first.")
    lines = [f"Watching {_STATE['directory']}"]
    lines.append(
        f"Last export seen: {_STATE['last']}" if _STATE.get("last")
        else "No export seen yet."
    )
    lines.append(bridges.describe())
    return text_result("\n".join(lines))


async def handle_watch_stop(args: dict):
    if _STATE.get("watcher") is None:
        return text_result("Not watching.")
    directory = _STATE.get("directory")
    _STATE.clear()
    return text_result(f"Stopped watching {directory}.")


async def handle_watch_pull(args: dict):
    watcher = _STATE.get("watcher")
    if watcher is None:
        return text_result("Not watching. Run watch_start first.")

    try:
        timeout = float(args.get("timeout", 120.0))
    except (TypeError, ValueError):
        return text_result(f"timeout must be a number, got {args.get('timeout')!r}")

    try:
        found = watcher.pull(timeout=timeout)
    except ValueError as exc:
        return text_result(str(exc))
    if found is None:
        return text_result(
            f"No export in {timeout:.0f}s. In Final Cut Pro: File > Export XML "
            f"(Cmd-E) into {_STATE['directory']}, then run watch_pull again."
        )

    previous = _STATE.get("last")
    _STATE["last"] = found
    lines = [f"Export detected: {found}"]

    if previous and previous != found and Path(previous).exists():
        try:
            lines.append(format_diff(compare_timelines(previous, found)))
        except (ValueError, OSError) as exc:
            lines.append(f"Could not diff against {previous}: {exc}")
    else:
        lines.append(
            "No previous export to diff against — this one is the baseline. "
            "Export again after your next change and watch_pull will show the "
            "difference."
        )
    return text_result("\n".join(lines))


ACTIONS = {
    "watch_start": handle_watch_start,
    "watch_status": handle_watch_status,
    "watch_stop": handle_watch_stop,
    "watch_pull": handle_watch_pull,
}

DESCRIPTION = (
    "Close the round-trip. Watch the Final Cut Pro export folder, detect the "
    "XML the moment it lands, and diff it against the last one seen. Pair with "
    "deliver.push_to_fcp for a full loop: push in, edit, Cmd-E, watch_pull."
)
