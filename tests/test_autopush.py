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
    result = asyncio.run(server.call_tool("add_marker", {
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
    result = asyncio.run(server.call_tool("add_marker", {
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
    result = asyncio.run(server.call_tool("add_marker", {
        "filepath": str(project), "timecode": "1.0", "name": "resilient",
    }))
    text = result[0].text
    assert "Saved to:" in text
    assert "not pushed" in text


def _pushes(monkeypatch):
    pushed = []
    monkeypatch.setattr(
        server.live, "push_to_fcp",
        lambda path, *a, **k: (pushed.append(path), {
            "sent": path, "launched_fcp": False, "stdout": ""
        })[1],
    )
    return pushed


def test_every_write_handler_pushes_through_the_seam(monkeypatch, project):
    """trim_clip never called _maybe_autopush itself. It does not need to:
    autopush reads the same ledger the journal does, so any handler that
    writes an FCPXML gets it. Through a group call too."""
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    pushed = _pushes(monkeypatch)
    result = asyncio.run(server.call_tool("edit", {
        "action": "trim_clip",
        "args": {"filepath": str(project), "clip_id": "Interview_A", "trim_start": "+12f"},
    }))
    assert "Pushed to Final Cut Pro" in result[0].text
    assert len(pushed) == 1 and pushed[0].endswith(".fcpxml")


def test_a_read_only_call_pushes_nothing(monkeypatch, project):
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    pushed = _pushes(monkeypatch)
    result = asyncio.run(server.call_tool("analyze_timeline", {"filepath": str(project)}))
    assert "Pushed" not in result[0].text
    assert pushed == []


def test_push_to_fcp_is_not_pushed_twice(monkeypatch, project):
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    pushed = _pushes(monkeypatch)
    asyncio.run(server.call_tool("push_to_fcp", {
        "filepath": str(project), "confirm_unreviewed": True,
    }))
    assert len(pushed) <= 1


def test_the_seam_pushes_only_fcpxml_outputs(monkeypatch, project, tmp_path):
    """A JSON sidecar written by a handler is an output the journal records
    and NOT something to import into Final Cut Pro."""
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    pushed = _pushes(monkeypatch)
    token = server._journal.begin("t", "export_csv", {}, str(project))
    side = tmp_path / "in_export.csv"
    side.write_text("a,b")
    server._journal.note_output(str(side))
    written = server._journal.finish(token)
    assert written == [str(side)]
    assert all(p.endswith((".fcpxml", ".fcpxmld")) for p in pushed)
