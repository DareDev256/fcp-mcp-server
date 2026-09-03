"""The spine-editing handlers, driven end to end through server.call_tool.

`delete_clips`, `split_clip`, `insert_clip`, `reorder_clips` and
`add_transition` shipped without a test that names them. The writer methods
underneath were covered; the handlers were not — so argument parsing, clip
resolution, the ripple flag and the saved artifact were all unasserted, and
`delete_clips` is the most destructive tool on the surface.

Every case asserts the FILE, not the sentence the handler returns. A handler
that reports success and writes nothing reads identically to one that works,
and each of these tests was mutation-checked against exactly that.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

import server
from fcpxml.parser import parse_fcpxml

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


def call(name, args):
    return asyncio.run(server.call_tool(name, args))[0].text


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    out = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, out)
    return out


def clips(path):
    return [c.name for c in parse_fcpxml(str(path)).timelines[0].clips]


def test_delete_clips_removes_one_clip_per_id_not_every_match(project, tmp_path):
    """The sample repeats clip names, and the handler deletes the FIRST match.

    Worth pinning: an id here is a name, names are not unique in FCPXML, and
    the destructive tool on this surface resolving one name to three clips
    would be the kind of surprise you only find after it has run.
    """
    out = tmp_path / "deleted.fcpxml"
    before = clips(project)
    assert before.count("Broll_City") == 3, before

    body = call("delete_clips", {
        "filepath": str(project), "clip_ids": ["Broll_City"],
        "output_path": str(out),
    })
    assert "Deleted 1 clip" in body
    assert out.is_file(), body
    after = clips(out)
    assert len(after) == len(before) - 1
    assert after.count("Broll_City") == 2
    assert after == [c for i, c in enumerate(before) if i != before.index("Broll_City")]


def test_delete_clips_leaves_the_input_untouched(project, tmp_path):
    """The output path is the only thing that may change on disk."""
    original = project.read_bytes()
    call("delete_clips", {
        "filepath": str(project), "clip_ids": [clips(project)[0]],
        "output_path": str(tmp_path / "d.fcpxml"),
    })
    assert project.read_bytes() == original


def test_delete_clips_on_a_name_that_is_not_there_deletes_nothing(project, tmp_path):
    out = tmp_path / "nothing.fcpxml"
    body = call("delete_clips", {
        "filepath": str(project), "clip_ids": ["No Such Clip"],
        "output_path": str(out),
    })
    assert "Deleted 0 clip" in body, body
    if out.is_file():
        assert clips(out) == clips(project)


def test_split_clip_turns_one_clip_into_two(project, tmp_path):
    out = tmp_path / "split.fcpxml"
    first = clips(project)[0]
    body = call("split_clip", {
        "filepath": str(project), "clip_id": first,
        "split_points": ["2s"], "output_path": str(out),
    })
    assert out.is_file(), body
    assert len(clips(out)) == len(clips(project)) + 1


def test_reorder_clips_moves_a_clip_to_the_front(project, tmp_path):
    out = tmp_path / "reordered.fcpxml"
    before = clips(project)
    body = call("reorder_clips", {
        "filepath": str(project), "clip_ids": [before[-1]],
        "target_position": "start", "output_path": str(out),
    })
    assert out.is_file(), body
    assert clips(out)[0] == before[-1]


def test_add_transition_writes_a_transition_element(project, tmp_path):
    out = tmp_path / "dissolved.fcpxml"
    body = call("add_transition", {
        "filepath": str(project), "clip_id": clips(project)[0],
        "transition_type": "cross-dissolve", "duration": "1s",
        "output_path": str(out),
    })
    assert out.is_file(), body
    assert "<transition" in out.read_text()
    assert len(parse_fcpxml(str(out)).timelines[0].transitions) == 1


def test_insert_clip_adds_a_clip_at_the_end(project, tmp_path):
    out = tmp_path / "inserted.fcpxml"
    before = clips(project)
    body = call("insert_clip", {
        "filepath": str(project), "position": "end",
        "asset_name": before[0], "duration": "3s",
        "output_path": str(out),
    })
    assert out.is_file(), body
    assert len(clips(out)) == len(before) + 1


def test_delete_clips_reports_the_ids_that_matched_nothing(project, tmp_path):
    """A partial batch must not read like a complete one.

    Before this, the handler reported `len(clip_ids)` — the size of the
    REQUEST — so deleting one real clip alongside one typo answered
    "Deleted 2 clip(s)", and a request that matched nothing at all answered
    "Deleted 2 clip(s)" too. On the most destructive tool on the surface,
    success and total failure read identically.
    """
    out = tmp_path / "partial.fcpxml"
    before = clips(project)
    body = call("delete_clips", {
        "filepath": str(project),
        "clip_ids": ["Broll_City", "No Such Clip"],
        "output_path": str(out),
    })
    assert "Deleted 1 clip" in body, body
    assert "No clip matched: 'No Such Clip'" in body
    assert len(clips(out)) == len(before) - 1


def test_the_delete_report_counts_deletions_not_requests(project, tmp_path):
    """Mutation guard: two ids that match nothing must not read as two deletes."""
    out = tmp_path / "none.fcpxml"
    body = call("delete_clips", {
        "filepath": str(project), "clip_ids": ["zzz", "qqq"],
        "output_path": str(out),
    })
    assert "Deleted 0 clip" in body, body
    assert clips(out) == clips(project)
