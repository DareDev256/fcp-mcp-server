"""Tests for FCPXML validation functions and DTD infrastructure.

Covers the 6 _check_*() validators, _dtd_insert() ordering,
_enforce_standard_timebases() snapping, and validate_fcpxml() orchestration.
These are critical output-correctness guards — bugs here mean FCP import failures.
"""

import xml.etree.ElementTree as ET

from fcpxml.models import ValidationIssueType
from fcpxml.writer import (
    _check_asset_sources,
    _check_child_order,
    _check_effect_refs,
    _check_frame_alignment,
    _check_required_attributes,
    _check_timebases,
    _dtd_insert,
    _enforce_standard_timebases,
    _sanitize_xml_value,
    validate_fcpxml,
)

# ── _dtd_insert: DTD-ordered element insertion ──────────────────────


def test_dtd_insert_marker_before_filter():
    """Marker must appear before filter-video per DTD ordering."""
    clip = ET.Element("asset-clip")
    ET.SubElement(clip, "filter-video")
    marker = ET.Element("marker", value="test")
    _dtd_insert(clip, marker)
    tags = [c.tag for c in clip]
    assert tags.index("marker") < tags.index("filter-video")


def test_dtd_insert_note_always_first():
    """<note> has lowest priority — always inserted at position 0."""
    clip = ET.Element("clip")
    ET.SubElement(clip, "marker")
    ET.SubElement(clip, "filter-audio")
    note = ET.Element("note")
    _dtd_insert(clip, note)
    assert clip[0].tag == "note"


def test_dtd_insert_unknown_tag_appended():
    """Unknown tags get appended at the end."""
    clip = ET.Element("asset-clip")
    ET.SubElement(clip, "note")
    ET.SubElement(clip, "marker")
    custom = ET.Element("custom-extension")
    _dtd_insert(clip, custom)
    assert clip[-1].tag == "custom-extension"


def test_dtd_insert_into_empty_parent():
    """Inserting into an empty element works without error."""
    clip = ET.Element("clip")
    marker = ET.Element("marker", value="first")
    _dtd_insert(clip, marker)
    assert len(clip) == 1
    assert clip[0].tag == "marker"


def test_dtd_insert_preserves_between_existing():
    """Insert conform-rate between note and marker — correct middle position."""
    clip = ET.Element("asset-clip")
    ET.SubElement(clip, "note")
    ET.SubElement(clip, "marker")
    ET.SubElement(clip, "filter-video")
    conform = ET.Element("conform-rate")
    _dtd_insert(clip, conform)
    tags = [c.tag for c in clip]
    assert tags == ["note", "conform-rate", "marker", "filter-video"]


# ── _check_child_order: DTD ordering violations ─────────────────────


def test_check_child_order_valid():
    root = ET.fromstring(
        '<fcpxml><asset-clip name="A">'
        '<note/><marker/><filter-video/>'
        '</asset-clip></fcpxml>'
    )
    assert _check_child_order(root) == []


def test_check_child_order_detects_violation():
    """filter-video before marker violates DTD ordering."""
    root = ET.fromstring(
        '<fcpxml><asset-clip name="Bad">'
        '<filter-video ref="r1"/><marker value="x"/>'
        '</asset-clip></fcpxml>'
    )
    issues = _check_child_order(root)
    assert len(issues) == 1
    assert issues[0].issue_type == ValidationIssueType.ELEMENT_ORDER
    assert "Bad" in issues[0].message


def test_check_child_order_skips_single_child():
    """Single-child elements can't be out of order."""
    root = ET.fromstring(
        '<fcpxml><clip name="Solo"><marker/></clip></fcpxml>'
    )
    assert _check_child_order(root) == []


def test_check_child_order_ignores_non_clip_parents():
    """Only clip/asset-clip/video/audio/ref-clip are checked."""
    root = ET.fromstring(
        '<fcpxml><spine><filter-video/><marker/></spine></fcpxml>'
    )
    assert _check_child_order(root) == []


# ── _check_required_attributes ──────────────────────────────────────


def test_required_attrs_all_present():
    root = ET.fromstring(
        '<fcpxml><asset-clip ref="r1" duration="100/2400s" name="A"/>'
        '<format id="r1"/></fcpxml>'
    )
    assert _check_required_attributes(root) == []


def test_required_attrs_missing_ref_on_asset_clip():
    root = ET.fromstring(
        '<fcpxml><asset-clip duration="100/2400s" name="No Ref"/></fcpxml>'
    )
    issues = _check_required_attributes(root)
    types = [i.issue_type for i in issues]
    assert ValidationIssueType.MISSING_ATTRIBUTE in types
    assert any("ref" in i.message for i in issues)


def test_required_attrs_missing_id_on_format():
    root = ET.fromstring('<fcpxml><format name="HD"/></fcpxml>')
    issues = _check_required_attributes(root)
    assert len(issues) == 1
    assert "id" in issues[0].message


def test_required_attrs_transition_missing_all():
    """Transition missing name, offset, duration → 3 issues."""
    root = ET.fromstring('<fcpxml><transition/></fcpxml>')
    issues = _check_required_attributes(root)
    assert len(issues) == 3


# ── _check_timebases ────────────────────────────────────────────────


def test_timebases_standard_ok():
    """100/2400s simplifies to 1/24, 2400/2400s to 1/1 — both standard."""
    root = ET.fromstring(
        '<fcpxml><clip offset="100/2400s" duration="2400/2400s"/></fcpxml>'
    )
    assert _check_timebases(root) == []


def test_timebases_flags_nonstandard():
    """Denominator 7 is not a standard FCPXML timebase."""
    root = ET.fromstring(
        '<fcpxml><clip offset="3/7s" name="Weird"/></fcpxml>'
    )
    issues = _check_timebases(root)
    assert len(issues) == 1
    assert issues[0].issue_type == ValidationIssueType.INVALID_TIMEBASE
    assert "7" in issues[0].message


def test_timebases_deduplicates():
    """Same non-standard value on same element+attr only reported once."""
    root = ET.fromstring(
        '<fcpxml>'
        '<clip offset="3/7s" name="A"/>'
        '<clip offset="3/7s" name="B"/>'
        '</fcpxml>'
    )
    issues = _check_timebases(root)
    # Both have same (clip, offset, 3/7s) key — only 1 issue
    assert len(issues) == 1


# ── _check_frame_alignment ──────────────────────────────────────────


def test_frame_alignment_exact():
    """100 frames at 24fps = 100/24s — frame-aligned."""
    root = ET.fromstring(
        '<fcpxml><clip duration="100/24s" name="Aligned"/></fcpxml>'
    )
    assert _check_frame_alignment(root, fps=24.0) == []


def test_frame_alignment_misaligned():
    """7/24s = 7 frames — aligned. But 7/48s = 3.5 frames at 24fps — misaligned."""
    root = ET.fromstring(
        '<fcpxml><clip duration="7/48s" name="Half Frame"/></fcpxml>'
    )
    issues = _check_frame_alignment(root, fps=24.0)
    assert len(issues) == 1
    assert issues[0].issue_type == ValidationIssueType.FRAME_MISALIGNMENT


def test_frame_alignment_ignores_non_clip_elements():
    root = ET.fromstring(
        '<fcpxml><format duration="7/48s"/></fcpxml>'
    )
    assert _check_frame_alignment(root, fps=24.0) == []


# ── _check_effect_refs ──────────────────────────────────────────────


def test_effect_refs_valid():
    root = ET.fromstring(
        '<fcpxml><resources><effect id="e1"/></resources>'
        '<clip><filter-video ref="e1"/></clip></fcpxml>'
    )
    assert _check_effect_refs(root) == []


def test_effect_refs_dangling():
    root = ET.fromstring(
        '<fcpxml><clip><filter-video ref="missing_effect"/></clip></fcpxml>'
    )
    issues = _check_effect_refs(root)
    assert len(issues) == 1
    assert issues[0].issue_type == ValidationIssueType.MISSING_EFFECT_REF
    assert "missing_effect" in issues[0].message


# ── _check_asset_sources ────────────────────────────────────────────


def test_asset_with_src():
    root = ET.fromstring(
        '<fcpxml><resources>'
        '<asset id="r1" src="file:///video.mov" name="V"/>'
        '</resources></fcpxml>'
    )
    assert _check_asset_sources(root) == []


def test_asset_with_media_rep():
    root = ET.fromstring(
        '<fcpxml><resources>'
        '<asset id="r1" name="V"><media-rep/></asset>'
        '</resources></fcpxml>'
    )
    assert _check_asset_sources(root) == []


def test_asset_missing_both():
    root = ET.fromstring(
        '<fcpxml><resources>'
        '<asset id="r1" name="Orphan"/>'
        '</resources></fcpxml>'
    )
    issues = _check_asset_sources(root)
    assert len(issues) == 1
    assert issues[0].issue_type == ValidationIssueType.MISSING_MEDIA_REP


# ── _enforce_standard_timebases ─────────────────────────────────────


def test_enforce_timebases_snaps_nonstandard():
    root = ET.fromstring('<fcpxml><clip offset="3/7s"/></fcpxml>')
    _enforce_standard_timebases(root)
    clip = root.find("clip")
    val = clip.get("offset")
    assert "/" in val and val.endswith("s")
    # Should be snapped to a standard timebase denominator
    denom = int(val.rstrip("s").split("/")[1])
    assert denom in {1, 24, 25, 30, 48, 50, 60, 90, 96, 100, 120, 240, 600, 2400}


def test_enforce_timebases_leaves_standard_alone():
    root = ET.fromstring('<fcpxml><clip offset="3600/2400s"/></fcpxml>')
    _enforce_standard_timebases(root)
    assert root.find("clip").get("offset") == "3600/2400s"


def test_enforce_timebases_handles_unparseable():
    """Malformed time values are silently skipped, not crashed on."""
    root = ET.fromstring('<fcpxml><clip offset="garbage/0s"/></fcpxml>')
    _enforce_standard_timebases(root)  # Should not raise
    assert root.find("clip").get("offset") == "garbage/0s"


# ── _sanitize_xml_value edge cases ──────────────────────────────────


def test_sanitize_strips_null_bytes():
    assert _sanitize_xml_value("he\x00llo") == "hello"


def test_sanitize_preserves_tabs_newlines():
    assert _sanitize_xml_value("line1\tline2\n") == "line1\tline2\n"


def test_sanitize_enforces_max_length():
    result = _sanitize_xml_value("x" * 500, max_length=100)
    assert len(result) == 100


def test_sanitize_coerces_non_string():
    assert _sanitize_xml_value(42) == "42"


# ── validate_fcpxml orchestration ───────────────────────────────────


def test_validate_fcpxml_aggregates_all_checks():
    """Intentionally broken XML triggers multiple validator categories."""
    root = ET.fromstring(
        '<fcpxml>'
        '<resources><asset id="r1" name="No Source"/></resources>'
        '<asset-clip ref="r1" duration="7/48s" name="Clip">'
        '<filter-video ref="ghost"/><marker value="x"/>'
        '</asset-clip>'
        '</fcpxml>'
    )
    issues = validate_fcpxml(root, fps=24.0)
    types = {i.issue_type for i in issues}
    # Should catch: ordering (filter before marker), missing media rep,
    # dangling effect ref, frame misalignment
    assert ValidationIssueType.ELEMENT_ORDER in types
    assert ValidationIssueType.MISSING_MEDIA_REP in types
    assert ValidationIssueType.MISSING_EFFECT_REF in types
    assert ValidationIssueType.FRAME_MISALIGNMENT in types


def test_validate_fcpxml_clean_returns_empty():
    root = ET.fromstring(
        '<fcpxml>'
        '<resources><effect id="e1"/>'
        '<asset id="r1" src="file:///v.mov"/></resources>'
        '<asset-clip ref="r1" duration="100/2400s" name="OK">'
        '<marker value="ch1"/><filter-video ref="e1"/>'
        '</asset-clip>'
        '</fcpxml>'
    )
    assert validate_fcpxml(root) == []


class TestTimebaseCheckDoesNotWarnOnCanonicalFCPOutput:
    """Regression: the validator warned on Final Cut Pro's own canonical form.

    _check_timebases inlined `tv.simplify().denominator not in
    _FCPXML_STANDARD_TIMEBASES` — a second copy of a rule that already lives on
    TimeValue.is_standard_timebase(), and the copy is the one that drifted.
    "36/24s" reduces to 3/2, so every generated timeline logged a warning for
    every clip that was not a whole number of seconds long. An alarm that fires
    on correct output trains people to ignore the alarm.
    """

    @staticmethod
    def _tree(*durations):
        import xml.etree.ElementTree as ET

        root = ET.Element('fcpxml')
        sequence = ET.SubElement(root, 'sequence')
        for duration in durations:
            ET.SubElement(sequence, 'asset-clip', duration=duration)
        return root

    def test_frame_aligned_values_do_not_warn(self):
        from fcpxml.writer import _check_timebases

        tree = self._tree("36/24s", "18/24s", "108/24s", "42/30s", "1001/24000s")
        assert _check_timebases(tree) == []

    def test_a_genuinely_non_standard_denominator_still_warns(self):
        """Mutation check: the guard must not have been merely switched off."""
        from fcpxml.writer import _check_timebases

        issues = _check_timebases(self._tree("15/7s", "5/13s"))
        assert len(issues) == 2
        assert "7" in issues[0].message
        assert "13" in issues[1].message

    def test_good_and_bad_values_are_told_apart_in_one_pass(self):
        from fcpxml.writer import _check_timebases

        issues = _check_timebases(self._tree("36/24s", "15/7s", "1001/24000s"))
        assert len(issues) == 1
        assert '15/7s' in issues[0].message
