# Workflow Recipes

Real-world tool chains for common post-production tasks. Each recipe shows what to ask Claude and which tools fire under the hood.

> **Reading the tool chains.** As of v0.14.0 the MCP client sees 7 grouped
> verbs, not 62 flat tool names. Chains below are written `group:action` — so
> `diagnose:validate_timeline` means calling the `diagnose` tool with
> `{"action": "validate_timeline", "args": {...}}`. The flat names still
> dispatch for backwards compatibility, and setting `FCP_MCP_LEGACY_TOOLS=1`
> advertises them alongside the groups, but the grouped form is what Claude
> can see by default.

---

## Delivery QC Pipeline

**Scenario:** Final timeline needs quality sign-off before client delivery.

```
"Run a full QC check on /path/to/project.fcpxml"
```

**Tool chain:** `inspect:analyze_timeline` → `diagnose:detect_flash_frames` → `diagnose:detect_gaps` → `diagnose:detect_duplicates` → `diagnose:validate_timeline`

The `diagnose:validate_timeline` tool returns a 0–100% health score. Anything below 80% flags specific issues. Follow up with:

```
"Fix all flash frames by extending previous clips, then fill any gaps"
```

**Tool chain:** `edit:fix_flash_frames` → `edit:fill_gaps`

Both tools generate `_modified` output files — your original XML is never touched.

---

## YouTube Chapter Export

**Scenario:** 45-minute podcast edit with chapter markers needs YouTube-formatted timestamps.

```
"List all markers in my timeline formatted for YouTube chapters"
```

**Tool chain:** `inspect:list_markers` (with format filter)

If chapters don't exist yet but you have a transcript:

```
"Import these YouTube chapters as markers: 0:00 Intro, 2:15 Topic One, 14:30 Deep Dive..."
```

**Tool chain:** `mark:import_transcript_markers` → `inspect:list_markers`

For SRT/VTT subtitle files from auto-transcription services:

```
"Import chapters from /path/to/captions.srt as markers"
```

**Tool chain:** `mark:import_srt_markers`

---

## Beat-Synced Music Video Assembly

**Scenario:** 200 B-roll clips tagged by keyword, one music track with beat analysis.

**Step 1 — Import beats:**
```
"Import beat markers from /path/to/beats.json"
```

**Step 2 — Generate assembly:**
```
"Create a rough cut using clips tagged 'performance' and 'broll', target 3:30 duration, accelerating pacing"
```

**Tool chain:** `mark:import_beat_markers` → `generate:auto_rough_cut` or `generate:generate_montage`

**Step 3 — Snap to beats:**
```
"Snap all cuts to the nearest beat marker"
```

**Tool chain:** `mark:snap_to_beats`

The beat JSON format expects an array of timestamps in seconds:
```json
{ "beats": [0.0, 0.48, 0.96, 1.44, 1.92] }
```

---

## Cross-NLE Handoff

**Scenario:** Timeline edited in FCP needs to go to a colorist on DaVinci Resolve and an audio mixer on Pro Tools (via Premiere).

```
"Export my timeline for DaVinci Resolve and also as FCP7 XML for Premiere"
```

**Tool chain:** `deliver:export_resolve_xml` + `deliver:export_fcp7_xml`

**What changes in each export:**
- **Resolve (FCPXML v1.9):** Compound clips flattened, unsupported attributes stripped, simpler element tree
- **FCP7 XMEML:** Spine-based model converted to track-based model — primary storyline becomes Track 0, connected clip lanes map to higher tracks

---

## Documentary A/B Roll

**Scenario:** Interview footage (A-roll) with cutaway B-roll needs structured assembly.

```
"Generate an A/B roll edit — 'interview' clips as A-roll, 'broll' clips as B-roll, 8-minute target"
```

**Tool chain:** `generate:generate_ab_roll`

The generator alternates between A-roll and B-roll clips, placing B-roll on connected lanes (above the primary storyline). This matches the standard documentary editing pattern where interview audio runs continuously and visuals cut between talking head and supplementary footage.

---

## Social Media Reformat

**Scenario:** 16:9 master edit needs vertical versions for Reels/TikTok and square for feed posts.

```
"Reformat my timeline to 9:16 for Instagram Reels"
```

**Tool chain:** `deliver:reformat_timeline` (preset: `9:16`)

Available presets: `9:16` (vertical), `1:1` (square), `4:5` (portrait feed), `4:3` (classic), `16:9` (widescreen). Custom resolutions also supported.

> **Note:** This changes the project format metadata — it doesn't re-frame or crop footage. You'll still need to adjust framing in FCP after import.

---

## Timeline Version Comparison

**Scenario:** Director sent revision notes, you made changes, now need to document what changed.

```
"Compare /path/to/edit_v2.fcpxml with /path/to/edit_v1.fcpxml"
```

**Tool chain:** `diagnose:diff_timelines`

Returns structured diff: clips added, removed, moved, or trimmed. Marker changes, transition changes, and format changes are all tracked. Useful for revision logs and client communication.

---

## Silence Cleanup

**Scenario:** Long-form interview has dead air that needs trimming.

**Step 1 — Detect:**
```
"Find silence candidates in my timeline"
```

**Tool chain:** `diagnose:detect_silence_candidates`

Uses heuristics: gaps, ultra-short clips, naming patterns (clips named "silence", "room tone"), and duration anomalies. Results include confidence scores.

**Step 2 — Review and remove:**
```
"Remove all silence candidates with high confidence"
```

**Tool chain:** `edit:remove_silence_candidates` (mode: delete or mark)

Mark mode adds markers instead of deleting — safer for first pass.

---

## Composing Tools in AI Agent Workflows

Each tool in this MCP server follows the same pattern: read FCPXML → process → write modified FCPXML. This makes them composable — the output of one tool is valid input for the next.

When used through Claude Desktop or any MCP client, you describe intent in natural language and the agent selects and chains tools automatically. The 5 built-in MCP prompts (`qc-check`, `youtube-chapters`, `rough-cut`, `timeline-summary`, `cleanup`) are pre-built chains for the most common workflows.

For custom workflows, describe the full pipeline in one message:

```
"Analyze my timeline, fix any flash frames, add chapter markers at every 5-minute
interval, then export for DaVinci Resolve"
```

The agent will chain: `inspect:analyze_timeline` → `edit:fix_flash_frames` → `mark:batch_add_markers` → `deliver:export_resolve_xml`, passing the modified file through each step.
