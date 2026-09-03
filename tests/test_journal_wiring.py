"""Every write is journaled through one seam; renders are too."""

import shutil
from pathlib import Path

import pytest

import server
from fcpxml import journal

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, p)
    return p


@pytest.mark.asyncio
async def test_flat_write_is_journaled(project):
    res = await server.call_tool(
        "add_marker", {"filepath": str(project), "timecode": "00:00:01:00", "name": "j"}
    )
    assert "Saved to" in res[0].text
    rows = journal.records(str(project))
    assert len(rows) == 1 and rows[0]["tool"] == "add_marker" and rows[0]["action"] == "add_marker"
    assert Path(rows[0]["output"]["path"]).is_file()
    assert rows[0]["input"]["sha256"] == journal.file_hash(str(project))
    assert rows[0]["output"]["sha256"] == journal.file_hash(rows[0]["output"]["path"])


@pytest.mark.asyncio
async def test_group_write_is_journaled_with_group_name(project):
    await server.call_tool("mark", {"action": "add_marker", "args": {
        "filepath": str(project), "timecode": "00:00:01:00", "name": "j"}})
    rows = journal.records(str(project))
    assert rows[-1]["tool"] == "mark" and rows[-1]["action"] == "add_marker"


@pytest.mark.asyncio
async def test_read_only_call_is_not_journaled(project):
    await server.call_tool("inspect", {"action": "get_timeline_info", "args": {"filepath": str(project)}})
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_failed_write_is_not_journaled(project):
    res = await server.call_tool(
        "add_marker", {"filepath": str(project), "timecode": "garbage", "name": "j"}
    )
    assert "Saved to" not in res[0].text
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_journal_off_records_nothing(project, monkeypatch):
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    await server.call_tool(
        "add_marker", {"filepath": str(project), "timecode": "00:00:01:00", "name": "j"}
    )
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_preview_render_records_proxy(project, monkeypatch):
    from tools import preview as pv

    out = project.parent / "sample_proxy.mp4"

    def fake_render(tl, out_path=None, height=480):
        out.write_bytes(b"mp4")
        return {"path": str(out), "expected": 1, "duration": 1, "drift": 0}

    monkeypatch.setattr(pv.render, "render_proxy", fake_render)
    monkeypatch.setattr(pv, "_open_beside_terminal", lambda paths: None)
    await server.call_tool("preview", {"action": "preview_render", "args": {"filepath": str(project)}})
    assert journal.reviewed(str(project))["output"]["path"] == str(out)
