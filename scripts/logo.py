"""Draw the fcp-mcp-server logo from the palette.

    uv run --with playwright scripts/logo.py        # from the repo root

Writes ``docs/assets/logo.svg`` and ``docs/assets/logo-400.png`` (transparent
outside the tile), and the no-glyph mark ``logo-small.svg`` / ``logo-small-400.png``
for anything drawn at ``SMALL`` px or less (the README header, the card). The layout is the 0.14 logo's, three lanes, three clips,
a playhead through the middle clip and two ``</>`` glyphs, redrawn in code so
its colours are tokens (``fcpxml/palette.py``) instead of a purple bitmap that
could only be recoloured by hand. The glyphs are strokes, not text, so the SVG
looks the same wherever it is rendered.

Small sizes: the tile is a lifted grey with black lane slots, so it still
reads as a tile on a true-black page at 52 to 58 px (a black tile vanished
there), and ``svg(glyphs=False)`` drops the ``</>`` marks for anything drawn
at 96 px or smaller, where they turn to noise.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from fcpxml import palette  # noqa: E402

ASSETS = REPO / "docs" / "assets"
SMALL = 96  # px: at or below this, use the no-glyph mark (logo-small.*)


def svg(glyphs: bool = True) -> str:
    t = palette.dark
    lanes = "".join(
        f'<rect x="84" y="{y}" width="232" height="40" rx="10" fill="{t("ground")}"/>' for y in (124, 180, 236)
    )

    def glyph(x: float, y: float) -> str:
        # "</>" in strokes, 48 wide and 22 tall, centred on (x, y)
        return (
            f'<g fill="none" stroke="{t("ink")}" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round">'
            f'<polyline points="{x - 15},{y - 9} {x - 24},{y} {x - 15},{y + 9}"/>'
            f'<line x1="{x + 4}" y1="{y - 11}" x2="{x - 4}" y2="{y + 11}"/>'
            f'<polyline points="{x + 15},{y - 9} {x + 24},{y} {x + 15},{y + 9}"/></g>'
        )

    marks = f"{glyph(266, 144)}{glyph(134, 256)}" if glyphs else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400">'
        '<title>fcp-mcp-server</title>'
        f'<rect x="36" y="36" width="328" height="328" rx="76" fill="{t("raised-2")}"/>'
        f'{lanes}'
        f'<rect x="84" y="124" width="92" height="40" rx="10" fill="{t("accent")}"/>'
        f'<rect x="150" y="180" width="100" height="40" rx="10" fill="{t("accent")}"/>'
        f'<rect x="224" y="236" width="92" height="40" rx="10" fill="{t("accent")}"/>'
        f'{marks}'
        f'<line x1="200" y1="104" x2="200" y2="296" stroke="{t("ink")}" stroke-width="6" stroke-linecap="round"/>'
        '</svg>'
    )


def main() -> None:
    from playwright.sync_api import sync_playwright

    ASSETS.mkdir(parents=True, exist_ok=True)
    outputs = {"logo": svg(), "logo-small": svg(glyphs=False)}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 400, "height": 400})
        for name, drawing in outputs.items():
            (ASSETS / f"{name}.svg").write_text(drawing + "\n", encoding="utf-8")
            page.set_content(f'<html><body style="margin:0;background:transparent">{drawing}</body></html>')
            page.screenshot(path=str(ASSETS / f"{name}-400.png"), omit_background=True)
        browser.close()
    print("docs/assets/logo{,-small}.svg, docs/assets/logo{,-small}-400.png")


if __name__ == "__main__":
    main()
