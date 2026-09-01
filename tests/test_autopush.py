"""FCP_MCP_AUTOPUSH — every write also lands in the running Final Cut Pro.

Off by default. Repeated imports accumulate library churn, and that is the
operator's call to make, not a default to inflict on them.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

import server

SAMPLE = str(Path(__file__).parent.parent / "examples" / "sample.fcpxml")


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FCP_MCP_AUTOPUSH", raising=False)
    assert server._autopush_enabled() is False


def test_enabled_by_the_environment(monkeypatch):
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    assert server._autopush_enabled() is True


def test_off_switches_are_honoured(monkeypatch):
    for value in ("0", "", "false", "no", "off", "  OFF  "):
        monkeypatch.setenv("FCP_MCP_AUTOPUSH", value)
        assert server._autopush_enabled() is False, value


def test_disabled_produces_no_line(monkeypatch):
    monkeypatch.delenv("FCP_MCP_AUTOPUSH", raising=False)
    assert server._maybe_autopush("/tmp/x.fcpxml") == ""


def test_a_push_failure_never_fails_the_edit(monkeypatch):
    """The edit succeeded and is on disk. A failed push is a note."""
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")

    def boom(*args, **kwargs):
        raise RuntimeError("no Automation permission")

    monkeypatch.setattr(server.live, "push_to_fcp", boom)
    line = server._maybe_autopush("/tmp/x.fcpxml")
    assert "no Automation permission" in line
    assert "not pushed" in line


def test_a_successful_push_is_reported(monkeypatch):
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    monkeypatch.setattr(
        server.live, "push_to_fcp",
        lambda path, *a, **k: {"sent": path, "launched_fcp": False, "stdout": ""},
    )
    assert "Pushed to Final Cut Pro" in server._maybe_autopush("/tmp/x.fcpxml")


def test_a_launch_is_reported_because_it_is_a_side_effect(monkeypatch):
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    monkeypatch.setattr(
        server.live, "push_to_fcp",
        lambda path, *a, **k: {"sent": path, "launched_fcp": True, "stdout": ""},
    )
    assert "launched Final Cut Pro" in server._maybe_autopush("/tmp/x.fcpxml")


@pytest.fixture
def project(tmp_path):
    target = tmp_path / "in.fcpxml"
    shutil.copy(SAMPLE, target)
    return target


def test_a_wired_handler_appends_the_autopush_line(monkeypatch, project):
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    pushed = []
    monkeypatch.setattr(
        server.live, "push_to_fcp",
        lambda path, *a, **k: (pushed.append(path), {
            "sent": path, "launched_fcp": False, "stdout": ""
        })[1],
    )
    result = asyncio.run(server.handle_add_marker({
        "filepath": str(project), "timecode": "1.0", "name": "autopush probe",
    }))
    text = result[0].text
    assert "Pushed to Final Cut Pro" in text
    assert len(pushed) == 1
    assert pushed[0].endswith(".fcpxml")


def test_a_wired_handler_stays_silent_when_autopush_is_off(monkeypatch, project):
    monkeypatch.delenv("FCP_MCP_AUTOPUSH", raising=False)
    called = []
    monkeypatch.setattr(server.live, "push_to_fcp", lambda *a, **k: called.append(1))
    result = asyncio.run(server.handle_add_marker({
        "filepath": str(project), "timecode": "1.0", "name": "quiet",
    }))
    assert "Pushed to Final Cut Pro" not in result[0].text
    assert called == []


def test_the_edit_still_reports_success_when_the_push_fails(monkeypatch, project):
    """Mutation check on the swallow: the saved path must survive a bad push."""
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")

    def boom(*args, **kwargs):
        raise RuntimeError("Final Cut Pro is not installed")

    monkeypatch.setattr(server.live, "push_to_fcp", boom)
    result = asyncio.run(server.handle_add_marker({
        "filepath": str(project), "timecode": "1.0", "name": "resilient",
    }))
    text = result[0].text
    assert "Saved to:" in text
    assert "not pushed" in text
