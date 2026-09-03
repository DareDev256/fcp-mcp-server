# Cutting Room — design spec

**Date:** 2026-09-01
**Target releases:** v0.17.0, v0.18.0, v0.19.0
**Status:** approved for planning
**Supersedes the open items of:** `docs/CAPABILITY-AUDIT-2026-06.md` §5 (v0.10 tail, v1.0)

---

## 1. Problem

The server is capable and blind. It has 62 operations over FCPXML 1.8–1.14, local
Whisper transcription, real silence and beat detection, and a zero-click push into a
running Final Cut Pro. What it does not have is a **loop**. Today the operator:

1. exports XML by hand,
2. prompts,
3. reads a wall of text describing changes they cannot see,
4. imports,
5. looks at Final Cut Pro to find out what actually happened.

Three structural gaps produce that experience, and no amount of new operations fixes
any of them:

| Gap | Consequence |
|---|---|
| **Read-back asymmetry.** Apple ships a fully scriptable import (`odoc` + `<import-options>`) and *no* programmatic export. Verified across FCP 11.0 → 12.2: zero new automation hooks in six releases. | Every iteration requires a human `File → Export XML`. "Seamless" is impossible by construction until this is worked around. |
| **No visual feedback.** `fcpxml/preview.py` renders an HTML bar chart of clip extents. There is no frame, anywhere, ever. | The operator cannot evaluate a cut without opening Final Cut Pro, which defeats the point of driving it from a terminal. |
| **No index, no streaming.** Every tool call re-parses the FCPXML from disk. Long operations (transcription, media silence scan) return one blob after minutes of silence with no progress signal. | Felt latency is the product. A two-minute silent hang reads as broken. |

Independently, the competitive ground moved. The June 2026 audit's risk #1 —
"SpliceKit revives" — has fired. SpliceKit is active again under Elliott Tate and
Chris Hocking (LateNite/CommandPost), shipping ~200 MCP tools that drive Final Cut
Pro's ObjC runtime in-process via an injected dylib. It owns the raw-power axis. This
spec deliberately does not contest that axis.

### What the domain research says

Surveyed 2026-09-01 across professional-editor practice writing. The findings are
consistent and they constrain the design:

- AI reliably wins at the **pre-edit layer**: logging, transcription, search, silence
  and filler removal, multicam sync, assembly cut. That is roughly 80% of edit time
  and it is mechanical.
- AI reliably loses at narrative and emotional judgement, brand-specific creative
  vision, and projects where the story is discovered in the edit.
- The single loudest complaint about AI assemblies is **shot redundancy** — similar
  shots or soundbites selected repeatedly, making the sequence monotonous.
- Every credible hybrid workflow mandates a **review checkpoint before export**:
  the assembly is watched in full before it leaves the machine, no exceptions.

That last point is load-bearing. The proxy renderer in Layer 2 is not a convenience
feature. It is the review checkpoint, and it is what makes a generative assembly
acceptable to an editor who does not want a machine deciding what the cut means.

---

## 2. Goals

1. Close the round-trip so an operator can prompt, see the result, and iterate without
   leaving the terminal.
2. Make the second and subsequent prompts on a project feel instantaneous.
3. Make every long operation legible while it runs.
4. Make every write reversible.
5. Own the pre-edit layer — log, search, assemble, verify — on an unmodified Mac.

## 3. Non-goals

- **No binary patching, no injection, no re-signed Final Cut Pro.** Portability and
  safety are the brand. SpliceKit and CommandPost are *detected and adapted to*, never
  bundled or required.
- **No creative authority.** The server does not decide what a cut means. It proposes,
  renders the proposal, and waits.
- **No CapCut.** Unchanged from the June audit: crowded lane, encrypted format,
  trademark risk.
- **No `.fcpbundle` writes.** Library organization is done through the FCPXML
  round-trip only. Writing into a live library package is the one operation here that
  could destroy a user's work, and it is out of scope.
- **No bet on Apple opening automation.** Two major versions, zero new hooks.

---

## 4. Architecture

Six layers. Each is independently useful; each depends only on the layers above it.

```
  Layer 5  organize · journal · review gate        trust
  Layer 4  scenes · find (VLM) · diversity         moat
  Layer 3  index (SQLite) · progress streaming     speed
  Layer 2  preview render/sheet/timeline · autopush  eyes
  Layer 1  watch (folder + bridge adapters)        the loop
  Layer 0  tools/ split · operation Protocol       the seam
  ------------------------------------------------
           existing: parser · writer · rough_cut · diff ·
           export · media_intel · transcribe · live · dtd
```

### Layer 0 — the seam

`server.py` is 4,585 lines holding every tool definition, every handler, the seven
`TOOL_GROUPS`, the QC detection helpers and the resource/prompt registration. Five new
subsystems cannot land on that without it becoming unmaintainable.

**Work:**

- Split `server.py` into a `tools/` package, one module per tool group, mirroring the
  existing `TOOL_GROUPS` keys: `tools/inspect.py`, `diagnose.py`, `edit.py`,
  `mark.py`, `generate.py`, `transcript.py`, `deliver.py`, plus the new
  `watch.py`, `preview.py`, `find.py`, `organize.py`. `server.py` retains only entry
  point, registration, and the dispatch tables.
- Extract an **operation Protocol** over `FCPXMLModifier`'s ~30 public methods. The
  June audit identified this as the de facto operation vocabulary; handlers are already
  thin (`_setup_modifier → method → save → _text_result`). Make `_setup_modifier`
  backend-selecting so a future non-XML backend is a drop-in rather than a rewrite.
- Unify the time representation. The read path uses frame-quantized `Timecode`; the
  write path uses rational `TimeValue`. Converge on `TimeValue` everywhere, with
  `Timecode` retained strictly as a display formatter.

**Constraint:** `TOOL_GROUPS` and `TOOL_HANDLERS` must keep their current shape and the
existing test that asserts every group action resolves to a real handler must keep
passing unchanged. The seven advertised verbs and the `FCP_MCP_LEGACY_TOOLS=1` flat
surface are public API; this refactor is invisible from outside.

### Layer 1 — `watch`

New tool group. Closes the read-back loop without an unofficial surface.

| Action | Behaviour |
|---|---|
| `watch_start` | Begin observing a directory (default: the configured FCP export destination). Records a baseline of existing files. |
| `watch_status` | Report observer state, watched path, and the last detected export. |
| `watch_stop` | Stop the observer. |
| `watch_pull` | Block until a new or modified `.fcpxml`/`.fcpxmld` appears (bounded timeout, default 120s), then parse it and diff against the last known state for that project using the existing `fcpxml/diff.py`. |

Implementation: `watchdog` is added as a dependency of the `watch` extra. Absent it,
`watch_pull` degrades to polling on `stat` mtime at 1s intervals — functional, slightly
lazier, no hard dependency for the core install.

**Bridge adapters.** A `fcpxml/bridges.py` module probes, on demand and never at
import time:

- SpliceKit JSON-RPC on `127.0.0.1:9876`
- CommandPost WebSocket on `127.0.0.1:27480`

When one answers, `watch_pull` can *trigger* the export itself, making the loop
hands-free. When neither answers — the default assumption — the server prints the one
keystroke the operator needs and waits. Detection is a bounded-timeout connect with the
result cached for the session; a probe failure is never an error, only a downgrade to
the manual path. No adapter is ever a required dependency, and neither tool is bundled,
vendored, or installed by us.

Paired with the existing `push_to_fcp`, this is a genuine round-trip: push in, edit,
export, diff, push in again.

### Layer 2 — `preview`

New tool group. Gives the loop eyes.

| Action | Behaviour |
|---|---|
| `preview_render` | Compile the timeline into an ffmpeg `trim`/`concat`/`xfade` filtergraph and render a low-resolution proxy (default 480p, configurable). Writes to a cache directory, returns the path, and auto-opens it beside the terminal. |
| `preview_sheet` | Extract one representative frame per cut and tile them into a contact sheet. Reads a cut's rhythm at a glance for a fraction of the render cost. |
| `preview_timeline` | Text timeline rendered for the terminal: spine and lanes, cut positions, durations, markers. Recomputed and reprinted after each write when auto-preview is on. |
| `preview_frame` | Single frame at a given timeline position. |
| `preview_check` | Filmstrip **plus audio waveform** across a time range, sampled from the **source media**, with word labels from the transcript where one exists. The verification instrument for a specific cut. |

**Why `preview_check` is separate from `preview_render`.** `preview.py` today draws
coloured blocks from the XML — a picture of what we *wrote*, not of what the media
*looks like*. `fix_flash_frames` and `remove_media_silence` are therefore currently
verified by re-parsing our own output, which is precisely the failure mode the project's
own rule names: a check that cannot see the failure certifies nothing. `preview_check`
reads the media. Borrowed directly from `browser-use/video-use`, which renders a
filmstrip + waveform + word-label PNG at every cut boundary and looks at it before
showing the user anything. We already shell ffmpeg as a bounded subprocess in
`media_intel.py`, so this adds no dependency.

**Compilation rules.** The filtergraph is built from the same `TimeValue` rationals the
writer uses, so preview timing is exact rather than approximate. Connected clips on
positive lanes composite over the spine; negative lanes mix into the audio graph.
Transitions map to `xfade` where an equivalent exists and to a hard cut where none
does — the substitution is reported, never silent. Clips whose media is missing render
as a labelled slate rather than failing the whole job.

**Delivery to the terminal.** Rendered artifacts are handed to
`~/.claude/hooks/cmux-image-preview.mjs`, which paints them into a pane beside the
terminal. A path printed in chat is not showing the operator the thing.

**Auto-push mode.** `FCP_MCP_AUTOPUSH=1` makes every write operation additionally fire
`push_to_fcp`, so Final Cut Pro updates as the operator prompts. Off by default:
repeated imports accumulate library churn, and that is the operator's call to make, not
a default to inflict.

**Degradation.** ffmpeg absent → every `preview` action returns a clear instruction to
install it and the rest of the server is unaffected. This mirrors how `media_intel`
already handles a missing ffmpeg.

### Layer 3 — index and streaming

**The index.** A SQLite database at `~/.fcp-mcp/index.db`, one row-set per library.

```sql
CREATE TABLE media (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  duration_num INTEGER, duration_den INTEGER,
  fps_num INTEGER, fps_den INTEGER,
  width INTEGER, height INTEGER,
  indexed_at REAL NOT NULL
);

CREATE TABLE transcript (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  start_num INTEGER, start_den INTEGER,
  end_num INTEGER, end_den INTEGER,
  text TEXT NOT NULL,
  speaker TEXT
);

CREATE TABLE analysis (             -- beats, silences, scene cuts
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- 'beat' | 'silence' | 'scene'
  start_num INTEGER, start_den INTEGER,
  end_num INTEGER, end_den INTEGER,
  payload TEXT                      -- JSON, kind-specific
);

CREATE TABLE shot (                 -- Layer 4
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  start_num INTEGER, start_den INTEGER,
  end_num INTEGER, end_den INTEGER,
  caption TEXT,
  embedding BLOB                    -- float32 vector
);
```

Every time value is stored as an integer numerator/denominator pair. Floats never
enter the index; that is the same invariant the rest of the codebase already holds.

Invalidation is by `(path, mtime, size)`. A changed source file drops its dependent
rows and re-analyses on next access. The index is a **cache and never a source of
truth**: any tool must produce a correct answer with the database deleted, only more
slowly. A test asserts this by running the suite twice, once with the index disabled.

**Streaming.** Any operation whose expected duration exceeds ~2 seconds emits MCP
progress notifications: transcription per segment, media silence scan per clip, preview
render per ffmpeg progress line, index build per file. Where the MCP SDK version in use
does not expose progress, the notification is dropped silently — `fcpxml/mcp_compat.py`
already owns exactly this kind of 1.x/2.x divergence and gains one more probe.

### Layer 4 — the moat

**`transcript` extensions** — the reading surface, not just the data.

- `transcript_pack` — collapse N sources into a single packed markdown view, phrase
  lines broken on silence >= 0.5s or speaker change, each prefixed `[start-end] S0`.
  `video-use` gets an entire multi-take shoot into roughly 12KB this way, which is what
  makes a model able to *reason over the whole shoot* rather than one file at a time.
  Our `transcribe.py` is per-file spans with no packed multi-source view.
- **Diarization and audio events.** An optional ElevenLabs Scribe backend alongside the
  local faster-whisper default, adding speaker labels and audio events — `(laughter)`,
  `(applause)`, `(sigh)`. Those are cut signals, not decoration: they feed
  `auto_rough_cut` and `detect_beats` directly. The local backend stays the default and
  the only one that requires no network; Scribe is opt-in and states plainly that audio
  leaves the machine.

**`scenes`** — shot-boundary detection. PySceneDetect 0.7 as the default detector
(content and adaptive modes), with TransNetV2 as an optional heavier backend for hard
content. Results land in `analysis` as `kind='scene'`. Feeds `preview_sheet`, marker
import, and selects-reel assembly.

**`find`** — semantic search over source media, and the reason to build any of this.

| Action | Behaviour |
|---|---|
| `find_index` | For each detected shot, sample frames, caption them with a local vision-language model, embed the caption, store both in `shot`. Idempotent and resumable; interrupting it loses at most one shot. |
| `find_shots` | Natural-language query → ranked shots with source paths and exact timecodes. |
| `find_to_timeline` | Same query, but assemble the hits directly into a selects reel via the existing `rough_cut` path. |

**`find` is a ROUTER, not an indexer.** Google shipped agentic video
understanding in Gemini on 2026-09-01: rather than ingesting media at a fixed
frame rate, the model decides what to watch, at what speed, and through which
modality — frames, audio, or transcript — fetching only the moments it needs.
Reported: up to 66% lower cost, up to 88% fewer tokens, and *higher* accuracy.

That is a direct verdict on the first draft of this layer, which said "caption
every shot, embed, index." Uniform ingestion is exactly the static processing
being deprecated, and on a 2TB shoot it is hours of compute spent captioning
footage no query will ever reach.

The revision costs nothing to build, because the cheap modalities are already
in the index. `find_shots` reads them in ascending order of cost and stops as
soon as the query is answered:

1. **Transcript text** — free, already stored. Most queries about an interview
   or a podcast terminate here.
2. **Scene and analysis metadata** — shot boundaries, beats, silences. Narrows
   a query to candidate windows without decoding a frame.
3. **Frames, via the VLM** — only for the windows that survive step 2, and only
   when the query is genuinely visual.

An hour of footage becomes on the order of a dozen VLM calls instead of several
hundred. `find_index` stops being a mandatory up-front pass and becomes an
optional warm-up. Every result states which tier answered it, because a hit
from transcript text and a hit from frame analysis are different kinds of
evidence and must not read the same.

`preview_check` (Layer 2) is already an instance of this pattern — it fetches
one narrow window through exactly two modalities rather than ingesting the
timeline. The Gemini announcement names the principle the codebase had
stumbled into; this makes it the rule for the whole layer.

The model runs locally through `mlx-vlm` on Apple silicon (InternVL3 / Qwen-VL class),
under the `find` extra. No frames, captions, embeddings or media ever leave the
machine. The model identifier is configuration, not a hard-coded constant, so the
choice can move as the field moves without a code change. Without the extra installed,
`find_shots` falls back to searching transcript text and shot metadata — degraded but
useful, and it says which mode it is in.

**Diversity constraint.** Shot redundancy is the loudest documented complaint about
AI-assembled cuts. `tests/test_diversity.py` already encodes the idea as a test; this
promotes it to an enforced constraint in the assembly path — a configurable minimum
separation between reuses of the same source, and a similarity ceiling between adjacent
shots once embeddings exist. Assemblies report their diversity score.

### Layer 4b — the EDL bridge

New action on `generate`: **`import_edl_json`** — read an `edl.json` of the shape
`browser-use/video-use` emits and author a real FCPXML from it.

This is small (`writer.py` already does the hard part) and it is the sharpest wedge in
this document. `video-use` is a Claude Code skill with ffmpeg helpers whose pipeline
terminates at `render.py` -> `final.mp4`. It never touches an NLE. It solves the half we
skip; we solve the half it skips. The bridge means its reasoning can finish *in Final
Cut Pro* instead of dead-ending in a flat file — which is the only outcome that works
for anyone who has to deliver a project file rather than a video. Every colorist and
finishing editor is in that bucket.

Deliberately **not** copied from `video-use`: the Manim/Remotion overlay machinery
(Final Cut does titles better) and the render loop generally. Burning subtitles and
grades into pixels is the opposite of what an FCPXML round-trip exists to preserve.

### Layer 5 — organization and trust

**`organize`** — batch logging through the FCPXML round-trip.

| Action | Behaviour |
|---|---|
| `organize_keywords` | Add, remove, or replace keyword ranges across many clips by filter. |
| `organize_rate` | Set favorite/rejected ranges in bulk. |
| `organize_roles` | Bulk role and subrole assignment. |
| `organize_auto` | Derive keywords from Layer 4 captions and transcript content, propose them, apply on confirmation. |

All of it is non-destructive: read XML, modify, write a suffixed copy, hand back for
import. The live library package is never touched.

**Operation journal.** Every write operation appends a record to
`~/.fcp-mcp/journal/<project-hash>.jsonl`: timestamp, tool, action, arguments, input
file hash, output path, output file hash. Two new actions:

- `history` — show the last N operations on a project.
- `undo` — revert the last N by restoring the recorded prior output. Because every
  operation already writes a new suffixed file and never mutates its input, undo is a
  pointer move, not a reconstruction.

**Review gate.** `deliver` refuses to run when the current timeline state has no
corresponding rendered preview, unless `confirm_unreviewed=true` is passed. The refusal
message names the exact `preview_render` call that would satisfy it. This is the
editors' "watch it in full before it leaves" checkpoint, encoded where it cannot be
forgotten rather than written in a README.

---

## 5. Dependencies

Core install stays at `mcp` and `defusedxml`. Everything new is an extra, and every
extra degrades to a clear message rather than an import error.

| Extra | Adds | Used by |
|---|---|---|
| `watch` | `watchdog>=4.0` | Layer 1 (polling fallback without it) |
| `intelligence` | `librosa` *(existing)* | beats |
| `transcribe` | `faster-whisper` *(existing)* | transcription |
| `scenes` | `scenedetect>=0.7` | Layer 4 scene detection |
| `find` | `mlx-vlm`, `numpy` | Layer 4 semantic search |

ffmpeg remains an external binary, probed at call time with `shutil.which`, exactly as
`media_intel` does today. SQLite is stdlib.

---

## 6. Error handling

The governing rule, from the project's own hard-won history: **the instrument must be
able to see the failure.** A check whose passing and failing readings are identical
certifies nothing.

- **Missing external tool** (ffmpeg, a model) → named, actionable message identifying
  the tool and the install command. Never a stack trace, never a silent no-op.
- **Missing media** → reported per clip and skipped; the operation completes over what
  it could reach and states what it could not. Established pattern in `media_intel`.
- **Bridge unreachable** → not an error. Downgrade to the manual export path and say
  so once.
- **Filtergraph substitution** (unsupported transition → hard cut) → reported in the
  render summary.
- **Index corruption or schema drift** → drop and rebuild. The index is a cache.
- **Subprocess bounds** → every ffmpeg and model invocation carries an explicit timeout
  and a bounded output read, matching the existing `detect_silence` treatment.
- **Progress unsupported by the SDK** → silently dropped, and the operation still
  completes. Never a hard failure.

---

## 7. Security

Extends `SECURITY.md`, does not amend it.

- Sandbox roots continue to confine every read and write, including the new cache and
  index directories.
- The index and journal live under `~/.fcp-mcp/` at mode 700. They contain transcript
  text and file paths from the user's media, which is user content and stays local.
- Bridge probes connect to loopback only, with a bounded timeout. No remote host is
  ever contacted by Layer 1.
- `find_index` runs the vision model in-process on local frames. No network egress.
  A test asserts no outbound socket is opened during indexing.
- ffmpeg command construction uses argument lists, never shell strings. No user-supplied
  value reaches a shell.
- The journal records file hashes and paths, never media content.

---

## 8. Testing

The suite is at 1337 tests across 30 files. Every layer lands with tests in the
established style, and CI keeps running against both the declared `mcp` floor and 2.x.

| Layer | Coverage |
|---|---|
| 0 | Group/action resolution unchanged; `TimeValue` unification proven by round-tripping all six broadcast rates including the NTSC-fractionals; public tool surface byte-identical before and after the split. |
| 1 | Watch fires on a real file write; diff correctness against a known pair; bridge probe returns *unavailable* cleanly with nothing listening; polling fallback exercised with `watchdog` absent. |
| 2 | Filtergraph is asserted against expected output for known timelines, including lanes and transitions; render is skipped without ffmpeg; a rendered proxy's actual duration is read back from the file and compared against the timeline's rational duration — not merely that a file exists and not by its byte count. `preview_check` proven by a mutation test: introduce a flash frame, confirm the emitted filmstrip differs — an instrument that reads identically on a good and a bad cut is rejected. |
| 3 | Whole suite passes twice, once with the index disabled, proving the cache is never load-bearing. Invalidation asserted by mutating mtime. Progress notifications asserted on both SDK versions. |
| 4 | Scene detection against a fixture with known cuts; `find` fallback path with the extra absent; diversity constraint proven by a mutation test — remove the constraint and a redundancy assertion must go red. |
| 5 | Journal append/replay; undo restores a byte-identical prior file; the review gate is proven by a mutation test — delete the gate and a `deliver`-without-preview test must fail. |

Every guard added by this spec gets a mutation check. A guard whose deletion leaves the
suite green is not a guard.

---

## 9. Phasing

Three releases, each usable on its own.

**v0.17.0 — the loop.** Layer 0, Layer 1, Layer 2, plus Layer 4b (`import_edl_json`,
which is small and unblocks the `video-use` bridge immediately).
Ships when: an operator can prompt an edit, see a rendered proxy beside the terminal,
**confirm a flash-frame fix from a `preview_check` image rather than from the XML**,
push it into Final Cut Pro, export, and have the change detected and diffed — without
opening a file browser.

**v0.18.0 — speed and sight.** Layer 3, Layer 4 `scenes`, `transcript_pack` and the
optional diarization backend.
Ships when: the second prompt on a project returns in well under a second, and every
operation over two seconds streams progress.

**v0.19.0 — the moat and the ledger.** Layer 4 `find`, Layer 5.
Ships when: a natural-language query over an indexed shoot returns correct timecodes,
every write is reversible, and `deliver` refuses an unreviewed cut.

Each release carries the standard git protocol: README, CHANGELOG entry, version bump,
GitHub release.

**Plan scope.** This spec describes all three releases so the shape is visible end to
end, but only **v0.17.0 (Layers 0–2)** goes into the first implementation plan.
v0.18.0 and v0.19.0 each get their own plan, written after the release before them
ships, so that real usage informs them rather than this document's guesses.

---

## 10. DONE WHEN

| Gate | Satisfied by |
|---|---|
| **runs** | Server starts on `mcp` floor and 2.x; every new group advertised; CI green on both. |
| **proves** | Proxy duration read back from the rendered file and compared to the timeline rational. Round-trip diff asserted against a known-modified pair. |
| **fails loud** | Missing ffmpeg, missing model, unreachable bridge, missing media — each produces a named message identifying what is absent and what to do. Nothing degrades silently. |
| **refuses** | `deliver` refuses an unreviewed timeline. `find_shots` refuses to imply semantic search when running in transcript-fallback mode; it names the mode. |
| **admits age** | Index rows carry `indexed_at`; results built on stale analysis state their age against the source mtime. |
| **mutation** | Delete the review gate, the diversity constraint, or the index-invalidation check, and a specific named test goes red for each. |

---

## 11. Risks

1. **SpliceKit expands into the pre-edit layer.** Mitigation: it is injection-bound and
   therefore unavailable on managed Macs and broken by every FCP update. This runs
   unmodified everywhere. Ship the semantic index before the gap closes.
2. **ffmpeg filtergraph complexity on dense timelines.** A 250-clip multi-lane music
   video is a large graph. Mitigation: render in segments and concatenate; cap the
   default preview resolution; make `preview_sheet` the cheap default and
   `preview_render` explicit.
3. **VLM indexing cost on large shoots.** Hours of footage is a long first pass.
   Mitigation: idempotent and resumable, streamed progress, one shot per detected scene
   rather than per frame, and an explicit up-front estimate before it starts.
4. **Refactor regression in Layer 0.** Mitigation: the public tool surface is asserted
   identical before and after, and Layer 0 ships with no behavioural change at all.
5. **Hosted agentic video understanding outruns the local tier.** Gemini's
   API does this today at a cost and accuracy the local path will not match.
   Mitigation: keep the routing policy — which is the actual value — provider
   agnostic, so a Gemini backend can slot in beside `mlx-vlm` as an opt-in that
   states plainly that frames leave the machine, exactly as the Scribe
   transcription backend does. Local stays the default; the promise that
   nothing leaves the machine is only worth anything if it holds by default.
6. **`video-use` grows an FCPXML writer itself.** Seven commits, ~2.4k lines, moving
   fast. Mitigation: an FCPXML writer that survives real libraries is years of format
   depth, not a weekend — but the window is open now, so ship `import_edl_json` in
   v0.17.0 rather than holding it for v0.19.0.
7. **Apple changes FCPXML or the import event.** Mitigation: unchanged — validate
   against the DTDs shipped inside the app bundle, which remain the only real spec.
