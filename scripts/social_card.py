"""Build the GitHub social preview card: 1280x640, from the palette and a real frame.

    uv run --with playwright scripts/social_card.py     # from the repo root

Writes ``docs/assets/social-card.png``. The picture is
``docs/assets/card-frame.png``: the prompt and the tool calls beside the
finished ``ui://fcp/timeline`` app, as the hero recorder captured them from a
real ``view_timeline`` result (``demo/hero/build.py``). "Talking to Claude"
is the claim, so the conversation is in the picture; the idle selection
panel is not. Re-record the hero and re-run this to refresh it.

The card carries no version and no star count. The card it replaces showed
a version and a star count that stayed frozen for seventeen releases, because
a number printed into an image goes stale the day it is uploaded and GitHub
has no API to replace the image. Uploading is one step in the repo's Settings
(Social preview); ``scripts/check_social_preview.py`` then compares what
github.com serves with this file.

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
import image_text  # noqa: E402
import logo  # noqa: E402

ASSETS = REPO / "docs" / "assets"
FRAME = ASSETS / "card-frame.png"
OUT = ASSETS / "social-card.png"
WIDTH, HEIGHT = 1280, 640

NAME = "fcp-mcp-server"
PROMISE = "Edit Final Cut Pro by talking to Claude, and watch the cut change."
SUBTITLE = "reads and writes FCPXML, no plugin, no patch"
INSTALL = "uvx fcp-mcp-server"


def html(frame_png: bytes) -> str:
    t = palette.dark
    shot = "data:image/png;base64," + base64.b64encode(frame_png).decode("ascii")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; width: {WIDTH}px; height: {HEIGHT}px; overflow: hidden; background: {t("ground")}; }}
  /* One centred column, header over picture: no empty band whatever the picture's height. */
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, "Helvetica Neue", sans-serif; color: {t("ink")};
         display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 30px; }}
  #head {{ width: 1168px; box-sizing: border-box; padding: 0 6px; display: grid;
          grid-template-columns: auto 1fr auto; column-gap: 18px; row-gap: 10px; align-items: center; }}
  #mark {{ width: 58px; height: 58px; grid-row: 1 / span 2; align-self: start; }}
  #mark svg {{ width: 58px; height: 58px; display: block; }}
  #name {{ font-size: 46px; line-height: 1; font-weight: 700; letter-spacing: -0.035em; margin: 0; }}
  #install {{ justify-self: end; font: 600 18px/1 ui-monospace, "SF Mono", Menlo, monospace; color: {t("accent")};
             border: 1px solid {t("line-strong")}; border-radius: 8px; padding: 10px 13px; }}
  #promise {{ grid-column: 2 / span 2; margin: 0; font-size: 25px; line-height: 1.2; font-weight: 600;
             letter-spacing: -0.01em; }}
  #subtitle {{ grid-column: 2 / span 2; margin: -4px 0 0; font-size: 17px; color: {t("mute")}; }}
  #shot {{ width: 1168px; flex: none;
          border: 1px solid {t("line")}; border-radius: 14px; overflow: hidden; background: {t("ground")}; }}
  #shot img {{ display: block; width: 100%; }}
</style></head><body>
<div id="head">
  <div id="mark">{logo.svg(glyphs=False)}</div>
  <h1 id="name">{NAME}</h1>
  <div id="install">{INSTALL}</div>
  <p id="promise">{PROMISE}</p>
  <p id="subtitle">{SUBTITLE}</p>
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
             x1: Math.max(...rs.map(r => r.right)), y1: Math.max(...rs.map(r => r.bottom)), lines: rs.length }; };
  return { texts: [text("name"), text("promise"), text("subtitle"), box("install")],
           mark: box("mark"), shot: box("shot") };
}"""


def collisions(layout: dict, gap: float = 12) -> list[str]:
    """Every overlap, every box off the card, every wrapped line. Empty means clean."""
    problems = []
    boxes = [layout["mark"], *layout["texts"]]
    shot = layout["shot"]
    for b in boxes:
        if b["y1"] > shot["y0"] - gap:
            problems.append(f"{b['id']} runs to y={b['y1']:.0f}, into the picture at y={shot['y0']:.0f}")
        if b.get("lines", 1) > 1:
            problems.append(f"{b['id']} wraps onto {b['lines']} lines")
    for b in [*boxes, shot]:
        if b["x0"] < 0 or b["y0"] < 0 or b["x1"] > WIDTH or b["y1"] > HEIGHT:
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
        # nothing a release would make wrong: no price, model id, frame size, version or star count
        problems += image_text.stale_in(page.evaluate("document.body.innerText"))
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
