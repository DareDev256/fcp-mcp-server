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


def test_pyproject_summary_fits_pypi():
    """PyPI rejects a `description` over 512 characters at upload time —
    after the tag, the release and the test job all read green. v0.19.1
    died there. Fail here instead, before anything is tagged. (Regex rather
    than tomllib: the floor is Python 3.10.)"""
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    m = re.search(r'^description = "(.*)"$', text, re.M)
    assert m, "no description in pyproject.toml"
    assert len(m.group(1)) <= 512, f"pyproject description is {len(m.group(1))} chars; PyPI caps it at 512"
