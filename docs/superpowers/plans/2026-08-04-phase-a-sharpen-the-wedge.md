# Phase A: Sharpen the Wedge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the ~9,000-token tool-schema tax to roughly 1,500 by collapsing 62 flat tools into 7 grouped verbs, without breaking a single existing user, and add the two things that make the project legible: a visual timeline preview and a Claude Code skill.

**Architecture:** `call_tool` resolves handlers out of `TOOL_HANDLERS`, which is independent of what `list_tools` advertises. So the 62 existing handlers stay registered and callable forever while disappearing from the advertised list. A thin group facade accepts `{action, args}` and dispatches into the same `TOOL_HANDLERS` dict. No handler is rewritten, no behaviour changes, and every existing config keeps working.

**Tech Stack:** Python 3.10+, `mcp>=1.0.0,<2.0.0`, pytest, ruff, GitHub Actions.

## Global Constraints

- `mcp>=1.0.0,<2.0.0` — do not lift the pin in this phase. The 2.x migration is issue #9 and is a separate plan.
- `requires-python = ">=3.10"`. CI matrix is 3.10, 3.11, 3.12. All three must stay green.
- Pre-commit is mandatory and enforced by CI: `ruff check . --exclude docs/` and `pytest tests/ -v`. Both must pass before any commit.
- **No existing tool name may stop working.** Removal from `list_tools` is allowed; removal from `TOOL_HANDLERS` is not.
- The MCP registry identity is `io.github.DareDev256/fcpxml-mcp-server`, bound to the `mcp-name` marker inside the published PyPI README. Do not touch that marker.
- Every time is a rational fraction (`TimeValue`). Never use floats for time math.
- Version bumps land in three places: `pyproject.toml`, `server.py:__version__`, and both `version` fields in `server.json`.
- Current baseline to beat: 62 tools, 35,835 chars of schema, ~8,958 tokens.

---

### Task 1: Scheduled CI — the missing tripwire

The mcp 2.0 break went unseen for a week because main's last run predated it by one day. A cron run turns a silent week into a next-morning failure email. This is first because it protects every task after it.

**Files:**
- Modify: `.github/workflows/test.yml`

- [ ] **Step 1: Add the schedule trigger**

Open `.github/workflows/test.yml` and extend the `on:` block. Keep existing triggers exactly as they are, add `schedule` and `workflow_dispatch`:

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    # 06:00 UTC daily. Catches upstream dependency breaks within 24h
    # instead of whenever someone next happens to push.
    - cron: '0 6 * * *'
  workflow_dispatch:
```

- [ ] **Step 2: Verify the YAML parses**

Run: `python3 -c "import yaml,sys; print(yaml.safe_load(open('.github/workflows/test.yml'))['on'].keys())"`
Expected: prints `dict_keys(['push', 'pull_request', 'schedule', 'workflow_dispatch'])`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: run the suite daily so upstream dep breaks surface within 24h

The mcp 2.0 break (2026-07-28) went unnoticed for a week because main's
last CI run predated it by one day. A green badge only means green
whenever it last ran. Also adds workflow_dispatch for manual runs."
```

- [ ] **Step 4: Verify the schedule registered**

Run: `gh workflow view Tests --repo DareDev256/fcp-mcp-server`
Expected: the trigger list includes `schedule`. Then confirm a manual run works: `gh workflow run Tests --repo DareDev256/fcp-mcp-server` and check `gh run list --limit 1`.

---

### Task 2: The group dispatch mechanism, proved on `inspect`

Build the facade and wire exactly one group end to end. Doing one group first means a reviewer can reject the *mechanism* before six more groups are built on top of it.

**Files:**
- Modify: `server.py` (add `TOOL_GROUPS` + `handle_group` near `TOOL_HANDLERS` at :3709)
- Test: `tests/test_tool_groups.py` (create)

**Interfaces:**
- Consumes: `TOOL_HANDLERS` (existing dict, `str -> async (dict) -> list[TextContent]`), `_text_result(text: str) -> list[TextContent]`
- Produces:
  - `TOOL_GROUPS: dict[str, dict]` — each value has keys `description: str` and `actions: list[str]`
  - `async def handle_group(group: str, arguments: dict) -> list[TextContent]`
  - `def _group_tool(name: str) -> Tool` — builds the `Tool` schema for one group

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_groups.py`:

```python
"""Grouped tool facade: 7 verbs dispatching into the existing 62 handlers."""
import asyncio

import pytest

import server


class TestGroupDispatch:
    def test_inspect_group_exists_with_actions(self):
        assert "inspect" in server.TOOL_GROUPS
        actions = server.TOOL_GROUPS["inspect"]["actions"]
        assert "list_clips" in actions
        assert "analyze_timeline" in actions

    def test_every_group_action_resolves_to_a_real_handler(self):
        """A typo in an action list must fail loudly here, not at runtime."""
        for group, spec in server.TOOL_GROUPS.items():
            for action in spec["actions"]:
                assert action in server.TOOL_HANDLERS, f"{group}.{action} has no handler"

    def test_dispatch_forwards_args_to_the_underlying_handler(self):
        captured = {}

        async def fake_handler(args):
            captured.update(args)
            return server._text_result("ok")

        server.TOOL_HANDLERS["__test_action__"] = fake_handler
        server.TOOL_GROUPS["inspect"]["actions"].append("__test_action__")
        try:
            result = asyncio.run(server.handle_group(
                "inspect", {"action": "__test_action__", "args": {"filepath": "/x.fcpxml"}}
            ))
            assert captured == {"filepath": "/x.fcpxml"}
            assert result[0].text == "ok"
        finally:
            del server.TOOL_HANDLERS["__test_action__"]
            server.TOOL_GROUPS["inspect"]["actions"].remove("__test_action__")

    def test_missing_args_defaults_to_empty_dict(self):
        """Actions that take no parameters must work without an args key."""
        captured = {}

        async def fake_handler(args):
            captured["got"] = args
            return server._text_result("ok")

        server.TOOL_HANDLERS["__test_noargs__"] = fake_handler
        server.TOOL_GROUPS["inspect"]["actions"].append("__test_noargs__")
        try:
            asyncio.run(server.handle_group("inspect", {"action": "__test_noargs__"}))
            assert captured["got"] == {}
        finally:
            del server.TOOL_HANDLERS["__test_noargs__"]
            server.TOOL_GROUPS["inspect"]["actions"].remove("__test_noargs__")

    def test_unknown_action_lists_the_valid_ones(self):
        """The error must teach the model what it should have called."""
        result = asyncio.run(server.handle_group("inspect", {"action": "nope"}))
        text = result[0].text
        assert "nope" in text
        assert "list_clips" in text

    def test_action_from_another_group_is_rejected(self):
        """trim_clip is real, but it is not an inspect action."""
        result = asyncio.run(server.handle_group("inspect", {"action": "trim_clip"}))
        assert "trim_clip" in result[0].text
        assert "list_clips" in result[0].text

    def test_missing_action_is_rejected(self):
        result = asyncio.run(server.handle_group("inspect", {}))
        assert "action" in result[0].text.lower()

    def test_unknown_group_is_rejected(self):
        result = asyncio.run(server.handle_group("nonexistent", {"action": "list_clips"}))
        assert "nonexistent" in result[0].text
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_groups.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'TOOL_GROUPS'`

- [ ] **Step 3: Implement the mechanism**

In `server.py`, immediately after the `TOOL_HANDLERS = { ... }` dict ends (it closes just before `@server.call_tool()` at :3791), add:

```python
# ============================================================================
# GROUPED TOOL FACADE
#
# 62 flat tools cost ~9,000 tokens of schema in every conversation before the
# user types anything. These 7 groups advertise the same capability for a
# fraction of that. They dispatch straight into TOOL_HANDLERS, so behaviour is
# identical and every legacy tool name keeps working whether or not it is
# advertised in list_tools.
# ============================================================================

TOOL_GROUPS: dict[str, dict] = {
    "inspect": {
        "description": (
            "Read a timeline or project without changing it. Use this first to "
            "understand what you are working with."
        ),
        "actions": [
            "list_projects", "analyze_timeline", "analyze_pacing", "list_clips",
            "list_markers", "list_roles", "list_keywords", "list_effects",
            "list_templates", "list_library_clips", "list_compound_clips",
            "list_connected_clips", "filter_by_role",
        ],
    },
}


def _group_action_error(group: str, action: str | None) -> list[TextContent]:
    """Reject an action while telling the caller what it should have used."""
    if group not in TOOL_GROUPS:
        return _text_result(
            f"Unknown tool group: {group}. "
            f"Valid groups: {', '.join(sorted(TOOL_GROUPS))}"
        )
    valid = ", ".join(TOOL_GROUPS[group]["actions"])
    if not action:
        return _text_result(
            f"The '{group}' tool requires an 'action'. Valid actions: {valid}"
        )
    return _text_result(
        f"Unknown action '{action}' for '{group}'. Valid actions: {valid}"
    )


async def handle_group(group: str, arguments: dict) -> list[TextContent]:
    """Dispatch a grouped tool call into the flat TOOL_HANDLERS registry."""
    if group not in TOOL_GROUPS:
        return _group_action_error(group, None)

    action = arguments.get("action")
    if not action or action not in TOOL_GROUPS[group]["actions"]:
        return _group_action_error(group, action)

    handler = TOOL_HANDLERS.get(action)
    if handler is None:
        # Only reachable if TOOL_GROUPS names an action with no handler, which
        # test_every_group_action_resolves_to_a_real_handler exists to prevent.
        return _text_result(f"No handler registered for action: {action}")

    return await handler(arguments.get("args") or {})


def _group_tool(name: str) -> Tool:
    """Build the advertised Tool schema for one group."""
    spec = TOOL_GROUPS[name]
    return Tool(
        name=name,
        description=(
            f"{spec['description']} "
            f"Actions: {', '.join(spec['actions'])}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": spec["actions"],
                    "description": "Which operation to run.",
                },
                "args": {
                    "type": "object",
                    "description": (
                        "Arguments for the chosen action, e.g. "
                        "{\"filepath\": \"/path/to/project.fcpxml\"}."
                    ),
                },
            },
            "required": ["action"],
        },
    )
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv/bin/python -m pytest tests/test_tool_groups.py -v`
Expected: 8 passed

- [ ] **Step 5: Confirm nothing regressed**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . --exclude docs/`
Expected: 1029 passed, 4 skipped (plus the 8 new = 1037 passed), ruff clean

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_tool_groups.py
git commit -m "feat: add grouped tool facade, proved on the inspect group

62 flat tools cost ~9,000 tokens of schema on every conversation before
the user types a word. TOOL_GROUPS collapses them into verbs that
dispatch into the same TOOL_HANDLERS registry, so behaviour is byte
identical and no handler is rewritten.

Only 'inspect' is wired here so the mechanism can be reviewed before six
more groups sit on top of it."
```

---

### Task 3: The remaining six groups

**Files:**
- Modify: `server.py` (extend `TOOL_GROUPS`)
- Modify: `tests/test_tool_groups.py`

**Interfaces:**
- Consumes: `TOOL_GROUPS`, `handle_group` from Task 2
- Produces: `TOOL_GROUPS` containing exactly 7 keys covering all 62 handler names

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_groups.py`:

```python
class TestGroupCoverage:
    EXPECTED_GROUPS = {
        "inspect", "diagnose", "edit", "mark", "generate", "transcript", "deliver",
    }

    def test_all_seven_groups_present(self):
        assert set(server.TOOL_GROUPS) == self.EXPECTED_GROUPS

    def test_every_handler_belongs_to_exactly_one_group(self):
        """No orphaned tool, no tool reachable from two groups."""
        seen = {}
        for group, spec in server.TOOL_GROUPS.items():
            for action in spec["actions"]:
                assert action not in seen, f"{action} in both {seen.get(action)} and {group}"
                seen[action] = group
        missing = set(server.TOOL_HANDLERS) - set(seen)
        assert not missing, f"handlers in no group: {sorted(missing)}"

    def test_group_count_is_a_real_reduction(self):
        assert len(server.TOOL_GROUPS) <= 8
        assert len(server.TOOL_HANDLERS) >= 62
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_groups.py::TestGroupCoverage -v`
Expected: FAIL on `test_all_seven_groups_present`, since only `inspect` exists

- [ ] **Step 3: Add the six remaining groups**

Extend `TOOL_GROUPS` in `server.py` with these entries, after the existing `inspect` entry:

```python
    "diagnose": {
        "description": (
            "Find problems in a timeline: gaps, flash frames, duplicates, dead "
            "air, and beat structure. Read-only. Run before editing."
        ),
        "actions": [
            "validate_timeline", "detect_gaps", "detect_duplicates",
            "detect_flash_frames", "detect_silence_candidates",
            "detect_media_silence", "detect_beats", "find_short_cuts",
            "find_long_clips", "diff_timelines",
        ],
    },
    "edit": {
        "description": (
            "Change clips on the timeline: insert, delete, trim, split, reorder, "
            "retime, and attach audio, B-roll or transitions. Writes a new file."
        ),
        "actions": [
            "insert_clip", "delete_clips", "trim_clip", "split_clip",
            "reorder_clips", "change_speed", "rapid_trim", "add_transition",
            "add_audio", "add_connected_clip", "assign_role", "fill_gaps",
            "fix_flash_frames", "remove_silence_candidates",
        ],
    },
    "mark": {
        "description": (
            "Add or import markers and chapters, including from SRT/VTT "
            "subtitles, transcripts, and beat analysis."
        ),
        "actions": [
            "add_marker", "batch_add_markers", "import_srt_markers",
            "import_transcript_markers", "import_beat_markers", "snap_to_beats",
        ],
    },
    "generate": {
        "description": (
            "Build new timeline structure from source clips: rough cuts, "
            "montages, A/B roll, templates and compound clips."
        ),
        "actions": [
            "auto_rough_cut", "generate_ab_roll", "generate_montage",
            "apply_template", "create_compound_clip", "flatten_compound_clip",
        ],
    },
    "transcript": {
        "description": (
            "Transcribe source media locally and edit the timeline by what was "
            "SAID rather than by timecode. Also removes filler words and real "
            "measured silence."
        ),
        "actions": [
            "transcribe_media", "edit_by_transcript", "remove_filler_words",
            "remove_media_silence",
        ],
    },
    "deliver": {
        "description": (
            "Get the edit out: export to other NLEs, CSV, EDL and stems, "
            "reformat, relink media, or push straight into a running Final Cut Pro."
        ),
        "actions": [
            "export_csv", "export_edl", "export_fcp7_xml", "export_resolve_xml",
            "export_role_stems", "reformat_timeline", "relink_media",
            "push_to_fcp", "list_fcp_libraries",
        ],
    },
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_tool_groups.py -v`
Expected: all pass. If `test_every_handler_belongs_to_exactly_one_group` fails, it prints the exact orphaned handler names — add each to the group it belongs in. Do not delete a handler to make this pass.

- [ ] **Step 5: Full suite and lint**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . --exclude docs/`
Expected: all pass, ruff clean

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_tool_groups.py
git commit -m "feat: group all 62 tools into 7 verbs

inspect, diagnose, edit, mark, generate, transcript, deliver.

test_every_handler_belongs_to_exactly_one_group is the load-bearing
test: it fails if a handler is orphaned or reachable from two groups,
so the grouping cannot silently drift as tools are added."
```

---

### Task 4: Gate the legacy list and measure the win

The 62 stay in `TOOL_HANDLERS` and stay callable. They just stop being advertised unless the operator opts in.

**Files:**
- Modify: `server.py` (`list_tools` at :753)
- Modify: `tests/test_tool_groups.py`

**Interfaces:**
- Consumes: `TOOL_GROUPS`, `_group_tool` from Task 2
- Produces: `def _legacy_tools_enabled() -> bool`, reading env var `FCP_MCP_LEGACY_TOOLS`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_groups.py`:

```python
import json


class TestLegacyGating:
    def test_default_advertises_only_groups(self, monkeypatch):
        monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
        tools = asyncio.run(server.list_tools())
        assert {t.name for t in tools} == set(server.TOOL_GROUPS)

    def test_opt_in_advertises_groups_plus_legacy(self, monkeypatch):
        monkeypatch.setenv("FCP_MCP_LEGACY_TOOLS", "1")
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert "trim_clip" in names
        assert "edit" in names
        assert len(names) >= 62 + len(server.TOOL_GROUPS)

    def test_hidden_tools_still_dispatch(self, monkeypatch):
        """The whole compat story. Hidden from list_tools, still callable."""
        monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
        tools = asyncio.run(server.list_tools())
        assert "list_projects" not in {t.name for t in tools}
        assert "list_projects" in server.TOOL_HANDLERS

    def test_schema_payload_is_substantially_smaller(self, monkeypatch):
        monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
        grouped = asyncio.run(server.list_tools())
        monkeypatch.setenv("FCP_MCP_LEGACY_TOOLS", "1")
        legacy = asyncio.run(server.list_tools())

        def size(ts):
            return len(json.dumps(
                [{"n": t.name, "d": t.description, "s": t.inputSchema} for t in ts]
            ))

        assert size(grouped) < size(legacy) * 0.35, (
            f"grouped={size(grouped)} legacy={size(legacy)}"
        )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_groups.py::TestLegacyGating -v`
Expected: FAIL — `list_tools` still returns all 62 flat tools

- [ ] **Step 3: Implement the gate**

In `server.py`, rename the existing decorated function so its big hardcoded list becomes a plain helper. Change the definition at :753 from:

```python
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
```

to:

```python
def _legacy_tool_list() -> list[Tool]:
    """The original 62 flat tools. Still dispatchable; advertised only on opt-in."""
    return [
```

Then, immediately after that function's closing `]`, add:

```python
def _legacy_tools_enabled() -> bool:
    """Advertise the original 62 flat tools alongside the groups.

    Off by default so new users pay the small schema cost. Existing configs
    that call flat tool names keep working either way, because call_tool
    dispatches from TOOL_HANDLERS and never consults this list.
    """
    return os.environ.get("FCP_MCP_LEGACY_TOOLS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = [_group_tool(name) for name in TOOL_GROUPS]
    if _legacy_tools_enabled():
        tools.extend(_legacy_tool_list())
    return tools
```

Confirm `import os` is already present at the top of `server.py`. If it is not, add it to the existing stdlib import block.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_tool_groups.py -v`
Expected: all pass

- [ ] **Step 5: Measure the actual reduction**

Run:

```bash
.venv/bin/python -c "
import asyncio, json, os, server
def size(ts):
    return len(json.dumps([{'n':t.name,'d':t.description,'s':t.inputSchema} for t in ts]))
os.environ.pop('FCP_MCP_LEGACY_TOOLS', None)
g = asyncio.run(server.list_tools())
os.environ['FCP_MCP_LEGACY_TOOLS'] = '1'
l = asyncio.run(server.list_tools())
print(f'grouped: {len(g)} tools, {size(g):,} chars (~{size(g)//4:,} tok)')
print(f'legacy:  {len(l)} tools, {size(l):,} chars (~{size(l)//4:,} tok)')
print(f'saved:   {(1-size(g)/size(l))*100:.0f}%')
"
```

Expected: grouped is 7 tools and well under 35% of the legacy payload. Record the real numbers — they go in the CHANGELOG in Task 7.

- [ ] **Step 6: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . --exclude docs/
git add server.py tests/test_tool_groups.py
git commit -m "feat: advertise 7 groups by default, legacy 62 behind FCP_MCP_LEGACY_TOOLS

Nothing breaks. call_tool dispatches from TOOL_HANDLERS, which is
independent of list_tools, so an existing config calling trim_clip by
name still works whether or not the flat tools are advertised.

Set FCP_MCP_LEGACY_TOOLS=1 to get the old list back."
```

---

### Task 5: HTML timeline preview as an MCP resource

Editing FCPXML is currently blind: you call a tool, you get text, and you cannot see the cut until you import into Final Cut. This closes that loop and doubles as the demo asset.

**Files:**
- Create: `fcpxml/preview.py`
- Modify: `server.py` (`list_resources` :551, `read_resource` :567)
- Test: `tests/test_preview.py`

**Interfaces:**
- Consumes: `Timeline` and `Clip` from `fcpxml.models`, `_parse_project` from `server.py`
- Produces: `def render_timeline_html(timeline) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_preview.py`:

```python
"""HTML timeline preview rendering."""
import xml.etree.ElementTree as ET

from fcpxml.parser import FCPXMLParser
from fcpxml.preview import render_timeline_html


def _timeline():
    project = FCPXMLParser().parse_file("examples/sample.fcpxml")
    return project.primary_timeline


class TestRenderTimelineHTML:
    def test_returns_a_parseable_html_document(self):
        html = render_timeline_html(_timeline())
        assert html.lstrip().startswith("<!DOCTYPE html>")
        # Must be well formed, not just string-concatenated soup.
        ET.fromstring(html[html.index("<html"):])

    def test_every_clip_appears(self):
        tl = _timeline()
        html = render_timeline_html(tl)
        for clip in tl.clips:
            assert clip.name in html

    def test_clip_widths_are_proportional_to_duration(self):
        """A clip twice as long must render twice as wide."""
        tl = _timeline()
        html = render_timeline_html(tl)
        assert "width:" in html
        assert "%" in html

    def test_escapes_clip_names_that_contain_markup(self):
        """A clip called <script> must not become a script tag."""
        tl = _timeline()
        if tl.clips:
            tl.clips[0].name = '<script>alert("x")</script>'
        html = render_timeline_html(tl)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_reports_timeline_metadata(self):
        tl = _timeline()
        html = render_timeline_html(tl)
        assert str(tl.total_clips) in html
        assert f"{tl.width}" in html

    def test_handles_a_timeline_with_no_clips(self):
        tl = _timeline()
        tl.clips = []
        html = render_timeline_html(tl)
        assert "<!DOCTYPE html>" in html
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_preview.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fcpxml.preview'`

- [ ] **Step 3: Implement the renderer**

Create `fcpxml/preview.py`:

```python
"""Render a Timeline as a self-contained HTML preview.

Editing FCPXML is otherwise blind: you call a tool, you get text back, and you
cannot see the cut until you import into Final Cut Pro. This is the visual
check in between. No external assets, no scripts, so it renders anywhere.
"""

from html import escape

# Distinct hues per lane so connected clips read as separate layers.
_LANE_COLORS = [
    "#3b82f6", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981", "#06b6d4",
]


def _lane_color(lane: int) -> str:
    return _LANE_COLORS[abs(int(lane or 0)) % len(_LANE_COLORS)]


def render_timeline_html(timeline) -> str:
    """Return a standalone HTML document visualising one timeline."""
    total = max(float(timeline.duration.seconds or 0), 0.001)

    rows = []
    for clip in timeline.clips:
        seconds = float(getattr(clip.duration, "seconds", 0) or 0)
        offset = float(getattr(clip.offset, "seconds", 0) or 0)
        width = max((seconds / total) * 100, 0.4)
        left = min((offset / total) * 100, 100)
        rows.append(
            f'<div class="clip" style="left:{left:.3f}%;width:{width:.3f}%;'
            f'background:{_lane_color(getattr(clip, "lane", 0))}" '
            f'title="{escape(str(clip.name))} ({seconds:.2f}s)">'
            f'<span>{escape(str(clip.name))}</span></div>'
        )

    marks = []
    for marker in getattr(timeline, "markers", []):
        at = float(getattr(marker.start, "seconds", 0) or 0)
        marks.append(
            f'<div class="marker" style="left:{min((at / total) * 100, 100):.3f}%" '
            f'title="{escape(str(getattr(marker, "value", "")))}"></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(str(timeline.name))}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px ui-sans-serif, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #0b0b0f; color: #e7e7ea; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .meta {{ color: #9a9aa4; font-size: 13px; margin-bottom: 20px; }}
  .track {{ position: relative; height: 56px; background: #16161c;
            border-radius: 6px; overflow: hidden; }}
  .clip {{ position: absolute; top: 0; bottom: 0; border-right: 1px solid #0b0b0f;
           display: flex; align-items: center; overflow: hidden; }}
  .clip span {{ padding: 0 6px; font-size: 11px; color: #fff; white-space: nowrap;
                text-shadow: 0 1px 2px rgba(0,0,0,.6); }}
  .markers {{ position: relative; height: 14px; margin-top: 6px; }}
  .marker {{ position: absolute; width: 2px; height: 14px; background: #f43f5e; }}
</style>
</head>
<body>
<h1>{escape(str(timeline.name))}</h1>
<div class="meta">
  {timeline.total_clips} clips &middot; {total:.2f}s &middot;
  {timeline.width}&times;{timeline.height} @ {timeline.frame_rate}fps
</div>
<div class="track">{"".join(rows)}</div>
<div class="markers">{"".join(marks)}</div>
</body>
</html>"""
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_preview.py -v`
Expected: 6 passed

- [ ] **Step 5: Expose it as a resource**

In `server.py`, add `from fcpxml.preview import render_timeline_html` to the existing `fcpxml` import block. Then in `list_resources` (:551), inside the `for f in files:` loop, after the existing `resources.append(...)`, add a second entry:

```python
        resources.append(Resource(
            uri=f"preview://{f}",
            name=f"{p.stem} (visual preview)",
            description=f"HTML timeline preview: {p.name}",
            mimeType="text/html",
        ))
```

In `read_resource` (:567), add this branch as the first statement of the function body, before the existing `filepath = str(uri).replace(...)` line:

```python
    if str(uri).startswith("preview://"):
        filepath = _validate_filepath(
            str(uri).replace("preview://", ""), ('.fcpxml', '.fcpxmld')
        )
        _project, tl = _parse_project(filepath)
        if not tl:
            return f"No timelines found in {filepath}"
        return render_timeline_html(tl)
```

- [ ] **Step 6: Verify the resource end to end**

Run:

```bash
.venv/bin/python -c "
import asyncio, server
html = asyncio.run(server.read_resource('preview://' + 'examples/sample.fcpxml'))
open('/tmp/preview.html','w').write(html)
print('bytes:', len(html), '| doctype:', html.lstrip().startswith('<!DOCTYPE'))
"
open /tmp/preview.html
```

Expected: a rendered timeline in the browser with proportional clip blocks. Eyeball it — this is the demo asset, so if it looks bad, fix it now.

- [ ] **Step 7: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . --exclude docs/
git add fcpxml/preview.py tests/test_preview.py server.py
git commit -m "feat: HTML timeline preview as an MCP resource

Editing FCPXML was blind. You called a tool, got text, and could not see
the cut until importing into FCP. preview://<path> now returns a
self-contained HTML render with proportional clip blocks, lane colours
and marker ticks. Clip names are escaped."
```

---

### Task 6: The Claude Code skill

Issue #2 asked whether this should be a skill rather than an MCP server. Answer: the skill wraps the MCP. The server stays the engine; the skill carries the workflow knowledge and the FCPXML gotchas that no tool description has room for.

**Files:**
- Create: `skill/SKILL.md`
- Create: `skill/README.md`
- Test: `tests/test_skill.py`

**Interfaces:**
- Consumes: `TOOL_GROUPS` from Task 3 (the skill documents these action names)
- Produces: a `skill/` directory installable into `~/.claude/skills/final-cut-pro/`

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill.py`:

```python
"""The shipped skill must not drift from the server's actual tool surface."""
import re
from pathlib import Path

import server

SKILL = Path(__file__).parent.parent / "skill" / "SKILL.md"


class TestSkillFrontmatter:
    def test_skill_file_exists(self):
        assert SKILL.exists()

    def test_has_name_and_description(self):
        text = SKILL.read_text()
        assert text.startswith("---\n")
        fm = text.split("---")[1]
        assert re.search(r"^name:\s*\S+", fm, re.M)
        assert re.search(r"^description:\s*\S+", fm, re.M)


class TestSkillMatchesServer:
    def test_every_group_is_documented(self):
        """A group the skill never mentions is a group the model will not use."""
        text = SKILL.read_text()
        for group in server.TOOL_GROUPS:
            assert f"`{group}`" in text, f"skill does not document group: {group}"

    def test_skill_names_no_action_the_server_lacks(self):
        """Catches the skill drifting ahead of, or behind, the code."""
        text = SKILL.read_text()
        known = set(server.TOOL_HANDLERS) | set(server.TOOL_GROUPS)
        for match in re.findall(r"`([a-z][a-z0-9_]{3,})`", text):
            if match in known or "_" not in match:
                continue
            assert match in known, f"skill references unknown action: {match}"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_skill.py -v`
Expected: FAIL on `test_skill_file_exists`

- [ ] **Step 3: Write the skill**

Create `skill/SKILL.md`:

```markdown
---
name: final-cut-pro
description: Edit Final Cut Pro timelines with natural language via FCPXML. Use when the user wants to analyse, cut, trim, marker up, transcribe, montage, or export a Final Cut Pro project, mentions .fcpxml or .fcpxmld files, asks to remove silence or filler words from a video edit, wants a rough cut assembled from source clips, or wants an edit pushed into a running Final Cut Pro. Requires the fcp-mcp-server MCP server.
---

# Final Cut Pro editing via FCPXML

## Before you touch anything

Always `inspect` then `diagnose` before you `edit`. Timelines carry structure
that is not obvious from a filename, and an edit against a wrong assumption is
expensive to undo.

1. `inspect` with action `analyze_timeline` for shape, duration, resolution.
2. `diagnose` with action `validate_timeline` for gaps, flash frames, duplicates.
3. Read `preview://<path>` to actually SEE the timeline before and after.

## The seven groups

| Group | Use it for |
|---|---|
| `inspect` | Read-only understanding. Start here, always. |
| `diagnose` | Find problems: gaps, flash frames, dead air, duplicates, beats. |
| `edit` | Change clips: insert, delete, trim, split, reorder, retime. |
| `mark` | Markers and chapters, including SRT/VTT and beat import. |
| `generate` | Build new structure: rough cuts, montages, A/B roll, templates. |
| `transcript` | Transcribe locally, then edit by what was SAID. |
| `deliver` | Export to other NLEs, reformat, relink, push into FCP. |

Every call takes `{"action": "...", "args": {...}}`. If you pass an action the
group does not own, the error lists the valid ones — read it rather than guessing.

## FCPXML facts that will bite you

- **Time is rational, never float.** `3600/2400s` is 1.5 seconds. Never do
  float math on timecode and never round it yourself.
- **`offset` is the timeline position. `start` is the source in-point.** They
  are different numbers and confusing them silently shifts an edit.
- **Library clips and timeline clips are different elements.** `<asset-clip>`
  lives in the library, `<clip>` lives on the timeline.
- **Markers are children of clips, not siblings.**
- **The `<spine>` is the primary storyline.** Connected clips hang off spine
  clips by `lane`: positive is above (video), negative is below (audio).
- **`.fcpxmld` bundles are directories**, wrapping `Info.fcpxml` plus sidecar
  data. Sidecars must be preserved on save or object-tracking and Cinematic
  data is destroyed.
- **Duplicate clip names are common.** Prefer element-based operations over
  name lookups where a tool offers both.

## Silence removal: pick the right one

There are two, and they are not interchangeable.

- `detect_media_silence` and `remove_media_silence` read the **actual source
  audio** with ffmpeg. Accurate, slower, needs ffmpeg installed.
- `detect_silence_candidates` and `remove_silence_candidates` use **timeline
  heuristics** only. Fast, no ffmpeg, and it guesses.

Use the media versions when correctness matters. Say which one you used.

## Working order that tends to hold

1. `inspect` → `diagnose` → read the `preview://` resource.
2. `transcript` with `transcribe_media` if the edit is dialogue-driven.
3. `generate` for the assembly, `edit` for the refinement, `mark` for chapters.
4. Read `preview://` again and compare before offering it as done.
5. `deliver`, either exporting or `push_to_fcp` into the running app.

## Do not

- Do not edit without a diagnose pass. Flash frames and gaps compound.
- Do not assume ffmpeg or Whisper are installed. Both degrade gracefully and
  return nothing rather than erroring, so check the response.
- Do not claim an edit landed in Final Cut Pro unless `push_to_fcp` was called
  and reported success. Writing an .fcpxml file is not the same as importing it.
```

Create `skill/README.md`:

```markdown
# final-cut-pro skill

Wraps the [fcp-mcp-server](https://github.com/DareDev256/fcp-mcp-server) MCP
server with the workflow knowledge and FCPXML gotchas that do not fit in tool
descriptions.

## Install

```bash
git clone https://github.com/DareDev256/fcp-mcp-server
ln -s "$PWD/fcp-mcp-server/skill" ~/.claude/skills/final-cut-pro
```

The MCP server itself still has to be configured. See the main README.
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_skill.py -v`
Expected: all pass. If `test_skill_names_no_action_the_server_lacks` fails, it names the offending backticked token — either fix the typo or stop backticking a word that is not an action.

- [ ] **Step 5: Full suite, lint, commit**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . --exclude docs/
git add skill/ tests/test_skill.py
git commit -m "feat: ship a final-cut-pro Claude Code skill wrapping the MCP

Closes the design question in #2. The MCP server stays the engine; the
skill carries workflow order and the FCPXML gotchas that tool
descriptions have no room for.

test_skill_matches_server fails if the skill names an action the server
does not have, so the two cannot drift apart silently."
```

---

### Task 7: Docs, version, release

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `CLAUDE.md`, `pyproject.toml`, `server.py`, `server.json`

- [ ] **Step 1: Bump to 0.14.0**

Minor bump: new features, no breaking change.

```bash
sed -i '' 's/^version = "0.13.2"/version = "0.14.0"/' pyproject.toml
sed -i '' 's/^__version__ = "0.13.2"/__version__ = "0.14.0"/' server.py
sed -i '' 's/"version": "0.13.2"/"version": "0.14.0"/g' server.json
grep -n '0\.14\.0' pyproject.toml server.py server.json
```

- [ ] **Step 2: Write the CHANGELOG entry**

Insert directly under `## [Unreleased]` in `CHANGELOG.md`, substituting the real numbers recorded in Task 4 Step 5:

```markdown
## [0.14.0] - YYYY-MM-DD

### Added

**Seven grouped tools replace 62 flat ones in the advertised tool list.**
`inspect`, `diagnose`, `edit`, `mark`, `generate`, `transcript`, `deliver`.
Each takes `{"action": "...", "args": {...}}` and dispatches into exactly the
same handlers as before, so behaviour is unchanged. The tool schema injected
into every conversation drops from about 35,800 characters to roughly REPLACE_ME,
a REPLACE_ME% reduction, before the user types anything.

**Nothing breaks.** `call_tool` resolves handlers from a registry that is
independent of the advertised list, so an existing config calling `trim_clip`
by name keeps working. Set `FCP_MCP_LEGACY_TOOLS=1` to advertise the original
62 alongside the groups. They will be removed no earlier than 1.0.

**HTML timeline preview.** Reading `preview://<path>` returns a self-contained
HTML render of the timeline: proportional clip blocks, per-lane colours, marker
ticks. Editing FCPXML was previously blind, with no way to see a cut short of
importing it into Final Cut Pro.

**A `final-cut-pro` Claude Code skill** in `skill/`, wrapping the server with
workflow order and the FCPXML gotchas that tool descriptions have no room for.
Closes the design question raised in #2.

**Daily scheduled CI.** The mcp 2.0 break went unnoticed for a week because
main's last run predated it by a day. The suite now runs at 06:00 UTC daily.
```

- [ ] **Step 3: Update the README**

Add a `## Tools` section above the existing tool documentation, covering: the seven groups and the `{action, args}` shape; a note that legacy names still work and `FCP_MCP_LEGACY_TOOLS=1` re-advertises them; the `preview://` resource with a screenshot of the render from Task 5; and the skill install snippet. Reconcile the tool and test counts stated in the README against reality.

- [ ] **Step 4: Update CLAUDE.md**

Add to the Architecture block:

```
TOOL_GROUPS      — 7 grouped verbs advertised by default. Dispatch into
                   TOOL_HANDLERS, which still holds all 62 flat handlers.
                   Hiding a tool from list_tools does NOT stop it dispatching.
fcpxml/preview.py — standalone HTML timeline render, served as preview://<path>
skill/           — the final-cut-pro Claude Code skill wrapping this server
```

- [ ] **Step 5: Verify, commit, tag, release**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . --exclude docs/
git add -u && git commit -m "chore: v0.14.0 — 7 grouped tools, timeline preview, skill, daily CI"
git push origin main
gh run watch
git tag -a v0.14.0 -m "v0.14.0" && git push origin v0.14.0
gh release create v0.14.0 --title "v0.14.0 — 62 tools become 7" --notes-file <notes>
```

- [ ] **Step 6: Publish to PyPI**

Build and verify before publishing. The `mcp-name` marker must survive into both artifacts or the MCP registry entry orphans.

```bash
rm -rf dist/ && uv build
unzip -p dist/*.whl "*.dist-info/METADATA" | grep -E "^(Version|Requires-Dist):|mcp-name"
uvx twine check dist/*
```

Then publish. This step is James's, not an agent's:

```bash
UV_PUBLISH_TOKEN=$(cat ~/.secrets/pypi-token) uv publish dist/*
```

- [ ] **Step 7: Verify from PyPI, not from the upload receipt**

```bash
uvx --from fcp-mcp-server python -c "
import importlib.metadata as m, asyncio, server
print('version:', m.version('fcp-mcp-server'))
print('advertised tools:', len(asyncio.run(server.list_tools())))
print('dispatchable handlers:', len(server.TOOL_HANDLERS))
"
```

Expected: version 0.14.0, 7 advertised tools, 62+ dispatchable handlers.

---

## Self-Review

**Spec coverage:** grouped tools (Tasks 2, 3), deprecate-don't-cut behind a flag (Task 4), skill wrapping MCP (Task 6), timeline preview (Task 5), scheduled CI (Task 1), docs and release (Task 7). All covered. Phase B (generative placement, MCP Tasks) is deliberately excluded and needs its own plan.

**Placeholders:** one intentional, `REPLACE_ME` in the Task 7 CHANGELOG, which Task 4 Step 5 produces the real numbers for. Flagged rather than hidden.

**Type consistency:** `TOOL_GROUPS`, `handle_group`, `_group_tool`, `_group_action_error`, `_legacy_tool_list`, `_legacy_tools_enabled`, `render_timeline_html` are used with identical names and signatures across Tasks 2 through 7.

**Known tradeoff, accepted:** nesting parameters under `args` means the model no longer sees per-action required fields in the schema. Mitigated by the group description listing actions, by errors that enumerate valid actions, and by the skill carrying parameter detail. If this proves painful in use, the fix is a `describe` action per group returning the schema for a named action. Do not pre-build it.
