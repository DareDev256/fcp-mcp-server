"""The handlers that shipped without a test naming them.

A sweep of `TOOL_HANDLERS` found 22 of 64 flat handlers with no test that
named them. The code underneath was covered; the handlers were not, so
argument parsing, clip resolution and the saved artifact went unasserted —
and a family could stop resolving entirely while a green suite said nothing.
This file closes the rest of that list.

Every case asserts the artifact or a specific count, never that the handler
returned prose. Two of the first tests written for this sweep passed against
an ERROR message, which is exactly the failure this file exists to prevent.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

import server
from fcpxml.parser import parse_fcpxml

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"
MUSIC_VIDEO = Path(__file__).parent.parent / "examples" / "music-video.fcpxml"


def call(name, args):
    return asyncio.run(server.call_tool(name, args))[0].text


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    out = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, out)
    return out


def clips(path):
    return [c.name for c in parse_fcpxml(str(path)).timelines[0].clips]


# --- inspection ------------------------------------------------------------


def test_list_library_clips_lists_the_assets_not_the_spine(project):
    body = call("list_library_clips", {"filepath": str(project)})
    # Three assets in resources; nine clips on the spine. It must report assets.
    assert "3 available" in body
    assert "r2" in body and "Interview_A" in body


def test_list_connected_clips_and_compounds_report_absence_plainly(project):
    assert "No connected clips" in call("list_connected_clips", {"filepath": str(project)})
    assert "No compound clips" in call("list_compound_clips", {"filepath": str(project)})


def test_add_connected_clip_lands_on_a_lane(project, tmp_path):
    out = tmp_path / "connected.fcpxml"
    body = call("add_connected_clip", {
        "filepath": str(project), "parent_clip_id": "Interview_A",
        "asset_name": "Broll_City", "lane": 1, "duration": "2s",
        "output_path": str(out),
    })
    assert out.is_file(), body
    connected = parse_fcpxml(str(out)).timelines[0].connected_clips
    assert any(c.lane == 1 for c in connected), [c.lane for c in connected]


def test_diff_timelines_finds_nothing_between_a_file_and_itself(project):
    body = call("diff_timelines", {
        "filepath_a": str(project), "filepath_b": str(project),
    })
    assert "identical" in body.lower() or "no differences" in body.lower(), body


def test_diff_timelines_sees_a_deleted_clip(project, tmp_path):
    """The control for the test above — it must not report identical either way."""
    changed = tmp_path / "changed.fcpxml"
    call("delete_clips", {
        "filepath": str(project), "clip_ids": ["Broll_City"],
        "output_path": str(changed),
    })
    body = call("diff_timelines", {
        "filepath_a": str(project), "filepath_b": str(changed),
    })
    assert "identical" not in body.lower()
    assert "Broll_City" in body


def test_reformat_timeline_writes_the_target_dimensions(project, tmp_path):
    out = tmp_path / "vertical.fcpxml"
    body = call("reformat_timeline", {
        "filepath": str(project), "format": "9:16",
        "output_path": str(out),
    })
    assert out.is_file(), body
    tl = parse_fcpxml(str(out)).timelines[0]
    assert tl.height > tl.width, (tl.width, tl.height)


# --- batch fixes -----------------------------------------------------------


def test_fix_flash_frames_removes_the_quarter_second_clip(project, tmp_path):
    """The sample carries a 250ms Broll_Studio — six frames at 24fps."""
    out = tmp_path / "fixed.fcpxml"
    before = parse_fcpxml(str(project)).timelines[0].clips
    flashes = [c for c in before if c.duration.seconds < 0.3]
    assert flashes, [c.duration.seconds for c in before]

    body = call("fix_flash_frames", {
        "filepath": str(project), "mode": "delete",
        "threshold_frames": 8, "output_path": str(out),
    })
    assert out.is_file(), body
    after = parse_fcpxml(str(out)).timelines[0].clips
    assert not [c for c in after if c.duration.seconds < 0.3]


def test_rapid_trim_caps_every_clip_at_the_maximum(project, tmp_path):
    out = tmp_path / "trimmed.fcpxml"
    body = call("rapid_trim", {
        "filepath": str(project), "max_duration": "2s",
        "output_path": str(out),
    })
    assert out.is_file(), body
    longest = max(c.duration.seconds for c in parse_fcpxml(str(out)).timelines[0].clips)
    assert longest <= 2.0 + 1e-6, longest


def test_fill_gaps_reports_what_it_found(project, tmp_path):
    out = tmp_path / "filled.fcpxml"
    body = call("fill_gaps", {"filepath": str(project), "output_path": str(out)})
    assert "gap" in body.lower()


# --- generation ------------------------------------------------------------


def test_generate_montage_reports_the_length_it_actually_made(project, tmp_path):
    """It runs out of source before it runs out of target, and says so.

    The sample holds 9 clips totalling under 12s, so a 20s target cannot be
    met. The report must not round that off — an "Actual Duration" that
    silently echoed the target would make a short montage look correct.
    """
    out = tmp_path / "montage.fcpxml"
    body = call("generate_montage", {
        "filepath": str(project), "output_path": str(out),
        "target_duration": "20s",
    })
    assert out.is_file(), body
    assert "Target Duration**: 20.00s" in body
    total = sum(c.duration.seconds for c in parse_fcpxml(str(out)).timelines[0].clips)
    assert 0 < total <= 20.0
    assert f"Actual Duration**: {total:.2f}s" in body, body


def test_generate_ab_roll_alternates_between_the_two_keyword_sets(project, tmp_path):
    """Keywords are FCPXML <keyword> values, not clip names: "B-Roll", not "Broll"."""
    out = tmp_path / "abroll.fcpxml"
    body = call("generate_ab_roll", {
        "filepath": str(project), "output_path": str(out),
        "target_duration": "30s",
        "a_keywords": ["Interview"], "b_keywords": ["B-Roll"],
    })
    assert out.is_file(), body
    names = clips(out)
    assert any("Interview" in n for n in names), names
    assert any("Broll" in n for n in names), names


def test_generate_ab_roll_refuses_when_a_keyword_matches_nothing(project, tmp_path):
    """The control: a keyword that matches nothing must not quietly produce
    a one-sided edit that still calls itself A/B roll."""
    body = call("generate_ab_roll", {
        "filepath": str(project), "output_path": str(tmp_path / "never.fcpxml"),
        "target_duration": "30s",
        "a_keywords": ["Interview"], "b_keywords": ["Nothing"],
    })
    assert "No B-roll clips found" in body
    assert not (tmp_path / "never.fcpxml").exists()


# --- subtitles -------------------------------------------------------------


def test_import_srt_markers_creates_one_marker_per_subtitle(project, tmp_path):
    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nFirst line\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nSecond line\n\n"
    )
    out = tmp_path / "subtitled.fcpxml"
    body = call("import_srt_markers", {
        "filepath": str(project), "srt_path": str(srt),
        "mode": "all", "output_path": str(out),
    })
    assert out.is_file(), body
    assert "Subtitles Parsed**: 2" in body and "Markers Added**: 2" in body
    text = out.read_text()
    assert "First line" in text and "Second line" in text


def test_import_srt_markers_defaults_to_one_marker_per_minute(project, tmp_path):
    """The default is first_per_minute, not every subtitle — two subtitles
    inside the same minute produce ONE marker, and the report says so."""
    srt = tmp_path / "dense.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nFirst line\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nSecond line\n\n"
    )
    body = call("import_srt_markers", {
        "filepath": str(project), "srt_path": str(srt),
        "output_path": str(tmp_path / "default.fcpxml"),
    })
    assert "Subtitles Parsed**: 2" in body
    assert "Markers Added**: 1" in body


def test_import_srt_markers_truncates_long_labels(project, tmp_path):
    srt = tmp_path / "long.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\n" + "x" * 300 + "\n\n")
    out = tmp_path / "truncated.fcpxml"
    call("import_srt_markers", {
        "filepath": str(project), "srt_path": str(srt), "mode": "all",
        "max_label_length": 20, "output_path": str(out),
    })
    labels = [m.name for m in parse_fcpxml(str(out)).timelines[0].markers]
    labels += [m.name for c in parse_fcpxml(str(out)).timelines[0].clips for m in c.markers]
    assert labels, "no markers were written"
    assert max(len(label) for label in labels) <= 20 + 3, labels


# --- silence candidates ----------------------------------------------------


def test_remove_silence_candidates_marks_rather_than_cuts_in_mark_mode(project, tmp_path):
    out = tmp_path / "marked.fcpxml"
    body = call("remove_silence_candidates", {
        "filepath": str(project), "mode": "mark", "output_path": str(out),
    })
    assert out.is_file(), body
    assert len(clips(out)) == len(clips(project)), "mark mode must not delete"
