"""The timeline app moves between payloads; it never dissolves (0.31, round 5).

A re-render after an edit used to replace the picture, and the README hero
cross-faded between whole re-renders: for 200 ms two layouts showed at once
("SqSquat", "PresPress", two ruler times stacked). Now the app animates a new
payload from the old one the way Final Cut closes a gap: the pieces of a
split clip start where that footage sat and slide together, the clips after
them slide left, a new clip is laid in from its left edge, and a new marker
drops onto the ruler. Only position and size animate, never opacity.

Also here: a spine with nothing above it keeps an empty lane-1 row, so a clip
laid on lane 1 does not push the spine down; and a ruler tick whose time does
not fit is dropped whole (critic round 4: the 30 s tick showed with no label).

Rendered in headless Chromium; the `rendered-labels` CI job runs it.
"""

from __future__ import annotations

import copy
import os
from fractions import Fraction

import pytest

from fcpxml import timeline_app

REQUIRED = os.environ.get("FCP_MCP_REQUIRE_BROWSER") == "1"
if REQUIRED:
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")

MOTION_MS = 400


def r(x: Fraction) -> str:
    return f"{x.numerator}/{x.denominator}"


def _clip(i: int, name: str, start: Fraction, dur: Fraction) -> dict:
    return {"id": f"s{i}", "name": name, "lane": 0, "kind": "spine", "type": "asset-clip",
            "start": r(start), "duration": r(dur), "end": r(start + dur), "source_start": "0/1",
            "file_start": "0/1", "media": f"/media/{name.lower()}.mov", "role": "", "keywords": [],
            "markers": [], "words": [], "thumb": None}


def _base() -> dict:
    """Bench 5 s, a 14 s Squat, Press 5 s, Finish 5 s: the hero's shape."""
    clips, t = [], Fraction(0)
    for i, (name, dur) in enumerate((("Bench", 5), ("Squat", 14), ("Press", 5), ("Finish", 5))):
        clips.append(_clip(i, name, t, Fraction(dur)))
        t += dur
    return {"schema": timeline_app.PAYLOAD_SCHEMA, "file": {"path": "/tmp/cut.fcpxml", "sha256": "0" * 64},
            "name": "Session cut", "fps": "24/1", "fps_label": "24", "nominal_fps": 24, "width": 1920,
            "height": 1080, "origin": "0/1", "duration": r(t), "gap_based": False,
            "lanes": [{"lane": 0, "kind": "spine", "clips": [c["id"] for c in clips]}],
            "clips": clips, "gaps": [], "markers": [], "transitions": []}


def _split(p: dict, index: int = 1, keep=(Fraction(3), Fraction(4)), rest=Fraction(2)) -> dict:
    """The spine clip at *index* cut into two pieces with *rest* seconds of it
    removed between them; everything after it ripples left by *rest*."""
    q = copy.deepcopy(p)
    spine = sorted((c for c in q["clips"] if c["kind"] == "spine"), key=lambda c: Fraction(c["start"]))
    target = spine[index]
    start, src = Fraction(target["start"]), Fraction(target["source_start"])
    a, b = copy.deepcopy(target), copy.deepcopy(target)
    a.update(id=target["id"] + "a", duration=r(keep[0]), end=r(start + keep[0]))
    b.update(id=target["id"] + "b", start=r(start + keep[0]), duration=r(keep[1]), end=r(start + keep[0] + keep[1]),
             source_start=r(src + keep[0] + rest))
    shift = Fraction(target["duration"]) - keep[0] - keep[1]
    for c in spine[index + 1:]:
        c["start"], c["end"] = r(Fraction(c["start"]) - shift), r(Fraction(c["end"]) - shift)
    q["clips"] = [c for c in q["clips"] if c is not target] + [a, b]
    for lane in q["lanes"]:
        if target["id"] in lane["clips"]:
            i = lane["clips"].index(target["id"])
            lane["clips"][i:i + 1] = [a["id"], b["id"]]
    q["duration"] = r(Fraction(q["duration"]) - shift)
    return q


def _with_lane_one(p: dict) -> dict:
    """A connected clip on lane 1 over the second spine clip, with a marker."""
    q = copy.deepcopy(p)
    spine = sorted((c for c in q["clips"] if c["kind"] == "spine"), key=lambda c: Fraction(c["start"]))
    at = Fraction(spine[1]["start"]) + 1
    q["clips"].append({"id": "c0", "name": "B-roll", "lane": 1, "kind": "video", "type": "asset-clip",
                       "start": r(at), "duration": "4/1", "end": r(at + 4), "source_start": "0/1",
                       "file_start": "0/1", "media": "/media/broll.mov", "role": "", "keywords": [],
                       "markers": [{"name": "b-roll", "start": r(at), "kind": "standard"}], "words": [], "thumb": None})
    q["lanes"].insert(0, {"lane": 1, "kind": "video", "clips": ["c0"]})
    return q


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:
            if REQUIRED:
                raise
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


def _page(browser, width=1440, reduced=False):
    page = browser.new_page(viewport={"width": width, "height": 900},
                            reduced_motion="reduce" if reduced else "no-preference")
    page.set_content(timeline_app.render_app_html())
    return page


# Set and pause in ONE evaluate: under load the 400 ms can pass between two
# round trips, and a finished animation is no longer in getAnimations().
SET = "p => { window.__fcpApp.setPayload(p); document.getAnimations().forEach(a => { a.pause(); a.currentTime = 0; }); }"
SEEK = """ms => { const all = document.getAnimations(); all.forEach(a => { a.pause(); a.currentTime = ms; }); return all.length; }"""
CLIPS = """() => [...document.querySelectorAll('#canvas .clip')].map(e => ({
  name: e.__c.name, src: e.__c.source_start, left: parseFloat(getComputedStyle(e).left), width: e.getBoundingClientRect().width }))"""


def test_a_split_clip_starts_where_its_footage_sat_and_closes_the_gap(browser):
    a = _base()
    b = _split(a)
    page = _page(browser)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        old = {c["name"]: c for c in page.evaluate(CLIPS)}
        pps = page.evaluate("() => window.__fcpApp.state.pps")
        page.evaluate(SET, b)
        assert page.evaluate(SEEK, 0) > 0, "no motion at all"
        start = page.evaluate(CLIPS)
        page.evaluate(SEEK, MOTION_MS)
        end = page.evaluate(CLIPS)
    finally:
        page.close()
    pieces = sorted((c for c in start if c["name"] == "Squat"), key=lambda c: Fraction(c["src"]))
    assert len(pieces) == 2
    rest_start = old["Squat"]["left"]
    # piece one where the clip began; piece two where ITS footage sat, keep[0] + rest later
    assert abs(pieces[0]["left"] - rest_start) < 1
    assert abs(pieces[1]["left"] - (rest_start + 5 * pps)) < 1
    final = sorted((c for c in end if c["name"] == "Squat"), key=lambda c: Fraction(c["src"]))
    gap = lambda two: two[1]["left"] - (two[0]["left"] + two[0]["width"])  # noqa: E731
    assert gap(pieces) > 2 * pps - 2, "the rest was not there at the start"
    assert gap(final) < 2, "the gap did not close"


def motion_problems(page) -> list[str]:
    """Every running animation moves or resizes; none fades (a fade is two layouts at once)."""
    return page.evaluate("""() => document.getAnimations().flatMap(a => a.effect.getKeyframes()
        .filter(k => 'opacity' in k || 'filter' in k).map(k => 'fades ' + (a.effect.target.className || '?')))""")


def test_motion_never_fades(browser):
    a = _base()
    page = _page(browser)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        page.evaluate(SET, _split(a))
        problems = motion_problems(page)
        page.evaluate(SET, _with_lane_one(_split(a)))
        problems += motion_problems(page)
        kinds = page.evaluate("() => document.getAnimations().length")
    finally:
        page.close()
    assert kinds > 0
    assert problems == []


def test_the_fade_check_goes_red(browser):
    page = _page(browser)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", _base())
        page.evaluate("() => document.querySelector('#canvas .clip').animate([{opacity: 0}, {opacity: 1}], 400)")
        assert motion_problems(page) != []
    finally:
        page.close()


def _with_two_markers(p: dict) -> dict:
    """The lane-one cut plus a second new marker, on Finish (to its right)."""
    q = _with_lane_one(p)
    fin = next(c for c in q["clips"] if c["name"] == "Finish")
    fin["markers"] = [{"name": "Finish", "start": fin["start"], "kind": "chapter"}]
    return q


NAMES = """() => {
  const vis = (e) => getComputedStyle(e).visibility !== 'hidden';
  const clip = [...document.querySelectorAll('#canvas .clip')].find(e => e.__c.name === 'B-roll');
  return { width: clip.getBoundingClientRect().width, name: vis(clip.querySelector('.label')),
           flags: [...document.querySelectorAll('.mlabel')].filter(vis).map(e => e.textContent) };
}"""


def test_a_new_clip_is_laid_in_and_new_markers_land_whole_in_turn(browser):
    a = _split(_base())
    page = _page(browser)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        page.evaluate(SET, _with_two_markers(a))
        page.evaluate(SEEK, 0)
        start = page.evaluate(NAMES)
        steps = page.evaluate("""() => [...document.querySelectorAll('.mlabel')].flatMap(e => e.getAnimations()
            .flatMap(a => a.effect.getKeyframes().flatMap(k => Object.keys(k).filter(p => !['offset', 'easing', 'composite', 'computedOffset'].includes(p)))))""")
        page.evaluate(SEEK, MOTION_MS + 200)
        end = page.evaluate(NAMES)
    finally:
        page.close()
    # the tile grows from nothing, its name hidden until it is whole
    assert start["width"] < 3 < end["width"]
    assert start["name"] is False and end["name"] is True
    # the first marker lands at once, the one to its right in turn; whole, never part-drawn
    assert "b-roll" in start["flags"] and "Finish" not in start["flags"]
    assert {"b-roll", "Finish"} <= set(end["flags"])
    assert set(steps) == {"visibility"}


def test_reduced_motion_jumps(browser):
    a = _base()
    page = _page(browser, reduced=True)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        page.evaluate(SET, _split(a))
        n = page.evaluate("() => document.getAnimations().length")
    finally:
        page.close()
    assert n == 0


SPINE_TOP = "() => document.querySelector('.lane.spine').getBoundingClientRect().top"


def test_the_spine_stays_put_when_lane_one_fills(browser):
    """A spine-only cut keeps an empty lane-1 row, so a clip laid there does
    not push the spine (or the panels under it) down."""
    a = _split(_base())
    page = _page(browser)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        before = page.evaluate(SPINE_TOP)
        reserved = page.evaluate("() => document.querySelectorAll('.lane.reserved').length")
        page.evaluate(SET, _with_lane_one(a))
        after = page.evaluate(SPINE_TOP)
        reserved_after = page.evaluate("() => document.querySelectorAll('.lane.reserved').length")
    finally:
        page.close()
    assert reserved == 1 and reserved_after == 0
    assert abs(before - after) < 0.5


VISIBLE_TICKS = """() => [...document.querySelectorAll('.tick')].filter(t => getComputedStyle(t).visibility !== 'hidden')
  .map(t => { const r = document.createRange(); r.selectNodeContents(t); const b = r.getBoundingClientRect();
              const s = document.getElementById('scroller').getBoundingClientRect();
              return { text: t.textContent, line: t.getBoundingClientRect().left, inside: b.right <= s.right - 1 + 0.5 && b.width > 0 }; })"""


@pytest.mark.parametrize("width", [1440, 390])
def test_every_ruler_tick_shows_its_time_or_is_gone(browser, width):
    """The critic's case: a 30 s tick near the end of a 30.7 s cut drew its
    line with no label. A tick is its time; one that cannot show it goes."""
    p = _base()
    p["duration"] = "737/24"  # 30.708 s: a 30 s tick lands at the very end
    page = _page(browser, width)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", p)
        ticks = page.evaluate(VISIBLE_TICKS)
        unfit_visible = page.evaluate("() => [...document.querySelectorAll('.tick.unfit')].filter(t => getComputedStyle(t).visibility !== 'hidden').length")
    finally:
        page.close()
    assert ticks, "no ruler at all"
    assert all(t["inside"] for t in ticks), ticks
    assert unfit_visible == 0
