"""deliver refuses an unreviewed cut and names the render that would satisfy it."""

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


def _render(path: Path) -> None:
    tok = journal.begin("preview", "preview_render", {"filepath": str(path)}, str(path))
    proxy = path.parent / "sample_proxy.mp4"
    proxy.write_bytes(b"mp4")
    journal.note_output(str(proxy))
    journal.finish(tok)


async def _export(project, **extra):
    res = await server.call_tool("deliver", {"action": "export_csv", "args": {"filepath": str(project), **extra}})
    return res[0].text


@pytest.mark.asyncio
async def test_export_refused_unreviewed_names_the_render_call(project):
    text = await _export(project)
    assert text.startswith("Refused:")
    assert '"action": "preview_render"' in text and str(project) in text and "confirm_unreviewed" in text
    assert list(project.parent.glob("*.csv")) == []


@pytest.mark.asyncio
async def test_export_allowed_after_render(project):
    _render(project)
    assert not (await _export(project)).startswith("Refused:")


@pytest.mark.asyncio
async def test_render_of_old_state_does_not_count(project):
    _render(project)
    project.write_text(project.read_text().replace("</fcpxml>", "<!-- edited --></fcpxml>"))
    assert (await _export(project)).startswith("Refused:")


@pytest.mark.asyncio
async def test_confirm_unreviewed_bypasses_and_says_so(project):
    text = await _export(project, confirm_unreviewed=True)
    assert not text.startswith("Refused:") and "unreviewed" in text.lower()


@pytest.mark.asyncio
async def test_flat_name_is_gated_too(project):
    res = await server.call_tool("export_csv", {"filepath": str(project)})
    assert res[0].text.startswith("Refused:")


@pytest.mark.asyncio
async def test_journal_off_refuses_and_names_it(project, monkeypatch):
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    res = await server.call_tool("export_csv", {"filepath": str(project)})
    assert res[0].text.startswith("Refused:") and "FCP_MCP_JOURNAL" in res[0].text


@pytest.mark.asyncio
async def test_read_only_deliver_action_not_gated():
    res = await server.call_tool("deliver", {"action": "list_fcp_libraries", "args": {}})
    assert not res[0].text.startswith("Refused:")


@pytest.mark.asyncio
async def test_edit_actions_not_gated(project):
    res = await server.call_tool("mark", {"action": "add_marker", "args": {
        "filepath": str(project), "timecode": "00:00:01:00", "name": "m"}})
    assert "Saved to" in res[0].text


def test_every_gated_action_is_a_deliver_action():
    assert server.GATED_ACTIONS <= set(server.TOOL_GROUPS["deliver"]["actions"])
    for name in server.GATED_ACTIONS:
        schema = server.tool_input_schema(next(t for t in server._legacy_tool_list() if t.name == name))
        assert "confirm_unreviewed" in schema["properties"], name


@pytest.mark.asyncio
async def test_review_gate_mutation(project, monkeypatch):
    """Delete the gate and this goes red: the refusal must come FROM the gate."""
    monkeypatch.setattr(server, "_review_gate", lambda action, arguments: None)
    res = await server.call_tool("export_csv", {"filepath": str(project)})
    assert not res[0].text.startswith("Refused:")
