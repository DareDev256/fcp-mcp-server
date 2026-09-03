from fractions import Fraction as Fr

from fcpxml import find

WORDS = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(
    "so the budget was the real problem and we fixed the budget by cutting scope".split())]


def test_tokens_strip_stopwords_and_plurals():
    assert find.tokens("The budgets were Problems!") == ["budget", "problem"]
    assert find.tokens("glass grass") == ["glass", "grass"]


def test_transcript_hits_rank_the_phrase_first():
    hits = find.transcript_hits(WORDS, "budget problem", source="/a.mov", clip_name="A")
    assert hits and hits[0].tier == "transcript" and hits[0].score > 0.5
    assert Fr(1, 2) <= hits[0].start <= Fr(3, 2) and hits[0].why.startswith("said:")
    assert all(isinstance(h.start, Fr) for h in hits)


def test_transcript_no_match_is_empty():
    assert find.transcript_hits(WORDS, "sunset beach", source="/a.mov", clip_name="A") == []
    assert find.transcript_hits(WORDS, "the", source="/a.mov", clip_name="A") == []
    assert find.transcript_hits([], "budget", source="/a.mov", clip_name="A") == []


def test_metadata_hits_fields_and_ranges():
    hits = find.metadata_hits(
        {"name": "Beach Wide", "keywords": "sunset, b-roll", "notes": ""},
        [(Fr(2), Fr(4), "marker: sunset flare"), (Fr(10), Fr(12), "event: laughter")],
        "sunset", source="/b.mov", clip_name="Beach Wide", clip_start=Fr(0), clip_end=Fr(30))
    whys = {h.why.split(":")[0] for h in hits}
    assert whys == {"keyword", "marker"} and all(h.tier == "metadata" for h in hits)
    assert any(h.start == Fr(2) and h.end == Fr(4) for h in hits)


def test_rank_orders_dedupes_and_limits():
    a = find.Hit("/a", "A", Fr(0), Fr(5), 0.5, "metadata", "keyword: x")
    b = find.Hit("/a", "A", Fr(1), Fr(4), 0.9, "transcript", "said: x")
    c = find.Hit("/b", "B", Fr(0), Fr(5), 0.9, "vision", "looks like: x")
    out = find.rank([a, b, c], limit=5)
    assert out[0] is b and out[1] is c and a not in out
    assert find.rank([a, b, c], limit=1) == [b]


def test_is_visual():
    assert find.is_visual("wide shot of the beach") and not find.is_visual("budget problem")
    assert not find.is_visual("wide shot of the beach", transcript_text="we went for a wide shot")
