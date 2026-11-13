"""Record the README hero: "watch Claude cut", on the author's own gym session.

One command re-records it:

    uv run --with pillow demo/hero/build.py                  # from the repo root
    uv run --with pillow demo/hero/build.py --record-only    # re-capture the last run's states (host page tweaks)
    GEMINI_API_KEY=... uv run --with pillow demo/hero/build.py --generate   # buy the B-roll once (see below)

What it does, in order:

1. Takes the four silent proxies in ``demo/hero/footage/`` (the author's own
   2026-08-08 gym session, cut by ``footage.py``: he is the only person in
   frame, and no audio was kept because the gym's music is on it) and gives
   each a synthesised test-tone soundtrack. The squat set's tone stops for
   two 1.5 s rests (5-6.5 s and 11.5-13 s) between three 5 s sets: that real
   digital silence is what remove_media_silence cuts. The on-screen note says
   the audio is a tone. (The lengths are chosen so every clip's name fits the
   phone-width stage, cut and uncut.)
2. Runs the REAL tool handlers through ``server.call_tool`` — the same entry
   an MCP client reaches — and keeps every ``view_timeline`` result verbatim:

       generate · import_edl_json      the cut, from an EDL of the four clips (setup:
                                       the story opens on it, the chat starts after)
       view     · view_timeline        state 0
       edit     · remove_media_silence state 1   (real ffmpeg silencedetect)
       mark     · batch_add_markers    state 2   (one marker per lift)
       gen      · gen_quote            the money gate, priced from the table
       gen      · gen_broll            state 3   (Gemini Omni Flash, prompt only)
       organize · history              the journal rows the run produced

3. The B-roll is ONE real generation, bought once. ``--generate`` runs the
   pipeline with the real ``gemini`` provider under a per-call cap of
   US$0.70 (``FCP_MCP_GEN_MAX_USD``), from the prompt alone
   (``condition_on_frame: false``: no frame of the footage leaves the
   machine), keeps a silent 360p copy and a record of what was quoted in
   ``demo/hero/footage/broll.{mp4,json}``, and stops. It refuses when that
   record already exists: a rebuild never pays twice. Every ordinary build
   REPLAYS the kept copy through the same gate, probe, insert, DTD check
   and journal, and the on-screen note says so. With no copy on file and
   no ``--generate``, the provider is mocked and the note says THAT plainly.
4. Writes ``.work/states.json`` (the prompt, the tool lines, the four
   payload-bearing results, the palette and the patterns no image may
   carry, ``scripts/image_text.py``) plus the ``ui://fcp/timeline`` resource
   as served, then runs ``record.js`` (Playwright), which sends the four
   results IN ORDER to one app instance, so every change is the app's own
   motion (a split clip's pieces slide together, new markers drop in, the
   B-roll is laid in), never a dissolve between two renders. It encodes
   ``docs/assets/hero-watch-claude-cut.mp4`` (the chat beside the app; each
   tool call lands as its edit starts to move) and ``.gif`` (the README's own
   cut: the ruler and lanes alone, laid out at phone width and drawn at 3x so
   the app's type is 30 px or more, ~9 s with a hold per step). Both open on
   the finished cut, and the MP4 also carries that frame as its attached
   poster. A mocked B-roll is relabelled in the file ("B-roll (mocked)",
   "mock b-roll: ...") and gets a solid MOCK pill.

Requires ffmpeg/ffprobe on PATH, Pillow (for the GIF: it merges identical
frames, which ffmpeg's gif muxer does not), and Node with the ``playwright``
package resolvable (``npm i playwright`` anywhere and point ``HERO_PLAYWRIGHT``
at that ``node_modules/playwright`` if it is not on Node's module path).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import image_text  # noqa: E402

from fcpxml import palette  # noqa: E402

WORK = HERE / ".work"
FOOTAGE = HERE / "footage"
ASSETS = REPO / "docs" / "assets"
FPS = 24
GIF_MAX_BYTES = 5_000_000
PROMPT = "Cut the rest between sets, mark every lift, and put B-roll over the long set."
LOOP_START = 15.0  # seconds into the story where the finished cut is on screen and every line is in
DURATION = 16.5
# The README GIF's own cut: (state, seconds on screen, moves into it). Frame 0
# is the finished cut, then the story from the imported cut, one step each.
GIF_STORY = [(3, 3.0, False), (0, 1.5, False), (1, 2.0, True), (2, 2.0, True), (3, 0.6, True)]
MOCK_CLIP, MOCK_MARKER = "B-roll (mocked)", "mock b-roll: Toronto skyline at dusk"
LONG_SET = "Squat"

# name -> (proxy file, seconds used, tone expression or None for a steady quiet tone)
CLIPS = {
    "Bench": ("bench.mp4", 5.5, None),
    LONG_SET: ("squat.mp4", 18, "0.25*sin(2*PI*220*t)*(between(t,0,5)+between(t,6.5,11.5)+between(t,13,18))"),
    "Press": ("press.mp4", 5, None),
    "Finish": ("finish.mp4", 5.5, None),
}

BROLL_PROMPT = "Toronto skyline at dusk from a rooftop, slow push in"
BROLL_SECONDS = 10  # Omni is quoted as at least one 10 s call, so 10 s costs what 8 s does
BROLL_PROVIDER, BROLL_MODEL = "gemini", "omni-1.2-flash"
BROLL_CAP_USD = Decimal("0.70")
BROLL_COPY = FOOTAGE / "broll.mp4"
BROLL_RECORD = FOOTAGE / "broll.json"

FOOTAGE_NOTE = "Footage: the author's own gym session; its audio is a test tone, so the rests are real silence."
NOTES = {
    "replay": ("B-roll: one real Gemini Omni Flash generation from the prompt alone, replayed here from the "
               "copy kept in demo/hero/footage through the same quote gate, probe, insert and journal."),
    "real": "B-roll: one real Gemini Omni Flash generation from the prompt alone.",
    "mock": ("B-roll provider MOCKED: no real generation was available for this recording, so the quote "
             "gate, probe, insert, DTD check and journal ran on a stand-in clip."),
}


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=300)


def with_tone(path: Path, proxy: Path, seconds: int, tone: str | None) -> None:
    """The proxy's picture with a synthesised soundtrack: the footage has none."""
    audio = (f"aevalsrc='{tone}':s=48000:d={seconds}" if tone
             else f"sine=frequency=330:sample_rate=48000:duration={seconds}")
    cmd = ["-i", str(proxy), "-f", "lavfi", "-i", audio, "-map", "0:v", "-map", "1:a", "-t", str(seconds),
           "-c:v", "copy", "-c:a", "aac"]
    if not tone:
        cmd += ["-af", "volume=0.2"]
    ffmpeg(*cmd, str(path))


def mock_clip(path: Path) -> None:
    """The mocked provider's clip: the gap hatch, so its thumbnail reads as
    "not footage". What it IS is said by the clip's name, its marker and the
    solid MOCK pill record.js puts on the tile (a watermark under the label
    read at 1.5:1)."""
    from PIL import Image, ImageDraw

    frame = Image.new("RGB", (1280, 720), palette.dark("panel"))
    draw = ImageDraw.Draw(frame)
    for x in range(-720, 1280, 48):
        draw.line([(x, 720), (x + 720, 0)], fill=palette.dark("raised-2"), width=14)
    still = path.with_suffix(".png")
    frame.save(still)
    ffmpeg("-loop", "1", "-i", str(still), "-t", str(BROLL_SECONDS), "-r", str(FPS),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", str(path))


def text_of(result) -> str:
    return "\n".join(c.text for c in result)


def broll_mode() -> str:
    on_file = BROLL_COPY.is_file() and BROLL_RECORD.is_file()
    if "--generate" in sys.argv:
        if on_file:
            sys.exit(f"REFUSED: a generation is already on file ({BROLL_RECORD.relative_to(REPO)}); a rebuild "
                     "never pays twice. Delete both broll files deliberately to buy another.")
        if not os.environ.get("GEMINI_API_KEY"):
            sys.exit("REFUSED: --generate needs GEMINI_API_KEY in the environment.")
        return "real"
    return "replay" if on_file else "mock"


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
    mode = broll_mode()
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "home").mkdir(parents=True)
    (WORK / "project").mkdir()
    os.environ["FCP_MCP_JOURNAL"] = str(WORK / "journal")
    os.environ["FCP_MCP_INDEX"] = "off"
    os.environ["FCP_MCP_GEN_MAX_USD"] = str(BROLL_CAP_USD)  # the gate refuses anything dearer
    real_home = os.environ.get("HOME", "")
    os.environ["HOME"] = str(WORK / "home")  # the private thumbnail cache lands here
    if mode != "real":
        os.environ.setdefault("GEMINI_API_KEY", "hero-replay-no-key")  # the gate reads a key; nothing is sent
    os.environ.pop("FCP_PROJECTS_DIRS", None)
    os.environ.pop("FCP_PROJECTS_DIR", None)
    import server  # noqa: E402
    from fcpxml import gen, timeline_app  # noqa: E402

    media = WORK / "media"
    media.mkdir()
    for name, (proxy, seconds, tone) in CLIPS.items():
        with_tone(media / f"{name}.mov", FOOTAGE / proxy, seconds, tone)

    def call(tool: str, action: str, args: dict) -> str:
        return text_of(asyncio.run(server.call_tool(tool, {"action": action, "args": args})))

    project = WORK / "project"
    edl = {
        "sources": {n: str(media / f"{n}.mov") for n in CLIPS},
        "ranges": [{"source": n, "start": 0, "end": CLIPS[n][1]} for n in CLIPS],
    }
    (project / "edl.json").write_text(json.dumps(edl), encoding="utf-8")
    cut = project / "cut.fcpxml"
    r_import = call("generate", "import_edl_json",
                    {"filepath": str(project / "edl.json"), "output_path": str(cut), "name": "Session cut"})
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
                     {"filepath": str(cut), "clip_name": LONG_SET, "min_silence": 1.0,
                      "output_path": str(project / "cut_rests_removed.fcpxml")})
    s1 = saved(r_silence)
    v1, p1 = view(s1)
    states.append({"call": "edit · remove_media_silence", "result": v1})
    removed = re.search(r"\*\*Total Removed\*\*: ([^\n]+)", r_silence).group(1).strip()
    rests = sum(int(n) for n in re.findall(rf"\| {LONG_SET} \| (\d+) \|", r_silence))

    # One marker per lift: the first clip of each source name, in timeline order.
    def tc(fraction: str) -> str:
        total = round(Fraction(fraction) * FPS)
        s, f = divmod(total, FPS)
        return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"

    lifts, seen = [], set()
    for c in sorted((c for c in p1["clips"] if c["kind"] == "spine"), key=lambda c: Fraction(c["start"])):
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        lifts.append({"timecode": tc(c["start"]), "name": c["name"], "marker_type": "chapter"})
    r_mark = call("mark", "batch_add_markers",
                  {"filepath": str(s1), "markers": lifts, "output_path": str(project / "cut_lifts.fcpxml")})
    s2 = saved(r_mark)
    v2, p2 = view(s2)
    states.append({"call": "mark · batch_add_markers", "result": v2})

    r_quote = call("gen", "gen_quote", {"provider": BROLL_PROVIDER, "model": BROLL_MODEL,
                                         "seconds": BROLL_SECONDS, "prompt": BROLL_PROMPT})
    quote_id = re.search(r"Quote (\w+)", r_quote).group(1)
    quoted = Decimal(re.search(r"Estimated: \$([\d.]+)", r_quote).group(1))
    if quoted > BROLL_CAP_USD:
        if mode == "real":
            sys.exit(f"REFUSED: the quote is ${quoted}, over the ${BROLL_CAP_USD} cap; nothing was spent.")
        mode = "mock"

    class Kept(gen.Provider):  # the shape tests/test_gen_group.py uses
        name = BROLL_PROVIDER

        def __init__(self, model, data: bytes):
            super().__init__(model)
            self._data = data

        def generate(self, prompt, seconds, *, image_path=None):
            return self._data

    real_provider_for = gen.provider_for
    if mode == "replay":
        data = BROLL_COPY.read_bytes()
        gen.provider_for = lambda p, m=None: Kept(gen.resolve_model(p, m), data)
    elif mode == "mock":
        mock_clip(media / "mock-broll.mp4")
        data = (media / "mock-broll.mp4").read_bytes()
        gen.provider_for = lambda p, m=None: Kept(gen.resolve_model(p, m), data)
    try:
        r_broll = call("gen", "gen_broll",
                       {"filepath": str(s2), "clip_name": LONG_SET, "prompt": BROLL_PROMPT,
                        "provider": BROLL_PROVIDER, "model": BROLL_MODEL, "seconds": BROLL_SECONDS,
                        "at": 1, "condition_on_frame": False, "quote_id": quote_id, "confirm": True,
                        "output_path": str(project / "cut_broll.fcpxml")})
    finally:
        gen.provider_for = real_provider_for
    if "Saved to:" not in r_broll:
        sys.exit("gen_broll did not write a file:\n" + r_broll)
    if mode == "real":
        keep_generation(r_broll, quoted)
        print(f"one generation bought and kept in {BROLL_COPY.relative_to(REPO)}; run again without --generate")
        return

    s3 = saved(r_broll)
    if mode == "mock":
        relabel_mock(s3)
    v3, p3 = view(s3)
    states.append({"call": "gen · gen_broll", "result": v3})
    placed = re.search(r"Placed: lane (\d+) at ([\d.]+)s for ([\d.]+)s", r_broll)

    r_history = call("organize", "history", {"filepath": str(s3)})
    journaled = len(re.findall(r"^\| \S+ ago \|", r_history, flags=re.M))
    assert journaled >= 4, r_history

    made = "provider mocked" if mode == "mock" else "generated"
    # The cut was imported (generate · import_edl_json) before the prompt, so
    # the chat lists the six calls Claude makes in answer. Each line lands as
    # its re-render starts to move (record.js starts the motion 0.1 s after
    # "at"); the timeline first appears WITH the view call, a second in.
    lines = [
        {"at": 1.0, "call": "view · view_timeline", "note": "rendered inline (ui://fcp/timeline)", "state": 0},
        {"at": 3.8, "call": "edit · remove_media_silence",
         "note": f"{rests} rests cut · {removed} removed", "state": 1},
        {"at": 6.6, "call": "mark · batch_add_markers", "note": f"{len(lifts)} lift markers", "state": 2},
        {"at": 9.2, "call": "gen · gen_quote", "note": "quoted under the cap, refused until confirm=true"},
        {"at": 11.4, "call": "gen · gen_broll",
         "note": f"lane {placed.group(1)} · {float(placed.group(3)):.0f}s over the long set · {made}", "state": 3},
        {"at": 14.0, "call": "organize · history", "note": f"{journaled} outputs journaled · undo ready"},
    ]
    app_html = asyncio.run(server.read_resource(timeline_app.APP_URI))[0].content
    (WORK / "app.html").write_text(app_html, encoding="utf-8")
    (WORK / "states.json").write_text(json.dumps({
        "prompt": PROMPT, "lines": lines, "states": states, "duration": DURATION,
        "loop_start": LOOP_START, "palette": palette.as_json()["dark"],
        "gif_story": [{"state": st, "hold": hold, "moves": moves} for st, hold, moves in GIF_STORY],
        "mock_clip": MOCK_CLIP if mode == "mock" else None,
        "footer": f"{FOOTAGE_NOTE} {NOTES[mode]}",
        "stale": {"host": image_text.patterns("host"), "app": image_text.patterns("app")},
    }), encoding="utf-8")
    for name, text in [("import", r_import), ("silence", r_silence), ("mark", r_mark), ("quote", r_quote),
                       ("broll", r_broll), ("history", r_history)]:
        (WORK / f"reply-{name}.md").write_text(text, encoding="utf-8")
    print(f"B-roll: {mode}")

    record(WORK, real_home)
    encode(WORK)


def relabel_mock(path: Path) -> None:
    """A mocked B-roll says so in the file the app draws: the clip is named
    "B-roll (mocked)", its marker "mock b-roll: ...", its note "mocked
    provider". The real gen output names them for a real generation."""
    text = path.read_text(encoding="utf-8")
    clip = re.search(r'name="(Generated broll-[^"]+)"', text).group(1)
    marker = re.search(r'value="(generated: [^"]+)"', text).group(1)
    note = re.search(r'note="(generated by [^"]+)"', text).group(1)
    # The name is on the asset and on its clip; the marker and its note once.
    assert text.count(f'name="{clip}"') == 2, clip
    text = text.replace(f'name="{clip}"', f'name="{MOCK_CLIP}"')
    for old, new in ((f'value="{marker}"', f'value="{MOCK_MARKER}"'),
                     (f'note="{note}"', 'note="mocked provider: no generation ran"')):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def keep_generation(reply: str, quoted: Decimal) -> None:
    """Keep the one paid generation: a silent 360p copy and what it cost."""
    media = Path(re.search(r"Media: (\S+) \(", reply).group(1))
    FOOTAGE.mkdir(exist_ok=True)
    # First, out of .work (which every build wipes): a paid file is never lost.
    original = FOOTAGE / "broll-original-with-audio.mp4"  # .gitignore'd: it carries native audio
    shutil.copy2(media, original)
    probe = re.search(r"\(([\d.]+)s, (\d+)x(\d+) @ (\d+)/(\d+) fps", reply)
    seconds = Decimal(probe.group(1))
    ffmpeg("-i", str(media), "-an", "-sn", "-dn", "-map_metadata", "-1", "-vf", "scale=640:-2,fps=24",
           "-c:v", "libx264", "-preset", "slow", "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           str(BROLL_COPY))
    BROLL_RECORD.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "provider": BROLL_PROVIDER, "model": BROLL_MODEL, "prompt": BROLL_PROMPT,
        "requested_seconds": BROLL_SECONDS, "conditioned_on_a_frame": False,
        "quoted_usd": str(quoted), "cap_usd": str(BROLL_CAP_USD),
        "delivered": {"seconds": str(seconds), "width": int(probe.group(2)), "height": int(probe.group(3)),
                      "fps": f"{probe.group(4)}/{probe.group(5)}"},
        "estimated_billed_usd": str((seconds * Decimal("0.07")).quantize(Decimal("0.01"))),
        "estimate_basis": "Omni 1.2 Flash at about $0.07 per delivered second of 720p (fcpxml/gen.py, 2026-11-12)",
        "original_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        "kept_copy": "silent, 640 wide, H.264; the original carried native audio, which is not kept",
    }, indent=2) + "\n", encoding="utf-8")


def record(work: Path, home: str) -> None:
    frames = work / "frames"
    frames.mkdir()
    env = {**os.environ, "HOME": home}  # Playwright finds its browsers under the real HOME
    subprocess.run(["node", str(HERE / "record.js"), str(work), str(frames), str(ASSETS)],
                   check=True, timeout=900, env=env)


def encode(work: Path) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    frames = str(work / "frames" / "%05d.png")
    mp4 = ASSETS / "hero-watch-claude-cut.mp4"
    gif = ASSETS / "hero-watch-claude-cut.gif"
    poster = ASSETS / "hero-still.png"
    flatten(ASSETS / "card-frame.png")
    # The frames already open on the finished cut (record.js captures from
    # LOOP_START round to it), so the MP4's first decoded frame is the
    # finished cut too; it also carries that frame as an attached poster.
    ffmpeg("-framerate", "30", "-i", frames, "-i", str(poster), "-map", "0:v", "-map", "1:v",
           "-c:v:0", "libx264", "-pix_fmt:v:0", "yuv420p", "-crf", "22", "-preset", "slow",
           "-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic", "-movflags", "+faststart", str(mp4))
    # Pillow writes the GIF: one global palette drawn from sample frames,
    # every frame quantised to it, identical consecutive frames merged and
    # each frame cropped to the pixels that changed. ffmpeg 9.0's gif muxer
    # wrote every frame whole (20 MB for 28 s) and its paletteuse pass
    # duplicated frames, so it is used for the mp4 only.
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is needed to write the GIF: uv run --with pillow demo/hero/build.py")
    # The README GIF is its own cut (record.js's gif-frames: the ruler and lanes
    # alone, laid out at phone width and drawn at 3x), one PNG per distinct
    # picture with how long it stays (durations.json): a hold is one frame.
    gif_dir = work / "gif-frames"
    holds = json.loads((gif_dir / "durations.json").read_text())
    sources = [gif_dir / name for name, _ in holds]
    Image.open(sources[0]).convert("RGB").save(ASSETS / "hero-timeline-still.png")  # frame 0: the finished cut
    width = Image.open(sources[0]).width
    # Always the full width: a narrower GIF would shrink the type the cut
    # exists to keep, so over the cap is a refusal, not a resize.
    write_gif(gif, sources, width, Image, [ms for _, ms in holds])
    if gif.stat().st_size > GIF_MAX_BYTES:
        sys.exit(f"the GIF is {gif.stat().st_size / 1e6:.2f} MB at {width} px, over the "
                 f"{GIF_MAX_BYTES / 1e6:.1f} MB cap: shorten the story")
    for p in (mp4, gif):
        print(f"{p.relative_to(REPO)}  {p.stat().st_size / 1e6:.2f} MB")


def flatten(path: Path) -> None:
    """A PNG with no transparency: corners composited onto the ground."""
    from PIL import Image
    im = Image.open(path)
    if im.mode in ("RGBA", "LA", "P"):
        ground = Image.new("RGBA", im.size, palette.dark("ground"))
        ground.alpha_composite(im.convert("RGBA"))
        ground.convert("RGB").save(path)


def write_gif(gif: Path, picks: list[Path], width: int, image_module, durations: list[int]) -> None:
    Image = image_module  # noqa: N806

    def small(path: Path):
        im = Image.open(path).convert("RGB")
        return im if im.width == width else im.resize((width, round(im.height * width / im.width)), Image.Resampling.LANCZOS)

    samples = [small(picks[int(len(picks) * f)]) for f in (0.02, 0.12, 0.25, 0.4, 0.55, 0.75, 0.97)]
    sheet = Image.new("RGB", (samples[0].width, samples[0].height * len(samples)))
    for i, im in enumerate(samples):
        sheet.paste(im, (0, i * im.height))
    quant = sheet.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    frames_q = [small(p).quantize(palette=quant, dither=Image.Dither.FLOYDSTEINBERG) for p in picks]
    frames_q[0].save(gif, save_all=True, append_images=frames_q[1:], duration=durations, loop=0, disposal=1)
    with Image.open(gif) as g:
        total_ms = 0
        for i in range(g.n_frames):
            g.seek(i)
            total_ms += g.info.get("duration", 0)
        print(f"gif {width}px: {len(frames_q)} frames in, {g.n_frames} after merging identical ones, {total_ms / 1000:.1f}s")


if __name__ == "__main__":
    main()
