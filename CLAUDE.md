# CLAUDE.md — fcp-mcp-server

## What This Is

MCP server that reads/writes Final Cut Pro XML (FCPXML) files. 15 grouped tools (`inspect`, `diagnose`, `edit`, `mark`, `generate`, `transcript`, `deliver`, `preview`, `watch`, `index`, `scenes`, `organize`, `find`, `view`, `gen`) are advertised by default, dispatching into 102 underlying operations for timeline analysis, batch editing, QC, generation, multi-track support, media relink, NLE export, transcript-based editing (local Whisper, or ElevenLabs Scribe opt-in for speakers), shot-boundary detection, bulk library organization with a journal-backed `undo`, tiered shot search (transcript → metadata → offline MLX captions), a review gate on `deliver`, an MCP Apps timeline the host renders inline (`view`), generative fill behind a quote-then-confirm money gate (`gen`), an `expected_sha256` project guard on every filepath action, and LIVE FCP control (push_to_fcp / list_fcp_libraries via Apple events). Set `FCP_MCP_LEGACY_TOOLS=1` to also advertise the 71 flat tool schemas (86 advertised in total; the 31 operations born as group actions — preview, watch, index, scenes, organize, find, view, gen, import_edl_json — have no flat schema). Reads FCPXML 1.8–1.14 (incl. `.fcpxmld` bundles with sidecar preservation), writes 1.13 by default. Dual-mode (XML + Live) direction: `docs/CAPABILITY-AUDIT-2026-06.md`.

## Architecture

```
server.py           — MCP server entry point. 71 flat tool definitions, handlers, resources, prompts.
                      Dispatch dict pattern: TOOL_HANDLERS maps tool names → async handler functions.
TOOL_GROUPS         — 15 grouped verbs advertised by default. Dispatch into
                      TOOL_HANDLERS, which holds all 102 handlers. New groups are
                      defined in tools/ and merged in by _merge_extra_tools().
                      Hiding a tool from list_tools does NOT stop it dispatching.
                      _journaled is the one seam every call crosses: the
                      expected_sha256 project guard, then the review gate,
                      then the journal ledger, then the sha256 footer on
                      every write. A guard added anywhere else is a guard
                      some action skips.
fcpxml/timeline_app.py — The MCP Apps timeline: a PURE payload builder (every
                      time an exact "n/d" string; thumbnails and transcripts
                      are injected callables, so the fixtures round-trip with
                      no ffmpeg) and the self-contained ui://fcp/timeline
                      shell (no CDN, no fetch). tools/view.py is the group:
                      its own tool because _meta.ui binds the UI to a TOOL.
                      The in-iframe method names follow the SEP-1865 spec
                      text; only the server-side field names are verified
                      against the 2.x SDK (mcp/server/apps.py).
fcpxml/gen.py       — Generative fill. quote() -> confirm_quote() is the money
                      gate: the quote is RECOMPUTED from the job, never trusted
                      from the register, and _build_quote must not register or
                      an expired quote is refreshed by the call checking it.
                      Keys travel in one header; _http refuses a key in a URL
                      or body and redact() scrubs every reply. probe_media()
                      is the only source of fps. insert_generated() writes
                      every time attribute through format_seconds in the
                      sequence's timebase (the asset in the media's own) and
                      appends a <gap> when the position is past the spine.
                      tools/gen.py is the group; one runner for the three
                      write actions so the gate cannot be skipped by one.
fcpxml/journal.py   — Append-only operation ledger under ~/.fcp-mcp/journal/
                      (FCP_MCP_JOURNAL relocates; `off` disables). One ledger per
                      INPUT DIRECTORY — the project is the folder. Records hold
                      paths + sha256, never content. undo() moves outputs to
                      undone/ and refuses on hash mismatch; it never deletes.
                      _review_gate in server.py reads it: six deliver actions
                      refuse without a preview_render matching the current hash
                      (confirm_unreviewed=true overrides, and is stamped).
fcpxml/find.py      — PURE ranking over transcript words and metadata ranges;
                      every Hit names its tier and a why. tools/find.py is the
                      router: tier 3 (vision) only when asked, when the query
                      reads visual, or when tiers 1-2 fall short; at most
                      MAX_LIVE_FRAMES live captions per call. It never
                      transcribes — index, then sidecar, then "no transcript".
fcpxml/vlm.py       — Offline MLX captions ([find] extra, Apple Silicon only).
                      Captions a frame downscaled to CAPTION_SHORT_SIDE=1080 (short
                      edge, aspect kept): Qwen-VL tokenizes by pixel area, so 4K
                      costs ~4x the vision tokens for the same sentence. Only this
                      caller passes the cap — preview tools measure frames and must
                      keep full resolution.
                      HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE are set BEFORE
                      mlx_vlm is imported; a missing model is reported with its
                      `hf download <id>` command and never fetched.
fcpxml/diversity.py — min_source_separation + Jaccard near-duplicate ceiling
                      for assemblies; score() is reported on every rough cut.
fcpxml/index.py     — SQLite cache at ~/.fcp-mcp/index.db (FCP_MCP_INDEX=off disables;
                      any other value is a path). A CACHE, never a source of truth:
                      the suite runs green with it off (CI job `index-off`). Rows
                      keyed to (path, mtime, size); a re-exported source drops its
                      rows on the next touch. Time stored as integer num/den.
fcpxml/progress.py  — MCP progress notifications per clip. Resolves the request
                      context on either SDK (1.x property, 2.x contextvar set by
                      mcp_compat) and mutes itself on the first send failure.
fcpxml/scenes.py    — Shot boundaries. PySceneDetect ([scenes] extra) when
                      installed, else ffmpeg's `select=gt(scene,T)`, which is
                      coarse: red->green scores 0.0 and is missed. Results cached.
fcpxml/transcript_pack.py — Every transcript on one page for planning a dialogue
                      edit; sizes in BYTES, 60KB chat cap, full file on write=true.
fcpxml/transcribe.py — backend="local" (faster-whisper, never leaves the machine)
                      or "elevenlabs" (Scribe upload, speakers + audio events,
                      ELEVENLABS_API_KEY in the xi-api-key header ONLY). A local
                      cache never satisfies a diarize request — is_diarized().
fcpxml/palette.py   — THE colours (0.31). True black, one cyan (#00E5FF; #007A8A for
                      text and lines on white), red only for failure. The app,
                      preview://, the waveform, the hero, the card, the logo and
                      the badges read it; tests/test_palette.py fails on any hex
                      in a surface file that is not a token, any token that is
                      not grey/cyan/red, and any text pair under 4.5:1. A new
                      surface file goes on that test's SURFACES list.
fcpxml/preview.py   — standalone HTML timeline render, served as preview://<path>.
                      Draws from the XML: shows what was WRITTEN. It cannot tell a
                      fixed cut from a broken one — use fcpxml/visual.py for that.
fcpxml/filtergraph.py — Timeline -> ffmpeg graph. PURE (no subprocess, no I/O), so
                      compilation is asserted exactly on machines without ffmpeg.
                      INVARIANT (0.28): output time IS timeline time, and every
                      frame is the timeline's picture or a NAMED black substitute.
                      Fraction end to end, quantised to 24fps output frames once,
                      cumulatively (_frame). Source time is on the ASSET clock:
                      file time = src_in - MediaRef.origin; a still has no clock
                      and is looped. Spine Clip position is .start; a ConnectedClip
                      is placed by .timeline_start (its .offset is in the PARENT's
                      clock — the fallback only for hand-built models). Holes in
                      the spine are black gaps. Transitions draw on HANDLES like
                      Final Cut (outgoing stream runs into its tail, incoming
                      starts in its head, xfade offset = the transition's own
                      start); never squeezed to a neighbour. Every picture is
                      overlaid onto a canvas of the SEQUENCE's shape at its
                      conform + transform (placement()), which is also why mixed
                      sizes no longer break concat/xfade. Sound (0.30): every
                      clip that plays its asset's audio is adelay'd to its
                      instant over an anullsrc bed of the timeline's length
                      (amix duration=first, normalize=0); transitions afade
                      over the same handles. Lanes composite as ONE
                      transparent track per lane number from frame 0
                      (lane_tracks): never an overlay whose secondary starts in
                      the future — ffmpeg 6.1 drops a chained one silently.
fcpxml/render.py    — Probes every source (probe_source), executes the graph with a
                      fallback ladder (full -> hard cuts -> no lanes, each named),
                      reads the artifact's OWN duration back (UNVERIFIED past one
                      frame; no readable video = error, gate stays shut), and
                      verify_lanes() looks for each lane clip in the frame at its
                      placement. A rendered file existing is not evidence.
fcpxml/visual.py    — Filmstrip + waveform from SOURCE MEDIA. The verification
                      instrument. showwavespic draws on a transparent background that
                      flattens to white, so the trace is composited over a dark plate.
fcpxml/watchfolder.py — Export detection. Digests CONTENT, not (mtime, size): two
                      exports of equal byte count in one timestamp tick are otherwise
                      indistinguishable from no export at all.
fcpxml/bridges.py   — SpliceKit :9876 / CommandPost :27480. DETECTION ONLY, loopback
                      only. Their RPC signatures are unverified; never call them.
fcpxml/edl.py       — video-use edl.json -> FCPXML. Schema is {sources, ranges, grade?}
                      and ranges[].source is a KEY into sources, NOT a path.
tools/              — New tool groups register here instead of growing server.py.
                      server.py binds itself via tools.bind_server(); group modules
                      must NEVER `import server` — in production it runs as __main__,
                      so that loads a SECOND copy with its own registry and sandbox.
                      Existing families migrate out of server.py a slice at a time:
                      tools/nle.py holds NLE export, effects, audio, compound clips,
                      templates and relink; tools/media.py holds silence/beat
                      detection and every transcript-driven edit. server.py
                      RE-EXPORTS them under their
                      original names, so TOOL_HANDLERS and every caller resolve one
                      definition. Moved handlers reach server-owned names through
                      srv = _common.tools.server_module() rather than importing them:
                      tests monkeypatch those on the server module, and an import
                      would bind a copy no patch can reach — the guard would keep
                      passing while guarding nothing.
skill/              — the final-cut-pro Claude Code skill wrapping this server

fcpxml/retime.py    — Exact positive speed maps, frame-baked ramps and guarded ripple/reset.
                      Intrinsic animation uses the adjusted local clock, not mapped
                      media time; see docs/retiming.md before changing timing.
tools/retime.py     — Speed schemas/adapters using the keyframe staged publisher.
fcpxml/keyframes.py — Intrinsic animation edits on the original XML; unique clip paths,
                      rational clip-relative time, native FCP interpolation. See
                      docs/keyframes.md before changing its parameter/time contract.
tools/keyframes.py  — Keyframe schemas/adapters, staged DTD validation, no-replace
                      publication. Hooks into inspect/edit, not a new group.

fcpxml/parser.py    — Reads FCPXML → Python objects (Timeline, Clip, ConnectedClip, Marker, etc.)
                      Parses spine, connected clips (lanes), secondary storylines, gap-attached clips, roles.
fcpxml/writer.py    — Writes modifications back to FCPXML. Handles markers, trimming, gaps, transitions,
                      connected clips, roles, reformatting, silence detection/removal.
fcpxml/rough_cut.py — Generates new timelines from source clips (rough cuts, montages, A/B rolls).
fcpxml/diff.py      — Timeline comparison engine. Detects added/removed/moved/trimmed clips & markers.
fcpxml/export.py    — DaVinci Resolve FCPXML v1.9 export + FCP7 XMEML v5 export for cross-NLE workflows.
fcpxml/models.py    — Data classes: TimeValue, Timecode, Clip, ConnectedClip, CompoundClip, Timeline, etc.
fcpxml/media_intel.py — Real media analysis (v0.10). Audio silence detection + beat detection (librosa, optional
                      [intelligence] extra, lazy import). Silence via bounded ffmpeg
                      subprocess (silencedetect), source→timeline mapping; removal composes
                      writer.cut_clip_ranges (element-based — immune to duplicate-name ambiguity). No new Python deps;
                      degrades gracefully (returns None) when ffmpeg is absent.
fcpxml/mcp_compat.py — Registers the six MCP handlers against either the mcp 1.x
                      decorator API or the 2.x add_request_handler API. mcp 2.0
                      removed the decorators outright; detection is by attribute
                      probe, not version string. Also carries tool_input_schema()
                      and resource_mime_type(), because 2.x renamed those fields
                      to snake_case while keeping the old spelling as a
                      SERIALISATION alias only — building with inputSchema=
                      works, reading .inputSchema raises.
fcpxml/dtd.py       — Validates output against Apple's official DTDs (located inside the installed FCP app bundle;
                      xmllint needs the DTD path as a percent-encoded file:// URI — spaces in "Final Cut Pro.app" break it).
```

## Key Patterns

- **TimeValue**: All times are rational fractions (numerator/denominator) matching FCPXML's `"600/2400s"` format. Never use floats for time math.
- **Frame rates are rationals, never `int(fps)`**: 23.976 is `24000/1001`, and `int(23.976)` is 23. Every timebase comes from `fcpxml.rational.rational_fps()`. Companions: `frame_duration_seconds/_attr` (a one-frame duration), `nominal_fps` (frames per labelled second of non-drop timecode — 24 at 23.98), `fcp_frame_rate_name` (the `srcFrameRate` enum string, and what to print), `tick_timebase` (frame snapping; integer rates keep 2400 ticks, NTSC rates cannot).
- **_parse_project()**: Helper that parses FCPXML and returns `(tree, timeline, project)` tuple. Most handlers start with this.
- **generate_output_path()**: Creates `_modified`, `_chapters`, etc. suffixed output paths so originals aren't overwritten.
- **Tool handlers**: Each tool has its own `async def handle_<name>(arguments: dict)` function. All return via `_text_result(text)` which wraps strings in the MCP `TextContent` list.
- **Connected clips**: Clips with `lane` attribute hang off spine clips. Positive lane = above (video), negative = below (audio). Secondary `<storyline>` elements also contain connected clips.
- **XMEML export**: Converts spine-based model to track-based model. Primary storyline → Track 0, connected clip lanes → higher tracks.

## Running

```bash
uv run server.py                    # Start MCP server
uv run --extra dev pytest tests/ -v # Run tests
```

## Pre-Commit (MANDATORY)

Before committing ANY changes, run both:
```bash
ruff check . --exclude docs/   # Lint — must pass with zero errors
pytest tests/ -v               # Tests — all must pass
```

CI also runs the suite at the declared `mcp` floor and on `mcp` 2.x. If you
touch anything in `server.py`'s handler registration, `fcpxml/mcp_compat.py`,
or a test that reaches into SDK internals, run it against 2.x locally before
pushing — the two APIs disagree in ways that pass silently on one side:

```bash
uv venv /tmp/v2 && uv pip install --python /tmp/v2/bin/python -e ".[dev]" "mcp>=2.0.0"
/tmp/v2/bin/python -m pytest tests/ -q
```
CI runs both on every push to main. If either fails, the commit gets an X on GitHub. Fix lint errors before committing, not after.

## Testing

2587 tests across 95 files (2582 pass, 5 skip on mcp 1.28.1 with Final Cut Pro, ffmpeg 9.0.1, Pillow and Playwright's Chromium present; 2553 pass / 4 skip on mcp 2.3.0 and 2549 pass / 8 skip on ffmpeg 6.1 via `scripts/test-ffmpeg6.sh`, both without Playwright or Pillow, where `test_app_labels.py` and `test_hero_assets.py` skip as modules; with `FCP_MCP_INDEX=off` 2558 pass / 29 skip, the skips being tests OF the cache; measured 2026-11-13). `test_hero_assets.py` decodes the hero MP4's first frame with ffmpeg and requires the finished cut. Mutation runs go with PYTHONDONTWRITEBYTECODE=1: a same-length mutation restored within the same second leaves a .pyc of the mutated source that Python still trusts. `test_app_labels.py` renders the app and the legacy page in headless Chromium and fails on any cut or needlessly hidden label; CI's `rendered-labels` job installs the browser and sets `FCP_MCP_REQUIRE_BROWSER=1` so it cannot skip there. `test_sequence_duration.py` holds the ripple-shortens-the-sequence fix. v0.31.0 adds `test_palette.py` (every colour in a surface file is a token of `fcpxml/palette.py`, tokens are grey/cyan/red, text pairs at 4.5:1 in both modes, the rendered app and preview:// carry only tokens, three README badges on the tokens), `test_schema_budget.py` (the advertised schema, 30,714 characters, may not grow; a shrink of more than 500 asks for the budget to be lowered), `test_front_door.py` (the old tagline, name, Omni model and card version at zero outside CHANGELOG.md; one display name), `test_pypi_readme.py` (the publish-time rewrite and the check of the BUILT description), the price table's sources and 45-day age in `test_gen.py`, and `view`'s summary agreeing with `diagnose` in `test_view_group.py`; each guard was broken in the real code once and seen red (2026-11-12). v0.30.0 adds `test_preview_audio.py`: loudness measured at chosen instants of the rendered file (each clip heard at its time, a gap silent, an audio lane from its offset, -96 dB silent, a `<video>` element silent) with a mutation check that a clip moved to 0s reads as silence where it belongs. Run `scripts/test-ffmpeg6.sh` before pushing anything that touches the filtergraph: ffmpeg 6.1 is CI's build and it has already behaved differently from 9.x once. v0.29.0 adds `test_asset_clock.py`: one 8s clip written with and without a 01:00:00:00 timecode track must give the same answer from silence detect/remove, scene detect/split, transcript edits, find_shots, find_to_timeline (in-points written on the asset clock), the view payload and gen's frame; mutation-checked (9 fail with the origin ignored). v0.28.0 adds `test_preview_truth.py`: John Palchak's three preview_render cases rebuilt in Final Cut's own shapes through the real parser and ffmpeg (a transition longer than its neighbour, a PNG still alone on the spine, lanes on a trimmed parent and on a 3600s gap), plus timecoded media, a mid-timeline gap, a clip past the end of its file, an audio-only spine clip, a missing head handle, a transformed PiP, mixed clip shapes (the "Nothing was written" error, which FAILS on 0.27.2), the lane instrument run against a render without the lane, an empty render refused, drift past a frame unverified, and the fallback ladder. Each guard was mutation-checked (2026-11-02). `test_filtergraph.py` now asserts the handle model (xfade at the transition's own start, no change to total) instead of the old overlap. v0.27.1 adds `test_live_bundle_push.py` (a `.fcpxmld` push carries its library location in the copied bundle's `Info.fcpxml` with sidecars intact, the autopush shape gets a temp copy, bundle verification reads `Info.fcpxml`, the read-back refusal and the blocked-Apple-event message, each mutation-checked). v0.27.0 adds `test_effect_registry.py` (every transition uid read against Final Cut's own `Filters.bundle` plugin list when the app is installed, with a mutation check that a fabricated uid fails it) and `test_fcp_import_refusals.py` (the #31-#34 repros verbatim: handle refusals that write nothing, the validator's handle check including an off-centre transition, rough-cut transitions DTD-valid and skipped-and-reported without media, assets copied whole with their format, anchored offsets on the parent's clock read back through the parser). v0.26.0 adds `test_timeline_app.py` (the `ui://` resource's MIME type through the real handler on both SDKs, `_meta.ui.resourceUri` on the wire, the payload round-tripping both fixtures as Fractions, marker clocks, word clipping, thumbnail bounds, no external URLs in the shell), `test_view_group.py`, `test_gen.py` (prices, the gate and its refusals, the key guards with mutation checks, both adapters against a fake transport, the probe on synthesized media, the insert on a DTD-valid fixture validated with xmllint when the DTD is present), `test_gen_group.py` (end to end with a fake provider: journal rows, redaction with its mutation check, sandboxed writes) and `test_project_guard.py` (mismatch refuses before the review gate, the sha256 footer, `analyze_timeline` and `history` report it, mutation check). v0.24.0 adds `test_gap_reporting.py` (the reporting tools and the preview on a gap-based edit, each with a spine-based control, plus the sub-frame silence floor). v0.23.0 adds `test_gap_multicam.py` (issue #23: mc-clip/caption under a gap spine, `<media>` angle resolution via `mc-source`, timeline_start against the gap's local clock, the `media_clips()` view, and that `detect_media_silence`/`detect_scenes` open the angle files). v0.20.0 adds transition and lane coverage to `test_filtergraph.py` (xfade placement, the unplaceable/missing/occupied/shortened reports, the crossfade-shifted overlay window, lane stacking order) and to `test_render.py` (real ffmpeg accepts both chains; the overlaid frame is compared against the same frame of a spine-only render, so a graph that composites nothing fails). v0.19.0 adds `test_journal.py` + `test_journal_wiring.py` (hash-checked undo, never-deletes, one ledger per folder), `test_review_gate.py`, `test_organize.py` + `test_organize_group.py` (DTD order of inserted `<keyword>`/`<rating>`, organize_auto never computes), `test_diversity_constraint.py` (with its mutation check), `test_find.py`, `test_vlm.py` (fake `mlx_vlm` records the env at import; sockets patched to raise) and `test_find_group.py` (never transcribes, never downloads, live captions stored once). v0.18.0 adds `test_index.py` (schema, invalidation on a touched source, corrupt-file rebuild, dir mode 700), `test_index_wiring.py` (second call skips ffmpeg/whisper; index off hits them every time), `test_index_group.py`, `test_progress.py` (both SDKs), `test_scenes.py` + `test_scenes_group.py` (synthesised colour-bar clips; real PySceneDetect when installed), `test_transcript_pack.py` + `test_transcript_pack_handler.py`, `test_transcribe_scribe.py` (urlopen patched throughout; the key-leak guard is mutation-checked), and `test_version.py`. Beyond the pre-existing suites, v0.17.0 added: `test_filtergraph.py` (Timeline -> ffmpeg graph, exact Fractions at NTSC rates, the Clip/.start vs ConnectedClip/.offset distinction, transition substitution reporting), `test_render.py` (proxy render plus the artifact duration read-back and its mutation check), `test_visual.py` (filmstrip+waveform from source media, silent-source fallback, and the mutation check that caught the invisible white-on-white waveform), `test_watchfolder.py` (export detection, content digesting, bundle handling), `test_bridges.py` (loopback-only probing, session caching, describe() honesty), `test_preview_group.py` and `test_watch_group.py` (MCP wiring, UNVERIFIED labelling), `test_edl_import.py` (the real video-use schema, round-tripped against the literal from their own test file), `test_autopush.py`, and `test_tool_seam.py` (the tools/ registry, the merge guard, and the server binding). Tests use `examples/sample.fcpxml` and `examples/music-video.fcpxml` as fixture data plus inline XML and synthesised ffmpeg media. Tests create temp files and clean up after.

## Releasing — the tag is not the release

A version bump that is committed and not tagged is a lie in three files. A tag
that is pushed and not verified on PyPI is a lie to every user. Five Publish
runs failed silently between v0.18.0 and v0.22.0 while the repo read as
shipped, and the packaging bug that made four consecutive releases uninstall-
able was found by a user, not by us. So the release is one unbroken sequence,
and it is not done in the middle.

1. **Bump all four version locations.** `pyproject.toml`, `server.py`
   `__version__`, `server.json` (twice) and `.claude-plugin/plugin.json`.
   `tests/test_version.py` is the only thing that catches the last two; run
   it, do not eyeball it. The counts in `server.json`'s and `plugin.json`'s
   descriptions are measured from `TOOL_GROUPS` / `TOOL_HANDLERS` by the
   same tests.
2. **Write the CHANGELOG entry before tagging**, and mark it for an editor
   mid-cut: `Added` means new operations only and is safe to take at any
   point; `Changed`/`Fixed` means the output of an existing operation moved.
   An existing operation does not change its output in a patch release. If
   behaviour has to change it takes its own minor version and says so.
3. **Run `ruff check .` as well as pytest.** CI lints and the local pytest
   run does not, so a green suite here is not a green run there. A lint-only
   failure on `main` still puts an X on the commit a user sees.
4. **Tag, push the tag, then WATCH the Publish run.** `gh run list
   --workflow=Publish`. Walking away at the push is how five failures went
   unnoticed. If it is going to fail, cancel before it uploads: a PyPI
   version number can never be reused.
5. **Verify the artifact, not the workflow's green tick.** A successful
   publish job only proves an upload happened. Install the published wheel
   into a clean venv and start the server:

   ```bash
   uv venv /tmp/rel && uv pip install --python /tmp/rel/bin/python "fcp-mcp-server==<version>"
   /tmp/rel/bin/python -c "import server, tools; print(server.__version__)"
   ```

   That import is the exact check that would have caught v0.19.2 through
   v0.21.0, where every install succeeded and every server then refused to
   start. Serving a file is not the same as the file working. Run it from
   outside this directory: inside the repo, `import server` finds the working
   copy and passes no matter what PyPI holds.

   When checking whether PyPI has the version, do NOT read
   `https://pypi.org/pypi/fcp-mcp-server/json`. That aggregate endpoint is
   CDN-cached and reported 0.24.0 as latest for minutes after 0.24.1's files
   were live. Read `https://pypi.org/simple/fcp-mcp-server/`, or the
   version-specific `.../pypi/fcp-mcp-server/0.24.1/json`, both of which are
   keyed per version. A stale reader that says "not published" will send you
   chasing a publish that already worked.
6. **Look at the PyPI page.** The publish job pins the README's relative
   images to the tag (`scripts/pypi_readme.py rewrite`) and refuses an upload
   whose BUILT description still holds a relative path or an image that does
   not answer 200 (`check --fetch`). Open the version's page anyway, at
   desktop and phone width, and see the hero.
7. **The front door is generated, never hand-edited.** `docs/assets/social-card.png`
   comes from `scripts/social_card.py` (no version, no star count: a number
   in an image goes stale on upload); `logo.svg`/`logo-400.png` from
   `scripts/logo.py`; the hero, its still and the card frame (conversation beside timeline) from
   `demo/hero/build.py`. GitHub has no API to set the card: when it changes,
   upload it in Settings -> Social preview, then `scripts/check_social_preview.py`
   must exit 0. A release with no Claude Desktop capture says "not verified in
   Claude Desktop" (`scripts/capture-claude-desktop.md`).

## FCPXML Gotchas

- FCPXML uses rational time everywhere: `"3600/2400s"` = 1.5 seconds
- `offset` in clips is the timeline position, `start` is the source media in-point
- Library clips (`<asset-clip>`) are different from timeline clips (`<clip>`)
- Markers are children of clips, not siblings
- A marker's `start` is in its HOST's local time, which begins at the host's `start` (source in-point), not at 0. Timeline position = `host.offset + (marker.start - host.start)`. The parser resolves this into `Marker.timeline_start`; read `Marker.position` for anything shown or compared, and let `add_marker_at_timeline` do the arithmetic when writing. Until 0.19.3 the writer dropped the `host.start` term and every marker on a trimmed clip landed early by the in-point
- The `<spine>` element is the primary storyline — clips go here
- `.fcpxmld` bundles are DIRECTORIES wrapping `Info.fcpxml` + sidecar data files — sidecars must be copied on save or object-tracking/Cinematic data is destroyed
- `examples/sample.fcpxml` is NOT DTD-conformant (pre-`media-rep` assets, sequence-level chapter markers) — don't use it as a DTD-validity fixture
- An anchored (connected) item's `offset` is in its PARENT's local time, which begins at the parent's `start`, exactly like a marker's. Writers add the parent's `start` (`FCPXMLModifier._anchored_offset`); until 0.27.0 `add_connected_clip`/`add_audio_clip` did not (#34)
- A `<transition>` moves no offsets: it draws on media OUTSIDE both clips' used ranges (the incoming clip's head, the outgoing clip's tail). Final Cut refuses the whole import when either is short, and the DTD cannot see it. Every transition writer goes through `_transition_shortfalls`; `validate_fcpxml` checks it too (#31)
- Effect uids are what Final Cut writes: `FxPlug:<UUID>` for FxPlug effects. A transition uid is only trusted when it appears as an `FxTransition` in `Final Cut Pro.app/.../Filters.bundle/Contents/Info.plist`; `tests/test_effect_registry.py` reads it. The pre-0.27 table had seven UUIDs that matched nothing and a "fade" that was a colour look (#30)
- A clip's `start` is on its ASSET's clock, which begins at `<asset start>` (a camera's timecode: 3600s for 01:00:00:00). The FILE position is `start - asset.start`. Anything that COMPARES a source time with media (ffmpeg, Whisper, PySceneDetect, captions) uses `media_intel.file_in_point(clip)` (or `asset_origin_seconds` on writer elements); anything WRITTEN back to FCPXML or shown beside Final Cut's source timecode goes back on the asset clock (find's `_Ctx.origin`). Until 0.28 the renderer seeked 3602s into an 8s file; until 0.29 silence, scenes, transcripts and find reported "nothing found" on every timecoded clip
- A still is an asset with `duration="0s"` (format `FFVideoFormatRateUndefined`, often no size) played by a `<video start="3600s">`. It has no clock: loop it, never seek it
- A `<transition>`'s own `offset` is where it starts; it is usually `cut - duration/2` but need not be. The parser keeps `start` = the cut and records `lead` (time before the cut). The parsed `transition_type` is `""` — the NAME says what it is
- Spine `<title>`/`<audio>`/`<audition>`/`<caption>` take primary-storyline time even though they are not timeline clips; the parser advances past them (until 0.28 it did not, and every later clip was early)
- ffmpeg `concat` and `xfade` refuse inputs of different sizes, and since ffmpeg 7 that surfaces as "Nothing was written into output file, because at least one of its streams received no packets" rather than a config error. Never scale sources independently; overlay each onto one canvas
- ffmpeg 6.1 (Ubuntu's apt build, what CI runs) silently drops an `overlay` whose secondary stream starts in the future (`setpts=...+N/TB` plus an `enable` window) when it is chained after another overlay; 9.x draws it. Keep every overlay's inputs in step from frame 0 (transparent padding). The local 9.x suite cannot see this; CI can
- `<video ref>` is the PICTURE component of an asset and `<audio ref>` the sound; a `<video>` element's sound is not in the cut. The DTD leaves `hasAudio`/`hasVideo` `#IMPLIED`: Final Cut writes `hasAudio="1"` when there is sound, so the renderer reads absence as none (`declaresAudio`), while the older `hasAudio` resource field still defaults to true for the tools that read it
- `adjust-transform` position is in percent of the FRAME HEIGHT from the centre, y up (the 2x2 split-screen is +-44.4444 +-25 on 16:9); scale applies after the conform. Apple's DTD does not state the units; this reading is from an FCP 12.4 hand check (storytold/filmcraft#2639) and matches the split-screen values
- `push_to_fcp` ALWAYS sends an `<import-options>`-injected copy, never the original: `<name>_import.fcpxml`, or for a bundle `<name>_import.fcpxmld` (copied whole, sidecars included, options in its `Info.fcpxml`); no copy path means a temp copy. A document without a `library location` makes Final Cut 12.3 raise a library picker that blocks the Apple event (bundle) or import nothing (flat). Until 0.27.1 both bundles and autopush were sent bare
