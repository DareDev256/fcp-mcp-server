"""Cut the hero's source footage into small silent proxies (run once, by hand).

    uv run demo/hero/footage.py <folder of the raw gym session>

The README hero is cut from the author's own gym session (2026-08-08, the
SLVLING form pass: he is the only person in frame). The raw clips are
gigabytes of 4K HEVC with gym music on them, so the repository keeps only
what the demo needs: four short 640x360 H.264 proxies with NO audio track
at all (``-an``) and no metadata, in ``demo/hero/footage/``. ``build.py``
gives each one a synthesised test-tone soundtrack (with real silence where
the rests between sets are) and the on-screen note says so.

Footage (c) the author; it is in this repository for this demo only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "footage"

# name -> (raw file, start seconds, seconds, centre of the band as a fraction
# of the frame's height). The raw clips are portrait; each proxy is a
# full-width 16:9 band placed so the bar sits a little above its middle at
# the clip's midpoint (where the app takes its thumbnail). The timeline's
# wide tiles (object-fit: cover) then show the lift above the label, not the
# wall or the floor, and 640x360 matches the 16:9 sequence it is cut into.
CUTS = {
    "bench": ("2026-08-08_video-5348_singular_display.mov", 12, 6, 0.61),
    "squat": ("2026-08-08_IMG_0128.MOV", 172, 19, 0.18),
    "press": ("2026-08-08_video-5107_singular_display.mov", 26, 6, 0.54),
    "finish": ("2026-08-08_video-5348_singular_display.mov", 80, 6, 0.63),
}


def main(raw: Path) -> None:
    OUT.mkdir(exist_ok=True)
    for name, (src, start, seconds, centre) in CUTS.items():
        band = f"crop=iw:trunc(iw*9/32)*2:0:'min(max(ih*{centre}-oh/2,0),ih-oh)'"
        vf = band + ",scale=640:360,setsar=1,fps=24"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(seconds), "-i", str(raw / src),
             "-an", "-sn", "-dn", "-map_metadata", "-1", "-vf", vf, "-c:v", "libx264", "-preset", "slow",
             "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(OUT / f"{name}.mp4")],
            check=True, timeout=600,
        )
        print(f"{name}.mp4  {(OUT / f'{name}.mp4').stat().st_size / 1e3:.0f} kB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).expanduser())
