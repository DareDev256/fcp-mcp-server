"""Files Final Cut refused, and the validator passed (#31-#34).

Each issue was reported from a real import into Final Cut Pro by
@brannonwinn against v0.26.0. The repros are taken from the reports
as written.
"""

import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from fcpxml import dtd
from fcpxml.models import ValidationIssueType
from fcpxml.parser import FCPXMLParser
from fcpxml.rational import parse_seconds
from fcpxml.rough_cut import RoughCutGenerator
from fcpxml.writer import FCP_EFFECTS, FCPXMLModifier, validate_fcpxml

HAS_DTD = dtd.find_apple_dtd("1.13") is not None


def _project(tmp_path, asset_seconds, clip_start, spine_extra="", name="p.fcpxml"):
    """Two 5s clips back to back, A then B, each on its own asset.

    *asset_seconds* is each asset's length and *clip_start* each clip's
    in-point, so ``(5, "0s")`` is the reporter's file: media used in full,
    no handles at all.
    """
    path = tmp_path / name
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="r2" name="A" start="0s" duration="{asset_seconds}s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/a.mov"/>
    </asset>
    <asset id="r3" name="B" start="0s" duration="{asset_seconds}s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/b.mov"/>
    </asset>
    <asset id="r4" name="Title" start="0s" duration="10s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/t.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
      <spine>
        <asset-clip ref="r2" name="A" offset="0s" start="{clip_start}" duration="5s"/>
        <asset-clip ref="r3" name="B" offset="5s" start="{clip_start}" duration="5s"/>
        {spine_extra}
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
""")
    return path


def _errors(root, kind):
    return [i for i in validate_fcpxml(root) if i.issue_type == kind]


# ---------------------------------------------------------------------------
# #31  add_transition() and media handles
# ---------------------------------------------------------------------------

class TestTransitionHandles:
    def test_the_reported_file_is_refused_before_anything_is_written(self, tmp_path):
        modifier = FCPXMLModifier(str(_project(tmp_path, 5, "0s")))
        before = ET.tostring(modifier.tree.getroot())
        with pytest.raises(ValueError) as caught:
            modifier.add_transition("A", position="end", duration="00:00:00:12")
        message = str(caught.value)
        assert "'A' has 0 frames of media after its out-point and needs 6 frames" in message
        assert "'B' has 0 frames of media before its in-point and needs 6 frames" in message
        assert "No transition fits" in message
        # No orphan <effect>, no half-written <transition>.
        assert ET.tostring(modifier.tree.getroot()) == before

    def test_with_handles_the_reported_transition_is_written(self, tmp_path):
        """The reporter's fix: 1s of media either side, in-points at 1s."""
        modifier = FCPXMLModifier(str(_project(tmp_path, 7, "1s")))
        transition = modifier.add_transition("A", position="end", duration="00:00:00:12")
        assert parse_seconds(transition.get("offset")) == Fraction(114, 24)
        assert parse_seconds(transition.get("duration")) == Fraction(12, 24)
        out = tmp_path / "out.fcpxml"
        modifier.save(str(out))
        assert not _errors(ET.parse(out).getroot(), ValidationIssueType.TRANSITION_HANDLES)

    def test_the_refusal_names_the_longest_transition_that_fits(self, tmp_path):
        # 2 frames of media each side: a 4-frame dissolve fits, 12 does not.
        modifier = FCPXMLModifier(str(_project(tmp_path, "124/24", "2/24s")))
        with pytest.raises(ValueError, match="longest Cross Dissolve that fits is 4 frames"):
            modifier.add_transition("A", position="end", duration="00:00:00:12")
        modifier.add_transition("A", position="end", duration="00:00:00:04")

    def test_both_cuts_are_checked_before_either_is_written(self, tmp_path):
        # B has handles at its head but none after its out-point, and the
        # gap after it is fine: position="both" on B must fail on the end cut
        # without leaving the start-cut transition behind.
        path = _project(tmp_path, 7, "1s", spine_extra='<gap name="G" offset="10s" duration="2s"/>')
        text = path.read_text().replace(
            '<asset-clip ref="r3" name="B" offset="5s" start="1s" duration="5s"/>',
            '<asset-clip ref="r3" name="B" offset="5s" start="2s" duration="5s"/>',
        )
        path.write_text(text)
        modifier = FCPXMLModifier(str(path))
        with pytest.raises(ValueError, match="'B' has 0 frames of media after its out-point"):
            modifier.add_transition("B", position="both", duration="00:00:00:12")
        assert modifier.tree.getroot().find(".//transition") is None

    def test_a_gap_neighbour_never_runs_short(self, tmp_path):
        path = _project(tmp_path, 7, "1s", spine_extra='<gap name="G" offset="10s" duration="2s"/>')
        modifier = FCPXMLModifier(str(path))
        # B's tail: 7s asset, in at 1s for 5s, so 1s after its out-point.
        modifier.add_transition("B", position="end", duration="00:00:00:12")

    def test_a_still_never_runs_short(self, tmp_path):
        path = _project(tmp_path, 5, "0s")
        path.write_text(path.read_text().replace('duration="5s" hasVideo', 'duration="0s" hasVideo'))
        FCPXMLModifier(str(path)).add_transition("A", position="end", duration="00:00:00:12")


class TestValidatorSeesHandles:
    def test_the_reported_file_is_an_error(self, tmp_path):
        transition = (
            '<transition name="Cross Dissolve" offset="114/24s" duration="12/24s"/>'
        )
        path = _project(tmp_path, 5, "0s")
        path.write_text(path.read_text().replace(
            '<asset-clip ref="r3"', transition + '\n        <asset-clip ref="r3"'))
        found = _errors(ET.parse(path).getroot(), ValidationIssueType.TRANSITION_HANDLES)
        assert {i.clip_name for i in found} == {"A", "B"}
        assert all(i.severity == "error" for i in found)

    def test_an_off_centre_transition_is_held_to_what_it_overlaps(self, tmp_path):
        # Ends at the edit: 12 frames before the cut, none after it. Only B's
        # head is drawn on, and B has 1s of it.
        transition = '<transition name="Cross Dissolve" offset="108/24s" duration="12/24s"/>'
        path = _project(tmp_path, 7, "1s")
        text = path.read_text().replace(
            '<asset-clip ref="r2" name="A" offset="0s" start="1s"',
            '<asset-clip ref="r2" name="A" offset="0s" start="2s"')  # A: no tail
        path.write_text(text.replace(
            '<asset-clip ref="r3"', transition + '\n        <asset-clip ref="r3"'))
        assert not _errors(ET.parse(path).getroot(), ValidationIssueType.TRANSITION_HANDLES)


# ---------------------------------------------------------------------------
# #32 / #33  RoughCutGenerator
# ---------------------------------------------------------------------------

def _seed(tmp_path, handles=True):
    """A seed library: four 10s clips on 20s assets, each with its own format.

    With *handles* the clips sit mid-asset; without, each clip uses its
    asset whole, so no transition between them can fit.
    """
    asset_len = "20s" if handles else "10s"
    start = "5s" if handles else "0s"
    assets = "\n".join(
        f'<asset id="a{i}" name="S{i}" uid="UID-{i}" start="0s" duration="{asset_len}" hasVideo="1" '
        f'hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000" format="f2">'
        f'<media-rep kind="original-media" src="file:///tmp/s{i}.mov"/></asset>'
        for i in range(4)
    )
    clips = "\n".join(
        f'<asset-clip ref="a{i}" name="S{i}" offset="{i * 10}s" start="{start}" duration="10s"/>'
        for i in range(4)
    )
    path = tmp_path / "seed.fcpxml"
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="f1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <format id="f2" name="FFVideoFormat3840x2160p24" frameDuration="100/2400s" width="3840" height="2160"/>
    {assets}
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="f1" duration="40s" tcStart="0s" tcFormat="NDF"><spine>
    {clips}
    </spine></sequence>
  </project></event></library>
</fcpxml>
""")
    return path


def _rough_cut(tmp_path, handles=True, **kwargs):
    out = tmp_path / "cut.fcpxml"
    result = RoughCutGenerator(str(_seed(tmp_path, handles))).generate(
        output_path=str(out), target_duration="15s", priority="longest", **kwargs)
    return result, ET.parse(out).getroot(), out


class TestRoughCutTransitions:
    def test_every_filter_video_points_at_a_registered_effect(self, tmp_path):
        result, root, _ = _rough_cut(
            tmp_path, add_transitions=True, transition_duration="00:00:00:12")
        transitions = root.findall(".//spine/transition")
        assert transitions, "the seed has handles, so transitions must be written"
        effects = {e.get("id"): e for e in root.iter("effect")}
        for transition in transitions:
            ref = transition.find("filter-video").get("ref")
            assert effects[ref].get("uid") == FCP_EFFECTS["cross-dissolve"][1]
        assert result.transitions_skipped == []
        assert not [i for i in validate_fcpxml(root) if i.severity == "error"]

    def test_cuts_without_media_stay_hard_and_are_reported(self, tmp_path):
        result, root, _ = _rough_cut(
            tmp_path, handles=False, add_transitions=True,
            transition_duration="00:00:00:12")
        assert root.find(".//spine/transition") is None
        assert result.transitions_skipped
        assert "0 frames of media" in result.transitions_skipped[0]
        assert not _errors(root, ValidationIssueType.TRANSITION_HANDLES)

    @pytest.mark.skipif(not HAS_DTD, reason="Apple FCPXML 1.13 DTD not installed")
    def test_the_rough_cut_is_dtd_valid(self, tmp_path):
        _, _, out = _rough_cut(tmp_path, add_transitions=True,
                               transition_duration="00:00:00:12")
        ok, detail = dtd.validate_against_dtd(str(out))
        assert ok, detail

    def test_ab_roll_default_transitions_are_valid(self, tmp_path):
        out = tmp_path / "ab.fcpxml"
        seed = _seed(tmp_path)
        text = seed.read_text()
        for i, kw in enumerate(("a", "a", "b", "b")):
            text = text.replace(
                f'name="S{i}" offset="{i * 10}s" start="5s" duration="10s"/>',
                f'name="S{i}" offset="{i * 10}s" start="5s" duration="10s">'
                f'<keyword start="5s" duration="10s" value="{kw}"/></asset-clip>')
        seed.write_text(text)
        RoughCutGenerator(str(seed)).generate_ab_roll(
            output_path=str(out), target_duration="20s",
            a_keywords=["a"], b_keywords=["b"])
        root = ET.parse(out).getroot()
        for fv in root.iter("filter-video"):
            assert fv.get("ref"), "filter-video ref is #REQUIRED in the DTD"
        assert not [i for i in validate_fcpxml(root) if i.severity == "error"]


class TestRoughCutAssets:
    def test_copied_assets_keep_format_and_every_attribute(self, tmp_path):
        _, root, _ = _rough_cut(tmp_path)
        seed = ET.parse(tmp_path / "seed.fcpxml").getroot()
        originals = {a.get("id"): a for a in seed.iter("asset")}
        copied = list(root.iter("asset"))
        assert copied
        for asset in copied:
            assert asset.attrib == originals[asset.get("id")].attrib
        assert {a.get("format") for a in copied} == {"f2"}

    def test_the_format_an_asset_points_at_is_copied(self, tmp_path):
        _, root, _ = _rough_cut(tmp_path)
        formats = {f.get("id") for f in root.iter("format")}
        assert formats == {"f1", "f2"}  # the sequence's and the assets'

    def test_a_pre_1_9_src_attribute_moves_onto_a_media_rep(self, tmp_path):
        seed = _seed(tmp_path)
        text = seed.read_text()
        text = text.replace(
            'format="f2"><media-rep kind="original-media" src="file:///tmp/s0.mov"/></asset>',
            'format="f2" src="file:///tmp/s0.mov"/>')
        seed.write_text(text)
        out = tmp_path / "cut.fcpxml"
        RoughCutGenerator(str(seed)).generate(
            output_path=str(out), target_duration="40s", priority="longest")
        asset = ET.parse(out).getroot().find(".//asset[@id='a0']")
        assert asset.get("src") is None
        assert asset.find("media-rep").get("src") == "file:///tmp/s0.mov"


# ---------------------------------------------------------------------------
# #34  add_connected_clip() / add_audio_clip() offset
# ---------------------------------------------------------------------------

class TestAnchoredOffset:
    def _trimmed_parent(self, tmp_path):
        # A starts 1s into its media: its local clock begins at 1s.
        return FCPXMLModifier(str(_project(tmp_path, 7, "1s")))

    def test_connected_clip_offset_is_relative_to_the_parent_start(self, tmp_path):
        modifier = self._trimmed_parent(tmp_path)
        clip = modifier.add_connected_clip(
            parent_clip_id="A", asset_id="r4", offset="0s", duration="3s", lane=1)
        assert parse_seconds(clip.get("offset")) == 1
        parent = modifier.clips["A"]
        assert modifier.connected_timeline_offset(clip, parent) == 0

    def test_the_parser_reads_it_back_at_the_parent_in_point(self, tmp_path):
        modifier = self._trimmed_parent(tmp_path)
        modifier.add_connected_clip(
            parent_clip_id="B", asset_id="r4", offset="2s", duration="1s", lane=1)
        out = tmp_path / "out.fcpxml"
        modifier.save(str(out))
        timeline = FCPXMLParser().parse_file(str(out)).primary_timeline
        connected = [c for c in timeline.connected_clips if c.name == "Title"]
        assert len(connected) == 1
        # B is at 5s on the timeline; 2s into it is 7s.
        assert connected[0].timeline_start.seconds == pytest.approx(7.0)

    def test_audio_clip_offset_is_relative_to_the_parent_start(self, tmp_path):
        modifier = self._trimmed_parent(tmp_path)
        clip = modifier.add_audio_clip(
            parent_clip_id="A", asset_id="r4", offset="500/1000s", duration="1s")
        assert parse_seconds(clip.get("offset")) == Fraction(3, 2)

    def test_an_untrimmed_parent_is_unchanged(self, tmp_path):
        modifier = FCPXMLModifier(str(_project(tmp_path, 5, "0s")))
        clip = modifier.add_connected_clip(
            parent_clip_id="A", asset_id="r4", offset="2s", duration="1s", lane=1)
        assert parse_seconds(clip.get("offset")) == 2

    def test_a_connected_clip_goes_before_the_parents_keywords(self, tmp_path):
        """Found building the 0.27.0 Final Cut import pack: appended after a
        <keyword>, the clip broke the DTD's child order."""
        path = _project(tmp_path, 7, "1s")
        path.write_text(path.read_text().replace(
            '<asset-clip ref="r2" name="A" offset="0s" start="1s" duration="5s"/>',
            '<asset-clip ref="r2" name="A" offset="0s" start="1s" duration="5s">'
            '<keyword start="1s" duration="5s" value="k"/></asset-clip>'))
        modifier = FCPXMLModifier(str(path))
        clip = modifier.add_connected_clip(
            parent_clip_id="A", asset_id="r4", offset="0s", duration="1s", lane=1)
        assert [c.tag for c in modifier.clips["A"]] == ["asset-clip", "keyword"]
        assert list(modifier.clips["A"])[0] is clip
        out = tmp_path / "out.fcpxml"
        modifier.save(str(out))
        assert not [i for i in validate_fcpxml(ET.parse(out).getroot())
                    if i.issue_type == ValidationIssueType.ELEMENT_ORDER]
        if HAS_DTD:
            ok, detail = dtd.validate_against_dtd(str(out))
            assert ok, detail
