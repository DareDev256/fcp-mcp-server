"""Live mode drives the free trial too.

The trial is its own app, "Final Cut Pro Trial" (com.apple.FinalCutTrial),
with the same scripting dictionary. AppleScript, pgrep and open address an
app by its name, so with only the trial installed every command that named
"Final Cut Pro" failed: `tell application "Final Cut Pro"` answers -1728 and
pgrep never sees the process.

osascript, pgrep and open are mocked: nothing here launches Final Cut.
"""

import subprocess

import pytest

from fcpxml import live

XML = (
    '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE fcpxml><fcpxml version="1.11">'
    '<resources><format id="r1" frameDuration="1/24s" width="1920" height="1080"/></resources>'
    '<library><event name="Evt"><project name="Trial Test">'
    '<sequence format="r1" duration="240/24s"><spine>'
    '<gap name="Gap" offset="0s" duration="240/24s"/>'
    '</spine></sequence></project></event></library></fcpxml>'
)


def _installed(monkeypatch, tmp_path, *names):
    apps = tmp_path / "Applications"
    apps.mkdir()
    for name in names:
        (apps / f"{name}.app").mkdir()
    monkeypatch.setattr(live, "_APPLICATIONS", apps)


@pytest.fixture
def commands(monkeypatch):
    """Every command live mode runs; all of them succeed."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "_fcp_answered", False)
    monkeypatch.setattr(live, "_blocked_until", 0.0)
    return calls


def test_the_full_app_is_named_when_installed(monkeypatch, tmp_path):
    _installed(monkeypatch, tmp_path, "Final Cut Pro", "Final Cut Pro Trial")
    assert live.fcp_app_name() == "Final Cut Pro"


def test_the_trial_is_named_when_it_is_the_only_one(monkeypatch, tmp_path):
    _installed(monkeypatch, tmp_path, "Final Cut Pro Trial")
    assert live.fcp_app_name() == "Final Cut Pro Trial"


def test_no_app_found_keeps_the_usual_name(monkeypatch, tmp_path):
    _installed(monkeypatch, tmp_path)
    assert live.fcp_app_name() == "Final Cut Pro"


def test_running_check_looks_for_the_trial_process(monkeypatch, commands, tmp_path):
    _installed(monkeypatch, tmp_path, "Final Cut Pro Trial")
    assert live.fcp_is_running()
    assert commands == [["pgrep", "-x", "Final Cut Pro Trial"]]


def test_libraries_are_asked_of_the_trial(monkeypatch, commands, tmp_path):
    _installed(monkeypatch, tmp_path, "Final Cut Pro Trial")
    live.list_fcp_libraries()
    scripts = [cmd[2] for cmd in commands if cmd[0] == "osascript"]
    assert len(scripts) == 2  # the probe, then the listing
    assert all('tell application "Final Cut Pro Trial"' in s for s in scripts)


def test_push_tells_the_trial(monkeypatch, commands, tmp_path):
    _installed(monkeypatch, tmp_path, "Final Cut Pro Trial")
    src = tmp_path / "cut.fcpxml"
    src.write_text(XML)
    live.push_to_fcp(str(src), library_location=str(tmp_path / "Lib.fcpbundle"), verify=False)
    assert 'tell application "Final Cut Pro Trial"\nactivate' in commands[-1][2]


def test_blocked_push_opens_with_the_trial(monkeypatch, tmp_path):
    _installed(monkeypatch, tmp_path, "Final Cut Pro Trial")
    opened = []

    def run(cmd, **kwargs):
        if cmd[0] == "osascript":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        if cmd[0] == "open":
            opened.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live, "_fcp_answered", False)
    monkeypatch.setattr(live, "_blocked_until", 0.0)
    src = tmp_path / "cut.fcpxml"
    src.write_text(XML)
    result = live.push_to_fcp(str(src), library_location=str(tmp_path / "Lib.fcpbundle"), verify=False)
    assert result["delivered_via"] == "open"
    assert opened and opened[0][:3] == ["open", "-a", "Final Cut Pro Trial"]
