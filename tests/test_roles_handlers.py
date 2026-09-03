"""The roles handlers, including the gated one, driven end to end.

`list_roles`, `assign_role`, `filter_by_role` and `export_role_stems` had no
test that named them. `export_role_stems` is in GATED_ACTIONS — the review
gate's own list — so the most consequential path through the gate had never
run in the suite. The gate was tested generically; this handler was not.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

import server

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


def call(name, args):
    return asyncio.run(server.call_tool(name, args))[0].text


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    out = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, out)
    return out


def test_list_roles_says_so_when_nothing_is_assigned(project):
    body = call("list_roles", {"filepath": str(project)})
    assert "No audio roles assigned" in body


def test_assign_role_then_list_roles_sees_it(project, tmp_path):
    out = tmp_path / "roled.fcpxml"
    assign = call("assign_role", {
        "filepath": str(project), "clip_id": "Broll_City",
        "audio_role": "music", "output_path": str(out),
    })
    assert "Broll_City" in assign
    assert out.is_file()
    assert 'audioRole="music"' in out.read_text()

    listed = call("list_roles", {"filepath": str(out)})
    assert "music" in listed
    assert "No audio roles assigned" not in listed


def test_filter_by_role_finds_only_the_clips_that_carry_it(project, tmp_path):
    out = tmp_path / "roled.fcpxml"
    call("assign_role", {
        "filepath": str(project), "clip_id": "Broll_City",
        "audio_role": "music", "output_path": str(out),
    })
    hit = call("filter_by_role", {"filepath": str(out), "role": "music"})
    assert "Broll_City" in hit

    miss = call("filter_by_role", {"filepath": str(out), "role": "dialogue"})
    assert "No clips found" in miss


def test_export_role_stems_groups_clips_under_their_role(project, tmp_path):
    out = tmp_path / "roled.fcpxml"
    call("assign_role", {
        "filepath": str(project), "clip_id": "Broll_City",
        "audio_role": "music", "output_path": str(out),
    })
    plan = call("export_role_stems", {
        "filepath": str(out), "confirm_unreviewed": True,
    })
    # The plan title-cases its headings: "## Music (1 clip, 1.00s)".
    assert "## Music (1 clip," in plan, plan
    assert "Broll_City" in plan
    assert "Unassigned (8 clips" in plan


def test_export_role_stems_is_gated_without_a_reviewed_preview(project):
    """It is in GATED_ACTIONS; the gate has to actually stop it.

    A gate that never fires on the handler it names is indistinguishable
    from no gate, and this is the path that hands files to another tool.
    """
    body = call("export_role_stems", {"filepath": str(project)})
    assert "preview" in body.lower()
    assert "confirm_unreviewed" in body


def test_the_override_is_stamped_on_the_output(project):
    """Shipping unreviewed is allowed, but it must leave a mark."""
    body = call("export_role_stems", {
        "filepath": str(project), "confirm_unreviewed": True,
    })
    assert "Stem Plan" in body
    assert server._UNREVIEWED_NOTE.strip().splitlines()[0] in body
