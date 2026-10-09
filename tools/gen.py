"""The gen tool group — generative fill behind a money gate.

``gen_quote`` prices a job; ``gen_fill_gap``, ``gen_broll`` and
``gen_extend_clip`` generate against a confirmed quote and attach the result
as a connected clip. One runner does the shared work so the gate, the probe,
the insert, the DTD check and the key redaction cannot be skipped by one
action and kept by another. Everything a handler returns goes through
``gen.redact`` — including the refusals, which quote the prompt back.
"""

from __future__ import annotations

import json
import re
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import tools
from fcpxml import dtd, gen, journal, render, timeline_app
from fcpxml.media_intel import media_src_to_path
from fcpxml.writer import FCPXMLModifier
from tools._common import parse_project, text_result

Target = dict  # {"at": Fraction, "seconds": Fraction, "name": str, "frame": Optional[str], "note": str}


def _seconds(value, field: str, default: Optional[Fraction] = None) -> Fraction:
    if value is None:
        if default is None:
            raise ValueError(f"{field} is required")
        return default
    try:
        f = Fraction(str(value)).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"{field} must be a number of seconds, got {value!r}") from None
    if f <= 0:
        raise ValueError(f"{field} must be positive, got {value!r}")
    return f


def _frame_of(media_src: str, at: Fraction, tag: str) -> tuple[Optional[str], str]:
    """A JPEG of the clip's source at *at* seconds, for image conditioning.

    None (with the reason) when ffmpeg or the media is absent: the job then
    runs text-to-video and the result says so, rather than quietly changing
    what was asked for.
    """
    path = media_src_to_path(media_src or "")
    if not path or not Path(path).is_file():
        return None, "source media not found; generated from the prompt alone"
    target = str(render.cache_dir() / f"gen_condition_{tag}.jpg")
    written = render.render_frame(path, at, target, max_short_side=1080)
    if not written:
        return None, "ffmpeg could not extract a frame; generated from the prompt alone"
    return written, f"conditioned on the frame at {float(at):.3f}s of {Path(path).name}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-")[:40].lower() or "generated"


# -- targets: where the new picture goes -------------------------------------

def _gap_target(args: dict, payload: dict) -> Target:
    gaps = payload["gaps"]
    if not gaps:
        raise ValueError("The primary storyline has no gaps to fill.")
    if args.get("at") is not None:
        at = _seconds(args.get("at"), "at")
        hit = next((g for g in gaps if Fraction(g["start"]) <= at < Fraction(g["start"]) + Fraction(g["duration"])), None)
        if hit is None:
            spans = ", ".join(f"{float(Fraction(g['start'])):.2f}s+{float(Fraction(g['duration'])):.2f}s" for g in gaps)
            raise ValueError(f"No gap at {float(at):.2f}s. Gaps: {spans}")
    else:
        index = int(args.get("gap_index", 0))
        if not 0 <= index < len(gaps):
            raise ValueError(f"gap_index must be 0..{len(gaps) - 1}; there are {len(gaps)} gaps")
        hit = gaps[index]
    seconds = Fraction(hit["duration"])
    if args.get("seconds") is not None:
        seconds = min(seconds, _seconds(args.get("seconds"), "seconds"))
    return {"at": Fraction(hit["start"]), "seconds": seconds, "name": "gap", "frame": None,
            "note": f"filling the gap at {float(Fraction(hit['start'])):.2f}s ({float(seconds):.2f}s)"}


def _clip_named(args: dict, payload: dict) -> dict:
    name = args.get("clip_name")
    if not name:
        raise ValueError("clip_name is required")
    hits = [c for c in payload["clips"] if c["name"] == name]
    if not hits:
        names = ", ".join(sorted({c["name"] for c in payload["clips"]})[:20])
        raise ValueError(f"No clip named '{name}'. Clips: {names}")
    return hits[0]


def _broll_target(args: dict, payload: dict) -> Target:
    clip = _clip_named(args, payload)
    start, dur = Fraction(clip["start"]), Fraction(clip["duration"])
    into = _seconds(args.get("at"), "at", Fraction(0)) if args.get("at") is not None else Fraction(0)
    if into >= dur:
        raise ValueError(f"at={float(into):.2f}s is past the end of '{clip['name']}' ({float(dur):.2f}s)")
    seconds = _seconds(args.get("seconds"), "seconds", Fraction(5))
    frame, note = (None, "generated from the prompt alone")
    if args.get("condition_on_frame", True) is not False:
        frame, note = _frame_of(clip["media"], Fraction(clip["source_start"]) + into, "broll")
    return {"at": start + into, "seconds": seconds, "name": f"broll-{_slug(clip['name'])}",
            "frame": frame, "note": f"B-roll over '{clip['name']}' from {float(start + into):.2f}s; {note}"}


def _extend_target(args: dict, payload: dict) -> Target:
    clip = _clip_named(args, payload)
    end, dur = Fraction(clip["end"]), Fraction(clip["duration"])
    seconds = _seconds(args.get("seconds"), "seconds", Fraction(5))
    # The LAST frame of the clip, so the generation continues the shot.
    last = Fraction(clip["source_start"]) + dur - Fraction(1, 1000)
    frame, note = _frame_of(clip["media"], max(last, Fraction(0)), "extend")
    return {"at": end, "seconds": seconds, "name": f"extend-{_slug(clip['name'])}",
            "frame": frame, "note": f"continuing '{clip['name']}' from its out point at {float(end):.2f}s; {note}"}


# -- the runner ---------------------------------------------------------------

async def _run(args: dict, target_of: Callable[[dict, dict], Target], prompt_default: Optional[str] = None):
    srv = tools.server_module()
    prompt = (args.get("prompt") or prompt_default or "").strip()
    provider = args.get("provider", "fal")
    try:
        filepath, output_path = srv._resolve_io_paths(args, "_generated")
        _project, timeline = parse_project(filepath)
        if timeline is None:
            return text_result("No timelines found")
        payload = timeline_app.build_payload(timeline, filepath=filepath)
        target = target_of(args, payload)
        lane = int(args.get("lane", 1))
        if lane == 0:
            raise ValueError("lane must be non-zero; the generated clip connects above (or below) the spine")

        # The gate. Refuses before any key is read, any frame rendered, any
        # byte sent.
        q = gen.confirm_quote(
            args.get("quote_id"), args.get("confirm"), provider, target["seconds"], prompt, args.get("model")
        )
        gen.key_for(provider)

        gen_dir = Path(filepath).resolve().parent / "generated"
        gen_dir.mkdir(exist_ok=True)
        media_target = srv._validate_output_path(
            str(gen_dir / f"{target['name']}-{int(time.time())}.mp4"),
            anchor_dir=str(Path(filepath).resolve().parent),
        )

        adapter = gen.provider_for(provider, q.model)
        data = adapter.generate(prompt, q.billable_seconds, image_path=target["frame"])
        if not data:
            raise gen.ProviderError(f"{adapter.label} returned an empty file")
        Path(media_target).write_bytes(data)

        probe = gen.probe_media(media_target)
        if probe is None:
            return text_result(gen.redact(
                f"Generated media written to {media_target} but ffprobe could not read it "
                "(ffprobe missing, or the provider returned something that is not video). "
                "Nothing was inserted: the frame rate has to come from the file, never from a guess."
            ))

        modifier = FCPXMLModifier(filepath)
        placed = gen.insert_generated(
            modifier, media_path=media_target, probe=probe, at=target["at"],
            duration=target["seconds"], name=f"Generated {target['name']}",
            prompt=prompt, provider_label=adapter.label, lane=lane,
        )
        modifier.save(output_path)
        ok, detail = dtd.validate_against_dtd(output_path)
    except (gen.GenError, ValueError, OSError, FileNotFoundError) as exc:
        return text_result(gen.redact(str(exc)))

    fps = probe["fps"]
    lines = [
        f"Generated {q.billable_seconds}s on {adapter.label} — {target['note']}.",
        f"Media: {media_target} ({float(probe['duration']):.3f}s, {probe['width']}x{probe['height']} "
        f"@ {fps.numerator}/{fps.denominator} fps, probed)",
        f"Placed: lane {placed['lane']} at {float(placed['at']):.3f}s for {float(placed['duration']):.3f}s "
        f"on a <{placed['host']}>, role `{gen.GENERATED_ROLE}`, marker \"{placed['marker']}\""
        + (" — conformed to the sequence frame grid and the media length" if placed["conformed"] else "")
        + (" (a <gap> was appended to the spine to reach it)" if placed["gap_added"] else ""),
        f"Saved to: `{output_path}`",
        f"sha256: {journal.file_hash(output_path)}",
        f"Estimated cost: ${q.usd} (prices as of {q.prices_as_of}, unverified; cap ${q.cap_usd})",
    ]
    if ok is True:
        lines.append(f"DTD: valid ({detail})")
    elif ok is False:
        lines.append(f"DTD: INVALID — {detail}. The file is kept; do not import it until this is understood.")
    else:
        lines.append(f"DTD: not checked — {detail}")
    return text_result(gen.redact("\n".join(lines)))


# -- actions ---------------------------------------------------------------------

async def handle_gen_quote(args: dict):
    try:
        q = gen.quote(args.get("provider", "fal"), args.get("seconds"), args.get("prompt") or "", args.get("model"))
    except (gen.GenError, ValueError) as exc:
        return text_result(gen.redact(str(exc)))
    d = q.as_dict()
    lines = [
        f"**Quote {q.id}** — {q.provider}/{q.model}, {q.billable_seconds}s billable "
        f"for {float(q.requested_seconds):.2f}s requested",
        f"Estimated: {'$' + str(q.usd) if q.usd is not None else 'no price on file'} "
        f"(prices as of {q.prices_as_of}; third-party figures, NOT verified against an invoice)",
        f"Cap: ${q.cap_usd} per call ({gen.MAX_USD_ENV})" + (" — OVER CAP, generation will refuse" if q.over_cap else ""),
    ]
    if not q.available:
        lines.append(f"{q.provider}/{q.model} is video-to-video and not implemented here; this quote is an estimate only.")
    else:
        lines.append(
            "To spend it: call gen_fill_gap / gen_broll / gen_extend_clip with the same provider, model, "
            f"seconds and prompt, plus quote_id=\"{q.id}\" and confirm=true. Valid for "
            f"{gen.QUOTE_TTL_SECONDS // 60} minutes in this server process."
        )
    lines.append("```json\n" + json.dumps(d) + "\n```")
    return text_result(gen.redact("\n".join(lines)))


async def handle_gen_fill_gap(args: dict):
    return await _run(args, _gap_target)


async def handle_gen_broll(args: dict):
    return await _run(args, _broll_target)


async def handle_gen_extend_clip(args: dict):
    return await _run(args, _extend_target, prompt_default="continue the shot")


ACTIONS = {
    "gen_quote": handle_gen_quote,
    "gen_fill_gap": handle_gen_fill_gap,
    "gen_broll": handle_gen_broll,
    "gen_extend_clip": handle_gen_extend_clip,
}

DESCRIPTION = (
    "Generative fill, quote-then-confirm. gen_quote(provider, seconds, prompt, "
    "model?) prices a job from a dated table (fal: kling-2.5, wan-2.5; gemini: "
    "veo-3.1; runway/luma are honest stubs) and returns a quote id. "
    "gen_fill_gap(filepath, prompt, gap_index|at), gen_broll(filepath, "
    "clip_name, prompt, seconds=5, at?) and gen_extend_clip(filepath, "
    "clip_name, seconds=5) REFUSE without quote_id + confirm=true, a matching "
    "job, a key in the environment (FAL_KEY / GEMINI_API_KEY) and a price under "
    "FCP_MCP_GEN_MAX_USD (default 5). The result is probed with ffprobe for its "
    "real fps and length, saved under generated/ next to the project, attached "
    "as a connected clip with role `generated` and a marker naming prompt and "
    "provider, DTD-validated and journaled."
)
