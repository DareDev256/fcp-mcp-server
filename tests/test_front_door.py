"""The front door says what is true today (0.31 "Front door").

The GitHub social card showed a version seventeen releases old, a frozen
star count, the repository's former name and a tagline the README had
dropped in January, long after each stopped being true. These checks keep
those values at zero everywhere outside the CHANGELOG (which is history and
keeps them), and hold the one display name with its subtitle.

The needles are built from parts so this file does not match itself.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NEEDLES = {
    "the retired tagline": re.compile("first AI" + "-powered", re.I),
    "the old name": re.compile("fcpxml" + "-mcp-server", re.I),
    "the retired Omni model": re.compile("omni" + r"-1\.1", re.I),
    "the version on the old card": re.compile(r"(?<![\d.])0\.13" + r"\.1(?!\d)"),
}

# Real identifiers that keep the old name because renaming them breaks
# something outside this repo: the MCP registry namespace is bound to the
# `mcp-name` marker PyPI publishes, and a registry search must use that name.
KEEP = re.compile(r"io\.github\.[\w-]+/fcpxml" + r"-mcp-server|search=fcpxml" + r"-mcp-server")

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".claude", "__pycache__", ".work", "build",
             "dist", ".pytest_cache", ".local-validation", ".ruff_cache"}
SKIP_FILES = {"CHANGELOG.md", Path(__file__).name}
TEXT = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".js", ".html", ".txt", ".sh", ".tape", ".cfg", ".svg"}


def text_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            path = Path(root) / name
            if name not in SKIP_FILES and path.suffix in TEXT:
                yield path


def stale_hits(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        cleaned = KEEP.sub("", line)
        hits += [label for label, needle in NEEDLES.items() if needle.search(cleaned)]
    return hits


def test_no_stale_value_outside_the_changelog():
    found = {}
    for path in text_files():
        hits = stale_hits(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            found[str(path.relative_to(REPO))] = sorted(set(hits))
    assert found == {}


def test_the_sweep_sees_each_stale_value():
    """Mutation check: each old value planted in a line is caught; the
    registry identity the old name survives in is not."""
    for planted in ("The first AI" + "-powered MCP server", "# fcpxml" + "-mcp-server",
                    "model: omni" + "-1.1-flash", "badge v0.13" + ".1"):
        assert stale_hits(planted), planted
    assert stale_hits("mcp-name: io.github.DareDev256/fcpxml" + "-mcp-server") == []


def test_the_sweep_actually_walks_the_repo():
    names = {p.relative_to(REPO).as_posix() for p in text_files()}
    assert {"README.md", "server.py", "fcpxml/gen.py", ".github/workflows/publish.yml"} <= names
    assert "CHANGELOG.md" not in names


# ---------------------------------------------------------------------------
# One name
# ---------------------------------------------------------------------------

NAME = "fcp-mcp-server"
SUBTITLE = "reads and writes FCPXML, no plugin, no patch"
BENEFIT = "Edit Final Cut Pro by talking to Claude, and watch the cut change."


def test_readme_opens_with_the_name_then_the_benefit_then_the_subtitle():
    lines = [line for line in (REPO / "README.md").read_text(encoding="utf-8").splitlines() if line.strip()]
    at = next(i for i, line in enumerate(lines) if line.startswith("# "))
    assert lines[at] == f"# {NAME}"
    assert lines[at + 1] == f"**{BENEFIT}**"
    # non-breaking spaces keep "no plugin, no patch" together on a phone
    assert lines[at + 2].replace("&nbsp;", " ") == SUBTITLE
    assert "no&nbsp;patch" in lines[at + 2]


def test_every_display_name_is_the_one_name():
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["displayName"] == NAME
    import server
    assert server.server.name == NAME


@pytest.mark.parametrize("path", ["README.md", "skills/fcpxml/SKILL.md", "docs/MCP_ECOSYSTEM.md"])
def test_the_old_display_name_is_gone(path):
    assert "FCPXML MCP" not in (REPO / path).read_text(encoding="utf-8")
