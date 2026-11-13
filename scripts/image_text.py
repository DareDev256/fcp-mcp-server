"""What may not be printed into an image: numbers that go stale the day it ships.

The first social card printed a version and a star count that stayed wrong
for seventeen releases; the 0.31 first-pass hero printed a model id, a
price and a frame size. An image cannot be updated by a release, so the
image builders (``demo/hero/build.py`` + ``record.js``, ``scripts/social_card.py``)
check the text they are about to bake in against these patterns and REFUSE
on a match. ``tests/test_front_door.py`` holds the patterns themselves.

Two scopes, because the timeline app inside the hero is real data: its
header shows the demo sequence's own frame size, which is a fact about that
timeline, not about this release. Host text (the chat, the captions, the
card copy) is held to every pattern; the app's text to all but frame size.
"""

from __future__ import annotations

import re

STALE: dict[str, str] = {
    "a price": r"\$\s?\d",
    "a model or provider id": r"\b(?:kling|wan|omni|veo|aleph|ray)-[\w.]+|\b(?:fal|gemini|runway|luma)/[\w.-]+",
    "a frame size": r"\b\d{3,4}\s?[×x]\s?\d{3,4}\b",
    "a version": r"\bv?\d+\.\d+\.\d+\b",
    "a star count": r"\b\d[\d,]*\s+stars?\b",
}
APP_EXEMPT = ("a frame size",)


def patterns(scope: str = "host") -> dict[str, str]:
    """The regex sources for one scope, for the JavaScript side too."""
    return {k: v for k, v in STALE.items() if scope == "host" or k not in APP_EXEMPT}


def stale_in(text: str, scope: str = "host") -> list[str]:
    """Every stale-prone match in *text*, as 'what: matched text'."""
    found = []
    for label, src in patterns(scope).items():
        found += [f"{label}: {m.group(0)}" for m in re.finditer(src, text, flags=re.I)]
    return found
