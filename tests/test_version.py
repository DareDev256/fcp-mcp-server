"""The version the MCP handshake reports must be the version that ships.

server.__version__ sat at 0.16.0 through the whole of 0.17.0 while
pyproject.toml said otherwise; nothing read both. This does.
"""

import re
from pathlib import Path

import server


def test_server_version_matches_pyproject():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"', text, re.M)
    assert m, "no version in pyproject.toml"
    assert server.__version__ == m.group(1)
