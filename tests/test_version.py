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


def _server_json():
    import json

    return json.loads((Path(__file__).resolve().parent.parent / "server.json").read_text())


def test_server_json_versions_match_pyproject():
    """server.json is what the MCP registry publishes. Both of its version
    fields have to move with the release, or the registry advertises a
    package version PyPI does not serve."""
    d = _server_json()
    assert d["version"] == server.__version__
    assert all(p["version"] == server.__version__ for p in d["packages"])


def test_server_json_description_fits_registry():
    """The MCP registry rejects a description over 100 characters with a
    422 — and nothing in the release path read that limit, so the registry
    sat at 0.13.1 for eight releases while the description grew to 208."""
    assert len(_server_json()["description"]) <= 100


def test_server_json_description_states_measured_counts():
    """The description names the group and operation counts. They are the
    surface a registry browser sees first, and they drifted to 11/79 while
    the server had 13/88 — the same stale-count failure the README tests
    exist for, on the one file none of them read."""
    desc = _server_json()["description"]
    assert f"{len(server.TOOL_GROUPS)} grouped tools" in desc
    assert f"{len(server.TOOL_HANDLERS)} operations" in desc
