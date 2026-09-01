"""transcript_pack — the whole shoot on one page.

The pack is what a model reads to plan an edit, so its shape matters
more than its prose: one header per source, one line per utterance,
utterances broken on silence or a speaker change, audio events inline.
"""

from fcpxml.transcript_pack import PACK_LIMIT_BYTES, pack, pack_size, truncate


def _w(word, start, end, speaker=None):
    d = {"word": word, "start": start, "end": end}
    if speaker is not None:
        d["speaker"] = speaker
    return d


def test_header_per_source_in_name_order():
    text = pack([
        {"name": "b.mov", "words": [_w("late", 0.0, 0.5)]},
        {"name": "a.mov", "words": [_w("early", 0.0, 0.5)]},
    ])
    assert text.index("# a.mov") < text.index("# b.mov")
    assert "[0.00-0.50] early" in text
    assert "[0.00-0.50] late" in text


def test_speaker_change_breaks_the_line():
    text = pack([{
        "name": "two.mov",
        "words": [_w("hi", 0.0, 0.3, "S0"), _w("there", 0.3, 0.6, "S0"),
                  _w("hello", 0.7, 1.0, "S1")],
    }])
    lines = [ln for ln in text.splitlines() if ln.startswith("[")]
    assert lines == ["[0.00-0.60] S0 hi there", "[0.70-1.00] S1 hello"]


def test_gap_at_threshold_breaks_and_under_does_not():
    words = [_w("one", 0.0, 0.5), _w("two", 0.9, 1.2), _w("three", 1.8, 2.0)]
    lines = [ln for ln in pack([{"name": "m", "words": words}]).splitlines()
             if ln.startswith("[")]
    # 0.4s between one and two: same line. 0.6s before three: new line.
    assert lines == ["[0.00-1.20] one two", "[1.80-2.00] three"]


def test_gap_parameter_is_honoured():
    words = [_w("one", 0.0, 0.5), _w("two", 0.9, 1.2)]
    lines = [ln for ln in pack([{"name": "m", "words": words}], gap=0.3).splitlines()
             if ln.startswith("[")]
    assert len(lines) == 2


def test_no_speaker_omits_the_tag():
    text = pack([{"name": "m", "words": [_w("solo", 0.0, 0.4)]}])
    assert "[0.00-0.40] solo" in text
    assert " S0 " not in text


def test_events_are_inlined_in_time_order():
    text = pack([{
        "name": "m",
        "words": [_w("joke", 0.0, 0.5), _w("next", 3.0, 3.5)],
        "events": [{"start": 1.0, "end": 2.0, "label": "laughter"}],
    }])
    body = [ln for ln in text.splitlines() if ln.startswith("[")]
    assert body == ["[0.00-0.50] joke", "[1.00-2.00] (laughter)", "[3.00-3.50] next"]


def test_empty_words_says_no_speech():
    text = pack([{"name": "silent.mov", "words": []}])
    assert "# silent.mov" in text
    assert "(no speech)" in text


def test_pack_size_counts_bytes_not_characters():
    assert pack_size("é") == 2
    assert pack_size("abc") == 3


def test_truncate_keeps_whole_lines_and_says_so():
    text = "\n".join(f"[{i}.00-{i}.50] word{i}" for i in range(2000))
    out = truncate(text, limit=500)
    assert pack_size(out) <= 500 + 80  # the note is allowed past the limit
    assert out.endswith("… truncated (pack is %d bytes; wrote nothing beyond this line)" % pack_size(text))
    assert all(ln.startswith("[") or ln.startswith("…") for ln in out.splitlines())


def test_truncate_is_identity_under_limit():
    assert truncate("short", limit=PACK_LIMIT_BYTES) == "short"
