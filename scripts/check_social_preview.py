"""Does github.com serve the committed social card?

    uv run --with pillow scripts/check_social_preview.py

Reads the repo page's ``og:image`` (what every link unfurl shows), downloads
it, and compares it with ``docs/assets/social-card.png`` at 320x160 in grey.
Exit 0 when they match, 1 when they differ (the card was regenerated and not
uploaded, or someone uploaded something else), 2 when the page could not be
read. GitHub has no API to SET the image, so uploading stays one step in
Settings -> Social preview; this is the check that the step happened.
"""

from __future__ import annotations

import io
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CARD = REPO / "docs" / "assets" / "social-card.png"
PAGE = "https://github.com/DareDev256/fcp-mcp-server"
THRESHOLD = 6.0  # mean absolute grey difference out of 255; re-encoding alone stays under 2


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fcp-mcp-server social-preview check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def og_image(html: str) -> str | None:
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    return m.group(1) if m else None


def difference(a: bytes, b: bytes) -> float:
    from PIL import Image, ImageChops, ImageStat

    def norm(data: bytes):
        return Image.open(io.BytesIO(data)).convert("L").resize((320, 160), Image.Resampling.LANCZOS)

    return ImageStat.Stat(ImageChops.difference(norm(a), norm(b))).mean[0]


def main() -> int:
    try:
        url = og_image(fetch(PAGE).decode("utf-8", "replace"))
        if not url:
            print(f"no og:image on {PAGE}", file=sys.stderr)
            return 2
        served = fetch(url)
    except OSError as exc:
        print(f"could not read {PAGE}: {exc}", file=sys.stderr)
        return 2
    diff = difference(served, CARD.read_bytes())
    verdict = "MATCHES" if diff <= THRESHOLD else "DIFFERS FROM"
    print(f"github.com serves {url}\n{verdict} docs/assets/social-card.png (mean grey difference {diff:.1f}/255)")
    return 0 if diff <= THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
