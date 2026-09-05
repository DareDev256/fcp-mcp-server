"""Tests for Live mode v1 (v0.9.0) — push_to_fcp / list_fcp_libraries.

osascript and pgrep are mocked throughout: unit tests must never launch
Final Cut Pro.  The import-options injection path is real (and
DTD-checked in test_dtd_validation.py's environment when available).
"""

import subprocess
import xml.etree.ElementTree as ET

import pytest

import server
from fcpxml import live
from fcpxml.live import inject_import_options, list_fcp_libraries, push_to_fcp

SIMPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<fcpxml version="1.13"><resources>'
    '<format id="r1" frameDuration="1/24s" width="1920" height="1080"/>'
    '</resources>'
    '<library><event name="Evt"><project name="Live Test">'
    '<sequence format="r1" duration="240/24s"><spine>'
    '<gap name="Gap" offset="0s" duration="240/24s"/>'
    '</spine></sequence></project></event></library></fcpxml>'
)


@pytest.fixture
def project_file(tmp_path):
    f = tmp_path / "live.fcpxml"
    f.write_text(SIMPLE_XML)
    return f


def _fake_proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["osascript"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ============================================================
# inject_import_options
# ============================================================

def test_inject_import_options_first_child(project_file, tmp_path):
    out = tmp_path / "live_import.fcpxml"
    inject_import_options(
        str(project_file), str(out),
        library_location="/Users/me/Movies/Test.fcplibrary",
        suppress_warnings=True,
        copy_assets=False,
    )
    root = ET.parse(out).getroot()
    first = list(root)[0]
    assert first.tag == "import-options"
    opts = {o.get("key"): o.get("value") for o in first.findall("option")}
    # location is normalized to .fcpbundle so FCP auto-creates instead of
    # raising the modal "Open Library" picker (live-verified on FCP 12.2)
    assert opts["library location"].startswith("file:///Users/me/Movies/")
    assert opts["library location"].endswith("Test.fcpbundle")
    assert opts["suppress warnings"] == "1"
    assert opts["copy assets"] == "0"


def test_inject_normalizes_to_fcpbundle(project_file, tmp_path):
    out = tmp_path / "n.fcpxml"
    inject_import_options(
        str(project_file), str(out), library_location="/tmp/Bare/MyLib"
    )
    loc = ET.parse(out).getroot().find(
        "import-options/option[@key='library location']"
    ).get("value")
    assert loc.endswith("MyLib.fcpbundle")


def test_inject_import_options_replaces_existing(project_file, tmp_path):
    out1 = tmp_path / "a.fcpxml"
    inject_import_options(str(project_file), str(out1), suppress_warnings=False)
    out2 = tmp_path / "b.fcpxml"
    inject_import_options(str(out1), str(out2), suppress_warnings=True)
    root = ET.parse(out2).getroot()
    assert len(root.findall("import-options")) == 1


def test_inject_percent_encodes_spaces(project_file, tmp_path):
    out = tmp_path / "c.fcpxml"
    inject_import_options(
        str(project_file), str(out),
        library_location="/Users/me/My Movies/Lib.fcplibrary",
    )
    root = ET.parse(out).getroot()
    loc = root.find("import-options/option[@key='library location']").get("value")
    assert "%20" in loc and " " not in loc


# ============================================================
# push_to_fcp (osascript mocked)
# ============================================================

def test_push_sends_options_copy(project_file, tmp_path, monkeypatch):
    calls = []

    def fake_run(script):
        calls.append(script)
        return _fake_proc()

    monkeypatch.setattr(live, "_run_osascript", fake_run)
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)

    copy_path = str(tmp_path / "live_import.fcpxml")
    result = push_to_fcp(
        str(project_file),
        library_location="/tmp/Lib.fcplibrary",
        import_copy_path=copy_path,
        # These tests assert DELIVERY of the Apple event, not import. The
        # osascript call is faked, so nothing can ever land in a library and
        # the v0.25.0 verification would (correctly) raise.
        verify=False,
    )
    assert result["sent"] == copy_path
    assert result["launched_fcp"] is False
    assert 'tell application "Final Cut Pro"' in calls[0]
    assert "live_import.fcpxml" in calls[0]


def test_push_reports_launch(project_file, tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_run_osascript", lambda s: _fake_proc())
    monkeypatch.setattr(live, "fcp_is_running", lambda: False)
    # v0.25.0: a push with no library_location now refuses rather than
    # sending a document with nowhere to land, so these delivery-path
    # tests must name a target. verify=False because osascript is faked:
    # no import can land, and the verification would correctly raise.
    result = push_to_fcp(
        str(project_file), import_copy_path=str(tmp_path / "x.fcpxml"),
        library_location="/tmp/Lib.fcpbundle", verify=False,
    )
    assert result["launched_fcp"] is True


def test_push_permission_error_hint(project_file, tmp_path, monkeypatch):
    monkeypatch.setattr(
        live, "_run_osascript",
        lambda s: _fake_proc(returncode=1, stderr="Not authorized (-1743)"),
    )
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    with pytest.raises(RuntimeError, match="Automation permission"):
        push_to_fcp(str(project_file), import_copy_path=str(tmp_path / "x.fcpxml"),
                    library_location="/tmp/Lib.fcpbundle", verify=False)


def test_push_escapes_applescript_quotes(tmp_path, monkeypatch):
    evil = tmp_path / 'a"b.fcpxml'
    evil.write_text(SIMPLE_XML)
    captured = {}
    monkeypatch.setattr(
        live, "_run_osascript",
        lambda s: captured.setdefault("script", s) and _fake_proc() or _fake_proc(),
    )
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    push_to_fcp(str(evil), library_location="/tmp/Lib.fcpbundle", verify=False)
    assert '\\"' in captured["script"]


# ============================================================
# list_fcp_libraries (osascript mocked)
# ============================================================

def test_list_refuses_to_launch(monkeypatch):
    monkeypatch.setattr(live, "fcp_is_running", lambda: False)
    with pytest.raises(RuntimeError, match="not running"):
        list_fcp_libraries()


def test_list_parses_records(monkeypatch):
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    # v0.25.0: field 1 is the library's POSIX path. push_to_fcp matches the
    # target library on that path, not on the display name — two libraries
    # can share a name, and only one of them is the import target.
    payload = (
        "LibA\x1f/m/LibA.fcpbundle/\x1fEvt1\x1fProj1\x1fProj2\x1f\x1e"
        "LibA\x1f/m/LibA.fcpbundle/\x1fEvt2\x1f\x1e"
        "LibB\x1f/m/LibB.fcpbundle/\x1f\x1e"
    )
    monkeypatch.setattr(live, "_run_osascript", lambda s: _fake_proc(stdout=payload))
    libs = list_fcp_libraries()
    by_name = {lib["name"]: lib for lib in libs}
    assert by_name["LibA"]["file"] == "/m/LibA.fcpbundle/"
    assert by_name["LibA"]["events"][0]["projects"] == ["Proj1", "Proj2"]
    assert by_name["LibA"]["events"][1]["name"] == "Evt2"
    assert by_name["LibB"]["events"] == []
    assert by_name["LibB"]["file"] == "/m/LibB.fcpbundle/"


# ============================================================
# MCP handlers
# ============================================================

async def test_handler_push(project_file, monkeypatch):
    monkeypatch.setattr(live, "_run_osascript", lambda s: _fake_proc())
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    result = await server.handle_push_to_fcp({
        "filepath": str(project_file),
        "library_location": "/tmp/Lib.fcpbundle",
        "verify": False,
    })
    text = result[0].text
    assert "Sent to Final Cut Pro" in text
    assert "NOT VERIFIED" in text  # the handler never claims an unseen import
    assert "_import" in text  # options copy, original untouched
    assert "Export XML" in text  # read-back asymmetry stated


async def test_handler_list_not_running(monkeypatch):
    monkeypatch.setattr(live, "fcp_is_running", lambda: False)
    result = await server.handle_list_fcp_libraries({})
    assert "not running" in result[0].text


def test_live_tools_registered():
    assert "push_to_fcp" in server.TOOL_HANDLERS
    assert "list_fcp_libraries" in server.TOOL_HANDLERS
