"""Record the README hero: "watch Claude cut".

One command re-records it:

    uv run --with pillow demo/hero/build.py                  # from the repo root
    uv run --with pillow demo/hero/build.py --record-only    # re-capture the last run's states (host page tweaks)

What it does, in order:

1. Synthesises four source clips with ffmpeg (slow colour gradients, 24 fps;
   the interview take carries two stretches of real silence in its audio).
   Nothing is kept as a fixture; the media is rebuilt every run.
2. Runs the REAL tool handlers through ``server.call_tool`` — the same entry
   an MCP client reaches — and keeps every ``view_timeline`` result verbatim:

       generate · import_edl_json      the cut, from an EDL of the four clips
       view     · view_timeline        state 0
       edit     · remove_media_silence state 1   (real ffmpeg silencedetect)
       mark     · batch_add_markers    state 2   (one marker per section)
       gen      · gen_quote            the money gate, priced from the table
       gen      · gen_broll            state 3   (provider MOCKED, see below)
       organize · history              the journal rows the run produced

   The B-roll step patches ``fcpxml.gen.provider_for`` with a fake provider
   exactly as ``tests/test_gen_group.py`` does, so the gate, the key check,
   the probe, the insert, the DTD check and the journal all run for real while
   no request leaves the machine and no money is spent. The recording labels
   it "provider mocked" on screen, and the clip the fake returns is itself a
   synthesised gradient. Nothing in the video claims a real generation.
3. Writes ``.work/states.json`` (the prompt, the tool lines with figures
   lifted from the real tool replies, and the four payload-bearing results)
   plus the ``ui://fcp/timeline`` resource as served, then runs
   ``record.js`` (Playwright) to capture frames from the stub host page, and
   ffmpeg to encode ``docs/assets/hero-watch-claude-cut.{mp4,gif}``.

Requires ffmpeg/ffprobe on PATH, Pillow (for the GIF: it merges identical
frames, which ffmpeg's gif muxer does not), and Node with the ``playwright``
package resolvable (``npm i playwright`` anywhere and point ``HERO_PLAYWRIGHT``
at that ``node_modules/playwright`` if it is not on Node's module path).
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
WORK = HERE / ".work"
ASSETS = REPO / "docs" / "assets"
FPS = 24
GIF_FPS = 12
GIF_WIDTH = 960
PROMPT = "Cut the dead air, mark every section, and put B-roll over the long interview take."
BROLL_PROMPT = "city skyline at dusk, slow push in"

# name -> (seconds, gradient colours, audio expression or None)
# The interview's audio is a 220 Hz tone with two stretches of digital silence
# (6.0-9.5 s and 15.0-18.0 s): that is the dead air remove_media_silence cuts.
CLIPS = {
    "Opening": (4, ("0x1a0b3d", "0x7b2cff", "0x0b0620"), None),
    "Interview_Take": (22, ("0x0b3d91", "0x00e5ff", "0x06111f"),
                       "0.25*sin(2*PI*220*t)*(between(t,0,6)+between(t,9.5,15)+between(t,18,22))"),
    "Studio": (5, ("0x0b3d2a", "0x19ffb0", "0x061f14"), None),
    "Closing": (4, ("0x3d0b1a", "0xff2c6b", "0x200610"), None),
}
MOCK_BROLL = ("0x0b2f3d", "0x9be7ff", "0x061a20")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=300)


def synth(path: Path, seconds: int, colours, audio: str | None) -> None:
    c0, c1, c2 = colours
    video = (f"gradients=s=1280x720:c0={c0}:c1={c1}:c2={c2}:nb_colors=3:"
             f"speed=0.012:type=linear:r={FPS}:d={seconds}")
    cmd = ["-f", "lavfi", "-i", video]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"aevalsrc='{audio}':s=48000:d={seconds}"]
    else:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={seconds}", "-af", "volume=0.2"]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-shortest", str(path)]
    ffmpeg(*cmd)


def text_of(result) -> str:
    return "\n".join(c.text for c in result)


def main() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg and ffprobe are required")
    if "--record-only" in sys.argv:
        if not (WORK / "states.json").is_file():
            sys.exit("no previous run under demo/hero/.work; run without --record-only first")
        shutil.rmtree(WORK / "frames", ignore_errors=True)
        record(WORK, os.environ.get("HOME", ""))
        encode(WORK)
        return
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "home").mkdir(parents=True)
    (WORK / "project").mkdir()
    os.environ["FCP_MCP_JOURNAL"] = str(WORK / "journal")
    os.environ["FCP_MCP_INDEX"] = "off"
    real_home = os.environ.get("HOME", "")
    os.environ["HOME"] = str(WORK / "home")          # the private thumbnail cache lands here
    os.environ["FAL_KEY"] = "hero-recording-no-key"  # the gate reads a key; the provider is mocked
    os.environ.pop("FCP_PROJECTS_DIRS", None)
    os.environ.pop("FCP_PROJECTS_DIR", None)
    sys.path.insert(0, str(REPO))

    import server  # noqa: E402
    from fcpxml import gen, timeline_app  # noqa: E402

    media = WORK / "media"
    media.mkdir()
    for name, (seconds, colours, audio) in CLIPS.items():
        synth(media / f"{name}.mov", seconds, colours, audio)
    mock_clip = media / "mock-provider-broll.mp4"
    synth(mock_clip, 5, MOCK_BROLL, None)

    def call(tool: str, action: str, args: dict) -> str:
        return text_of(asyncio.run(server.call_tool(tool, {"action": action, "args": args})))

    project = WORK / "project"
    edl = {
        "sources": {n: str(media / f"{n}.mov") for n in CLIPS},
        "ranges": [{"source": n, "start": 0, "end": CLIPS[n][0]} for n in CLIPS],
    }
    (project / "edl.json").write_text(json.dumps(edl), encoding="utf-8")
    cut = project / "cut.fcpxml"
    r_import = call("generate", "import_edl_json",
                    {"filepath": str(project / "edl.json"), "output_path": str(cut), "name": "Interview cut"})
    assert cut.is_file(), r_import

    def view(path: Path) -> tuple[str, dict]:
        text = call("view", "view_timeline", {"filepath": str(path)})
        payload = timeline_app.extract_payload(text)
        assert payload, text
        return text, payload

    def saved(text: str) -> Path:
        m = re.search(r"Saved to:\s*`?([^`\n]+)`?", text)
        assert m, text
        return Path(m.group(1).strip())

    states = []
    v0, p0 = view(cut)
    states.append({"call": "view · view_timeline", "result": v0})

    r_silence = call("edit", "remove_media_silence",
                     {"filepath": str(cut), "clip_name": "Interview_Take", "min_silence": 1.0,
                      "output_path": str(project / "cut_silence_removed.fcpxml")})
    s1 = saved(r_silence)
    v1, p1 = view(s1)
    states.append({"call": "edit · remove_media_silence", "result": v1})
    removed = re.search(r"\*\*Total Removed\*\*: ([^\n]+)", r_silence).group(1).strip()
    spans = sum(int(n) for n in re.findall(r"\| Interview_Take \| (\d+) \|", r_silence))

    # One marker per section: the first clip of each source name, in timeline order.
    def tc(fraction: str) -> str:
        from fractions import Fraction
        total = round(Fraction(fraction) * FPS)
        s, f = divmod(total, FPS)
        return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"

    sections, seen = [], set()
    for c in sorted((c for c in p1["clips"] if c["kind"] == "spine"), key=lambda c: float(eval_frac(c["start"]))):
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        sections.append({"timecode": tc(c["start"]), "name": c["name"].replace("_", " "), "marker_type": "chapter"})
    r_mark = call("mark", "batch_add_markers",
                  {"filepath": str(s1), "markers": sections, "output_path": str(project / "cut_sections.fcpxml")})
    s2 = saved(r_mark)
    v2, p2 = view(s2)
    states.append({"call": "mark · batch_add_markers", "result": v2})

    r_quote = call("gen", "gen_quote", {"provider": "fal", "seconds": 5, "prompt": BROLL_PROMPT})
    quote_id = re.search(r"Quote (\w+)", r_quote).group(1)
    price = re.search(r"Estimated: (\$[\d.]+)", r_quote).group(1)
    model = re.search(r"— (fal/[\w.-]+)", r_quote).group(1)

    class MockProvider(gen.Provider):  # the shape tests/test_gen_group.py uses
        name = "fal"

        def generate(self, prompt, seconds, *, image_path=None):
            return mock_clip.read_bytes()

    real_provider_for = gen.provider_for
    gen.provider_for = lambda p, m=None: MockProvider(gen.resolve_model(p, m))
    try:
        r_broll = call("gen", "gen_broll",
                       {"filepath": str(s2), "clip_name": "Interview_Take", "prompt": BROLL_PROMPT,
                        "seconds": 5, "at": 1, "quote_id": quote_id, "confirm": True,
                        "output_path": str(project / "cut_broll.fcpxml")})
    finally:
        gen.provider_for = real_provider_for
    s3 = saved(r_broll)
    v3, p3 = view(s3)
    states.append({"call": "gen · gen_broll", "result": v3})
    placed = re.search(r"Placed: lane (\d+) at ([\d.]+)s for ([\d.]+)s", r_broll)

    r_history = call("organize", "history", {"filepath": str(s3)})
    journaled = len(re.findall(r"^\| \S+ ago \|", r_history, flags=re.M))
    assert journaled >= 4, r_history

    lines = [
        {"at": 4.6, "call": "generate · import_edl_json",
         "note": f"{len(p0['clips'])} clips · {float(eval_frac(p0['duration'])):.1f}s"},
        {"at": 5.4, "call": "view · view_timeline", "note": "rendered inline (ui://fcp/timeline)", "state": 0},
        {"at": 9.2, "call": "edit · remove_media_silence",
         "note": f"{spans} silences cut · {removed} removed", "state": 1},
        {"at": 13.6, "call": "mark · batch_add_markers",
         "note": f"{len(sections)} section markers", "state": 2},
        {"at": 17.8, "call": "gen · gen_quote",
         "note": f"{model} · {price} · refused until confirm=true"},
        {"at": 20.4, "call": "gen · gen_broll",
         "note": f"lane {placed.group(1)} · {float(placed.group(3)):.0f}s over the take · provider mocked", "state": 3},
        {"at": 24.2, "call": "organize · history",
         "note": f"{journaled} outputs journaled · undo ready"},
    ]
    app_html = asyncio.run(server.read_resource(timeline_app.APP_URI))[0].content
    (WORK / "app.html").write_text(app_html, encoding="utf-8")
    (WORK / "states.json").write_text(json.dumps({
        "prompt": PROMPT, "lines": lines, "states": states, "duration": 28.0,
        "mock_note": "B-roll provider MOCKED for this recording: the quote gate, probe, insert, DTD check and journal ran; no generation did.",
    }), encoding="utf-8")
    for name, text in [("import", r_import), ("silence", r_silence), ("mark", r_mark), ("quote", r_quote),
                       ("broll", r_broll), ("history", r_history)]:
        (WORK / f"reply-{name}.md").write_text(text, encoding="utf-8")

    record(WORK, real_home)
    encode(WORK)


def eval_frac(value: str):
    from fractions import Fraction
    return Fraction(value)


def record(work: Path, home: str) -> None:
    frames = work / "frames"
    frames.mkdir()
    env = {**os.environ, "HOME": home}  # Playwright finds its browsers under the real HOME
    subprocess.run(["node", str(HERE / "record.js"), str(work), str(frames)], check=True, timeout=900, env=env)


def encode(work: Path) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    frames = str(work / "frames" / "%05d.png")
    mp4 = ASSETS / "hero-watch-claude-cut.mp4"
    gif = ASSETS / "hero-watch-claude-cut.gif"
    ffmpeg("-framerate", "30", "-i", frames, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
           "-preset", "slow", "-movflags", "+faststart", str(mp4))
    # Pillow writes the GIF: one global palette drawn from sample frames,
    # every frame quantised to it, identical consecutive frames merged and
    # each frame cropped to the pixels that changed. ffmpeg 9.0's gif muxer
    # wrote every frame whole (20 MB for 28 s) and its paletteuse pass
    # duplicated frames, so it is used for the mp4 only.
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is needed to write the GIF: uv run --with pillow demo/hero/build.py")
    sources = sorted((work / "frames").glob("*.png"))
    picks = [sources[min(len(sources) - 1, round(i * 30 / GIF_FPS))] for i in range(round(len(sources) * GIF_FPS / 30))]

    def small(path: Path) -> Image.Image:
        im = Image.open(path).convert("RGB")
        return im.resize((GIF_WIDTH, round(im.height * GIF_WIDTH / im.width)), Image.Resampling.LANCZOS)

    samples = [small(picks[int(len(picks) * f)]) for f in (0.12, 0.25, 0.4, 0.55, 0.75, 0.97)]
    sheet = Image.new("RGB", (samples[0].width, samples[0].height * len(samples)))
    for i, im in enumerate(samples):
        sheet.paste(im, (0, i * im.height))
    palette = sheet.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    frames_q = [small(p).quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for p in picks]
    frames_q[0].save(gif, save_all=True, append_images=frames_q[1:], duration=round(1000 / GIF_FPS), loop=0, disposal=1)
    with Image.open(gif) as g:
        total_ms = 0
        for i in range(g.n_frames):
            g.seek(i)
            total_ms += g.info.get("duration", 0)
        print(f"gif: {len(frames_q)} frames in, {g.n_frames} after merging identical ones, {total_ms / 1000:.1f}s")
    for p in (mp4, gif):
        print(f"{p.relative_to(REPO)}  {p.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
