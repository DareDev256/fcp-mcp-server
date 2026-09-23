"""Regression test for the adjust-colorConform / marker DTD ordering bug.

Bug: ``_ASSET_CLIP_CHILD_ORDER`` (fcpxml/writer.py) was missing four DTD
element names (``object-tracker``, ``adjust-cinematic``,
``adjust-colorConform``, ``adjust-stereo-3D``). A missing tag falls back to
"last priority" in ``_dtd_insert``'s lookup, which is worse than not being
ranked: it made a correctly-placed ``<adjust-colorConform>`` look like it
belonged *after* a newly-inserted ``<marker>``, so ``add_marker`` /
``batch_add_markers`` inserted the marker immediately after the clip's
``<adjust-transform>`` and before its ``<adjust-colorConform>``.

``adjust-transform`` + ``adjust-colorConform`` on one clip is an entirely
ordinary combination (any clip with a reframe/scale and a color-space
conform — e.g. any iPhone-sourced vertical clip pulled into a 1.14
project), so this reliably broke real timelines: Final Cut Pro 12.3
(FCPXML 1.14) rejected the exported file with:

    DTD validation failed. (Element asset-clip content does not follow
    the DTD, expecting (note?, (conform-rate?, timeMap?), (object-tracker?,
    adjust-crop?, adjust-corners?, adjust-conform?, adjust-transform?,
    adjust-blend?, adjust-stabilization?, adjust-rollingShutter?,
    adjust-360-transform?, adjust-reorient?, adjust-orientation?,
    adjust-cinematic?, adjust-colorConform?, adjust-stereo-3D?,
    adjust-volume?, adjust-panner?), (audio | video | clip | title |
    caption | mc-clip | ref-clip | sync-clip | asset...))

Reproduced and diagnosed against a real project (not included here —
these fixtures are synthetic, per project policy of never committing a
user's own FCPXML).

The incomplete table also blinded ``_check_child_order`` (the server's own
pre-export validator) to the exact bug it was introducing, since both read
the same ``_CHILD_ORDER_INDEX`` lookup.
"""

import shutil
import xml.etree.ElementTree as ET

import pytest

from fcpxml.dtd import available_dtd_versions, validate_against_dtd
from fcpxml.writer import (
    _ASSET_CLIP_CHILD_ORDER,
    FCPXMLModifier,
    MarkerType,
    _dtd_insert,
    build_marker_element,
    validate_fcpxml,
)

HAVE_DTDS = bool(available_dtd_versions()) and shutil.which("xmllint")

# Minimal DTD-valid 1.14 fixture: one asset-clip carrying both
# adjust-transform and adjust-colorConform, the exact combination that
# triggered the bug.
FIXTURE_114 = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<fcpxml version="1.14"><resources>'
    '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" '
    'width="1080" height="1920"/>'
    '<asset id="r2" name="A" start="0s" duration="300s" hasVideo="1" format="r1">'
    '<media-rep kind="original-media" src="file:///a.mov"/>'
    '</asset>'
    '</resources>'
    '<library><event name="Evt"><project name="DTD Test">'
    '<sequence format="r1" duration="150/30s" tcStart="0s"><spine>'
    '<asset-clip ref="r2" offset="0s" name="A" start="0s" duration="150/30s">'
    '<adjust-transform scale="1.37 1.37"/>'
    '<adjust-colorConform enabled="1" autoOrManual="manual" conformType="conformNone" '
    'peakNitsOfPQSource="1000" peakNitsOfSDRToPQSource="203"/>'
    '</asset-clip>'
    '</spine></sequence></project></event></library></fcpxml>'
)


def _child_tags(elem):
    return [c.tag for c in elem]


# ---------------------------------------------------------------------------
# Pure unit tests — no xmllint / Final Cut Pro required, always run in CI.
# ---------------------------------------------------------------------------

def test_ordering_table_includes_previously_missing_dtd_tags():
    """Lock in the six tags whose absence caused the bug (four in the
    adjust-* group that actually broke Mountain House, plus two more
    revealed by xmllint's full content model once fixture-tested locally:
    live-drawing and hidden-clip-marker).
    """
    for tag in (
        "object-tracker", "adjust-cinematic", "adjust-colorConform", "adjust-stereo-3D",
        "live-drawing", "hidden-clip-marker",
    ):
        assert tag in _ASSET_CLIP_CHILD_ORDER, f"{tag} missing from _ASSET_CLIP_CHILD_ORDER"


def test_ordering_table_matches_dtd_content_model_sequence():
    """Every ranked tag must appear in the same relative order as the DTD's
    asset-clip content model (as quoted in the FCP 12.3 / FCPXML 1.14 error
    text), so no later tag can outrank an earlier one.
    """
    dtd_order = [
        'note', 'conform-rate', 'timeMap', 'object-tracker',
        'adjust-crop', 'adjust-corners', 'adjust-conform', 'adjust-transform',
        'adjust-blend', 'adjust-stabilization', 'adjust-rollingShutter',
        'adjust-360-transform', 'adjust-reorient', 'adjust-orientation',
        'adjust-cinematic', 'adjust-colorConform', 'adjust-stereo-3D',
        'adjust-volume', 'adjust-panner',
        'audio', 'video', 'clip', 'title', 'caption',
        'mc-clip', 'ref-clip', 'sync-clip', 'asset-clip', 'audition', 'spine',
        'live-drawing',
        'marker', 'chapter-marker', 'rating', 'keyword', 'analysis-marker',
        'hidden-clip-marker',
    ]
    ranked_positions = [_ASSET_CLIP_CHILD_ORDER.index(tag) for tag in dtd_order]
    assert ranked_positions == sorted(ranked_positions), (
        "adjust-* / conform group in _ASSET_CLIP_CHILD_ORDER has drifted "
        "out of DTD order"
    )


def test_dtd_insert_places_marker_after_adjust_colorconform():
    """The exact repro: adjust-transform + adjust-colorConform present,
    then a marker is inserted — it must land after BOTH adjust elements.
    """
    clip = ET.Element('asset-clip', name='A')
    ET.SubElement(clip, 'adjust-transform', scale="1.37 1.37")
    ET.SubElement(
        clip, 'adjust-colorConform', enabled="1",
        autoOrManual="manual", conformType="conformNone",
    )

    build_marker_element(
        clip, MarkerType.STANDARD, start="10/30s", duration="1/30s", name="Marker 1",
    )

    tags = _child_tags(clip)
    assert tags == ['adjust-transform', 'adjust-colorConform', 'marker'], tags


def test_dtd_insert_orders_keyword_after_adjust_colorconform():
    """The same bug class for <keyword> (organize_auto / organize_keywords)."""
    clip = ET.Element('asset-clip', name='A')
    ET.SubElement(clip, 'adjust-transform', scale="1.0 1.0")
    ET.SubElement(
        clip, 'adjust-colorConform', enabled="1",
        autoOrManual="manual", conformType="conformNone",
    )

    _dtd_insert(clip, ET.Element('keyword', value="test"))

    tags = _child_tags(clip)
    assert tags == ['adjust-transform', 'adjust-colorConform', 'keyword'], tags


def test_validator_flags_marker_before_adjust_colorconform():
    """_check_child_order (validate_fcpxml) must catch the pre-fix ordering
    if it ever recurs — this is what should have caught the original bug
    before it ever reached Final Cut Pro.
    """
    root = ET.fromstring(
        '<fcpxml version="1.14"><library><event><project><sequence>'
        '<spine><asset-clip name="A">'
        '<adjust-transform scale="1.0 1.0"/>'
        '<marker start="0s" duration="1/30s" value="Marker 1"/>'
        '<adjust-colorConform enabled="1" autoOrManual="manual" conformType="conformNone"/>'
        '</asset-clip></spine>'
        '</sequence></project></event></library></fcpxml>'
    )
    issues = validate_fcpxml(root)
    order_issues = [i for i in issues if i.issue_type.name == "ELEMENT_ORDER"]
    assert order_issues, "validator failed to flag marker-before-adjust-colorConform"


def test_validator_accepts_correctly_ordered_clip():
    root = ET.fromstring(
        '<fcpxml version="1.14"><library><event><project><sequence>'
        '<spine><asset-clip name="A">'
        '<adjust-transform scale="1.0 1.0"/>'
        '<adjust-colorConform enabled="1" autoOrManual="manual" conformType="conformNone"/>'
        '<marker start="0s" duration="1/30s" value="Marker 1"/>'
        '</asset-clip></spine>'
        '</sequence></project></event></library></fcpxml>'
    )
    issues = validate_fcpxml(root)
    order_issues = [i for i in issues if i.issue_type.name == "ELEMENT_ORDER"]
    assert not order_issues, order_issues


# ---------------------------------------------------------------------------
# DTD-gated integration test — needs a local Final Cut Pro install + xmllint,
# matching the style of tests/test_dtd_validation.py. Skips cleanly in CI.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not HAVE_DTDS,
    reason="Apple FCPXML DTDs not available (Final Cut Pro not installed) "
    "or xmllint missing",
)
def test_add_marker_on_transform_plus_colorconform_clip_stays_dtd_valid(tmp_path):
    src = tmp_path / "fixture.fcpxml"
    src.write_text(FIXTURE_114)
    ok, detail = validate_against_dtd(str(src))
    assert ok is True, f"fixture itself invalid: {detail}"

    out = str(tmp_path / "fixture_marked.fcpxml")
    modifier = FCPXMLModifier(str(src))
    modifier.add_marker("A", "00:00:00:15", "dtd-check")
    modifier.save(out)

    ok, detail = validate_against_dtd(out)
    assert ok is True, detail
