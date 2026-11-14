"""Counted nouns are written out (0.33): "1 marker", never "1 markers".

0.32 swept "(s)" out of the outputs; the mark tool still answered "Added 1
markers", because an f-string that prints ``{n} markers`` reads right for
every n but one. Every count the server, its tools and the fcpxml modules
print goes through ``fcpxml.words.plural``. Two gates:

- the source: no f-string prints a count into a plural noun, straight
  (``{n} markers``) or behind up to two modifiers (``{n} connected clips``,
  ``{n} transcript words``), apart from the verbs and values listed here;
- every ``plural(n, "<noun>")`` the code calls is rendered with n = 1 and
  n = 2 and must read "1 <noun>" and "2 <nouns>".
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fcpxml.words import plural

REPO = Path(__file__).resolve().parent.parent
FILES = [REPO / "server.py", *sorted((REPO / "tools").glob("*.py")), *sorted((REPO / "fcpxml").glob("*.py"))]

# "{x} is", "{name} has", "{x} before this": the first word ending in s
# after a value, and not a counted noun
NOT_COUNTS = {"is", "has", "was", "does", "exceeds", "meets", "leaves", "overlaps", "escapes", "generates",
              "grows", "shows", "needs", "rounds", "across", "fps", "this", "vs"}
# values printed before words that are not a counted noun
NOT_A_COUNT = {
    ("keyframes.py", "name"),         # "Multiple {name} parameters are ambiguous": a parameter name
    ("media.py", "verb"),             # "{verb} the matched phrases"
    ("writer.py", "effect_name"),     # "no {effect_name} that fits"
    ("view.py", "on_clips"),          # "3 markers (2 on clips)": a share of a count already written out
}
WORDS_AFTER_VALUE = re.compile(r"^ ([a-z][a-z-]*(?: [a-z][a-z-]*){0,2})")


def raw_counts(path: Path) -> list:
    """Every f-string piece that prints a value into a plural noun: the first
    word ending in s within three words of the value."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.JoinedStr):
            continue
        for a, b in zip(node.values, node.values[1:]):
            if not (isinstance(a, ast.FormattedValue) and isinstance(b, ast.Constant) and isinstance(b.value, str)):
                continue
            m = WORDS_AFTER_VALUE.match(b.value)
            words = m.group(1).split() if m else []
            noun = next((w for w in words if w.endswith("s")), None)
            if noun is None or noun in NOT_COUNTS:
                continue
            if a.format_spec is not None:
                continue  # "{avg:.1f} frames": a measured decimal, plural either way
            value = ast.unparse(a.value)
            if (path.name, value) in NOT_A_COUNT:
                continue
            if isinstance(a.value, ast.Call) and getattr(a.value.func, "id", "") == "plural":
                continue  # "{plural(n, 'minute')} in this": already written out
            phrase = " ".join(words[:words.index(noun) + 1])
            out.append(f"{path.name}:{node.lineno}: {{{value}}} {phrase}")
    return out


def plural_calls(path: Path) -> list:
    """(singular, many) of every plural(...) call with literal nouns."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "plural":
            words = [a.value for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if words:
                out.append((path.name, node.lineno, words[0], words[1] if len(words) > 1 else ""))
    return out


def test_no_count_is_printed_straight_into_a_plural_noun():
    assert [hit for f in FILES for hit in raw_counts(f)] == []


def test_the_scanner_sees_one_markers(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(
        'n = 1\n'
        'text = f"Added {n} markers"\n'
        'ok = f"{n} is fine"\n'
        'lanes = f"the {n} connected clips in lanes"\n'
        'words = f"{n} transcript words."\n'
        'fine = f"{n} clip in this"\n'
    )
    assert raw_counts(planted) == [
        "planted.py:2: {n} markers",
        "planted.py:4: {n} connected clips",
        "planted.py:5: {n} transcript words",
    ]


def test_every_counted_noun_reads_right_at_one_and_two():
    calls = [c for f in FILES for c in plural_calls(f)]
    assert len(calls) >= 50, len(calls)
    for name, line, single, many in calls:
        assert plural(1, single, many) == f"1 {single}", (name, line)
        assert plural(2, single, many) == f"2 {many or single + 's'}", (name, line)
        assert not single.endswith("s"), (name, line, single)  # the singular is singular
