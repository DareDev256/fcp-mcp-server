"""Bulk keyword / rating / role edits on FCPXMLModifier."""

import xml.etree.ElementTree as ET

import pytest

from fcpxml.writer import FCPXMLModifier

SAMPLE = "examples/sample.fcpxml"
CLIP_TAGS = {"asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip"}


def _values(elem) -> set:
    return {v.strip() for k in elem.findall("keyword") for v in k.get("value").split(",")}


def test_select_clips_all_and_by_glob():
    m = FCPXMLModifier(SAMPLE)
    every = m.select_clips()
    assert len(every) >= 2 and all(e.tag in CLIP_TAGS for e in every)
    first = every[0].get("name")
    assert first in [e.get("name") for e in m.select_clips(clip_name=first[:3] + "*")]
    assert m.select_clips(clip_name="zzz-no-such-clip*") == []


def test_bulk_keywords_add_remove_replace(tmp_path):
    m = FCPXMLModifier(SAMPLE)
    clips = m.select_clips()
    assert m.bulk_keywords(clips, ["interview", "wide"], "add") == len(clips)
    assert m.bulk_keywords(clips, ["interview"], "add") == 0  # already present
    out = tmp_path / "k.fcpxml"
    m.save(str(out))
    root = ET.parse(out).getroot()
    values = {v.strip() for k in root.iter("keyword") for v in k.get("value").split(",")}
    assert {"interview", "wide"} <= values

    m2 = FCPXMLModifier(str(out))
    c2 = m2.select_clips(keyword="wide")
    assert len(c2) == len(clips)
    assert m2.bulk_keywords(c2, ["wide"], "remove") == len(clips)
    assert m2.select_clips(keyword="wide") == []
    m2.bulk_keywords(c2, ["only"], "replace")
    assert all(_values(c) == {"only"} for c in c2)


def test_keyword_lands_in_dtd_order():
    """keyword is a marker item: after markers, before filters/metadata."""
    m = FCPXMLModifier(SAMPLE)
    clip = m.select_clips()[0]
    ET.SubElement(clip, "metadata")
    m.bulk_keywords([clip], ["late"], "add")
    tags = [c.tag for c in clip]
    assert tags.index("keyword") < tags.index("metadata")


def test_bulk_rating_and_clear():
    m = FCPXMLModifier(SAMPLE)
    clips = m.select_clips()
    m.bulk_rating(clips, "favorite")
    m.bulk_rating(clips[:1], "rejected")
    assert clips[0].find("rating").get("value") == "rejected" and len(clips[0].findall("rating")) == 1
    assert clips[1].find("rating").get("value") == "favorite"
    m.bulk_rating(clips, "clear")
    assert all(c.find("rating") is None for c in clips)


def test_bulk_roles_and_select_by_role():
    m = FCPXMLModifier(SAMPLE)
    clips = m.select_clips()
    assert m.bulk_roles(clips, audio_role="dialogue.interview") == len(clips)
    assert len(m.select_clips(role="DIALOGUE.interview")) == len(clips)


def test_bulk_keywords_rejects_mode_and_rating():
    m = FCPXMLModifier(SAMPLE)
    with pytest.raises(ValueError, match="mode"):
        m.bulk_keywords(m.select_clips(), ["x"], "upsert")
    with pytest.raises(ValueError, match="rating"):
        m.bulk_rating(m.select_clips(), "five-stars")
