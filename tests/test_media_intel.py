"""Tests for fcpxml/media_intel.py — real media analysis (v0.10 media intelligence).

Parser and mapping tests are pure and run everywhere; integration tests that
invoke ffmpeg skip automatically on machines without it (CI installs it).
"""

import math
import shutil
import struct
import tempfile
import wave
from pathlib import Path

import pytest

from fcpxml.media_intel import (
    detect_silence,
    map_silence_to_timeline,
    parse_silencedetect_output,
)

FFMPEG = shutil.which("ffmpeg") is not None

SAMPLE_STDERR = """\
Input #0, wav, from 'test.wav':
  Duration: 00:00:04.00, bitrate: 1536 kb/s
[silencedetect @ 0x600002f30000] silence_start: 1.0
[silencedetect @ 0x600002f30000] silence_end: 3.0 | silence_duration: 2.0
size=N/A time=00:00:04.00 bitrate=N/A speed= 500x
"""

MULTI_STDERR = """\
[silencedetect @ 0x1] silence_start: 0.5
[silencedetect @ 0x1] silence_end: 1.25 | silence_duration: 0.75
[silencedetect @ 0x1] silence_start: 7.099979
[silencedetect @ 0x1] silence_end: 9.5 | silence_duration: 2.400021
"""

UNTERMINATED_STDERR = """\
[silencedetect @ 0x1] silence_start: 2.0
"""


class TestParseSilencedetectOutput:
    def test_parses_single_silence_range(self):
        assert parse_silencedetect_output(SAMPLE_STDERR) == [(1.0, 3.0)]

    def test_parses_multiple_ranges(self):
        result = parse_silencedetect_output(MULTI_STDERR)
        assert len(result) == 2
        assert result[0] == (0.5, 1.25)
        assert result[1] == pytest.approx((7.099979, 9.5))

    def test_unterminated_silence_closed_at_total_duration(self):
        result = parse_silencedetect_output(UNTERMINATED_STDERR, total_duration=5.0)
        assert result == [(2.0, 5.0)]

    def test_unterminated_silence_without_duration_dropped(self):
        assert parse_silencedetect_output(UNTERMINATED_STDERR) == []

    def test_empty_and_unrelated_input(self):
        assert parse_silencedetect_output("") == []
        assert parse_silencedetect_output("frame=  100 fps=25\n") == []


class TestMapSilenceToTimeline:
    """Silence ranges are in SOURCE seconds; clips use [source_start,
    source_start + duration) of the source and sit at timeline_offset."""

    def test_silence_inside_used_range_is_mapped(self):
        # Clip uses source 10..20s, sits at timeline 100s.
        result = map_silence_to_timeline(
            [(12.0, 15.0)], source_start=10.0, clip_duration=10.0, timeline_offset=100.0
        )
        assert result == [(102.0, 105.0)]

    def test_silence_overlapping_range_edges_is_clamped(self):
        result = map_silence_to_timeline(
            [(8.0, 12.0), (18.0, 25.0)],
            source_start=10.0, clip_duration=10.0, timeline_offset=100.0,
        )
        assert result == [(100.0, 102.0), (108.0, 110.0)]

    def test_silence_outside_used_range_is_excluded(self):
        result = map_silence_to_timeline(
            [(0.0, 5.0), (30.0, 40.0)],
            source_start=10.0, clip_duration=10.0, timeline_offset=100.0,
        )
        assert result == []


class TestDetectSilenceBounds:
    def test_rejects_positive_noise_db(self):
        with pytest.raises(ValueError):
            detect_silence("/tmp/x.wav", noise_db=5.0)

    def test_rejects_extreme_negative_noise_db(self):
        with pytest.raises(ValueError):
            detect_silence("/tmp/x.wav", noise_db=-200.0)

    def test_rejects_nonpositive_min_duration(self):
        with pytest.raises(ValueError):
            detect_silence("/tmp/x.wav", min_duration=0)

    def test_rejects_huge_min_duration(self):
        with pytest.raises(ValueError):
            detect_silence("/tmp/x.wav", min_duration=4000)

    def test_missing_file_returns_none(self):
        assert detect_silence("/nonexistent/path/audio.wav") is None


class TestDetectSilenceCommand:
    """Silence detection must not decode the video stream (-vn): without it
    ffmpeg decodes every video frame into the null muxer, which blows past
    PROBE_TIMEOUT_SECONDS on long/high-bitrate camera files."""

    def test_video_decoding_is_disabled(self, tmp_path, monkeypatch):
        import subprocess

        from fcpxml import media_intel

        media_file = tmp_path / "clip.mov"
        media_file.write_bytes(b"\x00")

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=SAMPLE_STDERR)

        monkeypatch.setattr(media_intel.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        monkeypatch.setattr(media_intel.subprocess, "run", fake_run)

        result = detect_silence(str(media_file))

        assert result == [(1.0, 3.0)]
        cmd = captured["cmd"]
        assert "-vn" in cmd
        # Must sit between the input and the null-muxer output to take effect.
        assert cmd.index("-i") < cmd.index("-vn") < cmd.index("-f")


def _write_tone_silence_tone_wav(path: str, rate: int = 48000) -> None:
    """1s 440Hz tone, 2s silence, 1s 440Hz tone."""
    def tone(seconds: float) -> bytes:
        n = int(seconds * rate)
        return b"".join(
            struct.pack("<h", int(20000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(n)
        )

    def silence(seconds: float) -> bytes:
        return b"\x00\x00" * int(seconds * rate)

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(tone(1.0) + silence(2.0) + tone(1.0))


PROJECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="interview" start="0s" duration="4s" hasVideo="0" hasAudio="1"
           audioSources="1" audioChannels="1" audioRate="48000">
      <media-rep kind="original-media" src="{src}"/>
    </asset>
  </resources>
  <library>
    <event name="Test">
      <project name="SilenceTest">
        <sequence format="r1" duration="4s" tcStart="0s">
          <spine>
            <gap name="Gap" offset="0s" start="0s" duration="10s"/>
            <asset-clip ref="r2" offset="10s" name="interview" start="0s" duration="4s" audioRole="dialogue"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


class TestDetectMediaSilenceHandler:
    """The detect_media_silence MCP tool: probes each clip's real source
    audio and reports silence mapped into timeline time."""

    def _write_project(self, tmp_path: Path, media_src: str) -> str:
        fcpxml_path = tmp_path / "project.fcpxml"
        fcpxml_path.write_text(PROJECT_XML.format(src=media_src))
        return str(fcpxml_path)

    @pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
    async def test_reports_timeline_mapped_silence(self, tmp_path):
        from server import handle_detect_media_silence

        wav_path = tmp_path / "interview.wav"
        _write_tone_silence_tone_wav(str(wav_path))
        filepath = self._write_project(tmp_path, f"file://{wav_path}")

        result = await handle_detect_media_silence(
            {"filepath": filepath, "noise_db": -40.0, "min_silence": 0.5}
        )
        text = result[0].text
        # WAV is silent from 1s to 3s in source; clip sits at timeline 10s.
        assert "interview" in text
        assert "11.0" in text
        assert "13.0" in text

    async def test_missing_media_is_reported_not_crashed(self, tmp_path):
        from server import handle_detect_media_silence

        filepath = self._write_project(tmp_path, "file:///nonexistent/interview.wav")
        result = await handle_detect_media_silence({"filepath": filepath})
        text = result[0].text
        assert "interview" in text
        assert "skipped" in text.lower() or "missing" in text.lower()

    async def test_rejects_out_of_bounds_noise_db(self, tmp_path):
        from server import handle_detect_media_silence

        filepath = self._write_project(tmp_path, "file:///nonexistent/interview.wav")
        with pytest.raises(ValueError):
            await handle_detect_media_silence({"filepath": filepath, "noise_db": 40.0})


TWO_CLIP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.13">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="interview" start="0s" duration="10s" hasVideo="0" hasAudio="1">
      <media-rep kind="original-media" src="file:///media/interview.wav"/>
    </asset>
    <asset id="r3" name="broll" start="0s" duration="10s" hasVideo="1" hasAudio="1">
      <media-rep kind="original-media" src="file:///media/broll.mov"/>
    </asset>
  </resources>
  <library>
    <event name="Test">
      <project name="CutTest">
        <sequence format="r1" duration="6s" tcStart="0s">
          <spine>
            <asset-clip ref="r2" offset="0s" name="interview" start="0s" duration="4s">
              <marker start="0.5s" duration="1/24s" value="Keep"/>
              <marker start="2s" duration="1/24s" value="Drop"/>
            </asset-clip>
            <asset-clip ref="r3" offset="4s" name="broll" start="0s" duration="2s"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


class TestCutClipRanges:
    """FCPXMLModifier.cut_clip_ranges — rebuild a clip without the given
    clip-relative ranges, rippling everything after it."""

    def _make_modifier(self, tmp_path: Path):
        from fcpxml.writer import FCPXMLModifier

        fcpxml_path = tmp_path / "cut.fcpxml"
        fcpxml_path.write_text(TWO_CLIP_XML)
        return FCPXMLModifier(str(fcpxml_path))

    def _spine_clips(self, mod):
        spine = mod.root.find(".//spine")
        return [el for el in spine if el.tag == "asset-clip"]

    def test_middle_cut_splits_clip_and_ripples(self, tmp_path):
        from fcpxml.models import TimeValue

        mod = self._make_modifier(tmp_path)
        clip = self._spine_clips(mod)[0]
        removed = mod.cut_clip_ranges(clip, [(TimeValue(1, 1), TimeValue(3, 1))])

        assert removed.to_seconds() == pytest.approx(2.0)
        clips = self._spine_clips(mod)
        assert len(clips) == 3  # interview x2 + broll
        seg1, seg2, broll = clips
        assert seg1.get("offset") == "0s"
        assert mod._parse_time(seg1.get("duration")).to_seconds() == pytest.approx(1.0)
        assert mod._parse_time(seg2.get("offset")).to_seconds() == pytest.approx(1.0)
        assert mod._parse_time(seg2.get("start")).to_seconds() == pytest.approx(3.0)
        assert mod._parse_time(seg2.get("duration")).to_seconds() == pytest.approx(1.0)
        # broll rippled 4s -> 2s
        assert mod._parse_time(broll.get("offset")).to_seconds() == pytest.approx(2.0)

    def test_head_cut_trims_in_place(self, tmp_path):
        from fcpxml.models import TimeValue

        mod = self._make_modifier(tmp_path)
        clip = self._spine_clips(mod)[0]
        removed = mod.cut_clip_ranges(clip, [(TimeValue(0, 1), TimeValue(1, 1))])

        assert removed.to_seconds() == pytest.approx(1.0)
        clips = self._spine_clips(mod)
        assert len(clips) == 2
        seg, broll = clips
        assert seg.get("offset") == "0s"
        assert mod._parse_time(seg.get("start")).to_seconds() == pytest.approx(1.0)
        assert mod._parse_time(seg.get("duration")).to_seconds() == pytest.approx(3.0)
        assert mod._parse_time(broll.get("offset")).to_seconds() == pytest.approx(3.0)

    def test_full_span_cut_removes_clip(self, tmp_path):
        from fcpxml.models import TimeValue

        mod = self._make_modifier(tmp_path)
        clip = self._spine_clips(mod)[0]
        removed = mod.cut_clip_ranges(clip, [(TimeValue(0, 1), TimeValue(4, 1))])

        assert removed.to_seconds() == pytest.approx(4.0)
        clips = self._spine_clips(mod)
        assert len(clips) == 1
        assert clips[0].get("name") == "broll"
        assert clips[0].get("offset") == "0s"

    def test_overlapping_ranges_are_merged_and_clamped(self, tmp_path):
        from fcpxml.models import TimeValue

        mod = self._make_modifier(tmp_path)
        clip = self._spine_clips(mod)[0]
        removed = mod.cut_clip_ranges(clip, [
            (TimeValue(2, 1), TimeValue(9, 1)),    # clamped to 4s
            (TimeValue(1, 1), TimeValue(5, 2)),    # 1..2.5 overlaps
            (TimeValue(-1, 1), TimeValue(1, 2)),   # -1..0.5 clamped to 0..0.5
        ])
        # merged cuts: 0..0.5 and 1..4 -> removed 3.5, kept 0.5..1
        assert removed.to_seconds() == pytest.approx(3.5)
        clips = self._spine_clips(mod)
        seg = clips[0]
        assert mod._parse_time(seg.get("start")).to_seconds() == pytest.approx(0.5)
        assert mod._parse_time(seg.get("duration")).to_seconds() == pytest.approx(0.5)

    def test_markers_follow_their_segment(self, tmp_path):
        from fcpxml.models import TimeValue

        mod = self._make_modifier(tmp_path)
        clip = self._spine_clips(mod)[0]
        mod.cut_clip_ranges(clip, [(TimeValue(1, 1), TimeValue(3, 1))])

        clips = self._spine_clips(mod)
        seg1_markers = [m.get("value") for m in clips[0].findall("marker")]
        seg2_markers = [m.get("value") for m in clips[1].findall("marker")]
        assert seg1_markers == ["Keep"]   # 0.5s is in kept 0..1
        assert seg2_markers == []         # 2s was inside the cut


class TestRemoveMediaSilenceHandler:
    """The remove_media_silence MCP tool: detects real silence and cuts it
    out of the timeline with ripple."""

    def _write_project(self, tmp_path: Path, media_src: str) -> str:
        fcpxml_path = tmp_path / "project.fcpxml"
        fcpxml_path.write_text(PROJECT_XML.format(src=media_src))
        return str(fcpxml_path)

    @pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
    async def test_cuts_silence_and_saves_output(self, tmp_path):
        from fcpxml.parser import FCPXMLParser
        from server import handle_remove_media_silence

        wav_path = tmp_path / "interview.wav"
        _write_tone_silence_tone_wav(str(wav_path))
        filepath = self._write_project(tmp_path, f"file://{wav_path}")

        result = await handle_remove_media_silence(
            {"filepath": filepath, "noise_db": -40.0, "min_silence": 0.5, "padding": 0}
        )
        text = result[0].text
        assert "_silence_removed" in text

        out = str(tmp_path / "project_silence_removed.fcpxml")
        project = FCPXMLParser().parse_file(out)
        segments = [c for c in project.primary_timeline.clips if c.name == "interview"]
        # tone(1s) silence(2s) tone(1s) -> two 1s segments
        assert len(segments) == 2
        assert segments[0].duration.seconds == pytest.approx(1.0, abs=0.05)
        assert segments[1].duration.seconds == pytest.approx(1.0, abs=0.05)
        assert segments[1].source_start.seconds == pytest.approx(3.0, abs=0.05)

    async def test_missing_media_makes_no_changes(self, tmp_path):
        from server import handle_remove_media_silence

        filepath = self._write_project(tmp_path, "file:///nonexistent/interview.wav")
        result = await handle_remove_media_silence({"filepath": filepath})
        text = result[0].text
        assert "skipped" in text.lower() or "missing" in text.lower()
        assert not (tmp_path / "project_silence_removed.fcpxml").exists()

    async def test_rejects_out_of_bounds_padding(self, tmp_path):
        from server import handle_remove_media_silence

        filepath = self._write_project(tmp_path, "file:///nonexistent/interview.wav")
        with pytest.raises(ValueError):
            await handle_remove_media_silence({"filepath": filepath, "padding": -1})


try:
    import librosa  # noqa: F401
    LIBROSA = True
except ImportError:
    LIBROSA = False


def _write_click_track_wav(path: str, bpm: float = 120.0, seconds: float = 8.0,
                           rate: int = 22050) -> None:
    """Clicks (10ms 1kHz bursts) on the beat grid, silence between."""
    interval = 60.0 / bpm
    total = int(seconds * rate)
    samples = [0] * total
    burst = int(0.010 * rate)
    t = 0.0
    while t < seconds:
        start = int(t * rate)
        for i in range(burst):
            if start + i < total:
                samples[start + i] = int(28000 * math.sin(2 * math.pi * 1000 * i / rate))
        t += interval
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"".join(struct.pack("<h", s) for s in samples))


class TestDetectBeats:
    def test_missing_file_returns_none(self):
        from fcpxml.media_intel import detect_beats

        assert detect_beats("/nonexistent/song.wav") is None

    @pytest.mark.skipif(not LIBROSA, reason="librosa not installed")
    def test_detects_click_track_grid(self, tmp_path):
        from fcpxml.media_intel import detect_beats

        wav = tmp_path / "click.wav"
        _write_click_track_wav(str(wav), bpm=120.0, seconds=8.0)
        result = detect_beats(str(wav))
        assert result is not None
        beats = result["beats"]
        assert result["bpm"] > 0
        assert len(beats) >= 10
        # median inter-beat interval must sit on the 0.5s grid
        intervals = sorted(b - a for a, b in zip(beats, beats[1:]))
        median = intervals[len(intervals) // 2]
        assert median == pytest.approx(0.5, abs=0.06)


@pytest.mark.skipif(not LIBROSA, reason="librosa not installed")
class TestDetectBeatsHandler:
    async def test_writes_beats_json_and_reports(self, tmp_path):
        import json as jsonlib

        from server import handle_detect_beats

        wav = tmp_path / "song.wav"
        _write_click_track_wav(str(wav), bpm=120.0, seconds=8.0)
        result = await handle_detect_beats({"media_path": str(wav)})
        text = result[0].text
        assert "BPM" in text
        json_path = tmp_path / "song_beats.json"
        assert str(json_path) in text
        data = jsonlib.loads(json_path.read_text())
        assert len(data["beats"]) >= 10
        assert data["bpm"] > 0
        assert data["downbeats"] == data["beats"][::4]

    async def test_rejects_disallowed_extension(self, tmp_path):
        from server import handle_detect_beats

        bad = tmp_path / "song.txt"
        bad.write_text("not audio")
        with pytest.raises(ValueError):
            await handle_detect_beats({"media_path": str(bad)})

    async def test_reports_when_librosa_unavailable(self, tmp_path, monkeypatch):
        import server as server_mod
        from server import handle_detect_beats

        wav = tmp_path / "song.wav"
        _write_click_track_wav(str(wav), seconds=2.0)
        monkeypatch.setattr(server_mod, "detect_beats", lambda *a, **k: None)
        result = await handle_detect_beats({"media_path": str(wav)})
        assert "librosa" in result[0].text.lower()


class TestImportBeatMarkersBeyondTimeline:
    """detect_beats output routinely spans longer than the edit (songs are
    longer than timelines) — import must skip out-of-range beats, not crash."""

    async def test_out_of_range_beats_are_skipped_not_fatal(self, tmp_path):
        import json as jsonlib

        from server import handle_import_beat_markers

        proj = tmp_path / "edit.fcpxml"
        proj.write_text(TWO_CLIP_XML)  # timeline is 6s total
        beats = tmp_path / "beats.json"
        beats.write_text(jsonlib.dumps({"beats": [0.5, 2.0, 5.5, 7.0, 9.5]}))

        result = await handle_import_beat_markers(
            {"filepath": str(proj), "beats_path": str(beats)}
        )
        text = result[0].text
        assert "3" in text          # 3 in-range markers placed
        assert "skipped" in text.lower()
        assert "2" in text          # 2 beyond-timeline beats skipped


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
class TestDetectSilenceIntegration:
    def test_detects_silence_in_real_wav(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            _write_tone_silence_tone_wav(wav_path)
            result = detect_silence(wav_path, noise_db=-40.0, min_duration=0.5)
            assert result is not None
            assert len(result) == 1
            start, end = result[0]
            assert start == pytest.approx(1.0, abs=0.1)
            assert end == pytest.approx(3.0, abs=0.1)
        finally:
            Path(wav_path).unlink(missing_ok=True)

    def test_fully_silent_wav_is_one_range(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(b"\x00\x00" * 48000 * 3)
            result = detect_silence(wav_path, noise_db=-40.0, min_duration=0.5)
            assert result is not None
            assert len(result) == 1
            start, end = result[0]
            assert start == pytest.approx(0.0, abs=0.1)
            assert end == pytest.approx(3.0, abs=0.1)
        finally:
            Path(wav_path).unlink(missing_ok=True)


class TestBeatGridRefit:
    """detect_beats used to return only the beats librosa HEARD.

    On a programmed track that under-reports badly: an intro with no percussion
    and a drum-out before the outro both come back empty, which are exactly the
    sections an editor is eyeballing. Measured on a real 164s music video, the
    onset-only path returned 286 beats spanning 16.0s to 158.6s. The grid refit
    returns 329 spanning the whole file, at 119.996 BPM against a hand-written
    reference tracker's 120.005.
    """

    @staticmethod
    def _clicks(period, duration, jitter=0.0, drop_from=None, drop_to=None):
        """Observed beats on a fixed grid, optionally jittered and gapped."""
        import random
        rng = random.Random(7)
        out, t = [], 0.5
        while t < duration:
            if not (drop_from is not None and drop_from <= t <= drop_to):
                out.append(t + (rng.uniform(-jitter, jitter) if jitter else 0.0))
            t += period
        return out

    def test_fixed_tempo_extrapolates_across_the_whole_file(self):
        from fcpxml.media_intel import _grid_or_observed
        observed = self._clicks(0.5, 100.0, drop_from=0, drop_to=20)
        assert observed[0] > 20, "fixture must start after the gap"
        r = _grid_or_observed(120.0, observed, duration=100.0)
        assert r["source"] == "grid"
        assert r["beats"][0] < 1.0, "grid must reach back over the silent intro"
        assert r["beats"][-1] > 99.0, "grid must reach the end of the file"
        assert len(r["beats"]) > len(observed)

    def test_recovers_the_true_period_not_the_median_interval(self):
        """The median inter-onset interval is NOT the period.

        librosa quantises beats to its analysis frames, ~11.6ms at 44.1kHz. A
        true 0.5s grid lands on 43 frames (0.49926s) more often than 44
        (0.51068s), so the MEDIAN interval is biased low. Measured on the real
        track: median 0.4992s against a true 0.5000s, and that 0.8ms error
        compounds to 0.23s of drift across 286 beats — enough that a grid built
        on it disagrees with most of its own beats by the end of the song.

        Symmetric random jitter does not reproduce this; the median survives it.
        Only quantisation does, so the fixture quantises.
        """
        from fcpxml.media_intel import _grid_or_observed

        frame = 512 / 44100.0                     # librosa's default hop
        ideal = [0.5 * i for i in range(1, 300)]  # a true 120.000 BPM grid
        observed = [round(t / frame) * frame for t in ideal]

        intervals = sorted(b - a for a, b in zip(observed, observed[1:]))
        median = intervals[len(intervals) // 2]
        assert abs(median - 0.5) > 0.0005, (
            f"fixture must reproduce the biased median, got {median}"
        )

        r = _grid_or_observed(119.0, observed, duration=150.0)
        assert r["source"] == "grid"
        assert abs(r["period"] - 0.5) < 0.0002, (
            f"period {r['period']} — the median {median} was used instead of a refit"
        )
        assert abs(r["bpm"] - 120.0) < 0.05, r["bpm"]

    def test_variable_tempo_returns_observed_and_invents_nothing(self):
        """Rubato must NOT get a grid. Inventing beats there is worse than none."""
        from fcpxml.media_intel import _grid_or_observed
        observed, t, period = [], 0.5, 0.5
        while t < 60.0:
            observed.append(t)
            period *= 1.01          # accelerating, genuinely not a fixed grid
            t += period
        r = _grid_or_observed(120.0, observed, duration=60.0)
        assert r["source"] == "observed"
        assert r["grid"] is False
        assert r["beats"] == observed, "observed beats must pass through untouched"

    def test_reports_which_path_it_took(self):
        from fcpxml.media_intel import _grid_or_observed
        r = _grid_or_observed(120.0, self._clicks(0.5, 60.0), duration=60.0)
        assert r["source"] in ("grid", "observed")
        assert r["confidence"] is not None
        assert r["observed_count"] == len(self._clicks(0.5, 60.0))

    def test_too_few_beats_falls_back(self):
        from fcpxml.media_intel import _grid_or_observed
        r = _grid_or_observed(120.0, [1.0, 1.5, 2.0], duration=10.0)
        assert r["source"] == "observed"
