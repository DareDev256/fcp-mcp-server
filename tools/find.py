"""The find tool group — "where is the bit where…" as a router.

Three tiers, consulted in order and NAMED in the first line of every result:
what was said (transcript words), what was logged (names, keywords, notes,
markers, audio events, scene cuts), what it looks like (shot captions from a
local vision model). A query that falls through to a tier that is not
available says so, with the reason — it never quietly degrades to "no
results" or implies a semantic search that did not happen.

Nothing here transcribes or goes online. ``find_index`` warms what can be
warmed locally; missing transcripts are reported with the tool that makes them.
"""

import json
from fractions import Fraction
from pathlib import Path

import tools
from fcpxml import diversity, find, vlm
from fcpxml import index as _index
from fcpxml import progress as _progress
from fcpxml.media_intel import media_src_to_path
from fcpxml.models import TimeValue
from fcpxml.rough_cut import RoughCutGenerator
from tools import scenes as _scenes
from tools._common import parse_project, text_result

MAX_LIVE_FRAMES = 20
MAX_LIMIT = 100


# -- per-clip context ------------------------------------------------------------

class _Ctx:
    """One timeline clip with what the tiers need to know about it."""

    def __init__(self, clip, media_path, ref):
        self.clip = clip
        self.media_path = media_path
        self.ref = ref
        self.src_start = Fraction(clip.source_start._exact_seconds) if clip.source_start else Fraction(0)
        self.src_end = self.src_start + Fraction(clip.duration._exact_seconds)
        self.transcript = None

    @property
    def name(self):
        return self.clip.name

    def timeline_seconds(self, source_seconds: Fraction) -> Fraction:
        return Fraction(self.clip.start._exact_seconds) + (Fraction(source_seconds) - self.src_start)

    def clamp(self, start: Fraction, end: Fraction):
        """The part of a source range this clip actually uses, or None."""
        s, e = max(Fraction(start), self.src_start), min(Fraction(end), self.src_end)
        return (s, e) if s < e else None


def _transcript_for(media_path: str):
    """index → sidecar → None. Never transcribes."""
    srv = tools.server_module()
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            data = ix.get_transcript(media_path)
        if data is not None:
            return data
    sidecar = srv._transcript_json_path(media_path)
    if sidecar.is_file():
        try:
            with open(sidecar) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("words"), list):
                return data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    return None


def _contexts(filepath: str, clip_filter=None):
    _, tl = parse_project(filepath)
    refs = {}
    for c in RoughCutGenerator(filepath).clips:
        refs.setdefault(c["name"], c["ref"])
    out, skipped = [], []
    for clip in tl.clips:
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        ctx = _Ctx(clip, media_path, refs.get(clip.name, ""))
        ctx.transcript = _transcript_for(media_path)
        out.append(ctx)
    return out, skipped


# -- tiers -----------------------------------------------------------------------

def _tier1(ctxs, query):
    hits, missing = [], []
    for ctx in ctxs:
        if ctx.transcript is None:
            missing.append(ctx.name)
            continue
        words = [w for w in ctx.transcript.get("words", [])
                 if ctx.src_start <= Fraction(w["start"]).limit_denominator(1_000_000) < ctx.src_end]
        hits.extend(find.transcript_hits(words, query, source=ctx.media_path, clip_name=ctx.name))
    return hits, missing


def _tier2(ctxs, query, scene_backend="auto"):
    hits = []
    for ctx in ctxs:
        clip = ctx.clip
        fields = {
            "name": clip.name,
            "keywords": ", ".join(clip.keyword_values),
            "notes": " ".join(m.note for m in clip.markers if m.note),
        }
        ranges = []
        for m in clip.markers:
            s = Fraction(m.start._exact_seconds)
            e = s + (Fraction(m.duration._exact_seconds) if m.duration else Fraction(1))
            ranges.append((s, e, f"marker: {m.name}"))
        if ctx.transcript is not None:
            for ev in ctx.transcript.get("events", []) or []:
                r = ctx.clamp(Fraction(ev["start"]).limit_denominator(1_000_000),
                              Fraction(ev["end"]).limit_denominator(1_000_000))
                if r:
                    ranges.append((r[0], r[1], f"event: {ev.get('label', '')}"))
        hits.extend(find.metadata_hits(fields, ranges, query, source=ctx.media_path, clip_name=ctx.name,
                                       clip_start=ctx.src_start, clip_end=ctx.src_end))
    return hits


def _vision_reason():
    """None when the vision tier can answer live; else why it cannot."""
    if not vlm.available():
        return f"the find extra is not installed ({vlm.INSTALL})"
    if not vlm.model_cached():
        return f"model {vlm.model_id()} not downloaded (hf download {vlm.model_id()})"
    return None


def _cached_shots(media_path):
    ix = _index.Index.open()
    if ix is None:
        return None
    with ix:
        return ix.get_shots(media_path)


def _shots_for(ctx, backend="auto"):
    """(start, end) scene ranges inside the clip's used window, or the window itself."""
    result = _scenes.scenes_cached(ctx.media_path, backend, None, 0.5)
    if result is None or not result["scenes"]:
        return [(ctx.src_start, ctx.src_end)]
    out = []
    for s, e in result["scenes"]:
        r = ctx.clamp(Fraction(s).limit_denominator(1_000_000), Fraction(e).limit_denominator(1_000_000))
        if r:
            out.append(r)
    return out or [(ctx.src_start, ctx.src_end)]


def _tier3(ctxs, query, live_budget: int):
    """Cached captions first; live captioning for uncaptioned sources when the model loads."""
    hits = []
    reason = _vision_reason()
    used_cache = False
    for ctx in ctxs:
        rows = _cached_shots(ctx.media_path)
        if rows is None and reason is None and live_budget > 0:
            shots = _shots_for(ctx)[:live_budget]
            try:
                rows = vlm.caption_shots(ctx.media_path, shots, max_frames=live_budget)
            except RuntimeError as exc:
                reason = str(exc)
                rows = None
            else:
                live_budget -= len(rows)
                ix = _index.Index.open()
                if ix is not None and rows:
                    with ix:
                        ix.put_shots(ctx.media_path, rows)
        if not rows:
            continue
        used_cache = True
        for r in rows:
            clamped = ctx.clamp(r["start"], r["end"])
            if not clamped or not r.get("caption"):
                continue
            score = min(1.0, diversity.similarity(query, r["caption"]) * 1.5)
            if score > 0:
                hits.append(find.Hit(ctx.media_path, ctx.name, clamped[0], clamped[1], score,
                                     "vision", "looks like: " + r["caption"][:80]))
    if reason is not None and used_cache:
        reason = None  # cached captions answered; nothing was unavailable for this query
    return hits, reason


def _mode_line(vision_consulted: bool, vision_reason):
    if not vision_consulted:
        return "Mode: transcript + metadata (vision not consulted — pass visual=true to ask it)"
    if vision_reason:
        return f"Mode: transcript + metadata (vision unavailable: {vision_reason})"
    return "Mode: transcript + metadata + vision"


def _index_line(ctxs):
    if not _index.enabled():
        return "Index: off (FCP_MCP_INDEX=off — every question is answered from scratch)"
    cold = 0
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            for ctx in ctxs:
                if ix.media_id(ctx.media_path) is None:
                    cold += 1
    warm = len(ctxs) - cold
    if cold:
        return f"Index: cold ({cold} of {len(ctxs)} sources need find_index)"
    return f"Index: warm ({warm} sources)"


def _search(args):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query must be a non-empty string")
    limit = int(args.get("limit", 10))
    if not (1 <= limit <= MAX_LIMIT):
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    ctxs, skipped = _contexts(args["filepath"], args.get("clip_name"))
    hits, missing = _tier1(ctxs, query)
    hits += _tier2(ctxs, query)
    transcript_text = " ".join((c.transcript or {}).get("text", "") for c in ctxs)
    ask_vision = bool(args.get("visual")) or find.is_visual(query, transcript_text) or len(find.rank(hits, limit)) < limit
    vision_reason = None
    if ask_vision:
        surviving = {h.clip_name for h in hits}
        targets = [c for c in ctxs if c.name in surviving] or ctxs
        v_hits, vision_reason = _tier3(targets, query, MAX_LIVE_FRAMES)
        hits += v_hits
    ranked = find.rank(hits, limit)
    by_name = {c.name: c for c in ctxs}
    return {
        "query": query, "ctxs": ctxs, "skipped": skipped, "missing": missing, "hits": ranked,
        "by_name": by_name, "mode": _mode_line(ask_vision, vision_reason),
    }


def _fmt(x: Fraction) -> str:
    return f"{float(x):.3f}s"


# -- handlers ----------------------------------------------------------------------

async def handle_find_shots(args: dict):
    srv = tools.server_module()
    r = _search(args)
    out = r["mode"] + "\n" + _index_line(r["ctxs"]) + "\n\n"
    out += f"# Find: {r['query']}\n\n"
    if r["hits"]:
        rows = []
        for i, h in enumerate(r["hits"], 1):
            ctx = r["by_name"][h.clip_name]
            rows.append([str(i), h.clip_name, _fmt(h.start), _fmt(h.end), _fmt(ctx.timeline_seconds(h.start)),
                         h.tier, f"{h.score:.2f}", h.why])
        out += srv._markdown_table(["#", "Clip", "Source in", "Source out", "Timeline", "Tier", "Score", "Why"], rows)
        out += "\n\n*Next: `find_to_timeline` with the same query to assemble these into a selects reel.*"
    else:
        out += (f"No shots matched '{r['query']}' across {len(r['ctxs'])} clips "
                "(searched transcript words, names, keywords, notes, markers, events"
                + (", captions)" if "vision" in r["mode"] and "unavailable" not in r["mode"] else ")") + ".")
        if r["missing"]:
            out += "\nRun `find_index` to see what is missing; transcripts are made with `transcript_media`."
        else:
            out += "\nRun `find_index` to warm every tier before asking again."
    if r["missing"]:
        out += f"\n\n**No transcript** ({len(r['missing'])}): {', '.join(r['missing'][:10])}"
        out += " — make one with `transcript_media` (never done implicitly)."
    if r["skipped"]:
        out += "\n\n**Skipped**: " + "; ".join(f"{n} ({why})" for n, why in r["skipped"])
    return text_result(out)


async def handle_find_index(args: dict):
    ctxs, skipped = _contexts(args["filepath"], args.get("clip_name"))
    captions = args.get("captions", True)
    backend = str(args.get("backend", "auto"))
    prog = _progress.start(total=len(ctxs))
    with_transcript, scenes_done = 0, 0
    captioned, caption_note = 0, ""
    seen = set()
    reason = _vision_reason() if captions else None
    for ctx in ctxs:
        await prog.step(f"find_index: {ctx.name}")
        if ctx.transcript is not None:
            with_transcript += 1
        if ctx.media_path in seen:
            continue
        seen.add(ctx.media_path)
        result = _scenes.scenes_cached(ctx.media_path, backend, None, 0.5)
        if result is not None:
            scenes_done += 1
        if not captions or reason is not None:
            continue
        if not _index.enabled():
            caption_note = "not stored (FCP_MCP_INDEX=off)"
            continue
        if _cached_shots(ctx.media_path) is not None:
            captioned += 1
            continue
        try:
            rows = vlm.caption_shots(ctx.media_path, _shots_for(ctx, backend))
        except RuntimeError as exc:
            reason = str(exc)
            continue
        if rows:
            ix = _index.Index.open()
            if ix is not None:
                with ix:
                    ix.put_shots(ctx.media_path, rows)
            captioned += 1
    missing = [c.name for c in ctxs if c.transcript is None]
    out = f"# Find Index\n\n- **Clips**: {len(ctxs)} ({len(seen)} sources)\n"
    out += f"- **Transcripts**: {with_transcript} of {len(ctxs)} clips"
    if missing:
        out += f" — missing for {', '.join(missing[:10])}; make them with `transcript_media` (never done implicitly)"
    out += f"\n- **Scenes**: {scenes_done} of {len(seen)} sources analysed\n"
    if not captions:
        out += "- **Captions**: skipped (captions=false)\n"
    elif reason:
        out += f"- **Captions**: unavailable — {reason}\n"
    elif caption_note:
        out += f"- **Captions**: {caption_note}\n"
    else:
        out += f"- **Captions**: {captioned} of {len(seen)} sources\n"
    out += "- " + _index_line(ctxs)
    if skipped:
        out += "\n\n**Skipped**: " + "; ".join(f"{n} ({why})" for n, why in skipped)
    return text_result(out)


async def handle_find_to_timeline(args: dict):
    srv = tools.server_module()
    r = _search(args)
    if not r["hits"]:
        return text_result(r["mode"] + f"\n\nNo shots matched '{r['query']}'. Nothing written.")
    sep = int(args.get("min_source_separation", 1))
    if not (0 <= sep <= 20):
        raise ValueError("min_source_separation must be between 0 and 20")
    shots = [{"source": h.source, "caption": h.why[len("looks like: "):] if h.tier == "vision" else "", "hit": h}
             for h in sorted(r["hits"], key=lambda h: (h.source, h.start))]
    kept = diversity.apply(shots, min_separation=sep, ceiling=diversity.DEFAULT_CEILING)
    filepath, output_path, gen = srv._setup_generator(args, "_found")
    selections = []
    for s in kept:
        h = s["hit"]
        ctx = r["by_name"][h.clip_name]
        dur = max(h.end - h.start, Fraction(1))
        selections.append({
            "ref": ctx.ref or "r1", "name": h.clip_name,
            "in_point": TimeValue.from_seconds(float(h.start), gen.fps),
            "use_duration": TimeValue.from_seconds(float(dur), gen.fps),
        })
    total = gen._build_output(selections, output_path, False, "00:00:00:01")
    score = diversity.score(kept)
    out = r["mode"] + "\n\n# Selects Reel\n\n"
    out += f"- **Query**: {r['query']}\n- **Shots**: {len(kept)} of {len(r['hits'])} hits kept\n"
    out += f"- **Duration**: {total:.2f}s\n- **{diversity.describe(score, kept)}**\n\n"
    out += f"Saved to: `{output_path}`"
    return text_result(out)


ACTIONS = {
    "find_index": handle_find_index,
    "find_shots": handle_find_shots,
    "find_to_timeline": handle_find_to_timeline,
}

DESCRIPTION = (
    "Natural-language shot search. find_shots (filepath, query, limit=10, "
    "visual=false, clip_name?) ranks moments by what was said (transcript), what "
    "was logged (names, keywords, notes, markers, audio events) and, when a local "
    "vision model is installed, what the frames look like — the first line of "
    "every result names which tiers answered and why one could not. find_index "
    "(captions=true, backend) warms scenes and captions and reports which clips "
    "have no transcript (it never transcribes or goes online). find_to_timeline "
    "(min_source_separation=1, output_path?) assembles the hits into a _found "
    "selects reel and reports its diversity score."
)
