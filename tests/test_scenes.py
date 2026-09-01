"""Scene detection against a clip whose cuts are known by construction."""

import shutil
import subprocess
import sys
import types

import pytest

from fcpxml import scenes

FFMPEG = shutil.which("ffmpeg") is not None

SHOWINFO = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':
  Duration: 00:00:06.00, start: 0.000000, bitrate: 20 kb/s
[Parsed_showinfo_1 @ 0x1] n:   0 pts:  48000 pts_time:2       pos: 1 fmt:yuv420p
[Parsed_showinfo_1 @ 0x1] n:   1 pts:  96000 pts_time:4.0417  pos: 2 fmt:yuv420p
"""


@pytest.fixture(scope="module")
def three_colour_clip(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("scenes") / "cuts.mp4"
    parts = []
    for i, colour in enumerate(("black", "white", "red")):
        p = out.with_name(f"p{i}.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=160x90:r=24:d=2",
             "-pix_fmt", "yuv420p", str(p)], check=True, timeout=60,
        )
        parts.append(p)
    lst = out.with_name("list.txt")
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)],
        check=True, timeout=60,
    )
    return str(out)


class TestParser:
    def test_cuts_and_duration(self):
        cuts, duration = scenes.parse_showinfo(SHOWINFO)
        assert cuts == [2.0, 4.0417]
        assert duration == 6.0

    def test_scenes_from_cuts_respects_min_len_and_closes_on_duration(self):
        out = scenes._scenes_from_cuts([2.0, 2.2, 4.0], 6.0, 0.5)
        assert out == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]

    def test_no_cuts_is_one_scene(self):
        assert scenes._scenes_from_cuts([], 6.0, 0.5) == [(0.0, 6.0)]


class TestValidation:
    def test_bad_backend(self, tmp_path):
        with pytest.raises(ValueError):
            scenes.detect_scenes(str(tmp_path / "x.mp4"), backend="magic")

    def test_bad_min_len(self, tmp_path):
        with pytest.raises(ValueError):
            scenes.detect_scenes(str(tmp_path / "x.mp4"), min_scene_len=0)

    def test_missing_file_is_none(self, tmp_path):
        assert scenes.detect_scenes(str(tmp_path / "x.mp4")) is None

    def test_ffmpeg_threshold_bounds(self, tmp_path):
        p = tmp_path / "x.mp4"
        p.write_bytes(b"\x00")
        with pytest.raises(ValueError):
            scenes.detect_scenes(str(p), backend="ffmpeg", threshold=1.5)


class TestFallback:
    def test_content_without_scenedetect_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "scenedetect", None)
        p = tmp_path / "x.mp4"
        p.write_bytes(b"\x00")
        assert scenes.backends_available()["content"] is False
        assert scenes.detect_scenes(str(p), backend="content") is None

    def test_auto_reports_ffmpeg_when_scenedetect_absent(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "scenedetect", None)
        p = tmp_path / "x.mp4"
        p.write_bytes(b"\x00")
        monkeypatch.setattr(
            scenes, "_ffmpeg_backend",
            lambda path, threshold, min_scene_len: {"backend": "ffmpeg", "cuts": [1.0], "scenes": [(0, 1), (1, 2)], "duration": 2.0},
        )
        assert scenes.detect_scenes(str(p))["backend"] == "ffmpeg"

    def test_auto_prefers_scenedetect_when_present(self, tmp_path, monkeypatch):
        class FT:
            def __init__(self, s):
                self.s = s

            def get_seconds(self):
                return self.s

        fake = types.SimpleNamespace(
            ContentDetector=lambda **kw: "content",
            AdaptiveDetector=lambda **kw: "adaptive",
            detect=lambda path, det, start_in_scene=False: [(FT(0.0), FT(2.0)), (FT(2.0), FT(5.0))],
        )
        monkeypatch.setitem(sys.modules, "scenedetect", fake)
        p = tmp_path / "x.mp4"
        p.write_bytes(b"\x00")
        monkeypatch.setattr(scenes, "_ffmpeg_backend", lambda *a: pytest.fail("fell through to ffmpeg"))
        result = scenes.detect_scenes(str(p))
        assert result["backend"] == "content"
        assert result["cuts"] == [2.0]
        assert result["duration"] == 5.0


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
class TestRealCuts:
    def test_ffmpeg_finds_the_two_colour_changes(self, three_colour_clip):
        result = scenes.detect_scenes(three_colour_clip, backend="ffmpeg")
        assert result is not None and result["backend"] == "ffmpeg"
        assert len(result["cuts"]) == 2
        assert abs(result["cuts"][0] - 2.0) < 0.15
        assert abs(result["cuts"][1] - 4.0) < 0.15
        assert abs(result["duration"] - 6.0) < 0.1
        assert result["scenes"][0] == (0.0, result["cuts"][0])
        assert result["scenes"][-1][1] == result["duration"]

    def test_instrument_sees_no_cuts_in_a_single_colour(self, tmp_path):
        """Mutation target: a detector that hallucinates cuts fails here."""
        p = tmp_path / "flat.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=160x90:r=24:d=3",
             "-pix_fmt", "yuv420p", str(p)], check=True, timeout=60,
        )
        result = scenes.detect_scenes(str(p), backend="ffmpeg")
        assert result["cuts"] == []
        assert len(result["scenes"]) == 1


try:
    import scenedetect  # noqa: F401

    SCENEDETECT = True
except ImportError:
    SCENEDETECT = False


@pytest.mark.skipif(not (FFMPEG and SCENEDETECT), reason="ffmpeg + scenedetect needed")
class TestPySceneDetectBackend:
    def test_content_and_adaptive_find_the_cuts(self, three_colour_clip):
        for backend in ("content", "adaptive"):
            result = scenes.detect_scenes(three_colour_clip, backend=backend)
            assert result["backend"] == backend
            assert [round(c, 2) for c in result["cuts"]] == [2.0, 4.0], backend
            assert abs(result["duration"] - 6.0) < 0.1

    def test_auto_picks_content(self, three_colour_clip):
        assert scenes.detect_scenes(three_colour_clip)["backend"] == "content"
