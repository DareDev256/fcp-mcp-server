# CLAUDE.md — fcp-mcp-server

## What This Is

MCP server that reads/writes Final Cut Pro XML (FCPXML) files. 7 grouped tools (`inspect`, `diagnose`, `edit`, `mark`, `generate`, `transcript`, `deliver`) are advertised by default, dispatching into 62 underlying operations for timeline analysis, batch editing, QC, generation, multi-track support, media relink, NLE export, transcript-based editing (local Whisper), and LIVE FCP control (push_to_fcp / list_fcp_libraries via Apple events). Set `FCP_MCP_LEGACY_TOOLS=1` to advertise the original 62 flat tools alongside the groups (additive — 69 advertised tools, not 62). Reads FCPXML 1.8–1.14 (incl. `.fcpxmld` bundles with sidecar preservation), writes 1.13 by default. Dual-mode (XML + Live) direction: `docs/CAPABILITY-AUDIT-2026-06.md`.

## Architecture

```
server.py           — MCP server entry point. All 62 tool definitions, handlers, resources, prompts.
                      Dispatch dict pattern: TOOL_HANDLERS maps tool names → async handler functions.
TOOL_GROUPS         — 7 grouped verbs advertised by default. Dispatch into
                      TOOL_HANDLERS, which still holds all 62 flat handlers.
                      Hiding a tool from list_tools does NOT stop it dispatching.
fcpxml/preview.py   — standalone HTML timeline render, served as preview://<path>
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

1343 tests across 29 files (1337 pass, 6 skip without ffmpeg/FCP present; on mcp 2.x it is 1339 pass / 4 skip — two compat tests are 2.x-only). `test_models.py` covers TimeValue arithmetic, Timecode parsing/formatting, Clip properties, validation models, and Timeline helpers. `test_writer.py` covers insert_clip, add_marker (all types), trim_clip, delete_clip, split_clip, and change_speed operations. `test_server.py` covers MCP tool handlers, parsers, and dispatch. `test_rough_cut.py` covers RoughCutGenerator. `test_features_v05.py` covers connected clips, roles, timeline diff, reformat, silence detection, export, and backward compatibility. `test_marker_pipeline.py` covers build_marker_element shared builder, batch auto-modes, clip index duplicate-name behavior, and write_fcpxml output format. `test_refactored_helpers.py` covers _index_elements, _iter_spine_clips, _find_spine_clip_at_seconds, _resolve_clip_duration, _make_asset_clip, _format_batch_result, and serialize_xml edge cases. `test_transcribe.py` covers phrase/filler span matching, range merge/invert algebra, whisper graceful degradation, and transcript-driven handler cuts against cached transcripts. `test_media_intel.py` covers silencedetect stderr parsing, source-to-timeline mapping, parameter bounds, and real-WAV ffmpeg integration (skips without ffmpeg; CI installs it). `test_broadcast_rates.py` covers the NTSC-fractional rates end to end (rational_fps snapping, from_timecode/to_timecode/snap_to_frame/change_speed at 23.98 and 29.97, one-frame durations, conform-rate enum, XMEML timebase+ntsc, frame-rate display) — every test in it fails against the pre-#17 `int(fps)` implementation. `test_mcp_compat.py` covers handler registration on both SDK generations and the snake_case field-alias accessors. `test_tool_groups.py` covers the 7 TOOL_GROUPS dispatching to the 62 TOOL_HANDLERS, legacy-flag opt-in, and the schema-size reduction. `test_preview.py` covers the `preview://` HTML timeline render. `test_skill.py` covers the `final-cut-pro` skill's structure. Tests use `examples/sample.fcpxml` as fixture data and inline XML fixtures. Tests create temp files and clean up after.

## FCPXML Gotchas

- FCPXML uses rational time everywhere: `"3600/2400s"` = 1.5 seconds
- `offset` in clips is the timeline position, `start` is the source media in-point
- Library clips (`<asset-clip>`) are different from timeline clips (`<clip>`)
- Markers are children of clips, not siblings
- The `<spine>` element is the primary storyline — clips go here
- `.fcpxmld` bundles are DIRECTORIES wrapping `Info.fcpxml` + sidecar data files — sidecars must be copied on save or object-tracking/Cinematic data is destroyed
- `examples/sample.fcpxml` is NOT DTD-conformant (pre-`media-rep` assets, sequence-level chapter markers) — don't use it as a DTD-validity fixture
