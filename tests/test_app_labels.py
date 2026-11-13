"""No label in the timeline app is ever cut off (0.31).

Through 0.30 the app printed whatever fit and clipped the rest: a ruler
tick at the right edge read "00:", a phone-width tick "00:00:2(", clip
names read "Ope..." and "Closi...", and a chapter marker that collided with
its neighbour was dropped without a trace. Now every label is drawn whole,
inside the visible track area, or hidden with its tick, flag or clip still
drawn and the name in a tooltip (fitLabels() in fcpxml/timeline_app.py).

This renders the real shell in headless Chromium at 1440 and 390 on the two
fixtures and on a stress payload (a long clip name, five markers in two
seconds, a run-out past the last clip), and measures every label box. It is
skipped where Playwright or its browser is missing; the `rendered-labels` CI
job installs both so it runs on every push.
"""

from __future__ import annotations

import copy
import json
import os
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml import timeline_app
from fcpxml.parser import FCPXMLParser

# The CI job that exists to run this sets FCP_MCP_REQUIRE_BROWSER=1: there a
# missing Playwright or Chromium is a failure, not a skip that reads as green.
REQUIRED = os.environ.get("FCP_MCP_REQUIRE_BROWSER") == "1"
if REQUIRED:
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Every visible label: whole (not truncated by its own box), inside the clip
# it names, and inside the scroller's visible track area (right of the
# sticky lane gutter). A hidden label (.unfit) is allowed; a cut one is not.
OFFENDERS = """() => {
  const out = [], GUTTER = 48;
  const sc = document.getElementById('scroller'), r = sc.getBoundingClientRect();
  const left = r.left + GUTTER, right = r.right - 1;
  const shown = (e) => !e.classList.contains('unfit') && e.textContent.trim() !== '';
  const name = (e) => e.className + ' "' + e.textContent.trim().slice(0, 24) + '"';
  sc.querySelectorAll('.tick, .mlabel, .gap, .clip .label').forEach((e) => {
    if (!shown(e)) return;
    const range = document.createRange(); range.selectNodeContents(e);
    const b = range.getBoundingClientRect();   // the text, not its (possibly clip-wide) block
    if (e.scrollWidth > e.clientWidth + 0.5 && !e.classList.contains('tick') && !e.classList.contains('mlabel'))
      out.push('truncated ' + name(e));
    if (b.left < left - 0.5 || b.right > right + 0.5) out.push('clipped by the view ' + name(e));
    const clip = e.closest('.clip');
    if (clip) { const c = clip.getBoundingClientRect(); if (b.right > c.right + 0.5) out.push('outside its clip ' + name(e)); }
  });
  // The other way to fail: hiding a label that would have fitted (its text
  // fits its own box and lies inside the view). Crowded markers are exempt.
  sc.querySelectorAll('.tick.unfit, .gap.unfit, .clip .label.unfit, .mlabel.unfit:not(.crowded)').forEach((e) => {
    if (e.textContent.trim() === '') return;
    const range = document.createRange(); range.selectNodeContents(e);
    const b = range.getBoundingClientRect();
    const fitsOwn = e.classList.contains('tick') || e.classList.contains('mlabel') || e.scrollWidth <= e.clientWidth + 0.5;
    if (fitsOwn && b.left >= left - 0.5 && b.right <= right + 0.5) out.push('hidden though it fits ' + name(e));
  });
  // Nothing may touch (round 4): the playhead's head clears every label it
  // could meet, its line clears every marker flag, and no clip sits within
  // 6px of the track area's bottom border.
  const near = (a, b, pad) => a.left < b.right + pad && b.left < a.right + pad && a.top < b.bottom + pad && b.top < a.bottom + pad;
  const ph = sc.querySelector('.playhead'), head = sc.querySelector('.playhead .phead');
  if (head) {
    const hb = head.getBoundingClientRect(), lb = ph.getBoundingClientRect();
    const line = { left: lb.left, right: lb.left + 2, top: lb.top, bottom: lb.bottom };
    sc.querySelectorAll('.tick, .mlabel, .gap, .clip .label').forEach((e) => {
      if (!shown(e)) return;
      const range = document.createRange(); range.selectNodeContents(e);
      if (near(hb, range.getBoundingClientRect(), 1)) out.push('the playhead head touches ' + name(e));
    });
    sc.querySelectorAll('.mlabel').forEach((e) => {
      if (near(line, e.getBoundingClientRect(), 0)) out.push('the playhead line covers the marker ' + name(e));
    });
  }
  const inner = sc.getBoundingClientRect();
  sc.querySelectorAll('.clip').forEach((c) => {
    const b = c.getBoundingClientRect();
    if (b.bottom > inner.bottom - 1 - 6) out.push('a clip touches the bottom border ' + c.title);
  });
  const marks = [...sc.querySelectorAll('.mlabel')].filter(shown).map((e) => [e.getBoundingClientRect(), name(e)]);
  marks.forEach(([a, n], i) => marks.slice(i + 1).forEach(([b, m]) => {
    if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) out.push('overlap ' + n + ' / ' + m);
  }));
  return out;
}"""


def _payload(name: str) -> dict:
    tl = FCPXMLParser().parse_file(str(EXAMPLES / name)).timelines[0]
    return timeline_app.build_payload(tl, filepath=str(EXAMPLES / name), sha256="0" * 64)


def _stress() -> dict:
    p = copy.deepcopy(_payload("sample.fcpxml"))
    p["clips"][0]["name"] = "Interview_A_camera_two_long_take_from_the_morning_session"
    first = p["clips"][0]
    for i in range(5):  # five markers inside two seconds
        p["markers"].append({"name": f"Beat {i + 1}", "start": f"{2 * 24 + i * 9}/24", "kind": "standard"})
    p["duration"] = str(Fraction(p["duration"]) + 30)  # a run-out past the last clip
    assert first["name"].startswith("Interview_A_camera")
    return p




@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser not installed here
            if REQUIRED:
                raise
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


def offenders(browser, html: str, payload: dict, width: int) -> list[str]:
    page = browser.new_page(viewport={"width": width, "height": 900})
    try:
        page.set_content(html)
        page.evaluate("p => window.__fcpApp.setPayload(p)", json.loads(json.dumps(payload)))
        page.wait_for_timeout(50)
        return page.evaluate(OFFENDERS)
    finally:
        page.close()


CASES = [("sample", lambda: _payload("sample.fcpxml")), ("music-video", lambda: _payload("music-video.fcpxml")),
         ("stress", _stress)]


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("case,make", CASES, ids=[c[0] for c in CASES])
def test_no_label_is_cut(browser, case, make, width):
    assert offenders(browser, timeline_app.render_app_html(), make(), width) == []


def test_the_check_goes_red_without_the_fit_pass(browser):
    """Mutation check: the same shell with fitLabels() never called draws cut
    labels on the stress payload, and the measurement sees them."""
    html = timeline_app.render_app_html()
    broken = html.replace("    placePlayhead();\n    fitLabels();\n", "    placePlayhead();\n")
    assert broken != html, "the fit pass is no longer called where this test expects"
    found = offenders(browser, broken, _stress(), 390)
    assert found, "the instrument cannot see a cut label"
    assert any("clipped by the view" in f or "truncated" in f for f in found)


def test_two_close_markers_both_show_their_names(browser):
    """A second row before giving up: two markers a second apart at phone
    width both keep their names (0.30 dropped the second without a trace)."""
    p = copy.deepcopy(_payload("sample.fcpxml"))
    for c in p["clips"]:
        c["markers"] = []
    p["markers"] = [{"name": "Opening", "start": "0/1", "kind": "chapter"},
                    {"name": "Interview", "start": "1/1", "kind": "chapter"}]
    page = browser.new_page(viewport={"width": 390, "height": 900})
    try:
        page.set_content(timeline_app.render_app_html())
        page.evaluate("p => window.__fcpApp.setPayload(p)", p)
        shown = page.evaluate("() => [...document.querySelectorAll('.mlabel')].filter(e => !e.classList.contains('unfit')).map(e => e.textContent)")
    finally:
        page.close()
    assert shown == ["Opening", "Interview"]


def _marker_at_zero() -> dict:
    """The round-3 critic's case: a chapter marker exactly at the playhead."""
    p = copy.deepcopy(_payload("sample.fcpxml"))
    for c in p["clips"]:
        c["markers"] = []
    p["markers"] = [{"name": "Opening", "start": "0/1", "kind": "chapter"}]
    return p


@pytest.mark.parametrize("width", [1440, 390])
def test_a_marker_at_the_playhead_keeps_its_flag(browser, width):
    assert offenders(browser, timeline_app.render_app_html(), _marker_at_zero(), width) == []


@pytest.mark.parametrize("mutation,old,new", [
    ("the playhead back over the marker rows", ".playhead { position: absolute; top: 28px;", ".playhead { position: absolute; top: 0;"),
    ("tick text back against the playhead head", "padding-left: 9px;", "padding-left: 4px;"),
], ids=["playhead-over-markers", "tick-under-head"])
def test_the_touch_check_goes_red(browser, mutation, old, new):
    """Mutation checks: the round-3 collisions, put back, are seen."""
    html = timeline_app.render_app_html()
    assert html.count(old) == 1, mutation
    assert offenders(browser, html.replace(old, new), _marker_at_zero(), 390), mutation


def test_the_border_check_goes_red(browser):
    """Mutation check: without the track padding the last lane's clips sit
    within 6px of the border (the critic's TRACK clip at 390)."""
    html = timeline_app.render_app_html()
    old = "#canvas { position: relative; min-width: 100%; padding-bottom: 8px; }"
    assert html.count(old) == 1
    found = offenders(browser, html.replace(old, "#canvas { position: relative; min-width: 100%; }"),
                      _payload("music-video.fcpxml"), 390)
    assert any("bottom border" in f for f in found)


def test_a_crowded_marker_keeps_its_flag_and_its_name_in_the_tooltip(browser):
    page = browser.new_page(viewport={"width": 390, "height": 900})
    try:
        page.set_content(timeline_app.render_app_html())
        page.evaluate("p => window.__fcpApp.setPayload(p)", _stress())
        flags = page.evaluate("() => [...document.querySelectorAll('.mlabel')].map(e => [e.title, e.offsetHeight > 0])")
    finally:
        page.close()
    names = [t for t, _ in flags]
    assert all(f"Beat {i}" in names for i in range(1, 6)) and all(drawn for _, drawn in flags)


# ---------------------------------------------------------------------------
# A label over a thumbnail reads at 4.5:1 even over a white frame
# ---------------------------------------------------------------------------

def _white_thumb() -> str:
    import base64
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (255, 255, 255)).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _label_contrast(browser, html: str) -> float:
    """Render a clip whose thumbnail is pure white, hide the label's text,
    and measure the brightest pixel left where the text sits."""
    import io

    from PIL import Image

    from fcpxml import palette

    p = copy.deepcopy(_payload("sample.fcpxml"))
    for c in p["clips"]:
        c["thumb"] = _white_thumb()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(html)
        page.evaluate("p => window.__fcpApp.setPayload(p)", p)
        label = page.locator(".lane.spine .clip.has-thumb .label:not(.unfit)").first
        top = label.evaluate("e => parseFloat(getComputedStyle(e).paddingTop)")
        # the playhead and marker lines are drawn over clips on purpose; measure the scrim alone
        page.add_style_tag(content='.playhead, .markers, .mk, .range { visibility: hidden !important; }')
        label.evaluate("e => [e, ...e.querySelectorAll('*')].forEach(x => { x.style.color = 'transparent'; })")
        shot = Image.open(io.BytesIO(label.screenshot())).convert("RGB")
    finally:
        page.close()
    # where the text is drawn, inset 2px: the box sits at a fractional x, so its edge
    # columns catch a sliver of the clip border beside it
    band = shot.crop((2, int(top) + 1, shot.width - 2, shot.height - 1))
    raw = band.tobytes()  # RGB triples; getdata() is deprecated from Pillow 13
    brightest = max((tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)), key=sum)
    return palette.contrast(palette.dark("on-media"), "#%02X%02X%02X" % brightest)


def test_a_label_over_a_white_frame_reads_at_4_5_to_1(browser):
    assert _label_contrast(browser, timeline_app.render_app_html()) >= 4.5


def test_the_scrim_check_goes_red_without_the_scrim(browser):
    """Mutation check: the 0.31 first-pass scrim (a fade across the text) is caught."""
    html = timeline_app.render_app_html()
    broken = html.replace("background: linear-gradient(to bottom, transparent 0, var(--shade) 10px);",
                          "background: linear-gradient(transparent, var(--shade));")
    assert broken != html
    assert _label_contrast(browser, broken) < 4.5


def _spine_tile_height(browser) -> float:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(timeline_app.render_app_html())
        page.evaluate("p => window.__fcpApp.setPayload(p)", _payload("sample.fcpxml"))
        return page.evaluate("() => document.querySelector('.lane.spine .clip').getBoundingClientRect().height")
    finally:
        page.close()


def thumbnail_problem(short_side: int, tile_height: float) -> str | None:
    """A thumbnail's short side is at least twice the tile it fills, so it is
    sharp on a 2x display instead of upscaled (round 4: 54 px in a 48 px tile)."""
    if short_side < 2 * tile_height:
        return f"{short_side} px thumbnails for a {tile_height:.0f} px tile: under 2x"
    return None


def test_thumbnails_are_twice_the_tile(browser):
    assert thumbnail_problem(timeline_app.THUMBNAIL_SHORT_SIDE, _spine_tile_height(browser)) is None


def test_the_thumbnail_check_goes_red_at_the_old_size(browser):
    assert thumbnail_problem(54, _spine_tile_height(browser)) is not None


# ---------------------------------------------------------------------------
# The legacy preview:// page has no script: its rule is CSS (fcpxml/preview.py)
# ---------------------------------------------------------------------------

LEGACY_OFFENDERS = """() => {
  const out = [];
  document.querySelectorAll('.fit').forEach((row) => {
    const r = row.getBoundingClientRect(), name = row.querySelector('span'), b = name.getBoundingClientRect();
    const visible = b.top < r.bottom - 1 && b.bottom > r.top + 1;   // on the row's one visible line
    if (visible && b.right > r.right + 0.5) out.push('cut "' + name.textContent + '"');
  });
  return out;
}"""


def _legacy(width: int, html: str, browser) -> list[str]:
    page = browser.new_page(viewport={"width": width, "height": 700})
    try:
        page.set_content(html)
        return page.evaluate(LEGACY_OFFENDERS)
    finally:
        page.close()


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("fixture", ["music-video.fcpxml", "sample.fcpxml"])
def test_the_legacy_preview_never_shows_a_cut_name(browser, fixture, width):
    from fcpxml.preview import render_timeline_html
    tl = FCPXMLParser().parse_file(str(EXAMPLES / fixture)).timelines[0]
    assert _legacy(width, render_timeline_html(tl), browser) == []


def test_the_legacy_check_goes_red_without_the_wrapping_row(browser):
    """Mutation check: with the row allowed to stay on one line, the names
    of narrow blocks are cut at 390 and the measurement sees it."""
    from fcpxml.preview import render_timeline_html
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "music-video.fcpxml")).timelines[0]
    html = render_timeline_html(tl)
    broken = html.replace("flex-wrap: wrap;", "flex-wrap: nowrap;")
    assert broken != html
    assert _legacy(390, broken, browser)
