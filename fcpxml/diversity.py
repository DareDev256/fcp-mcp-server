"""The diversity constraint on assembled sequences.

Pure. A list of shot dicts goes in — each with a ``source`` (media path or
asset ref) and, once captions exist, a ``caption`` — and a filtered list or a
score comes out. Assemblies apply it and REPORT it, so a montage that reuses
one take every other cut says so in a number.
"""

from __future__ import annotations

import re

DEFAULT_CEILING = 0.6
_STOP = {"a", "an", "the", "of", "on", "in", "at", "and", "with", "to", "is", "are", "shot"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    """Jaccard over word sets. The embedding hook: same signature, other body."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _too_similar(a: dict, b: dict, ceiling: float) -> bool:
    return bool(a.get("caption") and b.get("caption")) and similarity(a["caption"], b["caption"]) > ceiling


def apply(shots: list[dict], min_separation: int = 0, ceiling: float | None = None) -> list[dict]:
    """Greedy left-to-right filter.

    A shot is dropped when its ``source`` appeared within the last
    ``min_separation`` KEPT shots, or when ``ceiling`` is set and its caption
    is more similar than that to the previous kept shot's. Shots without a
    caption are never judged on it. Defaults pass everything through.
    """
    kept: list[dict] = []
    for shot in shots:
        recent = [s.get("source") for s in kept[-min_separation:]] if min_separation > 0 else []
        if shot.get("source") in recent:
            continue
        if ceiling is not None and kept and _too_similar(kept[-1], shot, ceiling):
            continue
        kept.append(shot)
    return kept


def score(shots: list[dict]) -> float:
    """1 − (adjacent pairs sharing a source or a near-duplicate caption) / pairs."""
    if len(shots) < 2:
        return 1.0
    violations = sum(
        1 for prev, cur in zip(shots, shots[1:])
        if prev.get("source") == cur.get("source") or _too_similar(prev, cur, DEFAULT_CEILING)
    )
    return 1.0 - violations / (len(shots) - 1)


def describe(value: float, shots: list[dict] | None = None) -> str:
    if shots is not None and len(shots) >= 2:
        pairs = len(shots) - 1
        return f"Diversity: {value:.2f} ({round(value * pairs)} of {pairs} cuts change source)"
    return f"Diversity: {value:.2f}"
