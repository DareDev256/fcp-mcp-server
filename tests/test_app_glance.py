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


def fit_problems(facts: dict) -> list[str]:
    """The fit gates: the median clip under 44 px, the overview carrying less
    than the whole cut, a sideways page scroll."""
    out = []
    if facts["median"] < 44 - 0.01:
        out.append(f"median clip {facts['median']:.1f} px")
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
    """Picture lanes 88 px, titles and sound 72 px on a desktop; 56 px on a phone."""
    facts = _facts(browser, timeline_app.render_page_html(payloads["interview"]), width)
    seen = {(c["kind"], round(c["h"])) for c in facts["clips"]}
    assert seen == {("spine", picture), ("title", short), ("audio", short)}, seen


# ---- each gate, broken on purpose ------------------------------------------------

def _stress() -> dict:
    """A storyline clip too narrow for 4 characters of its long name, between
    two wide ones."""
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0]
    p = timeline_app.build_payload(tl)
    tiny = next(c for c in p["clips"] if c["duration"] == "1/4")
    tiny["name"] = "Interview_take_twelve"
    return p


def test_the_short_label_gate_goes_red(browser, payloads):
    html = timeline_app.render_app_html()
    old = "MEDIAN_MIN = 44, LABEL_MIN = 28, KEEP_MIN = 4,"
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
    broken = problems(html.replace(old, "MEDIAN_MIN = 44, LABEL_MIN = 4, KEEP_MIN = 1,"))
    assert any(p.startswith("short") for p in broken), broken


def test_the_overlap_gate_goes_red(browser, payloads):
    """Every rail name on one row: OVERLAY 1-3 land on each other."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "var row = cx > rowEnd[0] + 4 ? 0 : cx > rowEnd[1] + 4 ? 1 : -1;"
    assert html.count(old) == 1
    assert label_problems(_facts(browser, html, 1440)) == []
    broken = label_problems(_facts(browser, html.replace(old, "var row = 0;"), 1440))
    assert any(p.startswith("on top of each other") for p in broken), broken


def test_the_cut_label_gate_goes_red(browser, payloads):
    """Names measured without their padding run out of their boxes, and with
    the fit pass (the net that hides such a name) gone they are seen cut."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = "SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.25, PAD = 16, TIGHT = 8;"
    net = "    placePlayhead();\n    fitLabels();\n    drawOverview();\n"
    assert html.count(old) == 1 and html.count(net) == 1
    assert label_problems(_facts(browser, html, 390)) == []
    broken = html.replace(old, "SNAP_PX = 8, GUTTER = 16, MIN_PPS = 0.25, PAD = -40, TIGHT = -40;").replace(net, "    placePlayhead();\n    drawOverview();\n")
    assert any(p.startswith("cut") for p in label_problems(_facts(browser, broken, 390)))


def test_the_overview_gate_goes_red(browser, payloads):
    """An overview that maps only the part in view covers less than the cut."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "var W = ov.clientWidth, H = ov.clientHeight, dur = extent(), lanes = p.lanes;"
    assert html.count(old) == 1
    broken = html.replace(old, "var W = ov.clientWidth, H = ov.clientHeight, dur = (sc.clientWidth - GUTTER) / state.pps, lanes = p.lanes;")
    assert fit_problems(_facts(browser, html, 390)) == []
    assert any("overview" in p for p in fit_problems(_facts(browser, broken, 390)))


def test_the_median_gate_goes_red(browser, payloads):
    """The 0.32 fit (the whole cut in the width, nothing else) makes the music
    video's shots slivers at 390."""
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "state.pps = Math.max(MIN_PPS, w / extent(), (MEDIAN_MIN + 1) / medianDuration());"
    assert html.count(old) == 1
    broken = fit_problems(_facts(browser, html.replace(old, "state.pps = Math.max(MIN_PPS, w / extent());"), 390))
    assert any(p.startswith("median") for p in broken), broken


def test_the_track_area_gate_goes_red(browser, payloads):
    """0.33's 44 px lanes put the gym's tracks under a third of the page."""
    html = timeline_app.render_page_html(payloads["gym"])
    old = ":root { --clip-h: 88px; --clip-h-short: 72px; }"
    assert html.count(old) == 1
    facts = _facts(browser, html.replace(old, ":root { --clip-h: 36px; --clip-h-short: 36px; }"), 1440)
    assert facts["tracks"] / facts["pageH"] < 0.35


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


SHEET = """() => { const s = document.getElementById('sheet');
  return { shown: !s.hidden && getComputedStyle(s).display !== 'none', for: s.dataset.for || null,
           name: (s.querySelector('.sn') || {}).textContent || null,
           rows: [...s.querySelectorAll('dt')].map(d => [d.textContent, d.nextElementSibling.textContent]), sel: [...document.querySelectorAll('#sel dt')].map(d => [d.textContent, d.nextElementSibling.textContent]),
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
        return box, page.evaluate(SHEET)
    finally:
        page.close()


def test_a_clip_with_no_name_inside_says_it_in_the_tap_sheet(browser, payloads):
    """Under 28 px a clip draws no name; a tap opens the sheet with its name,
    start and length, and the selection panel shows its start timecode, which
    the playhead (the cyan record time) now reads too."""
    payload = payloads["music-video"]
    box, sheet = _tap(browser, payload)
    assert box["w"] < 28, box
    clip = next(c for c in payload["clips"] if c["id"] == box["id"])
    fps, start = Fraction(payload["fps"]), Fraction(clip["start"])
    frames = int(start * fps + Fraction(1, 2))
    tc = f"{frames // 24 // 3600:02d}:{frames // 24 // 60 % 60:02d}:{frames // 24 % 60:02d}:{frames % 24:02d}"
    length = Fraction(clip["duration"])
    assert sheet["shown"] and sheet["for"] == box["id"], sheet
    assert sheet["name"] == box["name"]
    assert dict(sheet["rows"]) == {"start": tc, "length": f"{float(length):.3f}s · {round(length * fps)}f"}, sheet
    assert dict(sheet["sel"])["start"] == tc and sheet["tc"] == tc


def test_the_tap_sheet_check_goes_red(browser, payloads):
    html = timeline_app.render_page_html(payloads["music-video"])
    old = "if (ce && !(lab && !lab.classList.contains(\"unfit\"))) openSheet(ce); else closeSheet();"
    assert html.count(old) == 1
    _box, sheet = _tap(browser, None, html.replace(old, "closeSheet();"))
    assert not sheet["shown"]


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
