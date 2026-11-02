"""Every tool that reads media reads it on the FILE's clock (0.29.0).

A clip's ``start`` is on its asset's clock, which begins at ``<asset start>``:
Final Cut writes a camera clip with 01:00:00:00 timecode as an asset with
``start="3600s"`` and clips with ``start="3602s"``. ffmpeg, Whisper and
PySceneDetect measure from 0. Compared directly, a timecoded clip had no
silence, no scene cut and no transcript word in it, and every tool reported
that as "nothing found".

Each test runs the SAME eight seconds of media twice: once as a file with no
timecode (asset start 0s) and once with timecode (asset start 3600s), and
asserts the two answers match. 0.28.0 fixed the renderer; this is the rest of
the category.
"""

import asyncio
import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import server
from fcpxml import media_intel
from fcpxml.models import MediaRef, Timecode
from fcpxml.parser import FCPXMLParser

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture(scope="module")
def footage(tmp_path_factory):
    """Bars for 4s then red for 4s (one hard cut at 4s); a tone with 2s-4s
    silent. Written twice: with a 01:00:00:00 timecode track and without."""
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("clock")
    _MEDIA_DIR["d"] = d
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "smptebars=size=320x180:rate=30:duration=4",
         "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30:duration=4",
         "-f", "lavfi", "-i", "sine=f=440:d=8,volume='if(between(t,2,4),0,1)':eval=frame",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
         "-c:v", "prores_ks", "-profile:v", "0", "-c:a", "pcm_s16le",
         "-timecode", "01:00:00:00", str(d / "tc.mov")],
        capture_output=True, timeout=120, check=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
         "-i", str(d / "tc.mov"), "-map", "0:v", "-map", "0:a", "-c", "copy",
         "-write_tmcd", "0", str(d / "notc.mov")],
        capture_output=True, timeout=120, check=True,
    )
    return d


_MEDIA_DIR = {}


def _project(d, name, timecoded, in_point=1):
    """One 6s clip starting 1s into the file: it holds the 2s-4s silence and
    the cut at 4s (timeline 3s)."""
    origin = 3600 if timecoded else 0
    media = "tc.mov" if timecoded else "notc.mov"
    path = Path(d) / f"{name}.fcpxml"
    path.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14"><resources>
<format id="r1" name="FFVideoFormat1080p30" frameDuration="100/3000s" width="1920" height="1080"/>
<asset id="a1" name="Cam" start="{origin}s" duration="8s" hasVideo="1" hasAudio="1" format="r1" audioSources="1" audioChannels="1" audioRate="44100"><media-rep kind="original-media" src="file://{_MEDIA_DIR['d']}/{media}"/></asset>
</resources><library><event name="E"><project name="{name}"><sequence format="r1" duration="6s" tcStart="0s"><spine>
<asset-clip ref="a1" offset="0s" name="Cam" start="{origin + in_point}s" duration="6s" format="r1"/>
</spine></sequence></project></event></library></fcpxml>''')
    return str(path)


def _cut_length(path):
    """Seconds of picture left on the spine (the sequence's own duration
    attribute is not what a ripple cut is measured by)."""
    clips = FCPXMLParser().parse_file(path).timelines[0].clips
    return sum(c.duration.seconds for c in clips)


def _call(group, action, **args):
    return asyncio.run(server.call_tool(group, {"action": action, **args}))[0].text


def _both(footage, tmp_path, fn):
    return [fn(_project(tmp_path, f"p{int(tc)}", tc)) for tc in (False, True)]


# --- the helper --------------------------------------------------------------


def test_file_in_point_subtracts_the_asset_clock():
    class Clip:
        source_start = Timecode(frames=108060, frame_rate=30.0)  # 3602s
        media = MediaRef(origin=Fraction(3600))

    assert media_intel.file_in_point(Clip()) == Fraction(2)


def test_file_in_point_of_a_still_is_its_one_picture():
    class Still:
        source_start = Timecode(frames=108000, frame_rate=30.0)
        media = MediaRef(still=True)

    assert media_intel.file_in_point(Still()) == Fraction(0)


def test_file_in_point_without_media_keeps_the_old_reading():
    class HandBuilt:
        source_start = Timecode(frames=60, frame_rate=30.0)
        media = None

    assert media_intel.file_in_point(HandBuilt()) == Fraction(2)


def test_asset_origin_reads_the_asset_and_nothing_else():
    resources = {"a1": {"start": "10800/3s"}, "m1": {}}
    assert media_intel.asset_origin_seconds(resources, "a1") == 3600.0
    assert media_intel.asset_origin_seconds(resources, "m1") == 0.0
    assert media_intel.asset_origin_seconds(resources, "nope") == 0.0


# --- the tools: same media, same answer, with or without timecode -------------


@needs_ffmpeg
def test_silence_detection_finds_the_same_span_on_timecoded_media(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    plain, timecoded = _both(footage, tmp_path, lambda f: _call(
        "diagnose", "detect_media_silence", filepath=f, min_silence=0.5))
    count = lambda text: int(re.search(r"Silence Spans Found\*\*: (\d+)", text).group(1))  # noqa: E731
    assert count(plain) == 1
    assert count(timecoded) == 1   # was 0
    span = lambda text: re.findall(r"\| (\d+\.\d+)s? \| (\d+\.\d+)s?", text)  # noqa: E731
    assert span(plain) == span(timecoded)


@needs_ffmpeg
def test_silence_removal_cuts_the_same_on_timecoded_media(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    lengths = []
    for timecoded in (False, True):
        source = _project(tmp_path, f"r{int(timecoded)}", timecoded)
        out = str(tmp_path / f"r{int(timecoded)}_out.fcpxml")
        text = _call("edit", "remove_media_silence", filepath=source, output_path=out,
                     min_silence=0.5, padding=0.0)
        assert "No silence found" not in text, text   # timecoded was "nothing to remove"
        lengths.append(_cut_length(out))
    assert lengths[0] == pytest.approx(lengths[1], abs=0.05)
    assert lengths[1] < 6.0 - 1.5


@needs_ffmpeg
def test_scene_detection_finds_the_same_cut_on_timecoded_media(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    plain, timecoded = _both(footage, tmp_path, lambda f: _call("scenes", "detect_scenes", filepath=f))
    count = lambda text: int(re.search(r"Cuts found\*\*: (\d+)", text).group(1))  # noqa: E731
    assert count(plain) == 1 and count(timecoded) == 1   # was 0
    # 4s into the file, 1s in-point: 3s on the timeline either way, and the
    # source column on each asset's own clock.
    cut = lambda text: re.search(r"\| Cam \| ([\d.]+)s \| ([^|]+) \|", text)  # noqa: E731
    assert cut(plain).group(2).strip() == cut(timecoded).group(2).strip()
    assert float(cut(plain).group(1)) == pytest.approx(4.0, abs=0.1)
    assert float(cut(timecoded).group(1)) == pytest.approx(3604.0, abs=0.1)


@needs_ffmpeg
def test_scene_split_splits_at_the_same_point_on_timecoded_media(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    starts = []
    for timecoded in (False, True):
        source = _project(tmp_path, f"s{int(timecoded)}", timecoded)
        out = str(tmp_path / f"s{int(timecoded)}_out.fcpxml")
        _call("scenes", "scenes_split", filepath=source, output_path=out)
        clips = FCPXMLParser().parse_file(out).timelines[0].clips
        assert len(clips) == 2
        starts.append(round(clips[1].start.seconds, 2))
    assert starts == [3.0, 3.0]


def _sidecar(d, media):
    """A transcript as Whisper would write it, in FILE time: 'hello world'
    at 0.5-1.5s and 'cut this' at 5.0-6.0s."""
    words = [
        {"word": "hello", "start": 0.5, "end": 1.0}, {"word": "world", "start": 1.0, "end": 1.5},
        {"word": "cut", "start": 5.0, "end": 5.5}, {"word": "this", "start": 5.5, "end": 6.0},
    ]
    path = Path(d) / (Path(media).stem + "_transcript.json")
    path.write_text(json.dumps({"text": "hello world cut this", "words": words, "segments": []}))


@needs_ffmpeg
def test_transcript_edits_find_the_same_words_on_timecoded_media(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    _sidecar(footage, "tc.mov")
    _sidecar(footage, "notc.mov")
    lengths = []
    for timecoded in (False, True):
        source = _project(tmp_path, f"w{int(timecoded)}", timecoded)
        out = str(tmp_path / f"w{int(timecoded)}_out.fcpxml")
        text = _call("transcript", "edit_by_transcript", filepath=source, output_path=out,
                     phrases=["cut this"], mode="remove")
        assert "no phrase" not in text.lower() and Path(out).is_file(), text
        lengths.append(_cut_length(out))
    assert lengths == [pytest.approx(5.0, abs=0.05)] * 2


@needs_ffmpeg
def test_find_places_a_transcript_hit_on_the_same_timeline_time(footage, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    _sidecar(footage, "tc.mov")
    _sidecar(footage, "notc.mov")
    plain, timecoded = _both(footage, tmp_path, lambda f: _call(
        "find", "find_shots", filepath=f, query="cut this"))
    row = lambda text: re.search(r"\| 1 \| Cam \| ([\d.]+)s \| ([\d.]+)s \| ([\d.]+)s \|", text)  # noqa: E731
    assert row(plain) and row(timecoded), (plain, timecoded)
    # Same place on the timeline; the source columns are on each asset's own
    # clock, as Final Cut shows them.
    assert row(plain).group(3) == row(timecoded).group(3) == "4.000"
    assert (row(plain).group(1), row(timecoded).group(1)) == ("5.000", "3605.000")


@needs_ffmpeg
def test_a_selects_reel_from_timecoded_media_is_written_on_the_asset_clock(footage, tmp_path, monkeypatch):
    """The hit is found in file time; the clip it writes must start on the
    asset's clock, or Final Cut reads it an hour before the media begins."""
    monkeypatch.setenv("FCP_MCP_INDEX", "off")
    _sidecar(footage, "tc.mov")
    _sidecar(footage, "notc.mov")
    for timecoded, expected in ((False, 5.0), (True, 3605.0)):
        source = _project(tmp_path, f"sel{int(timecoded)}", timecoded)
        out = str(tmp_path / f"sel{int(timecoded)}_out.fcpxml")
        text = _call("find", "find_to_timeline", filepath=source, query="cut this", output_path=out)
        assert Path(out).is_file(), text
        clip = FCPXMLParser().parse_file(out).timelines[0].clips[0]
        assert clip.source_start.seconds == pytest.approx(expected, abs=0.05)
        assert media_intel.file_in_point(clip) == pytest.approx(5.0, abs=0.05)


@needs_ffmpeg
def test_the_view_payload_reads_words_and_thumbnails_in_file_time(footage, tmp_path):
    from fcpxml import timeline_app

    _sidecar(footage, "tc.mov")
    timeline = FCPXMLParser().parse_file(_project(tmp_path, "v", True)).timelines[0]
    asked = []

    def thumb(media, at):
        asked.append(at)
        return None

    payload = timeline_app.build_payload(
        timeline, thumbnail=thumb,
        transcript=lambda media: json.loads(
            Path(media_intel.media_src_to_path(media)).with_name("tc_transcript.json").read_text()),
    )
    clip = payload["clips"][0]
    assert Fraction(clip["source_start"]) == Fraction(3601)   # what the document says
    assert Fraction(clip["file_start"]) == Fraction(1)        # where that is in the file
    assert asked == [Fraction(4)]                             # mid-clip, in the file
    # File 0.5-1.5s is before the 1s in-point only in part: "world" (1.0-1.5s)
    # is inside, and lands 0.0-0.5s on the timeline.
    assert clip["words"], clip["words"]


def test_gen_conditions_on_the_frame_at_the_file_time(monkeypatch):
    from tools import gen as gen_group

    seen = []
    monkeypatch.setattr(gen_group, "_frame_of", lambda media, at, kind: (seen.append(at) or (None, "x")))
    payload = {"clips": [{"name": "Cam", "start": "0", "duration": "6", "end": "6",
                          "source_start": "3601", "file_start": "1", "media": "/x.mov"}]}
    gen_group._broll_target({"clip_name": "Cam", "at": 2}, payload)
    gen_group._extend_target({"clip_name": "Cam"}, payload)
    assert seen[0] == Fraction(3)
    assert seen[1] == Fraction(7) - Fraction(1, 1000)
