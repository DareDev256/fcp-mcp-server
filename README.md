# Final Cut Pro MCP Server

> **The first AI-powered MCP server for Final Cut Pro.** Analyze timelines, detect flash frames, create montages with pacing curves, generate A/B roll edits, sync cuts to beats — all through natural language conversation with Claude.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-1.0-green.svg)](https://modelcontextprotocol.io/)
[![Final Cut Pro](https://img.shields.io/badge/Final%20Cut%20Pro-10.4+-purple.svg)](https://www.apple.com/final-cut-pro/)

---

## What This Does

Connect Claude to your Final Cut Pro projects. Ask questions, make edits, generate rough cuts.

```
You: "Validate my timeline for any issues"

Claude: Timeline Health: 87%
        - 3 flash frames detected (< 2 frames each) ⚠️
        - 1 gap found at 00:02:15:00
        - All media references valid ✅

You: "Fix the flash frames and fill the gap"

Claude: Fixed 3 flash frames (extended previous clips).
        Filled 1 gap by extending adjacent clip.
        Saved to project_fixed.fcpxml

You: "Create a 30-second montage from my broll, accelerating pace"

Claude: Generated montage: 18 clips
        Pacing: 2s cuts → 0.5s cuts (builds energy)
        Saved to montage.fcpxml

You: "Now create an A/B roll edit: interviews as main, broll as cutaways"

Claude: Generated A/B roll: 12 interview segments, 11 broll cutaways
        Total: 3m 15s alternating between A and B roll
        Saved to ab_roll.fcpxml

You: "Import my beat markers and snap the cuts to the music"

Claude: Added 96 beat markers from beats.json
        Adjusted 23 cuts to align with beats (avg shift: 3 frames)
        Your edit is now synced to the music!
```

## Features

### Read & Analysis Operations
- **Analyze timelines** — Duration, resolution, clip count, pacing metrics
- **List clips** — With timecodes, durations, and keyword metadata
- **List library clips** — See all source media available for insertion
- **Extract markers** — Chapter markers, TODOs, standard markers (YouTube chapter format too)
- **Find issues** — Flash frames (< 0.5s), overly long clips (> 30s)
- **Export** — EDL, CSV for handoffs to color/audio

### Speed Cutting Analysis (v0.3.0)
- **Detect flash frames** — Find ultra-short clips with severity levels (critical < 2 frames, warning < 6 frames)
- **Detect duplicates** — Find clips using the same source media
- **Detect gaps** — Find unintentional gaps in the timeline
- **Validate timeline** — Comprehensive health check with score (0-100%)

### Write Operations
- **Add markers** — Single or batch, auto-generate at cuts or intervals
- **Insert clips** — Add library clips to timeline at any position with subclip support
- **Trim clips** — Adjust in/out points with ripple
- **Reorder clips** — Move clips to new positions
- **Add transitions** — Cross-dissolve, fade to black, etc.
- **Change speed** — Slow motion or speed up
- **Split & delete** — Non-destructive timeline editing

### Speed Cutting Write (v0.3.0)
- **Fix flash frames** — Auto-fix by extending neighbors or deleting
- **Rapid trim** — Batch trim all clips to max duration for fast montages
- **Fill gaps** — Close gaps by extending adjacent clips

### AI-Powered Generation
- **Auto rough cut** — Generate a complete timeline from keywords, duration, and pacing preferences
- **Generate montage** — Create rapid-fire montages with pacing curves (accelerating, decelerating, pyramid)
- **Generate A/B roll** — Documentary-style alternating edits between main content and cutaways

### Beat Sync (v0.3.0)
- **Import beat markers** — Import from external audio analysis (JSON)
- **Snap to beats** — Align cuts to nearest beat markers for music-synced edits

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

### 4. Start Editing with AI

Open Claude Desktop and start talking to your timeline.

---

## All 32 Tools

### Analysis (Read) — 11 tools
| Tool | Description |
|------|-------------|
| `list_projects` | Find all FCPXML files in a directory |
| `analyze_timeline` | Get comprehensive stats on duration, resolution, pacing |
| `list_clips` | List all clips with timecodes, durations, keywords |
| `list_library_clips` | List all source clips available in the library |
| `list_markers` | Extract markers with timestamps (YouTube chapter format) |
| `find_short_cuts` | Find potential flash frames (< threshold) |
| `find_long_clips` | Find clips that might need trimming |
| `list_keywords` | Extract all keywords/tags from project |
| `export_edl` | Generate EDL for color/audio handoffs |
| `export_csv` | Export timeline data to CSV |
| `analyze_pacing` | AI analysis with suggestions |

### Speed Cutting Analysis (v0.3.0) — 4 tools
| Tool | Description |
|------|-------------|
| `detect_flash_frames` | Find ultra-short clips with severity (critical/warning) |
| `detect_duplicates` | Find clips using same source media |
| `detect_gaps` | Find unintentional gaps in timeline |
| `validate_timeline` | Comprehensive health check with score |

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

### Speed Cutting Write (v0.3.0) — 3 tools
| Tool | Description |
|------|-------------|
| `fix_flash_frames` | Auto-fix flash frames (extend neighbors or delete) |
| `rapid_trim` | Batch trim clips to max duration |
| `fill_gaps` | Close gaps by extending adjacent clips |

### AI-Powered — 3 tools
| Tool | Description |
|------|-------------|
| `auto_rough_cut` | Generate timeline from keywords, duration, pacing |
| `generate_montage` | Create montages with pacing curves (accelerating/decelerating/pyramid) |
| `generate_ab_roll` | Documentary-style A/B roll alternating edits |

### Beat Sync (v0.3.0) — 2 tools
| Tool | Description |
|------|-------------|
| `import_beat_markers` | Import beat markers from JSON audio analysis |
| `snap_to_beats` | Align cuts to nearest beat markers |

---

## Usage Examples

### Analyze Your Edit

```
"Analyze my latest FCP project"
"What's the pacing like in the first half vs the second half?"
"Find all clips shorter than 1 second"
"Extract chapter markers for my YouTube description"
```

### Make Edits

```
"Add a chapter marker at 00:01:30:00 called 'Intro'"
"Trim 2 seconds off the end of clip 'Interview_02'"
"Move the outro to the beginning"
"Add a cross-dissolve to every clip"
```

### Insert Library Clips

```
"What clips do I have available in my library?"
"Insert Broll_City at the end of the timeline"
"Put Interview_A at 00:01:25:00, just use seconds 10-20"
"Add Broll_Studio after the first Interview clip"
```

### Generate Rough Cuts

```
"Create a 3-minute rough cut using clips tagged 'broll', fast pacing"
"Generate a rough cut with these segments:
  - Intro (30s, 'intro' keyword)
  - Main content (2min, 'interview' keyword)
  - Outro (15s, 'outro' keyword)"
```

### Speed Cutting (v0.3.0)

```
"Validate my timeline for any issues"
"Find all flash frames in my timeline"
"Fix the flash frames by extending the previous clips"
"Trim all clips to max 2 seconds for a fast montage"
"Fill all the gaps in my timeline"
```

### Montage & A/B Roll (v0.3.0)

```
"Create a 30-second montage from my broll, accelerating pace"
"Generate a montage that starts slow and ends fast (pyramid curve)"
"Create an A/B roll: interviews as A, broll as B, 3 minutes"
"Make a documentary-style edit alternating between talking heads and cutaways"
```

### Beat Sync (v0.3.0)

```
"Import beat markers from beats.json"
"Snap all my cuts to the beat markers"
"Align cuts to beats, prefer earlier beats within 6 frames"
```

---

## How It Works

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Claude        │────▶│   MCP Server    │────▶│   FCPXML Files  │
│   Desktop       │◀────│   (Python)      │◀────│   (Your Edits)  │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │
        │                       ▼
        │               Parse & Modify
        │               FCPXML via Python
        │                       │
        └───────────────────────┘
              Natural Language
```

1. **Export from FCP**: `File → Export XML`
2. **Talk to Claude**: Analyze, suggest, modify
3. **Import back**: `File → Import → XML`

---

## Project Structure

```
fcp-mcp-server/
├── server.py              # MCP server (32 tools)
├── fcpxml/
│   ├── __init__.py
│   ├── parser.py          # Read FCPXML → Python + library clip listing
│   ├── writer.py          # Python → FCPXML, speed cutting, gap filling
│   ├── rough_cut.py       # AI-powered rough cut, montage, A/B roll generation
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

After directing 350+ music videos, I know the pain of repetitive editing tasks:
- Counting cuts for every video
- Extracting chapter markers manually
- Finding flash frames by scrubbing
- Building rough cuts clip by clip

Now I just ask Claude.

---

## Releases

### v0.3.0 — Speed Cutting & AI-Powered Generation (Latest)
*The ultimate speed editor: detect issues, fix them instantly, generate montages*

- **Speed Cutting Analysis:** `detect_flash_frames`, `detect_duplicates`, `detect_gaps`, `validate_timeline`
- **Speed Cutting Write:** `fix_flash_frames`, `rapid_trim`, `fill_gaps`
- **AI Generation:** `generate_montage` with pacing curves (accelerating/decelerating/pyramid), `generate_ab_roll` for documentary-style edits
- **Beat Sync:** `import_beat_markers`, `snap_to_beats` for music-synced edits
- Timeline health scoring (0-100%)
- Flash frame severity levels (critical < 2 frames, warning < 6 frames)
- 32 tools total

### v0.2.1 — Library Clip Insertion
*Insert clips from your library onto the timeline*

- **New:** `list_library_clips` — See all source media available for insertion
- **New:** `insert_clip` — Add library clips at any position with subclip support
- Position options: `start`, `end`, timecode, or `after:clip_name`
- Subclip support via in/out points
- Automatic ripple to shift subsequent clips
- 21 tools total

### v0.2.0 — Comprehensive Timeline Editing
*Full creative control over your timeline*

- **New write tools:** `trim_clip`, `reorder_clips`, `add_transition`, `change_speed`, `delete_clips`, `split_clip`, `batch_add_markers`
- **New AI tool:** `auto_rough_cut` — Generate rough cuts from keywords, duration, pacing
- TimeValue model for precise rational time math
- 19 tools total

### v0.1.0 — Initial Release
*The foundation: read and analyze*

- Core FCPXML parsing (v1.8 - v1.11)
- Timeline analysis and clip listing
- Marker extraction (chapters, TODOs, standard)
- Flash frame and long clip detection
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

Built by [@DareDev256](https://github.com/DareDev256) — Former music video director (350+ videos for Chief Keef, Migos, Masicka), now building AI tools for creators.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
