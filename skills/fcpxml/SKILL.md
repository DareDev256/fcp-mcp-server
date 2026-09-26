# FCPXML MCP — Agent Skill

Use when working with Final Cut Pro XML files (.fcpxml) — timeline analysis, quality control, batch editing, rough cut generation, marker management, cross-NLE export, and multi-track workflows.

TRIGGER when: user mentions FCPXML, Final Cut Pro, FCP, timeline analysis, video editing XML, markers, rough cuts, or any post-production workflow involving XML files.

## Tool Categories

### Read & Analyze (non-destructive)
| Tool | Use when... |
|------|-------------|
| `list_projects` | Finding FCPXML files in a directory (default: ~/Movies) |
| `analyze_timeline` | Getting stats — duration, resolution, clip count, frame rate, pacing metrics |
| `analyze_pacing` | Evaluating edit rhythm — cuts/min, pacing curve, suggestions |
| `list_clips` | Listing all clips with timecodes, durations, metadata |
| `list_markers` | Extracting markers (chapter, todo, standard). Use `format: "youtube"` for YouTube chapters |
| `list_keywords` | Showing keyword tags applied to clips |
| `list_library_clips` | Showing source clips available in the library (for rough cut planning) |
| `list_connected_clips` | Showing clips on connected lanes (B-roll, titles, SFX) |
| `list_compound_clips` | Showing compound clip structure |
| `list_roles` | Showing audio/video role assignments |
| `list_effects` | Showing applied effects and filters |
| `list_templates` | Showing available timeline templates |

### QC & Detection (non-destructive)
| Tool | Use when... |
|------|-------------|
| `validate_timeline` | Getting a 0-100 health score with issue breakdown |
| `detect_flash_frames` | Finding ultra-short clips (accidental 1-2 frame cuts) |
| `detect_gaps` | Finding unintentional gaps in the timeline |
| `detect_duplicates` | Finding repeated source clips |
| `detect_silence_candidates` | Finding dead air / silence regions |
| `diff_timelines` | Comparing two versions of a timeline (added/removed/moved/trimmed) |

### Edit & Fix (creates _modified output files, never overwrites originals)
| Tool | Use when... |
|------|-------------|
| `add_marker` | Adding a single marker (chapter, todo, standard) at a timecode |
| `batch_add_markers` | Adding multiple markers at once (chapter lists, beat markers) |
| `trim_clip` | Adjusting in/out points of a clip |
| `split_clip` | Splitting a clip at a specific timecode |
| `delete_clips` | Removing clips by index or name |
| `insert_clip` | Inserting a clip at a position in the timeline |
| `reorder_clips` | Rearranging clip order |
| `add_transition` | Adding cross-dissolve or other transitions between clips |
| `change_speed` | Applying speed changes (slow-mo, fast-forward, reverse) |
| `add_connected_clip` | Adding a clip to a connected lane (B-roll, titles) |
| `add_audio` | Adding audio clips to the timeline |
| `create_compound_clip` | Grouping clips into a compound clip |
| `flatten_compound_clip` | Expanding a compound clip back to individual clips |
| `assign_role` | Setting audio/video roles on clips |
| `apply_template` | Applying a timeline template |

### Auto-Fix (batch operations)
| Tool | Use when... |
|------|-------------|
| `fix_flash_frames` | Automatically extending adjacent clips to cover flash frames |
| `rapid_trim` | Quick batch trim operations |
| `fill_gaps` | Closing gaps by extending adjacent clips |
| `remove_silence_candidates` | Removing or marking detected silence regions |

### Generate (creates new FCPXML files)
| Tool | Use when... |
|------|-------------|
| `auto_rough_cut` | Building a rough cut from library clips with keyword filters and target duration |
| `generate_montage` | Creating a montage from tagged clips with pacing control |
| `generate_ab_roll` | Creating documentary-style A/B roll edits |

### Music & Markers
| Tool | Use when... |
|------|-------------|
| `import_beat_markers` | Importing beat timestamps from JSON for music-synced editing |
| `snap_to_beats` | Snapping all cuts to the nearest beat marker |
| `import_srt_markers` | Importing markers from SRT/VTT subtitle files |
| `import_transcript_markers` | Importing markers from transcript text |

### Export & Reformat
| Tool | Use when... |
|------|-------------|
| `export_edl` | Exporting an EDL (Edit Decision List) |
| `export_csv` | Exporting timeline data as CSV |
| `export_resolve_xml` | Exporting FCPXML v1.9 for DaVinci Resolve |
| `export_fcp7_xml` | Exporting FCP7 XMEML v5 for Premiere / legacy NLEs |
| `export_role_stems` | Exporting separate XMLs per audio role (dialogue, music, SFX) |
| `filter_by_role` | Filtering timeline to only show clips with a specific role |
| `reformat_timeline` | Changing format — presets: `9:16`, `1:1`, `4:5`, `4:3`, `16:9`, or custom |

## Workflow Chains

When the user describes a high-level intent, chain tools in this order:

### QC Pipeline
`analyze_timeline` → `detect_flash_frames` → `detect_gaps` → `detect_duplicates` → `validate_timeline`
Then offer fixes: `fix_flash_frames` → `fill_gaps`

### YouTube Chapter Export
`list_markers` (format: "youtube") → format output for copy-paste
If no chapters exist: `analyze_pacing` → suggest chapter points → `batch_add_markers`

### Rough Cut Assembly
`list_library_clips` → `list_keywords` → discuss structure → `auto_rough_cut` or `generate_montage`

### Beat-Synced Music Video
`import_beat_markers` → `auto_rough_cut` or `generate_montage` → `snap_to_beats`

### Cross-NLE Handoff
`export_resolve_xml` (for Resolve) + `export_fcp7_xml` (for Premiere/Pro Tools)

### Silence Cleanup
`detect_silence_candidates` → review results → `remove_silence_candidates`

### Timeline Comparison
`diff_timelines` → summarize changes for revision log

### Social Reformat
`reformat_timeline` with preset → note that framing adjustments still needed in FCP

## Key Rules

1. **Never overwrite originals.** All write operations create `_modified`, `_chapters`, etc. suffixed files.
2. **Times are rational fractions.** FCPXML uses `"600/2400s"` format — never convert to floats.
3. **Always analyze before fixing.** Run detection tools first, show the user what was found, then offer batch fixes.
4. **Connected clips use lanes.** Positive lane = above primary storyline (video/titles), negative = below (audio).
5. **Compound clips can be nested.** Use `list_compound_clips` to understand structure before flattening.
6. **Export formats differ.** Resolve XML flattens compounds. FCP7 XML converts spine-based to track-based model.
