# Capability Audit & Dual-Mode Roadmap — June 2026

> Produced 2026-06-11 from a 36-agent research sweep (8 researchers + 28 adversarial
> verifications: 22 confirmed, 6 partially-true, 0 refuted). Every load-bearing claim
> below was independently verified against primary sources — including the FCP 12.2
> binary installed on this machine (sdef dump, bundled DTD diff, Info.plist).

---

## 1. Where the ecosystem moved (Feb → Jun 2026)

While this repo idled after v0.7.0 (last push 2026-05-02), the FCP automation space
restructured:

| Event | Date | Why it matters |
|---|---|---|
| **Final Cut Pro 12.0** shipped (FCPXML **1.14**, Transcript Search, Visual Search, Beat Detection) | 2026-01-28 | We emit/target 1.11. FCP 12.2 (Apr 9) is current. Apple is absorbing low-end AI features natively. |
| **SpliceKit** launched (elliotttate + Chris Hocking/FCP Cafe) | 2026-03-30 | Live in-process FCP control via dylib injection. 221 MCP tools. Took the "most powerful" crown. |
| **CommandPost PR #3514** (MCP server over its WebSocket) opened, then abandoned | 2026-03-19 | Author pivoted to SpliceKit. Maintainer still "intends" native MCP. Unmerged. |
| **dreliq9/fcp-mcp** created — names THIS repo in its README as the baseline it beats | 2026-04-23 | "Where the competition stops at one layer." FCPXML engine + AppleScript live layer + ffprobe. 0 stars, but the positioning is public. |
| **SpliceKit went dormant** (no commits/releases since 2026-04-28; June issues unanswered) | 2026-04-28 → | The "live control" crown is sitting on an unmaintained injection layer. |

**Honest position today:** this repo is the most capable *pure-FCPXML* MCP server,
not "the most powerful AI editing MCP in the FCP ecosystem." SpliceKit owns the raw
power axis. The winnable axis is **safety + portability + media intelligence** —
no binary patching, runs on managed Macs, works without FCP installed, survives
every FCP update. Nobody else credibly occupies that quadrant *and* has format depth.

---

## 2. The control-surface map (verified)

### Official Apple surfaces (FCP 12.2, verified on-machine)

| Surface | Can | Cannot |
|---|---|---|
| **AppleScript dictionary** (`ProEditor.sdef`) | Enumerate open libraries → events → projects/sequences: names, IDs, durations, frame rates (100% read-only, sole command is `get`) | Create, modify, delete, export — anything |
| **Apple-event import** (`open` / odoc) | **Zero-click programmatic import** of `.fcpxml`/`.fcpxmld` with `<import-options>` (library location, copy assets, suppress warnings, base url). Officially documented. | Choose *where* in an open timeline content lands |
| **Workflow Extensions** (ProExtensionHost 1.1) | Floating panel inside FCP; read active sequence + playhead; observe selection/sequence changes; **movePlayhead(to:)** (the only write); drag media/FCPXML into timeline; host can network | Add clips, edit timeline content, modify sequences (explicitly excluded by Apple) |
| **FxPlug 4.3.4** | Render-graph plugins (effects/transitions/titles), timing/keyframe/project info | See or touch edit decisions entirely |
| **URL schemes / CLI / App Intents** | **Nothing — none exist** (verified: no CFBundleURLTypes, no appintents metadata, no headless mode) | — |

**The structural asymmetry: import is fully scriptable; there is NO official
programmatic export.** Reading back the user's current timeline requires a human
`File → Export XML` click — or an unofficial surface. Apple added **zero** new
automation hooks across FCP 11.0 → 12.2 (six releases). Do not bet on Apple opening up.

### Unofficial surfaces

| Route | Capability | Fragility | State |
|---|---|---|---|
| **SpliceKit** (MIT) — dylib-injected re-signed FCP copy, full ObjC runtime (78K classes: Flexo/Ozone/TimelineKit…), JSON-RPC on `127.0.0.1:9876` | Everything: blade, retime, color, effects, import/EXPORT, render, playback, 221 MCP tools | Breaks every FCP update; re-signed binary loses entitlements; DMCA §1201(f) asserted not tested; enterprise Macs will refuse it | **Dormant since 04-28** |
| **CommandPost** (MIT, 554★, v2.0.5 Feb 2026) — AXUIElement + per-version string tables; **built-in WebSocket server, port 27480**, executes any registered action by ID | Deep: timeline, full color suite, every inspector, export dialogs (incl. triggering Export XML via UI), media import. Production-proven (Apple's own WWDC videos graded through it) | Needs maintainer fix after essentially every FCP release; **v2 requires a paid LateNite app installed** (~US$10); v1 free but frozen | Alive |
| Raw AX / Keyboard Maestro / Hammerspoon | Menu clicks, shortcut chains | Brittle, shallow; CommandPost supersedes this for FCP | — |

### What FCPXML itself can never carry (the XML-only ceiling, DTD-verified)

- **Magnetic Mask** — Apple docs: "not included in XML exports." Period.
- **Transcript / visual-analysis / beat-detection data** — lives in the library, not the XML. 1.14's only additions are smart-collection *search* hooks referencing it.
- **Effect definitions** — referenced by uid; must exist on the importing machine.
- **Object-tracker + Cinematic depth data** — round-trips ONLY via `.fcpxmld` bundle sidecars (`dataLocator → locator`). Flat `.fcpxml` silently drops them.
- Render files, optimized media, undo history.

Fully round-trippable: clips/spines/lanes, multicam, sync clips, compounds,
**captions** (real CEA-608/ITT elements — Transcribe-to-Captions output lands here),
markers/keywords/roles, keyframed params, adjust-* intrinsics, filters with params.

**DTD version deltas are small** (1.12: filter nameOverride, optical-flow FRC;
1.13: stereo-3D/spatial, hidden-clip-marker, HFR conform; 1.14: smart-collection
search only — zero new timeline elements). Bumping support is cheap; the ground-truth
DTDs ship inside `/Applications/Final Cut Pro.app/.../Interchange.framework/Resources/`
(FCPXMLv1_0.dtd … FCPXMLv1_14.dtd). Apple's online FCPXML reference still documents
1.10 — the app bundle is the only real spec.

---

## 3. Competitive landscape

| Project | Stars | Approach | Status |
|---|---|---|---|
| samuelgursky/**davinci-resolve-mcp** | 1,217 | Real Resolve Python API; 341 granular tools | Active — shows the ceiling when an NLE has an official API (~20× FCP traction) |
| elliotttate/**SpliceKit** | 81 | Injection (live, everything) | Dormant 6 wks |
| **DareDev256/fcp-mcp-server** | 51 | Pure FCPXML | This repo |
| FireRed-**OpenStoryline** | 2,895 | Intention-driven editing agent (FCPXML as output) | Agentic rough-cut wave |
| DozaVisuals/**doza-assist** | 46 | Learns editing style from user's past cuts | Pushed daily |
| elliotttate/finalcutpro-mcp | 7 | JXA/System Events, 99 tools | Superseded by SpliceKit |
| Poechant/final-cut-pro-cli | 2 | FCPXML + .fcpbundle FS + minimal AppleScript | v0.1.0 May 27 |
| dreliq9/**fcp-mcp** | 0 | FCPXML + AppleScript live + ffprobe — "the most capable FCP MCP server" | Names us as the incumbent |
| OTIO fcpx adapter | 7 | — | **Dead** (last commit Jun 2024, no modern FCPXML) — OTIO is not a viable FCPXML path; we are more current than the "standard" |
| Eddie AI (commercial) | — | AI assembles → XML handoff → editor finishes in NLE | Validates our exact workflow pattern at NAB scale |

Python FCPXML competition is near zero (PyPI `fcpxml` is a placeholder; OTIO adapter
stale). The Swift side is livelier (pipeline-neo v2.5.2 validates DTDs 1.5–1.14;
orchetect/swift-fcpxml) — useful as reference implementations, not dependencies.

**CapCut verdict (asked 2026-06-11): lane crowded — don't build it.** ≥8 CapCut MCP
servers exist; VectCutAPI (1,977★) and capcut-mate (1,156★) push daily atop
pyJianYingDraft (3,425★). No official API; CN builds encrypt drafts; ByteDance
enforced its trademark (CapCutAPI → VectCutAPI forced rename). FCPXML is a documented,
stable Apple format — the opposite. Stay here.

---

## 4. Dual-mode architecture (decision)

Three tiers sharing one operation vocabulary. The repo audit found the de facto seam:
`FCPXMLModifier`'s ~30 public methods ARE the operation set; handlers are thin
(`_setup_modifier → method → save → _text_result`). Extract a backend Protocol over
that vocabulary; make `_setup_modifier` backend-selecting. Moderate refactor — not a
rewrite. (Known wart to fix on the way: read path uses frame-quantized `Timecode`
while write path uses rational `TimeValue` — unify on TimeValue.)

```
                    ┌────────────────────────────────────┐
                    │   MCP tool surface (one schema)     │
                    └────────────┬───────────────────────┘
                    ┌────────────┴───────────────────────┐
                    │   Operation layer (~30 verbs)       │
                    │   trim/split/retime/marker/reorder… │
                    └──┬──────────────┬──────────────┬───┘
        Tier 1: XML    │  Tier 2: Live (official) │  Tier 3: Bridges (optional)
        ───────────    │  ─────────────────────── │  ──────────────────────────
        parser/writer  │  • push-import (odoc +   │  • SpliceKit JSON-RPC :9876
        rough_cut/diff │    <import-options>)     │    (if patched FCP running)
        export — today │  • AppleScript library   │  • CommandPost WS :27480
                       │    inspection (read)     │    (if installed — incl.
                       │  • watch-folder round-   │    triggering Export XML →
                       │    trip ergonomics       │    closes the read-back loop)
                       │  • later: Workflow Ext   │
                       │    (playhead/selection)  │
                       └──────────────────────────┘
        + Media-intelligence layer feeding all tiers:
          whisperX/parakeet · PySceneDetect 0.7/TransNetV2 · Beat This! · Silero VAD
          · FastVLM/Qwen3-VL via mlx-vlm · ffmpeg-8 preview compiler
```

Principles:
1. **Never patch the binary.** Tier 3 *detects* SpliceKit/CommandPost if the user
   installed them; we ship adapters, not injections. Safety is the brand.
2. **The export asymmetry is handled, not hidden:** watch-folder + one-keystroke
   export instructions by default; CommandPost bridge automates the click when present;
   SpliceKit bridge reads live state when present. Degrade gracefully.
3. **Media intelligence is the real moat.** Nobody couples FCPXML depth + actual
   media analysis + preview-without-FCP. That combination is unique as of June 2026.

---

## 5. Roadmap

### v0.8 — Format currency (defensive, ~days)
- Parse FCPXML 1.12–1.14 (deltas enumerated above); tolerate unknown elements losslessly.
- `.fcpxmld` bundle read/write with **sidecar preservation** (don't destroy
  object-tracking/Cinematic data — rare third-party tool that doesn't).
- CI: `xmllint --dtdvalid` against Apple's own bundled DTDs.
- Emit 1.13 by default (1.11 forfeits HFR/spatial attrs).
- Bulk media-relink tool (rewrite `media-rep src` paths — known user pain, trivial here).
- Hygiene: CHANGELOG entry for 0.7.0 missing; README badge says 912 tests / Testing
  section says 795; README references nonexistent `fcpxml/README.md` and a phantom env var.

### v0.9 — Live mode v1, official surfaces only (~1-2 weeks)
- `push_to_fcp` tool: write FCPXML + `<import-options>`, fire the Apple event
  (~20 lines; zero-click; officially supported).
- `list_open_libraries` / `get_fcp_state`: AppleScript read-only inspection via osascript.
- Watch-folder round-trip: user exports XML to a watched dir; server auto-detects,
  diffs against last known state.
- Backend Protocol refactor (operation layer); split `server.py` (3,046 lines) into
  `tools/` modules while at it.

### v0.10 — Media intelligence (the moat, ~weeks)
- Transcription: whisperX (word-level <100ms) or FluidAudio/Parakeet CoreML →
  transcript-driven editing: cut-by-sentence, chapter markers, caption generation
  into real FCPXML caption elements.
- Scene cuts: PySceneDetect 0.7 (has `save-fcp`!) + TransNetV2 for hard content.
- Beats: **Beat This!** (`pip install beat-this`, Apr 2026) — madmom is dead, do not dep.
- Silence: Silero VAD v6 / wrap auto-editor (public domain) rather than reimplement.
- Shot understanding: FastVLM / Qwen3-VL via mlx-vlm for "find the b-roll of X."
- **Preview-without-FCP**: compile timeline → ffmpeg trim/concat/xfade filtergraph,
  render low-res proxy. Unfilled gap in the entire ecosystem; closes the verify loop.

### v1.0 — Bridges + positioning
- Optional adapters: SpliceKit `:9876` (live read/edit/export when present),
  CommandPost `:27480` (action execution incl. Export XML trigger when present).
- Maintained OTIO bridge (the official adapter is dead — become the de facto one).
- Flagship demo: beat-synced music-video rough cut (350-video director credibility —
  nobody else can author that demo).
- MCP Registry / Smithery refresh, FCP Cafe + Discord presence (the integration
  debates are happening there; demand is explicit and unserved).

### Explicitly NOT doing
- CapCut MCP (crowded, encrypted format, trademark risk).
- Forking/bundling SpliceKit's injection (legal + fragility + enterprise rejection).
- Betting on Apple opening automation (two major versions, zero new hooks).
- madmom, OTIO-as-engine, raw AX scripting maintained solo.

---

## 6. Risks register

1. **SpliceKit revives** → it subsumes the live axis; our safety/portability framing
   must be established before that. (Watch: github.com/elliotttate/SpliceKit commits.)
2. **dreliq9-class clones** — the tool taxonomy was replicated in ~a month by a 0-star
   repo. Capability alone isn't the moat; distribution + format depth + media layer are.
3. **Apple absorbs from above** (native transcription/beat/visual search already) —
   keep value in batch/programmatic workflows Apple won't ship.
4. **FCPXML coverage shrinks relatively** — Apple keeps new AI data library-side.
   Means: the live bridges matter more over time, not less.
5. Round-trip lossiness blame: users will blame the tool for Apple's drops (Magnetic
   Mask, sidecars). Document loudly; preserve bundles.

---

*Sources: 36-agent verified sweep 2026-06-11. Researcher outputs archived at
`/tmp/fcp-research/` (session-local). Key primary sources: FCP 12.2 sdef + bundled
DTDs (local), developer.apple.com Professional Video Applications docs,
github.com/elliotttate/SpliceKit, CommandPost PR #3514, Apple release notes 102825.*
