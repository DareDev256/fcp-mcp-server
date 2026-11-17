"""Every clip we AUTHOR goes on its file's own clock (#45).

Final Cut puts an asset on its media's timecode track: an iPhone clip with a
``tmcd`` of 18:03:22:05 at 30 fps is on a clock that starts at 1950065/30s,
whatever ``<asset start>`` says. ``import_edl_json`` wrote ``start="0s"`` and
clips at their file time, so every range asked for frames before the file
begins and Final Cut imported an empty timeline with no warning, while
``validate_timeline`` passed it. The reporter's hand fix (asset at
1950065/30s, clips at 1950215/30s and 1950665/30s for 5s and 20s in) played
both ranges.

The same disease was in every writer that makes a clip: ``apply_template``
and ``add_audio`` with a ``src`` (a new asset at 0s), and ``insert_clip`` /
``add_connected_clip`` / ``add_audio_clip`` on an EXISTING timecoded asset
(a clip at 0s on an asset that starts at 3600s).

Only the tmcd track counts: a Sony XAVC ``.mp4`` carries its timecode in an
``rtmd`` track that ffprobe prints all the same, and Final Cut starts it at 0
(the reporter imported it three ways; Final Cut's own export of 35 Sony
clips writes every asset at 0s). Media here is synthesised with ffmpeg's own
``-timecode`` (a real tmcd track), at the rates where the arithmetic differs:
30, 29.97 non-drop and drop-frame, 23.976.
"""

import asyncio
import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import server
from fcpxml import dtd, media_clock
from fcpxml.edl import edl_to_fcpxml
from fcpxml.media_clock import (
    IGNORED,
    NONE,
    TMCD,
    UNREAD,
    clock_from_probe,
    media_url,
    read_clock,
    smpte_frames,
)
from fcpxml.media_intel import file_in_point, media_src_to_path
from fcpxml.parser import FCPXMLParser
from fcpxml.templates import ClipSpec, apply_template
from fcpxml.writer import FCPXMLModifier

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


def _make(path: Path, rate: str, *extra: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={rate}:duration=6",
         "-f", "lavfi", "-i", "sine=f=440:d=6",
         "-c:v", "prores_ks", "-profile:v", "0", "-c:a", "pcm_s16le",
         *extra, str(path)],
        capture_output=True, timeout=120, check=True,
    )
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("media_clock")
    return {
        "iphone": _make(d / "IMG_1968.mov", "30", "-timecode", "18:03:22:05"),
        "plain": _make(d / "plain.mov", "30", "-write_tmcd", "0"),
        "ndf": _make(d / "ntsc_ndf.mov", "30000/1001", "-timecode", "01:00:00:00"),
        "df": _make(d / "ntsc_df.mov", "30000/1001", "-timecode", "01:00:00;00"),
        "film": _make(d / "film.mov", "24000/1001", "-timecode", "01:00:00:00"),
        # 10:00:00:01 at 25 is 36000.04s: 1080001.2 frames of a 30p cut, so
        # the nearest sequence frame is BEFORE the file begins
        "pal": _make(d / "pal.mov", "25", "-timecode", "10:00:00:01"),
        # A path with a space and Chinese in it: what a documentary editor's
        # folders look like, and what a raw file:// URL cannot carry.
        "named": _make(d / "素材 Day 1" / "iphone clip.mov", "30", "-timecode", "18:03:22:05"),
        "dir": d,
    }


def _assets(path) -> dict:
    """{name: <asset> element} of a written document."""
    import xml.etree.ElementTree as ET

    root = ET.parse(str(path)).getroot()
    return {a.get("name"): a for a in root.iter("asset")}


def _clip_starts(path) -> list[str]:
    import xml.etree.ElementTree as ET

    return [c.get("start") for c in ET.parse(str(path)).getroot().iter("asset-clip")]


def _dtd_valid(path):
    ok, detail = dtd.validate_against_dtd(str(path))
    if ok is None:
        pytest.skip(f"DTD validation unavailable: {detail}")
    assert ok is True, detail


# --- the arithmetic ------------------------------------------------------------


@pytest.mark.parametrize("label, rate, frames", [
    ("18:03:22:05", Fraction(30), 1950065),              # the #45 clip
    ("01:00:00:00", Fraction(30000, 1001), 108000),      # non-drop counts every label
    ("01:00:00;00", Fraction(30000, 1001), 107892),      # drop-frame skips 108 in an hour
    ("00:01:00;02", Fraction(30000, 1001), 1800),        # ;00 and ;01 do not exist
    ("00:10:00;00", Fraction(30000, 1001), 17982),       # except every tenth minute
    ("01:00:00;00", Fraction(60000, 1001), 215784),      # four a minute at 59.94
    ("01:00:00:00", Fraction(24000, 1001), 86400),       # 23.976 counts 24 a second
    ("10:00:00:00", Fraction(25), 900000),
    ("01:00:00;00", Fraction(25), 90000),                # no drop-frame at 25: ; is NDF
])
def test_a_label_names_its_frame(label, rate, frames):
    assert smpte_frames(label, rate) == frames


@pytest.mark.parametrize("label, rate", [
    ("00:00:00:30", Fraction(30)),          # frame 30 does not exist at 30
    ("00:01:00;00", Fraction(30000, 1001)),  # dropped label
    ("01:00:00", Fraction(30)),
    ("aa:00:00:00", Fraction(30)),
])
def test_a_label_that_names_no_frame_is_refused(label, rate):
    with pytest.raises(ValueError):
        smpte_frames(label, rate)


def _probe(*streams, fmt="mov,mp4,m4a,3gp,3g2,mj2", tags=None):
    return {"streams": list(streams), "format": {"format_name": fmt, "tags": tags or {}}}


VIDEO = {"codec_type": "video", "codec_tag_string": "avc1", "r_frame_rate": "30/1"}


def test_the_tmcd_track_sets_the_clock():
    """The shape ffprobe prints for the #45 iPhone clip: the tmcd track's rate
    is its avg_frame_rate (15360/512 is 30)."""
    clock = clock_from_probe(_probe(
        {**VIDEO, "tags": {"timecode": "18:03:22:05"}},
        {"codec_type": "data", "codec_tag_string": "tmcd", "avg_frame_rate": "15360/512",
         "tags": {"timecode": "18:03:22:05"}},
    ))
    assert clock.source == TMCD
    assert clock.origin == Fraction(1950065, 30)
    assert clock.start_attr == "1950065/30s"
    assert clock.place(Fraction(5)) == Fraction(1950215, 30)


def test_ntsc_timecode_runs_on_the_real_frame_length():
    """01:00:00:00 non-drop at 29.97 is 108000 frames of 1001/30000s: 3603.6s,
    not 3600s. Drop-frame 01:00:00;00 is 107892 of them: 3599.9964s."""
    ndf = clock_from_probe(_probe(
        {"codec_type": "data", "codec_tag_string": "tmcd", "avg_frame_rate": "30000/1001",
         "tags": {"timecode": "01:00:00:00"}}))
    df = clock_from_probe(_probe(
        {"codec_type": "data", "codec_tag_string": "tmcd", "avg_frame_rate": "30000/1001",
         "tags": {"timecode": "01:00:00;00"}}))
    assert ndf.origin == Fraction(36036, 10) and ndf.start_attr == "108108000/30000s"
    assert df.origin == Fraction(107892 * 1001, 30000) and df.start_attr == "107999892/30000s"
    assert df.drop and not ndf.drop


def test_a_sony_timecode_outside_tmcd_is_reported_and_not_used():
    """Sony XAVC .mp4: the timecode is in an rtmd track (and ffprobe copies it
    onto the video stream); Final Cut starts the file at 0."""
    clock = clock_from_probe(_probe(
        {**VIDEO, "codec_tag_string": "avc1", "tags": {"timecode": "14:22:31:07"}},
        {"codec_type": "data", "codec_tag_string": "rtmd", "tags": {"timecode": "14:22:31:07"}},
    ))
    assert clock.source == IGNORED and clock.origin == 0 and clock.start_attr == "0s"
    assert clock.timecode == "14:22:31:07"
    assert "outside a QuickTime timecode track" in clock.note and "0s" in clock.note


def test_a_file_without_timecode_starts_at_0():
    clock = clock_from_probe(_probe(VIDEO))
    assert clock.source == NONE and clock.start_attr == "0s" and clock.quicktime


def test_a_timecode_track_with_no_readable_start_is_not_guessed():
    clock = clock_from_probe(_probe({"codec_type": "data", "codec_tag_string": "tmcd"}))
    assert clock.source == UNREAD and clock.origin == 0 and clock.note
    # a label with no rate anywhere (no tmcd rate, no video stream): unread,
    # never a frame count divided by a guessed rate
    clock = clock_from_probe(_probe({"codec_type": "data", "codec_tag_string": "tmcd",
                                     "tags": {"timecode": "01:00:00:00"}}))
    assert clock.source == UNREAD and clock.origin == 0


def test_a_missing_file_or_ffprobe_is_unread_with_the_reason(tmp_path, monkeypatch):
    assert read_clock(str(tmp_path / "gone.mov")).note == "the file is not on this machine"
    (tmp_path / "here.mov").write_bytes(b"\x00")
    monkeypatch.setattr(media_clock.shutil, "which", lambda name: None)
    clock = read_clock(str(tmp_path / "here.mov"))
    assert clock.source == UNREAD and clock.note == "ffprobe is not installed"


def test_media_url_is_the_percent_encoded_form_final_cut_writes():
    """Final Cut's own export writes a space as %20 and UTF-8 as %XX."""
    url = media_url("/Volumes/素材 Day 1/iphone clip.mov")
    assert url == "file:///Volumes/%E7%B4%A0%E6%9D%90%20Day%201/iphone%20clip.mov"
    assert media_src_to_path(url) == "/Volumes/素材 Day 1/iphone clip.mov"
    assert media_url("file:///already/a%20url.mov") == "file:///already/a%20url.mov"
    assert media_url("relative/clip.mov") == "relative/clip.mov"


# --- real files ------------------------------------------------------------------


@needs_ffmpeg
def test_real_files_read_the_clock_ffmpeg_wrote(media):
    assert read_clock(str(media["iphone"])).start_attr == "1950065/30s"
    assert read_clock(str(media["ndf"])).start_attr == "108108000/30000s"
    assert read_clock(str(media["df"])).start_attr == "107999892/30000s"
    assert read_clock(str(media["film"])).start_attr == "86486400/24000s"
    assert read_clock(str(media["plain"])).source == NONE


@needs_ffmpeg
def test_a_matroska_timecode_tag_is_found_whatever_its_case(media):
    """ffprobe prints a Matroska tag as TIMECODE. Outside QuickTime there is no
    evidence for how Final Cut clocks the file, so it is "not determined",
    never claimed to start at 0."""
    mkv = media["dir"] / "tagged.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1",
         "-c:v", "mpeg4", "-metadata:s:v:0", "timecode=01:02:03:04", str(mkv)],
        capture_output=True, timeout=60, check=True,
    )
    clock = read_clock(str(mkv))
    assert clock.source == UNREAD and clock.timecode == "01:02:03:04" and clock.origin == 0
    assert "matroska" in clock.note and "not verified" in clock.note


@needs_ffmpeg
def test_mxf_and_broadcast_wav_start_on_their_own_clock(media):
    """Checked in Final Cut 12.4 (2026-11-16): an MXF timecode and a BWF time
    reference are the file's clock. Written at 0s, Final Cut warned "Invalid
    edit with no respective media" and imported the clip empty; written at
    01:00:00 (3600s) both imported with media."""
    mxf = media["dir"] / "tc.mxf"
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1",
                    "-c:v", "mpeg2video", "-timecode", "01:00:00:00", str(mxf)],
                   capture_output=True, timeout=60, check=True)
    wav = media["dir"] / "bwf.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=d=1:sample_rate=48000", "-c:a", "pcm_s16le", "-write_bext", "1",
                    "-metadata", "time_reference=172800000", str(wav)],
                   capture_output=True, timeout=60, check=True)
    m = read_clock(str(mxf))
    w = read_clock(str(wav))
    assert m.source == "mxf" and m.on_clock and m.timecode == "01:00:00:00" and m.start_attr == "3600s"
    assert w.source == "bwf" and w.on_clock and w.origin == 3600 and w.rate == 48000
    # samples / rate, not a frame count: 1 h at 44.1 kHz is also 3600 s
    w441 = clock_from_probe(_probe({"codec_type": "audio", "sample_rate": "44100"},
                                   fmt="wav", tags={"time_reference": "158760000"}))
    assert w441.origin == 3600 and w441.source == "bwf"


def test_a_broadcast_wav_with_no_sample_rate_is_not_guessed():
    clock = clock_from_probe(_probe({"codec_type": "audio"},
                                    fmt="wav", tags={"time_reference": "48000"}))
    assert clock.source == UNREAD and not clock.on_clock and clock.start_attr == "0s"


def test_a_2997_over_100_rate_is_the_ntsc_rate_it_stands_for():
    """Some files carry 29.97 as 2997/100: drop-frame still applies, and the
    origin lands on Final Cut's 30000 timebase."""
    clock = clock_from_probe(_probe(
        {"codec_type": "data", "codec_tag_string": "tmcd", "avg_frame_rate": "2997/100",
         "tags": {"timecode": "01:00:00;00"}}))
    assert clock.drop and clock.rate == Fraction(30000, 1001)
    assert clock.start_attr == "107999892/30000s"


# --- import_edl_json: the issue, verbatim in shape -------------------------------


def _edl(tmp_path, sources, ranges):
    payload = tmp_path / "edl.json"
    payload.write_text(json.dumps({"sources": sources, "ranges": ranges}))
    return payload


@needs_ffmpeg
def test_the_45_repro_writes_the_asset_and_its_clips_on_the_timecode(media, tmp_path):
    """The issue's two ranges (5-10 and 20-25 there; 1-2 and 3-4 of a 6s file
    here): the asset at 1950065/30s and each clip at its file time on it."""
    out = tmp_path / "edl.fcpxml"
    result = edl_to_fcpxml(
        {"sources": {"iphone": str(media["iphone"])},
         "ranges": [{"source": "iphone", "start": 1, "end": 2},
                    {"source": "iphone", "start": 3, "end": 4}]},
        str(out), fps=30,
    )
    asset = _assets(out)["IMG_1968"]
    assert asset.get("start") == "1950065/30s"                 # was 0s
    assert _clip_starts(out) == ["1950095/30s", "1950155/30s"]  # was 1s, 3s
    assert result["clocks"][0]["clock"].timecode == "18:03:22:05"
    _dtd_valid(out)


@needs_ffmpeg
def test_with_or_without_timecode_the_same_frames_of_the_file_are_cut(media, tmp_path):
    """Read back through the parser, every clip names the same FILE time from
    either file: the write side and the read side (0.29's file_in_point)
    agree, so preview, silence, scenes and transcripts see the right frames."""
    for key in ("iphone", "plain", "ndf", "film", "df"):
        out = tmp_path / f"{key}.fcpxml"
        edl_to_fcpxml({"sources": {"s": str(media[key])},
                       "ranges": [{"source": "s", "start": 1, "end": 2},
                                  {"source": "s", "start": 3, "end": 4}]},
                      str(out), fps=30)
        clips = FCPXMLParser().parse_file(str(out)).primary_timeline.clips
        points = [file_in_point(c) for c in clips]
        if key == "df":
            # 01:00:00;00 is 3599.9964s, off the 30p grid every clip start is
            # read on: snapped later, by under a frame, never before the file.
            assert all(Fraction(0) <= p - want < Fraction(1, 30)
                       for p, want in zip(points, (1, 3))), points
        else:
            assert points == [Fraction(1), Fraction(3)], (key, points)


@needs_ffmpeg
def test_ntsc_media_in_a_film_sequence_is_still_exact(media, tmp_path):
    """A 29.97 drop-frame file cut in a 23.976 sequence: no grid holds both
    clocks, so the asset keeps its own (107999892/30000s, exact) and the clip
    start goes on the sequence's frames, within one of the in-point and
    never before it; written over standard timebases, DTD-valid."""
    out = tmp_path / "mixed.fcpxml"
    edl_to_fcpxml({"sources": {"s": str(media["df"])},
                   "ranges": [{"source": "s", "start": 1, "end": 2}]},
                  str(out), fps=24000 / 1001)
    assert next(iter(_assets(out).values())).get("start") == "107999892/30000s"
    clip = FCPXMLParser().parse_file(str(out)).primary_timeline.clips[0]
    frame = Fraction(1001, 24000)
    assert Fraction(0) <= file_in_point(clip) - 24 * frame < frame   # 24 frames in
    assert _clip_starts(out)[0].endswith("/24000s")
    _dtd_valid(out)


@needs_ffmpeg
def test_a_folder_with_a_space_and_chinese_round_trips(media, tmp_path):
    out = tmp_path / "named.fcpxml"
    edl_to_fcpxml({"sources": {"s": str(media["named"])},
                   "ranges": [{"source": "s", "start": 0, "end": 1}]}, str(out), fps=30)
    src = next(iter(_assets(out).values())).find("media-rep").get("src")
    assert " " not in src and "素材" not in src and "%20" in src
    clip = FCPXMLParser().parse_file(str(out)).primary_timeline.clips[0]
    assert media_src_to_path(clip.media_path) == str(media["named"])
    assert _assets(out)["iphone clip"].get("start") == "1950065/30s"
    _dtd_valid(out)


def test_missing_media_is_written_at_0s_and_says_what_that_risks(tmp_path):
    payload = _edl(tmp_path, {"a": str(tmp_path / "gone.mov")}, [{"source": "a", "start": 0, "end": 1}])
    text = asyncio.run(server.handle_group("generate", {
        "action": "import_edl_json", "args": {"filepath": str(payload), "fps": 30}}))[0].text
    assert _assets(tmp_path / "edl.fcpxml")["gone"].get("start") == "0s"
    assert "Clock not read, written at 0s: gone.mov (the file is not on this machine)." in text
    assert "Final Cut shows its clips empty" in text


@needs_ffmpeg
def test_the_reply_names_the_clock_it_wrote(media, tmp_path):
    payload = _edl(tmp_path, {"iphone": str(media["iphone"]), "plain": str(media["plain"])},
                   [{"source": "iphone", "start": 1, "end": 2}, {"source": "plain", "start": 0, "end": 1}])
    text = asyncio.run(server.handle_group("generate", {
        "action": "import_edl_json", "args": {"filepath": str(payload), "fps": 30}}))[0].text
    assert ("On its timecode clock: IMG_1968.mov starts at 18:03:22:05 (30 fps), "
            "so <asset start> is 1950065/30s") in text
    assert "plain.mov" not in text      # no timecode, nothing to say
    assert "Clock not read" not in text


# --- the other writers that make a clip ----------------------------------------------


@needs_ffmpeg
def test_apply_template_puts_every_slot_on_its_files_clock(media, tmp_path):
    out = str(tmp_path / "tmpl.fcpxml")
    apply_template("intro_outro", {
        "intro_card": ClipSpec(src=str(media["plain"]), name="Intro", duration=1.0),
        "main_content": ClipSpec(src=str(media["iphone"]), name="Main", duration=2.0),
        "end_card": ClipSpec(src=str(media["plain"]), name="End", duration=1.0),
        "music_bed": ClipSpec(src=str(media["ndf"]), name="Bed", duration=2.0),
    }, out, fps=30)
    assets = _assets(out)
    assert assets["Main"].get("start") == "1950065/30s"
    assert assets["Bed"].get("start") == "108108000/30000s"
    assert assets["Intro"].get("start") == "0s"
    import xml.etree.ElementTree as ET

    from fcpxml.rational import parse_seconds
    clips = {c.get("name"): parse_seconds(c.get("start")) for c in ET.parse(out).getroot().iter("asset-clip")}
    assert clips == {"Intro": 0, "Main": Fraction(1950065, 30), "End": 0, "Bed": Fraction(36036, 10)}
    assert assets["Main"].find("media-rep").get("src").startswith("file:///")
    _dtd_valid(out)


def _timecoded_project(tmp_path, src="file:///nowhere/cam.mov") -> str:
    """A Final Cut-shaped document whose one asset starts at 01:00:00:00."""
    path = tmp_path / "cam.fcpxml"
    path.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13"><resources>
<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
<asset id="a1" name="Cam" start="3600s" duration="10s" hasVideo="1" hasAudio="1" format="r1"><media-rep kind="original-media" src="{src}"/></asset>
<asset id="a2" name="Music" start="3600s" duration="10s" hasAudio="1"><media-rep kind="original-media" src="file:///nowhere/music.mov"/></asset>
</resources><library><event name="E"><project name="P"><sequence format="r1" duration="4s" tcStart="0s"><spine>
<asset-clip ref="a1" offset="0s" name="Cam" start="3601s" duration="4s" format="r1"/>
</spine></sequence></project></event></library></fcpxml>''')
    return str(path)


def test_a_clip_made_from_a_timecoded_asset_starts_where_the_asset_does(tmp_path):
    """insert_clip, add_connected_clip and add_audio_clip with no in-point
    wrote start="0s" on an asset whose clock starts at 3600s: an hour before
    the file, which Final Cut shows empty."""
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    inserted = m.insert_clip(position="end", asset_id="a1", duration="2s")
    connected = m.add_connected_clip(parent_clip_id="Cam", asset_id="a1", duration="1s")
    audio = m.add_audio_clip(parent_clip_id="Cam", asset_id="a2", duration="2s")
    assert [inserted.get("start"), connected.get("start"), audio.get("start")] == ["3600s"] * 3


def test_a_whole_asset_clip_spans_the_asset_on_its_clock(tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    whole = m.insert_clip(position="end", asset_id="a1")
    assert (whole.get("start"), whole.get("duration")) == ("3600s", "10s")


@needs_ffmpeg
def test_a_clip_is_never_snapped_to_before_its_file_begins(media, tmp_path):
    """The file's clock is not on the 30p grid and its nearest frame is
    earlier: the clip goes to the next frame, never a sliver before the file."""
    out = str(tmp_path / "pal.fcpxml")
    apply_template("intro_outro", {
        "intro_card": ClipSpec(src=str(media["pal"]), name="Intro", duration=1.0),
        "main_content": ClipSpec(src=str(media["plain"]), name="Main", duration=1.0),
        "end_card": ClipSpec(src=str(media["plain"]), name="End", duration=1.0),
    }, out, fps=30)
    from fcpxml.rational import parse_seconds
    asset = parse_seconds(_assets(out)["Intro"].get("start"))
    start = parse_seconds(_clip_starts(out)[0])
    assert asset == Fraction(900001, 25)
    assert asset <= start < asset + Fraction(1, 30) and (start * 30).denominator == 1

    edl = tmp_path / "pal_edl.fcpxml"
    edl_to_fcpxml({"sources": {"s": str(media["pal"])},
                   "ranges": [{"source": "s", "start": 0, "end": 1}]}, str(edl), fps=30)
    start = parse_seconds(_clip_starts(edl)[0])
    assert asset <= start < asset + Fraction(1, 30)


def test_an_in_point_before_the_asset_begins_is_refused_with_the_file_time(tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    with pytest.raises(ValueError, match=r"before asset 'Cam' begins: its clock starts at 3600s.*2s into the file is 3602s"):
        m.insert_clip(position="end", asset_id="a1", in_point="2s", out_point="3s")
    ok = m.insert_clip(position="end", asset_id="a1", in_point="3602s", out_point="3603s")
    assert ok.get("start") == "3602s" and ok.get("duration") == "1s"


def test_the_audio_clamp_measures_the_asset_on_its_own_clock(tmp_path):
    """The asset is 10s from 3600s: a 20s request is clamped to 10s, not
    refused as starting 3590s past its end."""
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    audio = m.add_audio_clip(parent_clip_id="Cam", asset_id="a2", duration="20s")
    assert audio.get("start") == "3600s" and audio.get("duration") == "10s"


@needs_ffmpeg
def test_template_and_audio_replies_name_the_clock_they_wrote(media, tmp_path, monkeypatch):
    """Every writer that reads a clock says so; a silent 0s is how #45 hid."""
    monkeypatch.setattr(server, "PROJECTS_DIR", str(tmp_path))
    text = asyncio.run(server.call_tool("generate", {
        "action": "apply_template", "template_name": "intro_outro", "fps": 30,
        "output_path": str(tmp_path / "t.fcpxml"),
        "clips": {"intro_card": {"src": str(media["plain"]), "name": "Intro"},
                  "main_content": {"src": str(media["iphone"]), "name": "Main"},
                  "end_card": {"src": str(tmp_path / "gone.mov"), "name": "End"}}}))[0].text
    assert "On its timecode clock: IMG_1968.mov starts at 18:03:22:05" in text
    assert "Clock not read, written at 0s: gone.mov (the file is not on this machine)." in text

    project = _timecoded_project(tmp_path)
    text = asyncio.run(server.call_tool("edit", {
        "action": "add_audio", "filepath": project, "parent_clip_id": "Cam",
        "src": str(media["ndf"]), "duration": "1s"}))[0].text
    assert "On its timecode clock: ntsc_ndf.mov starts at 01:00:00:00 (29.97 fps)" in text


@needs_ffmpeg
def test_a_generated_clip_lands_on_its_files_clock(media, tmp_path):
    """gen's insert: generated media has no timecode track and stays at 0s;
    a file that carries one is played from it, never from 0s."""
    from fcpxml import gen

    m = FCPXMLModifier(_timecoded_project(tmp_path))
    probe = gen.probe_media(str(media["iphone"]))
    gen.insert_generated(m, media_path=str(media["iphone"]), probe=probe, at=Fraction(1),
                         duration=Fraction(2), name="Gen", prompt="p", provider_label="test")
    out = str(tmp_path / "gen.fcpxml")
    m.save(out)
    assert _assets(out)["Gen"].get("start") == "1950065/30s"
    clip = next(c for c in FCPXMLParser().parse_file(out).primary_timeline.connected_clips if c.name == "Gen")
    assert file_in_point(clip.as_clip()) == 0


def test_writing_a_parsed_project_keeps_each_assets_clock(tmp_path):
    """FCPXMLWriter rebuilt every asset at 0s with a bare path, so a parsed
    camera clip (asset at 3600s, clip at 3601s) came back an hour early."""
    from fcpxml.writer import FCPXMLWriter

    project = FCPXMLParser().parse_file(_timecoded_project(tmp_path, src="file:///Volumes/A%20Cam/cam.mov"))
    out = str(tmp_path / "rewritten.fcpxml")
    FCPXMLWriter().write_project(project, out)
    from fcpxml.rational import parse_seconds
    asset = next(iter(_assets(out).values()))
    assert asset.get("start") == "3600s" and [parse_seconds(v) for v in _clip_starts(out)] == [3601]
    assert asset.get("src") == "file:///Volumes/A%20Cam/cam.mov"


@needs_ffmpeg
def test_validate_judges_an_mxf_on_its_timecode(media, tmp_path):
    """The tool writes an MXF on its timecode (PASS); the same clip at 0s is
    the empty import Final Cut showed (FAIL)."""
    mxf = media["dir"] / "tc.mxf"
    if not mxf.exists():
        pytest.skip("made by the MXF test")
    out = tmp_path / "mxf.fcpxml"
    edl_to_fcpxml({"sources": {"s": str(mxf)}, "ranges": [{"source": "s", "start": 0, "end": 1}]}, str(out), fps=25)
    assert _row(_validate(out)) == "PASS"
    text = out.read_text().replace('start="3600s"', 'start="0s"')
    zero = tmp_path / "mxf0.fcpxml"
    zero.write_text(text)
    assert _row(_validate(zero)) == "FAIL"


@needs_ffmpeg
def test_add_audio_from_a_timecoded_file_writes_the_asset_on_its_clock(media, tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    clip = m.add_audio_clip(parent_clip_id="Cam", src=str(media["ndf"]), duration="2s")
    asset = m.resources[clip.get("ref")]["element"]
    assert asset.get("start") == "108108000/30000s"
    assert clip.get("start") == "108108000/30000s"
    assert asset.find("media-rep").get("src") == media["ndf"].as_uri()


# --- diagnose: the check validate_timeline did not have ---------------------------------


def _validate(path) -> str:
    return asyncio.run(server.call_tool("diagnose", {"action": "validate_timeline", "filepath": str(path)}))[0].text


def _row(text, name="Media clock") -> str:
    """The verdict of a check's row ("PASS", "WARN", ...), its icon dropped."""
    row = next(r for r in text.splitlines() if f"| {name} |" in r)
    return row.split("|")[1].split()[-1]


@needs_ffmpeg
def test_validate_fails_the_empty_timeline_45_describes(media, tmp_path):
    """0.32.0's output for the issue: asset at 0s on a file that starts at
    18:03:22:05. It passed; Final Cut imported it empty."""
    path = tmp_path / "old.fcpxml"
    edl_to_fcpxml({"sources": {"iphone": str(media["iphone"])},
                   "ranges": [{"source": "iphone", "start": 1, "end": 2}]}, str(path), fps=30)
    fixed = path.read_text()
    path.write_text(fixed.replace('start="1950065/30s"', 'start="0s"').replace('start="1950095/30s"', 'start="1s"'))
    text = _validate(path)
    assert _row(text) == "FAIL" and "IMG_1968.mov" in text
    assert "by the file's timecode track, its first frame is 18:03:22:05 (1950065/30s)" in text
    assert "Media clock: write each asset's start as its file's timecode" in text

    (tmp_path / "new.fcpxml").write_text(fixed)
    assert _row(_validate(tmp_path / "new.fcpxml")) == "PASS"


@needs_ffmpeg
def test_validate_passes_ntsc_assets_written_on_their_clock(media, tmp_path):
    """The check and the writer share one NTSC rule; an hour of 29.97 is
    3603.6s on both sides, so a correct document is never failed."""
    for key in ("ndf", "df", "film"):
        out = tmp_path / f"{key}.fcpxml"
        edl_to_fcpxml({"sources": {"s": str(media[key])},
                       "ranges": [{"source": "s", "start": 1, "end": 2}]}, str(out), fps=30)
        assert _row(_validate(out)) == "PASS", key


@needs_ffmpeg
def test_validate_warns_on_a_clock_the_file_does_not_have(media, tmp_path):
    """The other way round: a QuickTime file with no timecode track written
    at 3600s. Final Cut starts it at 0, so these clips are past its end."""
    path = Path(_timecoded_project(tmp_path, src=media["plain"].as_uri()))
    text = _validate(path)
    assert _row(text) == "WARN"
    assert "plain.mov: <asset start> is 3600s but the file has no QuickTime timecode track" in text


def test_without_ffprobe_the_clock_is_not_measured_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    text = _validate(_timecoded_project(tmp_path))
    assert _row(text) == "INFO" and "not measured: ffprobe is not installed" in text


# --- 0.33 "not considering", done: any file in, exact source starts, DF labels ---


@needs_ffmpeg
def test_insert_clip_brings_any_file_in_on_its_own_clock(media, tmp_path, monkeypatch):
    """Footage made anywhere (a camera, a generator) comes in by its path:
    a new asset probed for length, rate and size, on its timecode clock."""
    project = _timecoded_project(tmp_path)
    text = asyncio.run(server.call_tool("edit", {
        "action": "insert_clip", "filepath": project, "position": "end",
        "src": str(media["iphone"]), "output_path": str(tmp_path / "in.fcpxml")}))[0].text
    assert "On its timecode clock: IMG_1968.mov starts at 18:03:22:05" in text
    out = tmp_path / "in.fcpxml"
    asset = _assets(out)["IMG_1968"]
    assert asset.get("start") == "1950065/30s" and asset.get("duration") == "6s"
    assert asset.find("media-rep").get("src") == media["iphone"].as_uri()
    import xml.etree.ElementTree as ET
    fmt = next(f for f in ET.parse(str(out)).getroot().iter("format") if f.get("id") == asset.get("format"))
    assert (fmt.get("frameDuration"), fmt.get("width"), fmt.get("height")) == ("1/30s", "160", "90")
    clip = FCPXMLParser().parse_file(str(out)).primary_timeline.clips[-1]
    assert clip.name == "IMG_1968" and file_in_point(clip) == 0 and clip.duration.seconds == 6
    _dtd_valid(out)


@needs_ffmpeg
def test_add_connected_clip_brings_b_roll_in_by_path(media, tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    clip = m.add_connected_clip(parent_clip_id="Cam", src=str(media["plain"]), duration="2s", lane=2)
    assert clip.get("lane") == "2" and clip.get("start") == "0s" and clip.get("duration") == "2s"
    assert m.resources[clip.get("ref")]["element"].get("start") == "0s"
    assert [e["clock"].source for e in m.media_clocks] == [NONE]


def test_a_src_that_is_missing_or_has_no_picture_is_refused(tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    with pytest.raises(ValueError, match="src not found"):
        m.insert_clip(position="end", src=str(tmp_path / "gone.mov"))
    if not FFMPEG:
        return
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=d=1", str(wav)], capture_output=True, timeout=60, check=True)
    with pytest.raises(ValueError, match="no picture ffprobe could read.*add_audio"):
        m.add_connected_clip(parent_clip_id="Cam", src=str(wav))


def test_src_and_an_asset_together_is_refused_not_one_dropped(tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    with pytest.raises(ValueError, match="src or asset_id/asset_name, not both"):
        m.insert_clip(position="end", asset_id="a1", src=str(tmp_path / "x.mov"), duration="1s")
    with pytest.raises(ValueError, match="not both"):
        m.add_connected_clip(parent_clip_id="Cam", asset_name="Cam", src=str(tmp_path / "x.mov"))


@needs_ffmpeg
def test_a_file_brought_in_whole_ends_on_a_sequence_frame(media, tmp_path):
    """A 29.97 file in a 30p cut runs 179 x 1001/30000s: as a clip it is
    floored to whole 30p frames, so the clip after it stays on the grid."""
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    clip = m.insert_clip(position="start", src=str(media["ndf"]))
    from fcpxml.rational import parse_seconds
    length = parse_seconds(clip.get("duration"))
    asset = m.resources[clip.get("ref")]
    assert (length * 30).denominator == 1 and length <= parse_seconds(asset["duration"])
    assert parse_seconds(asset["duration"]) - length < Fraction(1, 30)
    assert clip.get("start") == "108108000/30000s"            # the start stays exact
    assert clip.get("format") == asset["element"].get("format")
    cam = next(c for c in m.root.iter("asset-clip") if c.get("name") == "Cam")
    assert (parse_seconds(cam.get("offset")) * 30).denominator == 1


@needs_ffmpeg
def test_the_same_file_twice_is_one_asset(media, tmp_path):
    m = FCPXMLModifier(_timecoded_project(tmp_path))
    a = m.insert_clip(position="end", src=str(media["plain"]), duration="1s")
    b = m.add_connected_clip(parent_clip_id="Cam", src=str(media["plain"]), duration="1s")
    assert a.get("ref") == b.get("ref") and len(m.media_clocks) == 1
    out = str(tmp_path / "twice.fcpxml")
    m.save(out)
    assert list(_assets(out)).count("plain") == 1


def test_writing_a_parsed_project_keeps_an_exact_source_start(tmp_path):
    from fcpxml.writer import FCPXMLWriter
    path = tmp_path / "df.fcpxml"
    path.write_text(_DF_DOC)
    out = str(tmp_path / "df_out.fcpxml")
    FCPXMLWriter().write_project(FCPXMLParser().parse_file(str(path)), out)
    assert _clip_starts(out) == ["107999892/30000s"]


_DF_DOC = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13"><resources>
<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
<asset id="a1" name="DF" start="107999892/30000s" duration="10s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file:///nowhere/df.mov"/></asset>
</resources><library><event name="E"><project name="P"><sequence format="r1" duration="2s" tcStart="0s"><spine>
<asset-clip ref="a1" offset="0s" name="DF" start="107999892/30000s" duration="2s" format="r1"/>
</spine></sequence></project></event></library></fcpxml>'''


def test_a_source_start_off_the_sequence_grid_is_read_exactly(tmp_path):
    """A clip at the head of a 29.97 drop-frame asset in a 30p sequence starts
    at 107999892/30000s: read as whole 30p frames it was 0.03s before its
    file began. Equality of Timecodes is still about frames."""
    path = tmp_path / "df.fcpxml"
    path.write_text(_DF_DOC)
    clip = FCPXMLParser().parse_file(str(path)).primary_timeline.clips[0]
    assert file_in_point(clip) == 0                      # was -0.0297s
    assert clip.source_start.seconds == float(Fraction(107999892, 30000))
    from fcpxml.models import Timecode
    assert clip.source_start == Timecode(frames=clip.source_start.frames, frame_rate=clip.source_start.frame_rate)


@pytest.mark.parametrize("label, fps, frames", [
    ("01:00:00;00", 29.97, 107892),
    ("00:01:00;02", 29.97, 1800),
    ("00:10:00;00", 29.97, 17982),
    ("01:00:00;00", 59.94, 215784),
    ("01:00:00:00", 29.97, 108000),    # non-drop unchanged
    ("01:00:00;00", 25, 90000),        # no drop-frame at 25
])
def test_a_typed_drop_frame_label_is_counted_as_drop_frame(label, fps, frames):
    """TimeValue.from_timecode read ";" as ":": 01:00:00;00 came out 3.6s late."""
    from fcpxml.models import TimeValue
    from fcpxml.rational import frame_duration_seconds
    tv = TimeValue.from_timecode(label, fps)
    assert Fraction(tv.numerator, tv.denominator) == frames * frame_duration_seconds(fps)


def test_a_drop_frame_label_that_does_not_exist_is_refused():
    from fcpxml.models import TimeValue
    with pytest.raises(ValueError, match="does not exist in drop-frame"):
        TimeValue.from_timecode("00:01:00;00", 29.97)


def test_drop_frame_labels_print_and_read_back_as_the_same_frame():
    from fcpxml.media_clock import smpte_label
    from fcpxml.models import Timecode
    for rate, fps in ((Fraction(30000, 1001), 29.97), (Fraction(60000, 1001), 59.94)):
        for f in range(0, 120000, 37):
            label = Timecode(frames=f, frame_rate=fps, drop_frame=True).to_smpte()
            assert smpte_frames(label, rate) == f, (fps, f, label)
    assert smpte_label(1800, Fraction(30000, 1001), drop=True) == "00:01:00;02"
    assert Timecode(frames=48, frame_rate=24.0, drop_frame=True).to_smpte() == "00:00:02;00"


@needs_ffmpeg
def test_diagnose_probes_each_file_once(media, tmp_path, monkeypatch):
    """Frame rate and size and the media clock read one ffprobe per file."""
    from fcpxml import render
    calls = []
    real = render.stream_facts
    monkeypatch.setattr(render, "stream_facts", lambda path: calls.append(path) or real(path))
    out = tmp_path / "once.fcpxml"
    edl_to_fcpxml({"sources": {"a": str(media["iphone"]), "b": str(media["plain"])},
                   "ranges": [{"source": "a", "start": 0, "end": 1}, {"source": "b", "start": 0, "end": 1},
                              {"source": "a", "start": 2, "end": 3}]}, str(out), fps=30)
    _validate(out)
    assert sorted(calls) == sorted({str(media["iphone"]), str(media["plain"])})


def test_validation_checks_alignment_at_the_sequences_own_rate(tmp_path):
    """A 29.97 timeline warned "not frame-aligned at 24fps" on every clip
    (2026-11-16): the validator assumed 24. It now reads the sequence."""
    import xml.etree.ElementTree as ET

    from fcpxml.writer import validate_fcpxml

    def doc(duration):
        return ET.fromstring(
            '<fcpxml version="1.13"><resources>'
            '<format id="r1" frameDuration="1001/30000s" width="1280" height="720"/>'
            '</resources><library><event name="e"><project name="p">'
            '<sequence format="r1" duration="180180/30000s"><spine>'
            f'<gap name="g" offset="0s" duration="{duration}"/>'
            '</spine></sequence></project></event></library></fcpxml>')

    def misaligned(root):
        return [i for i in validate_fcpxml(root) if "frame-aligned" in i.message]

    assert misaligned(doc("180180/30000s")) == []        # 180 frames at 29.97
    bad = misaligned(doc("1/48s"))
    assert bad and "29.97" in bad[0].message


def test_the_same_file_gets_the_same_asset_uid_on_every_write(tmp_path):
    """Final Cut refuses a second import of a file under another uid, so a
    revised cut pushed into the same library failed (2026-11-16). One file,
    one uid; a changed file, a new one; a file not here, a random one."""
    from fcpxml.writer import stable_media_uid
    clip = tmp_path / "a.mov"
    clip.write_bytes(b"x" * 10)
    first = stable_media_uid(str(clip))
    assert first == stable_media_uid(clip.as_uri()) == stable_media_uid(str(clip))
    clip.write_bytes(b"y" * 11)
    assert stable_media_uid(str(clip)) != first
    missing = str(tmp_path / "gone.mov")
    assert stable_media_uid(missing) != stable_media_uid(missing)


def test_two_writes_of_one_cut_share_the_asset_uid(media, tmp_path):
    import re
    uids = []
    for i in (1, 2):
        out = tmp_path / f"v{i}.fcpxml"
        edl_to_fcpxml({"sources": {"a": str(media["ndf"])}, "ranges": [{"source": "a", "start": 0, "end": i}]},
                      str(out), fps=30)
        uids.append(re.findall(r'<asset [^>]*uid="([^"]+)"', out.read_text()))
    assert uids[0] == uids[1] and len(uids[0]) == 1
