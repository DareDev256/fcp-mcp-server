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
4. Writes ``.work/states.json`` (the prompt, the six calls with the notes
   their results earn, the four payload-bearing results, the story's timing,
   the palette and the patterns no image may carry, ``scripts/image_text.py``)
   plus the ``ui://fcp/timeline`` resource as served, then runs two
   recorders (Playwright) on that ONE states.json:

   ``record.js`` composes the close-up: the REAL timeline view (the app as
   served, fed the results in order, so every change is the app's own motion)
   drawn 2.24x on true black, one caption line (the prompt, laid out at its
   final wrap, then a chip per call whose note types at 42 characters a
   second and holds 1.5 s or more), an empty beat before the first call, and
   a virtual camera that leads each edit by 0.3 s, pushes 1.4x onto what it
   changes and eases back out (``stage_story`` has the timing). It encodes
   ``docs/assets/hero-watch-claude-cut.mp4`` (1280x736, 30 fps, about 18 s,
   the finished cut as frame 0 and as its attached poster) and ``.gif`` (the
   same frames, 864 wide, 15 fps, a still picture one frame with its delay).
   ``record_client.js`` records the same run with the app inside a chat
   client, uncropped: ``docs/assets/hero-unedited-recording.mp4``.

   ``tie()`` then refuses unless both recordings showed the run's calls and
   notes, result digests and thumbnail digests, and writes
   ``docs/assets/hero-run.json`` with each file's sha256 (the test holds the
   files to it). The recorders refuse the frame-level gates (record.js says
   which); ``encode()`` refuses a hold over 1.5 s, a loop whose last frame is
   not frame 0 and a GIF over 5 MB. ``--card`` also writes ``card-frame.png``
   for the social card (frozen, so an ordinary build leaves it alone). A
   mocked B-roll is relabelled in the file ("B-roll (mocked)", "mock b-roll:
   ...") and gets a solid MOCK badge. ``--record-only`` re-derives the timing
   and the app from this checkout and records the last run's results again.

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
HOLD_MAX = 1.5      # seconds: no stretch of identical frames may last longer
SEAM = 0.3          # seconds: the last frames crossfade into frame 0, the one crossfade
GIF_WIDTH, GIF_EVERY = 864, 2  # the GIF: the same frames, 864 wide, every 2nd (15 fps), under 5 MB
# The story's reading pace (critic round 6): a result note types at CPS
# characters a second and then holds, fully typed, for HOLD seconds; the
# prompt holds PROMPT_HOLD. The recorder refuses a result held under 1.5 s
# (the prompt under 1.8 s), so these carry a margin.
CPS, HOLD, PROMPT_HOLD = 42, 1.55, 1.9
FOOTAGE_NOTE = "Footage: the author's own gym session; its audio is a test tone, so the rests are real silence."
NOTES = {
    "replay": ("B-roll: one real Gemini Omni Flash generation from the prompt alone, replayed here from the "
               "copy kept in demo/hero/footage through the same quote gate, probe, insert and journal."),
    "real": "B-roll: one real Gemini Omni Flash generation from the prompt alone.",
    "mock": ("B-roll provider MOCKED: no real generation was available for this recording, so the quote "
             "gate, probe, insert, DTD check and journal ran on a stand-in clip."),
}
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
        st = json.loads((WORK / "states.json").read_text())
        st["story"] = stage_story(st["lines"])  # the timing is this file's: re-derive it
        import server  # noqa: E402  (the app as served NOW, never a stale copy)
        from fcpxml import timeline_app  # noqa: E402
        (WORK / "app.html").write_text(asyncio.run(server.read_resource(timeline_app.APP_URI))[0].content, encoding="utf-8")
        (WORK / "states.json").write_text(json.dumps(st), encoding="utf-8")
        record(WORK, os.environ.get("HOME", ""))
        encode(WORK)
        tie(WORK)
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
    # The six calls Claude makes in answer, each with the note its result
    # earns, from the real replies above. The cut was imported (generate ·
    # import_edl_json) before the prompt. Both recordings show these words.
    lines = [
        {"call": "view · view_timeline", "note": "drawn inline: ui://fcp/timeline", "state": 0},
        {"call": "edit · remove_media_silence", "note": f"{rests} rests cut, {removed} removed", "state": 1},
        {"call": "mark · batch_add_markers", "note": f"{len(lifts)} lift markers", "state": 2},
        {"call": "gen · gen_quote", "note": "priced under the cap, waits for confirm"},
        {"call": "gen · gen_broll", "note": f"lane {placed.group(1)}, {float(placed.group(3)):.0f} s, {made}", "state": 3},
        {"call": "organize · history", "note": f"{journaled} outputs journaled, undo ready"},
    ]
    story = stage_story(lines)
    client = {
        "duration": 16.5, "loop_start": 15.0, "prompt": {"you": 0.05, "type": [0.15, 0.85], "claude": 0.9},
        "lines": [dict(line, at=at) for line, at in zip(lines, (1.0, 3.8, 6.6, 9.2, 11.4, 14.0))],
        "footer": f"{FOOTAGE_NOTE} {NOTES[mode]}",
    }
    app_html = asyncio.run(server.read_resource(timeline_app.APP_URI))[0].content
    (WORK / "app.html").write_text(app_html, encoding="utf-8")
    (WORK / "states.json").write_text(json.dumps({
        "prompt": PROMPT, "lines": lines, "states": states, "story": story, "client": client, "long_set": LONG_SET,
        "palette": palette.as_json()["dark"], "mock_clip": MOCK_CLIP if mode == "mock" else None,
        "stale": {"host": image_text.patterns("host"), "app": image_text.patterns("app")},
    }), encoding="utf-8")
    for name, text in [("import", r_import), ("silence", r_silence), ("mark", r_mark), ("quote", r_quote),
                       ("broll", r_broll), ("history", r_history)]:
        (WORK / f"reply-{name}.md").write_text(text, encoding="utf-8")
    print(f"B-roll: {mode}")

    record(WORK, real_home)
    encode(WORK)
    tie(WORK)


def stage_story(lines: list[dict]) -> dict:
    """The staged close-up's timing, in seconds, from the calls and notes.

    Frame 0 is the finished cut under the last result ("organize · history"),
    which holds round the loop's seam. The prompt types over it; the app
    rewinds (its own undo motion: the B-roll collapses, the squat's pieces
    stretch back into one clip, the markers leave) and the stage empties (the
    clips leave in turn). Each call then lands whole in a chip and its note
    types at CPS, then holds HOLD seconds; the result it re-renders lands 0.5
    s after the chip (the view call draws the imported cut in at 0.35 s), and
    the camera leads it by 0.3 s, pushing onto what changes (panning along
    the marker row as the markers land), and eases back out before the next
    call. Nothing on screen is
    still for 1.5 s (build.py checks the frames)."""
    note_lead = 0.15
    caps, deliveries = [], [{"at": 2.0, "state": 0}, {"at": 3.0, "state": "empty"}]
    prompt_typed = 0.4 + len(PROMPT) / CPS
    caps.append({"kind": "prompt", "at": 0.4, "type_at": 0.4, "until": round(prompt_typed + PROMPT_HOLD, 2)})
    t, land = caps[-1]["until"], {}
    for line in lines:
        typed = t + note_lead + len(line["note"]) / CPS
        until = typed + HOLD
        caps.append({"kind": "chip", "call": line["call"], "note": line["note"], "at": round(t, 2),
                     "type_at": round(t + note_lead, 2), "until": round(until, 2)})
        land[line["call"].split(" · ")[1]] = t
        t = until
    end_hold = 1.25  # the last result's hold before the seam; it goes on to the prompt at 0.4
    duration = round(caps[-1]["type_at"] + len(lines[-1]["note"]) / CPS + end_hold, 2)
    caps[-1]["until"] = duration
    v, e, m = land["view_timeline"], land["remove_media_silence"], land["batch_add_markers"]
    q, b, hs = land["gen_quote"], land["gen_broll"], land["history"]
    deliveries += [{"at": round(v + 0.35, 2), "state": 0}, {"at": round(e + 0.5, 2), "state": 1},
                   {"at": round(m + 0.5, 2), "state": 2}, {"at": round(b + 0.5, 2), "state": 3}]
    cam = [(0, "rest"), (e + 0.2, "rest"), (e + 0.65, "squat"), (e + 1.3, "squat"), (e + 1.8, "rest"),
           (m + 0.2, "rest"), (m + 0.6, "markers<"), (m + 0.68, "markers<"), (m + 1.0, "markers>"),
           (m + 1.25, "markers>"), (m + 1.75, "rest"),
           (q + 0.9, "rest"), (q + 1.5, "broll@1.12"), (b + 0.2, "broll@1.12"), (b + 0.65, "broll"),
           (hs + 0.6, "broll"), (hs + 1.2, "rest"), (duration, "rest")]
    return {"duration": duration, "seam": SEAM, "cps": CPS, "captions": caps, "deliveries": deliveries,
            "camera": [{"t": round(at, 2), "on": on} for at, on in cam]}


def tie(work: Path) -> None:
    """Both recordings from ONE run: what each showed (its calls and notes as
    drawn, the digests of the results it was fed, the digests of the
    thumbnails in its last one) must be the run's, or the build refuses.
    docs/assets/hero-run.json records it with each file's sha256, and
    tests/test_hero_assets.py holds the files to it."""
    st = json.loads((work / "states.json").read_text())
    final = json.loads(st["states"][-1]["result"].split("```json fcp-timeline-app", 1)[1].split("```", 1)[0])
    run = {
        "calls": [[line["call"], line["note"]] for line in st["lines"]],
        "results": [hashlib.sha256(x["result"].encode()).hexdigest() for x in st["states"]],
        "thumbs": {f'{c["name"]} @ {c["start"]}': hashlib.sha256(c["thumb"].encode()).hexdigest()
                   for c in final["clips"] if c.get("thumb")},
    }
    recordings = {}
    for name, shown in (("hero-watch-claude-cut.mp4", "shown-stage.json"), ("hero-unedited-recording.mp4", "shown-client.json")):
        seen = json.loads((work / shown).read_text())
        for key in ("calls", "results", "thumbs"):
            if seen[key] != run[key]:
                sys.exit(f"REFUSED: {name} showed other {key} than the run: {seen[key]} vs {run[key]}")
        recordings[name] = {"sha256": hashlib.sha256((ASSETS / name).read_bytes()).hexdigest(), "shown": seen}
    recordings["hero-watch-claude-cut.gif"] = {"sha256": hashlib.sha256((ASSETS / "hero-watch-claude-cut.gif").read_bytes()).hexdigest(),
                                               "shown": recordings["hero-watch-claude-cut.mp4"]["shown"]}
    (ASSETS / "hero-run.json").write_text(json.dumps({"run": run, "recordings": recordings}, indent=1) + "\n", encoding="utf-8")
    print("both recordings show the one run: docs/assets/hero-run.json")


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
    """The staged close-up (record.js) and the unedited chat-client recording
    (record_client.js), from the same states.json."""
    for d in ("frames", "client-frames"):
        shutil.rmtree(work / d, ignore_errors=True)
        (work / d).mkdir()
    env = {**os.environ, "HOME": home}  # Playwright finds its browsers under the real HOME
    card = ["--card"] if "--card" in sys.argv else []  # the card is frozen: only on request
    subprocess.run(["node", str(HERE / "record.js"), str(work), str(work / "frames"), str(ASSETS), *card],
                   check=True, timeout=3600, env=env)
    subprocess.run(["node", str(HERE / "record_client.js"), str(work), str(work / "client-frames")],
                   check=True, timeout=3600, env=env)


def encode(work: Path) -> None:
    """The MP4 and the GIF from the same frames, after two gates on them."""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        sys.exit("Pillow is needed: uv run --with pillow demo/hero/build.py")
    states = json.loads((work / "states.json").read_text())
    fps, seam = 30, states["story"]["seam"]
    frames = sorted((work / "frames").glob("*.png"))
    first = Image.open(frames[0]).convert("RGB")
    # The loop's seam, the one crossfade: the last SEAM seconds blend into frame 0.
    n = round(seam * fps)
    for i, f in enumerate(frames[-n:] if n else [], 1):  # (frames[-0:] would be every frame)
        Image.blend(Image.open(f).convert("RGB"), first, i / n).save(f)
    grey = [Image.open(f).convert("L").resize((160, 96)) for f in frames]
    diff = lambda a, b: ImageStat.Stat(ImageChops.difference(a, b)).mean[0]  # noqa: E731
    # Gate: the loop is seamless (the last frame is frame 0, near enough).
    if diff(grey[-1], grey[0]) > 1.0:
        sys.exit(f"REFUSED: the last frame differs from frame 0 by {diff(grey[-1], grey[0]):.2f}; the loop would jump")
    # Gate: no hold over HOLD_MAX (a run of unchanging frames, round the loop).
    still = [diff(grey[i], grey[(i + 1) % len(grey)]) < 0.05 for i in range(len(grey))]
    if all(still):
        sys.exit("REFUSED: nothing moves")
    k = still.index(False) + 1
    run = longest = 0
    for j in range(len(still)):
        run = run + 1 if still[(k + j) % len(still)] else 0
        longest = max(longest, run)
    if (longest + 1) / fps > HOLD_MAX:
        sys.exit(f"REFUSED: a hold of {(longest + 1) / fps:.2f} s (at most {HOLD_MAX} s)")
    print(f"longest hold {(longest + 1) / fps:.2f} s")
    mp4 = ASSETS / "hero-watch-claude-cut.mp4"
    gif = ASSETS / "hero-watch-claude-cut.gif"
    poster = ASSETS / "hero-still.png"
    first.save(poster)
    ffmpeg("-framerate", str(fps), "-i", str(work / "frames" / "%05d.png"), "-i", str(poster), "-map", "0:v", "-map", "1:v",
           "-c:v:0", "libx264", "-pix_fmt:v:0", "yuv420p", "-crf", "20", "-preset", "slow",
           "-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic", "-movflags", "+faststart", str(mp4))
    # The GIF: the same composition, scaled, every GIF_EVERY-th frame. Pillow
    # merges identical frames and crops each to what changed.
    picks = frames[::GIF_EVERY]
    each = 1000 / fps * GIF_EVERY
    durations = [round((i + 1) * each, -1) - round(i * each, -1) for i in range(len(picks))]
    write_gif(gif, picks, GIF_WIDTH, Image, [int(d) for d in durations])
    if gif.stat().st_size > GIF_MAX_BYTES:
        sys.exit(f"REFUSED: the GIF is {gif.stat().st_size / 1e6:.2f} MB, over {GIF_MAX_BYTES / 1e6:.0f} MB")
    # The unedited recording: the chat client, every frame, no crop.
    unedited = ASSETS / "hero-unedited-recording.mp4"
    ffmpeg("-framerate", str(fps), "-i", str(work / "client-frames" / "%05d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", "22", "-preset", "slow", "-movflags", "+faststart", str(unedited))
    for p in (mp4, gif, unedited):
        print(f"{p.relative_to(REPO)}  {p.stat().st_size / 1e6:.2f} MB")


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
    frames_q = [small(p).quantize(palette=quant, dither=Image.Dither.NONE) for p in picks]
    frames_q[0].save(gif, save_all=True, append_images=frames_q[1:], duration=durations, loop=0, disposal=1)
    with Image.open(gif) as g:
        total_ms = 0
        for i in range(g.n_frames):
            g.seek(i)
            total_ms += g.info.get("duration", 0)
        print(f"gif {width}px: {len(frames_q)} frames in, {g.n_frames} after merging identical ones, {total_ms / 1000:.1f}s")


if __name__ == "__main__":
    main()
