"""The preview tool group — the loop's eyes.

Five actions, one job: let the operator evaluate an edit without opening Final
Cut Pro. preview_check is the important one — it reads the SOURCE MEDIA, so a
fix is confirmed from an image rather than from re-parsing our own output.
"""

import subprocess
from fractions import Fraction
from pathlib import Path

from fcpxml import filtergraph, journal, render, visual
from tools._common import parse_project, text_result

CMUX_PREVIEW = Path.home() / ".claude" / "hooks" / "cmux-image-preview.mjs"

TIMELINE_BAR_WIDTH = 56


def _open_beside_terminal(paths: list) -> None:
    """Paint artifacts into a pane next to the terminal.

    A path printed in chat is not showing the operator the thing. Failure here
    is cosmetic — the file is written either way — so it must never fail the
    tool call.
    """
    if not paths or not CMUX_PREVIEW.is_file():
        return
    try:
        subprocess.run(
            ["node", str(CMUX_PREVIEW), *[str(p) for p in paths]],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _seconds(value, field: str) -> Fraction:
    try:
        return Fraction(str(value)).limit_denominator(100000)
    except (ValueError, TypeError, ZeroDivisionError):
        raise ValueError(f"{field} must be a number of seconds, got {value!r}") from None


def _timeline_or_message(filepath: str):
    """Return (timeline, None) or (None, error text)."""
    if not filepath:
        return None, "This action requires 'filepath'."
    try:
        _project, timeline = parse_project(filepath)
    except (ValueError, OSError) as exc:
        return None, f"Could not read {filepath}: {exc}"
    if timeline is None:
        return None, "No timelines found"
    return timeline, None


def _crossfade_line(transitions) -> list[str]:
    """One line naming the transitions that actually made it into the render.

    Reporting only the substitutions would leave a compiled dissolve looking
    identical to one that was silently dropped.
    """
    if not transitions:
        return []
    kinds = ", ".join(sorted({t.kind for t in transitions}))
    return [f"Crossfades compiled: {len(transitions)} ({kinds})"]


def _describe(result: dict) -> str:
    lines = []
    if result.get("error"):
        lines.append(f"Preview not rendered: {result['error']}")
    else:
        lines.append(f"Rendered: {result['path']}")
        expected, duration = result.get("expected"), result.get("duration")
        if duration is not None and expected is not None:
            lines.append(
                f"Duration {float(duration):.3f}s against an expected "
                f"{float(expected):.3f}s (drift {float(result['drift']):+.3f}s)"
            )
        else:
            # Saying nothing here would let an unverified render read exactly
            # like a verified one.
            lines.append(
                "Duration could NOT be read back — treat this render as UNVERIFIED."
            )
    lines += _crossfade_line(result.get("transitions") or ())
    for note in result.get("substitutions") or ():
        lines.append(f"Substituted: {note}")
    if result.get("skipped"):
        lines.append("Skipped (media missing): " + ", ".join(result["skipped"]))
    return "\n".join(lines)


async def handle_preview_render(args: dict):
    timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    try:
        height = int(args.get("height", 480))
    except (TypeError, ValueError):
        return text_result(f"height must be an integer, got {args.get('height')!r}")

    result = render.render_proxy(
        timeline, out_path=args.get("output_path"), height=height
    )
    if result.get("path"):
        # The proxy lands in the private cache, never under the project's
        # anchor, so it never passes _validate_output_path. This note is the
        # only way the render reaches the ledger — and the review gate.
        journal.note_output(result["path"])
        _open_beside_terminal([result["path"]])
    return text_result(_describe(result))


async def handle_preview_sheet(args: dict):
    timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)

    graph = filtergraph.compile_timeline(timeline)
    out_dir = render.cache_dir()
    written, skipped = [], []
    for index, segment in enumerate(s for s in graph.segments if s.lane == 0):
        if segment.missing:
            skipped.append(segment.label)
            continue
        target = str(out_dir / f"sheet_{index:04d}.png")
        # Mid-clip, so the frame is representative rather than a fade-in.
        frame = render.render_frame(
            segment.source, segment.src_in + segment.duration / 2, target
        )
        (written if frame else skipped).append(frame or segment.label)

    if written:
        _open_beside_terminal(written)
    lines = [f"Contact sheet: {len(written)} frames, one per spine cut."]
    if skipped:
        lines.append("No frame for: " + ", ".join(skipped))
    lines += _crossfade_line(graph.transitions)
    for note in graph.substitutions:
        lines.append(f"Substituted: {note}")
    return text_result("\n".join(lines))


async def handle_preview_frame(args: dict):
    source, at = args.get("source"), args.get("at")
    if not source or at is None:
        return text_result("preview_frame requires 'source' and 'at' (seconds).")
    try:
        position = _seconds(at, "at")
    except ValueError as exc:
        return text_result(str(exc))

    target = str(render.cache_dir() / "frame.png")
    written = render.render_frame(source, position, target)
    if not written:
        return text_result(
            f"No frame extracted from {source}. Check the file exists and that "
            "ffmpeg is on PATH."
        )
    _open_beside_terminal([written])
    return text_result(f"Frame at {at}s: {written}")


async def handle_preview_check(args: dict):
    source = args.get("source")
    start, end = args.get("start"), args.get("end")
    if not source or start is None or end is None:
        return text_result(
            "preview_check requires 'source', 'start' and 'end' (seconds)."
        )
    target = str(render.cache_dir() / "check.png")
    try:
        written = visual.visual_check(
            source, _seconds(start, "start"), _seconds(end, "end"), target,
            frames=int(args.get("frames", 8)),
        )
    except (ValueError, TypeError) as exc:
        return text_result(str(exc))
    if not written:
        return text_result(
            f"No visual check produced for {source}. Check the file exists and "
            "that ffmpeg is on PATH."
        )
    _open_beside_terminal([written])
    return text_result(
        f"Filmstrip + waveform for {source} [{start}s - {end}s]: {written}\n"
        "Read from the media, not from the XML."
    )


async def handle_preview_timeline(args: dict):
    timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)

    graph = filtergraph.compile_timeline(timeline)
    total = float(graph.total)
    lines = [f"{getattr(timeline, 'name', 'timeline')} — {total:.2f}s"]
    if total <= 0:
        lines.append("(empty timeline)")
        return text_result("\n".join(lines))

    for segment in graph.segments:
        if segment.lane != 0:
            continue
        left = int(float(segment.tl_in) / total * TIMELINE_BAR_WIDTH)
        span = max(1, int(float(segment.duration) / total * TIMELINE_BAR_WIDTH))
        bar = " " * left + "#" * span
        flag = "  [MEDIA MISSING]" if segment.missing else ""
        lines.append(f"{bar:<{TIMELINE_BAR_WIDTH}} {segment.label}{flag}")
    lines += _crossfade_line(graph.transitions)
    for note in graph.substitutions:
        lines.append(f"Substituted: {note}")
    return text_result("\n".join(lines))


ACTIONS = {
    "preview_render": handle_preview_render,
    "preview_sheet": handle_preview_sheet,
    "preview_frame": handle_preview_frame,
    "preview_check": handle_preview_check,
    "preview_timeline": handle_preview_timeline,
}

DESCRIPTION = (
    "See the edit without opening Final Cut Pro. Render a proxy video of the "
    "timeline, a contact sheet of every cut, a single frame, or a "
    "filmstrip-plus-waveform check read from the SOURCE MEDIA rather than from "
    "the XML. Run preview_check to confirm a fix actually landed."
)
