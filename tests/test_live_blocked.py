"""A Final Cut Pro that does not answer Apple Events fails in seconds with the
reason (2026-11-16: a pending macOS Automation consent prompt held every call
for the full 120 s, and the tool said nothing)."""
import subprocess

import pytest

from fcpxml import live


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(live, "_fcp_answered", False)
    monkeypatch.setattr(live, "_blocked_until", 0.0)
    monkeypatch.setattr(live, "fcp_is_running", lambda: True)


def _hang(*args, **kwargs):
    raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))


def test_a_pending_consent_prompt_is_named(monkeypatch):
    monkeypatch.setattr(live.subprocess, "run", _hang)
    monkeypatch.setattr(live, "pending_consent_prompt",
                        lambda app="Final Cut Pro": '"tmux" wants access to control "Final Cut Pro".')
    with pytest.raises(live.FCPAutomationBlockedError) as err:
        live.list_fcp_libraries()
    assert "permission click" in str(err.value) and "tmux" in str(err.value) and "Allow" in str(err.value)


def test_no_prompt_still_says_what_to_check(monkeypatch):
    monkeypatch.setattr(live.subprocess, "run", _hang)
    monkeypatch.setattr(live, "pending_consent_prompt", lambda app="Final Cut Pro": None)
    with pytest.raises(live.FCPAutomationBlockedError) as err:
        live.list_fcp_libraries()
    assert "did not answer" in str(err.value)


def test_the_probe_runs_before_the_long_call(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "pending_consent_prompt", lambda app="Final Cut Pro": None)
    with pytest.raises(live.FCPAutomationBlockedError):
        live.list_fcp_libraries()
    assert calls[0] == live._PROBE_TIMEOUT_SECONDS < live._OSASCRIPT_TIMEOUT_SECONDS


def test_the_prompt_reader_matches_only_the_app_asked_about(monkeypatch):
    class P:
        stdout = '"tmux" wants access to control "Music".\n'
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: P())
    assert live.pending_consent_prompt("Final Cut Pro") is None
    P.stdout = 'x\n"tmux" wants access to control "Final Cut Pro". Allowing\n'
    assert "Final Cut Pro" in live.pending_consent_prompt("Final Cut Pro")


@pytest.mark.parametrize("headline", [
    "«tmux» quiere acceder para controlar «Final Cut Pro».",          # Spanish
    "„tmux“ möchte Zugriff, um „Final Cut Pro“ zu steuern.",           # German
    "« tmux » souhaite accéder à « Final Cut Pro » pour le contrôler.",  # French
    "“tmux”想要控制“Final Cut Pro”。",                                   # Chinese
])
def test_the_prompt_reader_does_not_depend_on_the_system_language(monkeypatch, headline):
    """macOS writes the prompt in the system language; only the app's name is
    constant. The sentences above are illustrative, not copied from macOS:
    the reader must not care what they say. On a non-English Mac the reader used to find nothing, and the
    reply fell back to "check for a permission dialog" without naming it."""
    class P:
        stdout = f"{headline}\nmore words about Final Cut Pro\n"
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: P())
    assert live.pending_consent_prompt("Final Cut Pro") == headline
    P.stdout = headline.replace("Final Cut Pro", "Music") + "\n"
    assert live.pending_consent_prompt("Final Cut Pro") is None


@pytest.mark.asyncio
async def test_the_mcp_reply_carries_the_fix_not_just_the_class_name(monkeypatch):
    import server
    monkeypatch.setattr(live, "list_fcp_libraries",
                        lambda **k: (_ for _ in ()).throw(live.FCPAutomationBlockedError("click Allow")))
    out = await server.call_tool("deliver", {"action": "list_fcp_libraries", "args": {}})
    assert "click Allow" in out[0].text


@pytest.mark.asyncio
async def test_push_reply_carries_the_fix_too(monkeypatch, tmp_path):
    """push_to_fcp has no handler-level catch: the dispatcher must not reduce
    the message to the class name."""
    import server
    doc = tmp_path / "cut.fcpxml"
    doc.write_text('<?xml version="1.0"?><!DOCTYPE fcpxml><fcpxml version="1.13"><resources/>'
                   '<library><event name="e"/></library></fcpxml>')
    monkeypatch.setattr(live, "push_to_fcp",
                        lambda *a, **k: (_ for _ in ()).throw(live.FCPAutomationBlockedError("click Allow")))
    out = await server.call_tool("deliver", {"action": "push_to_fcp",
                                             "args": {"filepath": str(doc), "confirm_unreviewed": True}})
    assert "click Allow" in out[0].text


def test_a_block_is_remembered_so_the_next_call_does_not_wait_again(monkeypatch):
    probes = []

    def run(cmd, **kwargs):
        probes.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "pending_consent_prompt", lambda app="Final Cut Pro": None)
    for _ in range(3):
        with pytest.raises(live.FCPAutomationBlockedError):
            live.list_fcp_libraries()
    assert len(probes) == 1


def test_push_falls_back_to_open_and_verifies_on_disk(monkeypatch, tmp_path):
    """Apple Events blocked: the document goes through Launch Services and the
    import is proven by the project folder Final Cut writes in the bundle."""
    bundle = tmp_path / "Lib.fcpbundle"
    bundle.mkdir()
    doc = tmp_path / "cut.fcpxml"
    doc.write_text('<?xml version="1.0"?><!DOCTYPE fcpxml><fcpxml version="1.13"><resources/>'
                   '<library><event name="E"><project name="Cut 01"><sequence/></project></event></library></fcpxml>')
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "open":
            (bundle / "E" / "Cut 01").mkdir(parents=True)
            (bundle / "E" / "Cut 01" / "CurrentVersion.fcpevent").write_text("x")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, "1", "")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "pending_consent_prompt", lambda app="Final Cut Pro": "x wants access to control Final Cut Pro")
    r = live.push_to_fcp(str(doc), library_location=str(bundle), verify_timeout=3)
    assert r["delivered_via"] == "open" and r["verified"] is True and r["verified_by"] == "library bundle on disk"
    assert any(c[0] == "open" for c in calls)


def test_open_fallback_refuses_to_claim_an_import_that_never_lands(monkeypatch, tmp_path):
    bundle = tmp_path / "Lib.fcpbundle"
    bundle.mkdir()
    doc = tmp_path / "cut.fcpxml"
    doc.write_text('<?xml version="1.0"?><!DOCTYPE fcpxml><fcpxml version="1.13"><resources/>'
                   '<library><event name="E"><project name="Cut 01"><sequence/></project></event></library></fcpxml>')

    def run(cmd, **kwargs):
        if cmd[0] in ("open", "pgrep"):
            return subprocess.CompletedProcess(cmd, 0, "1", "")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "pending_consent_prompt", lambda app="Final Cut Pro": None)
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    with pytest.raises(live.ImportNotObservedError):
        live.push_to_fcp(str(doc), library_location=str(bundle), verify_timeout=0)


def test_a_denied_grant_also_falls_back_to_open(monkeypatch, tmp_path):
    """"Don't Allow" answers at once with -1743 instead of hanging; that is a
    refusal of Apple Events, not of the import, so open delivers it."""
    bundle = tmp_path / "Lib.fcpbundle"
    bundle.mkdir()
    doc = tmp_path / "cut.fcpxml"
    doc.write_text('<?xml version="1.0"?><!DOCTYPE fcpxml><fcpxml version="1.13"><resources/>'
                   '<library><event name="E"><project name="Cut 01"><sequence/></project></event></library></fcpxml>')

    def run(cmd, **kwargs):
        if cmd[0] == "open":
            (bundle / "E" / "Cut 01").mkdir(parents=True)
            (bundle / "E" / "Cut 01" / "CurrentVersion.fcpevent").write_text("x")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, "1", "")
        return subprocess.CompletedProcess(cmd, 1, "", "execution error: Not authorized to send Apple events to Final Cut Pro. (-1743)")
    monkeypatch.setattr(live.subprocess, "run", run)
    r = live.push_to_fcp(str(doc), library_location=str(bundle), verify_timeout=3)
    assert r["delivered_via"] == "open" and r["verified"] is True
    assert "Automation" in r["apple_events_blocked"]
