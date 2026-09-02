"""The ``find`` ranking core. Pure — no I/O, no index, no models.

Three tiers answer "where is the bit where…": what was SAID (transcript
words), what was LOGGED (names, keywords, notes, markers, events), and what
it LOOKS LIKE (shot captions, when a vision model has run). Each tier
produces ``Hit``s in SOURCE seconds as Fractions; ``rank`` merges them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

TIERS = ("transcript", "metadata", "vision")

_STOP = {
    "a", "an", "the", "of", "on", "in", "at", "and", "with", "to", "is", "are",
    "was", "were", "be", "it", "that", "this", "for", "where", "when", "he", "she",
    "they", "we", "i", "you", "so", "by", "or", "as", "bit", "part", "moment",
}
_VISUAL = {
    "shot", "wide", "close", "closeup", "frame", "looks", "looking", "colour", "color",
    "red", "blue", "green", "dark", "bright", "outside", "indoor", "outdoor", "sky",
    "face", "hand", "hands", "walking", "sitting", "standing",
}


@dataclass
class Hit:
    source: str
    clip_name: str
    start: Fraction
    end: Fraction
    score: float
    tier: str
    why: str


def _f(x) -> Fraction:
    return Fraction(x).limit_denominator(1_000_000)


def tokens(text: str) -> list[str]:
    """Lowercase word tokens, stop-words removed, a trailing plural ``s`` stripped."""
    out = []
    for w in re.findall(r"[a-z0-9']+", (text or "").lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _overlaps(a: Hit, b: Hit) -> bool:
    return a.source == b.source and a.start < b.end and b.start < a.end


def _merge_overlaps(hits: list[Hit]) -> list[Hit]:
    """Keep the higher-scoring of any two overlapping hits on one source."""
    kept: list[Hit] = []
    for h in sorted(hits, key=lambda h: (-h.score, TIERS.index(h.tier), h.start)):
        if not any(_overlaps(h, k) for k in kept):
            kept.append(h)
    return kept


def transcript_hits(
    words: list[dict], query: str, *, source: str, clip_name: str,
    window: float = 6.0, min_score: float = 0.34,
) -> list[Hit]:
    """Slide a *window* over the words; score = share of query tokens present.

    A verbatim phrase earns +0.25. Overlapping windows collapse to the best.
    """
    q = tokens(query)
    if not q or not words:
        return []
    qset, phrase = set(q), " ".join(q)
    hits: list[Hit] = []
    n = len(words)
    for i in range(n):
        j = i
        while j < n and float(words[j]["end"]) - float(words[i]["start"]) <= window:
            j += 1
        span = words[i:j] or words[i:i + 1]
        text = " ".join(str(w["word"]) for w in span)
        toks = tokens(text)
        score = len(qset & set(toks)) / len(qset)
        if phrase in " ".join(toks):
            score = min(1.0, score + 0.25)
        if score < min_score:
            continue
        # The hit spans the matched words, not the whole window.
        matched = [w for w in span if qset & set(tokens(str(w["word"])))]
        hits.append(Hit(source, clip_name, _f(matched[0]["start"]), _f(matched[-1]["end"]),
                        score, "transcript", "said: " + text[:80]))
    return sorted(_merge_overlaps(hits), key=lambda h: (-h.score, h.start))


def metadata_hits(
    fields: dict[str, str], ranges: list[tuple[Fraction, Fraction, str]], query: str, *,
    source: str, clip_name: str, clip_start: Fraction, clip_end: Fraction,
) -> list[Hit]:
    """Match *fields* (whole clip) and labelled *ranges* (markers, events) by token overlap."""
    qset = set(tokens(query))
    if not qset:
        return []
    hits: list[Hit] = []
    for field in ("keywords", "name", "notes"):
        value = fields.get(field) or ""
        present = qset & set(tokens(value))
        if present:
            label = "keyword" if field == "keywords" else field
            hits.append(Hit(source, clip_name, clip_start, clip_end, len(present) / len(qset),
                            "metadata", f"{label}: {value[:80]}"))
    for start, end, label in ranges:
        present = qset & set(tokens(label))
        if present:
            hits.append(Hit(source, clip_name, _f(start), _f(end), len(present) / len(qset),
                            "metadata", label[:80]))
    return hits


def rank(hits: list[Hit], limit: int) -> list[Hit]:
    """Best first: score, then tier order, then position; overlaps on one source collapse."""
    return _merge_overlaps(hits)[:limit]


def is_visual(query: str, transcript_text: str = "") -> bool:
    """Worth asking the vision tier: a visual cue and nothing said matches."""
    q = set(tokens(query))
    if not q & _VISUAL:
        return False
    return not (q & set(tokens(transcript_text)))
