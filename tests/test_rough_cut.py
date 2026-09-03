"""Tests for RoughCutGenerator — the flagship rough cut generation feature.

Covers: __init__, _detect_fps, _index_clips, _extract_clip_data, _index_resources,
        generate, _parse_duration, _filter_clips, _select_clips_simple,
        _select_clips_by_segments, _build_ab_sequence, _build_output,
        generate_rough_cut, generate_segmented_rough_cut convenience functions.
"""

import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fcpxml.models import PacingConfig, RoughCutResult, SegmentSpec, TimeValue
from fcpxml.rough_cut import (
    RoughCutGenerator,
    generate_rough_cut,
    generate_segmented_rough_cut,
)

SAMPLE = Path(__file__).parent.parent / "examples" / "sample.fcpxml"


@pytest.fixture
def temp_fcpxml():
    """Create a temp copy of sample.fcpxml for tests."""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", delete=False) as f:
        shutil.copy(SAMPLE, f.name)
        yield f.name
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def temp_output():
    """Create a temp file path for output."""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", delete=False) as f:
        yield f.name
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def generator(temp_fcpxml):
    """Pre-loaded RoughCutGenerator from sample."""
    return RoughCutGenerator(temp_fcpxml)


# ============================================================
# Initialization & Indexing
# ============================================================


def test_detect_fps(generator):
    """Should detect 24fps from sample.fcpxml's frameDuration='1/24s'."""
    assert generator.fps == 24.0


def test_detect_fps_default():
    """Should default to 30fps when format has no frameDuration."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" name="test"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="C" duration="10s"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        assert gen.fps == 30.0
    Path(f.name).unlink(missing_ok=True)


def test_index_clips_count(generator):
    """Sample has 9 asset-clips in spine — all should be indexed."""
    assert len(generator.clips) == 9


def test_index_clips_extracts_names(generator):
    """Clip names should match the FCPXML source."""
    names = [c["name"] for c in generator.clips]
    assert "Interview_A" in names
    assert "Broll_City" in names
    assert "Broll_Studio" in names


def test_index_clips_extracts_keywords(generator):
    """Keywords should be extracted from <keyword> children."""
    interview_clips = [c for c in generator.clips if "Interview" in c["keywords"]]
    broll_clips = [c for c in generator.clips if "B-Roll" in c["keywords"]]
    assert len(interview_clips) == 3
    assert len(broll_clips) == 2


def test_index_clips_skips_very_short():
    """Clips shorter than 0.1s should be skipped by _extract_clip_data."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" frameDuration="1/24s"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="Tiny" duration="1/24s"/>
                <asset-clip ref="r1" name="Normal" duration="48/24s"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        names = [c["name"] for c in gen.clips]
        assert "Normal" in names
        # 1/24s = 0.0417s < 0.1s threshold, should be skipped
        assert "Tiny" not in names
    Path(f.name).unlink(missing_ok=True)


def test_index_resources(generator):
    """Should index assets r2, r3, r4 from sample."""
    assert "r2" in generator.resources
    assert "r3" in generator.resources
    assert "r4" in generator.resources


def test_index_formats(generator):
    """Should index format r1 from sample."""
    assert "r1" in generator.formats


def test_extract_clip_data_favorites():
    """Should detect favorited and rejected clips."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" frameDuration="1/24s"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="Fav" duration="48/24s" rating="1"/>
                <asset-clip ref="r1" name="Rej" duration="48/24s" rating="-1"/>
                <asset-clip ref="r1" name="Neutral" duration="48/24s"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        by_name = {c["name"]: c for c in gen.clips}
        assert by_name["Fav"]["is_favorite"] is True
        assert by_name["Fav"]["is_rejected"] is False
        assert by_name["Rej"]["is_rejected"] is True
        assert by_name["Neutral"]["is_favorite"] is False
        assert by_name["Neutral"]["is_rejected"] is False
    Path(f.name).unlink(missing_ok=True)


# ============================================================
# _parse_duration
# ============================================================


def test_parse_duration_shorthand_minutes(generator):
    """'3m' should parse to 180 seconds."""
    tv = generator._parse_duration("3m")
    assert abs(tv.to_seconds() - 180.0) < 0.1


def test_parse_duration_shorthand_minutes_seconds(generator):
    """'3m30s' should parse to 210 seconds."""
    tv = generator._parse_duration("3m30s")
    assert abs(tv.to_seconds() - 210.0) < 0.1


def test_parse_duration_shorthand_minutes_only_trailing_s(generator):
    """'2m' should parse to 120 seconds (no trailing 's' after empty seconds)."""
    tv = generator._parse_duration("2m")
    assert abs(tv.to_seconds() - 120.0) < 0.1


def test_parse_duration_timecode(generator):
    """'00:00:10:00' timecode should parse to 10 seconds at 24fps."""
    tv = generator._parse_duration("00:00:10:00")
    assert abs(tv.to_seconds() - 10.0) < 0.1


def test_parse_duration_rational(generator):
    """'240/24s' should parse to 10 seconds."""
    tv = generator._parse_duration("240/24s")
    assert abs(tv.to_seconds() - 10.0) < 0.1


def test_parse_duration_fractional_seconds(generator):
    """'1m30.5s' should parse to 90.5 seconds, not crash."""
    tv = generator._parse_duration("1m30.5s")
    assert abs(tv.to_seconds() - 90.5) < 0.5


# ============================================================
# _filter_clips
# ============================================================


def test_filter_clips_no_filters(generator):
    """No filters should return all non-rejected clips (sample has none rejected)."""
    result = generator._filter_clips()
    assert len(result) == 9


def test_filter_clips_by_keyword(generator):
    """Filtering by 'Interview' keyword should return 3 clips."""
    result = generator._filter_clips(keywords=["Interview"])
    assert len(result) == 3
    for clip in result:
        assert "Interview" in clip["keywords"]


def test_filter_clips_keyword_case_insensitive(generator):
    """Keyword filtering should be case-insensitive."""
    result = generator._filter_clips(keywords=["interview"])
    assert len(result) == 3


def test_filter_clips_exclude_rejected():
    """Should exclude rejected clips when exclude_rejected=True."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" frameDuration="1/24s"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="Good" duration="48/24s"/>
                <asset-clip ref="r1" name="Bad" duration="48/24s" rating="-1"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        result = gen._filter_clips(exclude_rejected=True)
        names = [c["name"] for c in result]
        assert "Good" in names
        assert "Bad" not in names
    Path(f.name).unlink(missing_ok=True)


def test_filter_clips_include_rejected():
    """Should include rejected clips when exclude_rejected=False."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" frameDuration="1/24s"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="Good" duration="48/24s"/>
                <asset-clip ref="r1" name="Bad" duration="48/24s" rating="-1"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        result = gen._filter_clips(exclude_rejected=False)
        names = [c["name"] for c in result]
        assert "Good" in names
        assert "Bad" in names
    Path(f.name).unlink(missing_ok=True)


def test_filter_clips_favorites_only():
    """Should return only favorited clips when favorites_only=True."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" frameDuration="1/24s"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="Fav" duration="48/24s" rating="1"/>
                <asset-clip ref="r1" name="Neutral" duration="48/24s"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        result = gen._filter_clips(favorites_only=True)
        assert len(result) == 1
        assert result[0]["name"] == "Fav"
    Path(f.name).unlink(missing_ok=True)


# ============================================================
# _select_clips_simple — priority modes
# ============================================================


def test_select_clips_simple_respects_target(generator):
    """Should stop selecting when target duration is reached."""
    clips = generator._filter_clips()
    target = TimeValue.from_seconds(5.0, generator.fps)
    pacing = PacingConfig(pacing="medium")
    selected = generator._select_clips_simple(clips, target, pacing, "best")
    total = sum(c["use_duration"].to_seconds() for c in selected)
    assert total <= 6.0  # Should be close to 5s target


def test_select_clips_simple_priority_longest(generator):
    """Priority 'longest' should select longest clips first."""
    clips = generator._filter_clips()
    target = TimeValue.from_seconds(60.0, generator.fps)
    pacing = PacingConfig(pacing="slow")
    selected = generator._select_clips_simple(clips, target, pacing, "longest")
    # First selected clip should be the longest available
    assert len(selected) > 0
    durations = [c["use_duration"].to_seconds() for c in selected]
    # First clip duration should be >= second (sorted descending by source duration)
    if len(durations) > 1:
        assert durations[0] >= durations[1] - 0.1  # tolerance for pacing clamp


def test_select_clips_simple_priority_shortest(generator):
    """Priority 'shortest' should select shortest clips first."""
    clips = generator._filter_clips()
    target = TimeValue.from_seconds(60.0, generator.fps)
    pacing = PacingConfig(pacing="fast")
    selected = generator._select_clips_simple(clips, target, pacing, "shortest")
    assert len(selected) > 0


def test_select_clips_simple_adds_in_out_points(generator):
    """Selected clips should have in_point and out_point set."""
    clips = generator._filter_clips()
    target = TimeValue.from_seconds(10.0, generator.fps)
    pacing = PacingConfig(pacing="medium")
    selected = generator._select_clips_simple(clips, target, pacing, "best")
    for clip in selected:
        assert "in_point" in clip
        assert "out_point" in clip
        assert "use_duration" in clip
        assert isinstance(clip["in_point"], TimeValue)
        assert isinstance(clip["out_point"], TimeValue)


# ============================================================
# _select_clips_by_segments
# ============================================================


def test_select_clips_by_segments_uses_keywords(generator):
    """Segments with keywords should only select matching clips."""
    clips = generator._filter_clips()
    segments = [
        SegmentSpec(name="Interviews", keywords=["Interview"], duration_seconds=10),
        SegmentSpec(name="B-Roll", keywords=["B-Roll"], duration_seconds=5),
    ]
    target = TimeValue.from_seconds(15.0, generator.fps)
    pacing = PacingConfig(pacing="medium")

    selected = generator._select_clips_by_segments(clips, segments, target, pacing)

    # Check segment labels were applied
    interview_clips = [c for c in selected if c.get("segment") == "Interviews"]
    broll_clips = [c for c in selected if c.get("segment") == "B-Roll"]
    assert len(interview_clips) > 0
    assert len(broll_clips) > 0


def test_select_clips_by_segments_distributes_unspecified_duration(generator):
    """Segments without explicit duration should share remaining time equally."""
    clips = generator._filter_clips()
    # Total target = 20s, first segment = 10s, second should get ~10s
    segments = [
        SegmentSpec(name="Fixed", keywords=["Interview"], duration_seconds=10),
        SegmentSpec(name="Auto", keywords=["B-Roll"], duration_seconds=0),
    ]
    target = TimeValue.from_seconds(20.0, generator.fps)
    pacing = PacingConfig(pacing="medium")

    selected = generator._select_clips_by_segments(clips, segments, target, pacing)
    auto_clips = [c for c in selected if c.get("segment") == "Auto"]
    assert len(auto_clips) > 0


def test_select_clips_by_segments_overcommitted_clamps_to_zero(generator):
    """When specified segments exceed target, unspecified segments get 0 not negative."""
    clips = generator._filter_clips()
    # Target=10s but specified segments total 30s — unspecified should get 0, not -20
    segments = [
        SegmentSpec(name="Big1", keywords=None, duration_seconds=15),
        SegmentSpec(name="Big2", keywords=None, duration_seconds=15),
        SegmentSpec(name="Leftover", keywords=None, duration_seconds=0),
    ]
    target = TimeValue.from_seconds(10.0, generator.fps)
    pacing = PacingConfig(pacing="medium")

    # Should not crash and leftover segment should produce no negative durations
    selected = generator._select_clips_by_segments(clips, segments, target, pacing)
    leftover_clips = [c for c in selected if c.get("segment") == "Leftover"]
    for c in leftover_clips:
        assert c.get('duration_seconds', 0) >= 0


def test_select_clips_by_segments_no_reuse_across_segments(generator):
    """Clips used in one segment must not appear in subsequent segments."""
    clips = generator._filter_clips()
    # Two segments, both want all clips — second should get different ones
    segments = [
        SegmentSpec(name="Seg1", keywords=None, duration_seconds=5),
        SegmentSpec(name="Seg2", keywords=None, duration_seconds=5),
    ]
    target = TimeValue.from_seconds(10.0, generator.fps)
    pacing = PacingConfig(pacing="medium")

    selected = generator._select_clips_by_segments(clips, segments, target, pacing)
    seg1_names = [c['name'] for c in selected if c.get('segment') == 'Seg1']
    seg2_names = [c['name'] for c in selected if c.get('segment') == 'Seg2']
    # No clip name should appear in both segments
    overlap = set(seg1_names) & set(seg2_names)
    assert len(overlap) == 0, f"Clips reused across segments: {overlap}"


# ============================================================
# generate() — full pipeline
# ============================================================


def test_generate_creates_file(generator, temp_output):
    """generate() should create a valid FCPXML output file."""
    result = generator.generate(output_path=temp_output, target_duration="10s")
    assert Path(temp_output).exists()
    assert isinstance(result, RoughCutResult)
    assert result.clips_used > 0


def test_generate_returns_correct_result(generator, temp_output):
    """RoughCutResult should have all expected fields populated."""
    result = generator.generate(
        output_path=temp_output, target_duration="15s", pacing="fast"
    )
    assert result.output_path == temp_output
    assert result.clips_available > 0
    assert result.clips_used > 0
    assert result.target_duration > 0
    assert result.actual_duration > 0
    assert result.segments == 1  # No segments specified
    assert result.average_clip_duration > 0


def test_generate_with_keyword_filter(generator, temp_output):
    """generate() with keywords should only use matching clips."""
    result = generator.generate(
        output_path=temp_output,
        target_duration="10s",
        keywords=["Interview"],
    )
    assert result.clips_available == 3  # Only Interview-tagged clips


def test_generate_no_matching_clips_raises(generator, temp_output):
    """generate() should raise ValueError when no clips match filters."""
    with pytest.raises(ValueError, match="No clips match"):
        generator.generate(
            output_path=temp_output,
            target_duration="10s",
            keywords=["nonexistent_keyword"],
        )


def test_generate_with_segments(generator, temp_output):
    """generate() with segments should report correct segment count."""
    segments = [
        SegmentSpec(name="Intro", keywords=["Interview"], duration_seconds=5),
        SegmentSpec(name="Outro", keywords=["B-Roll"], duration_seconds=5),
    ]
    result = generator.generate(
        output_path=temp_output,
        target_duration="10s",
        segments=segments,
    )
    assert result.segments == 2
    assert result.clips_used > 0


def test_generate_output_is_valid_xml(generator, temp_output):
    """Output FCPXML should be parseable XML with correct structure."""
    generator.generate(output_path=temp_output, target_duration="10s")
    tree = ET.parse(temp_output)
    root = tree.getroot()
    assert root.tag == "fcpxml"
    assert root.find(".//spine") is not None
    assert len(root.findall(".//asset-clip")) > 0


def test_generate_favorites_only(temp_output):
    """favorites_only=True with no favorites should raise ValueError."""
    xml = """<?xml version="1.0"?>
    <fcpxml version="1.11">
        <resources><format id="r1" frameDuration="1/24s"/></resources>
        <library><event name="E"><project name="P">
            <sequence format="r1" duration="100s"><spine>
                <asset-clip ref="r1" name="C1" duration="48/24s"/>
                <asset-clip ref="r1" name="C2" duration="48/24s"/>
            </spine></sequence>
        </project></event></library>
    </fcpxml>"""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", mode="w", delete=False) as f:
        f.write(xml)
        f.flush()
        gen = RoughCutGenerator(f.name)
        with pytest.raises(ValueError, match="No clips match"):
            gen.generate(
                output_path=temp_output,
                target_duration="5s",
                favorites_only=True,
            )
    Path(f.name).unlink(missing_ok=True)


# ============================================================
# _build_output
# ============================================================


def test_build_output_xml_structure(generator, temp_output):
    """Output should have fcpxml > resources, library > event > project > sequence > spine."""
    generator.generate(output_path=temp_output, target_duration="10s")
    tree = ET.parse(temp_output)
    root = tree.getroot()
    assert root.find("resources") is not None
    assert root.find("library") is not None
    assert root.find(".//event") is not None
    assert root.find(".//project") is not None
    assert root.find(".//sequence") is not None


def test_build_output_copies_resources(generator, temp_output):
    """Output should contain format and asset resources for used clips."""
    generator.generate(
        output_path=temp_output,
        target_duration="10s",
        keywords=["Interview"],
    )
    tree = ET.parse(temp_output)
    root = tree.getroot()
    resources = root.find("resources")
    # Should have at least a format element
    assert resources.find("format") is not None


def test_build_output_doctype(generator, temp_output):
    """Output should start with XML declaration and DOCTYPE."""
    generator.generate(output_path=temp_output, target_duration="10s")
    content = Path(temp_output).read_text()
    assert '<?xml version="1.0" encoding="UTF-8"?>' in content
    assert "<!DOCTYPE fcpxml>" in content


# ============================================================
# _build_ab_sequence
# ============================================================


def test_build_ab_sequence_alternates(generator):
    """A/B sequence should alternate between roll types."""
    a_clips = generator._filter_clips(keywords=["Interview"])
    b_clips = generator._filter_clips(keywords=["B-Roll"])
    target = TimeValue.from_seconds(20.0, generator.fps)
    a_dur = TimeValue.from_seconds(5.0, generator.fps)
    b_dur = TimeValue.from_seconds(3.0, generator.fps)

    selected = generator._build_ab_sequence(
        a_clips, b_clips, target, a_dur, b_dur, "a"
    )

    # First clip should be A-roll, second should be B-roll
    assert selected[0]["roll_type"] == "A"
    assert selected[1]["roll_type"] == "B"


def test_build_ab_sequence_starts_with_b(generator):
    """start_with='b' should begin with B-roll."""
    a_clips = generator._filter_clips(keywords=["Interview"])
    b_clips = generator._filter_clips(keywords=["B-Roll"])
    target = TimeValue.from_seconds(10.0, generator.fps)
    a_dur = TimeValue.from_seconds(3.0, generator.fps)
    b_dur = TimeValue.from_seconds(3.0, generator.fps)

    selected = generator._build_ab_sequence(
        a_clips, b_clips, target, a_dur, b_dur, "b"
    )

    assert selected[0]["roll_type"] == "B"


def test_build_ab_sequence_loops_clips(generator):
    """Should loop clips when pool is exhausted before target duration."""
    a_clips = generator._filter_clips(keywords=["Interview"])  # 3 clips
    b_clips = generator._filter_clips(keywords=["B-Roll"])  # 2 clips
    # Set a long target to force looping
    target = TimeValue.from_seconds(120.0, generator.fps)
    a_dur = TimeValue.from_seconds(3.0, generator.fps)
    b_dur = TimeValue.from_seconds(3.0, generator.fps)

    selected = generator._build_ab_sequence(
        a_clips, b_clips, target, a_dur, b_dur, "a"
    )

    # Should have more clips than available in either pool (looped)
    a_count = sum(1 for c in selected if c["roll_type"] == "A")
    b_count = sum(1 for c in selected if c["roll_type"] == "B")
    assert a_count > len(a_clips) or b_count > len(b_clips)


# ============================================================
# generate_ab_roll — error cases
# ============================================================


def test_generate_ab_roll_no_a_clips_raises(generator, temp_output):
    """Should raise ValueError when no A-roll clips match keywords."""
    with pytest.raises(ValueError, match="No A-roll clips found"):
        generator.generate_ab_roll(
            output_path=temp_output,
            target_duration="10s",
            a_keywords=["nonexistent"],
            b_keywords=["B-Roll"],
        )


def test_generate_ab_roll_no_b_clips_raises(generator, temp_output):
    """Should raise ValueError when no B-roll clips match keywords."""
    with pytest.raises(ValueError, match="No B-roll clips found"):
        generator.generate_ab_roll(
            output_path=temp_output,
            target_duration="10s",
            a_keywords=["Interview"],
            b_keywords=["nonexistent"],
        )


# ============================================================
# Convenience functions
# ============================================================


def test_generate_rough_cut_convenience(temp_fcpxml, temp_output):
    """generate_rough_cut() convenience function should produce output."""
    result = generate_rough_cut(
        source_fcpxml=temp_fcpxml,
        output_path=temp_output,
        target_duration="10s",
        pacing="fast",
    )
    assert isinstance(result, RoughCutResult)
    assert Path(temp_output).exists()
    assert result.clips_used > 0


def test_generate_rough_cut_with_keywords(temp_fcpxml, temp_output):
    """Convenience function should pass keyword filter through."""
    result = generate_rough_cut(
        source_fcpxml=temp_fcpxml,
        output_path=temp_output,
        target_duration="10s",
        keywords=["Interview"],
    )
    assert result.clips_available == 3


def test_generate_segmented_rough_cut_convenience(temp_fcpxml, temp_output):
    """generate_segmented_rough_cut() should create segmented output."""
    result = generate_segmented_rough_cut(
        source_fcpxml=temp_fcpxml,
        output_path=temp_output,
        segments=[
            {"name": "Intro", "keywords": ["Interview"], "duration": 5},
            {"name": "Cutaway", "keywords": ["B-Roll"], "duration": 5},
        ],
        pacing="medium",
    )
    assert isinstance(result, RoughCutResult)
    assert result.segments == 2
    assert Path(temp_output).exists()


# ============================================================
# diversity constraint (v0.19.0)
# ============================================================


def test_generate_min_source_separation_filters_repeats(temp_output, monkeypatch):
    """Selection that reuses one source back-to-back is thinned; the score reports it."""
    gen = RoughCutGenerator("examples/music-video.fcpxml")
    pool = gen._filter_clips(keywords=None, exclude_rejected=True, favorites_only=False)
    a, b = (
        {**c, "use_duration": TimeValue.from_seconds(2, gen.fps), "in_point": c["start"],
         "out_point": c["start"] + TimeValue.from_seconds(2, gen.fps)}
        for c in pool[:2]
    )
    monkeypatch.setattr(gen, "_select_clips_simple", lambda *args, **kw: [a, a, b, a])

    loose = gen.generate(output_path=temp_output, target_duration="60s")
    assert loose.clips_used == 4 and loose.diversity_score == pytest.approx(2 / 3)

    tight = gen.generate(output_path=temp_output, target_duration="60s", min_source_separation=1)
    assert tight.clips_used == 3 and tight.diversity_score == 1.0
    refs = [c.get("ref") for c in ET.parse(temp_output).getroot().iter("asset-clip")]
    assert refs == [a["ref"], b["ref"], a["ref"]]


async def test_auto_rough_cut_reports_diversity(tmp_path):
    import shutil

    import server

    src = tmp_path / "mv.fcpxml"
    shutil.copy("examples/music-video.fcpxml", src)
    text = (await server.handle_auto_rough_cut({
        "filepath": str(src), "output_path": str(tmp_path / "cut.fcpxml"),
        "target_duration": "30s", "min_source_separation": 1,
    }))[0].text
    assert "Diversity: " in text
