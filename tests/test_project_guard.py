"""The project guard: expected_sha256 refuses a file that changed under the model.

Every action that takes a filepath accepts the argument; every write
answers with the hash of what it wrote; inspect and history report the
current one. The guard is mutation-checked: with it removed, the same
mismatched write goes through, so the tests here can see it fail.
"""

import asyncio
import re
import shutil
from pathlib import Path

import pytest

import server
from fcpxml import journal

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.fcpxml"
WRONG = "0" * 64


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    p = folder / "sample.fcpxml"
    shutil.copy(SAMPLE, p)
    return p


def _mark(project, **extra):
    args = {"filepath": str(project), "timecode": "00:00:01:00", "name": "g", **extra}
    return asyncio.run(server.call_tool("mark", {"action": "add_marker", "args": args}))[0].text


def test_a_matching_hash_lets_the_write_through(project):
    text = _mark(project, expected_sha256=journal.file_hash(str(project)))
    assert "Saved to" in text


def test_the_hash_is_matched_case_insensitively_and_trimmed(project):
    text = _mark(project, expected_sha256="  " + journal.file_hash(str(project)).upper() + " ")
    assert "Saved to" in text


def test_a_mismatched_hash_refuses_writes_nothing_and_journals_nothing(project):
    text = _mark(project, expected_sha256=WRONG)
    assert text.startswith("Refused:") and "changed since you last read it" in text
    assert f"expected sha256: {WRONG}" in text
    assert f"current sha256:  {journal.file_hash(str(project))}" in text
    assert [p.name for p in project.parent.iterdir()] == ["sample.fcpxml"]
    assert journal.records(str(project)) == []


def test_the_guard_refuses_before_the_review_gate_speaks(project):
    text = asyncio.run(server.call_tool("deliver", {"action": "export_edl", "args": {
        "filepath": str(project), "expected_sha256": WRONG}}))[0].text
    assert "changed since you last read it" in text and "preview_render" not in text


def test_a_missing_file_cannot_match(project):
    text = _mark(project.with_name("gone.fcpxml"), expected_sha256=WRONG)
    assert "does not exist" in text


def test_a_non_string_hash_is_refused(project):
    text = _mark(project, expected_sha256=12345)
    assert "must be a hex string" in text


def test_reads_honour_the_guard_too(project):
    text = asyncio.run(server.call_tool("inspect", {"action": "list_clips", "args": {
        "filepath": str(project), "expected_sha256": WRONG}}))[0].text
    assert "changed since you last read it" in text


def test_flat_tools_honour_the_guard(project):
    text = asyncio.run(server.call_tool("add_marker", {
        "filepath": str(project), "timecode": "00:00:01:00", "name": "g", "expected_sha256": WRONG}))[0].text
    assert "changed since you last read it" in text


def test_without_the_argument_nothing_changes(project):
    assert "Saved to" in _mark(project)


def test_every_write_answers_with_the_hash_of_what_it_wrote(project):
    text = _mark(project)
    out = re.search(r"Saved to: `?([^`\n]+)`?", text).group(1).strip()
    assert f"sha256 ({Path(out).name}): {journal.file_hash(out)}" in text
    # And that hash is accepted as expected_sha256 on the next edit of the output.
    next_text = _mark(Path(out), expected_sha256=journal.file_hash(out))
    assert "Saved to" in next_text


def test_a_read_answers_with_no_footer(project):
    text = asyncio.run(server.call_tool("inspect", {"action": "list_clips", "args": {"filepath": str(project)}}))[0].text
    assert "sha256 (" not in text


def test_analyze_timeline_reports_the_current_hash(project):
    text = asyncio.run(server.call_tool("inspect", {"action": "analyze_timeline", "args": {"filepath": str(project)}}))[0].text
    assert f"**sha256**: {journal.file_hash(str(project))}" in text and "expected_sha256" in text


def test_history_reports_the_full_output_hash(project):
    _mark(project)
    out = project.with_name("sample_modified.fcpxml")
    text = asyncio.run(server.call_tool("organize", {"action": "history", "args": {"filepath": str(project)}}))[0].text
    assert journal.file_hash(str(out)) in text
    assert "Output sha256" in text


def test_the_guard_is_load_bearing(project, monkeypatch):
    """Mutation check: remove the guard and the mismatched write goes through."""
    monkeypatch.setattr(server, "_project_guard", lambda arguments: None)
    text = _mark(project, expected_sha256=WRONG)
    assert "Saved to" in text


def test_a_json_reply_carries_the_hashes_inside_the_object():
    """Keyframe and retime actions answer with one JSON object. A text footer
    after it made json.loads raise "Extra data" in every caller, so the
    hashes go inside the object instead, and the reply stays one document."""
    import json

    reply = json.dumps({"output_path": "/x/out.fcpxml", "ok": True, "note": "café"}, ensure_ascii=False)
    merged = server._with_hashes(reply, {"out.fcpxml": "a" * 64})
    payload = json.loads(merged)
    assert payload["sha256"] == {"out.fcpxml": "a" * 64}
    assert payload["note"] == "café" and "\\u00e9" not in merged
    # Prose keeps the footer line the model reads.
    prose = server._with_hashes("Saved to: out.fcpxml", {"out.fcpxml": "b" * 64})
    assert prose.endswith(f"sha256 (out.fcpxml): {'b' * 64}")
    # No hashes, no change.
    assert server._with_hashes(reply, {}) == reply
