"""The read-back loop.

Apple ships a fully scriptable import (odoc + <import-options>) and no
programmatic export, unchanged across FCP 11.0 to 12.2. This is how one Cmd-E
becomes something the server notices.
"""

import threading
import time

import pytest

from fcpxml.watchfolder import Watcher, default_watch_dir


def test_baseline_ignores_files_that_already_existed(tmp_path):
    (tmp_path / "old.fcpxml").write_text("<fcpxml/>")
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    assert watcher.changed() == []


def test_a_new_export_is_detected(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    (tmp_path / "new.fcpxml").write_text("<fcpxml/>")
    changed = watcher.changed()
    assert len(changed) == 1
    assert changed[0].endswith("new.fcpxml")


def test_a_rewritten_export_is_detected(tmp_path):
    """Exporting over the same filename is the normal iteration loop."""
    target = tmp_path / "again.fcpxml"
    target.write_text("<fcpxml/>")
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    target.write_text("<fcpxml><event/></fcpxml>")
    assert [p for p in watcher.changed() if p.endswith("again.fcpxml")]


def test_a_rewrite_of_identical_length_within_the_same_second_is_still_caught(tmp_path):
    """mtime resolution is the trap here.

    Two writes of the same byte count inside one filesystem timestamp tick can
    produce an identical (mtime, size) pair. Content is what changed, so the
    snapshot has to notice content.
    """
    target = tmp_path / "same.fcpxml"
    target.write_text("<fcpxml>AAAA</fcpxml>")
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    target.write_text("<fcpxml>BBBB</fcpxml>")
    assert [p for p in watcher.changed() if p.endswith("same.fcpxml")]


def test_unrelated_files_are_ignored(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "render.mov").write_bytes(b"\x00")
    assert watcher.changed() == []


def test_fcpxmld_bundles_are_watched(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    (tmp_path / "bundle.fcpxmld").mkdir()
    assert [p for p in watcher.changed() if p.endswith("bundle.fcpxmld")]


def test_a_deleted_export_is_not_reported_as_a_change(tmp_path):
    target = tmp_path / "gone.fcpxml"
    target.write_text("<fcpxml/>")
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    target.unlink()
    assert watcher.changed() == []


def test_changed_before_baseline_baselines_instead_of_reporting_everything(tmp_path):
    (tmp_path / "pre.fcpxml").write_text("<fcpxml/>")
    watcher = Watcher(str(tmp_path))
    assert watcher.changed() == []


def test_pull_returns_none_on_timeout(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    assert watcher.pull(timeout=0.3, interval=0.05) is None


def test_pull_returns_the_export_when_it_lands(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()

    def write_later():
        time.sleep(0.15)
        (tmp_path / "landed.fcpxml").write_text("<fcpxml/>")

    threading.Thread(target=write_later, daemon=True).start()
    found = watcher.pull(timeout=5.0, interval=0.05)
    assert found is not None and found.endswith("landed.fcpxml")


def test_pull_rebaselines_so_the_same_export_is_not_returned_twice(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    (tmp_path / "once.fcpxml").write_text("<fcpxml/>")
    assert watcher.pull(timeout=1.0, interval=0.05) is not None
    assert watcher.pull(timeout=0.2, interval=0.05) is None


def test_missing_directory_raises_a_named_error(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        Watcher(str(tmp_path / "nope")).baseline()


def test_an_absurd_timeout_is_rejected(tmp_path):
    watcher = Watcher(str(tmp_path))
    watcher.baseline()
    with pytest.raises(ValueError, match="timeout"):
        watcher.pull(timeout=99999)


def test_default_watch_dir_reads_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("FCP_WATCH_DIR", raising=False)
    assert default_watch_dir() is None
    monkeypatch.setenv("FCP_WATCH_DIR", str(tmp_path))
    assert default_watch_dir() == str(tmp_path)
    monkeypatch.setenv("FCP_WATCH_DIR", "   ")
    assert default_watch_dir() is None
