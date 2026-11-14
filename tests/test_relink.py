"""Tests for the relink_media bulk path-rewrite operation (v0.8.0)."""

import xml.etree.ElementTree as ET

import pytest

import server
from fcpxml.writer import FCPXMLModifier

RELINK_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<fcpxml version="1.13"><resources>'
    '<format id="r1" frameDuration="1/24s" width="1920" height="1080"/>'
    '<asset id="r2" name="Interview" start="0s" duration="100s">'
    '<media-rep kind="original-media" src="file:///Volumes/OldDrive/Media/My%20Footage/interview.mov"/>'
    '</asset>'
    '<asset id="r3" name="Broll" src="/Volumes/OldDrive/Media/broll.mov" start="0s" duration="50s"/>'
    '<asset id="r4" name="Elsewhere" src="file:///Volumes/OtherDrive/x.mov" start="0s" duration="10s"/>'
    '</resources>'
    '<library><event name="Evt"><project name="Relink Test">'
    '<sequence format="r1" duration="240/24s"><spine>'
    '<asset-clip ref="r2" offset="0s" name="Interview" start="0s" duration="120/24s" format="r1"/>'
    '</spine></sequence></project></event></library></fcpxml>'
)


@pytest.fixture
def project_file(tmp_path):
    f = tmp_path / "relink.fcpxml"
    f.write_text(RELINK_XML)
    return f


def test_relink_url_and_plain_src(project_file):
    modifier = FCPXMLModifier(str(project_file))
    result = modifier.relink_media("/Volumes/OldDrive/Media", "/Volumes/NewDrive/Footage")

    assert result["relinked"] == 2
    assert result["total_assets"] == 3
    by_asset = {c["asset"]: c for c in result["changes"]}
    # file:// URL form preserved, percent-encoding intact
    assert by_asset["Interview"]["new"] == (
        "file:///Volumes/NewDrive/Footage/My%20Footage/interview.mov"
    )
    # plain path stays a plain path
    assert by_asset["Broll"]["new"] == "/Volumes/NewDrive/Footage/broll.mov"
    # non-matching asset untouched
    assert "Elsewhere" not in by_asset


def test_relink_accepts_file_url_prefixes(project_file):
    modifier = FCPXMLModifier(str(project_file))
    result = modifier.relink_media(
        "file:///Volumes/OldDrive/Media", "file:///Volumes/NewDrive"
    )
    assert result["relinked"] == 2


def test_relink_whole_segment_matching(project_file):
    """/Volumes/OldDrive/Med must NOT match /Volumes/OldDrive/Media/..."""
    modifier = FCPXMLModifier(str(project_file))
    result = modifier.relink_media("/Volumes/OldDrive/Med", "/tmp/x")
    assert result["relinked"] == 0


def test_relink_dry_run_does_not_mutate(project_file):
    modifier = FCPXMLModifier(str(project_file))
    result = modifier.relink_media("/Volumes/OldDrive", "/Volumes/New", dry_run=True)
    assert result["relinked"] == 2
    assert result["dry_run"] is True
    # tree unchanged
    media_rep = modifier.root.find(".//media-rep")
    assert "OldDrive" in media_rep.get("src")


def test_relink_target_exists_flag(project_file, tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "broll.mov").write_bytes(b"x")
    modifier = FCPXMLModifier(str(project_file))
    result = modifier.relink_media("/Volumes/OldDrive/Media", str(real_dir))
    by_asset = {c["asset"]: c for c in result["changes"]}
    assert by_asset["Broll"]["target_exists"] is True
    assert by_asset["Interview"]["target_exists"] is False


def test_relink_empty_find_rejected(project_file):
    modifier = FCPXMLModifier(str(project_file))
    with pytest.raises(ValueError, match="non-empty"):
        modifier.relink_media("", "/tmp")


def test_relink_persists_through_save(project_file, tmp_path):
    modifier = FCPXMLModifier(str(project_file))
    modifier.relink_media("/Volumes/OldDrive/Media", "/Volumes/NewDrive")
    out = tmp_path / "relinked.fcpxml"
    modifier.save(str(out))
    root = ET.parse(out).getroot()
    assert root.find(".//media-rep").get("src") == (
        "file:///Volumes/NewDrive/My%20Footage/interview.mov"
    )


# ============================================================
# MCP handler
# ============================================================

async def test_handler_relink(project_file):
    result = await server.handle_relink_media({
        "filepath": str(project_file),
        "find": "/Volumes/OldDrive/Media",
        "replace": "/Volumes/NewDrive",
    })
    text = result[0].text
    assert "Relinked 2 media references" in text
    assert "_relinked" in text


async def test_handler_relink_dry_run(project_file):
    result = await server.handle_relink_media({
        "filepath": str(project_file),
        "find": "/Volumes/OldDrive/Media",
        "replace": "/Volumes/NewDrive",
        "dry_run": True,
    })
    text = result[0].text
    assert "Would relink 2" in text
    assert "Dry run" in text
    # No output file created
    assert not (project_file.parent / "relink_relinked.fcpxml").exists()


async def test_handler_relink_no_match(project_file):
    result = await server.handle_relink_media({
        "filepath": str(project_file),
        "find": "/Nonexistent/Prefix",
        "replace": "/tmp",
    })
    assert "Nothing to relink" in result[0].text


def test_relink_registered_in_dispatch():
    assert "relink_media" in server.TOOL_HANDLERS
