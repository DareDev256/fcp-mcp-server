"""Import targeting and the honesty check on push_to_fcp.

Until v0.25.0 a push with no `library_location` wrote no `library location`
import option at all: FCP received a document with no target, imported
nothing, and push_to_fcp returned success. Live-reproduced on FCP 12.3
(2026-09-05) — the Apple event was delivered, three libraries stayed
untouched, and the tool said "Sent to Final Cut Pro".

Nothing here talks to FCP. list_fcp_libraries and fcp_is_running are
patched throughout; a test that shells out to osascript is a defect.
"""

import pytest

from fcpxml import live


def _lib(name, path, projects=()):
    return {
        "name": name,
        "file": path,
        "events": [{"name": "E", "projects": list(projects)}],
    }


# ── resolve_library_target ──────────────────────────────────────────

def test_explicit_location_always_wins(monkeypatch):
    """An explicit target is never second-guessed, even with FCP closed."""
    monkeypatch.setattr(live, "fcp_is_running", lambda: False)
    assert live.resolve_library_target("/tmp/X.fcpbundle") == "/tmp/X.fcpbundle"


def test_single_open_library_is_inferred(monkeypatch):
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    monkeypatch.setattr(live, "list_fcp_libraries",
                        lambda: [_lib("Only", "/Users/x/Movies/Only.fcpbundle")])
    assert live.resolve_library_target() == "/Users/x/Movies/Only.fcpbundle"


def test_several_open_libraries_refuse_and_name_them(monkeypatch):
    """Guessing among open libraries is the bug, not the fix.

    AppleScript orders `libraries` by internal application order, not by
    which is frontmost, so a silent pick lands in the wrong library some
    of the time — and for an editor that means clips appearing in a
    client project they were not working on.
    """
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    monkeypatch.setattr(live, "list_fcp_libraries", lambda: [
        _lib("Wedding", "/Users/x/Movies/Wedding.fcpbundle"),
        _lib("2026", "/Users/x/Movies/2026.fcpbundle"),
    ])
    with pytest.raises(RuntimeError) as exc:
        live.resolve_library_target()
    msg = str(exc.value)
    assert "Wedding" in msg and "2026" in msg
    assert "library_location" in msg


def test_refuses_when_fcp_is_not_running(monkeypatch):
    monkeypatch.setattr(live, "fcp_is_running", lambda: False)
    with pytest.raises(RuntimeError, match="not running"):
        live.resolve_library_target()


def test_never_invents_a_movies_folder_default(monkeypatch):
    """No ~/Movies/<name>.fcpbundle convenience default, ever."""
    monkeypatch.setattr(live, "fcp_is_running", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        live.resolve_library_target()
    assert "Movies/" not in str(exc.value).replace("Your.fcpbundle", "")


# ── _library_contains ───────────────────────────────────────────────

def test_library_matched_by_path_not_name():
    """Two libraries can share a display name; the path is the identity."""
    libs = [_lib("Reveal", "/a/Reveal.fcpbundle", ["P"]),
            _lib("Reveal", "/b/Reveal.fcpbundle", [])]
    assert live._library_contains(libs, "/a/Reveal.fcpbundle", ["P"])
    assert not live._library_contains(libs, "/b/Reveal.fcpbundle", ["P"])


def test_trailing_slash_does_not_break_the_match():
    libs = [_lib("R", "/a/R.fcpbundle/", ["P"])]
    assert live._library_contains(libs, "/a/R.fcpbundle", ["P"])


def test_all_projects_must_be_present_not_just_one():
    libs = [_lib("R", "/a/R.fcpbundle", ["P1"])]
    assert not live._library_contains(libs, "/a/R.fcpbundle", ["P1", "P2"])


# ── the honesty check ───────────────────────────────────────────────

class _Proc:
    returncode = 0
    stdout = "ok"
    stderr = ""


@pytest.fixture
def sent(tmp_path):
    """A one-project document to push."""
    doc = tmp_path / "p.fcpxml"
    doc.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<fcpxml version="1.10"><library><event name="E">'
        '<project name="Reveal Cut"/></event></library></fcpxml>',
        encoding="utf-8",
    )
    return doc


def _patch_push(monkeypatch, libraries_after, libraries_before=None):
    calls = {"n": 0}

    def _list(allow_launch=False):
        calls["n"] += 1
        if calls["n"] == 1 and libraries_before is not None:
            return libraries_before
        return libraries_after

    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    monkeypatch.setattr(live, "list_fcp_libraries", _list)
    monkeypatch.setattr(live, "_run_osascript", lambda script: _Proc())
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    return calls


def test_push_raises_when_the_import_is_never_observed(monkeypatch, sent, tmp_path):
    """osascript exit 0 must not be reportable as a successful import.

    This is the exact defect: the Apple event lands, FCP does nothing, and
    the old code returned {"sent": ...} with no way to tell.
    """
    _patch_push(monkeypatch, [_lib("R", "/a/R.fcpbundle", [])])
    with pytest.raises(live.ImportNotObservedError) as exc:
        live.push_to_fcp(str(sent), library_location="/a/R.fcpbundle",
                         import_copy_path=str(tmp_path / "c.fcpxml"),
                         verify_timeout=0)
    assert "Reveal Cut" in str(exc.value)
    assert "never observed" in str(exc.value)


def test_push_verifies_when_the_project_appears(monkeypatch, sent, tmp_path):
    _patch_push(
        monkeypatch,
        libraries_before=[_lib("R", "/a/R.fcpbundle", [])],
        libraries_after=[_lib("R", "/a/R.fcpbundle", ["Reveal Cut"])],
    )
    r = live.push_to_fcp(str(sent), library_location="/a/R.fcpbundle",
                         import_copy_path=str(tmp_path / "c.fcpxml"),
                         verify_timeout=5)
    assert r["verified"] is True
    assert r["library_location"] == "/a/R.fcpbundle"


def test_a_preexisting_project_name_cannot_verify_anything(monkeypatch, sent, tmp_path):
    """If the name was already there, seeing it afterwards proves nothing.

    Reporting True here would be the same class of lie in a new costume.
    """
    _patch_push(monkeypatch, [_lib("R", "/a/R.fcpbundle", ["Reveal Cut"])])
    r = live.push_to_fcp(str(sent), library_location="/a/R.fcpbundle",
                         import_copy_path=str(tmp_path / "c.fcpxml"))
    assert r["verified"] is None
    assert "already existed" in r["verification_note"]


def test_verify_false_skips_the_check_and_says_so(monkeypatch, sent, tmp_path):
    _patch_push(monkeypatch, [_lib("R", "/a/R.fcpbundle", [])])
    r = live.push_to_fcp(str(sent), library_location="/a/R.fcpbundle",
                         import_copy_path=str(tmp_path / "c.fcpxml"),
                         verify=False)
    assert r["verified"] is None


def test_the_import_copy_carries_the_resolved_target(monkeypatch, sent, tmp_path):
    """The repair path: an old file with no target gets one on the way out."""
    _patch_push(
        monkeypatch,
        libraries_before=[_lib("R", "/a/R.fcpbundle", [])],
        libraries_after=[_lib("R", "/a/R.fcpbundle", ["Reveal Cut"])],
    )
    copy = tmp_path / "c.fcpxml"
    live.push_to_fcp(str(sent), library_location="/a/R.fcpbundle",
                     import_copy_path=str(copy), verify_timeout=5)
    xml = copy.read_text(encoding="utf-8")
    assert 'key="library location"' in xml
    assert "R.fcpbundle" in xml
