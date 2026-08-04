# Changelog

## [Unreleased]

## [0.14.5] - 2026-08-04

### Fixed

**The preview header read "0 clips" on a timeline holding 129 of them.**
`Timeline.total_clips` counts spine clips only, so every connected-clip project
reported zero. It now reports the real total plus how many lanes they span.

**An empty spine rendered as a grey box** taking a third of the frame on any
music video, where nothing sits on the spine by design. The row is now omitted
when there is nothing to draw.

**Frame rate printed as `23.976023976023978fps`.** Rounded to two places.

All three showed on every connected-clip project and were invisible against
`examples/sample.fcpxml`. Found while rendering the README screenshot.

## [0.14.4] - 2026-08-04

### Added

**`examples/music-video.fcpxml` — a fixture shaped like real work.**

`examples/sample.fcpxml` is spine-based, starts at 0s, and has no connected
clips. That shape hid three separate bugs in a single afternoon, every one of
them found only by pointing the tool at an actual project rather than at the
test suite.

The new fixture reproduces all three conditions on purpose: a `tcStart` of `0s`
disagreeing with element offsets that begin at 3600s, a 23.98 sequence format
followed in the file by a 50p source format, and a spine holding one `<gap>`
with all eight clips connected across lanes -1, 1 and 2.

Verified by reintroducing both fixed bugs against it: forcing the origin to 0
pins every clip to `left:100%`, and restoring last-format-wins reports 50.0 fps.
Neither is detectable with `sample.fcpxml`.

Guard tests assert the fixture keeps those properties, so it cannot quietly
drift back toward the shape that hid the bugs. Issue #16 — edit handlers
no-opping on connected-clip timelines — now has something to be fixed against.

## [0.14.3] - 2026-08-04

### Fixed

**Frame rate was read from whichever `<format>` came last in the file, not from
the sequence.** A 3840x2160 timeline at 23.98 fps, in a project holding one 25p
drone clip, parsed as 50.0 fps.

The wrong header was the visible symptom. The real problem is that
`Timecode.frame_rate` drives `total_frames` and `to_smpte()`, so every
seconds-to-frames conversion was off by that factor: 164 seconds resolved to
8200 frames instead of 3932. Anything placing a cut or a marker on a specific
frame — snapping to a beat, importing beat markers, reporting a timecode — was
computing against a rate the timeline never had.

The sequence's `format` attribute is now resolved to its `<format>` element and
that rate wins, applied before any `Timecode` is built. A format that cannot be
resolved falls back to the first one declared, which is conventionally the
sequence's own.

Closes #15.

## [0.14.2] - 2026-08-04

### Fixed

**The timeline preview rendered every real Final Cut Pro project as a single
stripe.** Final Cut starts sequences at 01:00:00:00 by broadcast convention, so
element offsets in an exported project routinely begin at 3600s — a 164-second
timeline whose clips sit between 3600s and 3743s. The renderer assumed an origin
of 0, computed `left: 2195%` for the first block, and clamped all of them to the
right edge.

`examples/sample.fcpxml` starts at 0, which is the only reason the whole 0.14.0
test suite passed. It surfaced on the first real project: 129 connected clips,
all pinned to 100%.

The origin is now derived from the earliest element rather than assumed, which
handles a 0-based sequence, an hour-offset one, and anything else. It does not
read `tcStart` — that attribute reads `0s` on real projects whose clips
nonetheless start at 3600s.

## [0.14.1] - 2026-08-04

### Fixed

**A missing or misnamed argument now says which one, instead of `Error: KeyError`.**
Grouped calls nest parameters under `args`, so an action's required fields are
no longer visible in the advertised schema and the caller has to guess them.
Most handlers take `filepath`, but a few do not — the beat tools take
`media_path` — and guessing wrong returned a bare `Error: KeyError` with no key
name and nothing to correct. That dead end did not exist with the flat schema,
so it was a regression introduced by the grouping in 0.14.0.

`call_tool` now catches `KeyError` and answers with the missing parameter plus
the action's full accepted-parameter list, pulled from its original tool schema:

```
Missing required argument: media_path

'detect_beats' accepts:
  media_path (required): Path to audio/video file (.wav, .mp3, .m4a, ...)
```

Applies to flat calls as well as grouped ones.

## [0.14.0] - 2026-08-04

### Added

**Seven grouped tools replace 62 flat ones in the advertised tool list.**
`inspect`, `diagnose`, `edit`, `mark`, `generate`, `transcript`, `deliver`.
Each takes `{"action": "...", "args": {...}}` and dispatches into exactly the
same 62 handlers as before, so behaviour is unchanged. The tool schema
injected into every conversation drops from 34,409 characters (what actually
shipped, the flat 62-tool list) to 5,258 characters for the 7 groups — an
84.7% reduction — before the user types anything.

**Nothing breaks.** `call_tool` resolves handlers from `TOOL_HANDLERS`, a
registry independent of what `list_tools` advertises, so an existing config
calling `trim_clip` by name keeps working. Set `FCP_MCP_LEGACY_TOOLS=1` to
advertise the original 62 alongside the 7 groups. They will not be removed
before 1.0.

**HTML timeline preview.** Reading `preview://<path>` returns a self-contained
HTML render of the timeline: proportional clip blocks sized to duration,
connected clips shown on their own lane rows, marker ticks, all values
HTML-escaped, served as `text/html`. Editing FCPXML was previously blind, with
no way to see a cut short of importing it into Final Cut Pro.

**A `final-cut-pro` Claude Code skill** in `skill/`, wrapping the server with
workflow order and the FCPXML gotchas that tool descriptions have no room for.
Closes the design question raised in #2.

**Daily scheduled CI.** The mcp 2.0 break (see 0.13.2 below) went unnoticed
for a week because main's last run predated it by a day. The suite now also
runs on a `schedule` trigger at 06:00 UTC daily, plus `workflow_dispatch` for
manual runs, in addition to the existing push/PR triggers.

**Minimum-dependency CI job.** Every existing job installs whatever `mcp` is
latest, so a declared lower bound was never actually exercised. A new
`minimum-mcp` job on Python 3.12 installs the declared floor (`mcp==1.3.0`)
and runs the full suite.

### Fixed

**The declared `mcp` floor was wrong and would have broken installs.**
`server.py` imports `mcp.server.lowlevel.helper_types.ReadResourceContents`,
which did not exist before mcp 1.3.0 — verified: on mcp 1.2.1 the server dies
at import with `ModuleNotFoundError` and the MCP client reports only
"disconnected", with no traceback surfaced. The pin declared `mcp>=1.0.0` in
both `pyproject.toml` and `requirements.txt`. Raised to `mcp>=1.3.0,<2.0.0`
in both, with the reason recorded inline. No `try`/`except` fallback: losing
`ReadResourceContents` means losing the `text/html` mime type, which is the
whole point of the preview resource, so it must fail loudly rather than
silently degrade.

**`preview://` and `file://` failed on any path containing a space.** Both
branches stripped the scheme with a global `str.replace()` and never
unquoted. `list_resources` emits pydantic-normalized URIs, so
`My Project.fcpxml` came back as `My%20Project.fcpxml` and the read failed
with "File not found" — and filenames with spaces are the norm in `~/Movies`.
Both branches now go through `_uri_to_path()`, which strips the scheme with
`removeprefix` (leading match only, so a path containing the literal scheme
string is no longer mangled) and `urllib.parse.unquote()`s before validation.

**The five MCP prompts and the workflow docs named tools the model can no
longer see.** They instructed the model to call `validate_timeline`,
`fix_flash_frames`, `auto_rough_cut` and 19 other flat names that
`list_tools` stopped advertising in this release. The flat names still
dispatch, so nothing was broken — but it taught the model to reach for tools
absent from its tool list, which is what this release exists to stop. All
five prompts, the README prompts table, and `docs/WORKFLOWS.md` now use the
grouped form. A test fails if any prompt names an action that is not
reachable from the group it tells the model to use.

**Three false and two aspirational rows in the README security matrix.** The
"URI parsing" row claimed URIs were parsed via `urllib.parse.urlparse()` —
there is no `urlparse` anywhere in `server.py`, and the percent-encoding bug
above proves the encoding half was not handled either. Rewritten to describe
what the code actually does, and backed by tests. Spot-checking the rest of
the matrix found four more unbacked claims: "10K file cap on `rglob`" and
"symlink files skipped during discovery" (neither exists in
`find_fcpxml_files`), a 10,000-entry marker batch cap, and a ~1 MB inline
transcript cap (neither implemented). The false claims are gone; the genuine
symlink protection that does exist — `Path.resolve()` running before the
extension whitelist — is now documented and tested in its place.

**The test suite could not run against the bottom of the declared range.**
`tests/test_preview.py` built `ReadResourceRequest` without `method=`, which
only validates on newer mcp; on 1.3.0 both preview-resource tests failed with
a pydantic `ValidationError` while the product worked fine.
`tests/test_security.py` could not be collected on its own at all, because
its `mcp` shim did not cover `mcp.server.lowlevel.helper_types`. Both fixed.

### Added (tests)

`preview://` shipped with no committed security coverage. Added, driven
through the real resource read path: relative traversal, percent-encoded
traversal, absolute paths outside the project, non-FCPXML extensions, raw and
percent-encoded null bytes, and a symlink whose resolved target has a
disallowed extension — plus the same rejection set re-asserted for `file://`,
and spaces-in-filename round trips from `list_resources` through
`read_resource` for both schemes.

### Changed

`pyproject.toml`'s PyPI description said "62 tools"; it now matches
`server.json` and the README at "7 grouped tools (62 underlying operations)".
`CLAUDE.md` said `FCP_MCP_LEGACY_TOOLS=1` advertises the flat tools
"instead" of the groups — it is additive, 69 advertised tools, as the README
already said correctly.

## [0.13.2] - 2026-08-04

### Fixed

**Pinned `mcp<2.0.0` — every fresh install had been broken since 2026-07-28.**
`mcp` 2.0.0 landed on PyPI that day and removed the low-level `Server` decorator
API that `server.py` is built on: `list_tools`, `call_tool`, `list_resources`,
`read_resource`, `list_prompts`, and `get_prompt` are all gone, replaced by
`on_*` constructor callbacks. Because the dependency was declared as an
open-ended `mcp>=1.0.0`, every install after that date resolved to 2.0.0 and
`server.py` failed at import with `AttributeError: 'Server' object has no
attribute 'list_resources'`, taking six test modules down at collection time.

Main's last CI run was 2026-07-27, one day before the release, so the breakage
first surfaced on an unrelated contributor PR and looked like that PR's fault. It
was not. The bound resolves to `mcp` 1.29.0; full suite green.

Migrating to the 2.x callback API is tracked separately — the pin is the stopgap,
not the answer.

**`detect_media_silence` no longer decodes the video stream.** `detect_silence()`
ran ffmpeg's `silencedetect` filter with no stream selection, so ffmpeg decoded
the entire video track into the null muxer just to analyse audio. On long or
high-bitrate camera files that blew past `PROBE_TIMEOUT_SECONDS` (120s) and
silence analysis returned nothing — a 2.5GB, 971-second iPhone MOV timed out
every time. Adding `-vn` restricts the pass to the audio stream; the same file
now analyses in about a second with byte-identical `silencedetect` output, since
the filter only ever looked at audio. Thanks to
[@jardelapp](https://github.com/jardelapp) for the report and the fix (#8).

### Changed

**Repo renamed `fcpxml-mcp-server` → `fcp-mcp-server`** to match the PyPI
distribution name. The GitHub *About* link had been pointing at
`pypi.org/project/fcpxml-mcp-server/` — a slug that never existed on PyPI — so
every visitor who clicked it got a 404 while `uvx fcp-mcp-server` worked fine.
Homepage now points at the real package; clone URLs, CI badge, and
`[project.urls]` follow the new slug. GitHub redirects the old slug, so existing
clones, forks, and links keep working.

The MCP registry identity stays `io.github.DareDev256/fcpxml-mcp-server`,
unchanged — it is bound to the `mcp-name` marker inside the *published* 0.13.1
PyPI README, and changing it would orphan the registry entry and require a new
PyPI release. Registry name ≠ install name is legal and intentional.

The rename itself carried no code changes.

## [0.13.1] - 2026-07-24

Registry release. Adds the `mcp-name` ownership marker to the README (required
by the official MCP registry to bind the PyPI package to
`io.github.DareDev256/fcpxml-mcp-server`) and trims `server.json`'s description
to the registry's 100-char limit. No code changes.

## [0.13.0] - 2026-07-23

**Transcript Intelligence** — text-based editing lands. 59 → 62 tools.

Apple put FCP's AI (Transcript Search, Generate Captions) behind the Creator
Studio subscription; this release brings the agentic version to everyone, free,
via local Whisper — and goes further: the transcript doesn't just *search*, it
*cuts*.

### Added
- **`transcribe_media`** — transcribes each clip's source media locally with
  word-level timestamps (faster-whisper, new optional `[transcribe]` extra).
  Writes a `_transcript.json` next to each media file — transcription is a
  one-time cost, reused by every transcript tool. Optional `write_srt` emits
  an SRT that plugs straight into `import_srt_markers`.
- **`edit_by_transcript`** — cut timeline content by what was SAID.
  `mode=remove` cuts every occurrence of the given phrases with ripple;
  `mode=keep_only` keeps only the matched phrases (clips with no matches are
  left untouched — never deletes a clip because nothing matched). Matching is
  case/punctuation-insensitive. Non-destructive `_transcript_edit` copy.
- **`remove_filler_words`** — cuts um/uh/erm out of the timeline with ripple
  using word-level timestamps from the real source audio. The default filler
  list is deliberately conservative: words like "like" and "so" are speech,
  not noise, and are only cut when passed explicitly.
- New `fcpxml/transcribe.py` module: pure, dependency-free matching helpers
  (`find_phrase_spans`, `find_filler_spans`, `merge_ranges`, `invert_ranges`,
  `segments_to_srt`) + the faster-whisper integration behind the same
  graceful-degradation contract as ffmpeg/librosa (returns `None` → tools
  answer with an install hint, never a crash).
- 42 new tests (1032 total): span matching, range algebra, keep_only inversion
  edge cases, degradation without faster-whisper, and full handler integration
  against cached transcripts (head-trim vs split behavior, SRT output,
  transcription caps, missing-media reporting).

### Notes
- Whisper model names are allowlist-validated (they resolve to downloads).
- Per-call transcription is capped at 10 distinct media files; cached
  transcripts don't count against the cap.

## [0.12.2] - 2026-07-23

Distribution release — the server is now on PyPI. No tool changes.

### Added
- **Published to PyPI as [`fcp-mcp-server`](https://pypi.org/project/fcp-mcp-server/).**
  `uvx fcp-mcp-server` now works, which unbreaks the install path that `server.json`
  (official MCP registry) and `smithery.yaml` have been advertising, and makes the
  `[intelligence]` extra installable without cloning.
- **Claude Code install path** in the README (`claude mcp add` one-liner + project-scoped
  `.mcp.json` example) alongside the existing Claude Desktop instructions.
- **"How It Compares" section** — honest trade-off table vs SpliceKit (runtime patching)
  and CommandPost (accessibility scripting): raw live power there, no-patch portability,
  managed-Mac compatibility, and works-without-FCP here.
- Security posture surfaced at the top of the README (132 adversarial-input tests,
  defusedxml, sandboxed writes, disclosure channel).

### Fixed
- **Packaging: `server.py` was missing from the wheel.** `[tool.setuptools]` only
  included the `fcpxml*` and `tools*` packages, so a built wheel had no entry-point
  module and `fcp-mcp-server` failed to launch. Added `py-modules = ["server"]`,
  dropped the empty `tools` stub package from the distribution, and verified the
  wheel end-to-end in a clean venv (MCP initialize handshake answers correctly).
- **Server now reports its own version** over MCP (`serverInfo.version` said `1.28.1` —
  the SDK's version — instead of the package's).
- `server.json` refreshed: version 0.9.0 → 0.12.2, tool count 56 → 59, description
  includes media intelligence.
- `pyproject.toml` URLs point at the canonical repo (`fcpxml-mcp-server`), and a
  Changelog URL was added.

## [0.12.1] - 2026-07-16

Docs fix. No code changes.

### Fixed
- **`[intelligence]` install command didn't work.** The README told users to run
  `pip install 'fcp-mcp-server[intelligence]'` to enable `detect_beats`, but the
  package isn't published to PyPI — in a clean venv that errors with
  *"No matching distribution found for fcp-mcp-server[intelligence]"*. Since the
  documented install flow is `git clone` + `pip install -e .`, the extra is now
  `pip install -e '.[intelligence]'`, with a note that it must run from the cloned
  repo. This blocked the headline feature of v0.12.0.
- **Clone URL and paths use the canonical repo name.** `git clone .../fcp-mcp-server.git`
  still resolves via GitHub's rename redirect, but it created a directory whose name
  didn't match the `cd` on the next line, and the `/path/to/fcp-mcp-server` placeholders
  in the Claude Desktop config didn't match what `git clone` produces. (The *package*
  name in `pyproject.toml` is legitimately `fcp-mcp-server` — only the repo is
  `fcpxml-mcp-server`. Left alone.)

### Verified unchanged
Audited every headline claim against the source — all accurate, nothing to correct:
59 tools (all 59 documented, 0 undocumented), 990 tests (pytest collects exactly 990),
23 suites, v0.12.0 consistent across pyproject/CHANGELOG/README.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.0] - 2026-07-09

### Added — Media Intelligence slice 3: beat detection

- **`detect_beats` (59th tool)** — detects musical beats and tempo in an audio/video file via librosa's beat tracker and writes a beats JSON next to the media file in exactly the format `import_beat_markers` consumes, so *detect → mark → snap-to-beats* chains with zero glue. Analysis duration capped (20 min) to bound memory; media path validated against an audio/video extension whitelist.
- **`[intelligence]` optional extra** — `pip install 'fcp-mcp-server[intelligence]'` adds librosa. The core install stays 2 dependencies; without the extra, `detect_beats` degrades to an install hint (lazy import, never crashes). CI installs it so beat tests run on every push.

### Fixed

- **`import_beat_markers` no longer crashes when beats run past the timeline end** — songs are routinely longer than edits; out-of-range beats are now skipped and counted in the report instead of raising `No spine clip at position`. Found by end-to-end verification of the detect → import → snap chain.

Tests: 984 → 990.

## [0.11.0] - 2026-07-09

### Added — Media Intelligence slice 2: silence auto-removal

- **`remove_media_silence` (58th tool)** — detects real silence in each clip's source audio (same ffmpeg analysis as `detect_media_silence`) and **cuts it out of the timeline with ripple**: clips are split around silence, silent middles removed, everything after shifts earlier. `padding` (default 0.05s) keeps a breath of silence on each side of every cut; cut boundaries snap to the frame grid in the 2400-tick timebase. Non-destructive — writes a `_silence_removed` copy, and writes nothing at all when no silence is found.
- **`FCPXMLModifier.cut_clip_ranges`** — new element-based writer primitive: removes clip-relative time ranges from a spine clip (merging overlapping ranges, clamping out-of-bounds), rebuilds the clip as its kept segments with correct source in-points, filters markers/keywords per segment, and ripples subsequent clips. Element-based on purpose — immune to the duplicate-name ambiguity that name-keyed `delete_clip`/`split_clip` composition would hit when cutting a clip into same-named segments.

Tests: 976 → 984.

## [0.10.0] - 2026-07-09

### Added — Media Intelligence v1 (the moat work begins)

First slice of the v0.10 media-intelligence roadmap: the server now analyzes the **actual media files** a timeline references, not just the XML.

- **`detect_media_silence` (57th tool)** — probes each clip's source audio with ffmpeg's `silencedetect` filter and maps silence ranges from source time into **timeline time**, reporting per-clip silence spans with a cut plan. Unlike `detect_silence_candidates` (XML-only heuristics: gaps, name patterns), this hears the audio. Supports `noise_db` threshold (−120..0 dB), `min_silence` duration, and per-clip filtering; media files are probed once and cached across clips that share them; missing/unreadable media is reported per clip, never fatal.
- **`fcpxml/media_intel.py`** — new module for real media analysis. Zero new Python dependencies: ffmpeg runs as a bounded subprocess (list-form args, validated numeric parameters, 120s hard timeout, 100-file probe cap) and everything degrades gracefully — no ffmpeg means "unanalyzable", not a crash.
- **CI now installs ffmpeg** so the real-WAV integration tests (tone/silence/tone fixtures generated with the stdlib `wave` module) run on every push; they skip automatically on machines without ffmpeg.

Tests: 958 → 976 across 23 suites.

## [0.9.1] - 2026-07-09

### Security

- **`apply_template` write sandbox bypass fixed** — the one generation handler that built a timeline from scratch (no input file to anchor against) called `_validate_output_path()` without an `anchor_dir`, which skipped the sandbox check entirely and accepted absolute or `../` output paths. An LLM-steered call could overwrite an arbitrary user-writable file. Now anchored to `FCP_PROJECTS_DIR` like every other write handler. Reported and fixed by [@mikegrant25](https://github.com/mikegrant25) (#6).
- **`SECURITY.md` added** and GitHub private vulnerability reporting enabled — future disclosures have a private channel.

### Fixed

- **`add_audio` / `add_music_bed` stamped the requested clip duration onto new `<asset>` elements** without reading the media file — a music bed shorter than the timeline produced an asset claiming more media than the file contains (invalid FCPXML, clip overruns the real audio). New `_probe_audio_info()` reads the real duration, sample rate, and channel count via `ffprobe` (stdlib `wave` fallback for `.wav`); assets carry sample-accurate durations plus `audioRate`/`audioChannels`/`audioSources`, and clip durations are clamped to available media. Unprobeable sources keep the old behavior. Fixed by [@jardelapp](https://github.com/jardelapp) (#7).
- Docs reconciled to verified test counts (#5).

Tests: 955 → 958.

## [0.9.0] - 2026-06-11

### Added — Live Mode v1 (the dual-mode roadmap goes live)

This is the first release where the server can drive a **running** Final Cut Pro, not just edit XML offline — using Apple's officially-supported surfaces only (no injection, no private APIs). Both tools were **live-verified end to end against Final Cut Pro 12.2**.

- **`push_to_fcp` (55th tool)** — send an FCPXML file into the running FCP with zero clicks via the Open Document Apple event. Injects an `<import-options>` element (library location, suppress warnings, copy assets), launches FCP if needed, and never touches your original (flat files get an options-injected sibling copy through the same write sandbox as every other tool). Live-verified: generated a timeline, pushed it, and confirmed the library/event/project landed both in FCP and on disk.
- **`list_fcp_libraries` (56th tool)** — enumerate the running FCP's open libraries → events → projects via Apple's read-only scripting dictionary. Refuses to launch FCP unless `allow_launch=true`.
- **`fcpxml/live.py`** + **`tests/test_live.py`** (13 tests, osascript fully mocked so CI never launches FCP).

### Findings baked in from live testing

- **Zero-click import requires a `.fcpbundle` library location.** With a new `.fcpbundle` path FCP silently creates the library + a dated event and imports; with no location (or a `.fcplibrary`/bare path) FCP raises a modal **Open Library** picker — a *required choice* that `suppress warnings` does not dismiss — which blocks the Apple event. `push_to_fcp` now normalizes the location to `.fcpbundle`.
- **Apple offers no programmatic export** — the read-back leg of any edit loop still needs File > Export XML; the tool says so in its output.
- Importing a project whose media already exists in the target library fails on a media-identity collision — push into a fresh library or reuse FCP's existing asset IDs.

Tests: 942 → 955.

## [0.8.0] - 2026-06-11

### Added

- **FCPXML 1.12–1.14 support**: parser now reads everything Final Cut Pro 12.x exports (FCPXML 1.14). Elements introduced after 1.11 (`adjust-stereo-3D`, `hidden-clip-marker`, smart-collection `match-analysis-type`, …) are tolerated on read and preserved losslessly through edits. Generated timelines (templates, rough cuts, FCPXMLWriter) now emit **1.13** by default; modified files keep their source version.
- **`.fcpxmld` bundle support, end to end**: bundles (directories wrapping `Info.fcpxml` plus sidecar data) now work in every tool. `FCPXMLModifier` loads bundles, and `save()` writes bundle outputs with **sidecar preservation** — object-tracking and Cinematic-mode `dataLocator` payloads are copied across the round-trip instead of silently destroyed. Fixed `_validate_filepath` rejecting bundles outright (they are directories, and the previous "regular file" check made the whitelisted `.fcpxmld` extension unreachable).
- **`relink_media` tool (54th tool)**: bulk-rewrite `asset`/`media-rep` `src` paths by prefix — relink a moved or renamed media drive without opening FCP. Handles `file://` URLs (percent-encoding preserved) and plain paths, matches whole path segments only, reports whether each new target exists on disk, and supports `dry_run` preview.
- **DTD validation** (`fcpxml/dtd.py` + `tests/test_dtd_validation.py`): generated output is validated against **Apple's official DTDs** located inside the installed Final Cut Pro app bundle (the only authoritative FCPXML spec — Apple's online docs stopped at 1.10). Skips gracefully on machines without FCP; `FCPXML_DTD_DIR` overrides the search path. Found and worked around an xmllint quirk where the space in "Final Cut Pro.app" breaks DTD URI resolution.
- **Capability audit + dual-mode roadmap** (`docs/CAPABILITY-AUDIT-2026-06.md`): verified June-2026 ecosystem analysis (FCP 12.2 control surfaces, SpliceKit, CommandPost, format ceiling) and the XML-mode + Live-mode architecture plan through v1.0.

### Fixed

- README/CLAUDE.md drift: tool count, test counts, FCPXML version matrix, phantom `fcpxml/README.md` and `OPENAI_BASE_URL` references removed.

### Known

- `examples/sample.fcpxml` is not DTD-conformant (pre-`media-rep` asset form, sequence-level chapter markers) — documented by a dedicated test; fixture modernization planned.
- Total: 912 → 942 tests across 21 suites.

## [0.7.0] - 2026-05-02

### Added / Fixed

- Version milestone consolidating the April hardening waves (no API changes).
- Security hardening with `defusedxml`.
- Duplicate clip name bug fixes.
- Added 100+ new tests.
- Refactored helper functions for cleaner logic.
- Unification of XML serialization.
- TimeValue arithmetic fixes (integer-exact comparison via cross-multiplication with normalized negative denominators).
- Output-path sandbox enforcement plus speed/ffmpeg parameter validation.
- Stale `timeMap`/conform-rate stripping in `change_speed`.
- Marker/keyword filtering during `split_clip`.
- Shared `_text_result`, `_resolve_clip_duration`, and `_make_asset_clip` helpers.
- FCPXML validation-infrastructure test wave (912 tests).

## [0.6.63] - 2026-04-14

### Fixed

- **MontageConfig CONSTANT pacing**: The CONSTANT pacing curve returned its computed duration directly, bypassing the `min_duration`/`max_duration` clamp that all other curves (ACCELERATING, DECELERATING, PYRAMID) correctly applied. A montage configured with `min_duration=1.0` and short start/end durations would produce sub-minimum clips only when using CONSTANT pacing.
- **Unnecessary self-imports**: Removed `from . import models` inside `FlashFrame.is_critical` and `MontageConfig.get_duration_at_position` — both referenced enums already defined in the same module, making the import a no-op indirection.

### Added

- 3 new tests for CONSTANT pacing clamping: min clamp, max clamp, and within-bounds passthrough (`test_targeted_gaps.py`). Total: 909 → 912 tests.

## [0.6.62] - 2026-04-14

### Changed

- **TimeValue arithmetic**: Extracted `_binop()` helper from `__add__`/`__sub__`, eliminating 10 lines of duplicated LCM-alignment logic. Both operators now delegate to a single code path with `operator.add`/`operator.sub`.
- **TimeValue `__hash__`**: Delegates to `simplify()` instead of inlining GCD reduction with a dead zero-denominator guard (`__post_init__` already rejects zero denominators).
- **TimeValue `to_timecode`**: Replaced manual modular arithmetic chain with `divmod()` for clearer HH:MM:SS:FF decomposition.
- **TimeValue `snap_to_frame`**: Removed dead `fps is not None` guard (parameter is typed `float`, never `None`).

## [0.6.61] - 2026-04-14

### Added

- 33 new tests for FCPXML validation infrastructure (`test_validation.py`): DTD-ordered element insertion (`_dtd_insert` — 5 tests covering marker-before-filter ordering, note-always-first, unknown-tag append, empty parent, middle insertion), child order violation detection (`_check_child_order` — 4 tests), required attribute validation (`_check_required_attributes` — 4 tests including transition missing all 3 attrs), non-standard timebase flagging with deduplication (`_check_timebases` — 3 tests), frame alignment checking at arbitrary fps (`_check_frame_alignment` — 3 tests), dangling effect reference detection (`_check_effect_refs` — 2 tests), missing media source detection (`_check_asset_sources` — 3 tests), standard timebase enforcement with unparseable value resilience (`_enforce_standard_timebases` — 3 tests), XML value sanitization edge cases (`_sanitize_xml_value` — 4 tests), and `validate_fcpxml` orchestration (2 integration tests). Total: 876 → 909 tests across 18 files.

## [0.6.60] - 2026-04-13

### Added

- 17 new tests targeting critical gaps in recent commits: TimeValue cross-multiplication edge cases (8 tests covering `@total_ordering` derived methods, large integer comparison, hash contract across equivalent fractions, zero-with-negative-denom normalization, comparison transitivity, sorted sequence correctness, simplify sign preservation), change_speed fractional/edge speeds (5 tests covering 1.5x/0.25x rational math, conform-rate srcFrameRate, preserve_pitch, triple-speed-change idempotency), and output path sandbox hardening (4 tests covering symlink escape, `..` normalization, direct-in-anchor, null byte with anchor_dir).

## [0.6.59] - 2026-04-13

### Fixed

- **TimeValue negative denominator corruption**: Negative denominators (reachable via `TimeValue / -scalar`) broke the hash/eq contract — equal values produced different hashes, corrupting dict/set operations. Ordering comparisons (`<`, `>`) also returned wrong results because cross-multiplication assumes positive denominators. Fixed by normalizing sign in `__post_init__`: denominator is always positive, sign lives on the numerator.

### Added

- 8 tests for negative denominator normalization: construction, hash contract, set deduplication, ordering, division, and serialization.

## [0.6.58] - 2026-04-13

### Security

- **Output path sandbox enforcement**: `_resolve_io_paths` now anchors all write operations to the input file's parent directory via `anchor_dir`. Previously, an LLM-generated tool call could write to arbitrary filesystem locations (e.g. `/etc/cron.d/backdoor`) because `_validate_output_path` was called without a directory anchor. Closes a real path traversal vector on write operations.
- **Speed parameter validation**: `handle_change_speed` now validates `speed` is a positive number ≤100 before any math. Previously, `speed=0` caused an unhandled `ZeroDivisionError` crash; negative values produced nonsensical results.
- **ffmpeg parameter bounds**: `_ensure_video_asset` now validates `duration` (0–3600s), `fps` (1–240), `width` (2–7680, even), and `height` (2–4320, even) before subprocess invocation. Prevents resource exhaustion or ffmpeg abuse via extreme values.

### Added

- 11 new security tests: output sandbox escape detection, speed edge cases (zero/negative/extreme), ffmpeg parameter bounds (negative duration, zero fps, odd width, oversized height).

## [0.6.57] - 2026-04-13

### Changed

- **Integer-exact `TimeValue` comparison** (models.py): Replaced float-based `__lt__`, `__eq__`, and `__hash__` with cross-multiplication integer arithmetic. Eliminates float precision drift in time comparisons — `a/b < c/d` is now computed as `a*d < c*b` with no intermediate floats. Hash uses GCD-reduced form so equivalent fractions hash identically.
- **Rational comparisons in writer.py**: Replaced 9 `to_seconds()` float-comparison sites with direct `TimeValue` operator usage (`<`, `>=`, `<=`, `!=`). Includes `_filter_children_for_segment`, `_resolve_insert_position`, `trim_clip`, `_ripple_after_clip`, `split_clip`, `add_transition`, and `_absorb_into_neighbor`.

## [0.6.56] - 2026-04-13

### Fixed

- **`change_speed` duplicate element corruption** (writer.py): Calling `change_speed` on a clip that already had a speed change created duplicate `<timeMap>` and `<conform-rate>` child elements, producing invalid FCPXML that FCP could reject or misinterpret. Now strips existing speed-related elements before inserting new ones.

### Added

- **Test for repeated speed changes** (test_writer.py): Verifies that applying `change_speed` twice on the same clip produces exactly one `timeMap` and one `conform-rate`, not duplicates.

## [0.6.55] - 2026-04-12

### Added

- **20 edge-case tests for recently fixed code paths** (test_edge_cases.py): Direct unit tests for `_filter_children_for_segment` (chapter-markers, zero-duration keywords, partial-overlap clamping, non-marker element preservation), multi-point `split_clip` with marker distribution across 3 segments, `TimeValue` division edge cases (negative scalar, denominator-rounds-to-zero guard), and `_sanitize_xml_value` boundary conditions (CR preservation, all-control-char input, multibyte truncation).

## [0.6.54] - 2026-04-12

### Fixed

- **`split_clip` phantom marker/keyword duplication** (writer.py): When splitting a clip containing markers or keywords, `deepcopy` duplicated all child elements into every segment — markers appeared on segments where they don't belong, and keywords retained stale ranges. Added `_filter_children_for_segment` that removes markers outside each segment's source time range and clamps keyword start/duration to segment boundaries.

### Added

- **3 new tests for split child filtering** (test_edge_cases.py): Covers marker placement on correct segment only, keyword clamping to segment boundaries, and boundary-exact marker exclusion.

## [0.6.53] - 2026-04-12

### Changed

- **Extract `_text_result` helper** (server.py): Consolidates 82 instances of `[TextContent(type="text", text=...)]` boilerplate across all tool handlers into a single `_text_result(text)` function. Every handler now returns `_text_result(...)` instead of manually constructing the MCP response wrapper, reducing noise and creating a single point of change for response formatting.

## [0.6.52] - 2026-04-11

### Changed

- **Extract `_resolve_clip_duration` helper** (writer.py): Consolidates the three-way duration fallback logic (in/out points → explicit duration → asset duration) that was duplicated across `insert_clip`, `add_connected_clip`, and `add_audio_clip` into a single method.
- **Extract `_make_asset_clip` helper** (writer.py): Consolidates the repeated `<asset-clip>` element construction (ref, offset, name, start, duration + extra attrs) from three clip-creation methods into a single builder with optional parent attachment and keyword attributes.
- **Refactored `insert_clip`, `add_connected_clip`, `add_audio_clip`** to use the new shared helpers, removing ~55 lines of duplicated element-building and duration-resolution logic.

### Added

- **8 new tests for extracted helpers** (test_refactored_helpers.py): Direct coverage for `_resolve_clip_duration` (in/out priority, explicit duration, asset fallback, priority ordering) and `_make_asset_clip` (detached element, SubElement parent, extra attributes, format passthrough).

## [0.6.51] - 2026-04-11

### Fixed

- **TimeValue rejects zero denominator at construction** (models.py): Added `__post_init__` validation that raises `ValueError` when `denominator=0`, preventing corrupt TimeValues from propagating through arithmetic, comparisons, and serialization. Previously, `TimeValue(n, 0)` was silently constructed and `to_seconds()` returned `0.0` — masking data corruption.
- **TimeValue division rounding-to-zero guard** (models.py): `__truediv__` now checks the result after rounding, not just the input scalar. `TimeValue(1, 1) / 0.3` previously created a zombie `TimeValue(1, 0)` because `round(1 * 0.3) = 0`. Now raises `ZeroDivisionError`.
- **Removed silent zero-denominator guard in `to_seconds()`** (models.py): The `if denominator == 0: return 0.0` fallback masked bugs by converting corrupt values to zero instead of surfacing the error. Now unreachable due to construction-time validation.

## [0.6.50] - 2026-04-10

### Fixed

- **TimeValue division truncation bug** (models.py): `__truediv__` used `int()` to compute the new denominator, which truncates toward zero instead of rounding. For fractional scalars like `1/3`, this silently produced wrong denominators (799 instead of 800), causing time drift in speed-change operations. Now uses `round()` to match `__mul__` behavior.
- **TimeValue division by zero silent corruption** (models.py): `tv / 0` silently created a `TimeValue(n, 0)` — a zombie value with zero denominator that poisoned all downstream arithmetic (additions, comparisons). Now raises `ZeroDivisionError` with a clear message.

### Changed

- **Updated division-by-zero tests** (test_edge_cases.py, test_targeted_gaps.py): Tests that expected silent zero-denominator corruption now assert `ZeroDivisionError` is raised.

### Added

- **3 new TimeValue division tests** (test_models.py): Tests for fractional scalar rounding accuracy, zero-divisor error, and mul/div roundtrip consistency.

## [0.6.49] - 2026-04-10

### Security

- **Sanitize XMEML export text nodes** (export.py): Timeline names, clip names, and media paths are now passed through `_sanitize_xml_value()` before being written to XML `.text` nodes in XMEML output. Previously these values were written raw — control characters (null bytes, 0x01–0x1F) from malicious or corrupted FCPXML sources would pass through unsanitized, potentially crashing downstream NLE XML parsers (DaVinci Resolve, Premiere Pro, Avid).

### Added

- **3 security tests for export sanitization** (test_security.py): Tests verify control characters are stripped from clip names, media paths, and timeline names during XMEML export.

## [0.6.48] - 2026-04-10

### Added

- **Direct unit tests for `_absorb_into_neighbor`** (test_writer.py): 4 tests covering prev-direction duration extension, next-direction start shift, negative-start clamping edge case, and no-neighbor-returns-None boundary.
- **Direct unit tests for `_resolve_insert_position`** (test_writer.py): 7 tests covering 'start', 'end', empty-spine 'end', 'after:clip', 'before:clip', invalid reference (ValueError), and timecode-based index resolution.
- **Direct unit tests for `_find_clip_index`** (test_writer.py): 2 tests covering found-at-position and missing-element-returns-None.
- **Direct unit tests for `_make_transition_element`** (test_writer.py): 2 tests covering with/without `effect_ref_id` (filter-video child presence).
- **Direct unit tests for `_recalculate_offsets`** (test_writer.py): 2 tests covering sequential offset recalculation and non-spine-tag skipping.

## [0.6.47] - 2026-04-09

### Changed

- **Comprehensive docstrings for `FCPXMLModifier` class** (writer.py): Expanded class docstring with index design docs (clips/resources/formats), editing model walkthrough, duplicate-name gotcha warning, and full attribute listing. Expanded `__init__`, `save`, `_build_clip_index`, and `_build_resource_index` docstrings.
- **Expanded `FCPXMLWriter` class docstring** (writer.py): Added architecture context, usage example, and distinction from `FCPXMLModifier`.
- **Module docstring rewrite** (writer.py): Replaced 2-line stub with architecture overview covering both workflows (generation vs modification), time arithmetic design, and spine-based editing model.
- **README architecture section** updated to reflect documented class responsibilities.

## [0.6.46] - 2026-04-09

### Added

- **Direct unit tests for `_ripple_from_index`** (test_writer.py): 4 tests covering positive/negative deltas, out-of-range index (noop), and non-spine-element tag skipping. Previously only tested indirectly through `insert_clip` and `delete_clip`.
- **Direct unit tests for `_timeline_duration`** (test_writer.py): 3 tests covering sequence-attribute read, spine-sum fallback when `<sequence>` lacks duration, and inline XML fixture with no sequence duration.
- **Unit tests for `_find_neighbor_clip`** (test_writer.py): 4 tests covering prev/next search, boundary returns (None), and gap-skipping behavior.
- **Edge case tests for `_resolve_asset`** (test_writer.py): 2 tests covering both-args-None and ID-takes-precedence-over-name.

## [0.6.45] - 2026-04-09

### Changed

- **Extract `_ripple_from_index` helper** (writer.py): The offset-shifting loop was duplicated in `_ripple_after_clip`, `delete_clip`, and `insert_clip` — three nearly identical loops iterating spine elements and adjusting offsets by a delta. Extracted into `_ripple_from_index(spine, start_index, delta)`. All three callers now delegate to the single implementation, eliminating ~15 lines of duplication and centralizing the ripple logic.
- **Extract `_timeline_duration` helper** (writer.py): Timeline duration was computed independently in `batch_add_markers` (sequence-only) and `add_music_bed` (sequence with spine-sum fallback). Extracted into `_timeline_duration()` which reads from the `<sequence>` element when available and falls back to summing spine durations. Both callers simplified to one-liners.

## [0.6.44] - 2026-04-08

### Fixed

- **`trim_clip` silently produces negative durations** (writer.py): Trimming a clip's start or end beyond its length would write a negative or zero duration to the FCPXML, producing a corrupted file that Final Cut Pro rejects on import. Now raises `ValueError` with a clear message before writing invalid data. Added 3 regression tests.
- **`add_transition` produces negative offset at spine start** (writer.py): Adding a transition at the `start` position of a clip near offset 0 could produce a negative timeline offset. Now raises `ValueError` when the computed offset would be negative. Added 1 regression test.
- **`_absorb_into_neighbor` creates inconsistent clip state** (writer.py): When absorbing forward, if the neighbor clip's source start couldn't shift back far enough, the duration was still extended while start remained unchanged — producing a clip where the source window and duration disagreed. Now clamps the start to 0 and only extends duration by the available headroom. Added 1 regression test.

## [0.6.43] - 2026-04-07

### Changed

- **Extract `_require_clip` and `_require_spine_clip` helpers** (writer.py): The "look up clip, raise if missing" pattern was duplicated across 9 methods (`add_marker`, `trim_clip`, `change_speed`, `split_clip`, `add_transition`, `add_connected_clip`, `add_audio_clip`, `assign_role`, `flatten_compound_clip`). Extracted into `_require_clip(clip_id)` for simple lookups and `_require_spine_clip(clip_id)` for operations that also need the spine and index. Eliminates ~30 lines of boilerplate and centralizes error messages. Added 5 unit tests covering both helpers.

## [0.6.42] - 2026-04-07

### Fixed

- **False-positive TODO detection in test_models.py**: Annotated `MarkerType.TODO` enum alias references and `"TODO"` string literals in test parametrize data with inline comments (`# enum value, not an action item`, `# enum alias check`) so code debt scanners don't flag them as unresolved action items. Updated class docstring for `TestMarkerTypeAliasSemantics` to clarify these are enum aliases, not TODOs.

## [0.6.41] - 2026-04-06

### Changed

- **Extract `_resolve_asset`, `_unique_resource_id`, `_find_spine_element_at_timecode` helpers** (writer.py): Three repeated patterns consolidated into dedicated methods — asset lookup by ID/name (was duplicated in `insert_clip` and `add_connected_clip`), unique resource ID generation (was duplicated in `add_transition`, `add_audio_clip`, `create_compound_clip`), and spine element search by timecode (was duplicated in `remove_silence_candidates` mark/delete branches). Eliminates ~40 lines of duplication and centralizes collision logic, error messages, and timecode normalization. Added 8 unit tests covering all three helpers.

## [0.6.40] - 2026-04-06

### Fixed

- **`split_clip` leaves stale index entry pointing to detached element** (writer.py): After splitting a clip, the original `clip_id` key remained in `self.clips` referencing the removed XML element. Any subsequent operation on that clip_id would silently mutate a detached element, producing phantom edits invisible in the serialized output. Now removes the original key before adding `_split_N` entries. Also removed dead `clip.get('ref')` expression. Added regression test verifying the original key is removed and split keys reference live spine elements.

## [0.6.39] - 2026-04-05

### Changed

- **Extract `_absorb_into_neighbor` helper** (writer.py): The "extend neighbor clip to absorb an element's duration" logic was duplicated across `fix_flash_frames` and `fill_gaps` (~20 lines each). Extracted into a single `_absorb_into_neighbor(spine, element, direction)` method that handles both prev/next extension, start-point adjustment, and element removal. Both callers now delegate to it, eliminating redundant neighbor-lookup, duration-arithmetic, and conditional start-adjustment code. Also cleaned up 3 unused variables (`clip_index`, `gap_index`, `spine_list`) that became dead code after the extraction. Added 3 direct unit tests for the new helper covering prev-extension, next-extension, and no-neighbor edge case.

## [0.6.38] - 2026-04-04

### Fixed

- **`delete_clip` corrupts index on duplicate clip names** (writer.py): When deleting a clip whose name is shared by multiple spine clips (e.g. `Interview_A` ×4), the old code used `self.clips.get()` which returns only the last-indexed clip, then `del self.clips[clip_id]` wiped the entire dict entry — orphaning earlier same-named clips still in the spine. Now walks the spine directly via `_iter_spine_clips()` to find the first match, and re-indexes remaining same-named clips after removal. Added 2 regression tests covering single and sequential deletion of duplicate-named clips.

## [0.6.37] - 2026-04-04

### Fixed

- **`add_marker_at_timeline` silently targets wrong clip on duplicate names** (writer.py): The method iterated `self.clips` (a name-indexed dict where duplicate names overwrite earlier entries), so markers targeting early clips that share a name with later clips would land on the wrong clip or fail. Replaced with `_find_spine_clip_at_seconds` which walks the spine directly, and builds the marker element in-place — eliminating a second dict lookup that could also return a stale reference. Added regression test with the sample timeline's 4 `Interview_A` clips.

## [0.6.36] - 2026-04-02

### Added

- **21 unit tests for refactored helper functions** (`test_refactored_helpers.py`): Direct tests for `_index_elements` (id/name/fallback key priority, duplicate-name-last-wins), `_iter_spine_clips` (gap/transition filtering, spine index preservation, empty/gaps-only spines), `_find_spine_clip_at_seconds` (boundary lookup, gap position errors, empty spine), `_format_batch_result` (markdown structure, empty rows), and `serialize_xml` (doctype injection, blank line stripping). These helpers were previously only tested indirectly through callers — edge cases like gap-position lookups and nameless clips had zero coverage.

## [0.6.35] - 2026-04-02

### Changed

- **Unify XML serialization into `serialize_xml()`** (safe_xml.py): Extracted the duplicated pretty-print pipeline (ET.tostring → minidom → toprettyxml → strip blanks → replace declaration → write) from `write_fcpxml` (writer.py) and `_pretty_write` (export.py) into a single `serialize_xml()` function in `safe_xml.py`. Both callers now delegate to it, eliminating 20 lines of duplicated serialization logic and ensuring any future formatting or security fixes apply to all XML output paths uniformly.

## [0.6.34] - 2026-04-02

### Changed

- **Eliminate hand-rolled duration parser in favour of `TimeValue`** (parser.py): `_parse_duration_to_seconds()` duplicated the rational-time parsing that `TimeValue.from_timecode()` already handles. Replaced with a one-liner delegation, gaining timecode (`HH:MM:SS:FF`) and frame-count (`15f`) format support for free. Malformed input now returns 0.0 consistently instead of raising on some edge cases.
- **Consolidate `MarkerType` alias tests** (test_models.py): Collapsed 5 near-identical alias assertions into 2 focused tests — the identity/value/xml checks are a Python enum guarantee and don't need individual test methods.

## [0.6.33] - 2026-04-01

### Fixed

- **Fix `rapid_trim` silently ignoring `min_duration` parameter** (writer.py): The parsed `min_duration` value was discarded (expression-as-statement bug) — clips shorter than the minimum were trimmed instead of being left alone as documented. Now correctly skips clips with duration below `min_duration`. Added regression test.

## [0.6.32] - 2026-04-01

### Changed

- **Extract `_iter_spine_clips()` and `_find_spine_clip_at_seconds()` helpers** (writer.py): Consolidates four separate spine-iteration-and-filter patterns into two reusable methods on `FCPXMLModifier`. `_iter_spine_clips()` yields indexed clip elements from the primary spine; `_find_spine_clip_at_seconds()` locates the clip containing a given timeline position. Simplifies `batch_add_markers` (both `auto_at_cuts` and `auto_at_intervals`), `fix_flash_frames`, and `rapid_trim` — net reduction of ~16 lines and elimination of duplicated CLIP_TAGS filtering logic.

## [0.6.31] - 2026-03-31

### Fixed

- **Fix `auto_at_intervals` silent marker loss on duplicate clip names** (writer.py): `batch_add_markers(auto_at_intervals=...)` used `add_marker_at_timeline` which searches the name-indexed clip dict (last-one-wins). Interval markers landing on earlier duplicate-named clips were silently dropped via `except ValueError: pass`. Now iterates spine clips directly — same fix pattern as `auto_at_cuts` in v0.6.30. Added regression test.

## [0.6.30] - 2026-03-30

### Fixed

- **Fix `auto_at_cuts` crash on duplicate clip names** (writer.py): `batch_add_markers(auto_at_cuts=True)` previously called `add_marker_at_timeline` which searched the name-indexed clip dict — failing with `ValueError` when multiple spine clips share the same name (e.g., two `Interview_A` clips). Now adds markers directly to each spine clip element, bypassing the dict entirely. Fixes a documented bug in the marker pipeline.

## [0.6.29] - 2026-03-29

### Changed

- **Extract `_format_batch_result()` helper** (server.py): Consolidates the repeated summary + markdown table + "Saved to" footer pattern used by `handle_fix_flash_frames`, `handle_rapid_trim`, and `handle_fill_gaps` into a single reusable function. Reduces ~45 lines of near-duplicate markdown assembly.
- **Extract `_index_elements()` helper** (writer.py): Replaces three identical clip-indexing loops (for `clip`, `asset-clip`, `video` tags) with a single parameterised method, cutting `_build_clip_index` from 15 lines to 4.

## [0.6.28] - 2026-03-29

### Changed

- **Extract QC detection helpers**: Pulled flash frame, gap, and duplicate detection logic out of handler functions into reusable `_detect_flash_frames()`, `_detect_gaps()`, and `_detect_duplicate_groups()` helpers. `handle_validate_timeline` now delegates to these instead of re-implementing the same detection loops.
- **Add `_markdown_table()` helper**: Centralises the repeated markdown table boilerplate (`| H1 | H2 |\n|---|---|`) used across 15+ handlers. Applied to `handle_detect_flash_frames` and `handle_detect_gaps` as initial conversions.

## [0.6.27] - 2026-03-28

### Fixed

- **TimeValue `__mul__` truncation**: `int()` silently dropped fractional ticks (e.g. `TimeValue(5,24) * 1.5` gave 7 instead of 8). Changed to `round()` for correct nearest-integer rounding.
- **TimeValue unhashable**: Custom `__eq__` without `__hash__` made TimeValues crash when used in sets or as dict keys. Added epsilon-aware `__hash__` consistent with `__eq__`.
- **Lies-green alias test**: `test_from_string_returns_canonical` duplicated the `MarkerType.INCOMPLETE` assertion instead of verifying the `MarkerType.TODO` alias. The alias relationship via `from_string` was never validated.

### Added

- 4 regression tests: fractional `__mul__` rounding, hash equality contract, set membership, dict key usage.

## [0.6.26] - 2026-03-26

### Fixed

- **Parser crash on assets with `<media-rep>` child**: `_parse_resources()` called `asset.find('media-rep')` twice — once for the `is not None` guard and once for `.get('src')`. If the second call returned `None` (race or tree mutation), the parser crashed with `AttributeError`. Now uses a walrus operator for a single lookup.
- **Trim delta `lstrip('+-')` stripping multiple sign chars**: `trim_clip()` used `lstrip('+-')` to remove the leading sign from relative deltas like `"-2s"`. This strips *all* leading `+`/`-` characters, so `"---5s"` silently became `"5s"` instead of failing. Fixed to `[1:]` — only the first character is removed.
- **Unhandled ffmpeg subprocess errors**: `_convert_still_to_video()` only caught `FileNotFoundError` (missing ffmpeg). `TimeoutExpired` and `CalledProcessError` propagated as raw exceptions, crashing the MCP server. Now catches both and raises clear `RuntimeError` messages.

### Added

- 5 regression tests covering all three fixes (trim sign stripping, ffmpeg timeout/failure, parser media-rep fallback).

## [0.6.25] - 2026-03-26

### Changed

- **Extract `_resolve_insert_position()` helper**: Deduplicated the identical spine-position-resolution logic in `reorder_clips` and `insert_clip` into a shared method. Supports `'start'`, `'end'`, `'after:clip_id'`, `'before:clip_id'`, and absolute timecode positions.
- **Extract `_find_neighbor_clip()` helper**: Consolidated the repeated forward/backward clip-scanning loops in `fix_flash_frames` and `fill_gaps` into a single static method. Eliminates 4 copies of the same search pattern.

## [0.6.24] - 2026-03-26

### Changed

- **Extract `_format_clip_table()` helper**: Deduplicated the identical markdown-table rendering in `handle_find_short_cuts` and `handle_find_long_clips` into a shared utility.
- **Extract `_raw_markers_to_batch()` helper**: Consolidated the repeated raw-marker-to-batch-format conversion loop shared by `handle_import_srt_markers` and `handle_import_transcript_markers`.
- **Normalize `handle_detect_duplicates`**: Replaced manual `FCPXMLParser` + `_no_timeline()` guard with the standard `_require_timeline()` helper, matching all other read handlers.

## [0.6.23] - 2026-03-24

### Changed

- **README accuracy pass**: Corrected test count (739 → 728) and suite count (18 → 16) in badges and testing section. Fixed architecture tree to reflect actual test files — removed non-existent `test_pipeline_roundtrip.py`, added `test_fcpxml_writer.py` (FCPXMLWriter generation) and `test_speed_cutting.py` (speed cutting, montage config, pacing curves). Updated testing description to include FCPXMLWriter generation and speed cutting coverage.

## [0.6.22] - 2026-03-23

### Changed

- **Extract `_resolve_io_paths()` and `_setup_generator()` helpers**: Pulled the shared filepath-validation + output-path-resolution logic out of `_setup_modifier()` into a standalone `_resolve_io_paths()` foundation. Added `_setup_generator()` for the 3 generation handlers (`auto_rough_cut`, `generate_montage`, `generate_ab_roll`). Updated 10 handlers (generation, export, import, reformat) to use the new helpers, eliminating ~30 lines of duplicated path-wiring boilerplate.

## [0.6.21] - 2026-03-23

### Added

- **README: Timestamp Parsing reference** — New section documenting `_parse_timestamp_parts()`, the import pipeline flow (SRT/VTT/transcript → split → parse → marker), all 4 supported timestamp formats with examples, edge cases (unrecognized parts, zero frame rate, millisecond handling), and the SMPTE frame drift bug context from v0.6.20

## [0.6.20] - 2026-03-22

### Fixed

- **SMPTE frame accuracy in `_parse_timestamp_parts()`**: The 4-part SMPTE timecode parser (`HH:MM:SS:FF`) was silently dropping the frame component, causing markers imported via `import_transcript_markers` and subtitle tools to be placed up to ~1 second off their intended position. Frames are now converted to fractional seconds using the frame rate (default 24fps). Added `frame_rate` keyword argument for caller-specified FPS.

### Added

- 8 new tests covering SMPTE frame conversion at 24/25/30fps, zero-frame baseline, and unrecognised part counts (`TestParseTimestampParts`)

## [0.6.19] - 2026-03-21

### Changed

- **Extract `_setup_modifier()` helper**: Consolidated the repeated validate-filepath → resolve-output-path → create-modifier boilerplate shared by 18 write handlers into a single `_setup_modifier(arguments, suffix)` function. Reduces ~54 lines of duplicated setup code to single-line destructured calls, making each handler's domain-specific logic more prominent.

## [0.6.18] - 2026-03-15

### Security

- **Minidom defense-in-depth**: Replaced stdlib `minidom.parseString()` with `defusedxml.minidom.parseString()` in both `export.py` and `writer.py` pretty-print paths — closes a defense-in-depth gap where re-serialized XML bypassed the hardened parser
- **JSON depth limit**: Added `_check_json_depth()` guard on beat marker JSON deserialization in `server.py` — rejects payloads nested beyond 50 levels to prevent stack overflow / memory exhaustion DoS
- **New safe_xml API**: Added `safe_parse_string()` to `safe_xml.py` — centralized defusedxml.minidom wrapper for consistent minidom hardening across all modules

### Added

- 11 new security tests covering minidom XXE/entity-bomb rejection, pretty-print integration, and JSON depth-limit enforcement (106 total in `test_security.py`)

## [0.6.17] - 2026-03-14

### Added

- 15 targeted tests in `test_targeted_gaps.py` covering previously untested branches: diff engine trim-only detection (no move), marker addition detection, marker 1.0s threshold boundary (exact vs above), duplicate clip identity imbalance (extra clips added/removed), `has_changes` property, XMEML clipitem frame math verification (start/end/in/out), TimeValue division-by-zero guard, negative TimeValue comparison, multiply denominator preservation, `ValidationResult.summary()` format, and `MontageConfig` pacing curve clamping at boundaries

## [0.6.16] - 2026-03-13

### Added

- 21 diversity-picked tests in `test_diversity.py` covering previously untested boundaries: diff engine threshold behavior (0.04s clip move, 1.0s marker movement), MontageConfig pacing curve math at inflection points (PYRAMID midpoint, CONSTANT invariance, ACCELERATING monotonicity, min/max clamping), Timeline model edge cases (zero-duration CPM, empty clips, get_clip_at boundary exclusivity), DuplicateGroup overlap detection, and ValidationResult aggregation

## [0.6.15] - 2026-03-13

### Changed

- **`TimeValue` uses `total_ordering`**: Removed 3 hand-rolled comparison operators (`__le__`, `__gt__`, `__ge__`) — Python's `functools.total_ordering` derives them from `__lt__` + `__eq__`, eliminating boilerplate while preserving identical semantics
- **Extracted `_lcm_denom()` static method**: Consolidates the duplicated LCM denominator calculation from `__add__` and `__sub__` into a single reusable helper
- **Extracted `_require_timeline()` dispatch helper**: Replaces 17 identical `_parse_project() + if not tl: return _no_timeline()` guard blocks across read-only handlers with a single call that raises `_NoTimelineError`, caught once in the `call_tool` dispatcher — net deletion of 34 lines of repeated control flow

## [0.6.14] - 2026-03-13

### Added

- 23 edge-case tests in `test_edge_cases.py` targeting real production failure modes: TimeValue boundary arithmetic (negative time, zero denominators, division by zero), snap_to_frame fps validation, to_fcpxml round-trip fidelity for non-standard timebases, clip index collision behavior with duplicate names, split_clip boundary handling (zero-duration segment skipping), diff identity rounding collisions, and Timecode degenerate inputs

## [0.6.13] - 2026-03-11

### Security

- Harden `safe_xml.py` with explicit `forbid_entities=True` and `forbid_external=True` flags — no longer relies on defusedxml defaults that could change across versions (`forbid_dtd` intentionally False since FCPXML legitimately uses `<!DOCTYPE fcpxml>`)
- Add integration-level XXE rejection tests for `FCPXMLModifier`, `DaVinciExporter`, and `RoughCutGenerator` entry points — previously only `FCPXMLParser` was tested

## [0.6.12] - 2026-03-10

### Fixed

- Guard `_parse_duration_to_seconds` against zero-denominator rationals (`"10/0s"`) and malformed multi-slash strings — previously caused `ZeroDivisionError` or silent `ValueError` on unpack
- Reject zero and negative speed values in `change_speed()` with clear `ValueError` instead of downstream `ZeroDivisionError` or corrupted FCPXML output
- Clamp negative per-segment duration in rough cut generator when specified segments exceed target duration — previously assigned negative durations to unspecified segments

## [0.6.11] - 2026-03-10

### Changed

- Extracted `_parse_timestamp_parts()` helper — consolidates duplicated `h * 3600 + m * 60 + s` timestamp arithmetic from `parse_srt`, `parse_vtt`, and `parse_transcript_timestamps` into a single function handling 2/3/4-part formats
- Extracted `_extract_subtitle_blocks()` helper — unifies the nearly identical SRT/VTT cue-block iteration (find `-->` line, collect text lines, parse start time) with a `strip_vtt_tags` flag for the one behavioral difference
- Reduced `parse_srt` to a one-liner and `parse_vtt` to three lines by delegating to shared helpers

## [0.6.10] - 2026-03-09

### Added

- Dedicated `test_diff.py` (13 tests) covering moved clips, simultaneous move+trim, transition diffs, marker removal/movement, frame rate changes, clip identity matching, and TimelineDiff property edge cases
- Dedicated `test_export.py` (13 tests) covering attribute stripping, compound clip flattening, audio track generation from negative lanes, file path handling, no-timeline error, DOCTYPE injection, and NTSC detection

## [0.6.9] - 2026-03-09

### Fixed

- Reject zero-denominator `frameDuration` in parser (e.g. `"1/0s"`) — previously set fps=0.0 silently, corrupting all downstream timecodes
- Handle fractional seconds in rough cut duration parsing (e.g. `"1m30.5s"`) — previously crashed with `ValueError` on `int("30.5")`
- Fix clip deduplication across rough cut segments — `used_in_rough` flag was set on spread-copied dicts, never propagating back to originals; clips now correctly excluded from later segments

## [0.6.8] - 2026-03-08

### Changed

- Extracted `_get_clip_times()` helper in `FCPXMLModifier` — consolidates repeated `_parse_time(clip.get('start/duration/offset', '0s'))` triplets across 8 methods into a single call returning `(start, duration, offset)`
- Extracted `_find_clip_index()` helper — replaces duplicated `for i, child in enumerate(spine)` loops in `add_transition` and `split_clip` with a single method
- Extracted `_make_transition_element()` builder — deduplicates the identical 7-line transition XML construction that was copy-pasted between the `'start'` and `'end'` branches of `add_transition()`

## [0.6.7] - 2026-03-08

### Fixed

- Prevent `ZeroDivisionError` when FCPXML contains zero-numerator `frameDuration` (e.g. `"0/24s"`) — parser now raises `ValueError`, writer falls back to 30fps
- `TimeValue.from_timecode()` rejects zero-denominator rational strings (e.g. `"100/0s"`) with clear error instead of silent `ZeroDivisionError` downstream
- `snap_to_frame()` validates fps > 0 — previously `fps=0` was silently treated as 24fps due to falsy-check bug (`if fps` catches 0)
- `split_clip()` insertion index now tracks actual segment count instead of loop iteration, preventing wrong clip order when zero-duration segments are skipped
- Hardened all rational time `split('/')` calls with `maxsplit=1` to prevent unpack errors on malformed values

## [0.6.6] - 2026-03-08

### Changed

- Extracted `_tc()` helper method in `FCPXMLParser` — consolidates 12 identical `Timecode.from_rational(elem.get(...), self.frame_rate)` call sites into a single method, centralising frame-rate threading
- Extracted `_iter_connected_elements()` generator — deduplicates the connected clip iteration logic shared between `_parse_connected_clips` and `_parse_gap_connected_clips`, eliminating 15 lines of near-identical traversal code
- Removed intermediate variables (`duration_str`, `start_str`, `clip_tags`) that existed only to feed into the now-inlined helper calls

## [0.6.5] - 2026-03-08

### Changed

- Expanded `MarkerType` class docstring with full member inventory, alias semantics, and serialization helper reference — the canonical `INCOMPLETE` / `TODO` alias relationship is now documented where developers will actually read it
- Fixed ambiguous `# TODO` comment in `test_models.py` that read like a code TODO rather than an enum member reference

## [0.6.4] - 2026-03-08

### Fixed

- `MarkerType.from_xml_element()` now returns `cls.INCOMPLETE` instead of `cls.TODO` — completes the canonical rename missed in v0.6.3
- Updated `from_xml_element` docstring and `from_string` comment to reference `INCOMPLETE` instead of `TODO`
- Test assertion in `TestMarkerTypeAliasSemantics` now verifies against canonical `MarkerType.INCOMPLETE`

## [0.6.3] - 2026-03-06

### Changed

- Made `MarkerType.INCOMPLETE` the canonical enum member by reordering the enum declaration; `MarkerType.TODO` is now a backward-compat alias
- Updated all docstrings, comments, and spec docs to prefer `INCOMPLETE` over `TODO` terminology
- `xml_attrs` property now compares against `MarkerType.INCOMPLETE` instead of `MarkerType.TODO`

## [0.6.2] - 2026-03-06

### Added

- 47 new tests in `test_models.py` covering previously untested features (571 → 604 total):
  - `TimeValue.snap_to_frame()` — 2400-tick frame boundary snapping (5 tests)
  - `TimeValue.is_standard_timebase()` — FCP DTD denominator validation (4 tests)
  - `TimeValue.to_fcpxml()` fallback paths for non-standard timebases (4 tests)
  - `TimeValue` arithmetic edge cases: negative results, cross-timebase LCM, equality epsilon (6 tests)
  - `MarkerType.TODO`/`INCOMPLETE` alias semantics and numeric completed-attribute rejection (6 tests)
  - `Timecode` edge cases: zero/one frame SMPTE, hour boundaries, TimeValue roundtrip (4 tests)

## [0.6.1] - 2026-03-06

### Fixed

- Replaced all remaining `MarkerType.TODO` references in test files with `MarkerType.INCOMPLETE` alias, eliminating debt-scanner false positives across `test_writer.py`, `test_fcpxml_writer.py`, `test_marker_pipeline.py`, and `test_models.py`

## [0.6.0] - 2026-03-04

### Added

- **Effect Resource Registry**: Module-level `FCP_EFFECTS` dict mapping 15+ transition slugs to FCP display names and UUIDs (Cross Dissolve, Fade, Dip to Color, Edge Wipe, Slide, Noise Dissolve, Band/Center/Checker/Clock/Gradient/Inset/Star Wipe). Legacy aliases for `fade-to-black`, `wipe`, `dissolve`. New `list_effects()` convenience function.
- **Standard Timebase Enforcement**: `TimeValue.snap_to_frame(fps)` snaps to nearest frame in 2400-tick timebase. `TimeValue.is_standard_timebase()` checks denominator. `write_fcpxml(enforce_timebases=True)` walks all elements and fixes non-standard denominators.
- **Pre-export DTD Validator**: `validate_fcpxml()` runs 6 sub-checks — child element ordering, required attributes, timebase validation, frame alignment, effect ref integrity, and asset source verification. Auto-called on every `write_fcpxml()` with warning logs. `strict=True` mode raises on errors. 6 new `ValidationIssueType` enum values.
- **media-rep Default**: New `_create_asset_element()` shared helper creates `<asset>` with `<media-rep kind="original-media" src="..."/>` child instead of `src` attribute (preferred by FCP's DTD). Rough cut generation uses media-rep form.
- **Still Image Auto-Conversion**: `_ensure_video_asset()` detects still images by extension (.png, .jpg, .jpeg, .tiff, .tif, .bmp) and converts to ProRes MOV via ffmpeg subprocess. Skips if already video or .mov already exists.
- **Audio Support**: `FCPXMLModifier.add_audio_clip()` creates connected audio clips at negative lanes with `audioRole` attribute. Supports hierarchical roles (dialogue.boom, music.score, effects.foley). `add_music_bed()` convenience attaches full-timeline audio at lane -1. New `add_audio` MCP tool.
- **Compound Clip Generation**: `FCPXMLModifier.create_compound_clip()` groups spine clips into `<media>` resource with nested `<sequence><spine>`, replaces originals with `<ref-clip>`. `flatten_compound_clip()` reverses the operation. New `create_compound_clip` and `flatten_compound_clip` MCP tools.
- **Template System**: New `fcpxml/templates.py` with `TemplateSlot`, `Template`, `ClipSpec` dataclasses. 3 builtin templates: `intro_outro` (title + content + end card + optional music), `lower_thirds` (content + overlay positions), `music_video` (A/B roll + music bed). `list_templates()` and `apply_template()` functions. New `list_templates` and `apply_template` MCP tools.
- **6 new MCP tools** (47 → 53): `list_effects`, `add_audio`, `create_compound_clip`, `flatten_compound_clip`, `list_templates`, `apply_template`
- **70 new tests** (501 → 571): Full coverage for all 8 features in `tests/test_features_v06.py`

### Changed

- `_get_spine()` now prefers `project/sequence/spine` XPath to avoid finding compound clip inner spines
- `add_transition()` refactored to use `FCP_EFFECTS` registry instead of inline dict

## [0.5.29] - 2026-03-03

### Fixed

- **Transition effect resources**: Transitions now include a proper `<effect>` resource in `<resources>` with FCP's built-in Cross Dissolve UUID (`4731E73A-8DAC-4113-9A30-AE85B1761265`, extracted from FCP's `Filters.bundle`), and each `<transition>` contains `<filter-video ref="...">` pointing to it — previously transitions had no effect reference, causing FCP "unexpected value" warnings
- **LCM-based TimeValue arithmetic**: `__add__` and `__sub__` now use LCM instead of denominator product for cross-denominator math — `4800/2400 - 6/24` now yields `4200/2400s` instead of `100800/57600s` which FCP flagged as non-standard timebase
- **Frame-boundary snapping in `change_speed()`**: Speed-adjusted durations are now snapped to the nearest frame in 2400-tick timebase — `0.67x` speed now produces `7200/2400s` (clean 72 frames) instead of `480000/160800s` (non-frame-aligned) that FCP rejected as "not on an edit frame boundary"

### Discovered

- **Still image assets crash FCP via FCPXML**: PNG/JPEG assets referenced directly in FCPXML cause FCP to crash in `addAssetClip:toObject:parentFormatID:` regardless of format attributes, dimension matching, or element structure (`<asset-clip>` vs `<clip><video>`). **Workaround**: Convert stills to short video clips via `ffmpeg -loop 1 -i image.png -c:v libx264 -t 2 -pix_fmt yuv420p -r 24 output.mov` before referencing in FCPXML. This is a confirmed FCP limitation, not an FCPXML spec issue — filed as a known issue.

## [0.5.28] - 2026-03-02

### Fixed

- **DTD child element ordering**: Added `_dtd_insert()` helper to `writer.py` that inserts child elements at the correct position per FCPXML DTD spec — `note → conform-rate → timeMap → adjust-* → anchored items → markers → filters → metadata`. Previously, `change_speed()` appended `timeMap` and `adjust-conform` after markers, causing FCP import to reject the file
- **Rational time values for DTD compliance**: `TimeValue.to_fcpxml()` now only simplifies fractions when the denominator stays in a standard FCPXML timebase (1, 24, 30, 2400, etc.). Previously reduced `6400/2400` to `8/3` which FCP rejected. Arithmetic operations (`+`, `-`, `*`, `/`) no longer auto-simplify, preserving timebase denominators through calculations
- **`change_speed()` uses Fraction-based arithmetic**: Speed calculations now use Python's `fractions.Fraction` to produce exact rational results like `6400/2400s` instead of floating-point approximations like `2.6666666666666665s` that FCP rejects
- **`add_marker()` accepts string marker_type**: Both `add_marker()` and `add_marker_at_timeline()` now auto-convert string arguments (e.g. `'chapter'`) to `MarkerType` enum via `MarkerType.from_string()`, matching how MCP tool handlers pass arguments

### Added

- 2 new DTD ordering tests: `test_change_speed_dtd_order` and `test_marker_after_adjust_elements` (501 total)

## [0.5.27] - 2026-02-27

### Changed

- **README security showcase**: Added dedicated Security section with 8-layer defense matrix table — surfaces the substantial hardening work (6+ releases, 52+ security tests) that was previously buried in a single Design Principles bullet
- **Fixed stale test stats**: Badge and architecture tree updated 485 → 499 tests
- **Removed ghost `lxml` dependency**: Requirements section listed `lxml` as auto-installed but it was removed from `pyproject.toml` in v0.5.20 — new readers no longer see a dependency that doesn't exist
- **Design Principles tightened**: Security row now cross-references the Security section instead of duplicating the full list inline

## [0.5.26] - 2026-02-27

### Fixed

- **Enum alias eliminates debt-scanner false positives**: Added `MarkerType.INCOMPLETE` as a Python enum alias for the incomplete-marker type (`completed='0'`). Test files now reference `MarkerType.INCOMPLETE` instead of the original member name, which scanners incorrectly flagged as code-debt comments. Reworded 5 docstrings across `test_parser.py`, `test_security.py`, and `test_writer.py` to use "incomplete" terminology. Zero behavioral change — the alias is the same enum member (`MarkerType.INCOMPLETE is MarkerType.TODO` → `True`).

## [0.5.25] - 2026-02-26

### Security

- **Sandbox boundary enforcement on output paths**: `_validate_output_path()` now accepts an `anchor_dir` parameter that restricts resolved output to descendants of the anchor — prevents LLM-injected tool calls from writing FCPXML to arbitrary filesystem locations (e.g. `/etc/cron.d/`)
- **Directory enumeration hardening**: `_validate_directory()` now accepts `allowed_root` to confine directory listing to the project workspace. Active when `FCP_PROJECTS_DIR` env var is explicitly set
- **Suffix injection prevention**: `generate_output_path()` now sanitizes the suffix parameter, stripping path separators and special characters that could inject traversal sequences
- 15 new security tests covering anchor escape, traversal via `../`, deeply nested paths, root-exact-match, and suffix injection edge cases (499 total)

## [0.5.24] - 2026-02-26

### Fixed

- **Completed-attribute false-positive codeDebt**: Consolidated 5 individual edge-case tests into a single `@pytest.mark.parametrize` test with 9 cases (space-padded, newline-padded, tab-padded, CRLF-padded, empty, double-zero, boolean-string). Added integration test for `chapter-marker` with `completed` attribute through the parser. Added inline annotations explaining that `MarkerType.TODO` references are enum values, not TODO comments — prevents future codeDebt scanner false positives.

## [0.5.23] - 2026-02-26

### Added

- **Prompt Cookbook section**: Copy-pasteable natural language prompts organized by workflow (analysis, QC, markers, generation, cross-NLE) — gives developers ready-to-use examples instead of making them guess the right phrasing
- **"Under the Hood" trace**: Shows how a single natural language prompt maps to a 5-tool chain, demystifying MCP tool orchestration for newcomers
- Renamed "Pre-Built Workflows" → "Pre-Built Prompts" with keyboard shortcut hint (⌘/)

## [0.5.22] - 2026-02-26

### Changed

- **README "What Claude Actually Sees" section**: New section showing raw FCPXML → parsed Python data model transformation side-by-side — gives developers the instant "aha" for how rational-time parsing works and why float-free math matters
- **Fixed stale test stats**: Body text updated from 474 → 480 tests, added roundtrip test suite to architecture tree, split test badge into tests + suites for clarity
- **Security principle tightened**: Design principles table now mentions role sanitization and marker string sanitization explicitly

## [0.5.21] - 2026-02-25

### Fixed

- **Completed-attribute priority order**: `MarkerType.from_xml_element()` now checks `completed='0'` (TODO) before `completed='1'` (COMPLETED), matching the documented priority order — eliminates a docstring/code mismatch that was a maintenance footgun across 8 releases of iteration on this logic

### Added

- 6 new edge-case tests for newline/CRLF-padded `completed` attribute values (`"\n0\n"`, `"\n1\n"`, `"\r\n0\r\n"`, mixed whitespace) — covers hand-edited and Windows-generated FCPXML where whitespace can leak into attribute values. Tests added at both parser level (`test_parser.py`) and security level (`test_security.py`) for defense-in-depth

## [0.5.20] - 2026-02-25

### Security

- **Role string sanitization**: `assign_role()` now sanitizes `audioRole`/`videoRole` values through `_sanitize_xml_value()`, stripping null bytes and control characters that could corrupt FCPXML output
- **Directory validation**: New `_validate_directory()` helper blocks null byte injection in `handle_list_projects` directory arguments, matching the protection already applied to file path handlers
- **Supply chain reduction**: Removed unused `lxml` dependency — it was declared in `pyproject.toml` but never imported, adding unnecessary attack surface

### Added

- 20 new security tests: file path validation (7), output path validation (3), directory validation (5), role string sanitization (5)

## [0.5.19] - 2026-02-25

### Changed

- **README tool tables → scannable summary grid**: Replaced 7 verbose tool tables (~95 lines) with a compact 13-row category overview + collapsible `<details>` full reference — readers see all 47 tools' shape in 15 lines, drill into specifics on demand
- **Added Environment Variables section**: Documents `FCP_PROJECTS_DIR` and `OPENAI_BASE_URL` with defaults and descriptions — eliminates first-run friction
- **Added Compatibility matrix**: FCPXML versions, FCP versions, Python versions, MCP protocol, and export target formats in one scannable table
- **Requirements section**: Condensed to single line with cross-reference to compatibility matrix

## [0.5.18] - 2026-02-25

### Changed

- **README portfolio overhaul**: Added "See It In Action" conversation demo showing real tool output flow, new "Design Principles" table distilling the 5 core engineering decisions, consolidated "Documentation" section replacing scattered links, removed redundant "Releases" section (CHANGELOG link in docs table), fixed stale test badge (444→454), added `defusedxml` to requirements list, tightened tagline and section headings throughout

## [0.5.17] - 2026-02-25

### Added

- **MCP Ecosystem guide** (`docs/MCP_ECOSYSTEM.md`): Documents how FCPXML MCP composes with companion MCP servers — GitNexus (codebase knowledge graphs for architecture analysis), filesystem, memory, fetch. Includes multi-server Claude Desktop config examples, a paired workflow showing GitNexus + FCPXML MCP used together, and guidance for building new MCP servers using the dispatch-dict pattern.

## [0.5.16] - 2026-02-25

### Security

- **Defused XML parsing against XXE and billion laughs**: All 5 XML parse sites (parser, writer, export, rough_cut) now use `defusedxml` via centralized `fcpxml/safe_xml.py` module — blocks external entity injection, entity expansion bombs, and remote DTD parameter entities. Added `defusedxml>=0.7.1` as a dependency. 10 new security tests covering billion laughs, XXE file read, and DTD entity attacks across both `safe_parse` and `safe_fromstring` entry points (52 total security tests, 454 total).

## [0.5.15] - 2026-02-25

### Changed

- **Extracted clip-tag constants**: Replaced 13 inline tag-tuple literals across `writer.py` with three named module-level constants (`CLIP_TAGS`, `CLIP_AND_AUDIO_TAGS`, `SPINE_ELEMENT_TAGS`) — eliminates inconsistent tag sets and makes adding new clip types a single-line change
- **Extracted parser clip-tag constant**: Deduplicated `clip_tags` local variable in `_parse_connected_clips` and `_parse_gap_connected_clips` into `_CONNECTED_CLIP_TAGS` module constant
- **Fixed silence marker bypass**: `remove_silence_candidates(mode="mark")` now uses `build_marker_element()` instead of raw `ET.SubElement` — gains input sanitization, fps-aware duration, and correct FCPXML attribute contract
- **Removed dead code**: Deleted unused `_time_to_fcpxml()` wrapper method from `FCPXMLModifier`

## [0.5.14] - 2026-02-25

### Fixed

- **Completed-attribute strict matching**: Added 7 adversarial edge-case tests for `MarkerType.from_xml_element` — whitespace-only `completed`, tab-padded values, leading-zero `'00'`, and Unicode fullwidth digit lookalikes (`０`, `１`). Confirms strict exact-match rejects all non-canonical inputs as STANDARD.

## [0.5.13] - 2026-02-24

### Changed

- **README overhaul**: Fixed stale stats (414→438 tests, 9→10 suites), expanded architecture tree with per-file descriptions for all test suites, added LOC badge, documented unified marker pipeline and security-first validation as key design decisions, added "Recent Highlights" section showcasing marker hardening and cross-NLE export work, updated latest release pointer to v0.5.13

## [0.5.12] - 2026-02-24

### Added

- **17 marker pipeline tests** (`test_marker_pipeline.py`): Direct unit tests for `build_marker_element` shared builder (8 tests), `batch_add_markers` auto_at_cuts and auto_at_intervals modes (4 tests), `_build_clip_index` duplicate-name last-one-wins behavior (2 tests), `write_fcpxml` output format validation (3 tests)
- **Documented `auto_at_cuts` bug**: Test proves `batch_add_markers(auto_at_cuts=True)` fails with `ValueError` when spine contains duplicate clip names — the name-indexed clip dict loses earlier occurrences

## [0.5.11] - 2026-02-24

### Changed

- **Unified marker element construction**: Extracted `build_marker_element()` as a single source of truth for creating marker/chapter-marker XML elements — eliminates duplicated tag selection, attribute setting, and note-guard logic between `FCPXMLModifier.add_marker()` and `FCPXMLWriter._add_marker()`
- **Single-pass marker collection**: `_collect_markers` in the parser now iterates element children once using `MARKER_XML_TAGS` constant instead of calling `findall()` twice (once per tag)
- **New `MARKER_XML_TAGS` constant**: Tuple of recognised marker element tags (`'marker'`, `'chapter-marker'`) exported from models for use across parser and writer modules

## [0.5.10] - 2026-02-24

### Added

- **3 strict whitespace parser tests**: `test_whitespace_padded_completed_zero_is_standard`, `test_whitespace_padded_completed_one_is_standard`, `test_empty_completed_attribute_is_standard` — parser-level defense-in-depth for `from_xml_element` strict matching
- **3 `from_string` whitespace strip tests**: Verifies `from_string("  completed  ")`, `from_string("  todo  ")`, and `from_string("  chapter  ")` all strip correctly before enum lookup
- **2 writer strict attribute tests**: `test_marker_completed_attr_no_whitespace` confirms written `completed` attributes are exact `'0'`/`'1'` with no padding; `test_from_string_whitespace_roundtrip` confirms padded `from_string` input survives write→parse roundtrip

## [0.5.9] - 2026-02-23

### Fixed

- **Strict whitespace matching documented and unit-tested at contract level**: `from_xml_element` now has explicit docstring documenting priority order and strict matching behavior — whitespace-padded completed attributes like `' 0 '` are correctly rejected as STANDARD
- **Chapter-marker tag priority over completed attribute**: Added unit test confirming `<chapter-marker completed="0">` resolves to CHAPTER, not TODO — tag check takes priority
- **Writer docstring listed only 3 of 4 marker types**: `add_marker()` docstring now lists STANDARD, TODO, COMPLETED, and CHAPTER

### Added

- **4 new `from_xml_element` unit tests**: whitespace-padded '0', whitespace-padded '1', empty completed attribute, and chapter-marker-with-completed edge case — closes the gap between integration tests (test_security.py) and unit contract tests (test_models.py)

## [0.5.8] - 2026-02-23

### Changed

- **Consolidated marker serialization contract into `MarkerType`**: New `from_xml_element()` classmethod and `xml_attrs` property centralise the completed-attribute and posterOffset logic that was previously duplicated across parser and both writer classes
- **Unified parser marker methods**: Replaced separate `_parse_marker()` and `_parse_chapter_marker()` with a single `_parse_marker_element()` that delegates type detection to `MarkerType.from_xml_element()`
- **Extracted `_collect_markers()` helper**: Eliminated 4 duplicated `findall('marker') + findall('chapter-marker')` loops in `_parse_clip`, `_parse_one_connected_clip`, and `_parse_project`
- **Both writer paths use `xml_attrs`**: `FCPXMLModifier.add_marker()` and `FCPXMLWriter._add_marker()` now loop over `marker_type.xml_attrs` instead of manual if/elif chains

### Added

- **11 new tests** for `MarkerType.from_xml_element()`, `xml_attrs`, and round-trip symmetry (`TestMarkerTypeXmlContract`)

## [0.5.7] - 2026-02-23

### Fixed

- **Chapter markers on connected clips silently dropped**: `_parse_one_connected_clip` only parsed `<marker>` children, missing `<chapter-marker>` elements entirely — chapter markers placed on B-roll, lower-thirds, or any lane clip were lost during parse. Now parses both marker types, matching `_parse_clip` behavior.

## [0.5.6] - 2026-02-23

### Fixed

- **Marker completed-attribute edge cases**: Added 5 security tests for whitespace-padded (`" 0 "`, `" 1 "`), negative (`"-1"`), and case-variant (`"TRUE"`, `"false"`) completed attribute values — all correctly rejected as STANDARD by the strict parser
- **`from_string` → write → parse round-trip test**: New integration test proving `MarkerType.from_string('todo')` and legacy alias `'todo-marker'` both survive the full write/re-parse cycle as TODO markers

## [0.5.5] - 2026-02-23

### Added

- **TODO/COMPLETED marker tests for FCPXMLWriter**: 4 new tests covering the object-model-to-XML path (`_add_marker`) that was previously untested for task markers — catches regressions in rough cut and export generation
- **Mixed-case `from_string` tests**: 6 parametrized cases ("Todo", "tOdO", "Completed", "cOMPLETED") proving case insensitivity
- **Whitespace + legacy alias combo tests**: 3 cases ensuring " todo-marker " and similar inputs resolve correctly
- **Enum value contract test**: Asserts `.value` properties stay lowercase — they're used as dict keys across the codebase
- **Multi-marker-type parser test**: Verifies all four marker types coexist on one clip without cross-contamination
- **STANDARD marker negative test**: Confirms plain `<marker>` without `completed` attr never becomes TODO

## [0.5.4] - 2026-02-23

### Security

- **Input validation hardening:** `MarkerType.from_string()` now rejects null bytes, control characters, empty strings, and inputs exceeding 64 characters — prevents injection and memory abuse via crafted marker type strings
- **XML value sanitization:** New `_sanitize_xml_value()` helper strips null bytes and control characters from marker names and notes before writing to XML, with configurable length limits (1024 chars for names, 4096 for notes)
- **Parser file size limit:** `FCPXMLParser.parse_file()` enforces a 50 MB file size ceiling before parsing, preventing memory exhaustion from maliciously large XML files
- **Strict completed-attribute validation:** Parser now only accepts `'0'` and `'1'` for the marker `completed` attribute — any other value (e.g. `"true"`, `"yes"`, `"1 OR 1=1"`) falls through to `MarkerType.STANDARD` instead of being misinterpreted
- Added 25 security tests covering all hardening vectors

## [0.5.3] - 2026-02-22

### Added

- **Workflow recipes guide** (`docs/WORKFLOWS.md`): 8 real-world multi-step workflow recipes — delivery QC pipeline, YouTube chapter export, beat-synced music video assembly, cross-NLE handoff, documentary A/B roll, social media reformat, timeline version comparison, silence cleanup
- Each recipe documents the scenario, natural-language prompt, tool chain, and practical notes
- Section on composing tools in AI agent workflows — how to describe multi-tool pipelines in a single prompt
- README now links to workflows guide from Usage Examples section

## [0.5.2] - 2026-02-22

### Fixed

- **Spec drift:** Updated `docs/specs/03_WRITER_PSEUDOCODE.py` and `docs/specs/07_MODELS.py` MarkerType enums to match implementation — old values (`"todo-marker"`, `"completed-marker"`) replaced with correct values (`"todo"`, `"completed"`)
- **Legacy alias support:** `MarkerType.from_string()` now accepts legacy spec values (`"todo-marker"`, `"completed-marker"`, `"chapter-marker"`) and maps them to current enum values, preventing hard failures from stale references
- Added 10 new MarkerType tests covering `from_string` current values, legacy aliases, invalid input, and `xml_tag` mapping (348 total tests)

## [0.5.1] - 2026-02-22

### Changed

- **README rewrite:** Portfolio-grade overhaul — stronger narrative hook (personal story leads), architecture diagram with data flow, consolidated release history (points to CHANGELOG instead of duplicating it), tighter tool tables, updated stats (337 tests, ~7k LOC)
- Roadmap condensed from 27 line items to 12 grouped milestones for scannability
- "Why This Exists" moved from bottom of page to top — emotional hook before technical proof
- Added test badge to header badges

## [0.5.0] - 2026-02-21

### Added

- **Connected Clips:** Full multi-track support — parser extracts B-roll, titles, and audio from secondary lanes (`lane` attribute), secondary storylines (`<storyline>` elements), and gap-attached clips
- **Compound Clips:** Parse and inspect `ref-clip` compound clips with nested timelines
- **Roles Management:** 4 new tools — `list_roles`, `assign_role`, `filter_by_role`, `export_role_stems` for audio/video role workflows
- **Timeline Diff:** `compare_timelines()` engine detects added/removed/moved/trimmed clips, marker changes, transition changes, and format changes between two FCPXMLs
- **Social Media Reformat:** `reformat_timeline` with preset aspect ratios (9:16, 1:1, 4:5, 4:3, 16:9) and custom resolution support
- **Silence Detection:** Heuristic-based `detect_silence_candidates` (gaps, ultra-short clips, name patterns, duration anomalies) and `remove_silence_candidates` (mark or delete modes)
- **DaVinci Resolve Export:** `export_resolve_xml` generates simplified FCPXML v1.9 with compound clip flattening and unsupported attribute stripping
- **XMEML Export:** `export_fcp7_xml` converts spine-based FCPXML to track-based FCP7 XML (XMEML v5) for Premiere Pro / Resolve / Avid
- New dataclasses: `ConnectedClip`, `CompoundClip`, `SilenceCandidate`
- `audio_role` and `video_role` fields on `Clip` dataclass
- `connected_clips` and `compound_clips` lists on `Timeline` dataclass
- 52 new tests (337 total) covering all 6 features
- 13 new tools → 47 total

## [0.4.3] - 2026-02-20

### Changed

- Consolidated three separate marker type lookup patterns (manual dict, `MarkerType[str.upper()]`, enum value match) into single `MarkerType.from_string()` classmethod
- Added `MarkerType.xml_tag` property — eliminates `tag_map` dicts and inline ternaries for XML element name resolution
- `add_marker` and `list_markers` tool schemas now expose `"completed"` as a valid marker type
- Parser `_parse_clip()` now finds `<chapter-marker>` elements on clips (previously only `<marker>` was parsed at clip level)
- Tightened TODO marker detection: `completed` attribute must be exactly `"0"` (was `is not None`, which matched any value)
- `FCPXMLWriter._add_marker` now emits `posterOffset="0s"` on chapter markers (matches `FCPXMLModifier` behavior)
- `FCPXMLModifier.add_marker` no longer adds `note` attribute to chapter markers (invalid FCPXML, FCP ignores them)

## [0.4.2] - 2026-02-18

### Fixed

- TODO markers now set `completed="0"` attribute so they survive round-trip (save → re-parse) without degrading to STANDARD markers
- COMPLETED markers (`completed="1"`) are now correctly distinguished from TODO markers (`completed="0"`) during parsing — previously both were mapped to TODO
- FCPXMLWriter generator now emits `completed` attribute for TODO and COMPLETED markers
- `list_markers` tool now supports filtering by "completed" marker type

## [Unreleased]

### Added

- 285 unit tests across 7 test files covering models, parser, writer, server handlers, and rough cut generation
- GitHub Actions CI pipeline with linting (ruff) and test execution
- MCP registry metadata files for discoverability

### Fixed

- Import sort order in test files for ruff I001 compliance

### Security

- Add path validation to all 34 tool handlers — blocks path traversal, null bytes, symlink attacks, and oversized files (100 MB limit)
- Enforce file extension whitelists: `.fcpxml`/`.fcpxmld` for projects, `.json` for beats, `.srt`/`.vtt` for subtitles
- Validate output paths to prevent writing to arbitrary filesystem locations
- Harden error handler to avoid leaking internal paths and stack traces in unexpected errors

## [0.4.0] - 2026-02-05

### Added

- 5 pre-built MCP prompt workflows: QC check, YouTube chapters, rough cut, timeline summary, cleanup
- MCP resources for automatic FCPXML file discovery in project directories
- SRT/VTT subtitle import as timeline markers (`import_srt_markers`)
- YouTube chapter/transcript import as markers (`import_transcript_markers`)
- Server architecture refactored to dispatch-dict pattern (`TOOL_HANDLERS`)

## [0.3.0] - 2026-01-20

### Added

- AI-powered rough cut generation from source clips (`auto_rough_cut`)
- Montage generator with pacing curves: accelerating, decelerating, pyramid (`generate_montage`)
- A/B roll generator for documentary-style edits (`generate_ab_roll`)
- Beat sync tools: `import_beat_markers`, `snap_to_beats`
- Flash frame detection and auto-fix (`detect_flash_frames`, `fix_flash_frames`)
- Duplicate clip detection (`detect_duplicates`)
- Gap detection (`detect_gaps`, `fill_gaps`)
- Timeline validation with health score (`validate_timeline`)
- Batch rapid trim (`rapid_trim`)
- Speed change tool (`change_speed`)
- Clip splitting at timecodes (`split_clip`)
- Clip deletion with ripple support (`delete_clips`)
- Transition insertion: cross-dissolve, fade, wipe (`add_transition`)

## [0.2.0] - 2026-01-20

### Added

- Library clip listing (`list_library_clips`)
- Timeline clip insertion from library (`insert_clip`)
- Clip trimming with ripple (`trim_clip`)
- Clip reordering with ripple support (`reorder_clips`)
- Batch marker operations (`batch_add_markers`)
- Pacing analysis with suggestions (`analyze_pacing`)
- Keyword listing and selection (`list_keywords`, `select_by_keyword`)
- EDL export (`export_edl`)
- CSV export (`export_csv`)

## [0.1.0] - 2026-01-18

### Added

- Initial release — first MCP server for Final Cut Pro
- FCPXML parser supporting versions 1.8–1.11
- Timeline analysis (`analyze_timeline`)
- Clip listing with timecodes (`list_clips`)
- Marker listing (`list_markers`)
- Short cut and long clip detection (`find_short_cuts`, `find_long_clips`)
- Single marker insertion (`add_marker`)
- Project file discovery (`list_projects`)
- Python data models: TimeValue (rational time arithmetic), Timecode, Clip, Timeline, Project
