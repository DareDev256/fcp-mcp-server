"""Counted nouns written out: "1 transition", "2 markers", never "transition(s)".

A template plural reads as machine output and makes a reader do the
grammar; every count the server prints goes through :func:`plural`.
"""


def plural(n: int, word: str, many: str = "") -> str:
    """``n`` and ``word``, the word made plural unless ``n`` is 1:
    plural(1, "marker") -> "1 marker", plural(2, "marker") -> "2 markers"."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def noun(n: int, word: str, many: str = "") -> str:
    """The word alone, singular for 1: noun(2, "argument") -> "arguments"."""
    return word if n == 1 else (many or word + "s")
