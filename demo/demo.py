"""The README demo, as a script that actually runs.

Every line the GIF shows is real output from `server.call_tool` — the same
entry point an MCP client hits. Nothing here is typed by hand or replayed
from a recording, so the demo cannot drift away from the code the way a
screenshot does. Media is synthesised with ffmpeg at run time: no fixture
to keep, and nothing of anyone's footage in a public asset.

    python demo/demo.py            # run it
    vhs demo/demo.tape             # record docs/assets/demo.gif
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402

DIM, BOLD, CYAN, GREEN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[0m"


def say(prompt: str) -> None:
    print(f"\n{CYAN}❯{RESET} {BOLD}{prompt}{RESET}")
    time.sleep(0.4)


def run(group: str, **args) -> str:
    action = args.get("action", "")
    print(f"{DIM}  ⎿ {group}({action}){RESET}")
    out = asyncio.run(server.call_tool(group, args))
    text = "\n".join(c.text for c in out)
    for line in text.splitlines()[:14]:
        print(f"    {line}")
    return text


def synthesise(root: Path) -> list[Path]:
    """Three shots with a sine bed and a silent stretch in the middle one."""
    shots = []
    for i, (colour, audio) in enumerate(
        [("cyan", "sine=frequency=330"), ("orange", "anullsrc=r=44100:cl=stereo"), ("navy", "sine=frequency=440")]
    ):
        out = root / f"shot_{i + 1}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=4",
             "-f", "lavfi", "-i", f"{audio}:duration=4",
             "-vf", f"drawbox=color={colour}@0.35:t=fill",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
            check=True,
        )
        shots.append(out)
    return shots


DEMO_DIR = Path(os.environ.get("FCP_DEMO_DIR", "/tmp/fcp-demo"))


def main() -> int:
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    DEMO_DIR.mkdir(parents=True)
    os.chdir(DEMO_DIR)

    print(f"{DIM}synthesising three 4s shots with ffmpeg…{RESET}")
    shots = synthesise(DEMO_DIR)

    Path("edl.json").write_text(json.dumps({
        "sources": {p.stem: p.name for p in shots},
        "ranges": [{"source": p.stem, "start": 0.5, "end": 3.5} for p in shots],
    }))

    say("cut these three shots into a timeline")
    run("generate", action="import_edl_json", filepath="edl.json",
        output_path="demo.fcpxml")

    say("what's in it?")
    run("inspect", action="analyze_timeline", filepath="demo.fcpxml")

    say("show me the timeline")
    run("preview", action="preview_timeline", filepath="demo.fcpxml")

    say("any dead air in the source media?")
    run("diagnose", action="detect_media_silence", filepath="demo.fcpxml")

    say("cut the dead air out")
    run("edit", action="remove_media_silence", filepath="demo.fcpxml",
        output_path="demo_cut.fcpxml")

    say("show me that again")
    run("preview", action="preview_timeline", filepath="demo_cut.fcpxml")

    say("render a proxy so I can actually watch it")
    run("preview", action="preview_render", filepath="demo_cut.fcpxml",
        output_path="proxy.mp4")

    print(f"\n{GREEN}Every line above is real output from the MCP handlers.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
