"""organize_keywords / organize_rate / organize_roles / history / undo through the MCP seam."""

import os
import shutil
import xml.etree.ElementTree as ET

import pytest

import server
import tools

ADVERTISED = {"organize_keywords", "organize_rate", "organize_roles", "history", "undo", "organize_auto"}


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "sample.fcpxml"
    shutil.copy("examples/sample.fcpxml", dst)
    return str(dst)


async def _call(action, args):
    result = await server.call_tool("organize", {"action": action, "args": args})
    return result[0].text


@pytest.mark.xfail(strict=True, reason="organize_auto lands in Task 9")
def test_advertised_actions():
    assert set(tools.EXTRA_GROUPS["organize"]["actions"]) == ADVERTISED


async def test_keywords_writes_organized_copy(project):
    text = await _call("organize_keywords", {"filepath": project, "keywords": ["interview"]})
    out = project.replace(".fcpxml", "_organized.fcpxml")
    assert "Written:" in text and os.path.exists(out)
    root = ET.parse(out).getroot()
    assert any("interview" in k.get("value") for k in root.iter("keyword"))
    assert "changed" in text


async def test_no_match_writes_nothing(project):
    text = await _call("organize_keywords", {"filepath": project, "keywords": ["x"], "clip_name": "nope*"})
    assert "No clips matched" in text and "Nothing written" in text
    assert not os.path.exists(project.replace(".fcpxml", "_organized.fcpxml"))


async def test_rate_and_roles(project):
    text = await _call("organize_rate", {"filepath": project, "rating": "favorite"})
    assert "Rated favorite" in text
    out = project.replace(".fcpxml", "_organized.fcpxml")
    assert all(r.get("value") == "favorite" for r in ET.parse(out).getroot().iter("rating"))
    text = await _call("organize_roles", {"filepath": out, "audio_role": "dialogue.a"})
    assert "audio=dialogue.a" in text
    out2 = out.replace(".fcpxml", "_organized.fcpxml")
    assert all(c.get("audioRole") == "dialogue.a" for c in ET.parse(out2).getroot().iter("asset-clip"))


async def test_bad_args_are_validation_errors(project):
    assert "Validation error" in await _call("organize_keywords", {"filepath": project, "keywords": []})
    assert "Validation error" in await _call("organize_rate", {"filepath": project, "rating": "meh"})
    assert "Validation error" in await _call("organize_roles", {"filepath": project})
    assert "Validation error" in await _call("undo", {"filepath": project, "n": 0})


async def test_history_lists_the_write(project):
    assert "No operations recorded" in await _call("history", {"filepath": project})
    await _call("organize_keywords", {"filepath": project, "keywords": ["k"]})
    text = await _call("history", {"filepath": project})
    assert "| organize | organize_keywords | sample.fcpxml | sample_organized.fcpxml |" in text
    assert "ago" in text


async def test_undo_moves_output_and_records(project, tmp_path):
    assert "Nothing to undo" in await _call("undo", {"filepath": project})
    await _call("organize_keywords", {"filepath": project, "keywords": ["k"]})
    out = project.replace(".fcpxml", "_organized.fcpxml")
    text = await _call("undo", {"filepath": project})
    assert "moved to" in text and "nothing was deleted" in text
    assert not os.path.exists(out)
    undone = list((tmp_path / "journal" / "undone").rglob("*_organized.fcpxml"))
    assert len(undone) == 1
    assert "| organize | undo |" in await _call("history", {"filepath": project})
    assert "Nothing to undo" in await _call("undo", {"filepath": project})


async def test_undo_refuses_changed_output(project):
    await _call("organize_keywords", {"filepath": project, "keywords": ["k"]})
    out = project.replace(".fcpxml", "_organized.fcpxml")
    with open(out, "a") as f:
        f.write("\n<!-- edited -->")
    text = await _call("undo", {"filepath": project})
    assert "Validation error" in text and "refusing" in text
    assert os.path.exists(out)


async def test_journal_off_names_the_variable(project, monkeypatch):
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    for action in ("history", "undo"):
        assert "FCP_MCP_JOURNAL" in await _call(action, {"filepath": project})
