# FCPXML MCP

Batch operations and analysis for Final Cut Pro XML files via Claude.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-1.0-green.svg)](https://modelcontextprotocol.io/)
[![Final Cut Pro](https://img.shields.io/badge/Final%20Cut%20Pro-10.4+-purple.svg)](https://www.apple.com/final-cut-pro/)

---

## What This Is

A Model Context Protocol (MCP) server that reads and writes FCPXML files. Claude can analyze your timelines, make batch edits, and generate new sequences.

## What This Is NOT

- **Not a plugin** that runs inside Final Cut Pro
- **Not real-time editing** — you export XML, modify it, then reimport
- **Not a replacement** for manual editing when you need visual feedback

## The Workflow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Final Cut Pro  │────▶│   Claude +      │────▶│  Final Cut Pro  │
│  Export XML     │     │   MCP Server    │     │  Import XML     │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

1. **Export from FCP**: `File → Export XML...`
2. **Run MCP tools**: Analyze, modify, generate via Claude
3. **Import back to FCP**: `File → Import → XML`

This is a roundtrip workflow. Each edit cycle requires an export and import.

---

## When To Use This

### Good For

| Use Case | Why It Works |
|----------|--------------|
| **Batch marker insertion** | Add 100 chapter markers from a transcript in one operation |
| **QC before delivery** | Find flash frames, gaps, duplicates programmatically |
| **Data extraction** | Export EDL, CSV, chapter markers for handoffs |
| **Template generation** | Create rough cuts from tagged clips automatically |
| **Automated assembly** | Build montages from keywords and pacing rules |
| **Timeline health checks** | Validate timing, find issues, get stats |

### Not Ideal For

| Use Case | Why Not |
|----------|---------|
| **Creative editing decisions** | No visual feedback — you can't see results until reimport |
| **Real-time adjustments** | Export/import cycle is slow for iterative changes |
| **Fine-tuning cuts** | Adjusting by a few frames is faster in FCP directly |
| **Anything visual** | Color, framing, motion — need to see it |

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/DareDev256/fcp-mcp-server.git
cd fcp-mcp-server
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fcp": {
      "command": "python",
      "args": ["/path/to/fcp-mcp-server/server.py"],
      "env": {
        "FCP_PROJECTS_DIR": "/Users/you/Movies"
      }
    }
  }
}
```

### 3. Export from Final Cut Pro

`File → Export XML...` (saves as `.fcpxml`)

### 4. Use the Tools

Open Claude Desktop and start working with your timeline.

---

## All 32 Tools

### Analysis (Read) — 11 tools
| Tool | Description |
|------|-------------|
| `list_projects` | Find all FCPXML files in a directory |
| `analyze_timeline` | Get stats on duration, resolution, pacing |
| `list_clips` | List all clips with timecodes, durations, keywords |
| `list_library_clips` | List all source clips available in the library |
| `list_markers` | Extract markers with timestamps (YouTube chapter format) |
| `find_short_cuts` | Find potential flash frames (< threshold) |
| `find_long_clips` | Find clips that might need trimming |
| `list_keywords` | Extract all keywords/tags from project |
| `export_edl` | Generate EDL for color/audio handoffs |
| `export_csv` | Export timeline data to CSV |
| `analyze_pacing` | Get pacing metrics with suggestions |

### QC & Validation — 4 tools
| Tool | Description |
|------|-------------|
| `detect_flash_frames` | Find ultra-short clips with severity (critical/warning) |
| `detect_duplicates` | Find clips using same source media |
| `detect_gaps` | Find unintentional gaps in timeline |
| `validate_timeline` | Health check with score (0-100%) |

### Editing (Write) — 9 tools
| Tool | Description |
|------|-------------|
| `add_marker` | Add a single marker at a timecode |
| `batch_add_markers` | Add multiple markers, or auto-generate at cuts/intervals |
| `insert_clip` | Insert a library clip onto the timeline at any position |
| `trim_clip` | Adjust in/out points with optional ripple |
| `reorder_clips` | Move clips to new timeline positions |
| `add_transition` | Add cross-dissolve, fade, wipe between clips |
| `change_speed` | Slow motion or speed up clips |
| `delete_clips` | Remove clips with optional ripple |
| `split_clip` | Split a clip at specified timecodes |

### Batch Fixes — 3 tools
| Tool | Description |
|------|-------------|
| `fix_flash_frames` | Auto-fix flash frames (extend neighbors or delete) |
| `rapid_trim` | Batch trim clips to max duration |
| `fill_gaps` | Close gaps by extending adjacent clips |

### Generation — 3 tools
| Tool | Description |
|------|-------------|
| `auto_rough_cut` | Generate timeline from keywords, duration, pacing |
| `generate_montage` | Create montages with pacing curves (accelerating/decelerating/pyramid) |
| `generate_ab_roll` | Documentary-style A/B roll alternating edits |

### Beat Sync — 2 tools
| Tool | Description |
|------|-------------|
| `import_beat_markers` | Import beat markers from JSON audio analysis |
| `snap_to_beats` | Align cuts to nearest beat markers |

---

## Usage Examples

### Timeline Analysis

```
"Analyze my latest FCP project"
"List all clips shorter than 1 second"
"Extract chapter markers for YouTube description"
"Run a health check on my timeline"
```

### Batch Edits

```
"Add chapter markers at these timestamps: [list]"
"Trim 2 seconds off the end of every interview clip"
"Add a cross-dissolve between all clips"
"Fix all flash frames by extending previous clips"
```

### Data Export

```
"Export an EDL for the colorist"
"Generate a CSV of all clips with timecodes"
"List all clips tagged 'interview' with durations"
```

### Automated Assembly

```
"Create a 3-minute rough cut using clips tagged 'broll'"
"Generate a montage with accelerating pacing"
"Build an A/B roll: interviews as main, broll as cutaways"
```

### Beat Sync

```
"Import beat markers from beats.json"
"Snap all cuts to the nearest beat"
```

---

## Project Structure

```
fcp-mcp-server/
├── server.py              # MCP server (32 tools)
├── fcpxml/
│   ├── __init__.py
│   ├── parser.py          # Read FCPXML → Python + library clip listing
│   ├── writer.py          # Python → FCPXML, batch fixes, gap filling
│   ├── rough_cut.py       # Rough cut, montage, A/B roll generation
│   └── models.py          # Timeline, Clip, Marker, TimeValue, PacingCurve
├── docs/
│   └── specs/             # Design specs and schemas
├── tests/
│   ├── test_parser.py     # Parser tests (8 tests)
│   ├── test_writer.py     # Writer tests (8 tests)
│   └── test_speed_cutting.py  # Speed cutting & montage tests (22 tests)
├── examples/
│   └── sample.fcpxml      # Sample FCPXML for testing
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## Requirements

- **Python 3.10+**
- **Final Cut Pro 10.4+** (for FCPXML 1.8+)
- **Claude Desktop** (or any MCP-compatible client)
- **mcp** package (`pip install mcp`)

---

## Why This Exists

After directing 350+ music videos, I got tired of repetitive editing tasks:
- Counting cuts manually
- Extracting chapter markers one by one
- Finding flash frames by scrubbing through footage
- Building rough cuts clip by clip

These are batch operations that don't need visual feedback. Export the XML, let Claude handle it, import the result.

---

## Releases

### v0.3.0 — Batch Operations & Generation (Latest)

- **QC Tools:** `detect_flash_frames`, `detect_duplicates`, `detect_gaps`, `validate_timeline`
- **Batch Fixes:** `fix_flash_frames`, `rapid_trim`, `fill_gaps`
- **Generation:** `generate_montage` with pacing curves, `generate_ab_roll` for documentary-style edits
- **Beat Sync:** `import_beat_markers`, `snap_to_beats`
- Timeline health scoring (0-100%)
- Flash frame severity levels (critical < 2 frames, warning < 6 frames)
- 32 tools total

### v0.2.1 — Library Clip Insertion

- **New:** `list_library_clips` — See all source media available for insertion
- **New:** `insert_clip` — Add library clips at any position with subclip support
- 21 tools total

### v0.2.0 — Timeline Editing

- **Write tools:** `trim_clip`, `reorder_clips`, `add_transition`, `change_speed`, `delete_clips`, `split_clip`, `batch_add_markers`
- **Generation:** `auto_rough_cut` — Generate rough cuts from keywords
- 19 tools total

### v0.1.0 — Initial Release

- Core FCPXML parsing (v1.8 - v1.11)
- Timeline analysis and clip listing
- Marker extraction (chapters, TODOs, standard)
- EDL/CSV export
- 10 tools total

---

## Roadmap

- [x] Core FCPXML parsing
- [x] Timeline analysis tools
- [x] Marker extraction & insertion
- [x] Clip trimming & reordering
- [x] Transition insertion
- [x] Speed changes
- [x] Auto rough cut generation
- [x] EDL/CSV export
- [x] Library clip listing & insertion
- [x] Flash frame detection & auto-fix
- [x] Gap detection & filling
- [x] Timeline validation with health scoring
- [x] Montage generation with pacing curves
- [x] A/B roll documentary-style editing
- [x] Beat marker import & snap-to-beat
- [ ] Audio sync detection
- [ ] Multi-timeline comparison
- [ ] Premiere Pro XML support

---

## Contributing

PRs welcome. If you're a video editor who codes (or a coder who edits), let's build this together.

---

## Credits

Built by [@DareDev256](https://github.com/DareDev256) — Former music video director (350+ videos for Chief Keef, Migos, Masicka), now building tools for creators.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
