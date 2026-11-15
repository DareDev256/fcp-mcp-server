"""The README's timeline picture (0.34 round 3): the gym cut at 1440, dark.

    uv run --extra dev --with playwright --with pillow python scripts/readme_timeline.py [work_dir]

Builds ``demo/fixtures/gym_session.py`` into ``work_dir`` (default
``demo/fixtures/.work``), takes the payload the server's own ``view`` tool
builds for it (real thumbnails and waveform peaks; the journal and the index
off), rewrites every local path to ``/fcp-demo``, draws it in headless
Chromium at 1440 px in the dark theme, and saves
``docs/assets/timeline-preview.png`` ONLY when the picture holds what the cut
holds:

- every clip with a picture has its filmstrip, and no filmstrip measures flat
  (pixel spread 0: an empty clip);
- every clip with sound has its waveform;
- the markers drawn on the ruler (each named, or counted in a cluster chip)
  are as many as the FCPXML holds.

Otherwise it prints why and exits 1 without writing anything. Until 0.34 the
README showed the music video drawn from its FCPXML alone: empty clips, an
empty bed, none of its markers. ``tests/test_readme_picture.py`` runs
:func:`problems` on that render and sees it refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "assets" / "timeline-preview.png"
WIDTH = 1440

MEASURE = r"""() => {
  const clips = [...document.querySelectorAll('#canvas .clip')].map(c => ({
    id: c.__c.id, name: c.__c.name, kind: c.__c.kind, media: !!c.__c.media, sound: !!(c.__c.wave && c.__c.wave.length),
    thumb: !!c.querySelector('.thumbs'), wave: !!c.querySelector('.wave svg path'),
    box: (() => { const t = c.querySelector('.thumbs'); if (!t) return null; const b = t.getBoundingClientRect();
                  return { x: b.left + 2, y: b.top + 2, width: Math.max(1, b.width - 4), height: Math.max(1, b.height - 4) }; })() }));
  // markers only: a transition's name in the marker rows (0.34 round 5) is not a marker
  const named = [...document.querySelectorAll('#canvas .mlabel:not(.tlabel)')].filter(m => getComputedStyle(m).visibility !== 'hidden').length;
  const clustered = [...document.querySelectorAll('#canvas .mchip')].reduce((n, c) => n + Number(c.dataset.count || 0) - Number(c.dataset.trans || 0), 0);
  return { clips, named, clustered };
}"""


def expected_markers(fcpxml: str) -> int:
    """Every marker the FCPXML holds: on the sequence (a gap's included) and on clips."""
    sys.path.insert(0, str(REPO))
    from fcpxml.parser import FCPXMLParser

    tl = FCPXMLParser().parse_file(fcpxml).timelines[0]
    return len(tl.markers) + sum(len(c.markers) for c in list(tl.clips) + list(tl.connected_clips))


def problems(page, markers_in_file: int) -> list[str]:
    """What is wrong with the timeline drawn on ``page`` (already loaded)."""
    from PIL import Image

    facts = page.evaluate(MEASURE)
    out = []
    pictures = [c for c in facts["clips"] if c["kind"] in ("spine", "video")]
    if not pictures:
        out.append("no clip with a picture")
    # the filmstrip alone: names, the playhead, a flash and a transition drawn over it are hidden while it is measured
    page.evaluate("""() => { const st = document.createElement('style'); st.id = 'readme-measure';
        st.textContent = '#canvas .label, #canvas .playhead, #canvas .flash, #canvas .trans { visibility: hidden !important; }';
        document.head.appendChild(st); }""")
    for c in pictures:
        if not c["thumb"]:
            out.append(f"{c['name']} ({c['id']}): no filmstrip")
            continue
        im = Image.open(BytesIO(page.screenshot(clip=c["box"]))).convert("L")
        lo, hi = im.getextrema()
        if hi - lo == 0:
            out.append(f"{c['name']} ({c['id']}): the filmstrip is flat (pixel spread 0)")
    page.evaluate("() => document.getElementById('readme-measure').remove()")
    for c in facts["clips"]:
        if c["media"] and c["kind"] in ("spine", "video", "audio") and not c["wave"]:
            out.append(f"{c['name']} ({c['id']}): no waveform")
    drawn = facts["named"] + facts["clustered"]
    if drawn != markers_in_file:
        out.append(f"{drawn} markers drawn ({facts['named']} named, {facts['clustered']} in clusters), the FCPXML holds {markers_in_file}")
    return out


def neutral(payload: dict, *roots: Path) -> dict:
    text = json.dumps(payload)
    for root in roots:
        text = text.replace(str(root), "/fcp-demo")
    return json.loads(text)


def main(argv: list[str]) -> int:
    work = Path(argv[1]).resolve() if len(argv) > 1 else REPO / "demo" / "fixtures" / ".work"
    os.environ["FCP_MCP_JOURNAL"] = "off"
    os.environ["FCP_MCP_INDEX"] = "off"
    edited = work / "edited.fcpxml"
    if not edited.is_file():
        subprocess.run([sys.executable, str(REPO / "demo" / "fixtures" / "gym_session.py"), str(work)], check=True)
    sys.path.insert(0, str(REPO))
    import server
    from fcpxml import timeline_app

    reply = asyncio.run(server.call_tool("view", {"action": "view_timeline", "args": {"filepath": str(edited)}}))
    payload = neutral(reply.structured, work, REPO)
    html = timeline_app.render_page_html(payload)
    for needle in ("/Users/", str(Path.home())):
        if needle in html:
            print(f"REFUSED: the page carries a local path ({needle!r})")
            return 1
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": 400}, color_scheme="dark")
        page.set_content(html)
        page.wait_for_timeout(300)
        found = problems(page, expected_markers(str(edited)))
        if found:
            browser.close()
            print("REFUSED, nothing saved:\n- " + "\n- ".join(found))
            return 1
        page.screenshot(path=str(OUT), full_page=True)
        browser.close()
    print(f"saved {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
