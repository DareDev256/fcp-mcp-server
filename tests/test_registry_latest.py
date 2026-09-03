"""The registry check must read the isLatest entry, not the first one."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "registry_latest.py"
spec = importlib.util.spec_from_file_location("registry_latest", SCRIPT)
registry_latest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_latest)

KEY = registry_latest.KEY
NAME = registry_latest.NAME


def _entry(version, is_latest, name=NAME):
    return {"server": {"name": name, "version": version}, "_meta": {KEY: {"isLatest": is_latest}}}


def test_reads_the_islatest_entry_not_the_first():
    payload = {"servers": [_entry("0.13.1", False), _entry("0.21.2", False), _entry("0.22.0", True)]}
    assert registry_latest.latest_version(payload) == "0.22.0"


def test_ignores_other_servers_that_match_the_search():
    payload = {"servers": [_entry("9.9.9", True, name="io.github.someone/fcpxml-mcp-server-fork"),
                           _entry("0.22.0", True)]}
    assert registry_latest.latest_version(payload) == "0.22.0"


def test_empty_when_nothing_is_latest():
    assert registry_latest.latest_version({"servers": [_entry("0.13.1", False)]}) == ""
    assert registry_latest.latest_version({}) == ""


def test_workflow_calls_the_script_not_an_inline_block():
    wf = (SCRIPT.parent.parent / "workflows" / "publish.yml").read_text()
    assert "python3 .github/scripts/registry_latest.py" in wf
    assert 'python3 -c "' not in wf
