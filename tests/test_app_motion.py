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


ALPHA = """(e) => { const m = getComputedStyle(e).color.match(/rgba?\\(([^)]+)\\)/); if (!m) return 1;
  const v = m[1].split(',').map(Number); return v.length > 3 ? v[3] : 1; }"""
NAMES = """() => {
  const vis = (e) => getComputedStyle(e).visibility !== 'hidden', alpha = %s;
  const clip = [...document.querySelectorAll('#canvas .clip')].find(e => e.__c.name === 'B-roll' && !e.classList.contains('ghost'));
  return { width: clip.getBoundingClientRect().width, name: alpha(clip.querySelector('.label')) > 0.99,
           flags: [...document.querySelectorAll('.mlabel')].filter(vis).map(e => e.textContent) };
}""" % ALPHA


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


# ---------------------------------------------------------------------------
# Round 6: the scrim stays, names fade in once they fit, thumbnails tile,
# the playhead never cuts a ruler time, ticks sit on whole timecode seconds,
# and an undo moves back.
# ---------------------------------------------------------------------------

# A 16:9 thumbnail, black on the left half and white on the right.
HALVES = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='90'>"
          "<rect width='80' height='90' fill='black'/><rect x='80' width='80' height='90' fill='white'/></svg>")


def _thumbed(p: dict) -> dict:
    q = copy.deepcopy(p)
    for c in q["clips"]:
        c["thumb"] = HALVES
    return q


FRAME_PROBLEMS = """(prev) => {
  const alpha = %s, out = [], now = {};
  document.querySelectorAll('#canvas .clip').forEach(c => {
    const lab = c.querySelector('.label'); if (!lab || lab.classList.contains('unfit')) return;
    const r = document.createRange(); r.selectNodeContents(lab);
    const need = r.getBoundingClientRect().width + 14, w = c.getBoundingClientRect().width;
    const key = c.__c.id, fits = w >= need - 0.5, a = alpha(lab);
    if (c.classList.contains('ghost')) {
      // on its way out: its name hidden, or whole
      const hidden = getComputedStyle(lab).visibility === 'hidden' || getComputedStyle(c).visibility === 'hidden' || a <= 0.01;
      if (!hidden && !fits) out.push('a cut name on ' + key + ' (leaving, ' + w.toFixed(1) + ' < ' + need.toFixed(1) + ')');
      return;
    }
    now[key] = fits;
    if (c.classList.contains('has-thumb')) {
      const cs = getComputedStyle(lab);
      if (cs.visibility === 'hidden' || !cs.backgroundImage.includes('gradient')) out.push('no scrim on ' + key);
    }
    if (a > 0.01 && !fits) out.push('a cut name on ' + key + ' (' + w.toFixed(1) + ' < ' + need.toFixed(1) + ')');
    if (fits && prev[key] && a < 0.01) out.push('a dropped name on ' + key);
  });
  return [out, now];
}""" % ALPHA


def label_problems_through_motion(page, step_ms: int = 30) -> list[str]:
    """Every name, frame by frame through the motion: scrim drawn, never shown
    cut, never still invisible one frame after its clip got wide enough."""
    out, prev = [], {}
    for ms in range(0, MOTION_MS + 160, step_ms):
        page.evaluate(SEEK, ms)
        problems, prev = page.evaluate(FRAME_PROBLEMS, prev)
        out += [f"{ms} ms: {x}" for x in problems]
    return out


def test_names_keep_their_scrim_and_fade_in_once_they_fit(browser):
    a = _thumbed(_base())
    page = _page(browser, width=390)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        page.evaluate(SET, _thumbed(_with_lane_one(_split(_base()))))
        problems = label_problems_through_motion(page)
    finally:
        page.close()
    assert problems == []


def test_the_name_check_goes_red_on_a_hidden_label(browser):
    """Mutation check: the round-5 way (the whole label, scrim and all, hidden
    until the motion settles) fails it."""
    a = _thumbed(_base())
    page = _page(browser, width=390)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", a)
        page.evaluate(SET, _thumbed(_split(a)))
        page.evaluate("""() => document.querySelectorAll('#canvas .clip .label').forEach(l => {
            l.getAnimations().forEach(x => x.cancel());
            const k = l.animate([{visibility: 'hidden'}, {visibility: 'hidden', offset: 0.999}, {visibility: 'visible'}], 400); k.pause(); })""")
        problems = label_problems_through_motion(page)
    finally:
        page.close()
    assert any("no scrim" in x for x in problems)


TILE = """() => {
  const c = [...document.querySelectorAll('#canvas .clip')].find(e => e.__c.name === 'Squat');
  const r = c.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height };
}"""


def runs(png: bytes) -> list[int]:
    """Lengths of the dark and light runs along a clip's middle row, without
    the two at the edges (they are cut by the clip, wherever the tiles start)."""
    from io import BytesIO

    from PIL import Image
    im = Image.open(BytesIO(png)).convert("L")
    row = [im.getpixel((x, im.height // 2)) > 128 for x in range(im.width)]
    out, n = [], 1
    for a, b in zip(row, row[1:]):
        if a == b:
            n += 1
        else:
            out.append(n)
            n = 1
    out.append(n)
    return out[1:-1]


def _tile_shot(browser, extra_css: str = ""):
    p = _thumbed(_base())
    page = _page(browser, width=1440)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", p)
        page.add_style_tag(content=".clip .label, .playhead, .mk { visibility: hidden !important; } "
                           ".clip .thumbs { opacity: 1 !important; }" + extra_css)
        box = page.evaluate(TILE)
        shot = page.screenshot(clip={"x": box["x"] + 2, "y": box["y"] + 2, "width": box["w"] - 4, "height": box["h"] - 4})
    finally:
        page.close()
    return box, shot


def test_a_long_clip_tiles_its_thumbnail_at_its_own_aspect(browser):
    """A clip 3x wider than its thumbnail repeats it, 16:9 at the clip's height:
    every whole dark or light run is height x 16/9 / 2 wide. Stretched, or one
    frame blown up to cover the clip, there is no whole run at all."""
    pytest.importorskip("PIL")
    box, shot = _tile_shot(browser)
    assert box["w"] > 3 * box["h"] * 16 / 9, box
    half = (box["h"] - 2) * 16 / 9 / 2  # inside the clip's 1px borders
    inner = runs(shot)
    assert len(inner) >= 4 and all(abs(r - half) < 3 for r in inner), (inner, half)


def test_the_tile_check_sees_a_stretched_thumbnail(browser):
    """Mutation check: one frame stretched over the clip fails it (one dark
    half, one light half: no whole run of the right length)."""
    pytest.importorskip("PIL")
    box, shot = _tile_shot(browser, " .clip .thumbs { background-size: 100% 100% !important; }")
    half = (box["h"] - 2) * 16 / 9 / 2
    inner = runs(shot)
    assert not (len(inner) >= 4 and all(abs(r - half) < 3 for r in inner)), inner


CUTS_A_TIME = """() => {
  const ph = document.getElementById('playhead'), parts = [ph.getBoundingClientRect(), ph.querySelector('.phead').getBoundingClientRect()];
  return [...document.querySelectorAll('.tick')].filter(t => getComputedStyle(t).visibility !== 'hidden').filter(t => {
    const r = document.createRange(); r.selectNodeContents(t); const b = r.getBoundingClientRect();
    return parts.some(q => q.left < b.right && b.left < q.right && q.top < b.bottom && b.top < q.bottom); }).map(t => t.textContent);
}"""


def _playhead_inside_first_time(page) -> None:
    page.evaluate("""() => { const t = document.querySelector('.tick'), r = document.createRange(); r.selectNodeContents(t);
      const b = r.getBoundingClientRect(), c = document.getElementById('canvas').getBoundingClientRect(), st = window.__fcpApp.state;
      st.playhead = ((b.left + b.right) / 2 - c.left - 48) / st.pps; window.__fcpApp.placePlayhead(); }""")


@pytest.mark.parametrize("how", ["select at 390", "inside the first time at 390", "inside the first time at 1440"])
def test_the_playhead_never_cuts_through_a_ruler_time(browser, how):
    """With a clip selected the playhead sits at the clip's start; at 390 it cut
    through "00:00:00:00" (critic round 5). The time under it is hidden while it is there."""
    page = _page(browser, width=1440 if how.endswith("1440") else 390)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", _base())
        if how.startswith("select"):
            page.locator(".lane.spine .clip").nth(1).click()
        else:
            _playhead_inside_first_time(page)
        cut = page.evaluate(CUTS_A_TIME)
        page.add_style_tag(content=".tick.under-playhead { visibility: visible !important; }")
        exposed = page.evaluate(CUTS_A_TIME)
    finally:
        page.close()
    assert cut == []
    assert exposed, "the case did not put the playhead over a ruler time (no instrument)"


def test_ticks_sit_on_whole_seconds_of_their_timecode():
    """At 23.98 a timecode second is 1.001 real seconds: a tick every 15 real
    seconds read 00:00:29:23. Every printed tick time ends in :00 frames."""


@pytest.mark.parametrize("fps,nominal,label", [("24000/1001", 24, "23.98"), ("30000/1001", 30, "29.97"), ("24/1", 24, "24")])
def test_every_ruler_time_is_a_whole_second(browser, fps, nominal, label):
    p = _base()
    p.update(fps=fps, nominal_fps=nominal, fps_label=label)
    p["duration"] = "90/1"
    page = _page(browser, width=1440)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", p)
        times = page.evaluate("() => [...document.querySelectorAll('.tick')].map(t => t.textContent)")
    finally:
        page.close()
    assert len(times) >= 3
    assert all(t.endswith(":00") for t in times), times


REWIND = """() => {
  const all = [...document.querySelectorAll('#canvas .clip')];
  const squat = all.filter(e => e.__c.name === 'Squat' && !e.classList.contains('ghost'));
  const ghosts = all.filter(e => e.classList.contains('ghost') && getComputedStyle(e).visibility !== 'hidden')
    .map(e => [e.__c.name, e.getBoundingClientRect().width]);
  const box = (e) => [parseFloat(getComputedStyle(e).left), e.getBoundingClientRect().width];
  const flags = [...document.querySelectorAll('.mlabel')].filter(e => getComputedStyle(e).visibility !== 'hidden').map(e => e.textContent);
  return { squat: squat.map(box), ghosts, flags };
}"""


def test_an_undo_moves_back(browser):
    """Back from the finished cut to the imported one: the pieces merge into
    the long clip from the span they covered, the laid-in clip shrinks out, and
    the markers leave in turn. Nothing jumps, nothing fades."""
    a = _base()
    done = _with_two_markers(_split(a))
    page = _page(browser)
    try:
        page.evaluate("p => window.__fcpApp.setPayload(p)", done)
        pieces = page.evaluate(CLIPS)
        page.evaluate(SET, a)
        start = page.evaluate(REWIND)
        fades = motion_problems(page)
        names = label_problems_through_motion(page)
        page.evaluate(SEEK, 50)
        early = page.evaluate(REWIND)
        page.evaluate(SEEK, 2000)
        page.wait_for_timeout(50)
        end = page.evaluate(REWIND)
    finally:
        page.close()
    span = sorted((c for c in pieces if c["name"] == "Squat"), key=lambda c: c["left"])
    width = span[-1]["left"] + span[-1]["width"] - span[0]["left"]
    # the long clip starts as exactly the span its pieces covered
    assert len(start["squat"]) == 1, start
    assert abs(start["squat"][0][0] - span[0]["left"]) < 1 and abs(start["squat"][0][1] - width) < 1.5, (start, span)
    # the laid-in clip is still there, whole, and shrinks out
    assert [g[0] for g in start["ghosts"]] == ["B-roll"] and start["ghosts"][0][1] > 20
    # markers leave in turn, right to left
    assert {"b-roll", "Finish"} <= set(start["flags"])
    assert "Finish" not in early["flags"] and "b-roll" in early["flags"]
    assert end["ghosts"] == [] and end["flags"] == []
    assert fades == []
    # the clip shrinking out never shows its name cut
    assert names == []


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
