"""The real-footage demo fixture: the author's own gym session (0.32).

    python demo/fixtures/gym_session.py [out_dir]     # default demo/fixtures/.work

Test bars read as engineering artefacts, so the proof images are shown on
real footage: the four silent 640x360 proxies the 0.31 README hero used
(``demo/hero/footage/``, cut by ``demo/hero/footage.py`` from the 2026-08-08
session; he is the only person in frame and the gym's music was never kept).
This script gives each a synthesised test tone (silent over the rests between
the squat sets, so silence detection has something real to find) and builds
two timelines through the server's own tools:

- ``imported.fcpxml``: Bench, Squat (three sets, two rests), Press, Finish,
  from an EDL (``generate · import_edl_json``), each clip trimmed to leave a
  handle so a transition can be laid (the assets are then declared at their
  files' real lengths: the import declares only the ranges it was given);
- ``edited.fcpxml``: the rests cut (``edit · remove_media_silence``), a cross
  dissolve from Press into Finish (``edit · add_transition``), one marker per
  lift (``mark · batch_add_markers``) and the Bench footage connected on lane 1
  over the long set (``edit · add_connected_clip``);
- ``takes.fcpxml``: one 10 s take with three hard cuts inside it (2.5 s of
  each lift back to back), for ``scenes · detect_scenes``.

Nothing here is in the wheel: ``demo/`` is not a package. Requires ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FOOTAGE = REPO / "demo" / "hero" / "footage"

# name -> (proxy, (in, out) seconds used from it, tone expression or None for a steady quiet tone)
CLIPS = {
    "Bench": ("bench.mp4", (0.25, 5.25), None),
    "Squat": ("squat.mp4", (0.0, 18.0), "0.25*sin(2*PI*220*t)*(between(t,0,5)+between(t,6.5,11.5)+between(t,13,18))"),
    "Press": ("press.mp4", (0.0, 4.75), None),
    "Finish": ("finish.mp4", (0.25, 5.5), None),
}
LENGTHS = {"Bench": 5.5, "Squat": 18.0, "Press": 5.0, "Finish": 5.5}


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=300)


def _media(out: Path) -> dict:
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, (proxy, _span, tone) in CLIPS.items():
        seconds = LENGTHS[name]
        audio = (f"aevalsrc='{tone}':s=48000:d={seconds}" if tone
                 else f"sine=frequency=330:sample_rate=48000:duration={seconds}")
        target = media / f"{name}.mov"
        if not target.is_file():
            cmd = ["-i", str(FOOTAGE / proxy), "-f", "lavfi", "-i", audio, "-map", "0:v", "-map", "1:a",
                   "-t", str(seconds), "-c:v", "copy", "-c:a", "aac"]
            if not tone:
                cmd += ["-af", "volume=0.2"]
            _ffmpeg(*cmd, str(target))
        paths[name] = target
    return paths


def _full_assets(path: Path, media: dict) -> None:
    """import_edl_json declares each asset only as long as the ranges it was
    given, which leaves no media past a clip's out-point; Final Cut (and the
    transition writer, which checks like it) then refuses a dissolve. Declare
    every asset at the real length of the file this script wrote for it
    (LENGTHS, the -t each file was cut to)."""
    text = path.read_text(encoding="utf-8")
    for name, file in media.items():
        frames = round(LENGTHS[name] * 24)
        text = re.sub(rf'(<asset id="r\d+" name="{name}" [^>]*duration=")[^"]+(")', rf"\g<1>{frames}/24s\2", text, count=1)
    path.write_text(text, encoding="utf-8")


def build(out: Path) -> dict:
    """Write the fixture into ``out``; return {"imported", "edited", "media"}."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for the gym-session fixture")
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("FCP_MCP_JOURNAL", "off")
    os.environ.setdefault("FCP_MCP_INDEX", "off")
    sys.path.insert(0, str(REPO))
    import server  # noqa: E402

    media = _media(out)

    def call(tool: str, action: str, args: dict) -> str:
        result = asyncio.run(server.call_tool(tool, {"action": action, "args": args}))
        return "\n".join(c.text for c in result if getattr(c, "type", "") == "text")

    def saved(text: str) -> Path:
        m = re.search(r"Saved to:\s*`?([^`\n]+)`?", text)
        if not m:
            raise RuntimeError(text)
        return Path(m.group(1).strip())

    imported = out / "imported.fcpxml"
    edl = {"sources": {n: str(p) for n, p in media.items()},
           "ranges": [{"source": n, "start": CLIPS[n][1][0], "end": CLIPS[n][1][1]} for n in CLIPS]}
    (out / "edl.json").write_text(json.dumps(edl), encoding="utf-8")
    reply = call("generate", "import_edl_json", {"filepath": str(out / "edl.json"), "output_path": str(imported),
                                                 "name": "Session cut"})
    if not imported.is_file():
        raise RuntimeError(reply)
    _full_assets(imported, media)
    step = saved(call("edit", "remove_media_silence", {"filepath": str(imported), "clip_name": "Squat",
                                                        "min_silence": 1.0, "output_path": str(out / "step1.fcpxml")}))
    step = saved(call("edit", "add_transition", {"filepath": str(step), "clip_id": "Press", "position": "end",
                                                 "transition_type": "cross-dissolve", "duration": "00:00:00:12",
                                                 "output_path": str(out / "step2.fcpxml")}))
    step = saved(call("mark", "batch_add_markers", {"filepath": str(step), "output_path": str(out / "step3.fcpxml"),
                                                    "markers": [{"timecode": "00:00:00:00", "name": "Bench", "marker_type": "chapter"},
                                                                {"timecode": "00:00:05:00", "name": "Squat", "marker_type": "chapter"},
                                                                {"timecode": "00:00:20:04", "name": "Press", "marker_type": "chapter"},
                                                                {"timecode": "00:00:24:22", "name": "Finish", "marker_type": "chapter"}]}))
    reply = call("edit", "add_connected_clip", {"filepath": str(step), "parent_clip_id": "Squat", "asset_name": "Bench",
                                                "offset": "2s", "duration": "3s", "lane": 1,
                                                "output_path": str(out / "edited.fcpxml")})
    edited = out / "edited.fcpxml"
    if not edited.is_file():
        raise RuntimeError(reply)
    # One take with three hard cuts in it (2.5 s of each lift, back to back),
    # imported whole: what detect_scenes is for.
    montage = media["Bench"].with_name("Montage.mov")
    if not montage.is_file():
        inputs, labels = [], []
        for i, name in enumerate(CLIPS):
            inputs += ["-ss", "1", "-t", "2.5", "-i", str(media[name])]
            labels.append(f"[{i}:v][{i}:a]")
        _ffmpeg(*inputs, "-filter_complex", "".join(labels) + f"concat=n={len(CLIPS)}:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(montage))
    takes = out / "takes.fcpxml"
    (out / "takes.json").write_text(json.dumps({"sources": {"Montage": str(montage)},
                                                "ranges": [{"source": "Montage", "start": 0, "end": 10}]}),
                                    encoding="utf-8")
    call("generate", "import_edl_json", {"filepath": str(out / "takes.json"), "output_path": str(takes),
                                         "name": "One take"})
    return {"imported": imported, "edited": edited, "takes": takes, "media": media}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / ".work"
    built = build(target)
    print(json.dumps({k: str(v) for k, v in built.items() if k != "media"}, indent=1))
