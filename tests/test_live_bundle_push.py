"""push_to_fcp carries its import target for bundles too (0.27.1).

Reported by John Palchak against 0.26.0 on Final Cut Pro 12.3: a ``.fcpxmld``
pushed with ``library_location`` raised Final Cut's "Which library do you want
to import Info.fcpxml into?" picker, and the call blocked until osascript's
120 s timeout. The option injection was skipped for bundles, and for any push
without an ``import_copy_path`` (autopush), so the document reached Final Cut
with no target. osascript and pgrep are mocked: nothing here launches Final Cut.
"""

import subprocess
from pathlib import Path

import pytest

import server
from fcpxml import live
from fcpxml.live import push_to_fcp
from fcpxml.safe_xml import safe_parse

INFO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<fcpxml version="1.13"><resources>'
    '<format id="r1" frameDuration="1/24s" width="1920" height="1080"/>'
    '</resources>'
    '<library><event name="Evt"><project name="Bundle Test">'
    '<sequence format="r1" duration="240/24s"><spine>'
    '<gap name="Gap" offset="0s" duration="240/24s"/>'
    '</spine></sequence></project></event></library></fcpxml>'
)


@pytest.fixture
def bundle(tmp_path):
    """A Final Cut-shaped export: Info.fcpxml plus a sidecar data file."""
    b = tmp_path / "Export.fcpxmld"
    b.mkdir()
    (b / "Info.fcpxml").write_text(INFO)
    (b / "Tracking.data").write_bytes(b"\x00sidecar\x01")
    return b


@pytest.fixture
def osascript(monkeypatch):
    calls = []

    def fake_run(script):
        calls.append(script)
        return subprocess.CompletedProcess(["osascript"], 0, "", "")

    monkeypatch.setattr(live, "_run_osascript", fake_run)
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    return calls


def _library_option(document: Path):
    root = safe_parse(str(document)).getroot()
    assert root[0].tag == "import-options", "import-options must be the first child"
    return {o.get("key"): o.get("value") for o in root[0]}


def test_the_reported_bundle_push_carries_the_library(bundle, tmp_path, osascript):
    library = tmp_path / "Edit.fcpbundle"
    copy = tmp_path / "Export_import.fcpxmld"
    original = (bundle / "Info.fcpxml").read_bytes()

    result = push_to_fcp(
        str(bundle), library_location=str(library),
        import_copy_path=str(copy), verify=False,
    )

    assert result["sent"] == str(copy)
    assert "Export_import.fcpxmld" in osascript[0]
    options = _library_option(copy / "Info.fcpxml")
    assert options["library location"] == library.resolve().as_uri()
    # The sidecar travels with the copy, and the original is untouched.
    assert (copy / "Tracking.data").read_bytes() == b"\x00sidecar\x01"
    assert (bundle / "Info.fcpxml").read_bytes() == original


def test_a_bundle_push_is_verified_against_its_projects(bundle, tmp_path, osascript):
    """The verifier read the bundle DIRECTORY as XML, failed, and so watched
    for no project at all: every bundle push was reported unverified."""
    result = push_to_fcp(
        str(bundle), library_location=str(tmp_path / "Edit.fcpbundle"),
        import_copy_path=str(tmp_path / "Export_import.fcpxmld"), verify=False,
    )
    assert result["expected_projects"] == ["Bundle Test"]


def test_a_push_without_a_copy_path_still_carries_the_library(tmp_path, osascript):
    """The autopush shape: no import_copy_path. A temp copy is sent."""
    flat = tmp_path / "edit.fcpxml"
    flat.write_text(INFO)
    library = tmp_path / "Edit.fcpbundle"
    result = push_to_fcp(str(flat), library_location=str(library), verify=False)
    sent = Path(result["sent"])
    assert sent != flat and sent.name == "edit.fcpxml"
    assert _library_option(sent)["library location"] == library.resolve().as_uri()
    assert flat.read_text() == INFO


def test_a_bundle_copy_path_must_be_a_bundle(bundle, tmp_path, osascript):
    with pytest.raises(ValueError, match="must end in .fcpxmld"):
        push_to_fcp(str(bundle), library_location=str(tmp_path / "L.fcpbundle"),
                    import_copy_path=str(tmp_path / "flat.fcpxml"), verify=False)
    assert osascript == []


def test_a_document_without_a_target_is_never_sent(bundle, tmp_path, osascript, monkeypatch):
    """Mutation check on the read-back: an injection that writes no library
    location must stop the push before the Apple event, not after 120 s."""
    real = live.inject_import_options

    def forgetful(src, dst, library_location=None, **kw):
        return real(src, dst, library_location=None, **kw)

    monkeypatch.setattr(live, "inject_import_options", forgetful)
    with pytest.raises(RuntimeError, match="carries no library location"):
        push_to_fcp(str(bundle), library_location=str(tmp_path / "L.fcpbundle"),
                    import_copy_path=str(tmp_path / "Export_import.fcpxmld"),
                    verify=False)
    assert osascript == []


def test_a_blocked_apple_event_fails_with_the_reason(bundle, tmp_path, monkeypatch):
    def hang(script):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=120)

    monkeypatch.setattr(live, "_run_osascript", hang)
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)
    with pytest.raises(RuntimeError, match="modal dialog is most likely waiting"):
        push_to_fcp(str(bundle), library_location=str(tmp_path / "L.fcpbundle"),
                    import_copy_path=str(tmp_path / "Export_import.fcpxmld"),
                    verify=False)


async def test_the_handler_sends_a_bundle_copy(bundle, tmp_path, osascript):
    text = (await server.handle_push_to_fcp({
        "filepath": str(bundle),
        "library_location": str(tmp_path / "Edit.fcpbundle"),
        "verify": False,
    }))[0].text
    copy = tmp_path / "Export_import.fcpxmld"
    assert str(copy) in text
    assert _library_option(copy / "Info.fcpxml")["library location"]
    assert (copy / "Tracking.data").is_file()
