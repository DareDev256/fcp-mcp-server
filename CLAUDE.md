# CLAUDE.md — fcp-mcp-server

## What This Is

MCP server that reads/writes Final Cut Pro XML (FCPXML) files. 13 grouped tools (`inspect`, `diagnose`, `edit`, `mark`, `generate`, `transcript`, `deliver`, `preview`, `watch`, `index`, `scenes`, `organize`, `find`) are advertised by default, dispatching into 88 underlying operations for timeline analysis, batch editing, QC, generation, multi-track support, media relink, NLE export, transcript-based editing (local Whisper, or ElevenLabs Scribe opt-in for speakers), shot-boundary detection, bulk library organization with a journal-backed `undo`, tiered shot search (transcript → metadata → offline MLX captions), a review gate on `deliver`, and LIVE FCP control (push_to_fcp / list_fcp_libraries via Apple events). Set `FCP_MCP_LEGACY_TOOLS=1` to also advertise the 63 flat tool schemas (76 advertised in total; the 25 operations born as group actions — preview, watch, index, scenes, organize, find, import_edl_json — have no flat schema). Reads FCPXML 1.8–1.14 (incl. `.fcpxmld` bundles with sidecar preservation), writes 1.13 by default. Dual-mode (XML + Live) direction: `docs/CAPABILITY-AUDIT-2026-06.md`.

## Architecture

```
server.py           — MCP server entry point. 63 flat tool definitions, handlers, resources, prompts.
                      Dispatch dict pattern: TOOL_HANDLERS maps tool names → async handler functions.
TOOL_GROUPS         — 13 grouped verbs advertised by default. Dispatch into
                      TOOL_HANDLERS, which holds all 88 handlers. New groups are
                      defined in tools/ and merged in by _merge_extra_tools().
                      Hiding a tool from list_tools does NOT stop it dispatching.
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
fcpxml/preview.py   — standalone HTML timeline render, served as preview://<path>.
                      Draws from the XML: shows what was WRITTEN. It cannot tell a
                      fixed cut from a broken one — use fcpxml/visual.py for that.
fcpxml/filtergraph.py — Timeline -> ffmpeg graph. PURE (no subprocess, no I/O), so
                      compilation is asserted exactly on machines without ffmpeg.
                      Fraction end to end; float seconds drift visibly at 23.976.
                      Spine Clip carries timeline position on .start; ConnectedClip
                      carries it on .offset and reuses .start for the source in-point.
                      Transitions compile onto the spine boundary they straddle
                      (xfade); one with no cut within its own duration, or with
                      media missing beside it, is REPORTED as a hard cut, never
                      moved. Video lanes composite as a full-frame overlay shifted
                      by any crossfade before them — transforms and opacity are
                      not read, and it says so rather than previewing a lie.
fcpxml/render.py    — Executes the graph. Probes the artifact's OWN duration back and
                      reports drift — a rendered file existing is not evidence.
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
                      templates and relink. server.py RE-EXPORTS them under their
                      original names, so TOOL_HANDLERS and every caller resolve one
                      definition. Moved handlers reach server-owned names through
                      srv = _common.tools.server_module() rather than importing them:
                      tests monkeypatch those on the server module, and an import
                      would bind a copy no patch can reach — the guard would keep
                      passing while guarding nothing.
skill/              — the final-cut-pro Claude Code skill wrapping this server

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

1747 tests across 65 files (1740 pass, 7 skip without ffmpeg/FCP/PySceneDetect present; on mcp 2.x it is 1741 pass / 6 skip; with `FCP_MCP_INDEX=off` it is 1716 pass / 31 skip — the skips being tests OF the cache). v0.20.0 adds transition and lane coverage to `test_filtergraph.py` (xfade placement, the unplaceable/missing/occupied/shortened reports, the crossfade-shifted overlay window, lane stacking order) and to `test_render.py` (real ffmpeg accepts both chains; the overlaid frame is compared against the same frame of a spine-only render, so a graph that composites nothing fails). v0.19.0 adds `test_journal.py` + `test_journal_wiring.py` (hash-checked undo, never-deletes, one ledger per folder), `test_review_gate.py`, `test_organize.py` + `test_organize_group.py` (DTD order of inserted `<keyword>`/`<rating>`, organize_auto never computes), `test_diversity_constraint.py` (with its mutation check), `test_find.py`, `test_vlm.py` (fake `mlx_vlm` records the env at import; sockets patched to raise) and `test_find_group.py` (never transcribes, never downloads, live captions stored once). v0.18.0 adds `test_index.py` (schema, invalidation on a touched source, corrupt-file rebuild, dir mode 700), `test_index_wiring.py` (second call skips ffmpeg/whisper; index off hits them every time), `test_index_group.py`, `test_progress.py` (both SDKs), `test_scenes.py` + `test_scenes_group.py` (synthesised colour-bar clips; real PySceneDetect when installed), `test_transcript_pack.py` + `test_transcript_pack_handler.py`, `test_transcribe_scribe.py` (urlopen patched throughout; the key-leak guard is mutation-checked), and `test_version.py`. Beyond the pre-existing suites, v0.17.0 added: `test_filtergraph.py` (Timeline -> ffmpeg graph, exact Fractions at NTSC rates, the Clip/.start vs ConnectedClip/.offset distinction, transition substitution reporting), `test_render.py` (proxy render plus the artifact duration read-back and its mutation check), `test_visual.py` (filmstrip+waveform from source media, silent-source fallback, and the mutation check that caught the invisible white-on-white waveform), `test_watchfolder.py` (export detection, content digesting, bundle handling), `test_bridges.py` (loopback-only probing, session caching, describe() honesty), `test_preview_group.py` and `test_watch_group.py` (MCP wiring, UNVERIFIED labelling), `test_edl_import.py` (the real video-use schema, round-tripped against the literal from their own test file), `test_autopush.py`, and `test_tool_seam.py` (the tools/ registry, the merge guard, and the server binding). Tests use `examples/sample.fcpxml` and `examples/music-video.fcpxml` as fixture data plus inline XML and synthesised ffmpeg media. Tests create temp files and clean up after.

## FCPXML Gotchas

- FCPXML uses rational time everywhere: `"3600/2400s"` = 1.5 seconds
- `offset` in clips is the timeline position, `start` is the source media in-point
- Library clips (`<asset-clip>`) are different from timeline clips (`<clip>`)
- Markers are children of clips, not siblings
- A marker's `start` is in its HOST's local time, which begins at the host's `start` (source in-point), not at 0. Timeline position = `host.offset + (marker.start - host.start)`. The parser resolves this into `Marker.timeline_start`; read `Marker.position` for anything shown or compared, and let `add_marker_at_timeline` do the arithmetic when writing. Until 0.19.3 the writer dropped the `host.start` term and every marker on a trimmed clip landed early by the in-point
- The `<spine>` element is the primary storyline — clips go here
- `.fcpxmld` bundles are DIRECTORIES wrapping `Info.fcpxml` + sidecar data files — sidecars must be copied on save or object-tracking/Cinematic data is destroyed
- `examples/sample.fcpxml` is NOT DTD-conformant (pre-`media-rep` assets, sequence-level chapter markers) — don't use it as a DTD-validity fixture
