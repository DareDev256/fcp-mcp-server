"""Cut the hero's source footage into small silent proxies (run once, by hand).

    uv run demo/hero/footage.py <folder of the raw gym session>

The README hero is cut from the author's own gym session (2026-08-08, the
SLVLING form pass: he is the only person in frame). The raw clips are
gigabytes of 4K HEVC with gym music on them, so the repository keeps only
what the demo needs: four short 360x640 H.264 proxies with NO audio track
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

# name -> (raw file, start seconds, seconds, crop to 9:16 first)
CUTS = {
    "bench": ("2026-08-08_video-5348_singular_display.mov", 12, 5, True),
    "squat": ("2026-08-08_IMG_0128.MOV", 172, 22, False),
    "press": ("2026-08-08_video-5107_singular_display.mov", 26, 5, True),
    "finish": ("2026-08-08_video-5348_singular_display.mov", 80, 5, True),
}


def main(raw: Path) -> None:
    OUT.mkdir(exist_ok=True)
    for name, (src, start, seconds, crop) in CUTS.items():
        vf = ("crop=ih*9/16:ih," if crop else "") + "scale=360:640,setsar=1,fps=24"
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
