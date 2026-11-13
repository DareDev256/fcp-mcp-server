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


FIRST_SCREEN = re.compile(r"^Python (\d+\.\d+)\+ · Final Cut Pro for Mac (\d+\.\d+) to (\d+\.\d+) ", re.M)


def first_screen_problems(readme: str, pyproject: str) -> list[str]:
    """The requirement line on the first screen (round 5: Python had dropped
    off it, and Final Cut's range was baked into a badge image) says what
    pyproject.toml and the Compatibility table say."""
    head = "\n".join(readme.splitlines()[:16])
    m = FIRST_SCREEN.search(head)
    if not m:
        return ["no 'Python X+ · Final Cut Pro for Mac A to B' line on the first screen"]
    out = []
    floor = re.search(r'requires-python = ">=(\d+\.\d+)"', pyproject).group(1)
    if m.group(1) != floor:
        out.append(f"first screen says Python {m.group(1)}+, pyproject requires >={floor}")
    row = re.search(r"^\| Final Cut Pro \| \*\*for Mac\*\* (\d+\.\d+)\+ through (\d+\.\d+)", readme, re.M)
    if not row or (row.group(1), row.group(2)) != (m.group(2), m.group(3)):
        out.append(f"first screen says Final Cut {m.group(2)} to {m.group(3)}, the Compatibility table "
                   f"{row.groups() if row else 'has no row'}")
    return out


def test_the_first_screen_states_the_requirements():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert first_screen_problems(readme, (REPO / "pyproject.toml").read_text(encoding="utf-8")) == []


def test_the_requirements_check_can_go_red():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert first_screen_problems(readme.replace("Python 3.10+ ·", "Python 3.9+ ·", 1), pyproject)
    assert first_screen_problems(readme.replace("for Mac 10.4 to 12.4 (", "for Mac 10.4 to 12.3 (", 1), pyproject)
    assert first_screen_problems(readme.replace("Python 3.10+ · Final Cut", "Final Cut", 1), pyproject)


# FROZEN since 0.31 round 6 (the coordinator's call): the social card and the
# logo. A hero re-render must not regenerate them (build.py writes the card's
# picture only with --card); changing one is a deliberate act that updates
# this pin in the same commit.
FROZEN = {
    "docs/assets/social-card.png": "8a3e9a93b88d075752b2012fbb06b550f1dd2fefd22b4ee809c63ff567f85943",
    "docs/assets/card-frame.png": "da40537231a852c12a6922960de1ab9b3e461b5baac0f171b072beacf9085826",
    "docs/assets/logo.svg": "fa7c7916d339e37bbcef2abfc80ca4334d7a3d82cfe1b9365213b3054d6bcaa9",
    "docs/assets/logo-small.svg": "15cfe199da6a7e1d1996e79befe75725f5da3e0cc3f13852046a15ccdcfd99cb",
    "docs/assets/logo-400.png": "4302206c442cee1153a97d1a0225dd746e45e84a56ec335c33b61fce616563a6",
    "docs/assets/logo-small-400.png": "bce6db1124c1f8e197d6c798aba1c7ce3f7df737f01d2b20267decf253402cb3",
}


def frozen_problems(root: Path, pins: dict) -> list:
    import hashlib
    out = []
    for rel, want in pins.items():
        f = root / rel
        got = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "missing"
        if got != want:
            out.append(f"{rel} changed ({got[:12]}, pinned {want[:12]})")
    return out


def test_the_card_and_the_logo_are_frozen():
    assert frozen_problems(REPO, FROZEN) == []


def test_the_freeze_sees_a_changed_byte(tmp_path):
    for rel in FROZEN:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((REPO / rel).read_bytes())
    card = tmp_path / "docs/assets/social-card.png"
    card.write_bytes(card.read_bytes() + b"\0")
    problems = frozen_problems(tmp_path, FROZEN)
    assert len(problems) == 1 and "social-card" in problems[0]


def test_an_ordinary_hero_build_leaves_the_card_alone():
    build = (REPO / "demo" / "hero" / "build.py").read_text(encoding="utf-8")
    record = (REPO / "demo" / "hero" / "record.js").read_text(encoding="utf-8")
    assert 'card = ["--card"] if "--card" in sys.argv else []' in build
    assert "if (WITH_CARD) await writeCardFrame(browser);" in record


def recorded_calls(build_source: str) -> list[str]:
    """The calls demo/hero/build.py puts in the hero's chat, in its order."""
    return re.findall(r'\{"at": [\d.]+, "call": "([^"]+)"', build_source)


def readme_calls(readme: str) -> list[str]:
    """The calls the README lists under the hero, in its order."""
    block = readme.split("<!-- hero-calls", 1)[1].split("<!-- /hero-calls -->", 1)[0]
    return re.findall(r"^\d+\. `([^`]+)`", block, flags=re.M)


def test_the_readme_lists_the_calls_the_hero_records():
    """At 390 px the GIF is the timeline alone; the chat is README text, so it
    must be the same calls in the same order as the recording."""
    recorded = recorded_calls((REPO / "demo" / "hero" / "build.py").read_text(encoding="utf-8"))
    assert len(recorded) == 6, recorded
    assert readme_calls((REPO / "README.md").read_text(encoding="utf-8")) == recorded


def test_the_call_list_check_can_go_red():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    recorded = recorded_calls((REPO / "demo" / "hero" / "build.py").read_text(encoding="utf-8"))
    dropped = readme.replace("6. `organize · history`", "6. organize · history", 1)
    swapped = readme.replace("2. `edit", "2. `EDIT", 1)
    assert readme_calls(dropped) != recorded
    assert readme_calls(swapped) != recorded


def test_every_display_name_is_the_one_name():
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["displayName"] == NAME
    import server
    assert server.server.name == NAME


@pytest.mark.parametrize("path", ["README.md", "skills/fcpxml/SKILL.md", "docs/MCP_ECOSYSTEM.md"])
def test_the_old_display_name_is_gone(path):
    assert "FCPXML MCP" not in (REPO / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The logo at small sizes is the no-glyph mark
# ---------------------------------------------------------------------------

SMALL_PX = 96
IMG = re.compile(r'<img[^>]*src="([^"]*logo[^"]*)"[^>]*width="(\d+)"')


def small_logos_with_glyphs(text: str) -> list[str]:
    """Every logo drawn at SMALL_PX or less that is not the no-glyph mark."""
    return [f"{src} at {w}px" for src, w in IMG.findall(text) if int(w) <= SMALL_PX and "logo-small" not in src]


def test_a_small_logo_is_the_no_glyph_mark():
    assert IMG.findall((REPO / "README.md").read_text(encoding="utf-8")), "the README header logo moved"
    assert small_logos_with_glyphs((REPO / "README.md").read_text(encoding="utf-8")) == []
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    import logo
    assert logo.SMALL == SMALL_PX
    assert "<polyline" not in logo.svg(glyphs=False) and "<polyline" in logo.svg()


def test_the_small_logo_rule_can_go_red():
    assert small_logos_with_glyphs('<img src="docs/assets/logo-400.png" width="72" height="72">')


# ---------------------------------------------------------------------------
# Nothing a release would make wrong is baked into an image
# ---------------------------------------------------------------------------

def _image_text():
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    import image_text
    return image_text


@pytest.mark.parametrize("baked", [
    "fal/kling-2.5 · $0.70 · refused until confirm=true",   # the 0.31 first-pass hero
    "ui://fcp/timeline · MCP Apps · 1920×1200",
    "generated: gemini/omni-1.2-flash — a skyline",
    "v0.13" + ".1",
    "71 stars",
])
def test_the_image_guard_sees_stale_text(baked):
    assert _image_text().stale_in(baked)


OWN_EXAMPLE = {
    "a price": "$0.70",
    "a model or provider id": "fal/kling-2.5",
    "a frame size": "1920×1200",
    "a version": "v0.13" + ".1",
    "a star count": "71 stars",
}


@pytest.mark.parametrize("label", sorted(OWN_EXAMPLE))
def test_each_rule_catches_its_own_example(label):
    """One example per rule, caught BY that rule: a rule removed from the
    guard cannot hide behind another rule matching the same string."""
    it = _image_text()
    assert label in it.STALE, f"the guard lost its rule for {label}"
    assert any(hit.startswith(label + ":") for hit in it.stale_in(OWN_EXAMPLE[label])), label


def test_every_rule_has_an_example():
    assert set(_image_text().STALE) == set(OWN_EXAMPLE)


@pytest.mark.parametrize("clean", [
    "Session cut", "4 clips · 37.0s", "2 rests cut · 6.33s removed", "00:00:10:00", "uvx fcp-mcp-server",
    "Edit Final Cut Pro by talking to Claude, and watch the cut change.",
    "quoted under the cap, refused until confirm=true",
])
def test_the_image_guard_passes_plain_text(clean):
    assert _image_text().stale_in(clean) == []


def test_the_app_may_state_its_own_frame_size():
    it = _image_text()
    assert it.stale_in("Session cut 7 clips · 30.667s · 1920×1080 @ 24fps", scope="app") == []
    assert it.stale_in("1920×1080", scope="host")


def test_both_image_builders_use_the_guard():
    assert "image_text.stale_in(" in (REPO / "scripts" / "social_card.py").read_text(encoding="utf-8")
    assert "image_text.patterns(" in (REPO / "demo" / "hero" / "build.py").read_text(encoding="utf-8")
    assert "refuseStale(" in (REPO / "demo" / "hero" / "record.js").read_text(encoding="utf-8")
