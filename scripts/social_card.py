"""Build the GitHub social preview card: 1280x640, from the palette and a real frame.

    uv run --with playwright scripts/social_card.py     # from the repo root

Writes ``docs/assets/social-card.png``. The picture on the right is
``docs/assets/timeline-app-frame.png``, the ``ui://fcp/timeline`` app as the
hero recorder captured it drawing a real ``view_timeline`` result
(``demo/hero/build.py``); re-record the hero and re-run this to refresh it.

The card carries no version and no star count. The card it replaces showed
a version and a star count that stayed frozen for seventeen releases, because
a number printed into an image goes stale the day it is uploaded and GitHub
has no API to replace the image. Uploading is one step in the repo's Settings (Social preview);
``scripts/check_social_preview.py`` then compares what github.com serves with
this file.

Before writing, the layout is measured in the browser and the script REFUSES
(exit 1) when any text box overlaps the picture or another text box, or runs
off the card: a collision is the first thing anyone notices on a card.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from fcpxml import palette  # noqa: E402

sys.path.insert(0, str(REPO / "scripts"))
import logo  # noqa: E402

ASSETS = REPO / "docs" / "assets"
FRAME = ASSETS / "timeline-app-frame.png"
OUT = ASSETS / "social-card.png"
WIDTH, HEIGHT = 1280, 640

NAME = "fcp-mcp-server"
SUBTITLE = "reads and writes FCPXML, no plugin, no patch"
PROMISE = "Edit Final Cut Pro by talking to Claude, and watch the cut change."
INSTALL = "uvx fcp-mcp-server"


def html(frame_png: bytes) -> str:
    t = palette.dark
    shot = "data:image/png;base64," + base64.b64encode(frame_png).decode("ascii")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; width: {WIDTH}px; height: {HEIGHT}px; overflow: hidden; background: {t("ground")}; }}
  body {{ position: relative; font-family: ui-sans-serif, system-ui, -apple-system, "Helvetica Neue", sans-serif;
         color: {t("ink")}; }}
  #copy {{ position: absolute; left: 64px; top: 64px; width: 470px; bottom: 64px;
          display: flex; flex-direction: column; }}
  #mark {{ width: 64px; height: 64px; margin: 0 0 28px -6px; }}
  #mark svg {{ width: 64px; height: 64px; display: block; }}
  #name {{ font-size: 60px; line-height: 1; font-weight: 700; letter-spacing: -0.035em; margin: 0; }}
  #subtitle {{ margin: 16px 0 0; font-size: 21px; line-height: 1.3; color: {t("mute")}; }}
  #promise {{ margin: 34px 0 0; font-size: 31px; line-height: 1.22; font-weight: 600; letter-spacing: -0.01em; }}
  #install {{ margin-top: auto; align-self: flex-start; font: 600 19px/1 ui-monospace, "SF Mono", Menlo, monospace;
             color: {t("accent")}; border: 1px solid {t("line-strong")}; border-radius: 8px; padding: 11px 14px; }}
  #shot {{ position: absolute; left: 568px; top: 57px; width: 680px; border-radius: 14px;
          border: 1px solid {t("line-strong")}; overflow: hidden; background: {t("ground")}; }}
  #shot img {{ display: block; width: 100%; }}
</style></head><body>
<div id="copy">
  <div id="mark">{logo.svg()}</div>
  <h1 id="name">{NAME}</h1>
  <p id="subtitle">{SUBTITLE}</p>
  <p id="promise">{PROMISE}</p>
  <div id="install">{INSTALL}</div>
</div>
<div id="shot"><img alt="" src="{shot}"></div>
</body></html>"""


MEASURE = """() => {
  const box = (id) => { const r = document.getElementById(id).getBoundingClientRect();
    return { id, x0: r.left, y0: r.top, x1: r.right, y1: r.bottom }; };
  // text measured by its line boxes, not its block, so a short last line is not counted wide
  const text = (id) => { const el = document.getElementById(id), range = document.createRange();
    range.selectNodeContents(el); const rs = [...range.getClientRects()];
    return { id, x0: Math.min(...rs.map(r => r.left)), y0: Math.min(...rs.map(r => r.top)),
             x1: Math.max(...rs.map(r => r.right)), y1: Math.max(...rs.map(r => r.bottom)) }; };
  return { texts: ["name", "subtitle", "promise", "install"].map(id => id === "install" ? box(id) : text(id)),
           mark: box("mark"), shot: box("shot") };
}"""


def collisions(layout: dict, gap: float = 20) -> list[str]:
    """Every overlap, and every box off the card. Empty means the card is clean."""
    problems = []
    boxes = [layout["mark"], *layout["texts"]]
    shot = layout["shot"]
    for b in boxes:
        if b["x1"] > shot["x0"] - gap:
            problems.append(f"{b['id']} runs to x={b['x1']:.0f}, into the picture at x={shot['x0']:.0f}")
        if b["x0"] < 0 or b["y0"] < 0 or b["y1"] > HEIGHT:
            problems.append(f"{b['id']} is off the card: {b}")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if a["x0"] < b["x1"] and b["x0"] < a["x1"] and a["y0"] < b["y1"] and b["y0"] < a["y1"]:
                problems.append(f"{a['id']} overlaps {b['id']}")
    return problems


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not FRAME.is_file():
        print(f"{FRAME.relative_to(REPO)} is missing: run demo/hero/build.py first", file=sys.stderr)
        return 1
    page_html = html(FRAME.read_bytes())
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        page.set_content(page_html)
        page.wait_for_load_state("load")
        problems = collisions(page.evaluate(MEASURE))
        if problems:
            browser.close()
            print("refusing to write the card:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        page.screenshot(path=str(OUT))
        browser.close()
    print(f"{OUT.relative_to(REPO)}  {WIDTH}x{HEIGHT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
