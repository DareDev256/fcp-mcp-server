"""The watch tool group."""

import asyncio
import shutil
from pathlib import Path

import pytest

import server
import tools
import tools.watch as watch_mod

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"
MUSIC = Path(__file__).parent.parent / "examples" / "music-video.fcpxml"


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    watch_mod._STATE.clear()
    # Never probe real ports from a test; describe() would otherwise depend on
    # whether the developer happens to have SpliceKit running.
    monkeypatch.setattr(watch_mod.bridges, "probe", lambda **kw: False)
    watch_mod.bridges._CACHE.clear()
    yield
    watch_mod._STATE.clear()
    watch_mod.bridges._CACHE.clear()


def _call(action, args=None):
    return asyncio.run(
        server.handle_group("watch", {"action": action, "args": args or {}})
    )


def test_watch_group_is_advertised():
    assert "watch" in tools.EXTRA_GROUPS
    for expected in ("watch_start", "watch_status", "watch_stop", "watch_pull"):
        assert expected in tools.EXTRA_GROUPS["watch"]["actions"]
    assert "watch" in server.TOOL_GROUPS


def test_every_watch_action_is_reachable_through_call_tool():
    for action in tools.EXTRA_GROUPS["watch"]["actions"]:
        result = asyncio.run(server.call_tool("watch", {"action": action}))
        assert result and result[0].text


def test_status_before_start_says_so():
    assert "Not watching" in _call("watch_status")[0].text


def test_pull_before_start_points_at_watch_start():
    assert "watch_start" in _call("watch_pull")[0].text


def test_start_without_a_directory_or_env_says_what_to_set(monkeypatch):
    monkeypatch.delenv("FCP_WATCH_DIR", raising=False)
    assert "FCP_WATCH_DIR" in _call("watch_start")[0].text


def test_start_falls_back_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FCP_WATCH_DIR", str(tmp_path))
    assert str(tmp_path) in _call("watch_start")[0].text


def test_start_on_a_missing_directory_reports_rather_than_raises(tmp_path):
    text = _call("watch_start", {"directory": str(tmp_path / "nope")})[0].text
    assert "not a directory" in text


def test_start_then_status_names_the_directory(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    assert str(tmp_path) in _call("watch_status")[0].text


def test_start_reports_the_manual_export_path_when_no_bridge_is_up(tmp_path):
    text = _call("watch_start", {"directory": str(tmp_path)})[0].text
    assert "Cmd-E" in text


def test_pull_times_out_with_the_keystroke_that_ends_the_wait(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    text = _call("watch_pull", {"timeout": 0.3})[0].text
    assert "No export in" in text
    assert "Cmd-E" in text
    assert str(tmp_path) in text


def test_pull_rejects_a_non_numeric_timeout(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    assert "timeout must be a number" in _call("watch_pull", {"timeout": "soon"})[0].text


def test_the_first_export_is_the_baseline(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    shutil.copy(SAMPLE, tmp_path / "first.fcpxml")
    text = _call("watch_pull", {"timeout": 2.0})[0].text
    assert "Export detected" in text
    assert "baseline" in text


def test_the_second_export_is_diffed_against_the_first(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    shutil.copy(SAMPLE, tmp_path / "a.fcpxml")
    _call("watch_pull", {"timeout": 2.0})
    shutil.copy(MUSIC, tmp_path / "b.fcpxml")
    text = _call("watch_pull", {"timeout": 2.0})[0].text
    assert "Export detected" in text
    assert "Timeline Diff" in text


def test_a_diff_against_a_vanished_previous_export_does_not_break_the_pull(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    first = tmp_path / "a.fcpxml"
    shutil.copy(SAMPLE, first)
    _call("watch_pull", {"timeout": 2.0})
    first.unlink()
    shutil.copy(MUSIC, tmp_path / "b.fcpxml")
    text = _call("watch_pull", {"timeout": 2.0})[0].text
    assert "Export detected" in text


def test_stop_clears_state(tmp_path):
    _call("watch_start", {"directory": str(tmp_path)})
    assert "Stopped watching" in _call("watch_stop")[0].text
    assert "Not watching" in _call("watch_status")[0].text
