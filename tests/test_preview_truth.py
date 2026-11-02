"""The proxy is exactly as long as the timeline, and every instant of it is the
timeline's picture or a named black substitute (0.28.0).

The first three tests are John Palchak's report of 2026-11-01 (fcp-mcp-server
0.27.1, ffmpeg 9.0.2, FCPXML 1.14 from Final Cut 12.3), rebuilt in the shapes
Final Cut writes, through the real parser and the real ffmpeg:

1. a transition longer than a neighbouring clip emptied the whole render;
2. a PNG still alone on the spine (``<video start="3600s">``) rendered an
   empty MP4: the renderer seeked 3600s into a one-frame image;
3. connected clips on lane 2 were logged "drawn full-frame" and never
   appeared: they were placed at their raw ``offset``, which is in the
   PARENT's clock, not the timeline's.

The rest are the same disease found beside them: timecoded media read at its
timecode, gaps collapsing, a short or audio-only file emptying the graph,
and an instrument that could not see any of it.
"""

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from fcpxml import filtergraph, render
from fcpxml.parser import FCPXMLParser

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


def _ff(*args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
        capture_output=True, timeout=120, check=True,
    )


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Small stand-ins for John's media: a 60p MP4, a 30p ProRes, a PNG, a
    timecoded ProRes, flat-colour lane clips and an audio-only file."""
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("truth")
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=60:duration=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "a.mp4"))
    _ff("-f", "lavfi", "-i", "smptebars=size=320x180:rate=30:duration=6",
        "-c:v", "prores_ks", "-profile:v", "0", str(d / "b.mov"))
    _ff("-f", "lavfi", "-i", "color=c=green:size=320x180", "-frames:v", "1", str(d / "still.png"))
    _ff("-f", "lavfi", "-i", "color=c=red:size=320x180:rate=60:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "red.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=30:duration=6",
        "-c:v", "prores_ks", "-profile:v", "0", str(d / "blue.mov"))
    _ff("-f", "lavfi", "-i", "smptebars=size=320x180:rate=30:duration=8",
        "-c:v", "prores_ks", "-profile:v", "0", "-timecode", "01:00:00:00", str(d / "tc.mov"))
    _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "aac", str(d / "tone.m4a"))
    _ff("-f", "lavfi", "-i", "color=c=red:size=180x320:rate=30:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "vertical.mp4"))
    return d


def _fcpxml(tmp_path, media, name, spine, duration):
    m = str(media)
    text = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14">
    <resources>
        <format id="r1" name="FFVideoFormat1080p30" frameDuration="100/3000s" width="1920" height="1080"/>
        <format id="r2" name="FFVideoFormat1080p60" frameDuration="100/6000s" width="1920" height="1080"/>
        <format id="r4" name="FFVideoFormatRateUndefined" width="1920" height="1080"/>
        <asset id="a1" name="A" start="0s" duration="25s" hasVideo="1" format="r2"><media-rep kind="original-media" src="file://{m}/a.mp4"/></asset>
        <asset id="a2" name="B" start="0s" duration="6s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file://{m}/b.mov"/></asset>
        <asset id="a3" name="Still" start="0s" duration="0s" hasVideo="1" format="r4"><media-rep kind="original-media" src="file://{m}/still.png"/></asset>
        <asset id="a4" name="Red" start="0s" duration="6s" hasVideo="1" format="r2"><media-rep kind="original-media" src="file://{m}/red.mp4"/></asset>
        <asset id="a5" name="Blue" start="0s" duration="6s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file://{m}/blue.mov"/></asset>
        <asset id="a6" name="TC" start="3600s" duration="8s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file://{m}/tc.mov"/></asset>
        <asset id="a7" name="Tone" start="0s" duration="6s" hasAudio="1" audioSources="1"><media-rep kind="original-media" src="file://{m}/tone.m4a"/></asset>
        <format id="r9" name="FFVideoFormatVertical" frameDuration="100/3000s" width="1080" height="1920"/>
        <asset id="a8" name="Vertical" start="0s" duration="6s" hasVideo="1" format="r9"><media-rep kind="original-media" src="file://{m}/vertical.mp4"/></asset>
    </resources>
    <library><event name="E"><project name="{name}">
        <sequence format="r1" duration="{duration}" tcStart="0s" tcFormat="NDF"><spine>
{spine}
        </spine></sequence>
    </project></event></library>
</fcpxml>'''
    path = Path(tmp_path) / f"{name}.fcpxml"
    path.write_text(text)
    return FCPXMLParser().parse_file(str(path)).timelines[0]


def _rgb(path, at):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{float(at):.4f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    return tuple(out[:3])


def _is(rgb, colour):
    """Loose colour match for a compressed 1x1 average."""
    r, g, b = rgb
    return {
        "red": r > 150 and g < 90 and b < 90,
        "green": g > 90 and r < 60 and b < 60,
        "blue": b > 150 and r < 60 and g < 60,
        "black": r < 30 and g < 30 and b < 30,
    }[colour]


def _render(timeline, tmp_path, name):
    out = Path(tmp_path) / f"{name}.mp4"
    result = render.render_proxy(timeline, out_path=str(out), height=180)
    return result, out


# --- John's three cases ----------------------------------------------------


@needs_ffmpeg
def test_case1_a_transition_longer_than_its_neighbour_renders(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "t", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="62400/3000s" format="r2"/>
        <transition name="Color Panels" offset="59600/3000s" duration="5600/3000s"/>
        <asset-clip ref="a2" offset="62400/3000s" name="B" start="3000/3000s" duration="3800/3000s" format="r1"/>
''', "66200/3000s")
    result, out = _render(tl, tmp_path, "t")
    assert result["error"] is None, result["error"]
    assert result["verified"] is True
    # The whole timeline, not the timeline minus the transition.
    assert result["expected"] == Fraction(66200, 3000)
    assert abs(result["drift"]) <= render.DURATION_TOLERANCE
    assert len(result["transitions"]) == 1
    assert result["transitions"][0].duration == Fraction(45, 24)  # 1.87s, not squeezed
    # Color Panels has no xfade twin; the parsed transition no longer hides
    # behind the model's "cross-dissolve" default.
    assert any("Color Panels" in n and "rendered as a dissolve" in n for n in result["substitutions"])


@needs_ffmpeg
def test_case2_a_png_still_alone_on_the_spine_renders_the_still(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "s", '''
        <video ref="a3" offset="0s" name="Still" start="3600s" duration="15000/3000s"><adjust-conform type="none"/></video>
''', "15000/3000s")
    result, out = _render(tl, tmp_path, "s")
    assert result["error"] is None, result["error"]
    assert result["duration"] is not None  # was: "Duration could NOT be read back"
    assert result["verified"] is True
    # adjust-conform none is native pixels: this 320x180 still sits at its own
    # size in the middle of a 1920x1080 frame, black around it.
    centre = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "2.5", "-i", str(out), "-frames:v", "1",
         "-vf", "crop=8:8:iw/2-4:ih/2-4,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    corner = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "2.5", "-i", str(out), "-frames:v", "1",
         "-vf", "crop=8:8:0:0,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    assert _is(tuple(centre[:3]), "green") and _is(tuple(corner[:3]), "black")


@needs_ffmpeg
def test_case3_connected_clips_appear_where_the_timeline_puts_them(media, tmp_path):
    """Red hangs off a spine clip trimmed to start at 2s (its offset 3s is 1s
    into the parent); Blue hangs off a gap whose clock starts at 3600s (its
    offset 3601s is 1s into the gap). The gap keeps its 4s of black."""
    tl = _fcpxml(tmp_path, media, "l", '''
        <asset-clip ref="a1" offset="0s" name="A" start="6000/3000s" duration="12000/3000s" format="r2">
            <asset-clip ref="a4" lane="2" offset="9000/3000s" name="Red" start="0s" duration="6000/3000s" format="r2"/>
        </asset-clip>
        <gap name="Gap" offset="12000/3000s" start="3600s" duration="12000/3000s">
            <asset-clip ref="a5" lane="2" offset="10803000/3000s" name="Blue" start="0s" duration="6000/3000s" format="r1"/>
        </gap>
''', "24000/3000s")
    result, out = _render(tl, tmp_path, "l")
    assert result["error"] is None, result["error"]
    assert result["expected"] == Fraction(8)
    assert result["verified"] is True
    assert [v for _, v, _ in result["lanes"]] == [True, True]
    assert _is(_rgb(out, 2.0), "red")      # Red: timeline 1s-3s
    assert not _is(_rgb(out, 3.5), "red")  # gone again before the gap
    assert _is(_rgb(out, 4.5), "black")    # the gap, before Blue
    assert _is(_rgb(out, 6.0), "blue")     # Blue: timeline 5s-7s
    assert _is(_rgb(out, 7.5), "black")


@needs_ffmpeg
def test_a_transformed_lane_is_drawn_at_its_scale_and_position(media, tmp_path):
    """scale 0.5 at position (-44.4444, 25): the top-left quarter of a 16:9
    frame (FCPXML position is percent of frame height from the centre, y up)."""
    tl = _fcpxml(tmp_path, media, "pip", '''
        <gap name="Gap" offset="0s" start="3600s" duration="6000/3000s">
            <asset-clip ref="a4" lane="1" offset="3600s" name="Red" start="0s" duration="6000/3000s" format="r2">
                <adjust-transform position="-44.4444 25" scale="0.5 0.5"/>
            </asset-clip>
        </gap>
''', "6000/3000s")
    result, out = _render(tl, tmp_path, "pip")
    assert result["error"] is None and result["verified"] is True
    quarters = {}
    for name, crop in (("tl", "crop=iw/2:ih/2:0:0"), ("br", "crop=iw/2:ih/2:iw/2:ih/2")):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
             "-vf", f"{crop},scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=60, check=True,
        ).stdout
        quarters[name] = tuple(raw[:3])
    assert _is(quarters["tl"], "red") and _is(quarters["br"], "black")


# --- the same disease, found beside them -------------------------------------


@needs_ffmpeg
def test_timecoded_media_is_read_from_the_file_not_from_its_timecode(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "tc", '''
        <asset-clip ref="a6" offset="0s" name="TC" start="10806000/3000s" duration="12000/3000s" format="r1"/>
''', "12000/3000s")
    graph = filtergraph.compile_timeline(tl)
    assert graph.pieces[0].read_in == Fraction(2)  # 3602s on the asset clock
    result, out = _render(tl, tmp_path, "tc")
    assert result["error"] is None and result["verified"] is True


@needs_ffmpeg
def test_a_clip_past_the_end_of_its_file_is_named_black_not_fatal(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "short", '''
        <asset-clip ref="a2" offset="0s" name="Past" start="30000/3000s" duration="6000/3000s" format="r1"/>
        <asset-clip ref="a1" offset="6000/3000s" name="A" start="0s" duration="6000/3000s" format="r2"/>
''', "12000/3000s")
    result, out = _render(tl, tmp_path, "short")
    assert result["error"] is None, result["error"]
    assert result["verified"] is True
    assert any("'Past'" in n and "shown black" in n and "6.00s long" in n for n in result["substitutions"])
    assert _is(_rgb(out, 1.0), "black")


@needs_ffmpeg
def test_an_audio_only_clip_on_the_spine_is_black_not_fatal(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "audio", '''
        <asset-clip ref="a7" offset="0s" name="Tone" start="0s" duration="6000/3000s"/>
        <asset-clip ref="a1" offset="6000/3000s" name="A" start="0s" duration="6000/3000s" format="r2"/>
''', "12000/3000s")
    result, out = _render(tl, tmp_path, "audio")
    assert result["error"] is None, result["error"]
    assert any("'Tone'" in n and "no picture" in n for n in result["substitutions"])
    assert result["verified"] is True


@needs_ffmpeg
def test_a_short_head_handle_holds_the_first_frame_and_says_so(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "hold", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="6000/3000s" format="r2"/>
        <transition name="Cross Dissolve" offset="4500/3000s" duration="3000/3000s"/>
        <asset-clip ref="a2" offset="6000/3000s" name="B" start="0s" duration="6000/3000s" format="r1"/>
''', "12000/3000s")
    result, _ = _render(tl, tmp_path, "hold")
    assert result["error"] is None and result["verified"] is True
    assert any("'B'" in n and "holds its first frame" in n and "head handle" in n
               for n in result["substitutions"])


# --- the instrument can see the failure --------------------------------------


@needs_ffmpeg
def test_the_lane_check_sees_a_lane_that_is_not_in_the_render(media, tmp_path):
    """Mutation check: verify the lane graph against a render made WITHOUT the
    lane. A check that passes here would have passed on 0.27.1's output."""
    tl = _fcpxml(tmp_path, media, "m", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="6000/3000s" format="r2">
            <asset-clip ref="a4" lane="1" offset="1500/3000s" name="Red" start="0s" duration="3000/3000s" format="r2"/>
        </asset-clip>
''', "6000/3000s")
    spine_only = filtergraph.compile_timeline(tl, with_lanes=False)
    out = Path(tmp_path) / "spine_only.mp4"
    subprocess.run(filtergraph.graph_to_args(spine_only, str(out), height=180),
                   capture_output=True, timeout=120, check=True)
    with_lane = filtergraph.compile_timeline(tl, render._probes(tl))
    rows = render.verify_lanes(with_lane, str(out), 180)
    assert [v for _, v, _ in rows] == [False]


@needs_ffmpeg
def test_a_render_with_no_readable_video_is_an_error_not_a_preview(media, tmp_path, monkeypatch):
    tl = _fcpxml(tmp_path, media, "e", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="3000/3000s" format="r2"/>
''', "3000/3000s")
    monkeypatch.setattr(render, "probe_duration", lambda path: None)
    result, _ = _render(tl, tmp_path, "e")
    assert result["path"] is None
    assert "no readable video" in result["error"]


@needs_ffmpeg
def test_a_render_off_by_more_than_a_frame_is_unverified(media, tmp_path, monkeypatch):
    tl = _fcpxml(tmp_path, media, "d", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="3000/3000s" format="r2"/>
''', "3000/3000s")
    monkeypatch.setattr(render, "probe_duration", lambda path: Fraction(1) + Fraction(2, 24))
    result, _ = _render(tl, tmp_path, "d")
    assert result["path"] is not None and result["verified"] is False


@needs_ffmpeg
def test_ffmpeg_refusing_the_crossfade_graph_falls_back_to_hard_cuts(media, tmp_path, monkeypatch):
    """One bad transition must not cost the whole preview (John's suggestion)."""
    tl = _fcpxml(tmp_path, media, "f", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="6000/3000s" format="r2"/>
        <transition name="Cross Dissolve" offset="4500/3000s" duration="3000/3000s"/>
        <asset-clip ref="a1" offset="6000/3000s" name="A2" start="9000/3000s" duration="6000/3000s" format="r2"/>
''', "12000/3000s")
    real = render.graph_to_args

    def refuse_xfade(graph, out_path, height=480):
        args = real(graph, out_path, height=height)
        index = args.index("-filter_complex") + 1
        args[index] = args[index].replace("xfade=transition=fade", "xfade=transition=nonsense")
        return args

    monkeypatch.setattr(render, "graph_to_args", refuse_xfade)
    result, _ = _render(tl, tmp_path, "f")
    assert result["error"] is None, result["error"]
    assert result["transitions"] == ()
    assert any("ffmpeg refused the full graph" in n for n in result["substitutions"])
    assert any("rendered as a hard cut" in n for n in result["substitutions"])
    assert result["verified"] is True


# --- the parser half ---------------------------------------------------------


def test_the_parser_reads_a_transitions_own_offset_and_leaves_its_type_empty(tmp_path):
    tl = _fcpxml(tmp_path, tmp_path, "p", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="6000/3000s"/>
        <transition name="Color Panels" offset="6000/3000s" duration="3000/3000s"/>
        <asset-clip ref="a1" offset="6000/3000s" name="B" start="0s" duration="6000/3000s"/>
''', "12000/3000s")
    transition = tl.transitions[0]
    assert transition.lead.frames == 0          # starts AT the cut, not centred
    assert transition.transition_type == ""
    assert transition.start.frames == 60


def test_a_spine_title_pushes_the_clips_after_it(tmp_path):
    """Before 0.28 a title on the primary storyline took no time, so every
    clip after it was placed early by the title's length."""
    tl = _fcpxml(tmp_path, tmp_path, "title", '''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="3000/3000s"/>
        <title ref="a1" offset="3000/3000s" name="Card" duration="6000/3000s"/>
        <asset-clip ref="a1" offset="9000/3000s" name="B" start="0s" duration="3000/3000s"/>
''', "12000/3000s")
    assert [c.name for c in tl.clips] == ["A", "B"]
    assert tl.clips[1].start.seconds == pytest.approx(3.0)


def test_the_parser_carries_the_asset_clock_still_size_and_picture(tmp_path):
    tl = _fcpxml(tmp_path, tmp_path, "m", '''
        <video ref="a3" offset="0s" name="Still" start="3600s" duration="3000/3000s"><adjust-conform type="none"/></video>
        <asset-clip ref="a6" offset="3000/3000s" name="TC" start="10803000/3000s" duration="3000/3000s">
            <adjust-transform position="10 -5" scale="0.5 0.75"/>
            <adjust-crop mode="trim"/>
        </asset-clip>
''', "6000/3000s")
    still, tc = tl.clips
    assert still.media.still is True and still.conform == "none"
    assert tc.media.origin == Fraction(3600) and tc.media.still is False
    assert (tc.media.width, tc.media.height) == (1920, 1080)
    assert tc.transform.position == (10.0, -5.0) and tc.transform.scale == (0.5, 0.75)
    assert tc.effects == ("adjust-crop",)


@needs_ffmpeg
def test_the_contact_sheet_reads_stills_and_timecoded_media(media, tmp_path):
    tl = _fcpxml(tmp_path, media, "sheet", '''
        <video ref="a3" offset="0s" name="Still" start="3600s" duration="3000/3000s"/>
        <asset-clip ref="a6" offset="3000/3000s" name="TC" start="10803000/3000s" duration="3000/3000s"/>
''', "6000/3000s")
    graph = filtergraph.compile_timeline(tl)
    clips = [s for s in graph.segments if s.kind == "clip"]
    for index, segment in enumerate(clips):
        frame = render.render_frame(
            segment.source, segment.file_time(segment.duration / 2), str(tmp_path / f"f{index}.png")
        )
        assert frame is not None, segment.label


@needs_ffmpeg
def test_a_gap_between_clips_keeps_its_time(media, tmp_path):
    """Before 0.28 the spine was concatenated clip to clip, so a gap vanished
    and everything after it played early by the gap's length."""
    tl = _fcpxml(tmp_path, media, "gap", '''
        <asset-clip ref="a5" offset="0s" name="Blue" start="0s" duration="6000/3000s" format="r1"/>
        <gap name="Gap" offset="6000/3000s" start="3600s" duration="6000/3000s"/>
        <asset-clip ref="a4" offset="12000/3000s" name="Red" start="0s" duration="6000/3000s" format="r2"/>
''', "18000/3000s")
    result, out = _render(tl, tmp_path, "gap")
    assert result["error"] is None and result["verified"] is True
    assert result["expected"] == Fraction(6)
    assert _is(_rgb(out, 1.0), "blue")
    assert _is(_rgb(out, 3.0), "black")
    assert _is(_rgb(out, 5.0), "red")



@needs_ffmpeg
@pytest.mark.parametrize("with_transition", [True, False])
def test_clips_of_different_shapes_render_onto_one_canvas(media, tmp_path, with_transition):
    """The error John quoted for case 1, reproduced on 0.27.2 with ffmpeg 9.0.1
    AND 9.0.2: "Nothing was written into output file, because at least one of
    its streams received no packets". Each source was scaled to its own width
    (854x480 beside 270x480), and concat and xfade both refuse inputs of
    different sizes. Every piece now lands on one canvas of the SEQUENCE's
    shape, fit the way Final Cut's default conform fits it: a vertical phone
    clip in a 16:9 sequence is pillarboxed."""
    transition = (
        '<transition name="Cross Dissolve" offset="4500/3000s" duration="3000/3000s"/>'
        if with_transition else ""
    )
    tl = _fcpxml(tmp_path, media, f"shapes{int(with_transition)}", f'''
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="6000/3000s" format="r2"/>
        {transition}
        <asset-clip ref="a8" offset="6000/3000s" name="Vertical" start="3000/3000s" duration="6000/3000s" format="r9"/>
''', "12000/3000s")
    result, out = _render(tl, tmp_path, f"shapes{int(with_transition)}")
    assert result["error"] is None, result["error"]
    assert result["verified"] is True
    centre = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "3.5", "-i", str(out), "-frames:v", "1",
         "-vf", "crop=8:8:iw/2-4:ih/2-4,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    side = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "3.5", "-i", str(out), "-frames:v", "1",
         "-vf", "crop=8:8:4:ih/2-4,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    assert _is(tuple(centre[:3]), "red") and _is(tuple(side[:3]), "black")
