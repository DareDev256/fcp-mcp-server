"""edl.json -> FCPXML.

browser-use/video-use reasons over footage and its pipeline terminates at a
flat mp4. This is the bridge that lets that reasoning finish in Final Cut Pro,
which is the only outcome that works for anyone delivering a project file
rather than a video.

The schema here was read off the repo (helpers/render.py extract_all_segments
and build_master_srt, tests/test_render_fps.py), NOT assumed:

    {"sources": {"name": "path.mp4", ...},
     "ranges":  [{"source": "name", "start": 0, "end": 1}, ...],
     "grade":   "auto" | preset | raw filter   (optional)}

`ranges[].source` is a KEY into `sources`, not a path. Guessing it was a path
is exactly the mistake this test file exists to prevent.
"""

import json
from fractions import Fraction

import pytest

from fcpxml.edl import EDLValidationError, edl_to_fcpxml, parse_edl
from fcpxml.parser import FCPXMLParser


def _edl(**over):
    base = {
        "sources": {"first": "first.mp4", "second": "second.mp4"},
        "ranges": [
            {"source": "first", "start": 0, "end": 2},
            {"source": "second", "start": 5.5, "end": 7},
        ],
    }
    base.update(over)
    return base


@pytest.fixture
def media(tmp_path):
    for name in ("first.mp4", "second.mp4"):
        (tmp_path / name).write_bytes(b"\x00")
    return tmp_path


def test_parses_ranges_in_order(media):
    cuts = parse_edl(_edl(), base_dir=str(media))
    assert [c["label"] for c in cuts] == ["first", "second"]


def test_a_range_source_is_a_key_not_a_path(media):
    """The field names a source; the path comes from the sources map."""
    cut = parse_edl(_edl(), base_dir=str(media))[0]
    assert cut["source"] == str(media / "first.mp4")


def test_relative_paths_resolve_against_the_edl_directory(media):
    cut = parse_edl(_edl(), base_dir=str(media))[0]
    assert cut["source"].startswith(str(media))


def test_absolute_paths_are_left_alone(tmp_path):
    absolute = tmp_path / "elsewhere.mp4"
    absolute.write_bytes(b"\x00")
    cuts = parse_edl(
        {"sources": {"a": str(absolute)},
         "ranges": [{"source": "a", "start": 0, "end": 1}]},
        base_dir=str(tmp_path / "other"),
    )
    assert cuts[0]["source"] == str(absolute)


def test_times_become_exact_fractions(media):
    cut = parse_edl(_edl(), base_dir=str(media))[1]
    assert isinstance(cut["src_in"], Fraction)
    assert cut["src_in"] == Fraction(11, 2)
    assert cut["src_out"] == Fraction(7, 1)


def test_missing_ranges_key_is_rejected():
    with pytest.raises(EDLValidationError, match="ranges"):
        parse_edl({"sources": {"a": "a.mp4"}})


def test_missing_sources_key_is_rejected():
    with pytest.raises(EDLValidationError, match="sources"):
        parse_edl({"ranges": [{"source": "a", "start": 0, "end": 1}]})


def test_empty_ranges_is_rejected():
    with pytest.raises(EDLValidationError, match="at least one"):
        parse_edl({"sources": {"a": "a.mp4"}, "ranges": []})


def test_a_range_naming_an_unknown_source_is_rejected_by_index():
    with pytest.raises(EDLValidationError, match="range 1"):
        parse_edl({
            "sources": {"a": "a.mp4"},
            "ranges": [
                {"source": "a", "start": 0, "end": 1},
                {"source": "ghost", "start": 0, "end": 1},
            ],
        })


def test_the_unknown_source_error_names_what_was_available():
    with pytest.raises(EDLValidationError, match="known sources"):
        parse_edl({
            "sources": {"a": "a.mp4"},
            "ranges": [{"source": "ghost", "start": 0, "end": 1}],
        })


def test_a_zero_length_range_is_rejected():
    with pytest.raises(EDLValidationError, match="range 0"):
        parse_edl({
            "sources": {"a": "a.mp4"},
            "ranges": [{"source": "a", "start": 2, "end": 2}],
        })


def test_a_reversed_range_is_rejected():
    with pytest.raises(EDLValidationError, match="range 0"):
        parse_edl({
            "sources": {"a": "a.mp4"},
            "ranges": [{"source": "a", "start": 3, "end": 1}],
        })


def test_a_non_numeric_time_is_rejected_by_index():
    with pytest.raises(EDLValidationError, match="range 0"):
        parse_edl({
            "sources": {"a": "a.mp4"},
            "ranges": [{"source": "a", "start": "soon", "end": 1}],
        })


def test_writes_a_parseable_fcpxml(media, tmp_path):
    out = tmp_path / "edl.fcpxml"
    result = edl_to_fcpxml(_edl(), str(out), base_dir=str(media), name="Bridge Test")
    assert result["path"] == str(out)
    timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
    assert timeline is not None
    assert len(timeline.clips) == 2
    assert [c.name for c in timeline.clips] == ["first", "second"]


def test_total_duration_is_the_sum_of_the_ranges(media, tmp_path):
    out = tmp_path / "edl.fcpxml"
    edl_to_fcpxml(_edl(), str(out), base_dir=str(media))
    timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
    # 2.0s + 1.5s
    assert abs(sum(c.duration_seconds for c in timeline.clips) - 3.5) < 1e-6


def test_clips_are_laid_end_to_end_with_no_gap(media, tmp_path):
    out = tmp_path / "edl.fcpxml"
    edl_to_fcpxml(_edl(), str(out), base_dir=str(media))
    timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
    first, second = timeline.clips
    assert abs(second.start.seconds - (first.start.seconds + first.duration_seconds)) < 1e-6


def test_the_source_in_point_survives_the_round_trip(media, tmp_path):
    """The second range starts 5.5s into its source; that must not become 0."""
    out = tmp_path / "edl.fcpxml"
    edl_to_fcpxml(_edl(), str(out), base_dir=str(media))
    timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
    assert abs(timeline.clips[1].source_start.seconds - 5.5) < 1e-6


def test_a_grade_is_reported_as_ignored_not_silently_dropped(media, tmp_path):
    """FCPXML carries the original media; colour is a call made in FCP.

    Dropping it silently would leave the operator believing their grade came
    across.
    """
    out = tmp_path / "edl.fcpxml"
    result = edl_to_fcpxml(
        _edl(grade="auto"), str(out), base_dir=str(media)
    )
    assert any("grade" in note for note in result["ignored"])


def test_no_grade_means_nothing_to_report(media, tmp_path):
    out = tmp_path / "edl.fcpxml"
    result = edl_to_fcpxml(_edl(), str(out), base_dir=str(media))
    assert result["ignored"] == []


def test_a_source_used_twice_gets_one_asset(media, tmp_path):
    out = tmp_path / "edl.fcpxml"
    edl_to_fcpxml(
        {"sources": {"a": "first.mp4"},
         "ranges": [
             {"source": "a", "start": 0, "end": 1},
             {"source": "a", "start": 4, "end": 5},
         ]},
        str(out), base_dir=str(media),
    )
    text = out.read_text()
    assert text.count("<asset ") == 1
    timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
    assert len(timeline.clips) == 2


def test_the_asset_is_long_enough_for_the_furthest_range_used(media, tmp_path):
    """An asset shorter than the range using it is an invalid FCPXML."""
    out = tmp_path / "edl.fcpxml"
    edl_to_fcpxml(
        {"sources": {"a": "first.mp4"},
         "ranges": [{"source": "a", "start": 90, "end": 100}]},
        str(out), base_dir=str(media),
    )
    project = FCPXMLParser().parse_file(str(out))
    assert project.primary_timeline is not None
    assert "duration=" in out.read_text()


def test_a_missing_media_file_is_reported_not_fatal(tmp_path):
    out = tmp_path / "edl.fcpxml"
    result = edl_to_fcpxml(
        {"sources": {"a": "gone.mp4"},
         "ranges": [{"source": "a", "start": 0, "end": 1}]},
        str(out), base_dir=str(tmp_path),
    )
    assert result["path"] == str(out)
    assert any("gone.mp4" in note for note in result["missing"])


def test_a_real_video_use_shaped_payload_round_trips(media, tmp_path):
    """The exact literal from video-use's own tests/test_render_fps.py."""
    payload = json.loads(json.dumps({
        "sources": {"first": "first.mp4", "second": "second.mp4"},
        "ranges": [
            {"source": "first", "start": 0, "end": 1},
            {"source": "second", "start": 0, "end": 1},
        ],
    }))
    out = tmp_path / "vu.fcpxml"
    edl_to_fcpxml(payload, str(out), base_dir=str(media))
    timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
    assert len(timeline.clips) == 2


class TestServerWiring:
    """import_edl_json reaches the MCP surface through the generate group."""

    def test_the_action_is_registered(self):
        import server

        assert "import_edl_json" in server.TOOL_HANDLERS
        assert "import_edl_json" in server.TOOL_GROUPS["generate"]["actions"]

    def test_missing_filepath_is_reported(self):
        import asyncio

        import server

        result = asyncio.run(server.handle_group(
            "generate", {"action": "import_edl_json", "args": {}}
        ))
        assert "filepath" in result[0].text

    def test_end_to_end_through_the_group(self, media, tmp_path):
        import asyncio

        import server

        payload = tmp_path / "edl.json"
        payload.write_text(json.dumps({
            "sources": {"first": str(media / "first.mp4")},
            "ranges": [{"source": "first", "start": 0, "end": 2}],
        }))
        out = tmp_path / "authored.fcpxml"
        result = asyncio.run(server.handle_group("generate", {
            "action": "import_edl_json",
            "args": {"filepath": str(payload), "output_path": str(out)},
        }))
        text = result[0].text
        assert "Authored" in text
        assert "1 ranges" in text
        timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
        assert len(timeline.clips) == 1

    def test_an_invalid_edl_reports_the_offending_range(self, tmp_path):
        import asyncio

        import server

        payload = tmp_path / "bad.json"
        payload.write_text(json.dumps({
            "sources": {"a": "a.mp4"},
            "ranges": [{"source": "ghost", "start": 0, "end": 1}],
        }))
        result = asyncio.run(server.handle_group("generate", {
            "action": "import_edl_json", "args": {"filepath": str(payload)},
        }))
        assert "Invalid EDL" in result[0].text
        assert "range 0" in result[0].text

    def test_malformed_json_is_reported_not_raised(self, tmp_path):
        import asyncio

        import server

        payload = tmp_path / "broken.json"
        payload.write_text("{not json")
        result = asyncio.run(server.handle_group("generate", {
            "action": "import_edl_json", "args": {"filepath": str(payload)},
        }))
        assert "Could not read" in result[0].text
