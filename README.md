# Final Cut Pro MCP Server

> **The first AI-powered MCP server for Final Cut Pro.** Analyze timelines, add markers, trim clips, insert library clips, and generate rough cuts — all through natural language conversation with Claude.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-1.0-green.svg)](https://modelcontextprotocol.io/)
[![Final Cut Pro](https://img.shields.io/badge/Final%20Cut%20Pro-10.4+-purple.svg)](https://www.apple.com/final-cut-pro/)

---

## What This Does

Connect Claude to your Final Cut Pro projects. Ask questions, make edits, generate rough cuts.

```
You: "Analyze my timeline and tell me where the pacing drags"

Claude: Looking at your timeline... The average cut in Q1 is 4.2s but jumps to 8.1s in Q3.
        I found 3 clips over 30 seconds that might need trimming. Want me to add markers?

You: "Yes, add TODO markers at each long clip. Then generate a 2-minute rough cut
      using only the clips tagged 'interview', fast pacing."

Claude: Done! Added 3 markers. Generated rough cut saved to rough_cut.fcpxml.
        Used 12 of 47 available clips. Import it into FCP: File → Import → XML

You: "Show me what clips are in my library that I haven't used yet"

Claude: Found 3 clips in your library:
        • r2: Interview_A (5m 00s)
        • r3: Broll_City (2m 00s)
        • r4: Broll_Studio (1m 30s)

You: "Insert Broll_City at 1:25, just the first 5 seconds"

Claude: Done! Inserted 'Broll_City' at 00:01:25:00 (5s subclip).
        Saved to project_modified.fcpxml
```

## Features

### Read Operations
- **Analyze timelines** — Duration, resolution, clip count, pacing metrics
- **List clips** — With timecodes, durations, and keyword metadata
- **List library clips** — See all source media available for insertion
- **Extract markers** — Chapter markers, TODOs, standard markers (YouTube chapter format too)
- **Find issues** — Flash frames (< 0.5s), overly long clips (> 30s)
- **Export** — EDL, CSV for handoffs to color/audio

### Write Operations
- **Add markers** — Single or batch, auto-generate at cuts or intervals
- **Insert clips** — Add library clips to timeline at any position with subclip support
- **Trim clips** — Adjust in/out points with ripple
- **Reorder clips** — Move clips to new positions
- **Add transitions** — Cross-dissolve, fade to black, etc.
- **Change speed** — Slow motion or speed up
- **Split & delete** — Non-destructive timeline editing

### AI-Powered
- **Auto rough cut** — Generate a complete timeline from keywords, duration, and pacing preferences

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

## All 21 Tools

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

### AI-Powered — 1 tool
| Tool | Description |
|------|-------------|
| `auto_rough_cut` | Generate timeline from keywords, duration, pacing |

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
├── server.py              # MCP server (21 tools)
├── fcpxml/
│   ├── __init__.py
│   ├── parser.py          # Read FCPXML → Python + library clip listing
│   ├── writer.py          # Python → FCPXML, in-place modification + clip insertion
│   ├── rough_cut.py       # AI-powered rough cut generation
│   └── models.py          # Timeline, Clip, Marker, TimeValue
├── docs/
│   └── specs/             # Design specs and schemas
├── tests/
│   ├── test_parser.py     # Parser tests (8 tests)
│   └── test_writer.py     # Writer tests (8 tests)
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

### v0.2.1 — Library Clip Insertion (Latest)
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
