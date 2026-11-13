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

SQUAT = "2026-08-08_IMG_0128.MOV"
BENCH = "2026-08-08_video-5348_singular_display.mov"
PRESS = "2026-08-08_video-5107_singular_display.mov"

# name -> segments (raw file, start s, seconds, band centre, hold s), joined
# in order. The raw clips are portrait; each segment is a full-width 16:9
# band whose centre is a fraction of the frame's height. The app takes each
# clip's thumbnail at its MIDPOINT, so the segments are placed for that
# frame to read as its own lift, and to differ from its neighbours:
#   bench   lying on the bench looking up: the bar across the frame and the
#           plates of the ceiling fan behind it (14.0 s; critic round 6: the
#           pad close-up read as a dark green blur)
#   squat   three 5 s sets whose midpoints are standing (171.0 s), half way
#           down (178.0 s) and at the bottom (187.0 s), 1.5 s rests between
#   press   the arm and watch driving the bar (58.0 s)
#   finish  the floor and the plates, after he sits up, last frame held
# build.py's test tone is silent exactly over the squat's rests.
CUTS = {
    "bench": [(BENCH, 11.25, 5.5, 0.61, 0)],
    "squat": [(SQUAT, 168.5, 5, 0.18, 0), (SQUAT, 173.5, 1.5, 0.18, 0), (SQUAT, 175.5, 5, 0.18, 0),
              (SQUAT, 180.5, 1.5, 0.18, 0), (SQUAT, 184.5, 5, 0.18, 0)],
    "press": [(PRESS, 55.5, 5, 0.54, 0)],
    "finish": [(PRESS, 67.15, 2.75, 0.75, 2.75)],
}


def main(raw: Path) -> None:
    OUT.mkdir(exist_ok=True)
    for name, segments in CUTS.items():
        inputs, chains = [], []
        for i, (src, start, seconds, centre, hold) in enumerate(segments):
            inputs += ["-ss", str(start), "-t", str(seconds), "-i", str(raw / src)]
            band = f"crop=iw:trunc(iw*9/32)*2:0:'min(max(ih*{centre}-oh/2,0),ih-oh)'"
            pad = f",tpad=stop_mode=clone:stop_duration={hold}" if hold else ""
            chains.append(f"[{i}:v]{band},scale=640:360,setsar=1,fps=24{pad}[v{i}]")
        joined = "".join(f"[v{i}]" for i in range(len(segments)))
        graph = ";".join(chains) + f";{joined}concat=n={len(segments)}:v=1:a=0[out]"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[out]",
             "-an", "-sn", "-dn", "-map_metadata", "-1", "-c:v", "libx264", "-preset", "slow",
             "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(OUT / f"{name}.mp4")],
            check=True, timeout=600,
        )
        print(f"{name}.mp4  {(OUT / f'{name}.mp4').stat().st_size / 1e3:.0f} kB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).expanduser())
