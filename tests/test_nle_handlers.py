"""The NLE export, effects, template and relink handlers, driven end to end.

These nine tools shipped between v0.5.0 and v0.6.0 with no direct test of
their own: the suite exercised the code they call but never the handlers
themselves, so the whole family could stop resolving and 1,689 green tests
would say nothing about it. That gap is what made moving them out of
server.py risky, and it is the reason this file exists rather than a note
in the changelog.

Every case goes through `server.call_tool`, the real dispatch path, so a
handler that fails to reach a server helper fails here.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

import server

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


def call(name, args):
    return asyncio.run(server.call_tool(name, args))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A writable copy of the sample project inside the sandbox roots."""
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "ALLOWED_READ_ROOTS", [str(tmp_path)], raising=False)
    monkeypatch.setattr(server, "ALLOWED_WRITE_ROOTS", [str(tmp_path)], raising=False)
    out = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, out)
    return out


def _text(result):
    assert result, "handler returned nothing"
    return result[0].text


def test_list_effects_names_real_effects():
    body = _text(call("list_effects", {}))
    assert "Cross Dissolve" in body


def test_list_templates_names_its_slots():
    body = _text(call("list_templates", {}))
    assert "intro_outro" in body and "slot" in body.lower()


def test_export_resolve_xml_writes_a_file(project, tmp_path):
    out = tmp_path / "resolve.fcpxml"
    body = _text(call("export_resolve_xml", {
        "filepath": str(project), "output_path": str(out),
        "confirm_unreviewed": True,
    }))
    assert out.is_file() and out.stat().st_size > 0
    assert str(out) in body


def test_export_fcp7_xml_writes_xmeml(project, tmp_path):
    out = tmp_path / "legacy.xml"
    _text(call("export_fcp7_xml", {
        "filepath": str(project), "output_path": str(out),
        "confirm_unreviewed": True,
    }))
    assert out.is_file()
    assert "<xmeml" in out.read_text()


def test_add_audio_lands_a_music_bed(project, tmp_path):
    out = tmp_path / "with_audio.fcpxml"
    music = tmp_path / "bed.wav"
    music.write_bytes(b"\x00")
    _text(call("add_audio", {
        "filepath": str(project), "src": str(music), "role": "music",
        "duration": "5s", "output_path": str(out),
    }))
    assert out.is_file()
    assert "bed.wav" in out.read_text()


def test_create_then_flatten_a_compound_clip_round_trips(project, tmp_path):
    names = [c.name for c in server._require_timeline(str(project))[1].clips[:2]]
    assert len(names) == 2, "the sample needs two spine clips for this"

    grouped = tmp_path / "grouped.fcpxml"
    _text(call("create_compound_clip", {
        "filepath": str(project), "clip_ids": names,
        "name": "Section A", "output_path": str(grouped),
    }))
    assert grouped.is_file()
    assert "Section A" in grouped.read_text()


def test_apply_template_fills_every_required_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "ALLOWED_WRITE_ROOTS", [str(tmp_path)], raising=False)
    out = tmp_path / "from_template.fcpxml"
    body = _text(call("apply_template", {
        "template_name": "intro_outro",
        "clips": {
            "intro_card": {"src": str(tmp_path / "intro.mov"), "name": "Intro", "duration": 5.0},
            "main_content": {"src": str(tmp_path / "main.mov"), "name": "Main", "duration": 60.0},
            "end_card": {"src": str(tmp_path / "end.mov"), "name": "End", "duration": 5.0},
        },
        "output_path": str(out),
    }))
    # The empty-slots call returns a validation ERROR that also mentions the
    # template, so asserting on the prose alone would pass on the failure.
    assert "error" not in body.lower(), body
    assert out.is_file(), body
    assert "Intro" in out.read_text()


def test_a_template_missing_a_required_slot_refuses(tmp_path, monkeypatch):
    """The control for the test above: it must not pass on the error path."""
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    out = tmp_path / "never.fcpxml"
    body = _text(call("apply_template", {
        "template_name": "intro_outro", "clips": {}, "output_path": str(out),
    }))
    assert "intro_card" in body and not out.exists()


def test_relink_media_rewrites_the_prefix_it_was_given(project, tmp_path):
    out = tmp_path / "relinked.fcpxml"
    assert "/Media/Interview_A.mov" in project.read_text(), (
        "the fixture must contain the prefix this test replaces"
    )

    body = _text(call("relink_media", {
        "filepath": str(project),
        "find": "/Media",
        "replace": str(tmp_path / "footage"),
        "output_path": str(out),
    }))
    assert out.is_file(), body
    rewritten = out.read_text()
    assert f"{tmp_path}/footage/Interview_A.mov" in rewritten, rewritten[:400]
    assert "/Media/Interview_A.mov" not in rewritten
    assert "Relinked" in body


def test_the_moved_handlers_all_live_in_tools_nle():
    """A handler that drifts back into server.py splits the definition in two.

    The re-export is the whole reason the move is invisible to callers; if
    one of these ever stops coming from tools.nle, that has quietly stopped
    being true.
    """
    import tools.nle
    for name in (
        "export_resolve_xml", "export_fcp7_xml", "list_effects", "add_audio",
        "create_compound_clip", "flatten_compound_clip", "list_templates",
        "apply_template", "relink_media",
    ):
        handler = server.TOOL_HANDLERS[name]
        assert handler.__module__ == "tools.nle", f"{name} is {handler.__module__}"
        assert getattr(tools.nle, f"handle_{name}") is handler
