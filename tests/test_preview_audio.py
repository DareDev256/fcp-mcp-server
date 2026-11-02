"""The proxy carries the timeline's sound, on the timeline's clock (0.30.0).

Measured by loudness at chosen instants of the rendered file: a clip heard
early, late, at the wrong level or not at all reads differently here, which
is the point of measuring instead of trusting the graph.
"""

import shutil
import subprocess
from array import array
from pathlib import Path

import pytest

from fcpxml import render
from fcpxml.parser import FCPXMLParser

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
                   capture_output=True, timeout=120, check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("audio")
    # Picture + a steady tone, the way a camera clip carries its sound.
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "cam.mp4"))
    _ff("-f", "lavfi", "-i", "sine=frequency=880:duration=8", "-c:a", "aac", str(d / "music.m4a"))
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "mute.mp4"))
    return d


def _timeline(tmp_path, media, spine, duration, name="a"):
    m = str(media)
    path = Path(tmp_path) / f"{name}.fcpxml"
    path.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14"><resources>
<format id="r1" name="FFVideoFormat1080p30" frameDuration="100/3000s" width="1920" height="1080"/>
<asset id="cam" name="Cam" start="0s" duration="8s" hasVideo="1" hasAudio="1" format="r1" audioSources="1" audioChannels="1" audioRate="44100"><media-rep kind="original-media" src="file://{m}/cam.mp4"/></asset>
<asset id="mus" name="Music" start="0s" duration="8s" hasAudio="1" audioSources="1" audioChannels="1" audioRate="44100"><media-rep kind="original-media" src="file://{m}/music.m4a"/></asset>
<asset id="mute" name="Mute" start="0s" duration="8s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file://{m}/mute.mp4"/></asset>
</resources><library><event name="E"><project name="{name}"><sequence format="r1" duration="{duration}" tcStart="0s"><spine>
{spine}
</spine></sequence></project></event></library></fcpxml>''')
    return FCPXMLParser().parse_file(str(path)).timelines[0]


def _rms(path, at, length=0.25):
    """Loudness 0..1 of [at, at+length) of the file's mixed sound."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-t", f"{length:.3f}", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    samples = array("h", raw[: len(raw) // 2 * 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768


def _render(tl, tmp_path, name):
    out = Path(tmp_path) / f"{name}.mp4"
    return render.render_proxy(tl, out_path=str(out), height=180), out


@needs_ffmpeg
def test_sound_is_heard_where_the_clips_are_and_silent_in_the_gap(media, tmp_path):
    tl = _timeline(tmp_path, media, '''
<asset-clip ref="cam" offset="0s" name="A" start="0s" duration="6000/3000s" format="r1"/>
<gap name="Gap" offset="6000/3000s" start="3600s" duration="6000/3000s"/>
<asset-clip ref="cam" offset="12000/3000s" name="B" start="3000/3000s" duration="6000/3000s" format="r1"/>
''', "18000/3000s", "gap")
    result, out = _render(tl, tmp_path, "gap")
    assert result["error"] is None and result["verified"] is True, result
    assert render.probe_audio_duration(str(out)) == pytest.approx(6.0, abs=0.05)
    assert _rms(out, 0.8) > 0.05      # A
    assert _rms(out, 3.0) < 0.005     # the gap is silence
    assert _rms(out, 4.8) > 0.05      # B, at its own time


@needs_ffmpeg
def test_an_audio_lane_plays_at_its_time_and_volume_is_applied(media, tmp_path):
    """Music on lane -1 from 1s, at -96 dB on the second copy: the first is
    heard from 1s over the silent picture-only clip, the second is not."""
    def tl(level):
        return _timeline(tmp_path, media, f'''
<asset-clip ref="mute" offset="0s" name="Picture" start="0s" duration="12000/3000s" format="r1">
    <asset-clip ref="mus" lane="-1" offset="3000/3000s" name="Music" start="0s" duration="6000/3000s"><adjust-volume amount="{level}"/></asset-clip>
</asset-clip>
''', "12000/3000s", f"lane{level[0] == '-'}")
    result, out = _render(tl("0dB"), tmp_path, "loud")
    assert result["error"] is None and result["verified"] is True, result
    assert _rms(out, 0.4) < 0.005   # before the music starts; the picture clip has no sound
    assert _rms(out, 1.8) > 0.05    # the music, from 1s
    assert _rms(out, 3.4) < 0.005   # after it ends
    quiet, qout = _render(tl("-96dB"), tmp_path, "quiet")
    assert quiet["error"] is None
    assert _rms(qout, 1.8) < 0.005


@needs_ffmpeg
def test_a_video_element_plays_its_picture_only(media, tmp_path):
    """<video ref> is the picture component of an asset; its sound is not
    in the cut unless an <audio> element says so."""
    tl = _timeline(tmp_path, media, '''
<video ref="cam" offset="0s" name="Picture only" start="0s" duration="6000/3000s"/>
''', "6000/3000s", "video")
    result, out = _render(tl, tmp_path, "video")
    assert result["error"] is None
    assert render.probe_audio_duration(str(out)) is None


@needs_ffmpeg
def test_the_instrument_hears_a_clip_in_the_wrong_place(media, tmp_path, monkeypatch):
    """Mutation check: drop every delay and B's tone lands at 0s. The loudness
    probe at B's real time must then read silence."""
    from fcpxml import filtergraph

    real = filtergraph.graph_to_args

    def no_delay(graph, out_path, height=480):
        args = real(graph, out_path, height=height)
        i = args.index("-filter_complex") + 1
        import re
        args[i] = re.sub(r"adelay=delays=\d+:all=1", "adelay=delays=0:all=1", args[i])
        return args

    monkeypatch.setattr(render, "graph_to_args", no_delay)
    tl = _timeline(tmp_path, media, '''
<gap name="Gap" offset="0s" start="3600s" duration="9000/3000s"/>
<asset-clip ref="cam" offset="9000/3000s" name="B" start="0s" duration="3000/3000s" format="r1"/>
''', "12000/3000s", "late")
    _, out = _render(tl, tmp_path, "late")
    assert _rms(out, 3.3) < 0.005   # B is not where the timeline puts it
    assert _rms(out, 0.3) > 0.05    # it played at the start instead
