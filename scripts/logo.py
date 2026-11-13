"""Draw the fcp-mcp-server logo from the palette.

    uv run --with playwright scripts/logo.py        # from the repo root

Writes ``docs/assets/logo.svg`` and ``docs/assets/logo-400.png`` (transparent
outside the tile). The layout is the 0.14 logo's, three lanes, three clips,
a playhead through the middle clip and two ``</>`` glyphs, redrawn in code so
its colours are tokens (``fcpxml/palette.py``) instead of a purple bitmap that
could only be recoloured by hand. The glyphs are strokes, not text, so the SVG
looks the same wherever it is rendered.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from fcpxml import palette  # noqa: E402

ASSETS = REPO / "docs" / "assets"


def svg() -> str:
    t = palette.dark
    lanes = "".join(
        f'<rect x="88" y="{y}" width="224" height="36" rx="9" fill="{t("raised")}"/>' for y in (128, 182, 236)
    )

    def glyph(x: float, y: float) -> str:
        # "</>" in strokes, 46 wide and 20 tall, centred on (x, y)
        return (
            f'<g fill="none" stroke="{t("ink")}" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">'
            f'<polyline points="{x - 15},{y - 8} {x - 23},{y} {x - 15},{y + 8}"/>'
            f'<line x1="{x + 4}" y1="{y - 10}" x2="{x - 4}" y2="{y + 10}"/>'
            f'<polyline points="{x + 15},{y - 8} {x + 23},{y} {x + 15},{y + 8}"/></g>'
        )

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400">'
        '<title>fcp-mcp-server</title>'
        f'<defs><filter id="glow" x="-40%" y="-80%" width="180%" height="260%">'
        f'<feDropShadow dx="0" dy="0" stdDeviation="7" flood-color="{t("accent")}" flood-opacity="0.55"/>'
        '</filter></defs>'
        f'<rect x="40" y="40" width="320" height="320" rx="72" fill="{t("ground")}" '
        f'stroke="{t("line-strong")}" stroke-width="2"/>'
        f'{lanes}'
        f'<rect x="88" y="128" width="84" height="36" rx="9" fill="{t("accent")}"/>'
        f'<rect x="156" y="182" width="88" height="36" rx="9" fill="{t("accent")}" filter="url(#glow)"/>'
        f'<rect x="228" y="236" width="84" height="36" rx="9" fill="{t("accent")}"/>'
        f'{glyph(262, 146)}{glyph(138, 254)}'
        f'<line x1="200" y1="110" x2="200" y2="290" stroke="{t("ink")}" stroke-width="3" stroke-linecap="round"/>'
        '</svg>'
    )


def main() -> None:
    from playwright.sync_api import sync_playwright

    ASSETS.mkdir(parents=True, exist_ok=True)
    drawing = svg()
    (ASSETS / "logo.svg").write_text(drawing + "\n", encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 400, "height": 400})
        page.set_content(f'<html><body style="margin:0;background:transparent">{drawing}</body></html>')
        page.screenshot(path=str(ASSETS / "logo-400.png"), omit_background=True)
        browser.close()
    print("docs/assets/logo.svg, docs/assets/logo-400.png")


if __name__ == "__main__":
    main()
