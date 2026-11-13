"""The view tool group: advertised, dispatching, read-only, honest about gaps."""

import asyncio
import shutil
from pathlib import Path

import pytest

import server
import tools
import tools.view as view_mod
from fcpxml import journal, timeline_app

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.fcpxml"


def _call(action, args=None):
    return asyncio.run(server.handle_group("view", {"action": action, "args": args or {}}))


def test_view_group_is_advertised_with_both_actions():
    assert "view" in tools.EXTRA_GROUPS and "view" in server.TOOL_GROUPS
    assert tools.EXTRA_GROUPS["view"]["actions"] == ["view_timeline", "view_clip"]
    assert tools.EXTRA_GROUPS["view"]["meta"] == view_mod.UI_META


def test_every_view_action_is_reachable_through_call_tool():
    for action in tools.EXTRA_GROUPS["view"]["actions"]:
        result = asyncio.run(server.call_tool("view", {"action": action}))
        assert result and "filepath" in result[0].text


def test_view_timeline_carries_the_payload_and_the_hash(tmp_path):
    project = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, project)
    text = _call("view_timeline", {"filepath": str(project)})[0].text
    payload = timeline_app.extract_payload(text)
    assert payload is not None
    assert payload["file"]["path"] == str(project.resolve())
    assert payload["file"]["sha256"] == journal.file_hash(str(project))
    assert payload["file"]["sha256"] in text.split(timeline_app.PAYLOAD_OPEN)[0]
    assert "ui://fcp/timeline" in text


def test_view_timeline_is_read_only_and_not_journaled(tmp_path):
    folder = tmp_path / "proj"  # its own folder: the suite's index cache sits in tmp_path
    folder.mkdir()
    project = folder / "sample.fcpxml"
    shutil.copy(SAMPLE, project)
    asyncio.run(server.call_tool("view", {"action": "view_timeline", "args": {"filepath": str(project)}}))
    assert [p.name for p in folder.iterdir()] == ["sample.fcpxml"]
    assert journal.records(str(project)) == []


def test_thumbnails_and_words_can_be_switched_off(monkeypatch):
    asked = {"thumb": 0, "words": 0}
    monkeypatch.setattr(view_mod, "_thumbnail", lambda m, at: asked.__setitem__("thumb", asked["thumb"] + 1))
    monkeypatch.setattr(view_mod, "_transcript", lambda m: asked.__setitem__("words", asked["words"] + 1))
    _call("view_timeline", {"filepath": str(SAMPLE), "thumbnails": False, "words": False})
    assert asked == {"thumb": 0, "words": 0}
    _call("view_timeline", {"filepath": str(SAMPLE)})
    assert asked["thumb"] > 0 and asked["words"] > 0


def test_missing_media_yields_no_thumbnail_rather_than_an_error():
    # The fixture's media (file:///Media/...) does not exist on this machine.
    assert view_mod._thumbnail("file:///Media/Interview_A.mov", 1) is None
    text = _call("view_timeline", {"filepath": str(SAMPLE)})[0].text
    assert "0 thumbnails" in text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_a_real_clip_gets_a_bounded_jpeg_thumbnail(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))  # the thumbnail cache lives under ~/.fcp-mcp
    media = tmp_path / "bars.mov"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x180:rate=24", "-t", "1", "-pix_fmt", "yuv420p", str(media)],
        check=True,
    )
    uri = view_mod._thumbnail(str(media), 0.5)
    assert uri and uri.startswith("data:image/jpeg;base64,")
    assert len(uri) < 20_000
    # Second call is served from the cache: ffmpeg is not run again.
    monkeypatch.setattr(view_mod.render, "render_frame", lambda *a, **k: pytest.fail("re-rendered"))
    assert view_mod._thumbnail(str(media), 0.5) == uri


def test_view_clip_reports_the_clip_and_missing_media():
    text = _call("view_clip", {"filepath": str(SAMPLE), "clip_name": "Broll_City"})[0].text
    assert text.startswith("# Broll_City")
    assert "3.000s (3/1)" in text and "MISSING" in text and "sha256:" in text
    assert "never done implicitly" in text


def test_view_clip_names_the_clips_when_the_name_is_unknown():
    text = _call("view_clip", {"filepath": str(SAMPLE), "clip_name": "nope"})[0].text
    assert "No clip named 'nope'" in text and "Interview_A" in text


def test_view_clip_requires_a_name():
    assert "clip_name" in _call("view_clip", {"filepath": str(SAMPLE)})[0].text


# ---------------------------------------------------------------------------
# 0.31: the summary line agrees with diagnose and with what the app draws
# ---------------------------------------------------------------------------

MUSIC_VIDEO = SAMPLE.parent / "music-video.fcpxml"


def _summary(path):
    return _call("view_timeline", {"filepath": str(path), "thumbnails": False})[0].text.split("\n")[0]


def _diagnose_gaps(path):
    text = asyncio.run(server.call_tool(
        "diagnose", {"action": "validate_timeline", "args": {"filepath": str(path)}}))[0].text
    return int(next(line for line in text.splitlines() if line.startswith("| Gaps |")).split("|")[2])


@pytest.mark.parametrize("path", [SAMPLE, MUSIC_VIDEO], ids=["sample", "music-video"])
def test_the_gap_count_is_the_one_diagnose_reports(path):
    """0.30 counted the hatched spans the app draws: "1 gaps" on the music
    video, where validate_timeline says 0. Now both read _detect_gaps."""
    n = _diagnose_gaps(path)
    assert f", {n} gap{'' if n == 1 else 's'}" in _summary(path)


def test_a_gap_based_storyline_is_named_not_counted():
    summary = _summary(MUSIC_VIDEO)
    assert "0 gaps (gap-based storyline)" in summary
    assert "1 gaps" not in summary
    # the app still gets the hatched span to draw
    text = _call("view_timeline", {"filepath": str(MUSIC_VIDEO), "thumbnails": False})[0].text
    assert len(timeline_app.extract_payload(text)["gaps"]) == 1


def test_markers_on_clips_are_counted():
    """Chapter markers live on their clips; 0.30 said "0 timeline markers"
    under a ruler showing them."""
    text = _call("view_timeline", {"filepath": str(SAMPLE), "thumbnails": False})[0].text
    payload = timeline_app.extract_payload(text)
    on_clips = sum(len(c["markers"]) for c in payload["clips"])
    total = on_clips + len(payload["markers"])
    assert on_clips > 0, "the fixture must carry clip markers for this to mean anything"
    assert f"{total} markers ({on_clips} on clips)" in text.split("\n")[0]
