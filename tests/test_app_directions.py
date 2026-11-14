"""0.33 directions: the view app drawn two ways for James to pick from.

``render_page_html(payload, direction="a" | "b")`` puts ``data-direction`` on
the page and nothing else; without it the page is the one 0.32 shipped. Both
directions are held, in a real headless Chromium, to the rules the plan
fixed for 0.33: the median clip at least 44 px wide on open, an overview bar
holding the whole cut, no visible clip or rail name under 4 characters and
none cut by its box, a name too long for a connected clip on its lane's rail,
a middle ellipsis that keeps the ending on a storyline clip, a glyph per
marker kind, cyan text for record time only, and no two texts on top of
each other at 1440 or 390.
"""

from __future__ import annotations

import os
import re
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

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MARKERS = ('<marker start="48048/24000s" duration="1001/24000s" value="Hook"/>'
           '<chapter-marker start="192192/24000s" duration="1001/24000s" value="Verse 1" posterOffset="0s"/>'
           '<marker start="288288/24000s" duration="1001/24000s" value="Fix colour" completed="0"/>')


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
    bed clip (the fixture has none), and a waveform for the bed."""
    text = (EXAMPLES / "music-video.fcpxml").read_text()
    bed = 'audioRole="music"/>'
    assert text.count(bed) == 1
    path = tmp_path / "mv.fcpxml"
    path.write_text(text.replace(bed, f'audioRole="music">{MARKERS}</asset-clip>'))
    tl = FCPXMLParser().parse_file(str(path)).timelines[0]
    return timeline_app.build_payload(tl, waveform=lambda media, start, dur: [(i * 37) % 100 for i in range(200)])


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
      texts.push({ t, cls: e.className, id: e.id, l: b.left, r: b.right, top: b.top, b: b.bottom, color: cs.color,
                   kept: e.dataset.kept ? Number(e.dataset.kept) : null,
                   cut: e.scrollWidth > e.clientWidth + 0.5 && /label|rl/.test(e.className) });
    }
  });
  const hits = [];
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    const a = texts[i], c = texts[j];
    if (a.l < c.r - 0.5 && c.l < a.r - 0.5 && a.top < c.b - 0.5 && c.top < a.b - 0.5) hits.push([a.t, c.t]);
  }
  const clips = [...document.querySelectorAll('#canvas .clip')];
  const widths = clips.map(c => c.getBoundingClientRect().width).sort((a, b) => a - b), m = widths.length >> 1;
  return { texts, hits, median: widths.length % 2 ? widths[m] : (widths[m - 1] + widths[m]) / 2,
           clips: clips.map(c => ({ name: c.__c.name, lane: c.__c.lane, label: (c.querySelector('.label') || {}).textContent || null })),
           rail: [...document.querySelectorAll('.rail .rl')].map(e => e.textContent),
           glyphs: [...document.querySelectorAll('.mlabel .mg')].map(e => e.className),
           overview: document.querySelectorAll('#overview .ob').length, box: !!document.getElementById('ovbox'),
           direction: document.documentElement.getAttribute('data-direction') };
}"""


def _facts(browser, html: str, width: int) -> dict:
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(html)
        page.wait_for_timeout(100)
        return page.evaluate(FACTS)
    finally:
        page.close()


def test_a_direction_is_an_attribute_and_nothing_else(tmp_path):
    payload = _music_video(tmp_path)
    shipped = timeline_app.render_page_html(payload)
    for d in timeline_app.DIRECTIONS:
        page = timeline_app.render_page_html(payload, direction=d)
        assert page.replace(f' data-direction="{d}"', "", 1) == shipped
    with pytest.raises(ValueError):
        timeline_app.render_page_html(payload, direction="c")


def test_the_waveform_is_carried_for_audio_clips_only(tmp_path):
    payload = _music_video(tmp_path)
    waves = {c["name"]: len(c.get("wave", [])) for c in payload["clips"]}
    assert waves["TRACK"] == 200 and all(n == 0 for name, n in waves.items() if name != "TRACK")
    assert "wave" not in timeline_app.build_payload(FCPXMLParser().parse_file(
        str(EXAMPLES / "music-video.fcpxml")).timelines[0])["clips"][0]


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("direction", ["a", "b"])
def test_each_direction_keeps_the_033_rules(browser, tmp_path, direction, width):
    payload = _music_video(tmp_path)
    facts = _facts(browser, timeline_app.render_page_html(payload, direction=direction), width)
    assert facts["direction"] == direction
    assert facts["median"] >= 44, facts["median"]
    assert facts["overview"] == len(payload["clips"]) and facts["box"]  # the whole cut, the view boxed
    assert facts["hits"] == [], facts["hits"]
    names = [t for t in facts["texts"] if t["kept"] is not None]
    assert names and all(t["kept"] >= 4 and not t["cut"] for t in names), names
    # a connected clip's name is whole inside it, or whole on its lane's rail
    for c in facts["clips"]:
        if c["lane"]:
            assert c["label"] == c["name"] or c["name"] in facts["rail"], c
    assert sorted(g.split()[-1] for g in facts["glyphs"]) == ["chapter", "standard", "todo"]
    accent = {palette.dark("accent").lower(), palette.light("accent").lower()}
    cyan = [t for t in facts["texts"] if _hex(t["color"]) in accent]
    assert [t["id"] for t in cyan] == ["tc"]  # the playhead's record time, and nothing else


def test_a_storyline_name_is_cut_in_the_middle_and_keeps_its_ending(browser):
    """A spine clip too narrow for its name shows its head and its ending
    ("A very…ke 12"), never fewer than 4 of its characters."""
    tl = FCPXMLParser().parse_file(str(EXAMPLES / "sample.fcpxml")).timelines[0]
    payload = timeline_app.build_payload(tl)
    long_name = "A very long interview clip name, take 12"
    # the 7 s clip: wide enough for a cut name (about 100 px), far too narrow for the whole one
    clip = next(c for c in payload["clips"] if c["kind"] == "spine" and c["duration"] == "7/1")
    clip["name"] = long_name
    for d in timeline_app.DIRECTIONS:
        facts = _facts(browser, timeline_app.render_page_html(payload, direction=d), 1440)
        label = next(c["label"] for c in facts["clips"] if c["name"] == long_name)
        assert label and "…" in label and label.endswith("12"), label
        head, tail = label.split("…")
        assert long_name.startswith(head) and long_name.endswith(tail) and len(head + tail) >= 4


def _hex(css: str) -> str:
    r, g, b = (int(v) for v in re.findall(r"\d+", css)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


# ---- pass 2: the gym fixture (real media), the flash, the ruler's zero, A's picture

GYM_BUILDER = Path(__file__).resolve().parent.parent / "demo" / "fixtures" / "gym_session.py"


@pytest.fixture(scope="module")
def gym(tmp_path_factory):
    """Built in its own process: the builder drives the server's handlers with
    asyncio.run, which cannot run inside Playwright's sync loop."""
    import shutil
    import subprocess
    import sys

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    out = tmp_path_factory.mktemp("gym")
    env = dict(os.environ, FCP_MCP_JOURNAL="off", FCP_MCP_INDEX="off", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, str(GYM_BUILDER), str(out)], check=True, capture_output=True, env=env, timeout=600)
    edited = out / "edited.fcpxml"
    return {"edited": edited, "timeline": FCPXMLParser().parse_file(str(edited)).timelines[0]}


def _frames(t) -> int:
    return int(t.frames)


def test_the_payload_carries_the_picture_flash(gym):
    """Bench (L1) ends one frame before Press starts: one frame of Squat. The
    payload says so, read from the parsed clips, as diagnose fails it."""
    tl = gym["timeline"]
    lane = tl.connected_clips[0]
    at = _frames(lane.timeline_start) + _frames(lane.duration)
    press = next(c for c in tl.clips if c.name == "Press")
    payload = timeline_app.build_payload(tl)
    (flash,) = payload["flashes"]
    assert flash["start"] == f"{Fraction(at, 24).numerator}/{Fraction(at, 24).denominator}"
    assert flash["frames"] == _frames(press.start) - at == 1 and flash["verdict"] == "FAIL"
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
           pictures: [...document.querySelectorAll('#canvas .lane:not(.linked) .clip')].map(c => c.classList.contains('has-thumb')),
           overflow: document.getElementById('scroller').scrollWidth - document.getElementById('scroller').clientWidth };
}"""


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("direction", ["a", "b"])
def test_the_flash_is_red_the_ruler_starts_at_zero_and_every_clip_has_a_picture(browser, gym, direction, width):
    from tools import view

    payload = timeline_app.build_payload(gym["timeline"], thumbnail=view._thumbnail, head_thumbs=True)
    page = browser.new_page(viewport={"width": width, "height": 700}, color_scheme="dark")
    try:
        page.set_content(timeline_app.render_page_html(payload, direction=direction))
        page.wait_for_timeout(100)
        facts = page.evaluate(GYM_FACTS)
    finally:
        page.close()
    assert facts["flash"]["inView"] and facts["flash"]["width"] >= 2 and facts["flash"]["height"] > 40, facts
    assert _hex(facts["flash"]["colour"]) == palette.dark("danger").lower()
    assert facts["flag"] == "flash 1f"
    assert facts["zero"], "00:00:00:00 is hidden"
    assert all(facts["pictures"]) and len(facts["pictures"]) == 7  # A has a picture in its thin clips too
    assert facts["overflow"] <= 1  # the 30 s cut fits: nothing scrolls sideways
