"""The long-interview fixture (0.34): two synthetic cameras, 18 minutes, 44 cuts.

    python demo/fixtures/interview.py [out_dir]     # default demo/fixtures/.work-interview

The third of the three fixtures every visual phase is judged on (the 32 s gym
cut and the 164 s music video are the other two). Nothing in it is a person:
the two "cameras" are ffmpeg's ``testsrc`` (Cam A) and ``testsrc2`` (Cam B)
at 320x180, each with a ``sine`` tone gated on and off like speech, and the
music bed and the whooshes are sine tones too. The cut is built with the
server's own tools where it has one and written as Final Cut writes it where
it does not:

- the spine: 44 cuts alternating Cam A and Cam B at the same source time (a
  two-camera interview cut on the speaker), from an EDL
  (``generate · import_edl_json``), every clip's sound on the dialogue role;
- eight lower thirds: ``<title>`` elements on lane 1 (Basic Title, the
  titles role), anchored on the spine clip under them at that clip's clock;
- a music bed under the open and the close (lane -1, music role) and four
  whooshes into the chapters (lane -2, effects role);
- markers of every kind with ``mark · batch_add_markers``: six chapters, four
  standard markers, two to-dos and two completed to-dos.

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
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

FPS = 24
SOURCE_SECONDS = 1100           # each camera file; the cut uses 0..1084 s of it
SIZE = "320x180"
# Lengths of the 44 cuts in seconds, four times round eleven: 1084 s (18:04).
LENGTHS = [18, 34, 12, 27, 41, 9, 22, 30, 15, 38, 25] * 4
LOWER_THIRDS = [  # (record second, name): fictional people, synthetic pictures
    (20, "Sam Rivera, host"), (64, "Noor Haddad, guest"), (190, "Sam Rivera, host"),
    (330, "Archive: test card"), (505, "Noor Haddad, guest"), (700, "Location: Studio 2"),
    (880, "Noor Haddad, guest"), (1030, "Credits: fcp-mcp-server"),
]
CHAPTERS = [(0, "Open"), (180, "Early work"), (420, "The studio"), (640, "Mistakes"),
            (860, "What changed"), (1040, "Close")]
STANDARD = [(95, "Good laugh"), (300, "Pull quote"), (560, "B-roll here"), (930, "Callback")]
TODO = [(250, "Fix the audio dip"), (770, "Check the name spelling")]
COMPLETED = [(140, "Colour matched"), (600, "Levels done")]
WHOOSHES = [180, 420, 640, 860]
BASIC_TITLE = (".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/"
               "Basic Title.moti")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=900)


def _speechlike(freq: int, phase: float) -> tuple:
    """A sine shaped like speech: phrases a few seconds long with a breath
    between them, and inside a phrase a level that rises and falls about
    three times a second, so its waveform reads as talk, not as a tone."""
    gate = f"gte(sin(2*PI*t/7.3+{phase})+0.35*sin(2*PI*t/2.1),-0.2)"
    syllables = f"(0.55+0.3*sin(2*PI*t*2.9+{phase})+0.15*sin(2*PI*t*7.1))"
    phrase = f"(0.6+0.4*sin(2*PI*t/3.7+{phase}))"
    return (f"sine=frequency={freq}:sample_rate=48000:duration={SOURCE_SECONDS}",
            f"volume='0.5*{gate}*{syllables}*{phrase}':eval=frame")


def _media(out: Path) -> dict:
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, pattern, freq, phase in (("Cam A", "testsrc", 196, 0.0), ("Cam B", "testsrc2", 247, 1.7)):
        target = media / f"{name.replace(' ', '_')}.mov"
        if not target.is_file():
            tone, gate = _speechlike(freq, phase)
            _ffmpeg("-f", "lavfi", "-i", f"{pattern}=size={SIZE}:rate={FPS}:duration={SOURCE_SECONDS}",
                    "-f", "lavfi", "-i", tone, "-af", gate, "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "48k", "-ac", "1", str(target))
        paths[name] = target
    bed = media / "Theme.m4a"
    if not bed.is_file():
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=110:sample_rate=48000:duration=120",
                "-af", "volume='0.25+0.2*sin(2*PI*t*2)':eval=frame", "-c:a", "aac", "-b:a", "48k", "-ac", "1", str(bed))
    paths["Theme"] = bed
    whoosh = media / "Whoosh.m4a"
    if not whoosh.is_file():
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
                "-af", "volume='0.6*(1-t/2)':eval=frame", "-c:a", "aac", "-b:a", "48k", "-ac", "1", str(whoosh))
    paths["Whoosh"] = whoosh
    return paths


def _seconds(value: str) -> Fraction:
    """An FCPXML time attribute ("3600s", "241/24s") as exact seconds."""
    v = value.rstrip("s")
    if "/" in v:
        n, d = v.split("/")
        return Fraction(int(n), int(d))
    return Fraction(v)


def _t(seconds: Fraction) -> str:
    """Exact seconds as FCPXML writes them on a 24 fps grid."""
    frames = seconds * FPS
    if frames.denominator == 1:
        return f"{frames.numerator}/{FPS}s" if frames.numerator % FPS else f"{frames.numerator // FPS}s"
    return f"{seconds.numerator}/{seconds.denominator}s"


def _decorate(path: Path, media: dict) -> None:
    """Roles, lower thirds, the music bed and the whooshes, written into the
    imported cut the way Final Cut writes them (anchored items on their
    parent's clock: offset = parent start + (record time - parent offset))."""
    tree = ET.parse(path)
    root = tree.getroot()
    resources = root.find("resources")
    # the cameras' files are 1100 s long: declare them so (the EDL import
    # declares only the ranges it was given)
    for asset in resources.findall("asset"):
        if asset.get("name") in ("Cam A", "Cam B"):
            asset.set("duration", f"{SOURCE_SECONDS}s")
    fmt = resources.find("format").get("id")

    def asset(rid: str, name: str, file: Path, seconds: int) -> None:
        a = ET.SubElement(resources, "asset", id=rid, name=name, start="0s", duration=f"{seconds}s",
                          hasAudio="1", audioSources="1", audioChannels="1", audioRate="48000")
        ET.SubElement(a, "media-rep", kind="original-media", src=file.resolve().as_uri())

    asset("r_theme", "Theme", media["Theme"], 120)
    asset("r_whoosh", "Whoosh", media["Whoosh"], 2)
    ET.SubElement(resources, "effect", id="r_title", name="Basic Title", uid=BASIC_TITLE)
    spine = root.find(".//sequence/spine")
    clips = [c for c in spine if c.tag == "asset-clip"]
    for c in clips:
        c.set("audioRole", "dialogue")

    def host(at: Fraction):
        for c in clips:
            off, dur = _seconds(c.get("offset")), _seconds(c.get("duration"))
            if off <= at < off + dur:
                return c, off, _seconds(c.get("start", "0s"))
        raise ValueError(f"nothing on the spine at {at}s")

    def anchor(at: Fraction, element: ET.Element) -> None:
        parent, off, start = host(at)
        element.set("offset", _t(start + (at - off)))
        # anchored items follow the parent's own children in DTD order: after
        # markers and the like, so append at the end
        parent.append(element)

    for k, (at, name) in enumerate(LOWER_THIRDS, 1):
        t = ET.Element("title", ref="r_title", lane="1", name=name, start="3600s", duration="5s", role="titles")
        text = ET.SubElement(t, "text")
        ET.SubElement(text, "text-style", ref=f"ts{k}").text = name
        style = ET.SubElement(t, "text-style-def", id=f"ts{k}")
        ET.SubElement(style, "text-style", font="Helvetica", fontSize="60", fontColor="1 1 1 1")
        anchor(Fraction(at), t)
    for at, length in ((0, 90), (1024, 60)):
        anchor(Fraction(at), ET.Element("asset-clip", ref="r_theme", lane="-1", name="Theme", start="0s",
                                        duration=f"{length}s", format=fmt, audioRole="music"))
    for at in WHOOSHES:
        anchor(Fraction(at) - 1, ET.Element("asset-clip", ref="r_whoosh", lane="-2", name="Whoosh", start="0s",
                                            duration="2s", format=fmt, audioRole="effects"))
    ET.indent(tree, space="    ")
    body = ET.tostring(root, encoding="unicode")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n", encoding="utf-8")


def build(out: Path) -> dict:
    """Write the fixture into ``out``; return {"interview", "media"}."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for the interview fixture")
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

    ranges, at = [], 0
    for i, length in enumerate(LENGTHS):
        ranges.append({"source": "Cam A" if i % 2 == 0 else "Cam B", "start": at, "end": at + length})
        at += length
    edl = {"sources": {n: str(media[n]) for n in ("Cam A", "Cam B")}, "ranges": ranges}
    (out / "interview-edl.json").write_text(json.dumps(edl), encoding="utf-8")
    cut = out / "interview-cut.fcpxml"
    reply = call("generate", "import_edl_json", {"filepath": str(out / "interview-edl.json"), "output_path": str(cut),
                                                 "name": "Interview cut"})
    if not cut.is_file():
        raise RuntimeError(reply)
    _decorate(cut, media)
    marked = out / "interview.fcpxml"

    def tc(seconds: int) -> str:
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}:00"

    markers = ([{"timecode": tc(s), "name": n, "marker_type": "chapter"} for s, n in CHAPTERS]
               + [{"timecode": tc(s), "name": n, "marker_type": "standard"} for s, n in STANDARD]
               + [{"timecode": tc(s), "name": n, "marker_type": "todo"} for s, n in TODO]
               + [{"timecode": tc(s), "name": n, "marker_type": "completed"} for s, n in COMPLETED])
    reply = call("mark", "batch_add_markers", {"filepath": str(cut), "output_path": str(marked), "markers": markers})
    if not marked.is_file():
        raise RuntimeError(reply)
    return {"interview": marked, "media": media}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / ".work-interview"
    built = build(target)
    print(json.dumps({k: str(v) for k, v in built.items() if k != "media"}, indent=1))
    text = built["interview"].read_text(encoding="utf-8")
    print("cuts:", len(re.findall(r'<asset-clip ref="r\d+" offset', text)))
