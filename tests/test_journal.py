"""The operation journal: append-only, hash-checked, never holds content."""

import json
import stat
from pathlib import Path

import pytest

from fcpxml import journal


def _write(p: Path, text: str) -> Path:
    p.write_text(text)
    return p


def _journaled_write(src: Path, out_name: str, body: str) -> Path:
    tok = journal.begin("edit", "trim_clip", {"filepath": str(src)}, str(src))
    out = _write(src.parent / out_name, body)
    journal.note_output(str(out))
    journal.finish(tok)
    return out


def test_dir_created_mode_700(tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_JOURNAL", str(tmp_path / "j"))
    d = journal.journal_dir()
    assert d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700


def test_off_disables(monkeypatch):
    monkeypatch.setenv("FCP_MCP_JOURNAL", "off")
    assert journal.journal_dir() is None and not journal.enabled()


def test_project_key_is_the_folder(tmp_path):
    a = _write(tmp_path / "a.fcpxml", "<x/>")
    b = _write(tmp_path / "b.fcpxml", "<y/>")
    assert journal.project_key(str(a)) == journal.project_key(str(b))
    assert journal.project_key(str(a)) != journal.project_key(str(tmp_path.parent / "z.fcpxml"))


def test_file_hash_bundle_reads_info(tmp_path):
    b = tmp_path / "p.fcpxmld"
    b.mkdir()
    (b / "Info.fcpxml").write_text("<x/>")
    assert journal.file_hash(str(b)) == journal.file_hash(str(_write(tmp_path / "flat.fcpxml", "<x/>")))
    assert journal.file_hash(str(tmp_path / "missing.fcpxml")) is None


def test_record_and_records(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    out = _write(tmp_path / "in_modified.fcpxml", "<b/>")
    journal.record({
        "tool": "edit", "action": "trim_clip", "args": {"filepath": str(src)},
        "input": {"path": str(src), "sha256": journal.file_hash(str(src))},
        "output": {"path": str(out), "sha256": journal.file_hash(str(out))},
    })
    rows = journal.records(str(src))
    assert len(rows) == 1 and rows[0]["action"] == "trim_clip" and "ts" in rows[0]
    ledger = journal.journal_dir() / f"{journal.project_key(str(src))}.jsonl"
    assert json.loads(ledger.read_text().splitlines()[0])["output"]["path"] == str(out)


def test_ledger_captures_only_outputs_that_exist(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    tok = journal.begin("edit", "trim_clip", {"filepath": str(src)}, str(src))
    journal.note_output(str(tmp_path / "never_written.fcpxml"))
    out = _write(tmp_path / "in_modified.fcpxml", "<b/>")
    journal.note_output(str(out))
    journal.finish(tok)
    rows = journal.records(str(src))
    assert [r["output"]["path"] for r in rows] == [str(out)]
    assert rows[0]["input"]["sha256"] == journal.file_hash(str(src))
    assert rows[0]["output"]["sha256"] == journal.file_hash(str(out))


def test_note_output_without_ledger_is_noop(tmp_path):
    journal.note_output(str(tmp_path / "x.fcpxml"))


def test_reviewed_matches_current_hash(tmp_path):
    src = _write(tmp_path / "cut.fcpxml", "<a/>")
    assert journal.reviewed(str(src)) is None
    tok = journal.begin("preview", "preview_render", {"filepath": str(src)}, str(src))
    journal.note_output(str(_write(tmp_path / "cut_proxy.mp4", "mp4")))
    journal.finish(tok)
    assert journal.reviewed(str(src))["action"] == "preview_render"
    src.write_text("<changed/>")
    assert journal.reviewed(str(src)) is None  # a render of the OLD state does not count


def test_undo_moves_output_aside_and_records(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    out = _journaled_write(src, "in_modified.fcpxml", "<b/>")
    undone = journal.undo(str(src), 1)
    assert not out.exists() and src.read_text() == "<a/>"
    moved = Path(undone[0]["moved_to"])
    assert moved.is_file() and moved.read_text() == "<b/>"
    assert journal.records(str(src))[-1]["action"] == "undo"
    assert journal.undo(str(src), 1) == []


def test_undo_n_walks_back_in_order(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    first = _journaled_write(src, "in_one.fcpxml", "<1/>")
    second = _journaled_write(src, "in_two.fcpxml", "<2/>")
    undone = journal.undo(str(src), 2)
    assert [Path(u["output"]["path"]).name for u in undone] == ["in_two.fcpxml", "in_one.fcpxml"]
    assert not first.exists() and not second.exists()


def test_undo_refuses_when_output_changed(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    out = _journaled_write(src, "in_modified.fcpxml", "<b/>")
    out.write_text("<edited by hand/>")
    with pytest.raises(ValueError, match="in_modified.fcpxml"):
        journal.undo(str(src), 1)
    assert out.exists()


def test_records_hold_no_media_content(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    _journaled_write(src, "o.fcpxml", "SECRET-BODY")
    ledger = journal.journal_dir() / f"{journal.project_key(str(src))}.jsonl"
    assert "SECRET-BODY" not in ledger.read_text()


def test_torn_line_is_skipped(tmp_path):
    src = _write(tmp_path / "in.fcpxml", "<a/>")
    _journaled_write(src, "o.fcpxml", "<o/>")
    ledger = journal.journal_dir() / f"{journal.project_key(str(src))}.jsonl"
    with open(ledger, "a") as fh:
        fh.write('{"tool": "edit", "act')
    assert len(journal.records(str(src))) == 1
