"""Tests for FCPXMLWriter — FCPXML generation from Python objects."""

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fcpxml.models import (
    Clip,
    Keyword,
    Marker,
    MarkerType,
    Project,
    Timecode,
    Timeline,
)
from fcpxml.writer import FCPXMLWriter


@pytest.fixture
def fps():
    return 24.0


@pytest.fixture
def writer():
    return FCPXMLWriter(version="1.11")


@pytest.fixture
def basic_clip(fps):
    """A single clip at 0 frames, 48 frames (2s) duration."""
    return Clip(
        name="Interview_A",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/interview_a.mov",
    )


@pytest.fixture
def basic_timeline(fps, basic_clip):
    """Timeline with one clip, 2 seconds long."""
    return Timeline(
        name="Main Timeline",
        duration=Timecode(frames=48, frame_rate=fps),
        frame_rate=fps,
        width=1920,
        height=1080,
        clips=[basic_clip],
    )


@pytest.fixture
def basic_project(basic_timeline):
    return Project(
        name="Test Project",
        timelines=[basic_timeline],
        fcpxml_version="1.11",
    )


# ============================================================
# Resource ID counter
# ============================================================


def test_next_resource_id_increments(writer):
    """Resource IDs should increment: r1, r2, r3, …"""
    assert writer._next_resource_id() == "r1"
    assert writer._next_resource_id() == "r2"
    assert writer._next_resource_id() == "r3"


def test_generate_uid_unique(writer):
    """Each UID should be unique."""
    uids = {writer._generate_uid() for _ in range(50)}
    assert len(uids) == 50


def test_generate_uid_uppercase(writer):
    """UIDs should be uppercase hex."""
    uid = writer._generate_uid()
    assert uid == uid.upper()


# ============================================================
# Timecode → rational string
# ============================================================


def test_tc_to_rational_basic(writer, fps):
    """48 frames at 24fps → '48/24s'."""
    tc = Timecode(frames=48, frame_rate=fps)
    assert writer._tc_to_rational(tc) == "48/24s"


def test_tc_to_rational_zero(writer, fps):
    """0 frames → '0/24s'."""
    tc = Timecode(frames=0, frame_rate=fps)
    assert writer._tc_to_rational(tc) == "0/24s"


# ============================================================
# write_project — file output
# ============================================================


def test_write_project_creates_file(writer, basic_project):
    """write_project should create a file on disk."""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", delete=False) as f:
        path = f.name
    try:
        writer.write_project(basic_project, path)
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0
    finally:
        Path(path).unlink(missing_ok=True)


def test_write_project_xml_declaration(writer, basic_project):
    """Output should start with XML declaration and DOCTYPE."""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", delete=False) as f:
        path = f.name
    try:
        writer.write_project(basic_project, path)
        content = Path(path).read_text()
        assert content.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<!DOCTYPE fcpxml>" in content
    finally:
        Path(path).unlink(missing_ok=True)


def test_write_project_valid_xml(writer, basic_project):
    """Output should be parseable XML."""
    with tempfile.NamedTemporaryFile(suffix=".fcpxml", delete=False) as f:
        path = f.name
    try:
        writer.write_project(basic_project, path)
        tree = ET.parse(path)
        root = tree.getroot()
        assert root.tag == "fcpxml"
    finally:
        Path(path).unlink(missing_ok=True)


# ============================================================
# _build_fcpxml — structure tests
# ============================================================


def test_build_fcpxml_root_version(writer, basic_project):
    """Root element should have the correct version."""
    root = writer._build_fcpxml(basic_project)
    assert root.get("version") == "1.11"


def test_build_fcpxml_has_resources(writer, basic_project):
    """FCPXML should contain a <resources> element."""
    root = writer._build_fcpxml(basic_project)
    resources = root.find("resources")
    assert resources is not None


def test_build_fcpxml_has_format_resource(writer, basic_project):
    """Resources should include a format element matching timeline specs."""
    root = writer._build_fcpxml(basic_project)
    fmt = root.find(".//format")
    assert fmt is not None
    assert fmt.get("width") == "1920"
    assert fmt.get("height") == "1080"
    assert fmt.get("frameDuration") == "1/24s"


def test_build_fcpxml_has_library(writer, basic_project):
    """FCPXML should contain a <library> element, with NO fabricated location.

    Until v0.25.0 the writer stamped every generated file with
    file:///Users/editor/Movies/<name>.fcpbundle — a path that exists on
    nobody's machine — and this test asserted the fabricated value, so it
    locked the defect in place. The DTD marks `location` #IMPLIED. The real
    import target is set at push time via <import-options>.
    """
    root = writer._build_fcpxml(basic_project)
    library = root.find("library")
    assert library is not None
    assert library.get("location") is None
    assert "/Users/editor" not in ET.tostring(root, encoding="unicode")


def test_build_fcpxml_has_event(writer, basic_project):
    """Library should contain an <event> with the project name."""
    root = writer._build_fcpxml(basic_project)
    event = root.find(".//event")
    assert event is not None
    assert event.get("name") == "Test Project"
    assert event.get("uid") is not None


def test_build_fcpxml_has_project_element(writer, basic_project):
    """Event should contain a <project> element."""
    root = writer._build_fcpxml(basic_project)
    proj = root.find(".//project")
    assert proj is not None
    assert proj.get("name") == "Main Timeline"


def test_build_fcpxml_has_sequence(writer, basic_project):
    """Project should contain a <sequence> element."""
    root = writer._build_fcpxml(basic_project)
    seq = root.find(".//sequence")
    assert seq is not None
    assert seq.get("tcFormat") == "NDF"
    assert seq.get("audioLayout") == "stereo"
    assert seq.get("audioRate") == "48k"


def test_build_fcpxml_has_spine(writer, basic_project):
    """Sequence should contain a <spine> element."""
    root = writer._build_fcpxml(basic_project)
    spine = root.find(".//spine")
    assert spine is not None


# ============================================================
# Clip generation
# ============================================================


def test_clip_in_spine(writer, basic_project):
    """Spine should contain the clip as an <asset-clip>."""
    root = writer._build_fcpxml(basic_project)
    clips = root.findall(".//asset-clip")
    assert len(clips) == 1
    assert clips[0].get("name") == "Interview_A"


def test_clip_has_ref(writer, basic_project):
    """Asset-clip should reference an asset resource."""
    root = writer._build_fcpxml(basic_project)
    clip = root.find(".//asset-clip")
    ref = clip.get("ref")
    # Verify the referenced asset exists
    asset = root.find(f".//asset[@id='{ref}']")
    assert asset is not None
    assert asset.get("name") == "Interview_A"


def test_clip_duration(writer, basic_project):
    """Clip should have correct duration in rational format."""
    root = writer._build_fcpxml(basic_project)
    clip = root.find(".//asset-clip")
    assert clip.get("duration") == "48/24s"


def test_clip_offset(writer, basic_project):
    """First clip offset should be its start timecode in rational format."""
    root = writer._build_fcpxml(basic_project)
    clip = root.find(".//asset-clip")
    assert clip.get("offset") == "0/24s"


def test_asset_has_media_path(writer, basic_project):
    """Asset resource should include the source media path."""
    root = writer._build_fcpxml(basic_project)
    asset = root.find(".//asset")
    assert asset.get("src") == "file:///media/interview_a.mov"


def test_asset_has_video_audio_flags(writer, basic_project):
    """Asset should declare video and audio presence."""
    root = writer._build_fcpxml(basic_project)
    asset = root.find(".//asset")
    assert asset.get("hasVideo") == "1"
    assert asset.get("hasAudio") == "1"


# ============================================================
# Multiple clips / shared media
# ============================================================


def test_multiple_clips(writer, fps):
    """Multiple clips should all appear in the spine."""
    clips = [
        Clip(name=f"Clip_{i}", start=Timecode(frames=i * 48, frame_rate=fps),
             duration=Timecode(frames=48, frame_rate=fps),
             media_path=f"file:///media/clip_{i}.mov")
        for i in range(3)
    ]
    tl = Timeline(name="Multi", duration=Timecode(frames=144, frame_rate=fps),
                  frame_rate=fps, clips=clips)
    proj = Project(name="Multi", timelines=[tl])
    root = writer._build_fcpxml(proj)
    assert len(root.findall(".//asset-clip")) == 3
    assert len(root.findall(".//asset")) == 3


def test_shared_media_single_asset(writer, fps):
    """Two clips from the same media should share one asset resource."""
    shared_path = "file:///media/shared.mov"
    clips = [
        Clip(name="PartA", start=Timecode(frames=0, frame_rate=fps),
             duration=Timecode(frames=24, frame_rate=fps), media_path=shared_path),
        Clip(name="PartB", start=Timecode(frames=24, frame_rate=fps),
             duration=Timecode(frames=24, frame_rate=fps), media_path=shared_path),
    ]
    tl = Timeline(name="Shared", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=clips)
    proj = Project(name="Shared", timelines=[tl])
    root = writer._build_fcpxml(proj)
    assets = root.findall(".//asset")
    assert len(assets) == 1  # deduplicated
    asset_clips = root.findall(".//asset-clip")
    assert len(asset_clips) == 2
    # Both clips reference the same asset
    refs = {ac.get("ref") for ac in asset_clips}
    assert len(refs) == 1


# ============================================================
# Markers
# ============================================================


def test_clip_with_marker(writer, fps):
    """A clip marker should appear as a child of the asset-clip element."""
    clip = Clip(
        name="Marked",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/marked.mov",
        markers=[Marker(name="Hit", start=Timecode(frames=12, frame_rate=fps))],
    )
    tl = Timeline(name="M", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    proj = Project(name="M", timelines=[tl])
    root = writer._build_fcpxml(proj)
    marker = root.find(".//asset-clip/marker")
    assert marker is not None
    assert marker.get("value") == "Hit"
    assert marker.get("start") == "12/24s"


def test_chapter_marker(writer, fps):
    """Chapter markers should use <chapter-marker> tag."""
    clip = Clip(
        name="Chaptered",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/ch.mov",
        markers=[Marker(
            name="Chapter 1", start=Timecode(frames=0, frame_rate=fps),
            marker_type=MarkerType.CHAPTER,
        )],
    )
    tl = Timeline(name="C", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    proj = Project(name="C", timelines=[tl])
    root = writer._build_fcpxml(proj)
    ch = root.find(".//chapter-marker")
    assert ch is not None
    assert ch.get("value") == "Chapter 1"


def test_marker_with_note(writer, fps):
    """Standard markers should include the note attribute when set."""
    clip = Clip(
        name="Noted",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/noted.mov",
        markers=[Marker(
            name="Review", start=Timecode(frames=6, frame_rate=fps),
            note="Fix audio",
        )],
    )
    tl = Timeline(name="N", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    proj = Project(name="N", timelines=[tl])
    root = writer._build_fcpxml(proj)
    marker = root.find(".//marker")
    assert marker.get("note") == "Fix audio"


def test_chapter_marker_no_note(writer, fps):
    """Chapter markers should NOT include a note attribute even if set."""
    clip = Clip(
        name="ChNoted",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/chnoted.mov",
        markers=[Marker(
            name="Ch", start=Timecode(frames=0, frame_rate=fps),
            marker_type=MarkerType.CHAPTER, note="ignored",
        )],
    )
    tl = Timeline(name="CN", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    proj = Project(name="CN", timelines=[tl])
    root = writer._build_fcpxml(proj)
    ch = root.find(".//chapter-marker")
    # chapter-marker check: the code only writes note for non-chapter tags
    assert ch.get("note") is None


def test_todo_marker_completed_attr(writer, fps):
    """INCOMPLETE markers must emit completed='0' — the only way FCP recognises them as open tasks."""
    clip = Clip(
        name="Todo", start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/todo.mov",
        markers=[Marker(
            name="Fix color", start=Timecode(frames=6, frame_rate=fps),
            marker_type=MarkerType.INCOMPLETE,
        )],
    )
    tl = Timeline(name="T", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    root = writer._build_fcpxml(Project(name="T", timelines=[tl]))
    marker = root.find(".//asset-clip/marker")
    assert marker is not None
    assert marker.get("completed") == "0", "INCOMPLETE marker must have completed='0'"
    assert marker.get("value") == "Fix color"


def test_completed_marker_completed_attr(writer, fps):
    """COMPLETED markers must emit completed='1' to signal done state in FCP."""
    clip = Clip(
        name="Done", start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/done.mov",
        markers=[Marker(
            name="Approved", start=Timecode(frames=6, frame_rate=fps),
            marker_type=MarkerType.COMPLETED,
        )],
    )
    tl = Timeline(name="D", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    root = writer._build_fcpxml(Project(name="D", timelines=[tl]))
    marker = root.find(".//asset-clip/marker")
    assert marker is not None
    assert marker.get("completed") == "1", "COMPLETED marker must have completed='1'"


def test_standard_marker_no_completed_attr(writer, fps):
    """STANDARD markers must NOT emit a completed attribute — it would confuse FCP."""
    clip = Clip(
        name="Std", start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/std.mov",
        markers=[Marker(
            name="Note", start=Timecode(frames=6, frame_rate=fps),
            marker_type=MarkerType.STANDARD,
        )],
    )
    tl = Timeline(name="S", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    root = writer._build_fcpxml(Project(name="S", timelines=[tl]))
    marker = root.find(".//asset-clip/marker")
    assert marker is not None
    assert marker.get("completed") is None, "STANDARD marker must not have completed attr"


def test_all_marker_types_on_one_clip(writer, fps):
    """All four marker types on one clip — each gets correct tag and attributes."""
    markers = [
        Marker(name="Std", start=Timecode(frames=0, frame_rate=fps),
               marker_type=MarkerType.STANDARD),
        Marker(name="Task", start=Timecode(frames=6, frame_rate=fps),
               marker_type=MarkerType.INCOMPLETE),
        Marker(name="Done", start=Timecode(frames=12, frame_rate=fps),
               marker_type=MarkerType.COMPLETED),
        Marker(name="Ch1", start=Timecode(frames=18, frame_rate=fps),
               marker_type=MarkerType.CHAPTER),
    ]
    clip = Clip(
        name="Multi", start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/multi.mov", markers=markers,
    )
    tl = Timeline(name="A", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    root = writer._build_fcpxml(Project(name="A", timelines=[tl]))
    ac = root.find(".//asset-clip")
    plain_markers = ac.findall("marker")
    chapter_markers = ac.findall("chapter-marker")
    # 3 <marker> elements (standard + todo + completed), 1 <chapter-marker>
    assert len(plain_markers) == 3
    assert len(chapter_markers) == 1
    by_name = {m.get("value"): m for m in plain_markers}
    assert by_name["Std"].get("completed") is None
    assert by_name["Task"].get("completed") == "0"
    assert by_name["Done"].get("completed") == "1"
    assert chapter_markers[0].get("value") == "Ch1"


def test_timeline_level_markers(writer, fps):
    """Timeline-level markers should be children of the sequence element."""
    clip = Clip(
        name="C1", start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/c1.mov",
    )
    tl = Timeline(
        name="TL", duration=Timecode(frames=48, frame_rate=fps),
        frame_rate=fps, clips=[clip],
        markers=[Marker(name="Section", start=Timecode(frames=24, frame_rate=fps))],
    )
    proj = Project(name="TL", timelines=[tl])
    root = writer._build_fcpxml(proj)
    seq = root.find(".//sequence")
    seq_markers = seq.findall("marker")
    assert len(seq_markers) == 1
    assert seq_markers[0].get("value") == "Section"


# ============================================================
# Keywords
# ============================================================


def test_clip_with_keyword(writer, fps):
    """Keywords should appear as <keyword> children of the asset-clip."""
    clip = Clip(
        name="Tagged",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/tagged.mov",
        keywords=[Keyword(value="interview")],
    )
    tl = Timeline(name="K", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    proj = Project(name="K", timelines=[tl])
    root = writer._build_fcpxml(proj)
    kw = root.find(".//asset-clip/keyword")
    assert kw is not None
    assert kw.get("value") == "interview"


def test_keyword_with_range(writer, fps):
    """Keywords with start/duration should include those attributes."""
    clip = Clip(
        name="Ranged",
        start=Timecode(frames=0, frame_rate=fps),
        duration=Timecode(frames=48, frame_rate=fps),
        media_path="file:///media/ranged.mov",
        keywords=[Keyword(
            value="broll",
            start=Timecode(frames=0, frame_rate=fps),
            duration=Timecode(frames=24, frame_rate=fps),
        )],
    )
    tl = Timeline(name="KR", duration=Timecode(frames=48, frame_rate=fps),
                  frame_rate=fps, clips=[clip])
    proj = Project(name="KR", timelines=[tl])
    root = writer._build_fcpxml(proj)
    kw = root.find(".//keyword")
    assert kw.get("start") == "0/24s"
    assert kw.get("duration") == "24/24s"


# ============================================================
# Empty / edge-case projects
# ============================================================


def test_empty_project_no_timelines(writer):
    """A project with no timelines should still produce valid FCPXML."""
    proj = Project(name="Empty")
    root = writer._build_fcpxml(proj)
    assert root.tag == "fcpxml"
    assert root.find("library") is not None
    # No format resource needed
    assert root.find(".//sequence") is None


def test_empty_timeline_no_clips(writer, fps):
    """A timeline with zero clips should produce a spine with no children."""
    tl = Timeline(name="Blank", duration=Timecode(frames=0, frame_rate=fps),
                  frame_rate=fps, clips=[])
    proj = Project(name="Blank", timelines=[tl])
    root = writer._build_fcpxml(proj)
    spine = root.find(".//spine")
    assert spine is not None
    assert len(list(spine)) == 0
