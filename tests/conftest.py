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
