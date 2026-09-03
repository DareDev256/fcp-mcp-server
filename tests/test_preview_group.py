"""The preview tool group.

Wiring tests: the group is advertised, every action dispatches, bad input is
reported rather than raised, and the two things an operator must never miss —
a substitution and an unverified render — are said out loud.
"""

import asyncio
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import server
import tools
import tools.preview as preview_mod

FFMPEG = shutil.which("ffmpeg") is not None
SAMPLE = str(Path(__file__).parent.parent / "examples" / "sample.fcpxml")


@pytest.fixture(autouse=True)
def never_open_a_pane(monkeypatch):
    """Tests must not spawn the image-preview pane."""
    monkeypatch.setattr(preview_mod, "_open_beside_terminal", lambda paths: None)


def _call(action, args=None):
    return asyncio.run(
        server.handle_group("preview", {"action": action, "args": args or {}})
    )


def test_preview_group_is_advertised():
    assert "preview" in tools.EXTRA_GROUPS
    actions = tools.EXTRA_GROUPS["preview"]["actions"]
    for expected in (
        "preview_render", "preview_sheet", "preview_frame",
        "preview_check", "preview_timeline",
    ):
        assert expected in actions
    assert "preview" in server.TOOL_GROUPS


def test_every_preview_action_is_reachable_through_call_tool():
    for action in tools.EXTRA_GROUPS["preview"]["actions"]:
        result = asyncio.run(server.call_tool("preview", {"action": action}))
        assert result and result[0].text


def test_unknown_preview_action_lists_the_valid_ones():
    result = asyncio.run(server.handle_group("preview", {"action": "nope"}))
    assert "preview_render" in result[0].text


def test_preview_render_without_filepath_says_so():
    assert "filepath" in _call("preview_render")[0].text


def test_preview_render_on_a_missing_file_reports_rather_than_raises(tmp_path):
    text = _call("preview_render", {"filepath": str(tmp_path / "nope.fcpxml")})[0].text
    assert "Could not read" in text or "not found" in text.lower()


def test_preview_render_rejects_a_non_integer_height():
    text = _call("preview_render", {"filepath": SAMPLE, "height": "tall"})[0].text
    assert "height" in text


def test_preview_check_rejects_a_reversed_range(tmp_path):
    text = _call("preview_check", {
        "source": str(tmp_path / "x.mov"), "start": 2.0, "end": 1.0,
    })[0].text
    assert "end must be after start" in text


def test_preview_check_rejects_a_non_numeric_position(tmp_path):
    text = _call("preview_check", {
        "source": str(tmp_path / "x.mov"), "start": "soon", "end": 1.0,
    })[0].text
    assert "start must be a number" in text


def test_preview_frame_rejects_a_non_numeric_position(tmp_path):
    text = _call("preview_frame", {"source": str(tmp_path / "x.mov"), "at": "later"})[0].text
    assert "at must be a number" in text


def test_substitutions_are_surfaced_to_the_operator(monkeypatch, tmp_path):
    """A dissolve rendered as a hard cut must be said out loud."""
    monkeypatch.setattr(preview_mod.render, "render_proxy", lambda *a, **k: {
        "path": str(tmp_path / "p.mp4"), "duration": Fraction(2),
        "expected": Fraction(2), "drift": Fraction(0),
        "substitutions": ("'Cross Dissolve' at 0.92s rendered as a hard cut",),
        "skipped": [], "error": None,
    })
    text = _call("preview_render", {"filepath": SAMPLE})[0].text
    assert "Cross Dissolve" in text
    assert "hard cut" in text


def test_an_unverified_render_is_labelled_unverified(monkeypatch, tmp_path):
    """A render whose duration could not be read must not read like a good one.

    This is the whole point of the read-back: if a failed probe printed the
    same summary as a successful one, the probe would certify nothing.
    """
    monkeypatch.setattr(preview_mod.render, "render_proxy", lambda *a, **k: {
        "path": str(tmp_path / "p.mp4"), "duration": None,
        "expected": Fraction(2), "drift": None,
        "substitutions": (), "skipped": [], "error": None,
    })
    text = _call("preview_render", {"filepath": SAMPLE})[0].text
    assert "UNVERIFIED" in text


def test_a_verified_render_is_not_labelled_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr(preview_mod.render, "render_proxy", lambda *a, **k: {
        "path": str(tmp_path / "p.mp4"), "duration": Fraction(2),
        "expected": Fraction(2), "drift": Fraction(0),
        "substitutions": (), "skipped": [], "error": None,
    })
    text = _call("preview_render", {"filepath": SAMPLE})[0].text
    assert "UNVERIFIED" not in text
    assert "drift" in text


def test_preview_timeline_draws_the_sample_project():
    text = _call("preview_timeline", {"filepath": SAMPLE})[0].text
    assert "#" in text
    assert "s" in text


def test_preview_timeline_flags_missing_media():
    """sample.fcpxml points at media that does not exist on this machine."""
    text = _call("preview_timeline", {"filepath": SAMPLE})[0].text
    assert "MEDIA MISSING" in text


def test_the_pane_is_never_opened_for_a_failed_render(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(preview_mod, "_open_beside_terminal", opened.append)
    monkeypatch.setattr(preview_mod.render, "render_proxy", lambda *a, **k: {
        "path": None, "duration": None, "expected": Fraction(0), "drift": None,
        "substitutions": (), "skipped": ["a"], "error": "ffmpeg is not on PATH.",
    })
    _call("preview_render", {"filepath": SAMPLE})
    assert opened == []


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_preview_check_end_to_end_on_real_media(tmp_path):
    source = tmp_path / "bars.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        capture_output=True, timeout=120, check=True,
    )
    text = _call("preview_check", {
        "source": str(source), "start": 0, "end": 2,
    })[0].text
    assert "Filmstrip + waveform" in text
    assert "not from the XML" in text
