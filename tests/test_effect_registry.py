"""The transition registry against what Final Cut actually ships (#30).

Until 0.27.0 ``FCP_EFFECTS`` held bare UUIDs (Final Cut writes and expects
``FxPlug:<UUID>``), seven of them matched no plugin in the app, and ``fade``
pointed at Fade, a colour look. Nothing caught it, because every test compared
the table to itself. The installed-app tests below read Final Cut's own plugin
list instead, and are skipped only where Final Cut is not installed (CI).
"""

import plistlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fcpxml.models import ValidationIssueType
from fcpxml.writer import (
    FCP_EFFECTS,
    FXPLUG_PREFIX,
    FCPXMLModifier,
    list_effects,
    resolve_transition,
    validate_fcpxml,
)

FILTERS_PLIST = Path(
    "/Applications/Final Cut Pro.app/Contents/PlugIns/InternalFiltersXPC.pluginkit"
    "/Contents/PlugIns/Filters.bundle/Contents/Info.plist"
)
FILTERS_STRINGS = FILTERS_PLIST.parent / "Resources" / "en.lproj" / "Localizable.strings"
needs_fcp = pytest.mark.skipif(
    not FILTERS_PLIST.is_file(), reason="Final Cut Pro is not installed"
)

UUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
FADE_LOOK = "8154D0DA-C99B-4EF8-8FF8-006FE5ED57F1"        # PAEFade, an FxFilter
NOISE_DISSOLVE = "ABFED81E-35D9-429C-AB47-438C1FB5D9DE"   # PAENoiseDissolve, an FxFilter


def _plugins():
    """Every plugin entry in Final Cut's Filters.bundle, by UUID."""
    with FILTERS_PLIST.open("rb") as f:
        data = plistlib.load(f)
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if "uuid" in node and "protocolNames" in node:
                found[node["uuid"]] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


class TestTheTable:
    def test_every_uid_carries_the_fxplug_prefix(self):
        for slug, (_, uid) in FCP_EFFECTS.items():
            assert uid.startswith(FXPLUG_PREFIX), slug
            assert UUID.match(uid[len(FXPLUG_PREFIX):]), slug

    def test_no_slug_points_at_a_filter(self):
        uids = {uid for _, uid in FCP_EFFECTS.values()}
        assert FXPLUG_PREFIX + FADE_LOOK not in uids
        assert FXPLUG_PREFIX + NOISE_DISSOLVE not in uids

    def test_fades_are_fade_to_color(self):
        for slug in ("fade", "fade-to-black", "fade-from-black"):
            assert FCP_EFFECTS[slug] == FCP_EFFECTS["dip-to-color"]

    def test_an_unknown_slug_raises_instead_of_dissolving(self):
        with pytest.raises(ValueError, match="noise-dissolve"):
            resolve_transition("noise-dissolve")

    def test_list_effects_reports_the_uid_as_written(self):
        for effect in list_effects():
            assert effect["uuid"].startswith(FXPLUG_PREFIX)


@needs_fcp
class TestAgainstInstalledFinalCut:
    def test_every_uid_is_a_transition_final_cut_ships(self):
        plugins = _plugins()
        for slug, (_, uid) in FCP_EFFECTS.items():
            entry = plugins.get(uid[len(FXPLUG_PREFIX):])
            assert entry is not None, f"{slug}: {uid} is not in Final Cut"
            assert "FxTransition" in entry["protocolNames"], (
                f"{slug}: {uid} is {entry['className']}, not a transition"
            )

    def test_every_name_is_final_cuts_display_name(self):
        plugins = _plugins()
        with FILTERS_STRINGS.open("rb") as f:
            strings = plistlib.load(f)
        for slug, (name, uid) in FCP_EFFECTS.items():
            key = plugins[uid[len(FXPLUG_PREFIX):]]["displayName"]
            assert strings.get(key) == name, slug

    def test_the_check_sees_a_fabricated_uid(self, monkeypatch):
        """Mutation check: the pre-0.27 Band Wipe uid must fail the test above."""
        monkeypatch.setitem(
            FCP_EFFECTS, "band-wipe",
            ("Band", "FxPlug:A4E0B8E4-E916-474B-A14C-E3A9E0B1A3C1"),
        )
        with pytest.raises(AssertionError, match="not in Final Cut"):
            self.test_every_uid_is_a_transition_final_cut_ships()


def _two_clip_project(tmp_path, effect_uid=None):
    """Two 5s clips on a 7s asset with 1s of media either side of each."""
    effect = (
        f'<effect id="r9" name="Cross Dissolve" uid="{effect_uid}"/>'
        if effect_uid else ""
    )
    path = tmp_path / "p.fcpxml"
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="r2" name="A" start="0s" duration="7s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/a.mov"/>
    </asset>
    <asset id="r3" name="B" start="0s" duration="7s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/b.mov"/>
    </asset>
    {effect}
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
      <spine>
        <asset-clip ref="r2" name="A" offset="0s" start="1s" duration="5s"/>
        <asset-clip ref="r3" name="B" offset="5s" start="1s" duration="5s"/>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
""")
    return path


class TestWrittenResource:
    def test_add_transition_writes_the_prefixed_uid(self, tmp_path):
        modifier = FCPXMLModifier(str(_two_clip_project(tmp_path)))
        modifier.add_transition("A", position="end", duration="00:00:00:12")
        effects = modifier.tree.getroot().findall(".//resources/effect")
        assert [e.get("uid") for e in effects] == [FCP_EFFECTS["cross-dissolve"][1]]

    def test_a_bare_uid_resource_is_upgraded_not_duplicated(self, tmp_path):
        bare = FCP_EFFECTS["cross-dissolve"][1][len(FXPLUG_PREFIX):]
        modifier = FCPXMLModifier(str(_two_clip_project(tmp_path, effect_uid=bare)))
        transition = modifier.add_transition("A", position="end", duration="00:00:00:12")
        effects = modifier.tree.getroot().findall(".//resources/effect")
        assert len(effects) == 1
        assert effects[0].get("uid") == FXPLUG_PREFIX + bare
        assert transition.find("filter-video").get("ref") == "r9"

    def test_the_validator_warns_on_a_bare_uid(self, tmp_path):
        bare = FCP_EFFECTS["cross-dissolve"][1][len(FXPLUG_PREFIX):]
        root = ET.parse(_two_clip_project(tmp_path, effect_uid=bare)).getroot()
        kinds = [i.issue_type for i in validate_fcpxml(root)]
        assert ValidationIssueType.EFFECT_UID in kinds

    def test_the_validator_is_quiet_on_the_prefixed_uid(self, tmp_path):
        path = _two_clip_project(tmp_path, effect_uid=FCP_EFFECTS["cross-dissolve"][1])
        root = ET.parse(path).getroot()
        assert not [i for i in validate_fcpxml(root)
                    if i.issue_type == ValidationIssueType.EFFECT_UID]
