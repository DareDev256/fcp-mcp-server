"""Pack every transcript in a timeline into one page a model can read.

The pack is a planning document, not a caption file. One header per
source, one line per utterance, and an utterance ends where the speaker
changes or the silence between words reaches ``gap`` seconds. Audio
events (laughter, applause — only a diarizing backend produces them)
land inline at their own time so the reader sees them where they
happened.

Sizes are measured in BYTES, not characters. A 60KB pack is a fixed
token budget only if the measurement is the one the wire uses.
"""

from __future__ import annotations

PACK_LIMIT_BYTES = 60_000
DEFAULT_GAP = 0.5


def pack_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _fmt(start: float, end: float) -> str:
    return f"[{start:.2f}-{end:.2f}]"


def _utterances(words: list[dict], gap: float) -> list[tuple[float, float, str | None, str]]:
    out: list[tuple[float, float, str | None, str]] = []
    cur: list[dict] = []

    def flush() -> None:
        if not cur:
            return
        out.append((
            float(cur[0]["start"]),
            float(cur[-1]["end"]),
            cur[0].get("speaker"),
            " ".join(str(w["word"]).strip() for w in cur if str(w["word"]).strip()),
        ))
        cur.clear()

    for w in words:
        if cur:
            silence = float(w["start"]) - float(cur[-1]["end"])
            if silence >= gap or w.get("speaker") != cur[0].get("speaker"):
                flush()
        cur.append(w)
    flush()
    return out


def pack(sources: list[dict], gap: float = DEFAULT_GAP) -> str:
    """Render *sources* — ``[{"name", "words", "events"?}]`` — as one document."""
    parts: list[str] = []
    for src in sorted(sources, key=lambda s: str(s.get("name", ""))):
        words = sorted(src.get("words") or [], key=lambda w: float(w["start"]))
        lines: list[tuple[float, str]] = []
        for start, end, speaker, text in _utterances(words, gap):
            if not text:
                continue
            tag = f"{speaker} " if speaker else ""
            lines.append((start, f"{_fmt(start, end)} {tag}{text}"))
        for ev in src.get("events") or []:
            start, end = float(ev["start"]), float(ev["end"])
            lines.append((start, f"{_fmt(start, end)} ({ev.get('label', 'event')})"))
        lines.sort(key=lambda item: item[0])
        body = "\n".join(text for _, text in lines) if lines else "(no speech)"
        parts.append(f"# {src.get('name', '?')}\n{body}")
    return "\n\n".join(parts) + ("\n" if parts else "")


def truncate(text: str, limit: int = PACK_LIMIT_BYTES) -> str:
    """Cut *text* at the last whole line under *limit* bytes and say so."""
    total = pack_size(text)
    if total <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        size = pack_size(line) + 1
        if used + size > limit:
            break
        kept.append(line)
        used += size
    kept.append(f"… truncated (pack is {total} bytes; wrote nothing beyond this line)")
    return "\n".join(kept)
