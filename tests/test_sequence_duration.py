"""A ripple that removes time removes it from the sequence too (0.31).

Until 0.31 every ripple moved the later clips and left the sequence's
declared duration where it was. remove_media_silence on the hero's 35 s cut
reported "6.33s removed" while the file still said 35 s, so the app drew
6.33 s of empty tail after the last clip and the summary read 35.00s. The
writer now keeps a sequence that ended where its spine ended doing so after
the ripple; one that declared a deliberate run-out keeps it.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from fcpxml.writer import FCPXMLModifier

DOC = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
  <resources>
    <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="A" start="0s" duration="20s" hasVideo="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/A.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P">
    <sequence format="r1" duration="{duration}" tcStart="{tc}" tcFormat="NDF">
      <spine>
        <asset-clip ref="r2" name="One" offset="{o1}" start="0s" duration="5s"/>
        <asset-clip ref="r2" name="Two" offset="{o2}" start="5s" duration="10s"/>
        <asset-clip ref="r2" name="Three" offset="{o3}" start="15s" duration="5s"/>
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>
"""


def _write(tmp_path: Path, duration="20s", tc="0s", base=0) -> Path:
    path = tmp_path / "cut.fcpxml"
    path.write_text(DOC.format(duration=duration, tc=tc, o1=f"{base}s", o2=f"{base + 5}s", o3=f"{base + 15}s"))
    return path


def _sequence_duration(path: Path) -> str:
    return ET.parse(path).getroot().find(".//sequence").get("duration")


def _cut_two_seconds_out_of_two(path: Path, out: Path) -> None:
    from fcpxml.models import TimeValue

    mod = FCPXMLModifier(str(path))
    clip = next(c for c in mod._get_spine() if c.get("name") == "Two")
    removed = mod.cut_clip_ranges(clip, [(TimeValue.from_seconds(2, 24), TimeValue.from_seconds(4, 24))])
    assert removed.to_seconds() == pytest.approx(2)
    mod.save(str(out))


@pytest.mark.parametrize("tc,base", [("0s", 0), ("3600s", 3600)], ids=["zero-start", "hour-start"])
def test_a_cut_shortens_a_sequence_that_ended_with_its_spine(tmp_path, tc, base):
    src = _write(tmp_path, tc=tc, base=base)
    out = tmp_path / "out.fcpxml"
    _cut_two_seconds_out_of_two(src, out)
    assert _sequence_duration(out) in ("18s", "432/24s", "43200/2400s")
    from fcpxml.parser import FCPXMLParser
    tl = FCPXMLParser().parse_file(str(out)).timelines[0]
    assert tl.duration.seconds == pytest.approx(18)
    assert sum(c.duration.seconds for c in tl.clips) == pytest.approx(18)  # no empty tail


def test_a_deliberate_run_out_is_kept(tmp_path):
    src = _write(tmp_path, duration="30s")  # 10 s past the spine on purpose
    out = tmp_path / "out.fcpxml"
    _cut_two_seconds_out_of_two(src, out)
    assert _sequence_duration(out) == "30s"


def test_a_rippled_delete_shortens_the_sequence_too(tmp_path):
    src = _write(tmp_path)
    mod = FCPXMLModifier(str(src))
    mod.delete_clip(["Two"], ripple=True)
    out = tmp_path / "out.fcpxml"
    mod.save(str(out))
    from fcpxml.models import TimeValue
    assert TimeValue.from_timecode(_sequence_duration(out), 24).to_seconds() == pytest.approx(10)
