"""Suite-wide fixtures.

The index is a cache under ``~/.fcp-mcp/`` in production. Tests must never
read or write the operator's real cache, so every test gets its own database
path — unless the run was started with ``FCP_MCP_INDEX=off``, which is the
"cache never load-bearing" pass and must stay disabled end to end.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path, monkeypatch):
    current = os.environ.get("FCP_MCP_INDEX", "")
    if current.strip().lower() in {"off", "0", "false", "no"}:
        return
    monkeypatch.setenv("FCP_MCP_INDEX", str(tmp_path / "index.db"))


def index_is_off() -> bool:
    return os.environ.get("FCP_MCP_INDEX", "").strip().lower() in {"off", "0", "false", "no"}


# Tests OF the cache need the cache. They are skipped on the index-off pass,
# whose job is to prove every OTHER test does not.
requires_index = pytest.mark.skipif(index_is_off(), reason="FCP_MCP_INDEX=off pass")


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path, monkeypatch):
    """The journal is a ledger under ``~/.fcp-mcp/``; tests never touch the real one."""
    monkeypatch.setenv("FCP_MCP_JOURNAL", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _never_paint_the_operators_screen(monkeypatch, tmp_path):
    """The preview tools paint their pictures into a cmux pane beside the
    terminal when ~/.claude/hooks/cmux-image-preview.mjs exists. A test run
    on the author's machine must not put test pictures on his screen."""
    from tools import preview

    monkeypatch.setattr(preview, "CMUX_PREVIEW", tmp_path / "no-cmux-preview.mjs")


@pytest.fixture(autouse=True)
def _never_open_final_cut(monkeypatch):
    """push_to_fcp falls back to Launch Services' ``open -a "Final Cut Pro"``
    when Apple Events are blocked (0.35.0). A test whose Apple Event is faked
    to fail reached that fallback for real on 2026-11-16 and opened a scratch
    library in the author's running Final Cut. No test may hand a document to
    Final Cut: a test that wants the fallback fakes ``subprocess.run`` itself,
    which replaces this guard."""
    import subprocess

    from fcpxml import live

    real = subprocess.run

    def guarded(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "open":
            raise AssertionError(f"a test tried to open a document in Final Cut Pro: {cmd}")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(live.subprocess, "run", guarded)


@pytest.fixture(autouse=True)
def _no_installed_final_cut(monkeypatch, tmp_path):
    """Live mode names the app after what is installed in /Applications (the
    trial is "Final Cut Pro Trial"). Tests see no installed app, so the name
    they read does not depend on the machine; tests/test_live_trial.py
    installs its own."""
    from fcpxml import live

    monkeypatch.setattr(live, "_APPLICATIONS", tmp_path / "no-applications")
