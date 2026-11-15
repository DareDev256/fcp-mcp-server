"""0.34: a timeline that reads at a glance, in one look (open, like Final Cut).

James picked direction B on 2026-11-15; direction A and the ``direction``
flag are gone, and the app (``ui://fcp/timeline``) and ``preview://`` draw
the same thing. Held here, in a real headless Chromium, on the three fixtures
every visual phase is judged on (the 30 s gym cut, the 164 s music video with
three lanes on a gap, the 18 min two-camera interview) at 1440, 430 and 390.
Round 4 (2026-11-15, the Fable ruling on two critics' FIX FIRST) holds:

- names: inside the clip, whole, 11 px, 6 px in, or not at all; no name
  rows, leaders or scrim pills; the hint strip counts the names shown;
- the zoom at open: the fit region (the picture + 10% when the picture
  covers under 25% of the cut, else the whole cut), zoomed to a 64 px median
  picture clip when it would be under 16 px; the overview a minimap (role
  colours, a 1 px tick per marker, the flash in its own band, >= 44 px);
- the playhead on top of everything, touching nothing at rest;
- the waveform on one absolute -60..0 dBFS scale, a column per peak, in its
  own band on the clip's grey; the gym's sound with dynamics;
- marker runs bracketed only where the names cannot all fit, never over
  more than a quarter of the view; the flash line under the marker rows;
- one set of role colours; cyan record times in the strip and Selection;
  00:00:00:00 stays on a tap; stems end at their dot on the storyline;
- a collision check over every drawn primitive.

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
  const vis = (e) => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && !e.closest('[hidden]') && e.getClientRects().length > 0; };
  const scEl = document.getElementById('scroller'), sc = scEl.getBoundingClientRect();
  const texts = [];
  document.querySelectorAll('#overview, #canvas, header, #panel').forEach(root => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n; while ((n = walker.nextNode())) {
      const t = n.textContent.trim(); if (!t) continue;
      const e = n.parentElement, cs = getComputedStyle(e);
      if (!vis(e) || cs.color === 'rgba(0, 0, 0, 0)') continue;
      const r = document.createRange(); r.selectNodeContents(n);
      const owner = e.closest('.label');
      // one box per line: a run that wraps is two boxes, not one box over both lines
      [...r.getClientRects()].forEach(b => {
        if (!b.width || (e.closest('#canvas') && (b.right < sc.left || b.left > sc.right))) return;
        texts.push({ t, cls: e.className, id: e.id, l: b.left, r: b.right, top: b.top, b: b.bottom, color: cs.color,
                     name: owner ? owner.closest('.clip').__c.name : null,
                     cut: !!owner && (owner.scrollWidth > owner.clientWidth + 0.5 || b.right > owner.closest('.clip').getBoundingClientRect().right + 0.5) });
      });
    }
  });
  const hits = [];
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    const a = texts[i], c = texts[j];
    if (a.l < c.r - 0.5 && c.l < a.r - 0.5 && a.top < c.b - 0.5 && c.top < a.b - 0.5) hits.push([a.t, c.t]);
  }
  const clips = [...document.querySelectorAll('#canvas .clip')];
  const widths = clips.map(c => c.getBoundingClientRect().width).sort((a, b) => a - b), m = widths.length >> 1;
  const tracks = [...document.querySelectorAll('#canvas .lane')].reduce((s, e) => s + e.getBoundingClientRect().height, 0);
  const ov = document.getElementById('overview'), W = ov.clientWidth;
  const fr = (n) => { const p = String(n).split('/'); return p.length === 2 ? p[0] / p[1] : Number(p[0]); };
  const bars = [...ov.querySelectorAll('.ob')].map(b => ({ l: parseFloat(b.style.left), r: parseFloat(b.style.left) + parseFloat(b.style.width) + 0.5,
                                                         end: b.__c ? fr(b.__c.end) : 0 }));
  const st = window.__fcpApp.state;
  return { texts, hits, median: widths.length % 2 ? widths[m] : (widths[m - 1] + widths[m]) / 2,
           // the app's own height (the body, padding and all), not the viewport it sits in
           tracks, pageH: document.body.getBoundingClientRect().height,
           pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
           trackScroll: scEl.scrollWidth - scEl.clientWidth, scW: scEl.clientWidth, scrollLeft: scEl.scrollLeft, pps: st.pps,
           gutter: window.__fcpApp.gutter,
           clips: clips.map(c => ({ name: c.__c.name, id: c.__c.id, lane: c.__c.lane, kind: c.__c.kind, h: c.getBoundingClientRect().height,
                                    w: c.getBoundingClientRect().width,
                                    label: (c.querySelector('.label') || {}).textContent || null,
                                    wave: !!c.querySelector('.wave'), thumb: c.classList.contains('has-thumb'),
                                    stripe: (() => { const s = c.querySelector('.stripe'); return s ? [s.className.split(' ')[1], getComputedStyle(s).backgroundColor, s.getBoundingClientRect().height] : null; })() })),
           glyphs: [...document.querySelectorAll('.mlabel')].filter(vis).map(e => e.querySelector('.mg').className.split(' ')[1]),
           overview: { W, span: Number(ov.dataset.span), bars, box: !!document.getElementById('ovbox') },
           extent: Math.max(...[...clips].map(c => fr(c.__c.end)), fr(st.payload.duration)),
           bg: getComputedStyle(document.body).backgroundColor,
           panel: getComputedStyle(scEl).backgroundColor };
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
    """The label gates: a visible name that is not the clip's whole name (cut,
    an ellipsis, shorter than 4 characters), two text boxes on top of each
    other, a name cut by its box or its clip."""
    out = []
    for t in facts["texts"]:
        if t["name"] is None:
            continue
        if t["t"] != t["name"]:
            out.append(f"not whole: {t['t']!r} for {t['name']!r}")
        if len(t["t"].replace("…", "")) < min(4, len(t["name"])):
            out.append(f"short: {t['t']!r} for {t['name']!r}")
        if t["cut"]:
            out.append(f"cut: {t['t']!r}")
    out += [f"on top of each other: {a!r} / {b!r}" for a, b in facts["hits"]]
    return out


ROOM = 24  # the canvas's run-out past the cut


def fit_region(payload: dict) -> tuple[float, float]:
    """The fit region, from the payload (round 4, the Fable ruling): the
    picture's extent and 10% of it after when the picture covers under 25%
    of the running time, else the whole cut."""
    pics = [c for c in payload["clips"] if c["kind"] in ("spine", "video")]
    dur = max([float(Fraction(c["end"])) for c in payload["clips"]] + [float(Fraction(payload["duration"]))])
    if not pics:
        return 0.0, dur
    a = min(float(Fraction(c["start"])) for c in pics)
    b = max(float(Fraction(c["end"])) for c in pics)
    if (b - a) / dur >= 0.25:
        return 0.0, dur
    return a, min(dur, b + (b - a) * 0.1)


def picture_median(payload: dict) -> float:
    pics = sorted(float(Fraction(c["duration"])) for c in payload["clips"] if c["kind"] in ("spine", "video"))
    m = len(pics) // 2
    return pics[m] if len(pics) % 2 else (pics[m - 1] + pics[m]) / 2


def zoom_problems(facts: dict, payload: dict) -> list[str]:
    """Round 4's zoom rule at open: the fit region fills the track from its
    start, unless its median picture clip would be under 16 px there, in
    which case the track opens at a 64 px median from the region's start."""
    r0, r1 = fit_region(payload)
    med = picture_median(payload)
    fit = (facts["scW"] - facts["gutter"] - ROOM) / (r1 - r0)
    want = 64 / med if med * fit < 16 else fit
    out = []
    if abs(facts["pps"] - want) > want * 0.005:
        out.append(f"opens at {facts['pps']:.3f} px/s, the rule says {want:.3f} (region {r0:.2f}-{r1:.2f} s, median {med:.2f} s)")
    if abs(facts["scrollLeft"] - r0 * facts["pps"]) > 1.5:
        out.append(f"opens scrolled to {facts['scrollLeft']} px, the region starts at {r0 * facts['pps']:.1f} px")
    return out


def fit_problems(facts: dict, payload: dict = None, opening: bool = True) -> list[str]:
    """The fit gates: at open, round 4's zoom rule (above); the overview
    carrying the whole cut; no sideways page scroll."""
    out = zoom_problems(facts, payload) if opening and payload is not None else []
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
TC_RE = re.compile(r"\d\d:\d\d:\d\d:\d\d")


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_every_fixture_fits_and_every_name_reads(browser, payloads, fixture, width):
    payload = payloads[fixture]
    facts = _facts(browser, timeline_app.render_page_html(payload), width)
    assert fit_problems(facts, payload) == []
    assert label_problems(facts) == []
    names = [t for t in facts["texts"] if t["name"] is not None]
    assert names, "nothing named at all"
    accent = {palette.dark("accent").upper(), palette.light("accent").upper()}
    cyan = [t for t in facts["texts"] if _hex(t["color"]) in accent]
    # cyan is a record time and nothing else: the header's and the Selection's playhead
    assert cyan and all(TC_RE.fullmatch(t["t"]) and (t["id"] == "tc" or "rec" in t["cls"]) for t in cyan), cyan


@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_track_area_is_over_a_third_of_the_page_at_1440(browser, payloads, fixture):
    facts = _facts(browser, timeline_app.render_page_html(payloads[fixture]), 1440)
    assert facts["tracks"] / facts["pageH"] >= 0.35, (facts["tracks"], facts["pageH"])


@pytest.mark.parametrize("width,picture,short", [(1440, 88, 72), (390, 56, 56)])
def test_clip_heights_follow_the_lane_type(browser, payloads, width, picture, short):
    """Picture lanes 88 px, sound 72 px on a desktop; 56 px on a phone; a
    title too narrow for its name is a 24 px bar (round 3)."""
    facts = _facts(browser, timeline_app.render_page_html(payloads["interview"]), width)
    seen = {(c["kind"], round(c["h"])) for c in facts["clips"] if c["h"]}  # (a collapsed lane's clips are not drawn)
    assert seen == {("spine", picture), ("title", 24), ("audio", short)}, seen


# ---- each gate, broken on purpose ------------------------------------------------

def _stress() -> dict:
    """A storyline clip too narrow for its long name, between two wide ones."""
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0]
    p = timeline_app.build_payload(tl)
    tiny = next(c for c in p["clips"] if c["duration"] == "2/1")
    tiny["name"] = "Interview_take_twelve"
    return p


def _set(browser, html: str, payload: dict, width: int = 1440, script: str = None):
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(html)
        page.evaluate("p => window.__fcpApp.setPayload(p)", json.loads(json.dumps(payload)))
        page.wait_for_timeout(50)
        return page.evaluate(script or FACTS)
    finally:
        page.close()


def test_the_short_label_gate_goes_red(browser):
    """Mutation check: names cut to two characters are seen."""
    html = timeline_app.render_app_html()
    old = 'var lab = el("div", "label", name.text);'
    assert html.count(old) == 1
    assert label_problems(_set(browser, html, _stress())) == []
    broken = label_problems(_set(browser, html.replace(old, 'var lab = el("div", "label", name.text.slice(0, 2));'), _stress()))
    assert any(p.startswith("short") for p in broken), broken


def test_the_overlap_gate_goes_red(browser, payloads):
    """Marker rows that do not look at what is already on them: the
    interview's marker names land on each other."""
    html = timeline_app.render_page_html(payloads["interview"])
    old = "var clear = function (r, bx) { return !rows[r].some(function (q) { return bx[0] < q[1] + GAP && q[0] < bx[1] + GAP; }); };"
    assert html.count(old) == 1
    assert label_problems(_facts(browser, html, 1440)) == []
    broken = label_problems(_facts(browser, html.replace(old, "var clear = function (r, bx) { return true; };"), 1440))
    assert any(p.startswith("on top of each other") for p in broken), broken


def test_the_cut_label_gate_goes_red(browser, payloads):
    """Names measured as if a clip had no inset run out of their boxes, and
    with the fit pass (the net that hides such a name) gone they are seen cut."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = "var SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, INSET = 6, BORDER = 1,"
    net = 'sc.querySelectorAll(".clip .label").forEach(function (l) { l.classList.toggle("unfit", l.scrollWidth > l.clientWidth + 0.5); });'
    assert html.count(old) == 1 and html.count(net) == 1
    assert label_problems(_facts(browser, html, 390)) == []
    broken = html.replace(old, "var SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, INSET = -30, BORDER = 1,").replace(net, "")
    found = _set(browser, broken, payloads["gym"], 390)
    assert any(p.startswith("cut") for p in label_problems(found)), label_problems(found)


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


@pytest.mark.parametrize("fixture,width,zoomed", [("gym", 1440, False), ("gym", 390, False), ("music-video", 1440, False),
                                                  ("music-video", 390, False), ("interview", 1440, False),
                                                  ("interview", 430, True), ("interview", 390, True)])
def test_the_track_opens_on_the_fit_region(browser, payloads, fixture, width, zoomed):
    """Item 2 (round 4): the gym whole at every width, the interview whole at
    1440 and zoomed in at phone width, the music video on its picture."""
    payload = payloads[fixture]
    facts = _facts(browser, timeline_app.render_page_html(payload), width)
    assert zoom_problems(facts, payload) == []
    med = picture_median(payload) * facts["pps"]
    assert (abs(med - 64) < 0.5) == zoomed, med
    r0, r1 = fit_region(payload)
    assert (r1 - r0 < facts["extent"] - 1e-6) == (fixture == "music-video")


@pytest.mark.parametrize("old,new,fixture,width", [
    # round 3's rule: the whole cut, always (the music video's picture in 9% of the track)
    ("var R = fitRegion(), med", "var R = [0, extent()], med", "music-video", 1440),
    # no median rule: 44 cuts of 6 px at phone width
    ("if (med > 0 && med * state.pps < MEDIAN_MIN)", "if (false)", "interview", 390),
], ids=["whole-cut", "no-median-zoom"])
def test_the_zoom_gate_goes_red(browser, payloads, old, new, fixture, width):
    html = timeline_app.render_page_html(payloads[fixture])
    assert html.count(old) == 1
    assert zoom_problems(_facts(browser, html.replace(old, new), width), payloads[fixture])


def test_the_track_area_gate_goes_red(browser, payloads):
    """0.33's 44 px lanes put the gym's tracks under a third of the page."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = ":root { --clip-h: 88px; --clip-h-short: 72px; }"
    assert html.count(old) == 1
    facts = _facts(browser, html.replace(old, ":root { --clip-h: 36px; --clip-h-short: 36px; }"), 1440)
    assert facts["tracks"] / facts["pageH"] < 0.35


# ---------------------------------------------------------------------------
# Item 1 (round 4, the Fable ruling): a name inside its clip, whole, or not
# at all; no name rows, no leaders, no scrim pills; the hint strip counts them
# ---------------------------------------------------------------------------

NAMES = r"""() => {
  const sc = document.getElementById('scroller').getBoundingClientRect(), gutter = window.__fcpApp.gutter;
  const left = sc.left + gutter, right = sc.right - 1;
  const textBox = (e) => { const t = [...e.childNodes].filter(n => n.nodeType === 3), r = document.createRange();
    r.setStart(t[0], 0); r.setEnd(t[t.length - 1], t[t.length - 1].length); return r.getBoundingClientRect(); };
  const labels = [...document.querySelectorAll('#canvas .clip .label')].filter(l => getComputedStyle(l).visibility !== 'hidden' && !l.closest('.collapsed'));
  const shown = labels.filter(l => { const b = textBox(l); return b.width > 0 && b.left >= left - 0.5 && b.right <= right + 0.5; });
  return {
    rows: document.querySelectorAll('#canvas .rail, #canvas .rl').length,
    labels: labels.map(l => { const c = l.closest('.clip'), cb = c.getBoundingClientRect(), b = textBox(l), cs = getComputedStyle(l);
      const cover = parseFloat(l.style.left || '0');
      return { name: c.__c.name, text: l.firstChild.textContent, size: cs.fontSize, bg: cs.backgroundColor, image: cs.backgroundImage,
               inLeft: b.left - cb.left - cover, inRight: cb.right - b.right }; }),
    shown: shown.length, total: window.__fcpApp.state.payload.clips.length,
    count: (document.getElementById('count') || {}).textContent || null };
}"""


def name_problems(facts: dict) -> list[str]:
    out = []
    if facts["rows"]:
        out.append(f"{facts['rows']} name rows")
    for lab in facts["labels"]:
        if lab["text"] != lab["name"]:
            out.append(f"{lab['name']!r} drawn as {lab['text']!r}")
        if lab["size"] != "11px":
            out.append(f"{lab['name']!r} in {lab['size']} type")
        if lab["bg"] not in ("rgba(0, 0, 0, 0)", "transparent") or lab["image"] != "none":
            out.append(f"{lab['name']!r} on a pill ({lab['bg']}, {lab['image'][:40]})")
        if lab["inLeft"] < 1 + 6 - 0.5 or lab["inRight"] < 1 + 6 - 0.5:
            out.append(f"{lab['name']!r} {lab['inLeft']:.1f}/{lab['inRight']:.1f} px from its clip's edges (inset 6)")
    n, total = facts["shown"], facts["total"]
    want = f"{n} of {total} names shown" + (" · tap or zoom" if n < total else "")
    if facts["count"] != want:
        out.append(f"the hint says {facts['count']!r}, the page shows {n} of {total}")
    return out


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_names_are_inside_whole_or_absent_and_counted(browser, payloads, fixture, width):
    facts = _eval(browser, timeline_app.render_page_html(payloads[fixture]), width, NAMES)
    assert name_problems(facts) == [], facts


@pytest.mark.parametrize("old,new", [
    # round 3's 12 px names
    (".clip .label, .meter.label { font: 600 11px/14px", ".clip .label, .meter.label { font: 600 12px/15px"),
    # a count of the storyline only (the brief's example was 44, the cut has 58 clips)
    ("var n = 0, total = state.payload.clips.length,", "var n = 0, total = state.payload.clips.filter(function (c) { return !c.lane; }).length,"),
    # round 3's scrim pill back
    ("  .clip .label { position: absolute; top: 0; left: 0; max-width: 100%; z-index: 7; padding: 3px 6px;",
     "  .clip .label { background: var(--raised); position: absolute; top: 0; left: 0; max-width: 100%; z-index: 7; padding: 3px 6px;"),
], ids=["12px", "storyline-count", "pill"])
def test_the_name_gate_goes_red(browser, payloads, old, new):
    html = timeline_app.render_page_html(payloads["interview"])
    assert html.count(old) == 1
    assert name_problems(_eval(browser, html.replace(old, new), 1440, NAMES))


def test_a_name_that_does_not_fit_is_absent_never_cut(browser):
    """A storyline clip too narrow for its long name shows no name (round 3
    showed "A very…ke 12"); the hint counts it, a tap names it."""
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0]
    payload = timeline_app.build_payload(tl)
    long_name = "A very long interview clip name, take 12"
    clip = next(c for c in payload["clips"] if c["kind"] == "spine" and c["duration"] == "7/1")
    clip["name"] = long_name
    facts = _facts(browser, timeline_app.render_page_html(payload), 1440)
    assert next(c["label"] for c in facts["clips"] if c["name"] == long_name) is None
    assert label_problems(facts) == []


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


# ---------------------------------------------------------------------------
# Markers (item 6, round 4): every marker in view named right of its own
# glyph, or in a bracketed run, only where the names truly cannot all fit
# ---------------------------------------------------------------------------

MARKS = r"""() => {
  const scEl = document.getElementById('scroller'), sc = scEl.getBoundingClientRect(), gutter = window.__fcpApp.gutter;
  const left = sc.left + gutter, right = sc.right - 1, st = window.__fcpApp.state, p = st.payload;
  const canvas = document.getElementById('canvas').getBoundingClientRect(), xOf = (t) => canvas.left + gutter + t * st.pps;
  const fr = (n) => { const q = String(n).split('/'); return q.length === 2 ? q[0] / q[1] : Number(q[0]); };
  const textOf = (m) => { const t = [...m.childNodes].filter(n => n.nodeType === 3), r = document.createRange();
    if (!t.length) return null; r.setStart(t[0], 0); r.setEnd(t[t.length - 1], t[t.length - 1].length); return r.getBoundingClientRect(); };
  const seen = (e) => getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none' && getComputedStyle(e).color !== 'rgba(0, 0, 0, 0)';
  const widthOf = {}; document.querySelectorAll('#canvas .mlabel').forEach(m => { widthOf[m.dataset.t] = Number(m.dataset.w); });
  // in view: its glyph and its name (as a ruler time: whole in view, or not drawn until a scroll)
  const inView = [...document.querySelectorAll('#canvas .mlabel')].filter(m => {
    const x = xOf(Number(m.dataset.t)); return x - 4 >= left - 4.5 && x + Number(m.dataset.w) <= right; }).length;
  const named = [...document.querySelectorAll('#canvas .mlabel')].filter(seen).map(m => {
    const g = m.querySelector('.mg').getBoundingClientRect(), tb = textOf(m) || { left: 0, right: 0, width: 0 };
    return { text: m.textContent, title: m.title.replace(/ \([a-z]+\)$/, ''), gl: g.left, gr: g.right, tl: tb.left, tr: tb.right,
             row: m.classList.contains('row1') ? 1 : 0, named: tb.width > 0 && tb.left >= left - 0.5 && tb.right <= right + 0.5 };
  });
  const chips = [...document.querySelectorAll('#canvas .mchip')].map(c => {
    const b = c.querySelector('.mb').getBoundingClientRect(), ct = textOf(c.querySelector('.ct')), times = c.dataset.times.split(',');
    return { text: c.querySelector('.ct').textContent, count: Number(c.dataset.count), listed: c.title.split(' · ').length,
             xs: times.map(t => xOf(Number(t))), ws: times.map(t => widthOf[t]), bl: b.left, br: b.right, tl: ct.left, tr: ct.right,
             row: c.classList.contains('row1') ? 1 : 0 };
  });
  const flags = [...document.querySelectorAll('#canvas .flag')].filter(seen).map(f => { const b = f.getBoundingClientRect();
    return { l: b.left, r: b.right, row: f.classList.contains('row1') ? 1 : 0 }; });
  return { named, chips, flags, inView, left, right, viewW: sc.right - left };
}"""


def _could_name(members, obstacles, left, right) -> bool:
    """Whether every member of a run could be named in the free space of the
    two marker rows (6 px clear of everything else and of each other)."""
    rows = [list(o) for o in obstacles]
    for x, w in members:
        a, b = x - 4, x + w
        for r in (0, 1):
            if a >= left - 4.5 and b <= right and not any(a < q[1] + 6 and q[0] < b + 6 for q in rows[r]):
                rows[r].append((a, b))
                break
        else:
            return False
    return True


def marker_problems(facts: dict) -> list[str]:
    """Round 4: every named marker's whole name is just right of its own
    glyph; a run is a bracket from its first marker to its last with its
    chip centred on it, spans at most 25% of the view, lists its markers,
    and holds markers whose names could NOT all be named in the free space;
    and the markers drawn (named + in runs) are every marker in view."""
    out, drawn = [], 0
    rows = [[], []]
    for g in facts["named"]:
        rows[g["row"]].append((g["gl"], g["tr"]))
    for f in facts["flags"]:
        rows[f["row"]].append((f["l"], f["r"]))
    for c in facts["chips"]:
        rows[c["row"]].append((min(c["bl"], c["tl"]), max(c["br"], c["tr"])))
    for g in facts["named"]:
        if not g["named"]:
            out.append(f"{g['title']!r}: glyph without its name")
            continue
        if not (g["gr"] - 0.5 <= g["tl"] <= g["gr"] + 10):
            out.append(f"{g['title']!r}: its name is not just right of its own glyph")
        if g["text"] != g["title"]:
            out.append(f"{g['title']!r} drawn as {g['text']!r}")
        drawn += 1
    for c in facts["chips"]:
        n = c["count"]
        if not (n == c["listed"] == len(c["xs"])) or c["text"] != f"{n} marker" + ("s" if n != 1 else ""):
            out.append(f"chip {c['text']!r} lists {c['listed']} of {len(c['xs'])}")
        if abs(c["bl"] - (c["xs"][0] - 1)) > 1.5 or abs(c["br"] - (c["xs"][-1] + 1)) > 1.5:
            out.append(f"chip {c['text']!r}: its bracket {c['bl']:.1f}-{c['br']:.1f} is not its run {c['xs'][0]:.1f}-{c['xs'][-1]:.1f}")
        if abs((c["tl"] + c["tr"]) / 2 - (c["bl"] + c["br"]) / 2) > 1.5:
            out.append(f"chip {c['text']!r} is not centred on its bracket")
        if c["xs"][-1] - c["xs"][0] > 0.25 * facts["viewW"] + 1:
            out.append(f"chip {c['text']!r} spans {c['xs'][-1] - c['xs'][0]:.0f} px of a {facts['viewW']:.0f} px view")
        own = (min(c["bl"], c["tl"]), max(c["br"], c["tr"]))
        others = [[q for q in rows[r] if q != own] for r in (0, 1)]
        if _could_name(list(zip(c["xs"], c["ws"])), others, facts["left"], facts["right"]):
            out.append(f"chip {c['text']!r} brackets markers whose names fit")
        drawn += n
    if drawn != facts["inView"]:
        out.append(f"{drawn} markers drawn, {facts['inView']} in view")
    return out


def _marks(browser, html: str, width: int, scheme: str = "dark", clicks: int = 0) -> dict:
    return _eval(browser, html, width, MARKS, scheme, clicks)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_every_marker_in_view_is_named_or_in_a_bracketed_run(browser, payloads, fixture, width):
    facts = _marks(browser, timeline_app.render_page_html(payloads[fixture]), width)
    assert marker_problems(facts) == [], facts
    assert facts["named"], "no marker drawn at all"


def _crowded(width_names: int = 12) -> dict:
    """sample.fcpxml with twelve long-named markers every 1.2 s: at 390 they
    cannot all be named, so some runs are bracketed."""
    p = timeline_app.build_payload(FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0])
    for c in p["clips"]:
        c["markers"] = []
    p["markers"] = [{"name": f"Beat number {i + 1}", "start": f"{6 * i + 3}/5", "type": "standard"} for i in range(width_names)]
    return p


def _two_close() -> dict:
    """Two markers a second apart: at 390 both are named, one on each row."""
    p = timeline_app.build_payload(FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0])
    for c in p["clips"]:
        c["markers"] = []
    p["markers"] = [{"name": "Opening", "start": "2/1", "type": "chapter"}, {"name": "Interview", "start": "3/1", "type": "chapter"}]
    return p


def _split_case() -> dict:
    """Nine markers in 10 s at 390 where the runs must split: bracketed as one
    run they would span 102 px of a 356 px view (found by a search over
    random layouts; the split rule keeps two runs of 64 and 77 px)."""
    p = timeline_app.build_payload(FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0])
    for c in p["clips"]:
        c["markers"] = []
    names = ["Check the name spelling", "Check the name spelling", "Chorus line", "B-roll here", "Chorus line", "Verse",
             "Fix the audio dip", "Chorus line", "Verse"]
    p["markers"] = [{"name": n, "start": f"{t}/10", "type": "standard"} for n, t in zip(names, (15, 23, 53, 65, 68, 71, 101, 113, 116))]
    return p


def _zoomed_out(browser, html: str, width: int, clicks: int, script: str) -> dict:
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(html)
        page.wait_for_timeout(50)
        for _ in range(clicks):
            page.click("#zout")
        page.wait_for_timeout(50)
        return page.evaluate(script)
    finally:
        page.close()


def test_crowded_markers_are_bracketed_and_never_far_apart(browser, payloads):
    """Twelve long names 1.2 s apart at 390, and the interview's fourteen
    markers with the whole 18 minutes in a phone's width: runs, each within
    a quarter of the view, and only where the names cannot all fit."""
    facts = _set(browser, timeline_app.render_app_html(), _crowded(), 390, MARKS)
    assert facts["chips"] and marker_problems(facts) == [], facts
    whole = _zoomed_out(browser, timeline_app.render_page_html(payloads["interview"]), 390, 6, MARKS)
    assert whole["chips"] and whole["inView"] == 14 and marker_problems(whole) == [], whole
    two = _set(browser, timeline_app.render_app_html(), _two_close(), 390, MARKS)
    assert len(two["named"]) == 2 and not two["chips"] and marker_problems(two) == [], two
    split = _set(browser, timeline_app.render_app_html(), _split_case(), 390, MARKS)
    assert len(split["chips"]) == 2 and marker_problems(split) == [], split


@pytest.mark.parametrize("old,new,ids", [
    # a marker with no room silently dropped (no run)
    ("      if (prev && reach(prev, f))", "      if (f.marks.forEach(function (mk) { mk.el.classList.add('gone'); }) || items.splice(fail, 1)) {} else if (prev && reach(prev, f))", "dropped"),
    # runs with no limit (round 3's "7 markers" from 9.3 to 17.4 min)
    ("var reach = function (p, q) { return q.marks[q.marks.length - 1].x - p.marks[0].x <= reachPx; };",
     "var reach = function (p, q) { return true; };", "unsplit"),
    # one row only: a marker that fits on the second is bracketed
    ("for (var r = 0; r < 2 && row < 0; r++) if (inView(bx[0], bx[1]) && clear(r, bx)) row = r;",
     "for (var r = 0; r < 1 && row < 0; r++) if (inView(bx[0], bx[1]) && clear(r, bx)) row = r;", "names-that-fit"),
], ids=["dropped", "unsplit", "names-that-fit"])
def test_the_marker_gate_goes_red(browser, payloads, old, new, ids):
    html = timeline_app.render_app_html()
    assert html.count(old) == 1
    case = {"dropped": _crowded, "unsplit": _split_case, "names-that-fit": _two_close}[ids]()
    found = marker_problems(_set(browser, html.replace(old, new), case, 390, MARKS))
    assert found and (ids != "unsplit" or any("spans" in f for f in found)), found


FLAT_HIDE = (".playhead, .flash, .trans, .conn, .range, .clip .label "
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
                # the waveform's own fill is not empty: under its quietest column it is sound
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
    the sound has dynamics (round 4: an envelope on the tone), so on the
    absolute scale every clip's loudest column reaches the band's top."""
    assert flat_bands(browser, timeline_app.render_page_html(payloads["gym"]), width, scheme) == []


def test_the_empty_band_gate_goes_red(browser, payloads):
    """Mutation check: abb76b1's clip (the filmstrip stopping 24 px short of
    the foot, the waveform under it) is seen."""
    html = timeline_app.render_page_html(payloads["gym"])
    new = ".clip.has-thumb .wave { height: var(--wave-h); background: var(--raised-2); }"
    assert html.count(new) == 1
    broken = html.replace(new, ".clip.has-thumb .wave { height: 12px; background: var(--panel); }")
    assert flat_bands(browser, broken, 1440, "dark")


ANATOMY = r"""() => [...document.querySelectorAll('#canvas .clip.has-thumb.has-wave')].map(c => {
  const t = c.querySelector('.thumbs').getBoundingClientRect(), w = c.querySelector('.wave').getBoundingClientRect();
  const p = getComputedStyle(c.querySelector('.wave path'));
  return { name: c.__c.name, thumbBottom: t.bottom, waveTop: w.top, waveBottom: w.bottom, clipBottom: c.getBoundingClientRect().bottom,
           fill: p.fill, fillOpacity: Number(p.fillOpacity), stroke: p.stroke, ground: getComputedStyle(c.querySelector('.wave')).backgroundColor,
           panel: getComputedStyle(document.getElementById('scroller')).backgroundColor, page: getComputedStyle(document.body).backgroundColor };
})"""


def anatomy_problems(clips: list) -> list[str]:
    """Round 3: the waveform in its OWN band under the filmstrip (they meet,
    they do not overlap), filled solid. Round 4: the band on the clip's own
    grey, never the page's or the track's (a page-coloured gap between the
    filmstrip and the sound)."""
    out = []
    for c in clips:
        if abs(c["thumbBottom"] - c["waveTop"]) > 0.6:
            out.append(f"{c['name']}: the filmstrip ends at {c['thumbBottom']:.1f}, the waveform starts at {c['waveTop']:.1f}")
        if c["fill"] in ("none", "") or c["fillOpacity"] < 1:
            out.append(f"{c['name']}: the waveform is not filled ({c['fill']}, {c['fillOpacity']})")
        if c["ground"] in ("rgba(0, 0, 0, 0)", "transparent", c["panel"], c["page"]):
            out.append(f"{c['name']}: the sound band is the page's colour ({c['ground']})")
    return out


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [1440, 390])
def test_the_waveform_has_its_own_band_under_the_filmstrip(browser, payloads, width, scheme):
    clips = _eval(browser, timeline_app.render_page_html(payloads["gym"]), width, ANATOMY, scheme)
    assert len(clips) == 7 and anatomy_problems(clips) == []


@pytest.mark.parametrize("old,new", [
    (".clip.has-thumb.has-wave .thumbs { bottom: calc(3px + var(--wave-h)); }", ""),  # fa9b811: the filmstrip to the foot
    (".clip.has-thumb .wave { height: var(--wave-h); background: var(--raised-2); }",
     ".clip.has-thumb .wave { height: var(--wave-h); background: var(--panel); }"),  # 035392a: the band on the panel
], ids=["overlay", "page-coloured"])
def test_the_anatomy_gate_goes_red(browser, payloads, old, new):
    html = timeline_app.render_page_html(payloads["gym"])
    assert html.count(old) == 1
    assert anatomy_problems(_eval(browser, html.replace(old, new), 1440, ANATOMY))


WAVE = r"""() => [...document.querySelectorAll('#canvas .clip.has-wave')].map(c => {
  const svg = c.querySelector('.wave svg'), band = c.querySelector('.wave').getBoundingClientRect(), box = svg.getBoundingClientRect();
  const nums = c.querySelector('.wave path').getAttribute('d').match(/-?[\d.]+/g).map(Number), pts = [];
  for (let i = 0; i < nums.length; i += 2) pts.push([nums[i], nums[i + 1]]);
  return { name: c.__c.name, levels: c.__c.wave, pts, n: svg.viewBox.baseVal.width, vh: svg.viewBox.baseVal.height,
           band: [band.top, band.bottom, band.left, band.right], svg: [box.top, box.bottom, box.left, box.right] };
})"""


def wave_problems(clips: list) -> list[str]:
    """Item 5 (round 4): one bottom-anchored column per level, on an
    absolute -60..0 dBFS scale: column i is (level / 100) of the band tall
    (round 3 scaled every clip to its own loudest peak), and the drawing
    fills its band."""
    out = []
    for c in clips:
        if any(abs(a - b) > 0.6 for a, b in zip(c["svg"], c["band"])):
            out.append(f"{c['name']}: the waveform does not fill its band")
        n, lv = c["n"], c["levels"]
        if n != len(lv) or c["vh"] != 100:
            out.append(f"{c['name']}: {n} columns for {len(lv)} levels")
            continue
        pts = c["pts"]
        # M0,100 then, per column, its top-left and top-right corners, then down to the foot
        if len(pts) != 2 * n + 2 or pts[0] != [0, 100] or pts[-1] != [n, 100]:
            out.append(f"{c['name']}: not one bottom-anchored column per level ({len(pts)} points for {n})")
            continue
        for i, v in enumerate(lv):
            want = 100 - max(0, min(100, v))
            a, z = pts[1 + 2 * i], pts[2 + 2 * i]
            if (a[0], z[0]) != (i, i + 1) or abs(a[1] - want) > 0.01 or abs(z[1] - want) > 0.01:
                out.append(f"{c['name']}: column {i} at level {v} is drawn {100 - a[1]:.2f}% tall")
                break
    return out


def _wave_case() -> dict:
    """The gym-shaped sample: two clips whose loudest peaks are -24 and -6
    dBFS (levels 60 and 90): on one scale they are not the same height."""
    p = timeline_app.build_payload(FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0])
    spine = [c for c in p["clips"] if c["kind"] == "spine"]
    spine[0]["wave"] = [20, 40, 60, 40, 20, 0, 60]
    spine[1]["wave"] = [90, 30, 90, 50, 70]
    return p


def test_the_waveform_is_drawn_on_one_absolute_scale(browser):
    clips = _set(browser, timeline_app.render_app_html(), _wave_case(), 1440, WAVE)
    assert len(clips) == 2 and wave_problems(clips) == [], clips


@pytest.mark.parametrize("fixture", FIXTURES)
def test_every_fixture_waveform_is_on_the_scale(browser, payloads, fixture):
    clips = _eval(browser, timeline_app.render_page_html(payloads[fixture]), 1440, WAVE)
    assert clips and wave_problems(clips) == []


def test_the_wave_scale_gate_goes_red(browser):
    """Mutation check: 035392a's drawing, each clip scaled to its own peak."""
    html = timeline_app.render_app_html()
    old = 'var h = (100 - Math.max(0, Math.min(100, v))).toFixed(2);'
    assert html.count(old) == 1
    broken = html.replace(old, 'var h = (100 - Math.max(0, v) / (Math.max.apply(null, levels) || 1) * 100).toFixed(2);')
    assert wave_problems(_set(browser, broken, _wave_case(), 1440, WAVE))


def dynamics_problems(payload: dict) -> list[str]:
    """The gym's sound has dynamics (round 4): every clip's levels span at
    least 20 (12 dB). A steady tone, as until round 3, spans about 1."""
    return [f"{c['name']} ({c['id']}): levels {min(c['wave'])}-{max(c['wave'])}"
            for c in payload["clips"] if c.get("wave") and max(c["wave"]) - min(c["wave"]) < 20]


def test_the_gym_sound_has_dynamics(payloads):
    assert all(c.get("wave") for c in payloads["gym"]["clips"])
    assert dynamics_problems(payloads["gym"]) == []


def test_the_dynamics_check_goes_red(payloads):
    steady = json.loads(json.dumps(payloads["gym"]))
    for c in steady["clips"]:
        c["wave"] = [86 + (i % 2) for i in range(len(c["wave"]))]  # 0.2 x full scale, the old steady tone
    assert len(dynamics_problems(steady)) == 7


# ---------------------------------------------------------------------------
# The strip above the ruler, the tap, the playhead, cyan record times
# ---------------------------------------------------------------------------

STRIP = """() => { const s = document.getElementById('strip'), sc = document.getElementById('scroller').getBoundingClientRect();
  const ruler = document.querySelector('#canvas .ruler').getBoundingClientRect(), sb = s.getBoundingClientRect();
  const cyan = (e) => /rgb\\(0, 229, 255\\)|rgb\\(0, 122, 138\\)/.test(getComputedStyle(e).color);
  // anything outside the timeline's own canvas that sits over the tracks
  const over = [...document.body.querySelectorAll('*')].filter(e => !e.closest('#canvas') && e !== document.body && !e.contains(document.getElementById('scroller')) &&
    !e.closest('#scroller') && getComputedStyle(e).position !== 'static' && getComputedStyle(e).display !== 'none' && (() => {
      const b = e.getBoundingClientRect(); return b.width > 0 && b.left < sc.right && sc.left < b.right && b.top < sc.bottom && sc.top < b.bottom; })())
    .map(e => e.id || e.className);
  const zero = [...document.querySelectorAll('#canvas .tick')].find(t => t.textContent === '00:00:00:00');
  return { for: s.dataset.for || null, name: (s.querySelector('b') || {}).textContent || null,
           items: [...s.querySelectorAll('.si')].map(e => e.textContent), title: [...s.querySelectorAll('.si')].map(e => e.title),
           itemCyan: [...s.querySelectorAll('.si')].map(e => [...e.querySelectorAll('.rec')].filter(cyan).map(r => r.textContent).join('|') +
                                                          (cyan(e) ? '!' : '')),
           above: sb.bottom <= sc.top + 0.5 && sb.bottom <= ruler.top, over,
           sel: [...document.querySelectorAll('#sel dt')].map(d => [d.textContent, d.nextElementSibling.textContent, d.nextElementSibling.title, cyan(d.nextElementSibling)]),
           tc: document.getElementById('tc').textContent,
           zero: !!zero && getComputedStyle(zero).visibility !== 'hidden' && !zero.classList.contains('unfit') }; }"""


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


def _tap(browser, payload, html=None, width=390, pick="narrowest"):
    """Tap a clip that has no name inside it (the narrowest, or the one whose
    start puts the playhead inside "00:00:00:00"); what the page shows."""
    page = browser.new_page(viewport={"width": width, "height": 800}, color_scheme="dark")
    try:
        page.set_content(html or timeline_app.render_page_html(payload))
        page.wait_for_timeout(50)
        box = page.evaluate("""(pick) => { const zero = [...document.querySelectorAll('#canvas .tick')].find(t => t.textContent === '00:00:00:00');
            const zr = document.createRange(); zr.selectNodeContents(zero); const zb = zr.getBoundingClientRect();
            const all = [...document.querySelectorAll('#canvas .clip')];
            const c = pick === 'zero' ? all.find(e => { const r = e.getBoundingClientRect(); return r.left > zb.left + 4 && r.left < zb.right - 4; })
              : all.filter(e => !e.querySelector('.label')).sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width)[0];
            c.scrollIntoView(); const r = c.getBoundingClientRect();
            return { id: c.__c.id, name: c.__c.name, w: r.width, label: !!c.querySelector('.label'), x: r.left + r.width / 2, y: r.top + r.height / 2 }; }""", pick)
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
    assert not box["label"], box
    clip = next(c for c in payload["clips"] if c["id"] == box["id"])
    fps = Fraction(payload["fps"])
    start, length = _timecode(Fraction(clip["start"]), fps), _timecode(Fraction(clip["duration"]), fps)
    assert strip["for"] == box["id"] and strip["name"] == box["name"], strip
    assert strip["items"] == [f"start {start}", f"length {length}"] and strip["title"][1] == clip["duration"] + " s", strip
    assert strip["above"] and strip["over"] == [], strip
    sel = {k: (v, t) for k, v, t, _c in strip["sel"]}
    assert sel["start"][0] == start and sel["length"] == (length, clip["duration"] + " s") and strip["tc"] == start
    assert re.fullmatch(r"\d\d:\d\d:\d\d:\d\d", sel["source in"][0]) and sel["source in"][1] == clip["source_start"] + " s"


def cyan_problems(strip: dict) -> list[str]:
    """Item 8 (round 4): a record (timeline) time is cyan in the strip and in
    the Selection panel; a length or a source time is not."""
    out = []
    start = next(i for i in strip["items"] if i.startswith("start "))
    if strip["itemCyan"][strip["items"].index(start)] != start[len("start "):]:
        out.append(f"the strip's start is not cyan ({strip['itemCyan']})")
    if any(c and i.startswith("length ") for i, c in zip(strip["items"], strip["itemCyan"])):
        out.append("the strip's length is cyan")
    for k, _v, _t, cyan in strip["sel"]:
        if cyan != (k in ("start", "end")):
            out.append(f"Selection {k}: cyan {cyan}")
    return out


def test_record_times_are_cyan_and_00_stays_on_a_tap(browser, payloads):
    """Item 8: the tapped clip's start in the strip and its start and end in
    the Selection panel are cyan, its length and source in are not; a tap
    that puts the playhead inside "00:00:00:00" leaves it on the ruler."""
    box, strip = _tap(browser, payloads["music-video"], pick="zero")
    assert cyan_problems(strip) == [], strip
    assert strip["zero"], (box, strip)


@pytest.mark.parametrize("old,new,what", [
    ('function recEl(sec) { return el("span", "rec", tcOf(sec)); }', 'function recEl(sec) { return el("span", "plain", tcOf(sec)); }', "cyan"),
    # round 3: a ruler time under the playhead was hidden, and its head sat at the ruler's top
    (".playhead { position: absolute; top: 54px;", ".playhead { position: absolute; top: 33px;", "zero"),
], ids=["white-times", "zero-gone"])
def test_the_tap_gates_go_red(browser, payloads, old, new, what):
    html = timeline_app.render_page_html(payloads["music-video"])
    assert html.count(old) == 1
    if what == "zero":
        cover = 'var parts = [];\n    document.querySelectorAll("#canvas .flash")'
        assert html.count(cover) == 1
        html = html.replace(cover, 'var parts = [$("playhead").getBoundingClientRect()];\n    document.querySelectorAll("#canvas .flash")')
        html = html.replace('$("tc").textContent = tcOf(state.playhead);', '$("tc").textContent = tcOf(state.playhead); coverTicks();')
    _box, strip = _tap(browser, None, html.replace(old, new), pick="zero")
    assert (cyan_problems(strip) if what == "cyan" else not strip["zero"]), strip


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
    old = "var SNAP_PX = 8,"
    assert html.count(old) == 1
    cut, near = _drop_playhead(browser, None, 5, html.replace(old, "var SNAP_PX = 0,"))
    assert near != pytest.approx(cut, abs=1e-6)


ON_TOP = r"""() => {
  // the playhead moved into the middle of a name: what paints on top there
  const st = window.__fcpApp.state, canvas = document.getElementById('canvas').getBoundingClientRect();
  const out = [];
  [...document.querySelectorAll('#canvas .clip .label:not(.unfit)')].slice(0, 6).forEach(l => {
    const t = [...l.childNodes].filter(n => n.nodeType === 3), r = document.createRange(); r.setStart(t[0], 0); r.setEnd(t[0], t[0].length);
    const b = r.getBoundingClientRect(), x = (b.left + b.right) / 2;
    st.playhead = (x - canvas.left - window.__fcpApp.gutter) / st.pps; window.__fcpApp.placePlayhead();
    const ph = document.getElementById('playhead'); ph.style.pointerEvents = 'auto';
    const lx = ph.getBoundingClientRect().left + 1, top = document.elementsFromPoint(lx, (b.top + b.bottom) / 2)[0];
    ph.style.pointerEvents = '';
    out.push([l.textContent, !!top && (top === ph || ph.contains(top))]);
  });
  return out;
}"""


@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_playhead_is_on_top_of_every_name(browser, payloads, fixture):
    """Item 4 (round 4, the Fable ruling): the playhead is the top layer; a
    name never covers it (round 3 drew names over it)."""
    found = _eval(browser, timeline_app.render_page_html(payloads[fixture]), 1440, ON_TOP)
    assert found and all(top for _n, top in found), found


def test_the_on_top_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["gym"])
    old = "pointer-events: none; z-index: 30; }"
    assert html.count(old) == 1
    found = _eval(browser, html.replace(old, "pointer-events: none; z-index: 6; }"), 1440, ON_TOP)
    assert not any(top for _n, top in found), found


# ---------------------------------------------------------------------------
# Roles, sound, markers
# ---------------------------------------------------------------------------

def test_role_stripes_are_final_cuts_colours_three_px(browser, payloads):
    """The interview carries four of Final Cut's roles: titles (lane 1),
    dialogue (the cameras' sound), music (lane -1), effects (lane -2). Each
    stripe is 3 px of that role's colour, the same hex in both themes."""
    for scheme in ("dark", "light"):
        facts = _facts(browser, timeline_app.render_page_html(payloads["interview"]), 1440, scheme)
        seen = {}
        for c in facts["clips"]:
            role, colour, height = c["stripe"]
            assert round(height) == 3, c
            seen.setdefault(role, set()).add(_hex(colour))
        assert seen == {f"role-{r}": {palette.dark(f"role-{r}").upper()} for r in ("titles", "dialogue", "music", "effects")}, seen


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
           zero: !!zero && vis(zero) && !zero.classList.contains('unfit') && !zero.classList.contains('under-flash'),
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


FLASH = r"""() => {
  const ruler = document.querySelector('#canvas .ruler').getBoundingClientRect(), out = [];
  const flashes = [...document.querySelectorAll('#canvas .flash')].map(f => f.getBoundingClientRect());
  const textOf = (e) => { const t = [...e.childNodes].filter(n => n.nodeType === 3); if (!t.length) return null;
    const g = document.createRange(); g.setStart(t[0], 0); g.setEnd(t[t.length - 1], t[t.length - 1].length); return g.getBoundingClientRect(); };
  flashes.forEach(f => { if (Math.abs(f.top - (ruler.top + window.__fcpApp.rules.flashTop)) > 0.5)
    out.push('the flash line starts at ' + (f.top - ruler.top) + ' px down the ruler, not under the marker rows'); });
  const meets = (b, f) => b && f.left < b.right + 0.5 && b.left < f.right + 0.5 && f.top < b.bottom && b.top < f.bottom;
  document.querySelectorAll('#canvas .mlabel, #canvas .mchip, #canvas .flag').forEach(m => {
    if (getComputedStyle(m).visibility === 'hidden' || getComputedStyle(m).display === 'none') return;
    const parts = [...m.querySelectorAll('.mg, .mb, .mt')].map(g => g.getBoundingClientRect()).concat([textOf(m.querySelector('.ct') || m)]);
    flashes.forEach(f => { if (parts.some(b => meets(b, f))) out.push('the flash line crosses ' + m.textContent); });
  });
  document.querySelectorAll('#canvas .clip .label:not(.unfit)').forEach(e => {
    if (getComputedStyle(e).visibility === 'hidden' || e.closest('.collapsed')) return;
    const b = textOf(e), clip = e.closest('.clip');
    if (b.right > clip.getBoundingClientRect().right + 0.5) out.push(e.textContent + ' runs past its clip');
    flashes.forEach(f => { if (meets(b, f)) out.push(e.textContent + ' crosses the flash line'); });
  });
  return out;
}"""


@pytest.mark.parametrize("width", WIDTHS)
def test_the_flash_line_stops_under_the_marker_rows(browser, payloads, width):
    """Item 6 (round 4): the red flash line starts under the ruler's marker
    rows, so it cannot cross a glyph (round 3 ran it through the Press
    marker, a frame after it); no name crosses it."""
    assert _eval(browser, timeline_app.render_page_html(payloads["gym"]), width, FLASH) == []


def test_the_flash_gate_goes_red(browser, payloads):
    """Mutation check: 035392a's flash line, from the ruler's top."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = ".flash { position: absolute; top: 32px;"
    assert html.count(old) == 1
    found = _eval(browser, html.replace(old, ".flash { position: absolute; top: 0px;"), 1440, FLASH)
    assert any("Press" in f for f in found) and any("starts at" in f for f in found), found


# ---------------------------------------------------------------------------
# Round 3 and 4: lanes that collapse, transitions and titles as Final Cut
# draws them, connections, the overview, the header
# ---------------------------------------------------------------------------

LANES = r"""() => [...document.querySelectorAll('#canvas .lane')].map(l => ({ lane: l.dataset.lane, h: l.getBoundingClientRect().height,
  collapsed: l.classList.contains('collapsed') }))"""


def test_a_lane_with_nothing_in_view_collapses(browser, payloads):
    """A3: on the interview at 390 (zoomed in at open) the whooshes (lane -2,
    from 00:02:59) are out of view: their lane is a thin rule; the music bed
    (lane -1, from 0) stays."""
    lanes = {r["lane"]: r for r in _eval(browser, timeline_app.render_page_html(payloads["interview"]), 390, LANES, clicks=2)}
    assert lanes["-2"]["collapsed"] and lanes["-2"]["h"] <= 10, lanes
    assert not lanes["-1"]["collapsed"] and lanes["-1"]["h"] > 50, lanes


def test_the_collapse_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["interview"])
    old = 'row.classList.toggle("collapsed", !shown);'
    assert html.count(old) == 1
    lanes = {r["lane"]: r for r in _eval(browser, html.replace(old, 'row.classList.toggle("collapsed", false);'), 390, LANES, clicks=2)}
    assert lanes["-2"]["h"] > 10


TRANS = r"""() => {
  const t = document.querySelector('#canvas .trans'); if (!t) return null;
  const b = t.getBoundingClientRect(), bg = t.querySelector('.tbg'), fin = [...document.querySelectorAll('#canvas .clip')].find(c => c.__c.name === 'Finish');
  const lab = fin.querySelector('.label:not(.unfit)'), bow = t.querySelector('.bow'), tn = t.querySelector('.tn');
  const shown = (e) => !!e && getComputedStyle(e).visibility !== 'hidden';
  let nb = null; if (tn) { const r = document.createRange(); r.selectNodeContents(tn); nb = r.getBoundingClientRect(); }
  const cut = fin.getBoundingClientRect().left;
  return { w: b.width, spans: b.left < cut && cut < b.right, ownBg: getComputedStyle(t).backgroundColor,
           bgOpacity: bg ? Number(getComputedStyle(bg).opacity) : 1, bow: shown(bow), name: shown(tn) ? tn.textContent : null,
           nameWhole: !shown(tn) || (nb.left >= b.left && nb.right <= b.right - 1), title: t.title,
           labelAfter: lab ? lab.getBoundingClientRect().left >= b.right - 0.5 : null };
}"""


def trans_problems(t: dict) -> list[str]:
    """D8 (round 3) as round 4 rules it: a translucent overlay across both
    clips at the edit, at least 14 px wide at any width; its bow-tie and its
    name inside it, whole, or neither (a glyph never shows without its
    name); "Cross Dissolve 0.5s" in its title; the next clip's name after it."""
    out = []
    if not t or t["w"] < 14 - 0.01 or not t["spans"]:
        out.append(f"no overlay across the cut: {t}")
        return out
    if t["ownBg"] not in ("rgba(0, 0, 0, 0)", "transparent") or t["bgOpacity"] >= 1:
        out.append("an opaque slab")
    if t["bow"] and (t["name"] != "Cross Dissolve" or not t["nameWhole"]):
        out.append(f"a bow-tie with the name {t['name']!r} (whole: {t['nameWhole']})")
    if "Cross Dissolve 0.5s" not in t["title"]:
        out.append(f"title {t['title']!r}")
    if t["labelAfter"] is False:
        out.append("the next clip's name is under the overlay")
    return out


@pytest.mark.parametrize("width", WIDTHS)
def test_a_transition_is_drawn_as_final_cut_draws_it(browser, payloads, width):
    assert trans_problems(_eval(browser, timeline_app.render_page_html(payloads["gym"]), width, TRANS)) == []


def test_a_tapped_transition_names_itself_in_the_strip(browser, payloads):
    page = browser.new_page(viewport={"width": 390, "height": 800}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payloads["gym"]))
        page.wait_for_timeout(50)
        b = page.evaluate("() => { const r = document.querySelector('#canvas .trans').getBoundingClientRect(); return [r.left + r.width / 2, r.top + 10]; }")
        page.mouse.click(*b)
        page.wait_for_timeout(30)
        strip = page.evaluate(STRIP)
    finally:
        page.close()
    assert strip["for"] == "transition" and strip["name"] == "Cross Dissolve", strip
    assert strip["items"][1] == "length 00:00:00:12" and strip["itemCyan"][0], strip


@pytest.mark.parametrize("old,new", [
    ("background: var(--shade); opacity: .45; }", "background: var(--raised-2); opacity: 1; }"),  # fa9b811's slab
    ('if (Math.ceil(textW(name, "label")) + 16 + 2 * INSET <= b[1] - b[0]) {', "if (true) {"),  # a bow-tie with a cut name
], ids=["slab", "glyph-without-its-name"])
def test_the_transition_gate_goes_red(browser, payloads, old, new):
    html = timeline_app.render_page_html(payloads["gym"])
    assert html.count(old) == 1
    assert trans_problems(_eval(browser, html.replace(old, new), 1440, TRANS))


TITLES = r"""() => [...document.querySelectorAll('#canvas .clip.k-title')].map(c => {
  const lab = c.querySelector('.label:not(.unfit)');
  return { name: c.__c.name, bg: getComputedStyle(c).backgroundColor, h: c.getBoundingClientRect().height, inside: !!lab,
           named: !!c.querySelector('.label'), ink: lab ? getComputedStyle(lab).color : null };
})"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width,clicks", [(1440, 0), (390, 0), (1440, 8)])
def test_titles_are_titles_coloured_named_or_bars_and_read(browser, payloads, width, clicks, scheme):
    """D9: a title clip's body is the Titles role colour (Final Cut 12.4's,
    the same hex in both themes); one too narrow for its name is a 24 px
    bar; zoomed in, the lower thirds are named and their text reads 4.5:1 or
    better on the purple, measured in both themes (item 7)."""
    titles = _eval(browser, timeline_app.render_page_html(payloads["interview"]), width, TITLES, scheme, clicks)
    assert len(titles) == 8
    for t in titles:
        assert _hex(t["bg"]) == palette.dark("role-titles").upper() == "#7E53D2", t
        assert t["named"] or round(t["h"]) == 24, t  # (a name out of view hides until a scroll)
        if t["inside"]:
            assert palette.contrast(_hex(t["ink"]), _hex(t["bg"])) >= 4.5, t
    if clicks:
        assert any(t["inside"] for t in titles), titles


@pytest.mark.parametrize("old,new", [
    (".clip.k-title { background: var(--role-titles); border-color: var(--role-titles); }", ".clip.k-title { background: var(--raised-2); }"),
    (".clip.k-title .label { color: var(--on-media); }", ".clip.k-title .label { color: var(--mute); }"),
], ids=["grey-body", "low-contrast-name"])
def test_the_title_gate_goes_red(browser, payloads, old, new):
    html = timeline_app.render_page_html(payloads["interview"])
    assert html.count(old) == 1
    titles = _eval(browser, html.replace(old, new), 1440, TITLES, "dark", 8)
    assert any(_hex(t["bg"]) != "#7E53D2" or (t["inside"] and palette.contrast(_hex(t["ink"]), _hex(t["bg"])) < 4.5) for t in titles)


THIN = r"""() => ({ clips: [...document.querySelectorAll('#canvas .clip')].filter(c => c.__c.lane).map(c => [c.__c.name, c.getBoundingClientRect().width]),
  conns: [...document.querySelectorAll('#canvas .conn')].map(c => [getComputedStyle(c).borderLeftWidth, getComputedStyle(c, '::after').width]) })"""


@pytest.mark.parametrize("width", WIDTHS)
def test_no_connected_clip_is_thinner_than_6_px(browser, payloads, width):
    """D10: the interview's 2 s whooshes are 6 px at least (fa9b811: 2 px);
    connection lines 2 px with a 6 px anchor dot."""
    facts = _eval(browser, timeline_app.render_page_html(payloads["interview"]), width, THIN)
    assert all(w >= 6 - 0.01 for _n, w in facts["clips"] if w), facts["clips"]  # (a collapsed lane's clips are not drawn)
    assert facts["conns"] and all(c == ["2px", "6px"] for c in facts["conns"]), facts["conns"][:3]


def test_the_thin_clip_gate_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["interview"])
    old = "var MIN_W = 6,"
    assert html.count(old) == 1
    facts = _eval(browser, html.replace(old, "var MIN_W = 1,"), 1440, THIN)
    assert any(w < 6 for _n, w in facts["clips"])


STEMS = r"""() => {
  const spine = document.querySelector('#canvas .lane[data-lane="0"]');
  const parts = [...spine.querySelectorAll('.clip, .gap')].map(x => x.getBoundingClientRect());
  const covers = [...document.querySelectorAll('#canvas .clip, #canvas .gap')].map(x => x.getBoundingClientRect());
  return [...document.querySelectorAll('#canvas .conn')].map(e => {
    const b = e.getBoundingClientRect(), down = e.classList.contains('down');
    const dot = { l: b.left - 2, r: b.left + 4, t: down ? b.bottom - 6 : b.top, b: down ? b.bottom : b.top + 6 };
    const under = parts.filter(r => r.left <= b.left + 4 && b.left - 4 <= r.right)[0];
    return { down, end: down ? b.bottom : b.top, edge: under ? (down ? under.top : under.bottom) : null,
             covered: covers.filter(r => r.left < dot.r - 0.5 && dot.l + 0.5 < r.right && r.top < dot.b - 0.5 && dot.t + 0.5 < r.bottom).length };
  });
}"""


def stem_problems(stems: list) -> list[str]:
    """Item 8 (round 4): every connection stem ends at its anchor dot ON the
    storyline's edge, the dot clear of every clip so it shows (round 3's dot
    sat 4 px inside the storyline, behind it)."""
    out = []
    for i, s in enumerate(stems):
        if s["edge"] is None or abs(s["end"] - s["edge"]) > 0.6:
            out.append(f"stem {i}: ends at {s['end']:.1f}, the storyline's edge is at {s['edge']}")
        if s["covered"]:
            out.append(f"stem {i}: its anchor dot is behind a clip")
    return out


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_every_stem_ends_at_its_dot_on_the_storyline(browser, payloads, fixture, width):
    stems = _eval(browser, timeline_app.render_page_html(payloads[fixture]), width, STEMS)
    assert stems and stem_problems(stems) == []


def test_the_stem_gate_goes_red(browser, payloads):
    """Mutation check: 035392a's stems, to 4 px inside the storyline."""
    html = timeline_app.render_page_html(payloads["interview"])
    old = "var top = hit ? hit.offsetTop : 4,"
    assert html.count(old) == 1
    assert stem_problems(_eval(browser, html.replace(old, "var top = (hit ? hit.offsetTop : 4) + 4,"), 1440, STEMS))


OVERVIEW = r"""() => { const ov = document.getElementById('overview'), ob = ov.getBoundingClientRect(), p = window.__fcpApp.state.payload;
  const box = (e) => { const b = e.getBoundingClientRect(); return { t: b.top - ob.top, b: b.bottom - ob.top, w: b.width }; };
  return { h: ob.height, ground: getComputedStyle(ov).backgroundColor,
           bars: [...ov.querySelectorAll('.ob')].map(b => [b.__c.role || '', b.__c.kind, getComputedStyle(b).backgroundColor, box(b).t]),
           ticks: [...ov.querySelectorAll('.om')].map(box), flashes: [...ov.querySelectorAll('.of')].map(box),
           glyphs: ov.querySelectorAll('.mg, .og').length, window: !!ov.querySelector('.ov'),
           total: p.markers.length + p.clips.reduce((n, c) => n + (c.markers || []).length, 0),
           fails: (p.flashes || []).filter(f => f.verdict === 'FAIL').length }; }"""


def _role(role: str, kind: str) -> str:
    main = (role or "").split(".")[0].lower()
    if main in ("video", "titles", "dialogue", "music", "effects"):
        return main
    return "dialogue" if kind == "audio" else "titles" if kind == "title" else "video"


def overview_problems(o: dict, width: int) -> list[str]:
    """Item 3 (round 4): the overview is a minimap: at least 44 px tall at
    phone width; every lane's bars in their clips' role colours (3:1 or
    better on its ground); a 1 px tick per marker (no glyphs) in its own top
    band, the flash in a band of its own under it; the window."""
    out = []
    if width <= 430 and o["h"] < 44:
        out.append(f"{o['h']} px tall")
    for role, kind, colour, _top in o["bars"]:
        want = palette.dark(f"role-{_role(role, kind)}").upper()
        if _hex(colour) != want:
            out.append(f"a {kind} bar is {_hex(colour)}, its role colour is {want}")
            break
        if palette.contrast(_hex(colour), _hex(o["ground"])) < 3:
            out.append(f"a {kind} bar at {palette.contrast(_hex(colour), _hex(o['ground'])):.2f}:1")
            break
    if o["glyphs"] or len(o["ticks"]) != o["total"] or any(round(t["w"]) != 1 for t in o["ticks"]):
        out.append(f"{len(o['ticks'])} marker ticks of {o['total']}, {o['glyphs']} glyphs")
    tick_band = max((t["b"] for t in o["ticks"]), default=0)
    if len(o["flashes"]) != o["fails"] or any(f["t"] < tick_band for f in o["flashes"]):
        out.append("the flash is not in a band of its own under the markers")
    bars_top = min((b[3] for b in o["bars"]), default=99)
    if any(f["b"] > bars_top for f in o["flashes"]) or tick_band > bars_top:
        out.append("ticks over the lanes")
    if not o["window"]:
        out.append("no window")
    return out


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_overview_is_a_minimap(browser, payloads, fixture, width, scheme):
    o = _eval(browser, timeline_app.render_page_html(payloads[fixture]), width, OVERVIEW, scheme)
    assert overview_problems(o, width) == [], o


@pytest.mark.parametrize("old,new", [
    ("#overview .ob { position: absolute; border-radius: 1px; }", "#overview .ob { position: absolute; border-radius: 1px; background: var(--mute) !important; }"),
    ("#overview { position: relative; height: 48px;", "#overview { position: relative; height: 30px;"),
    ("#overview .om { position: absolute; top: 3px; width: 1px; height: 7px;", "#overview .om { position: absolute; top: 3px; width: 6px; height: 16px;"),
], ids=["grey-bars", "short", "glyph-ticks"])
def test_the_minimap_gate_goes_red(browser, payloads, old, new):
    html = timeline_app.render_page_html(payloads["gym"])
    assert html.count(old) == 1
    assert overview_problems(_eval(browser, html.replace(old, new), 390, OVERVIEW), 390)


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


LANE_TAGS = r"""() => { const sc = document.getElementById('scroller').getBoundingClientRect(), mute = getComputedStyle(document.body).getPropertyValue('--mute');
  return { gutter: window.__fcpApp.gutter, lanes: [...document.querySelectorAll('#canvas .lane:not(.collapsed)')].map(l => {
    const t = l.querySelector('.ltag'); if (!t) return { lane: l.dataset.lane, tag: null };
    const r = document.createRange(); r.selectNodeContents(t); const b = r.getBoundingClientRect();
    return { lane: l.dataset.lane, tag: t.textContent, colour: getComputedStyle(t).color, left: b.left - sc.left, right: b.right - sc.left,
             box: t.getBoundingClientRect().right - sc.left }; }) }; }"""


def lane_tag_problems(facts: dict, width: int, scheme: str = "dark") -> list[str]:
    """Item 10 (round 4): at 1440 every lane in view carries its grey tag
    (Storyline, Lane 1, Lane -1...) whole inside a gutter of 64 px, or as
    wide as its widest tag needs in the font drawn (CI's Linux font ran
    "Storyline" past 64 px); a phone keeps its 16 px gutter and no tags."""
    out = []
    wide = width >= 1000
    if (facts["gutter"] < 64) if wide else (facts["gutter"] != 16):
        out.append(f"a {facts['gutter']} px gutter at {width}")
    mute = (palette.dark if scheme == "dark" else palette.light)("mute").upper()
    for lane in facts["lanes"]:
        if not wide:
            if lane["tag"] is not None:
                out.append(f"lane {lane['lane']} tagged at {width}")
            continue
        n = int(lane["lane"])
        want = "Storyline" if n == 0 else f"Lane {n}" if n > 0 else f"Lane \u2212{-n}"
        if lane["tag"] != want:
            out.append(f"lane {n} tagged {lane['tag']!r}, not {want!r}")
            continue
        if _hex(lane["colour"]) != mute:
            out.append(f"lane {n}'s tag is {_hex(lane['colour'])}, not the grey")
        if lane["left"] < 1 or lane["right"] > facts["gutter"] - 4 or lane["box"] > facts["gutter"]:
            out.append(f"lane {n}'s tag runs {lane['left']:.1f}-{lane['right']:.1f} px (box to {lane['box']:.1f}), the gutter is {facts['gutter']} px")
    return out


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_lanes_are_tagged_in_a_gutter_at_1440(browser, payloads, fixture, width, scheme):
    facts = _eval(browser, timeline_app.render_page_html(payloads[fixture]), width, LANE_TAGS, scheme)
    assert lane_tag_problems(facts, width, scheme) == [], facts
    assert width < 1000 or len([lane for lane in facts["lanes"] if lane["tag"]]) >= 2


@pytest.mark.parametrize("old,new,width", [
    ("var GUTTER_PHONE = GUTTER, GUTTER_WIDE = 64, WIDE_AT = 1000;", "var GUTTER_PHONE = GUTTER, GUTTER_WIDE = 64, WIDE_AT = 0;", 390),
    # a 64 px gutter whatever the font (CI's Linux font ran "Storyline" past it); a wide name stands in for that font here
    ("Math.max(GUTTER_WIDE, tagRoom())", "GUTTER_WIDE", 1440),
    ("background: var(--panel); border-right: 1px solid var(--line); white-space: nowrap; pointer-events: none; }",
     "background: var(--panel); border-right: 1px solid var(--line); white-space: nowrap; pointer-events: none; color: var(--accent); }", 1440),
    ('if (GUTTER > GUTTER_PHONE) { var tag = el("div", "ltag", laneName(lane.lane));', "if (false) { var tag;", 1440),
], ids=["tags-on-a-phone", "fixed-64", "cyan-tags", "no-tags"])
def test_the_lane_tag_gate_goes_red(browser, payloads, old, new, width):
    html = timeline_app.render_page_html(payloads["music-video"])
    assert html.count(old) == 1
    broken = html.replace(old, new)
    if new == "GUTTER_WIDE":  # the storyline's tag as wide as CI's font drew "Storyline": the gutter grows, or it runs past
        wide = 'return n === 0 ? "Storyline" :'
        assert broken.count(wide) == 1 and html.count(wide) == 1
        broken, html = (h.replace(wide, 'return n === 0 ? "The storyline" :') for h in (broken, html))
        def runs(h):
            f = _eval(browser, h, width, LANE_TAGS)
            return [lane["tag"] for lane in f["lanes"] if lane["tag"] and (lane["right"] > f["gutter"] - 4 or lane["box"] > f["gutter"])]
        assert runs(html) == [] and runs(broken), (runs(html), runs(broken))
        return
    assert lane_tag_problems(_eval(browser, broken, width, LANE_TAGS), width)


# ---------------------------------------------------------------------------
# Item 9 (round 4): nothing drawn collides with anything else drawn
# ---------------------------------------------------------------------------

COLLIDE = r"""(opts) => {
  const withPlayhead = !opts || opts.playhead !== false;
  const scEl = document.getElementById('scroller'), sc = scEl.getBoundingClientRect(), canvas = document.getElementById('canvas');
  const viewL = sc.left + 1, viewR = sc.right - 1;
  const vis = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden' && !e.closest('.collapsed');
  const prims = [];
  const box = (l, r, t, b) => ({ left: l, right: r, top: t, bottom: b });
  const add = (kind, owner, b, opt) => {
    const l = Math.max(b.left, viewL), r = Math.min(b.right, viewR);
    if (r - l <= 0.01 || b.bottom - b.top <= 0.01) return;
    prims.push(Object.assign({ kind, owner, l, r, t: b.top, b: b.bottom }, opt || {}));
  };
  // every text run drawn on the timeline
  const walker = document.createTreeWalker(canvas, NodeFilter.SHOW_TEXT);
  let n; while ((n = walker.nextNode())) {
    if (!n.textContent.trim()) continue;
    const e = n.parentElement; if (!vis(e) || getComputedStyle(e).color === 'rgba(0, 0, 0, 0)') continue;
    const g = document.createRange(); g.selectNodeContents(n);
    [...g.getClientRects()].forEach(b => add('text', e.closest('.clip, .mlabel, .mchip, .flag, .tick, .gap, .trans, .rail') || e, b,
                                             { name: JSON.stringify(n.textContent.trim().slice(0, 24)) }));
  }
  // glyphs and brackets
  canvas.querySelectorAll('.mg, .trans .bow, .mchip .mb, .mchip .mt').forEach(e => {
    if (vis(e)) add('glyph', e.closest('.mlabel, .mchip, .trans'), e.getBoundingClientRect(), { name: String(e.className.baseVal != null ? e.className.baseVal : e.className) }); });
  // ticks, each at its instant: the frame ticks, and every time's own line (its left border)
  canvas.querySelectorAll('.ftick').forEach(e => { if (vis(e)) { const b = e.getBoundingClientRect(); add('tick', e, b, { at: b.left, name: 'frame tick' }); } });
  canvas.querySelectorAll('.tick').forEach(e => { if (vis(e)) { const b = e.getBoundingClientRect(); add('tick', e, box(b.left, b.left + 1, b.top, b.bottom), { at: b.left, name: 'tick ' + e.textContent }); } });
  // the flash line, and its flag's border
  canvas.querySelectorAll('.flash').forEach(e => { if (vis(e)) { const b = e.getBoundingClientRect(); add('line', e, b, { at: b.left, name: 'flash' }); } });
  canvas.querySelectorAll('.flag').forEach(e => { if (vis(e)) { const b = e.getBoundingClientRect(); add('line', e, box(b.right - 2, b.right, b.top, b.bottom), { name: 'flag border' }); } });
  // a transition's overlay: a region over the edit
  canvas.querySelectorAll('.trans').forEach(e => { if (vis(e)) add('region', e, e.getBoundingClientRect(), { name: 'transition' }); });
  // leaders of any name row (round 3 had them)
  canvas.querySelectorAll('.rl').forEach(e => { if (vis(e)) { const b = e.getBoundingClientRect(), pc = getComputedStyle(e, '::before');
    if (pc.content !== 'none' && parseFloat(pc.height) > 0) add('line', e, box(b.left + parseFloat(pc.left || 0), b.left + parseFloat(pc.left || 0) + 1, b.top, b.top + parseFloat(pc.height)), { name: 'leader' }); } });
  // the playhead, at rest
  if (withPlayhead) {
    const ph = document.getElementById('playhead'), b = ph.getBoundingClientRect();
    add('line', ph, box(b.left, b.left + 2, b.top, b.bottom), { at: b.left, name: 'playhead' });
    add('line', ph, ph.querySelector('.phead').getBoundingClientRect(), { at: b.left, name: 'playhead head' });
  }
  // the connection stems: the parts not behind a clip or a gap (they are drawn at z -1), and the anchor dots
  const covers = [...canvas.querySelectorAll('.clip, .gap')].filter(vis).map(e => e.getBoundingClientRect());
  canvas.querySelectorAll('.conn').forEach(e => {
    const b = e.getBoundingClientRect(), x0 = b.left, x1 = b.left + 2, down = e.classList.contains('down');
    let segs = [[b.top, b.bottom]];
    covers.forEach(c => { if (c.left < x1 && x0 < c.right) segs = segs.flatMap(([t, u]) => {
      const o = []; if (c.top > t) o.push([t, Math.min(u, c.top)]); if (c.bottom < u) o.push([Math.max(t, c.bottom), u]);
      return o.filter(([p, q]) => q - p > 0.01); }); });
    segs.forEach(([t, u]) => add('line', e, box(x0, x1, t, u), { name: 'stem' }));
    add('glyph', e, box(b.left - 2, b.left + 4, down ? b.bottom - 6 : b.top, down ? b.bottom : b.top + 6), { name: 'anchor dot' });
  });
  const out = [];
  for (let i = 0; i < prims.length; i++) for (let j = i + 1; j < prims.length; j++) {
    const a = prims[i], c = prims[j];
    if (a.owner === c.owner) continue;
    if (a.at != null && c.at != null && Math.abs(a.at - c.at) <= 1.5) continue;  // two marks of one instant
    const ox = Math.min(a.r, c.r) - Math.max(a.l, c.l), oy = Math.min(a.b, c.b) - Math.max(a.t, c.t);
    if (ox > 0.5 && oy > 0.5) out.push(a.kind + ' ' + a.name + ' / ' + c.kind + ' ' + c.name + ' at ' + Math.round(Math.max(a.l, c.l)) + ',' + Math.round(Math.max(a.t, c.t)));
  }
  return { collisions: out, prims: prims.length, kinds: [...new Set(prims.map(p => p.name.split(' ')[0]))] };
}"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fixture", FIXTURES)
def test_nothing_drawn_collides(browser, payloads, fixture, width, scheme):
    """Item 9 (round 4): over every drawn primitive (text runs, marker glyphs
    and brackets, frame and time ticks, the flash and its flag, a
    transition's overlay, the stems' visible parts and their dots, the
    playhead at rest), no two overlap; two marks of one instant (the
    playhead on the 00:00:00:00 tick) are one mark. Round 3's check compared
    text with text and reported [] while the critics counted 14 or more."""
    found = _eval(browser, timeline_app.render_page_html(payloads[fixture]), width, COLLIDE, scheme)
    assert found["prims"] > 20 and found["collisions"] == [], found


def test_nothing_collides_after_a_tap(browser, payloads):
    """The playhead is a cursor once moved (it is on top, by the ruling);
    everything else still collides with nothing after each of three taps."""
    page = browser.new_page(viewport={"width": 390, "height": 800}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payloads["music-video"]))
        page.wait_for_timeout(50)
        for i in range(3):
            box = page.evaluate(f"""() => {{ const c = [...document.querySelectorAll('#canvas .clip')][{i}], r = c.getBoundingClientRect();
                return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }}; }}""")
            page.mouse.click(box["x"], box["y"])
            page.wait_for_timeout(30)
            assert page.evaluate(COLLIDE, {"playhead": False})["collisions"] == [], i
    finally:
        page.close()


LEADER_ROW = r"""() => {
  // round 3's name row put back under the storyline: one name on a leader, where a stem runs
  const spine = document.querySelector('#canvas .lane[data-lane="0"]'), stem = document.querySelector('#canvas .conn.up');
  const rail = document.createElement('div'); rail.className = 'rail below'; rail.style.cssText = 'position: relative; height: 16px;';
  const name = document.createElement('div'); name.className = 'rl'; name.textContent = 'Cam A 3';
  name.style.cssText = 'position: absolute; top: 0; padding: 0 4px; font: 600 12px/15px sans-serif; white-space: nowrap; left: ' + (stem.offsetLeft - 20) + 'px;';
  rail.appendChild(name); spine.after(rail); window.__fcpApp.fitLabels();
}"""


@pytest.mark.parametrize("mutation", ["leader-row", "no-inset"])
def test_the_collision_gate_goes_red(browser, payloads, mutation):
    """Mutation checks: round 3's name row and leader put back (a stem runs
    through it), and names with no inset (the playhead at rest on them)."""
    html = timeline_app.render_page_html(payloads["interview"])
    page = browser.new_page(viewport={"width": 1440, "height": 700}, color_scheme="dark")
    try:
        if mutation == "no-inset":
            old = "var SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, INSET = 6,"
            assert html.count(old) == 1
            html = html.replace(old, "var SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.01, INSET = 0,").replace(
                "white-space: nowrap; overflow: hidden; color: var(--ink); }", "white-space: nowrap; overflow: hidden; color: var(--ink); padding-left: 0; }")
        page.set_content(html)
        page.wait_for_timeout(50)
        if mutation == "leader-row":
            page.evaluate(LEADER_ROW)
        found = page.evaluate(COLLIDE)
    finally:
        page.close()
    assert found["collisions"], found


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
