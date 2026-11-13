"""Tests for server.py — tool handlers, utility functions, and dispatch logic.

The `mcp` package may not be installed in the test Python environment (system
Python vs uv venv).  We shim it with lightweight stubs so server.py can be
imported without the real MCP SDK.
"""

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shim the `mcp` package tree before importing server.py
# ---------------------------------------------------------------------------
_NEEDS_SHIM = "mcp" not in sys.modules or "mcp.server" not in sys.modules


def _install_mcp_shim():
    """Create lightweight fakes for the mcp modules server.py imports."""
    mcp = types.ModuleType("mcp")
    mcp_server = types.ModuleType("mcp.server")
    mcp_server_stdio = types.ModuleType("mcp.server.stdio")
    mcp_types = types.ModuleType("mcp.types")

    # mcp.server.Server — needs to be callable and return an object with
    # decorator methods (call_tool, list_resources, etc.)
    class _FakeServer:
        def __init__(self, *a, **kw):
            pass

        # Decorator stubs — return the decorated function unchanged
        def call_tool(self):
            return lambda fn: fn

        def list_tools(self):
            return lambda fn: fn

        def list_resources(self):
            return lambda fn: fn

        def read_resource(self):
            return lambda fn: fn

        def list_prompts(self):
            return lambda fn: fn

        def get_prompt(self):
            return lambda fn: fn

    mcp_server.Server = _FakeServer

    # mcp.server.stdio — just needs an async context manager
    async def _fake_stdio_server(srv):
        yield (MagicMock(), MagicMock())

    class _FakeCtx:
        def __init__(self, *a):
            pass
        async def __aenter__(self):
            return (MagicMock(), MagicMock())
        async def __aexit__(self, *a):
            pass

    mcp_server_stdio.stdio_server = _FakeCtx

    # mcp.types — dataclasses used by server.py
    class TextContent:
        def __init__(self, *, type: str, text: str):
            self.type = type
            self.text = text

    for name in ("GetPromptResult", "Prompt", "PromptArgument",
                 "PromptMessage", "Resource", "Tool"):
        setattr(mcp_types, name, MagicMock)
    mcp_types.TextContent = TextContent

    # Register in sys.modules
    sys.modules.setdefault("mcp", mcp)
    sys.modules.setdefault("mcp.server", mcp_server)
    sys.modules.setdefault("mcp.server.stdio", mcp_server_stdio)
    sys.modules.setdefault("mcp.types", mcp_types)


if _NEEDS_SHIM:
    _install_mcp_shim()

from server import (  # noqa: E402
    _parse_timestamp_parts,
    call_tool,
    find_fcpxml_files,
    format_duration,
    format_timecode,
    generate_output_path,
    handle_analyze_pacing,
    handle_analyze_timeline,
    handle_detect_duplicates,
    handle_detect_flash_frames,
    handle_detect_gaps,
    handle_export_csv,
    handle_export_edl,
    handle_find_long_clips,
    handle_find_short_cuts,
    handle_import_transcript_markers,
    handle_list_clips,
    handle_list_keywords,
    handle_list_markers,
    handle_list_projects,
    handle_validate_timeline,
    parse_srt,
    parse_transcript_timestamps,
    parse_vtt,
)

SAMPLE = str(Path(__file__).parent.parent / "examples" / "sample.fcpxml")



# ============================================================
# Utility Functions
# ============================================================


class TestFormatDuration:
    def test_milliseconds(self):
        assert format_duration(0.25) == "250ms"

    def test_sub_second(self):
        assert format_duration(0.999) == "999ms"

    def test_seconds(self):
        assert format_duration(3.5) == "3.50s"

    def test_one_second(self):
        assert format_duration(1.0) == "1.00s"

    def test_minutes(self):
        assert format_duration(90.5) == "1m 30.5s"

    def test_zero(self):
        assert format_duration(0) == "0ms"


class TestFormatTimecode:
    def test_none_returns_zeros(self):
        assert format_timecode(None) == "00:00:00:00"

    def test_with_timecode(self):
        from fcpxml.models import Timecode
        tc = Timecode(frames=48, frame_rate=24)
        result = format_timecode(tc)
        assert "00:00:02:00" == result


class TestGenerateOutputPath:
    def test_default_suffix(self):
        result = generate_output_path("/path/to/project.fcpxml")
        assert result == "/path/to/project_modified.fcpxml"

    def test_custom_suffix(self):
        result = generate_output_path("/path/to/edit.fcpxml", "_chapters")
        assert result == "/path/to/edit_chapters.fcpxml"


class TestFindFcpxmlFiles:
    def test_finds_fcpxml(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.fcpxml").touch()
            Path(d, "b.fcpxmld").touch()
            Path(d, "c.txt").touch()
            files = find_fcpxml_files(d)
            assert len(files) == 2
            assert any("a.fcpxml" in f for f in files)
            assert any("b.fcpxmld" in f for f in files)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            assert find_fcpxml_files(d) == []

    def test_nested_files(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d, "sub")
            sub.mkdir()
            Path(sub, "deep.fcpxml").touch()
            files = find_fcpxml_files(d)
            assert len(files) == 1


# ============================================================
# SRT / VTT / Transcript Parsers
# ============================================================


class TestParseSrt:
    def test_basic_srt(self):
        srt = """1
00:00:05,000 --> 00:00:10,000
Hello world

2
00:01:30,500 --> 00:01:35,000
Second subtitle
"""
        markers = parse_srt(srt)
        assert len(markers) == 2
        assert markers[0]["seconds"] == 5.0
        assert markers[0]["text"] == "Hello world"
        assert markers[1]["seconds"] == 90.5
        assert markers[1]["text"] == "Second subtitle"

    def test_multiline_subtitle(self):
        srt = """1
00:00:10,000 --> 00:00:15,000
Line one
Line two
"""
        markers = parse_srt(srt)
        assert len(markers) == 1
        assert markers[0]["text"] == "Line one Line two"

    def test_empty_input(self):
        assert parse_srt("") == []

    def test_malformed_blocks_skipped(self):
        srt = """garbage

1
00:00:01,000 --> 00:00:02,000
Valid
"""
        markers = parse_srt(srt)
        assert len(markers) == 1
        assert markers[0]["text"] == "Valid"


class TestParseVtt:
    def test_basic_vtt(self):
        vtt = """WEBVTT

00:00:05.000 --> 00:00:10.000
Hello world

00:01:30.500 --> 00:01:35.000
Second subtitle
"""
        markers = parse_vtt(vtt)
        assert len(markers) == 2
        assert markers[0]["seconds"] == 5.0
        assert markers[1]["seconds"] == 90.5

    def test_short_timestamp(self):
        """VTT allows MM:SS.mmm format (no hours)."""
        vtt = """WEBVTT

01:30.500 --> 01:35.000
Short format
"""
        markers = parse_vtt(vtt)
        assert len(markers) == 1
        assert markers[0]["seconds"] == 90.5

    def test_strips_html_tags(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:05.000
<b>Bold</b> and <i>italic</i>
"""
        markers = parse_vtt(vtt)
        assert len(markers) == 1
        assert markers[0]["text"] == "Bold and italic"

    def test_note_blocks_removed(self):
        vtt = """WEBVTT

NOTE
This is a note

00:00:01.000 --> 00:00:05.000
Real subtitle
"""
        markers = parse_vtt(vtt)
        assert len(markers) >= 1
        assert any(m["text"] == "Real subtitle" for m in markers)


class TestParseTranscriptTimestamps:
    def test_mm_ss_format(self):
        text = "0:00 Introduction\n1:30 Main Topic\n"
        markers = parse_transcript_timestamps(text)
        assert len(markers) == 2
        assert markers[0]["seconds"] == 0
        assert markers[0]["text"] == "Introduction"
        assert markers[1]["seconds"] == 90

    def test_hh_mm_ss_format(self):
        text = "01:05:30 Conclusion\n"
        markers = parse_transcript_timestamps(text)
        assert len(markers) == 1
        assert markers[0]["seconds"] == 3930

    def test_smpte_format(self):
        text = "00:00:10:00 Scene One\n"
        markers = parse_transcript_timestamps(text)
        assert len(markers) == 1
        assert markers[0]["seconds"] == 10

    def test_smpte_nonzero_frames(self):
        """SMPTE frames are converted to fractional seconds at default 24fps."""
        text = "00:00:10:12 Scene Two\n"
        markers = parse_transcript_timestamps(text)
        assert len(markers) == 1
        assert markers[0]["seconds"] == pytest.approx(10.5, abs=1e-6)

    def test_blank_lines_skipped(self):
        text = "0:00 Start\n\n\n0:30 Middle\n"
        markers = parse_transcript_timestamps(text)
        assert len(markers) == 2

    def test_no_timestamps(self):
        assert parse_transcript_timestamps("just plain text") == []


class TestParseTimestampParts:
    """Direct unit tests for _parse_timestamp_parts SMPTE accuracy."""

    def test_two_part(self):
        assert _parse_timestamp_parts(["1", "30"]) == 90.0

    def test_three_part(self):
        assert _parse_timestamp_parts(["1", "05", "30"]) == 3930.0

    def test_four_part_zero_frames(self):
        assert _parse_timestamp_parts(["0", "0", "10", "00"]) == 10.0

    def test_four_part_nonzero_frames_24fps(self):
        result = _parse_timestamp_parts(["0", "0", "10", "12"])
        assert result == pytest.approx(10.5, abs=1e-6)

    def test_four_part_custom_frame_rate(self):
        result = _parse_timestamp_parts(["0", "0", "5", "15"], frame_rate=30.0)
        assert result == pytest.approx(5.5, abs=1e-6)

    def test_four_part_frame_rate_25fps(self):
        result = _parse_timestamp_parts(["0", "0", "1", "5"], frame_rate=25.0)
        assert result == pytest.approx(1.2, abs=1e-6)

    def test_unrecognised_part_count(self):
        assert _parse_timestamp_parts(["10"]) is None
        assert _parse_timestamp_parts(["1", "2", "3", "4", "5"]) is None


# ============================================================
# Read Handlers (against sample.fcpxml)
# ============================================================


class TestHandleListProjects:
    async def test_finds_sample(self):
        example_dir = str(Path(SAMPLE).parent)
        result = await handle_list_projects({"directory": example_dir})
        assert len(result) == 1
        assert "sample.fcpxml" in result[0].text

    async def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = await handle_list_projects({"directory": d})
            assert "No FCPXML files" in result[0].text


class TestHandleAnalyzeTimeline:
    async def test_returns_markdown(self):
        result = await handle_analyze_timeline({"filepath": SAMPLE})
        text = result[0].text
        assert "# Timeline Analysis" in text
        assert "Music Video Edit" in text
        assert "1920" in text
        assert "**Clips**: 9 clips + 0 connected" in text
        assert "**Edit points**" in text


class TestHandleListClips:
    async def test_lists_all_clips(self):
        result = await handle_list_clips({"filepath": SAMPLE})
        text = result[0].text
        assert "Interview_A" in text
        assert "Broll_City" in text

    async def test_with_limit(self):
        result = await handle_list_clips({"filepath": SAMPLE, "limit": 2})
        text = result[0].text
        # Only 2 data rows should appear (not all 9)
        data_rows = [line for line in text.split("\n") if line.startswith("| ") and "---" not in line and "#" not in line]
        assert len(data_rows) == 2


class TestHandleListMarkers:
    async def test_default_format(self):
        result = await handle_list_markers({"filepath": SAMPLE})
        text = result[0].text
        assert "Markers" in text
        assert "Good take" in text

    async def test_youtube_format(self):
        result = await handle_list_markers({"filepath": SAMPLE, "format": "youtube"})
        text = result[0].text
        assert "YouTube Chapters" in text

    async def test_simple_format(self):
        result = await handle_list_markers({"filepath": SAMPLE, "format": "simple"})
        text = result[0].text
        assert " - " in text

    async def test_filter_chapter_markers(self):
        result = await handle_list_markers({"filepath": SAMPLE, "marker_type": "chapter"})
        text = result[0].text
        assert "Intro" in text
        assert "Good take" not in text


class TestHandleFindShortCuts:
    async def test_finds_flash_frame(self):
        # Sample has a 6/24s = 0.25s clip (Broll_Studio)
        result = await handle_find_short_cuts({"filepath": SAMPLE, "threshold_seconds": 0.5})
        text = result[0].text
        assert "Short Clips" in text
        assert "Broll_Studio" in text

    async def test_no_short_clips(self):
        result = await handle_find_short_cuts({"filepath": SAMPLE, "threshold_seconds": 0.01})
        text = result[0].text
        assert "No clips shorter than" in text


class TestHandleFindLongClips:
    async def test_finds_long_clip(self):
        # Longest clip is Interview_A at 720/24s = 30s
        result = await handle_find_long_clips({"filepath": SAMPLE, "threshold_seconds": 20})
        text = result[0].text
        assert "Long Clips" in text
        assert "Interview_A" in text

    async def test_no_long_clips(self):
        result = await handle_find_long_clips({"filepath": SAMPLE, "threshold_seconds": 999})
        text = result[0].text
        assert "No clips longer than" in text


class TestHandleListKeywords:
    async def test_finds_keywords(self):
        result = await handle_list_keywords({"filepath": SAMPLE})
        text = result[0].text
        assert "Interview" in text
        assert "B-Roll" in text

    async def test_counts(self):
        result = await handle_list_keywords({"filepath": SAMPLE})
        text = result[0].text
        assert "clips)" in text


class TestHandleExportEdl:
    async def test_edl_format(self):
        result = await handle_export_edl({"filepath": SAMPLE})
        text = result[0].text
        assert "TITLE:" in text
        assert "FROM CLIP NAME:" in text
        assert "```edl" in text


class TestHandleExportCsv:
    async def test_csv_format(self):
        result = await handle_export_csv({"filepath": SAMPLE})
        text = result[0].text
        assert "Name,Start,End,Duration,Keywords" in text
        assert "Interview_A" in text
        assert "```csv" in text


class TestHandleAnalyzePacing:
    async def test_pacing_analysis(self):
        result = await handle_analyze_pacing({"filepath": SAMPLE})
        text = result[0].text
        assert "Pacing Analysis" in text
        assert "Cuts/Min" in text
        assert "Q1" in text


# ============================================================
# QC Handlers
# ============================================================


class TestHandleDetectFlashFrames:
    async def test_detects_flash(self):
        # Sample has 6/24s = 0.25s clip = 6 frames at 24fps
        # Default warning_threshold_frames=6, so 6 < 6 is false
        # Use threshold=7 to catch it
        result = await handle_detect_flash_frames({
            "filepath": SAMPLE, "warning_threshold_frames": 7
        })
        text = result[0].text
        assert "Flash Frame" in text
        assert "Broll_Studio" in text

    async def test_no_flash_default(self):
        # With default thresholds, the 6-frame clip is exactly at the boundary
        result = await handle_detect_flash_frames({"filepath": SAMPLE})
        # At default (warning=6), clip with exactly 6 frames: 6 < 6 = False → no detection
        text = result[0].text
        assert "No flash frames detected" in text


class TestHandleDetectDuplicates:
    async def test_same_source_mode(self):
        result = await handle_detect_duplicates({
            "filepath": SAMPLE, "mode": "same_source"
        })
        text = result[0].text
        # Interview_A used 4 times, Broll_City 3 times, Broll_Studio 2 times
        assert "Duplicate" in text

    async def test_no_identical(self):
        result = await handle_detect_duplicates({
            "filepath": SAMPLE, "mode": "identical"
        })
        text = result[0].text
        # No two clips use exact same source range
        assert "No duplicate clips" in text


class TestHandleDetectGaps:
    async def test_no_gaps_in_sample(self):
        result = await handle_detect_gaps({"filepath": SAMPLE})
        text = result[0].text
        # Clips in sample are contiguous (offsets line up)
        assert "No gaps detected" in text


class TestHandleValidateTimeline:
    async def test_returns_health_score(self):
        result = await handle_validate_timeline({"filepath": SAMPLE})
        text = result[0].text
        assert "Health Score" in text
        assert "%" in text

    async def test_selective_checks(self):
        result = await handle_validate_timeline({
            "filepath": SAMPLE, "checks": ["flash_frames"]
        })
        text = result[0].text
        assert "Flash Frames" in text

    async def test_no_issues_passes(self):
        result = await handle_validate_timeline({
            "filepath": SAMPLE, "checks": ["gaps"]
        })
        text = result[0].text
        assert "PASS" in text


# ============================================================
# Transcript Import Handler
# ============================================================


class TestHandleImportTranscriptMarkers:
    async def test_inline_transcript(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            # Copy source into temp dir so output stays co-located (sandbox).
            src_copy = str(Path(d, "sample.fcpxml"))
            shutil.copy2(SAMPLE, src_copy)
            out = str(Path(d, "out.fcpxml"))
            result = await handle_import_transcript_markers({
                "filepath": src_copy,
                "output_path": out,
                "transcript": "0:10 Marker A\n0:47 Marker B",
            })
            text = result[0].text
            assert "Markers Added" in text
            assert Path(out).exists()

    async def test_no_transcript_provided(self):
        result = await handle_import_transcript_markers({
            "filepath": SAMPLE,
        })
        text = result[0].text
        assert "Provide either" in text

    async def test_missing_transcript_file(self):
        result = await call_tool("import_transcript_markers", {
            "filepath": SAMPLE,
            "transcript_path": "/nonexistent/path.txt",
        })
        text = result[0].text
        assert "File not found" in text


# ============================================================
# Tool Dispatch (call_tool)
# ============================================================


class TestCallTool:
    async def test_unknown_tool(self):
        result = await call_tool("nonexistent_tool", {})
        assert "Unknown tool" in result[0].text

    async def test_file_not_found_handled(self):
        result = await call_tool("analyze_timeline", {"filepath": "/no/such/file.fcpxml"})
        assert "File not found" in result[0].text or "Error" in result[0].text

    async def test_dispatches_correctly(self):
        example_dir = str(Path(SAMPLE).parent)
        result = await call_tool("list_projects", {"directory": example_dir})
        assert "sample.fcpxml" in result[0].text
