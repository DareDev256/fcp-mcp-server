"""The preview tool group — the loop's eyes.

Five actions, one job: let the operator (and the model, which takes no video)
evaluate an edit without opening Final Cut Pro. Since 0.32 every picture
carries the edit's names, timecodes and cuts, drawn by the server
(fcpxml/proofs.py), and comes back as an MCP image block as well as a path.
preview_check is the important one — it reads the SOURCE MEDIA, so a fix is
confirmed from an image rather than from re-parsing our own output.
"""

import base64
import subprocess
from fractions import Fraction
from pathlib import Path

from fcpxml import filtergraph, journal, proofs, render
from fcpxml import raster as rs
from tools._common import parse_project, text_result

CMUX_PREVIEW = Path.home() / ".claude" / "hooks" / "cmux-image-preview.mjs"

TIMELINE_WIDTH = 64          # characters of the text timeline's track area
IMAGE_BLOCKS_MAX = 4         # sheet pages returned inline; the rest by path
# One image block's bytes at most. The vision docs (platform.claude.com/docs/
# en/build-with-claude/vision, "Request limits", read 2026-11-13) allow 10 MB
# base64 per image on the Claude API and 5 MB on Bedrock; tool results travel
# through hosts with their own budgets, so a block stays far under both. A
# PNG over this goes inline as a light JPEG (the PNG stays at its path).
IMAGE_BLOCK_MAX_BYTES = 1_500_000


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


def image_block(picture):
    """An MCP image block for a Raster, under IMAGE_BLOCK_MAX_BYTES: the PNG
    when it fits, else a light JPEG (no chroma subsampling), else None."""
    from mcp.types import ImageContent

    if picture is None:
        return None
    for fmt, mime, quality in (("png", "image/png", 0), ("jpg", "image/jpeg", 2), ("jpg", "image/jpeg", 4)):
        data = picture.encode(fmt) if fmt == "png" else picture.encode(fmt, quality)
        if data and len(data) <= IMAGE_BLOCK_MAX_BYTES:
            return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime)
    return None


def resource_link(path: str, mime: str, name: str):
    from mcp.types import ResourceLink

    return ResourceLink(type="resource_link", name=name, uri=Path(path).resolve().as_uri(), mimeType=mime)


def _with_images(text: str, pictures: list) -> list:
    blocks = [b for b in (image_block(p) for p in pictures) if b is not None]
    if len(blocks) < len([p for p in pictures if p is not None]):
        text += "\n(An image was over the inline size cap and is returned by path only.)"
    return text_result(text) + blocks


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
        lines.append(f"Rendered: {result['path']} ({result.get('size', '')})".replace(" ()", ""))
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
        if result.get("verified") is False and duration is not None:
            lines.append(
                "UNVERIFIED: the render does not match the timeline "
                "(see the drift above and the lane checks below)."
            )
    lines += _crossfade_line(result.get("transitions") or ())
    for label, verdict, detail in result.get("lanes") or ():
        if verdict is True:
            lines.append(f"Seen in the render: {label} ({detail})")
        elif verdict is False:
            lines.append(f"NOT drawn: {label} is not in the render ({detail})")
        else:
            lines.append(f"Not checked: {label} ({detail})")
    for note in result.get("substitutions") or ():
        lines.append(f"Substituted: {note}")
    if result.get("skipped"):
        lines.append("Skipped (media missing): " + ", ".join(result["skipped"]))
    if result.get("burned"):
        lines.append(f"Burned-in copy (timecode, clip names, lane badges; after the lane check): {result['burned']}")
    elif result.get("burn_error"):
        lines.append(f"No burned-in copy: {result['burn_error']}")
    if result.get("poster"):
        lines.append(f"Poster frame: {result['poster']}")
    return "\n".join(lines)


async def handle_preview_render(args: dict):
    timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    try:
        height = int(args.get("height", render.DEFAULT_HEIGHT))
    except (TypeError, ValueError):
        return text_result(f"height must be an integer, got {args.get('height')!r}")

    result = render.render_proxy(
        timeline, out_path=args.get("output_path"), height=height,
        burn_in=bool(args.get("burn_in", False)),
    )
    if not result.get("path"):
        return text_result(_describe(result))
    # The proxy lands in the private cache, never under the project's
    # anchor, so it never passes _validate_output_path. This note is the
    # only way the render reaches the ledger — and the review gate.
    journal.note_output(result["path"])
    shown = result.get("burned") or result["path"]
    _open_beside_terminal([shown])
    reply = text_result(_describe(result))
    poster = rs.load(result["poster"]) if result.get("poster") else None
    block = image_block(poster)
    if block is not None:
        reply.append(block)
    reply.append(resource_link(shown, "video/mp4", Path(shown).name))
    return reply


async def handle_preview_sheet(args: dict):
    timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    layout = args.get("layout", "desktop")
    if layout not in proofs.LAYOUTS:
        return text_result(f"layout must be one of {', '.join(proofs.LAYOUTS)}, got {layout!r}")

    graph = filtergraph.compile_timeline(timeline)
    sheet = proofs.contact_sheet(timeline, graph, render.cache_dir(), layout=layout,
                                 filepath=str(args.get("filepath")))
    if not sheet["pages"]:
        return text_result("No contact sheet: ffmpeg is not on PATH, or the timeline has no clips.")
    _open_beside_terminal(sheet["pages"])
    cells = sheet["cells"]
    fps = getattr(timeline, "frame_rate", 24) or 24
    lines = [f"Contact sheet ({layout}): {len(cells)} cuts on {len(sheet['pages'])} page(s), "
             f"{sheet['per_page']} cells a page."]
    lines += [f"Page {i + 1}: {p}" for i, p in enumerate(sheet["pages"])]
    lines.append("Cuts: " + "; ".join(f"#{c.index} {c.name} at {proofs.timecode(c.start, fps)}"
                                      + (" (media missing)" if c.missing else "") for c in cells))
    lanes = sum(1 for s in graph.segments if s.lane != 0 and s.kind == "clip")
    if lanes:
        lines.append(f"Not on the sheet: {lanes} connected clip(s) (see preview_render).")
    lines += _crossfade_line(graph.transitions)
    for note in graph.substitutions:
        lines.append(f"Substituted: {note}")
    if sheet["problems"]:
        lines.append("Legibility: " + "; ".join(sheet["problems"]))
    return _with_images("\n".join(lines), sheet["rasters"][:IMAGE_BLOCKS_MAX])


def _source_fps(source: str):
    info = render.probe_source(source) if Path(source).is_file() else None
    return getattr(info, "fps", None) or 24


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
    picture = rs.load(written)
    text = f"Frame at {at}s: {written}"
    if args.get("burn") and picture is not None:
        fps = _source_fps(source)
        tc = proofs.timecode(position, fps)
        proofs.burn_timecode(picture, tc)
        picture.save(written)
        text += f" (timecode {tc} burned in)"
    _open_beside_terminal([written])
    return _with_images(text, [proofs.fit_for_model(picture)])


async def handle_preview_check(args: dict):
    source = args.get("source")
    start, end = args.get("start"), args.get("end")
    if not source or start is None or end is None:
        return text_result(
            "preview_check requires 'source', 'start' and 'end' (seconds)."
        )
    target = str(render.cache_dir() / "check.png")
    try:
        cut = _seconds(args["cut"], "cut") if args.get("cut") is not None else None
        result = proofs.visual_check(
            source, _seconds(start, "start"), _seconds(end, "end"), target, cut=cut,
            frames=int(args.get("frames", 12)), fps=_source_fps(source),
        )
    except (ValueError, TypeError) as exc:
        return text_result(str(exc))
    if not result.get("path"):
        return text_result(
            f"No visual check produced for {source}. Check the file exists and "
            "that ffmpeg is on PATH."
        )
    _open_beside_terminal([result["path"]])
    lines = [f"Filmstrip + waveform for {source} [{start}s - {end}s]: {result['path']}",
             "Read from the media, not from the XML."]
    if not result["audio"]:
        lines.append("No audio in this source: no waveform.")
    elif result["spans"]:
        lines.append("Silence (at or under " f"{proofs.SILENCE_DB:.0f} dBFS, {float(proofs.SILENCE_MIN):.1f}s or longer): "
                     + ", ".join(f"{float(a):.2f}s to {float(b):.2f}s" for a, b in result["spans"]))
    if result["problems"]:
        lines.append("Legibility: " + "; ".join(result["problems"]))
    return _with_images("\n".join(lines), [result["raster"]])


async def handle_preview_timeline(args: dict):
    timeline, message = _timeline_or_message(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    graph = filtergraph.compile_timeline(timeline)
    return text_result(proofs.text_timeline(timeline, graph, TIMELINE_WIDTH))


ACTIONS = {
    "preview_render": handle_preview_render,
    "preview_sheet": handle_preview_sheet,
    "preview_frame": handle_preview_frame,
    "preview_check": handle_preview_check,
    "preview_timeline": handle_preview_timeline,
}

DESCRIPTION = (
    "See the edit without opening Final Cut Pro; pictures come back inline: a "
    "720p proxy (burn_in: timecode, names), a labelled sheet of every cut "
    "(layout: phone), a frame (burn), or a filmstrip-and-waveform check of the "
    "SOURCE MEDIA (cut). preview_check confirms a fix."
)
