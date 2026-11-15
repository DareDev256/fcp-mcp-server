"""0.34: a timeline that reads at a glance, in one look (open, like Final Cut).

James picked direction B on 2026-11-15; direction A and the ``direction``
flag are gone, and the app (``ui://fcp/timeline``) and ``preview://`` draw
the same thing. Held here, in a real headless Chromium, on the three fixtures
every visual phase is judged on (the 32 s gym cut, the 164 s music video with
three lanes on a gap, the 18 min two-camera interview) at 1440, 430 and 390:

- fit: the median clip at least 44 px wide on open, an overview bar carrying
  the whole cut with the part in view boxed, no sideways page scroll;
- the track area at least 35% of the page at 1440; clips 88 px (titles and
  sound 72 px) on a desktop, 56 px on a phone;
- labels: none shorter than 4 characters, none cut, no two on top of each
  other; a middle ellipsis that keeps the ending; a rail for narrow
  connected clips; a tap sheet (name, start, length) for a clip with no name
  inside; the playhead snaps to cuts; the selection shows the start;
- Final Cut's role colours on the stripe, a waveform under every clip with
  sound, a glyph per marker kind;
- theme: the app follows the host's theme and the OS, preview:// the OS, and
  the computed background is the token in both;
- the payload as ``structuredContent``, drawn by the app inside a stand-in
  host that speaks the ext-apps protocol.

Every gate is broken on purpose once below (the ``*_goes_red`` tests). The
byte budgets (the text fallback, the payload) are in test_view_budget.py,
which needs no browser. CI's ``rendered-labels`` job runs this module with
``FCP_MCP_REQUIRE_BROWSER=1``.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml import palette, timeline_app
from fcpxml.parser import FCPXMLParser

REQUIRED = os.environ.get("FCP_MCP_REQUIRE_BROWSER") == "1"
if REQUIRED:
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
# Three markers written where Final Cut (and the mark tool) writes them on a
# gap-based storyline: INSIDE the gap, on the gap's clock (0.33 reads them).
GAP_MARKERS = ('<marker start="3602s" duration="1001/24000s" value="Hook"/>'
               '<chapter-marker start="3608s" duration="1001/24000s" value="Verse 1" posterOffset="0s"/>'
               '<marker start="3612s" duration="1001/24000s" value="Fix colour" completed="0"/>')


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


def _music_video(tmp_path) -> dict:
    """music-video.fcpxml with a standard, a chapter and a to-do marker on its
    gap, and a waveform for the bed (its media path does not exist)."""
    text = (EXAMPLES / "music-video.fcpxml").read_text()
    gap_close = "</gap>"
    assert text.count(gap_close) == 1
    path = tmp_path / "mv.fcpxml"
    path.write_text(text.replace(gap_close, GAP_MARKERS + gap_close))
    tl = FCPXMLParser().parse_file(str(path)).timelines[0]
    assert len(tl.markers) == 3, "the gap's markers are timeline markers (0.33)"
    return timeline_app.build_payload(tl, waveform=lambda media, start, dur: [(i * 37) % 100 for i in range(120)])


def _build(script: str, out: Path) -> None:
    """Built in its own process: the builders drive the server's handlers with
    asyncio.run, which cannot run inside Playwright's sync loop."""
    env = dict(os.environ, FCP_MCP_JOURNAL="off", FCP_MCP_INDEX="off", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, str(REPO / script), str(out)], check=True, capture_output=True, env=env, timeout=900)


def _needs_ffmpeg():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")


@pytest.fixture(scope="module")
def gym(tmp_path_factory):
    _needs_ffmpeg()
    out = tmp_path_factory.mktemp("gym")
    _build("demo/fixtures/gym_session.py", out)
    edited = out / "edited.fcpxml"
    return {"edited": edited, "timeline": FCPXMLParser().parse_file(str(edited)).timelines[0]}


@pytest.fixture(scope="module")
def interview(tmp_path_factory):
    _needs_ffmpeg()
    out = tmp_path_factory.mktemp("interview")
    _build("demo/fixtures/interview.py", out)
    path = out / "interview.fcpxml"
    return {"path": path, "timeline": FCPXMLParser().parse_file(str(path)).timelines[0]}


def _real(timeline) -> dict:
    """The payload the view tool builds: real thumbnails and waveform peaks."""
    from tools import view

    return timeline_app.build_payload(timeline, thumbnail=view._thumbnail, waveform=view._waveform)


@pytest.fixture(scope="module")
def payloads(gym, interview, tmp_path_factory):
    return {"gym": _real(gym["timeline"]), "music-video": _music_video(tmp_path_factory.mktemp("mv")),
            "interview": _real(interview["timeline"])}


# ---------------------------------------------------------------------------
# What a page shows, measured
# ---------------------------------------------------------------------------

FACTS = r"""() => {
  const vis = (e) => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && !e.closest('[hidden]'); };
  const sc = document.getElementById('scroller').getBoundingClientRect();
  const texts = [];
  document.querySelectorAll('#overview, #canvas, header, #panel').forEach(root => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n; while ((n = walker.nextNode())) {
      const t = n.textContent.trim(); if (!t) continue;
      const e = n.parentElement, cs = getComputedStyle(e);
      if (!vis(e) || cs.color === 'rgba(0, 0, 0, 0)') continue;
      const r = document.createRange(); r.selectNodeContents(n); const b = r.getBoundingClientRect();
      if (!b.width || (e.closest('#canvas') && (b.right < sc.left || b.left > sc.right))) continue;
      const owner = e.closest('.label, .rl');
      texts.push({ t, cls: e.className, id: e.id, l: b.left, r: b.right, top: b.top, b: b.bottom, color: cs.color,
                   name: owner ? (owner.closest('.clip') ? owner.closest('.clip').__c.name : owner.title) : null,
                   cut: !!owner && (owner.scrollWidth > owner.clientWidth + 0.5 ||
                        (owner.closest('.clip') && b.right > owner.closest('.clip').getBoundingClientRect().right + 0.5)) });
    }
  });
  const hits = [];
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    const a = texts[i], c = texts[j];
    if (a.l < c.r - 0.5 && c.l < a.r - 0.5 && a.top < c.b - 0.5 && c.top < a.b - 0.5) hits.push([a.t, c.t]);
  }
  const clips = [...document.querySelectorAll('#canvas .clip')];
  const widths = clips.map(c => c.getBoundingClientRect().width).sort((a, b) => a - b), m = widths.length >> 1;
  const tracks = [...document.querySelectorAll('#canvas .lane, #canvas .rail')].reduce((s, e) => s + e.getBoundingClientRect().height, 0);
  const ov = document.getElementById('overview'), W = ov.clientWidth;
  const bars = [...ov.querySelectorAll('.ob')].map(b => ({ l: parseFloat(b.style.left), r: parseFloat(b.style.left) + parseFloat(b.style.width) + 0.5,
                                                         end: b.__c ? (n => { const p = String(n).split('/'); return p.length === 2 ? p[0] / p[1] : Number(p[0]); })(b.__c.end) : 0 }));
  return { texts, hits, median: widths.length % 2 ? widths[m] : (widths[m - 1] + widths[m]) / 2,
           // the app's own height (the body, padding and all), not the viewport it sits in
           tracks, pageH: document.body.getBoundingClientRect().height,
           pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
           trackScroll: document.getElementById('scroller').scrollWidth - document.getElementById('scroller').clientWidth,
           clips: clips.map(c => ({ name: c.__c.name, id: c.__c.id, lane: c.__c.lane, kind: c.__c.kind, h: c.getBoundingClientRect().height,
                                    w: c.getBoundingClientRect().width,
                                    label: (c.querySelector('.label') || {}).textContent || null,
                                    wave: !!c.querySelector('.wave'), thumb: c.classList.contains('has-thumb'),
                                    stripe: (() => { const s = c.querySelector('.stripe'); return s ? [s.className.split(' ')[1], getComputedStyle(s).backgroundColor, s.getBoundingClientRect().height] : null; })() })),
           rail: [...document.querySelectorAll('.rail .rl')].filter(e => !e.classList.contains('unfit')).map(e => e.textContent),
           glyphs: [...document.querySelectorAll('.mlabel .mg')].map(e => e.className.split(' ')[1]),
           overview: { W, span: Number(ov.dataset.span), bars, box: !!document.getElementById('ovbox') },
           extent: Math.max(...[...clips].map(c => { const p = String(c.__c.end).split('/'); return p.length === 2 ? p[0] / p[1] : Number(p[0]); }),
                            (n => { const p = String(n).split('/'); return p.length === 2 ? p[0] / p[1] : Number(p[0]); })(window.__fcpApp.state.payload.duration)),
           bg: getComputedStyle(document.body).backgroundColor,
           panel: getComputedStyle(document.getElementById('scroller')).backgroundColor };
}"""


def _facts(browser, html: str, width: int, scheme: str = "dark") -> dict:
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme=scheme)
    try:
        page.set_content(html)
        page.wait_for_timeout(100)
        return page.evaluate(FACTS)
    finally:
        page.close()


def _hex(css: str) -> str:
    r, g, b = (int(v) for v in re.findall(r"\d+", css)[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def label_problems(facts: dict) -> list[str]:
    """The three label gates: a visible name shorter than 4 characters (or
    than the whole name, when the name is shorter), two text boxes on top of
    each other, a name cut by its box or its clip."""
    out = []
    for t in facts["texts"]:
        if t["name"] is None:
            continue
        shown = t["t"].replace("…", "")
        if len(shown) < min(4, len(t["name"])):
            out.append(f"short: {t['t']!r} for {t['name']!r}")
        if t["cut"]:
            out.append(f"cut: {t['t']!r}")
    out += [f"on top of each other: {a!r} / {b!r}" for a, b in facts["hits"]]
    return out


def fit_problems(facts: dict, opening: bool = True) -> list[str]:
    """The fit gates: at open the whole cut in the track (round 3: no track
    scroll, at every width; it replaced "median clip >= 44 px"), the
    overview carrying the whole cut, no sideways page scroll."""
    out = []
    if opening and facts["trackScroll"] > 1:
        out.append(f"the track scrolls {facts['trackScroll']} px at open: the whole cut is not in view")
    ov = facts["overview"]
    if not ov["box"] or not ov["bars"]:
        out.append("no overview")
    elif abs(ov["span"] - facts["extent"]) > 1e-6:
        out.append(f"the overview spans {ov['span']:.3f}s of {facts['extent']:.3f}s")
    else:
        last = max(ov["bars"], key=lambda b: b["end"])
        if abs(last["r"] - (ov["W"] - 4)) > 1.5 and abs(last["end"] - facts["extent"]) < 1e-6:
            out.append(f"the last clip ends at {last['r']:.1f} px of the overview's {ov['W'] - 4} px")
        if any(b["r"] > ov["W"] - 4 + 1.5 or b["l"] < 4 - 0.5 for b in ov["bars"]):
            out.append("a clip runs off the overview")
    if facts["pageOverflow"] > 1:
        out.append(f"the page scrolls sideways by {facts['pageOverflow']} px")
    return out


FIXTURES = ["gym", "music-video", "interview"]
WIDTHS = [1440, 430, 390]


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_every_fixture_fits_and_every_name_reads(browser, payloads, fixture, width):
    payload = payloads[fixture]
    facts = _facts(browser, timeline_app.render_page_html(payload), width)
    assert fit_problems(facts) == []
    assert label_problems(facts) == []
    names = [t for t in facts["texts"] if t["name"] is not None]
    assert names, "nothing named at all"
    # a connected clip's name is whole inside it, whole on its lane's rail,
    # or (no name inside) in the tap sheet: never cut to fit
    for c in facts["clips"]:
        if c["lane"] and c["label"] is not None:
            assert c["label"] == c["name"], c
    accent = {palette.dark("accent").upper(), palette.light("accent").upper()}
    assert [t["id"] for t in facts["texts"] if _hex(t["color"]) in accent] == ["tc"]  # cyan: record time only


@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_track_area_is_over_a_third_of_the_page_at_1440(browser, payloads, fixture):
    facts = _facts(browser, timeline_app.render_page_html(payloads[fixture]), 1440)
    assert facts["tracks"] / facts["pageH"] >= 0.35, (facts["tracks"], facts["pageH"])


@pytest.mark.parametrize("width,picture,short", [(1440, 88, 72), (390, 56, 56)])
def test_clip_heights_follow_the_lane_type(browser, payloads, width, picture, short):
    """Picture lanes 88 px, sound 72 px on a desktop; 56 px on a phone; a
    title too narrow for its name is a 24 px bar (round 3)."""
    facts = _facts(browser, timeline_app.render_page_html(payloads["interview"]), width)
    seen = {(c["kind"], round(c["h"])) for c in facts["clips"]}
    assert seen == {("spine", picture), ("title", 24), ("audio", short)}, seen


# ---- each gate, broken on purpose ------------------------------------------------

def _stress() -> dict:
    """A storyline clip too narrow for 4 characters of its long name, between
    two wide ones."""
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0]
    p = timeline_app.build_payload(tl)
    tiny = next(c for c in p["clips"] if c["duration"] == "2/1")
    tiny["name"] = "Interview_take_twelve"
    return p


def test_the_short_label_gate_goes_red(browser, payloads):
    html = timeline_app.render_app_html()
    old = "LABEL_MIN = 28, KEEP_MIN = 4,"
    assert html.count(old) == 1

    def problems(page_html):
        page = browser.new_page(viewport={"width": 1440, "height": 700}, color_scheme="dark")
        try:
            page.set_content(page_html)
            page.evaluate("p => window.__fcpApp.setPayload(p)", _stress())
            page.wait_for_timeout(50)
            return label_problems(page.evaluate(FACTS))
        finally:
            page.close()

    assert problems(html) == []
    broken = problems(html.replace(old, "LABEL_MIN = 4, KEEP_MIN = 1,"))
    assert any(p.startswith("short") for p in broken), broken


def test_the_overlap_gate_goes_red(browser, payloads):
    """Name rows that do not look at what is already on them: the
    interview's camera names land on each other."""
    html = timeline_app.render_page_html(payloads["interview"])
    old = "if (rows[r].some(function (q) { return a < q[1] + 6 && q[0] < b + 6; })) continue;"
    assert html.count(old) == 1
    assert label_problems(_facts(browser, html, 1440)) == []
    broken = label_problems(_facts(browser, html.replace(old, ""), 1440))
    assert any(p.startswith("on top of each other") for p in broken), broken


def test_the_cut_label_gate_goes_red(browser, payloads):
    """Names measured without their padding run out of their boxes, and with
    the fit pass (the net that hides such a name) gone they are seen cut."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = "SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, PAD = 16, TIGHT = 8;"
    net = "    placePlayhead();\n    fitLabels();\n    drawOverview();\n"
    assert html.count(old) == 1 and html.count(net) == 1
    assert label_problems(_facts(browser, html, 390)) == []
    broken = html.replace(old, "SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, PAD = -40, TIGHT = -40;").replace(net, "    placePlayhead();\n    drawOverview();\n")
    assert any(p.startswith("cut") for p in label_problems(_facts(browser, broken, 390)))


def _zoomed(browser, html: str, width: int, clicks: int) -> dict:
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(html)
        page.wait_for_timeout(50)
        for _ in range(clicks):
            page.click("#zin")
        page.wait_for_timeout(50)
        return page.evaluate(FACTS)
    finally:
        page.close()


def test_the_overview_carries_the_whole_cut_when_zoomed_in(browser, payloads):
    facts = _zoomed(browser, timeline_app.render_page_html(payloads["music-video"]), 390, 3)
    assert facts["trackScroll"] > 1 and fit_problems(facts, opening=False) == []


def test_the_overview_gate_goes_red(browser, payloads):
    """An overview that maps only the part in view covers less than the cut."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "var W = ov.clientWidth, H = ov.clientHeight, dur = extent(), lanes = p.lanes;"
    assert html.count(old) == 1
    broken = html.replace(old, "var W = ov.clientWidth, H = ov.clientHeight, dur = ($(\"scroller\").clientWidth - GUTTER) / state.pps, lanes = p.lanes;")
    assert any("overview" in p for p in fit_problems(_zoomed(browser, broken, 390, 3), opening=False))


def test_the_whole_cut_gate_goes_red(browser, payloads):
    """Round 3: the 0.34 first build's fit (the median clip at least 44 px)
    left the music video, the interview, every phone width scrolling at open."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "state.pps = Math.max(MIN_PPS, w / extent());"
    assert html.count(old) == 1
    median = sorted(float(Fraction(c["duration"])) for c in payloads["music-video"]["clips"])[4]
    broken = fit_problems(_facts(browser, html.replace(old, f"state.pps = Math.max(MIN_PPS, w / extent(), 45 / {median});"), 1440))
    assert any("scrolls" in p for p in broken), broken


def test_the_track_area_gate_goes_red(browser, payloads):
    """0.33's 44 px lanes put the gym's tracks under a third of the page."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = ":root { --clip-h: 88px; --clip-h-short: 72px; }"
    assert html.count(old) == 1
    facts = _facts(browser, html.replace(old, ":root { --clip-h: 36px; --clip-h-short: 36px; }"), 1440)
    assert facts["tracks"] / facts["pageH"] < 0.35


# ---------------------------------------------------------------------------
# Round 2 (the gatekeeper's frame check of abb76b1): ticks through the
# times, a marker glyph without its name, an empty band in the clip
# ---------------------------------------------------------------------------

RULER = r"""() => {
  const box = (e) => { const b = e.getBoundingClientRect(); return { l: b.left, r: b.right, t: b.top, b: b.bottom }; };
  // every mark the ruler draws: the frame ticks, and each time's own tick line (its left border)
  const marks = [...document.querySelectorAll('#canvas .ftick')].map(box)
    .concat([...document.querySelectorAll('#canvas .tick')].map(e => { const b = box(e); return { l: b.l, r: b.l + 1, t: b.t, b: b.b }; }));
  const out = [];
  [...document.querySelectorAll('#canvas .tick')].filter(t => getComputedStyle(t).visibility !== 'hidden').forEach(t => {
    const g = document.createRange(); g.selectNodeContents(t); const b = g.getBoundingClientRect();
    marks.forEach(m => { if (m.l < b.right + 2 && b.left < m.r + 2 && m.t < b.bottom + 2 && b.top < m.b + 2)
      out.push(t.textContent + ' touches a tick at x ' + m.l.toFixed(1) + ' (text ' + b.top.toFixed(1) + '-' + b.bottom.toFixed(1) + ', tick ' + m.t.toFixed(1) + '-' + m.b.toFixed(1) + ')'); });
  });
  return { problems: out, times: document.querySelectorAll('#canvas .tick').length };
}"""


def ruler_problems(browser, html: str, width: int, scheme: str) -> dict:
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme=scheme)
    try:
        page.set_content(html)
        page.wait_for_timeout(50)
        return page.evaluate(RULER)
    finally:
        page.close()


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_no_ruler_time_touches_a_tick(browser, payloads, fixture, width, scheme):
    """Every time on the ruler keeps 2 px from every tick (the frame ticks
    under it and every time's own line): round 2 found the frame ticks
    running up through the digits' lower edge."""
    facts = ruler_problems(browser, timeline_app.render_page_html(payloads[fixture]), width, scheme)
    assert facts["times"] >= 2 and facts["problems"] == [], facts


def test_the_ruler_gate_goes_red(browser, payloads):
    """Mutation check: abb76b1's ruler (one 15 px time row the frame ticks
    rose into) is seen."""
    html = timeline_app.render_page_html(payloads["gym"])
    new = ('.ruler { position: relative; height: 62px;',
           'height: 24px; border-left: 1px solid var(--line-strong); padding: 0 0 9px 8px;', '.ftick.sec { height: 6px; }')
    old = ('.ruler { position: relative; height: 54px;',
           'height: 15px; border-left: 1px solid var(--line-strong); padding-left: 8px;', '.ftick.sec { height: 7px; }')
    for a, b in zip(new, old):
        assert html.count(a) == 1, a
        html = html.replace(a, b)
    assert ruler_problems(browser, html, 1440, "dark")["problems"]


MARKS = r"""() => {
  const sc = document.getElementById('scroller').getBoundingClientRect(), gutter = window.__fcpApp.gutter;
  const textOf = (m) => { const t = [...m.childNodes].filter(n => n.nodeType === 3), r = document.createRange();
    if (!t.length) return null; r.setStart(t[0], 0); r.setEnd(t[t.length - 1], t[t.length - 1].length); return r.getBoundingClientRect(); };
  const seen = (e) => getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).color !== 'rgba(0, 0, 0, 0)';
  const glyphs = [...document.querySelectorAll('#canvas .mlabel, #canvas .mchip')].map(m => {
    const g = m.querySelector('.mg'), gb = g.getBoundingClientRect(), tb = textOf(m) || { left: 0, right: 0, width: 0 };
    return { chip: m.classList.contains('mchip'), text: m.textContent, title: m.title.replace(/ \([a-z]+\)$/, ''),
             count: Number(m.dataset.count || 0), listed: m.classList.contains('mchip') ? m.title.split(' · ').length : 1,
             glyph: getComputedStyle(g).visibility !== 'hidden' && gb.width > 0 && gb.right > sc.left && gb.left < sc.right,
             gl: gb.left, gr: gb.right, tl: tb.left, tr: tb.right, named: seen(m) && tb.width > 0 &&
             tb.left >= sc.left + gutter - 0.5 && tb.right <= sc.right - 1 + 0.5 };
  });
  const p = window.__fcpApp.state.payload;
  const total = p.markers.length + p.clips.reduce((n, c) => n + (c.markers || []).length, 0);
  return { glyphs, total };
}"""


def marker_problems(facts: dict) -> list[str]:
    """Round 3: every glyph on screen has its OWN whole name immediately to
    its right, or is a cluster chip whose count is the markers it lists; and
    the markers drawn (named + in chips) are every marker the FCPXML holds."""
    out, drawn = [], 0
    for g in facts["glyphs"]:
        if not g["glyph"]:
            continue
        if not g["named"]:
            out.append(f"{g['title']!r}: glyph without its name")
            continue
        if not (g["gr"] - 0.5 <= g["tl"] <= g["gr"] + 10):
            out.append(f"{g['title']!r}: its name is not just right of its own glyph")
        if g["chip"]:
            if g["count"] != g["listed"] or g["text"] != f"{g['count']} marker" + ("s" if g["count"] != 1 else ""):
                out.append(f"chip {g['text']!r} lists {g['listed']}")
            drawn += g["count"]
        else:
            if g["text"] != g["title"]:
                out.append(f"{g['title']!r} drawn as {g['text']!r}")
            drawn += 1
    if drawn != facts["total"]:
        out.append(f"{drawn} markers drawn, the FCPXML holds {facts['total']}")
    return out


def _marks(browser, html: str, width: int, scheme: str = "dark") -> dict:
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme=scheme)
    try:
        page.set_content(html)
        page.wait_for_timeout(50)
        return page.evaluate(MARKS)
    finally:
        page.close()


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_every_marker_is_named_or_counted_in_a_cluster(browser, payloads, fixture, width):
    facts = _marks(browser, timeline_app.render_page_html(payloads[fixture]), width)
    assert marker_problems(facts) == [], facts
    assert any(g["glyph"] for g in facts["glyphs"]), "no marker drawn at all"


def test_markers_that_cannot_all_be_named_merge_into_a_chip(browser, payloads):
    """At 390 the interview's 14 markers do not all fit: the ones that cannot
    be named sit in chips whose counts add up, never a flipped name (round 2
    put "Colour matched" left of its glyph, under Good laugh's)."""
    facts = _marks(browser, timeline_app.render_page_html(payloads["interview"]), 390)
    chips = [g for g in facts["glyphs"] if g["chip"] and g["glyph"]]
    assert chips and sum(g["count"] for g in chips) + sum(1 for g in facts["glyphs"] if g["glyph"] and not g["chip"]) == 14


def test_the_marker_gate_goes_red(browser, payloads):
    """Mutation check: a marker with no room silently dropped (no cluster)."""
    html = timeline_app.render_page_html(payloads["interview"])
    old = "      host.marks.push(mk);\n"
    assert html.count(old) == 1
    broken = html.replace(old, '      m.classList.add("gone", "unfit"); return;\n')
    assert marker_problems(_marks(browser, broken, 390))


FLAT_HIDE = (".playhead, .flash, .trans, .conn, .range, .clip .label, .rail "
             "{ visibility: hidden !important; }")


def flat_bands(browser, html: str, width: int, scheme: str) -> list[str]:
    """Rows inside each picture clip's body (its borders and the 3 px role
    stripe left out, and 8 px each side for the rounded corners) that are one
    flat colour across the clip: 4 or more such rows of the same colour, one
    under the other, are an empty band. (A plain wall under the shade that
    deepens downward changes colour row by row; a band does not.)"""
    from io import BytesIO

    from PIL import Image

    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme=scheme, device_scale_factor=1)
    out = []
    try:
        page.set_content(html)
        page.wait_for_timeout(50)
        page.add_style_tag(content=FLAT_HIDE)
        boxes = page.evaluate("""() => [...document.querySelectorAll('#canvas .clip.has-thumb')].map(c => {
            const b = c.getBoundingClientRect(), p = c.querySelector('.wave path');
            return { name: c.__c.name, x: b.left, y: b.top, w: b.width, h: b.height,
                     fill: p ? getComputedStyle(p).fill : null }; })""")
        for b in boxes:
            if b["w"] < 24:
                continue
            shot = Image.open(BytesIO(page.screenshot(clip={"x": b["x"], "y": b["y"], "width": b["w"], "height": b["h"]})))
            im = shot.convert("RGB")
            width_px, height_px = im.size
            run, colour = 0, None
            for y in range(2, height_px - 5):  # the 1 px border, and the 3 px stripe over the bottom border
                row = [im.getpixel((x, y)) for x in range(8, width_px - 8)]
                lo, hi = [min(p[k] for p in row) for k in range(3)], [max(p[k] for p in row) for k in range(3)]
                flat = all(h - lo_ <= 2 for lo_, h in zip(lo, hi))
                # a band is rows of ONE colour: the same flat colour row after row
                same = flat and colour is not None and all(abs(a - c) <= 2 for a, c in zip(row[0], colour))
                run, colour = (run + 1, colour) if same else ((1, row[0]) if flat else (0, None))
                # the waveform's own fill is not empty: a steady tone IS a filled band (round 3)
                if run == 4 and not (b["fill"] and _hex(b["fill"]) == "#%02X%02X%02X" % tuple(colour)):
                    out.append(f"{b['name']} at {width}/{scheme}: rows {y - 3}-{y} are one colour {colour}")
    finally:
        page.close()
    return out


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [1440, 390])
def test_no_empty_band_in_a_clip(browser, payloads, width, scheme):
    """The gym's clips carry picture and sound: the filmstrip and the
    waveform's band share the clip, nothing flat between them (round 2), and
    the band is filled to each clip's own loudest peak, so a steady tone
    (Bench, Press, Finish) is a filled band and no empty rows sit over it."""
    assert flat_bands(browser, timeline_app.render_page_html(payloads["gym"]), width, scheme) == []


def test_the_empty_band_gate_goes_red(browser, payloads):
    """Mutation check: abb76b1's clip (the filmstrip stopping 24 px short of
    the foot, the waveform on the panel colour under it) is seen."""
    html = timeline_app.render_page_html(payloads["gym"])
    new = ".clip.has-thumb .wave { height: var(--wave-h); }"
    assert html.count(new) == 1
    broken = html.replace(new, ".clip.has-thumb .wave { height: 12px; }")
    assert flat_bands(browser, broken, 1440, "dark")


ANATOMY = r"""() => [...document.querySelectorAll('#canvas .clip.has-thumb.has-wave')].map(c => {
  const t = c.querySelector('.thumbs').getBoundingClientRect(), w = c.querySelector('.wave').getBoundingClientRect();
  const p = getComputedStyle(c.querySelector('.wave path'));
  return { name: c.__c.name, thumbBottom: t.bottom, waveTop: w.top, waveBottom: w.bottom, clipBottom: c.getBoundingClientRect().bottom,
           fill: p.fill, fillOpacity: Number(p.fillOpacity), stroke: p.stroke };
})"""


def anatomy_problems(clips: list) -> list[str]:
    """Round 3: the waveform in its OWN band under the filmstrip (they meet,
    they do not overlap), filled solid."""
    out = []
    for c in clips:
        if abs(c["thumbBottom"] - c["waveTop"]) > 0.6:
            out.append(f"{c['name']}: the filmstrip ends at {c['thumbBottom']:.1f}, the waveform starts at {c['waveTop']:.1f}")
        if c["fill"] in ("none", "") or c["fillOpacity"] < 1:
            out.append(f"{c['name']}: the waveform is not filled ({c['fill']}, {c['fillOpacity']})")
    return out


@pytest.mark.parametrize("width", [1440, 390])
def test_the_waveform_has_its_own_band_under_the_filmstrip(browser, payloads, width):
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payloads["gym"]))
        page.wait_for_timeout(50)
        clips = page.evaluate(ANATOMY)
    finally:
        page.close()
    assert len(clips) == 7 and anatomy_problems(clips) == []


def test_the_anatomy_gate_goes_red(browser, payloads):
    """Mutation check: fa9b811's clip, the filmstrip running to the foot and
    the waveform over it."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = ".clip.has-thumb.has-wave .thumbs { bottom: calc(3px + var(--wave-h)); }"
    assert html.count(old) == 1
    page = browser.new_page(viewport={"width": 1440, "height": 700}, color_scheme="dark")
    try:
        page.set_content(html.replace(old, ""))
        page.wait_for_timeout(50)
        clips = page.evaluate(ANATOMY)
    finally:
        page.close()
    assert anatomy_problems(clips)


# ---------------------------------------------------------------------------
# Names: the middle ellipsis, the rail, the tap sheet, the playhead
# ---------------------------------------------------------------------------

def test_a_storyline_name_is_cut_in_the_middle_and_keeps_its_ending(browser):
    """A spine clip too narrow for its name shows its head and its ending
    ("A very…ke 12"), never fewer than 4 of its characters."""
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0]
    payload = timeline_app.build_payload(tl)
    long_name = "A very long interview clip name, take 12"
    clip = next(c for c in payload["clips"] if c["kind"] == "spine" and c["duration"] == "7/1")
    clip["name"] = long_name
    facts = _facts(browser, timeline_app.render_page_html(payload), 1440)
    label = next(c["label"] for c in facts["clips"] if c["name"] == long_name)
    assert label and "…" in label and label.endswith("12"), label
    head, tail = label.split("…")
    assert long_name.startswith(head) and long_name.endswith(tail) and len(head + tail) >= 4


STRIP = """() => { const s = document.getElementById('strip'), sc = document.getElementById('scroller').getBoundingClientRect();
  const ruler = document.querySelector('#canvas .ruler').getBoundingClientRect(), sb = s.getBoundingClientRect();
  // anything outside the timeline's own canvas that sits over the tracks
  const over = [...document.body.querySelectorAll('*')].filter(e => !e.closest('#canvas') && e !== document.body && !e.contains(document.getElementById('scroller')) &&
    !e.closest('#scroller') && getComputedStyle(e).position !== 'static' && getComputedStyle(e).display !== 'none' && (() => {
      const b = e.getBoundingClientRect(); return b.width > 0 && b.left < sc.right && sc.left < b.right && b.top < sc.bottom && sc.top < b.bottom; })())
    .map(e => e.id || e.className);
  return { for: s.dataset.for || null, name: (s.querySelector('b') || {}).textContent || null,
           items: [...s.querySelectorAll('.si')].map(e => e.textContent), title: [...s.querySelectorAll('.si')].map(e => e.title),
           above: sb.bottom <= sc.top + 0.5 && sb.bottom <= ruler.top, over,
           sel: [...document.querySelectorAll('#sel dt')].map(d => [d.textContent, d.nextElementSibling.textContent, d.nextElementSibling.title]),
           tc: document.getElementById('tc').textContent }; }"""


def _tap(browser, payload, html=None, width=390):
    """Tap the narrowest clip that has no name inside it; what the page shows."""
    page = browser.new_page(viewport={"width": width, "height": 800}, color_scheme="dark")
    try:
        page.set_content(html or timeline_app.render_page_html(payload))
        page.wait_for_timeout(50)
        box = page.evaluate("""() => { const c = [...document.querySelectorAll('#canvas .clip')].filter(e => !e.querySelector('.label'))
            .sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width)[0];
            c.scrollIntoView(); const r = c.getBoundingClientRect();
            return { id: c.__c.id, name: c.__c.name, w: r.width, x: r.left + r.width / 2, y: r.top + r.height / 2 }; }""")
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(50)
        return box, page.evaluate(STRIP)
    finally:
        page.close()


def _timecode(seconds: Fraction, fps: Fraction) -> str:
    frames = int(seconds * fps + Fraction(1, 2))
    return f"{frames // 24 // 3600:02d}:{frames // 24 // 60 % 60:02d}:{frames // 24 % 60:02d}:{frames % 24:02d}"


def test_a_tap_names_the_clip_in_the_strip_above_the_ruler(browser, payloads):
    """Round 3: no sheet floats over the lanes. A tap selects the clip; the
    strip anchored above the ruler shows its name, start and length (lengths
    are timecodes, the exact rational in the title), the Selection panel
    shows the same, and the playhead (the cyan record time) is at its start."""
    payload = payloads["music-video"]
    box, strip = _tap(browser, payload)
    assert box["w"] < 28, box
    clip = next(c for c in payload["clips"] if c["id"] == box["id"])
    fps = Fraction(payload["fps"])
    start, length = _timecode(Fraction(clip["start"]), fps), _timecode(Fraction(clip["duration"]), fps)
    assert strip["for"] == box["id"] and strip["name"] == box["name"], strip
    assert strip["items"] == [f"start {start}", f"length {length}"] and strip["title"][1] == clip["duration"] + " s", strip
    assert strip["above"] and strip["over"] == [], strip
    sel = {k: (v, t) for k, v, t in strip["sel"]}
    assert sel["start"][0] == start and sel["length"] == (length, clip["duration"] + " s") and strip["tc"] == start
    assert re.fullmatch(r"\d\d:\d\d:\d\d:\d\d", sel["source in"][0]) and sel["source in"][1] == clip["source_start"] + " s"


def test_the_strip_gate_goes_red(browser, payloads):
    """Mutation check: the strip floated over the lanes, as round 2's sheet did."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "#strip { display: flex;"
    assert html.count(old) == 1
    _box, strip = _tap(browser, None, html.replace(old, "#strip { position: absolute; top: 260px; left: 40px; z-index: 30; display: flex;"))
    assert not strip["above"] or strip["over"]


SNAP = """([dx]) => { const app = window.__fcpApp, st = app.state, p = st.payload;
  const c = p.clips.find(c => c.kind === 'spine' && Number(c.start.split('/')[0]) > 0);
  const cut = c.start.split('/').reduce((a, b) => a / b);
  const canvas = document.getElementById('canvas').getBoundingClientRect(), ruler = document.querySelector('.ruler').getBoundingClientRect();
  return { cut, x: canvas.left + app.gutter + cut * st.pps + dx, y: ruler.bottom - 6 }; }"""


def _drop_playhead(browser, payload, dx: float, html=None):
    page = browser.new_page(viewport={"width": 1440, "height": 700})
    try:
        page.set_content(html or timeline_app.render_page_html(payload))
        page.wait_for_timeout(50)
        at = page.evaluate(SNAP, [dx])
        page.mouse.click(at["x"], at["y"])
        return at["cut"], page.evaluate("() => window.__fcpApp.state.playhead")
    finally:
        page.close()


def test_the_playhead_snaps_to_a_cut(browser, payloads):
    cut, near = _drop_playhead(browser, payloads["gym"], 5)
    assert near == pytest.approx(cut, abs=1e-9)
    cut, far = _drop_playhead(browser, payloads["gym"], 30)
    assert far != pytest.approx(cut, abs=1e-3)


def test_the_snap_check_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["gym"])
    old = "SNAP_PX = 8,"
    assert html.count(old) == 1
    cut, near = _drop_playhead(browser, None, 5, html.replace(old, "SNAP_PX = 0,"))
    assert near != pytest.approx(cut, abs=1e-6)


# ---------------------------------------------------------------------------
# Roles, sound, markers
# ---------------------------------------------------------------------------

def test_role_stripes_are_final_cuts_colours_three_px(browser, payloads):
    """The interview carries four of Final Cut's roles: titles (lane 1),
    dialogue (the cameras' sound), music (lane -1), effects (lane -2). Each
    stripe is 3 px of that role's token, in both themes."""
    for scheme, pick in (("dark", palette.dark), ("light", palette.light)):
        facts = _facts(browser, timeline_app.render_page_html(payloads["interview"]), 1440, scheme)
        seen = {}
        for c in facts["clips"]:
            role, colour, height = c["stripe"]
            assert round(height) == 3, c
            seen.setdefault(role, set()).add(_hex(colour))
        assert seen == {f"role-{r}": {pick(f"role-{r}").upper()} for r in ("titles", "dialogue", "music", "effects")}, seen


def test_a_waveform_sits_under_every_clip_with_sound(browser, payloads):
    """The gym's clips all carry sound (real peaks from the files, read by
    the view tool's own reader); on the music video only the bed does."""
    gym = _facts(browser, timeline_app.render_page_html(payloads["gym"]), 1440)
    assert all(c["wave"] and c["thumb"] for c in gym["clips"]) and len(gym["clips"]) == 7
    mv = _facts(browser, timeline_app.render_page_html(payloads["music-video"]), 1440)
    assert [c["name"] for c in mv["clips"] if c["wave"]] == ["TRACK"]
    assert all(len(c.get("wave", [])) <= timeline_app.MAX_WAVE_POINTS for c in payloads["interview"]["clips"])


def test_every_marker_kind_has_its_own_glyph(browser, payloads):
    """Standard, chapter, to-do and completed to-do: the interview writes 4,
    6, 2 and 2 of them with the mark tool; the music video's three sit on its
    gap (read since 0.33: "0 timeline markers" no more)."""
    from collections import Counter

    facts = _facts(browser, timeline_app.render_page_html(payloads["interview"]), 1440)
    assert Counter(facts["glyphs"]) == {"chapter": 6, "standard": 4, "todo": 2, "completed": 2}
    mv = _facts(browser, timeline_app.render_page_html(payloads["music-video"]), 1440)
    assert sorted(mv["glyphs"]) == ["chapter", "standard", "todo"]


def test_the_payload_carries_the_picture_flash(gym):
    """Bench (L1) ends one frame before Press starts: one frame of Squat. The
    payload says so, read from the parsed clips, as diagnose fails it."""
    tl = gym["timeline"]
    lane = tl.connected_clips[0]
    at = int(lane.timeline_start.frames) + int(lane.duration.frames)
    press = next(c for c in tl.clips if c.name == "Press")
    payload = timeline_app.build_payload(tl)
    (flash,) = payload["flashes"]
    assert flash["start"] == f"{Fraction(at, 24).numerator}/{Fraction(at, 24).denominator}"
    assert flash["frames"] == int(press.start.frames) - at == 1 and flash["verdict"] == "FAIL"
    assert flash["shows"] == "Squat" and flash["between"] == ["Bench (L1)", "Press"]


GYM_FACTS = r"""() => {
  const sc = document.getElementById('scroller').getBoundingClientRect();
  const vis = (e) => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none'; };
  const fl = document.querySelector('#canvas .flash'), fb = fl.getBoundingClientRect();
  const zero = [...document.querySelectorAll('#canvas .tick')].find(t => t.textContent === '00:00:00:00');
  const flag = document.querySelector('#canvas .flag');
  return { flash: { inView: fb.right > sc.left && fb.left < sc.right, width: fb.width, height: fb.height,
                    colour: getComputedStyle(fl).backgroundColor },
           flag: flag && !flag.classList.contains('unfit') ? flag.textContent : null,
           zero: !!zero && vis(zero) && !zero.classList.contains('unfit') && !zero.classList.contains('under-playhead'),
           overflow: document.getElementById('scroller').scrollWidth - document.getElementById('scroller').clientWidth };
}"""


@pytest.mark.parametrize("width", [1440, 390])
def test_the_flash_is_red_and_the_ruler_starts_at_zero(browser, payloads, width):
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payloads["gym"]))
        page.wait_for_timeout(100)
        facts = page.evaluate(GYM_FACTS)
    finally:
        page.close()
    assert facts["flash"]["inView"] and facts["flash"]["width"] >= 2 and facts["flash"]["height"] > 40, facts
    assert _hex(facts["flash"]["colour"]) == palette.dark("danger").upper()
    assert facts["flag"] == "flash 1f"
    assert facts["zero"], "00:00:00:00 is hidden"
    assert facts["overflow"] <= 1  # the 30 s cut fits: its tracks do not scroll sideways


# ---------------------------------------------------------------------------
# Round 3 (two fresh critics on fa9b811, the gatekeeper's fix brief): names
# on rows for the storyline too, lanes that collapse, nothing drawn through
# text, transitions and titles as Final Cut draws them, the overview, the header
# ---------------------------------------------------------------------------

SHARE = r"""() => {
  const named = (c) => { const l = c.querySelector('.label'); return (l && !l.classList.contains('unfit')) ||
      [...document.querySelectorAll('.rail .rl')].some(r => r.dataset.for === c.__c.id && !r.classList.contains('unfit')); };
  const spine = [...document.querySelectorAll('#canvas .clip')].filter(c => !c.__c.lane && c.getBoundingClientRect().width >= 28);
  return { wide: spine.length, named: spine.filter(named).length };
}"""


def _eval(browser, html: str, width: int, script: str, scheme: str = "dark", clicks: int = 0):
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme=scheme)
    try:
        page.set_content(html)
        page.wait_for_timeout(50)
        for _ in range(clicks):
            page.click("#zin")
        page.wait_for_timeout(50)
        return page.evaluate(script)
    finally:
        page.close()


def test_most_storyline_clips_are_named_inside_or_on_a_row(browser, payloads):
    """A2: on the interview at 1440 (whole cut at open: 44 cuts in 1,380 px)
    at least 80% of the storyline clips 28 px or wider show a name, inside or
    on the storyline's name rows."""
    facts = _eval(browser, timeline_app.render_page_html(payloads["interview"]), 1440, SHARE)
    assert facts["wide"] >= 20 and facts["named"] / facts["wide"] >= 0.8, facts


def test_the_name_share_gate_goes_red(browser, payloads):
    """Mutation check: no name rows under the storyline (fa9b811)."""
    html = timeline_app.render_page_html(payloads["interview"])
    old = "var rail = items.length ? railFor("
    assert html.count(old) == 1
    facts = _eval(browser, html.replace(old, "var rail = items.length && lane.lane ? railFor("), 1440, SHARE)
    assert facts["named"] / facts["wide"] < 0.8, facts


LANES = r"""() => [...document.querySelectorAll('#canvas .lane')].map(l => ({ lane: l.dataset.lane, h: l.getBoundingClientRect().height,
  collapsed: l.classList.contains('collapsed'), rail: l.__rail ? getComputedStyle(l.__rail).display : null }))"""


def test_a_lane_with_nothing_in_view_collapses(browser, payloads):
    """A3: zoomed in on the interview at 390 the whooshes (lane -2, from
    00:02:59) are out of view: their lane is a thin rule, its names with it;
    the music bed (lane -1, from 0) stays."""
    lanes = {r["lane"]: r for r in _eval(browser, timeline_app.render_page_html(payloads["interview"]), 390, LANES, clicks=6)}
    assert lanes["-2"]["collapsed"] and lanes["-2"]["h"] <= 10 and lanes["-2"]["rail"] in (None, "none"), lanes
    assert not lanes["-1"]["collapsed"] and lanes["-1"]["h"] > 50, lanes


def test_the_collapse_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["interview"])
    old = 'row.classList.toggle("collapsed", !shown);'
    assert html.count(old) == 1
    lanes = {r["lane"]: r for r in _eval(browser, html.replace(old, 'row.classList.toggle("collapsed", false);'), 390, LANES, clicks=6)}
    assert lanes["-2"]["h"] > 10


THROUGH = r"""() => {
  // lines are pointer-events: none; make them hit-testable so elementsFromPoint reports what is painted on top
  const st = document.createElement('style');
  st.textContent = '#canvas .playhead, #canvas .playhead *, #canvas .conn, #canvas .flash, #canvas .ftick, #canvas .mtick, ' +
                   '#canvas .mg, #canvas .trans, #canvas .trans *, #canvas .gap, #canvas .gap * { pointer-events: auto !important; }';
  document.head.appendChild(st);
  const shown = (e) => { const c = getComputedStyle(e); return c.visibility !== 'hidden' && c.display !== 'none' && !e.closest('.collapsed') &&
                                                            c.color !== 'rgba(0, 0, 0, 0)'; };
  const lines = [...document.querySelectorAll('#canvas .playhead, #canvas .phead, #canvas .conn, #canvas .flash, #canvas .ftick, ' +
                                              '#canvas .mtick, #canvas .mg, #canvas .trans .bow')].filter(shown);
  const texts = [];
  document.querySelectorAll('#canvas .clip .label:not(.unfit), #canvas .rail .rl:not(.unfit), #canvas .gap .gl:not(.unfit)')
    .forEach(e => { if (shown(e)) texts.push([e, e.getBoundingClientRect()]); });
  document.querySelectorAll('#canvas .tick, #canvas .mlabel, #canvas .mchip, #canvas .flag').forEach(e => {
    if (!shown(e) || e.classList.contains('unfit')) return;
    const t = [...e.childNodes].filter(n => n.nodeType === 3); if (!t.length) return;
    const r = document.createRange(); r.setStart(t[0], 0); r.setEnd(t[t.length - 1], t[t.length - 1].length);
    texts.push([e, r.getBoundingClientRect()]);
  });
  const out = [];
  // a name's own leader (its ::before) may run along its edge, never through its text
  document.querySelectorAll('#canvas .rail .rl:not(.unfit)').forEach(e => {
    if (!shown(e)) return;
    const b = e.getBoundingClientRect(), pc = getComputedStyle(e, '::before'), lx = b.left + parseFloat(pc.left);
    const top = pc.top !== 'auto' ? b.top + parseFloat(pc.top) : b.bottom - parseFloat(pc.bottom) - parseFloat(pc.height);
    const t = [...e.childNodes].filter(n => n.nodeType === 3), r = document.createRange();
    r.setStart(t[0], 0); r.setEnd(t[t.length - 1], t[t.length - 1].length); const tb = r.getBoundingClientRect();
    if (lx > tb.left + 0.5 && lx < tb.right - 0.5 && top < tb.bottom - 0.5 && top + parseFloat(pc.height) > tb.top + 0.5)
      out.push('the leader runs through ' + JSON.stringify(e.textContent));
  });
  texts.forEach(([te, tb]) => lines.forEach(l => {
    if (te.contains(l)) return;
    const lb = l.getBoundingClientRect();
    const x0 = Math.max(tb.left, lb.left), x1 = Math.min(tb.right, lb.right), y0 = Math.max(tb.top, lb.top), y1 = Math.min(tb.bottom, lb.bottom);
    if (x1 - x0 < 0.5 || y1 - y0 < 0.5) return;
    for (let x = x0 + 0.25; x < x1; x += 1.5) for (let y = y0 + 0.25; y < y1; y += 1.5) {
      const hits = document.elementsFromPoint(x, y);
      const li = hits.findIndex(h => h === l || l.contains(h)), ti = hits.findIndex(h => h === te || te.contains(h));
      // the text paints here (a clip's rounded corner clips its name's box) and the line paints over it
      if (li >= 0 && ti >= 0 && li < ti) { out.push((l.className.baseVal != null ? l.className.baseVal : l.className) + ' over ' +
                                                     JSON.stringify(te.textContent.slice(0, 24)) + ' at ' + x.toFixed(1) + ',' + y.toFixed(1)); return; }
    }
  }));
  st.remove();
  return out;
}"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_nothing_is_drawn_through_text(browser, payloads, fixture, width, scheme):
    """B4: no line, tick, playhead or glyph pixel is painted inside any text
    box (names on clips and rows, gap names, ruler times, marker names and
    chips, the flash flag): marker lines are out of the lanes, the playhead,
    a flash and every connection line go behind names on opaque grounds."""
    assert _eval(browser, timeline_app.render_page_html(payloads[fixture]), width, THROUGH, scheme) == []


def test_nothing_is_drawn_through_text_after_a_tap(browser, payloads):
    page = browser.new_page(viewport={"width": 390, "height": 800}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payloads["music-video"]))
        page.wait_for_timeout(50)
        for i in range(3):  # tap three clips in turn: the playhead moves to each one's start
            box = page.evaluate(f"""() => {{ const c = [...document.querySelectorAll('#canvas .clip')][{i}], r = c.getBoundingClientRect();
                return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }}; }}""")
            page.mouse.click(box["x"], box["y"])
            page.wait_for_timeout(30)
            assert page.evaluate(THROUGH) == [], i
    finally:
        page.close()


def test_the_through_text_gate_goes_red(browser, payloads):
    """Mutation check: the playhead painted over the names (fa9b811 drew it
    through "Gap" and "TRACK")."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "pointer-events: none; z-index: 6; }"
    assert html.count(old) == 1
    assert _eval(browser, html.replace(old, "pointer-events: none; z-index: 9; }"), 1440, THROUGH)


def test_the_leader_gate_goes_red(browser, payloads):
    """Mutation check: a centred name's leader drawn the full height of its
    name, down through "Cross Dissolve 0.5s" (the first round-3 render)."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = "  .rail .rl.mid::before { left: 50%; height: var(--lead); }"
    assert html.count(old) == 1
    found = _eval(browser, html.replace(old, "  .rail .rl.mid::before { left: 50%; height: calc(15px + var(--lead)); bottom: 0 !important; }"), 390, THROUGH)
    assert any("leader runs through" in f for f in found), found


FLASH_NAMES = r"""() => {
  const flashes = [...document.querySelectorAll('#canvas .flash')].map(f => f.getBoundingClientRect());
  const ruler = document.querySelector('#canvas .ruler').getBoundingClientRect(), out = [];
  flashes.forEach(f => { if (Math.abs(f.top - ruler.top) > 0.5) out.push('the flash line starts at ' + f.top + ', the ruler at ' + ruler.top); });
  document.querySelectorAll('#canvas .clip .label:not(.unfit), #canvas .rail .rl:not(.unfit)').forEach(e => {
    if (getComputedStyle(e).visibility === 'hidden' || e.closest('.collapsed')) return;
    // the name's own text, not its scrim
    const t = [...e.childNodes].filter(n => n.nodeType === 3); if (!t.length) return;
    const g = document.createRange(); g.setStart(t[0], 0); g.setEnd(t[t.length - 1], t[t.length - 1].length);
    const b = g.getBoundingClientRect(), clip = e.closest('.clip');
    if (clip && b.right > clip.getBoundingClientRect().right + 0.5) out.push(e.textContent + ' runs past its clip');
    flashes.forEach(f => { if (f.left < b.right + 0.5 && b.left < f.right + 0.5 && f.top < b.bottom && b.top < f.bottom)
      out.push(e.textContent + ' crosses the flash line'); });
  });
  return out;
}"""


@pytest.mark.parametrize("width", WIDTHS)
def test_no_name_crosses_the_flash_line(browser, payloads, width):
    """B5: the red flash line runs from the ruler's top through the picture's
    tracks; no clip's name (inside it, or on a row) crosses it, and a name
    inside a clip never runs past the clip's end (gym 390/430: the connected
    Bench's name crossed it)."""
    assert _eval(browser, timeline_app.render_page_html(payloads["gym"]), width, FLASH_NAMES) == []


@pytest.mark.parametrize("old,new", [
    ('lane.lane > 0 ? flashX : []', '[]'),  # with the names at their clip's start, as fa9b811 placed them (below)
    (".flash { position: absolute; top: 0; bottom: 0;", ".flash { position: absolute; top: 62px; bottom: 0;"),
], ids=["names-ignore-the-flash", "flash-under-the-ruler"])
def test_the_flash_name_gate_goes_red(browser, payloads, old, new):
    html = timeline_app.render_page_html(payloads["gym"])
    assert html.count(old) == 1
    broken = html.replace(old, new)
    if new == "[]":
        start = 'prefer: lane.lane ? "end" : "start"'
        assert broken.count(start) == 1
        broken = broken.replace(start, 'prefer: "start"')
    if "top: 62px" in new:  # the line drawn from the ruler's foot (fa9b811), not its top
        broken = broken.replace('sl.style.bottom = "auto"; sl.style.height = foot + "px";', 'sl.style.top = "62px";')
        broken = broken.replace('f.style.bottom = "auto"; f.style.height = (s0 + s.offsetHeight) + "px";', 'f.style.top = "62px";')
    assert _eval(browser, broken, 390, FLASH_NAMES)


TRANS = r"""() => {
  const t = document.querySelector('#canvas .trans'); if (!t) return null;
  const b = t.getBoundingClientRect(), bg = t.querySelector('.tbg'), fin = [...document.querySelectorAll('#canvas .clip')].find(c => c.__c.name === 'Finish');
  const lab = fin.querySelector('.label'), name = [...document.querySelectorAll('#canvas .rl.tname')].find(r => !r.classList.contains('unfit'));
  const cut = fin.getBoundingClientRect().left;
  return { w: b.width, spans: b.left < cut && cut < b.right, ownBg: getComputedStyle(t).backgroundColor,
           bgOpacity: bg ? Number(getComputedStyle(bg).opacity) : 1, bow: !!t.querySelector('svg.bow path'),
           name: name ? name.textContent : null, nameShown: !!name && getComputedStyle(name).visibility !== 'hidden',
           labelAfter: lab ? lab.getBoundingClientRect().left >= b.right - 0.5 : null };
}"""


@pytest.mark.parametrize("width", WIDTHS)
def test_a_transition_is_drawn_as_final_cut_draws_it(browser, payloads, width):
    """D8: a translucent overlay across both clips at the edit, the bow-tie,
    at least 12 px wide at any width (it never reads as a gap), its name
    "Cross Dissolve 0.5s" on a row, and the next clip's name after it."""
    t = _eval(browser, timeline_app.render_page_html(payloads["gym"]), width, TRANS)
    assert t and t["w"] >= 12 and t["spans"] and t["bow"], t
    assert t["ownBg"] in ("rgba(0, 0, 0, 0)", "transparent") and t["bgOpacity"] < 1, t
    assert t["name"] == "Cross Dissolve 0.5s" and t["nameShown"], t
    assert t["labelAfter"] in (True, None), t


def test_the_transition_gate_goes_red(browser, payloads):
    """Mutation check: an opaque slab (fa9b811's box)."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = "background: var(--shade); opacity: .45; }"
    assert html.count(old) == 1
    t = _eval(browser, html.replace(old, "background: var(--raised-2); opacity: 1; }"), 1440, TRANS)
    assert t["bgOpacity"] >= 1


TITLES = r"""() => [...document.querySelectorAll('#canvas .clip.k-title')].map(c => {
  const lab = c.querySelector('.label'), row = [...document.querySelectorAll('.rail .rl')].find(r => r.dataset.for === c.__c.id && !r.classList.contains('unfit'));
  return { name: c.__c.name, bg: getComputedStyle(c).backgroundColor, h: c.getBoundingClientRect().height, inside: !!lab, row: !!row };
})"""


@pytest.mark.parametrize("width", [1440, 390])
def test_titles_are_titles_coloured_and_named(browser, payloads, width):
    """D9: a title clip's body is the Titles role colour (Final Cut 12.4's,
    read from the app); one too narrow for its name is a 24 px bar named on
    its row (at 1440 every lower third is named); no empty grey pill."""
    titles = _eval(browser, timeline_app.render_page_html(payloads["interview"]), width, TITLES)
    assert len(titles) == 8
    for t in titles:
        assert _hex(t["bg"]) == palette.dark("role-titles").upper(), t
        assert t["inside"] or round(t["h"]) == 24, t
        if width == 1440:
            assert t["inside"] or t["row"], t


def test_the_title_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["interview"])
    old = ".clip.k-title { background: var(--role-titles); border-color: var(--role-titles); }"
    assert html.count(old) == 1
    titles = _eval(browser, html.replace(old, ".clip.k-title { background: var(--raised-2); }"), 1440, TITLES)
    assert any(_hex(t["bg"]) != palette.dark("role-titles").upper() for t in titles)


THIN = r"""() => ({ clips: [...document.querySelectorAll('#canvas .clip')].filter(c => c.__c.lane).map(c => [c.__c.name, c.getBoundingClientRect().width]),
  conns: [...document.querySelectorAll('#canvas .conn')].map(c => [getComputedStyle(c).borderLeftWidth, getComputedStyle(c, '::after').width]) })"""


@pytest.mark.parametrize("width", WIDTHS)
def test_no_connected_clip_is_thinner_than_6_px(browser, payloads, width):
    """D10: the interview's 2 s whooshes are 6 px at least (fa9b811: 2 px);
    connection lines 2 px with a 6 px anchor dot."""
    facts = _eval(browser, timeline_app.render_page_html(payloads["interview"]), width, THIN)
    assert all(w >= 6 - 0.01 for _n, w in facts["clips"]), facts["clips"]
    assert facts["conns"] and all(c == ["2px", "6px"] for c in facts["conns"]), facts["conns"][:3]


def test_the_thin_clip_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["interview"])
    old = "var MIN_W = 6,"
    assert html.count(old) == 1
    facts = _eval(browser, html.replace(old, "var MIN_W = 1,"), 1440, THIN)
    assert any(w < 6 for _n, w in facts["clips"])


OVERVIEW = r"""() => { const ov = document.getElementById('overview');
  return { ground: getComputedStyle(ov).backgroundColor, bars: [...new Set([...ov.querySelectorAll('.ob')].map(b => getComputedStyle(b).backgroundColor))],
           glyphs: ov.querySelectorAll('.og.mg').length, lines: ov.querySelectorAll('.om').length,
           total: window.__fcpApp.state.payload.markers.length + window.__fcpApp.state.payload.clips.reduce((n, c) => n + (c.markers || []).length, 0) }; }"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_overview_reads(browser, payloads, scheme):
    """E12: every clip bar 3:1 or better on the overview's ground; every
    marker a glyph (fa9b811: bars at about 1.5:1, markers plain lines)."""
    o = _eval(browser, timeline_app.render_page_html(payloads["interview"]), 1440, OVERVIEW, scheme)
    assert o["bars"] and all(palette.contrast(_hex(b), _hex(o["ground"])) >= 3 for b in o["bars"]), o
    assert o["glyphs"] == o["total"] == 14 and o["lines"] == 0, o


def test_the_overview_contrast_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["interview"])
    old = "#overview .ob { position: absolute; border-radius: 1px; background: var(--mute); }"
    assert html.count(old) == 1
    o = _eval(browser, html.replace(old, "#overview .ob { position: absolute; border-radius: 1px; background: var(--line-strong); }"), 1440, OVERVIEW)
    assert any(palette.contrast(_hex(b), _hex(o["ground"])) < 3 for b in o["bars"])


HEADER = r"""() => { const m = document.getElementById('meta'), t = m.firstChild.textContent, fmt = /\d+×\d+\s@\s[\d.]+fps/.exec(t);
  const r = document.createRange(); r.setStart(m.firstChild, fmt.index); r.setEnd(m.firstChild, fmt.index + fmt[0].length);
  return { text: t, tops: [...new Set([...r.getClientRects()].map(q => Math.round(q.top)))].length }; }"""


def test_the_header_reads_whole(browser, payloads):
    """E13: on the music video at 390 the format "3840×2160 @ 23.98fps" never
    breaks inside (fa9b811 broke after "@"); a gap spine says so; the length
    is a timecode."""
    h = _eval(browser, timeline_app.render_page_html(payloads["music-video"]), 390, HEADER)
    assert h["tops"] == 1, h
    assert h["text"].startswith("gap spine + 8 connected · 00:02:43:20 · "), h


def test_the_header_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["music-video"])
    old = '"\\u00a0@\\u00a0"'
    assert html.count(old) == 1
    assert _eval(browser, html.replace(old, '" @ "'), 390, HEADER)["tops"] > 1


# ---------------------------------------------------------------------------
# Theme, and the app inside a host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_preview_follows_the_os_and_its_background_is_the_token(browser, payloads, scheme):
    pick = palette.dark if scheme == "dark" else palette.light
    facts = _facts(browser, timeline_app.render_page_html(payloads["music-video"]), 1440, scheme)
    assert _hex(facts["bg"]) == pick("ground").upper() and _hex(facts["panel"]) == pick("panel").upper()


HOST = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<iframe id="app" style="width:1400px;height:900px;border:0"></iframe>
<script>
  window.__host = { theme: null, seen: [] };
  const frame = document.getElementById('app');
  window.addEventListener('message', (ev) => {
    const m = ev.data; if (!m || m.jsonrpc !== '2.0') return;
    window.__host.seen.push(m.method || 'reply');
    if (m.method === 'ui/initialize')
      frame.contentWindow.postMessage({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: '2025-11-21',
        hostInfo: { name: 'stand-in' }, hostCapabilities: {}, hostContext: window.__host.theme ? { theme: window.__host.theme } : {} } }, '*');
  });
  window.__post = (method, params) => frame.contentWindow.postMessage({ jsonrpc: '2.0', method, params }, '*');
</script></body></html>"""


def _host(browser, scheme: str, theme, app_html=None, payload=None):
    """The app in an iframe of a stand-in host that answers ui/initialize with
    ``theme`` and, given a payload, posts a tool-result carrying it as
    structuredContent with a text that carries none."""
    page = browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    page.set_content(HOST)
    page.evaluate("t => { window.__host.theme = t; }", theme)
    page.evaluate("h => { document.getElementById('app').srcdoc = h; }", app_html or timeline_app.render_app_html())
    page.wait_for_function("() => window.__host.seen.includes('ui/notifications/initialized')", timeout=5000)
    frame = page.frames[1]
    if payload is not None:
        page.evaluate("p => window.__post('ui/notifications/tool-result', { content: [{ type: 'text', text: 'summary only' }], structuredContent: p })",
                      json.loads(json.dumps(payload)))
        page.wait_for_timeout(100)
    return page, frame


BG = "() => ({ bg: getComputedStyle(document.body).backgroundColor, clips: document.querySelectorAll('#canvas .clip').length })"


@pytest.mark.parametrize("scheme,theme", [("dark", "light"), ("light", "dark"), ("dark", None), ("light", None)])
def test_the_app_follows_the_hosts_theme_and_then_the_os(browser, scheme, theme):
    page, frame = _host(browser, scheme, theme)
    try:
        bg = frame.evaluate(BG)["bg"]
        page.evaluate("() => window.__post('ui/notifications/host-context-changed', { theme: 'light' })")
        page.wait_for_timeout(50)
        changed = frame.evaluate(BG)["bg"]
    finally:
        page.close()
    want = theme or scheme
    assert _hex(bg) == (palette.dark if want == "dark" else palette.light)("ground").upper(), (scheme, theme, bg)
    assert _hex(changed) == palette.light("ground").upper()


def test_the_theme_checks_go_red(browser, payloads):
    """Without the host hook the app keeps the OS's dark under a light host;
    without the media query preview:// stays dark under a light OS."""
    html = timeline_app.render_app_html()
    old = 'if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);'
    assert html.count(old) == 1
    page, frame = _host(browser, "dark", "light", html.replace(old, old.replace('document.documentElement.setAttribute("data-theme", theme);', "void theme;")))
    try:
        assert _hex(frame.evaluate(BG)["bg"]) == palette.dark("ground").upper()
    finally:
        page.close()
    query = "@media (prefers-color-scheme: light)"
    page_html = timeline_app.render_page_html(payloads["gym"])
    assert page_html.count(query) == 1
    facts = _facts(browser, page_html.replace(query, "@media (prefers-color-scheme: no-such)"), 1440, "light")
    assert _hex(facts["bg"]) != palette.light("ground").upper()


def test_the_app_draws_the_structured_payload(browser, payloads):
    """The host hands the app the CallToolResult; the payload is its
    structuredContent and the text carries no JSON (0.34)."""
    page, frame = _host(browser, "dark", "dark", payload=payloads["music-video"])
    try:
        drawn = frame.evaluate(BG)["clips"]
    finally:
        page.close()
    assert drawn == len(payloads["music-video"]["clips"])


def test_the_structured_payload_check_goes_red(browser, payloads):
    html = timeline_app.render_app_html()
    old = "if (sc && sc.schema === SCHEMA) return sc;"
    assert html.count(old) == 1
    page, frame = _host(browser, "dark", "dark", html.replace(old, ""), payload=payloads["music-video"])
    try:
        assert frame.evaluate(BG)["clips"] == 0
    finally:
        page.close()


# ---------------------------------------------------------------------------
# One look
# ---------------------------------------------------------------------------

def test_there_is_one_look():
    """Direction A and the flag that chose it are gone: the page takes a
    payload and nothing else, and the shell carries no direction switch."""
    assert list(inspect.signature(timeline_app.render_page_html).parameters) == ["payload"]
    assert not hasattr(timeline_app, "DIRECTIONS")
    html = timeline_app.render_app_html()
    for gone in ("data-direction", "DIR ===", "thumb_in", "#key", ".onum"):
        assert gone not in html, gone
