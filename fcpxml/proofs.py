"""The proof images: pictures of an edit that carry its names, timecodes and cuts.

These are the only pictures a Claude Code user gets of a timeline, and they
are what the model itself looks at (it takes no video), so every one is laid
out to be READ: by a person at 1:1 or on a phone, and by the model at the
size it receives. Each one is a :class:`raster.Raster` composed here, written
as a PNG, and checked against the legibility rule
(:func:`raster.legibility_problems`) before it is returned.

- :func:`contact_sheet`: one composed sheet per page, every video clip in
  time order (connected clips badged with their lane), each with its number,
  name, length in frames, record timecode and source in and out, a bowtie and
  the dissolve's length where a transition joins two cells, and a header
  (project, duration, frame rate, clip counts, the file's sha256 prefix, the
  date). ``layout="phone"`` draws two columns at twice the size for a 390 px
  column.
- :func:`visual_check`: SOURCE media in bands, each band one row of frames
  over its own ruler and its own waveform on that row's time axis (Final
  Cut's filmstrip and waveform); the waveform drawn as peak and RMS levels in
  dB with the silence threshold as a line; with the timeline, the removed
  spans dimmed and marked and the frames either side of the cut shown.
- :func:`scene_strip`: the frame before and the frame after each detected
  cut, each with its own timecode, the detector's score against its
  threshold, and the list of scenes with their lengths.
- :func:`burn_in`: the proxy's burn-in, one opaque chip naming what is ON
  SCREEN (top layer first) with its source timecode, composited by ffmpeg onto
  the clean proxy (render.py calls it AFTER verify_lanes() has read the clean
  file); :func:`burned_strips` reads every frame of it back.
- :func:`text_timeline`: the inline text picture of the cut, rows in EDL order.
- :func:`picture_runs`: what fills the picture frame by frame (the top layer
  that covers the frame, connected clips included): the flash-frame check
  reads it, not the spine alone.

One rule for lengths everywhere a reader adds them up: whole frames at the
timeline's rate (``121f``), so a start plus a length is the next start.

Everything here is drawn with the bundled bitmap face; nothing calls
``drawtext``.
"""

from __future__ import annotations

import array
import datetime
import hashlib
import math
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

from . import filtergraph as fg
from . import raster as rs
from .rational import nominal_fps, rational_fps
from .words import plural

GROUND, PANEL = rs.colour("ground"), rs.colour("panel")
INK, MUTE, ACCENT = rs.colour("ink"), rs.colour("mute"), rs.colour("accent")
LINE, LINE_STRONG, DANGER = rs.colour("line"), rs.colour("line-strong"), rs.colour("danger")
WHITE = rs.colour("on-media")


def timecode(seconds: Fraction, fps) -> str:
    """HH:MM:SS:FF of an instant at a frame rate (non-drop, nominal frames
    per labelled second: 23.98 counts 0..23)."""
    exact = rational_fps(fps)
    nominal = nominal_fps(fps)
    frames = int(Fraction(seconds) * exact + Fraction(1, 2))  # the frame the instant falls in, to the nearest
    return frames_timecode(frames, nominal)


def frames_timecode(frames: int, nominal: int) -> str:
    ff = frames % nominal
    total = frames // nominal
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}:{ff:02d}"


def short_hash(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:7]
    except OSError:
        return "-------"


def _seconds_label(seconds: Fraction) -> str:
    """Seconds to a tenth, half up and exact (a float's half-to-even shows
    5.25 s as 5.2): "5.3s", "1m02.5s". Only where nothing is added up."""
    tenths = int(Fraction(seconds) * 10 + Fraction(1, 2))
    whole, tenth = divmod(tenths, 10)
    if whole < 60:
        return f"{whole}.{tenth}s"
    return f"{whole // 60}m{whole % 60:02d}.{tenth}s"


# ---- the picture, frame by frame ---------------------------------------------------------

def frame_of(t) -> int:
    """The output frame an instant falls on (the renderer's own quantising)."""
    return fg._frame(Fraction(t))


def length_label(frames: int) -> str:
    """The one length rule: whole frames."""
    return f"{frames}f"


def counts_label(timeline) -> str:
    """The clip count, worded one way in every output: "6 clips + 1 connected"."""
    n = len(getattr(timeline, "clips", []) or [])
    m = len(getattr(timeline, "connected_clips", []) or [])
    return f"{n} clip{'' if n == 1 else 's'} + {m} connected"


def lane_name(segment) -> str:
    """A clip's name with its lane when it is not on the spine: "Bench (L1)"."""
    return segment.label + (f" (L{segment.lane})" if segment.lane else "")


def source_name(segment) -> str:
    return Path(segment.source).name if segment.source else segment.label


def source_tc(segment, into: Fraction, fps) -> str:
    """The source timecode, on the asset's clock as Final Cut shows it, of the
    frame ``into`` seconds after the clip's in-point."""
    return timecode(segment.src_in + into, fps)


def exact_frame(source: str, t: Fraction, fps, width: int, height: int) -> Optional[rs.Raster]:
    """The frame a timecode names, not the next one: ffmpeg returns the first
    frame whose time is at or after the seek, so seek a quarter of a frame
    before the labelled frame's own start."""
    rate = rational_fps(fps)
    k = int(Fraction(t) * rate + Fraction(1, 2))
    return rs.frame(source, max(Fraction(0), Fraction(k) / rate - 1 / (4 * rate)), width, height)


def video_segments(graph) -> list:
    """The clips that put a picture on screen: a disabled clip (enabled="0")
    is in the cut and in no picture, so every reader of the picture (layers,
    flashes, the sheet, the burn-in) leaves it out, as the render does."""
    return [s for s in graph.segments if s.kind == "clip" and s.has_video and getattr(s, "enabled", True)]


def covers(graph, segment, height: int = 720) -> bool:
    """Does this clip's picture fill the whole frame (no PiP, no letterbox)?"""
    cw, ch = fg.canvas_size(graph, height)
    w, h, x, y = fg.placement(graph, segment, height)
    return x <= 0 and y <= 0 and x + w >= cw and y + h >= ch


def stack_at(graph, k: int) -> list:
    """Every video clip at output frame ``k``, top layer first."""
    present = [s for s in video_segments(graph) if frame_of(s.tl_in) <= k < frame_of(s.tl_out)]
    return sorted(present, key=lambda s: -s.lane)


def layers_at(graph, k: int, height: int = 720) -> list:
    """The clips seen at output frame ``k``, top layer first, down to (and
    including) the first one that fills the frame: what is ON SCREEN."""
    out = []
    for s in stack_at(graph, k):
        out.append(s)
        if covers(graph, s, height):
            break
    return out


def picture_runs(graph, height: int = 720) -> list:
    """[(first frame, frame after the last, layers)] where the picture's layers
    stay the same: the edit as it is SEEN, connected clips included."""
    runs, last, first = [], None, 0
    for k in range(graph.total_frames):
        layers = tuple(layers_at(graph, k, height))
        key = tuple(id(s) for s in layers)
        if last is None:
            last, first, held = key, k, layers
        elif key != last:
            runs.append((first, k, held))
            last, first, held = key, k, layers
    if last is not None:
        runs.append((first, graph.total_frames, held))
    return runs


def picture_name(layers) -> str:
    """"Bench (L1) over Squat", or "black" when nothing is on screen."""
    return " over ".join(lane_name(s) for s in layers) if layers else "black"


def picture_flashes(graph, warning_frames: int = 6, height: int = 720) -> list:
    """Runs of the composited picture shorter than ``warning_frames``: a
    flash anywhere in what is seen, including a frame of the spine showing
    between a connected clip and the next cut. [(start frame, frames, what,
    before, after)]."""
    runs = picture_runs(graph, height)
    out = []
    for i, (a, b, layers) in enumerate(runs):
        if b - a >= warning_frames or not layers:
            continue
        before = picture_name(runs[i - 1][2]) if i > 0 else ""
        after = picture_name(runs[i + 1][2]) if i + 1 < len(runs) else ""
        out.append((a, b - a, picture_name(layers), before, after))
    return out


# ---- the contact sheet --------------------------------------------------------------

@dataclass(frozen=True)
class SheetLayout:
    """A page geometry. ``scale`` is how many pixels each font pixel takes;
    ``delivered`` is the size the page is meant to be seen at, as a fraction
    of its pixels (a phone shows the 780 px phone page in a 390 px column)."""

    name: str
    width: int
    max_height: int
    columns: int
    margin: int
    gutter: int
    scale: int
    delivered: float


DESKTOP = SheetLayout("desktop", 1344, 896, 4, 16, 24, 1, 1.0)
PHONE = SheetLayout("phone", 780, 1568, 2, 12, 32, 2, 0.5)
LAYOUTS = {"desktop": DESKTOP, "phone": PHONE}


@dataclass(frozen=True)
class Cell:
    """One video clip on the sheet."""

    index: int            # 1-based, in record order (the text timeline's event number)
    name: str
    lane: int
    start: int            # record frame of its first frame
    frames: int           # length in frames
    source: str
    src_in: Fraction      # source in, asset clock
    src_out: Fraction
    at: Fraction          # file time of the frame shown (the clip's middle)
    missing: bool
    transition_in: str = ""   # the transition INTO this (spine) cell: its length label
    after: int = 0            # the index of the spine cell it comes out of, when it has one
    over: int = 0             # a connected cell: the spine cell it plays over
    flash: tuple = ()         # the flash this cell's picture IS: ("flash 1f 00:00:20:03", "src 00:00:17:23")
    notes: tuple = ()         # grey lines: "ends 1f before #6", "dup 66f with #5"


def record_order(graph) -> list:
    """Video clips in record order: by first frame, the spine before a lane
    at the same frame, higher lanes after lower."""
    return sorted(video_segments(graph), key=lambda s: (frame_of(s.tl_in), s.lane != 0, s.lane))


def sheet_cells(graph) -> list:
    """Every video clip of a compiled timeline as a cell, in record order,
    with the transition that leads into each spine cell."""
    spine = [s for s in graph.segments if s.lane == 0]
    into = {}
    for t in graph.transitions:
        if t.boundary + 1 < len(spine):
            into[id(spine[t.boundary + 1])] = (length_label(t.end - t.start), id(spine[t.boundary]))
    # numbered over EVERY clip (audio ones too) in record order, so a cell's
    # number is the text timeline's event number; only pictures get a cell
    every = sorted([s for s in graph.segments if s.kind == "clip"],
                   key=lambda s: (frame_of(s.tl_in), s.lane != 0, s.lane))
    index = {id(s): i for i, s in enumerate(every, 1)}
    order = record_order(graph)
    # one flash is one red badge, on the card whose picture flashes (diagnose
    # counts it once); the clip whose early end left it showing gets a grey
    # line saying so
    flashed, notes = {}, {}
    runs = picture_runs(graph)
    nominal = nominal_fps(24)
    for i, (fa, fb, layers) in enumerate(runs):
        if fb - fa < 6 and layers and 0 < i < len(runs) - 1:
            top = layers[0]
            src = int((top.src_in + Fraction(fa, fg.FPS) - top.tl_in) * nominal + Fraction(1, 2))
            flashed.setdefault(id(top), (f"flash {length_label(fb - fa)} {frames_timecode(fa, nominal)}",
                                         f"src {frames_timecode(src, nominal)}"))
            before = runs[i - 1][2][0] if runs[i - 1][2] else None
            after = runs[i + 1][2][0] if runs[i + 1][2] else None
            if before is not None and before is not top and after is not None and id(after) in index:
                notes.setdefault(id(before), []).append(f"ends {length_label(fb - fa)} before #{index[id(after)]}")
    # frames shown twice (diagnose's Duplicate frames WARN), on both cards
    pictures = [s for s in order if s.source and not s.missing]
    for a_i, a in enumerate(pictures):
        for b in pictures[a_i + 1:]:
            if a.source != b.source:
                continue
            fa_in, fa_out = (int(v * nominal + Fraction(1, 2)) for v in (a.src_in, a.src_out))
            fb_in, fb_out = (int(v * nominal + Fraction(1, 2)) for v in (b.src_in, b.src_out))
            shared = min(fa_out, fb_out) - max(fa_in, fb_in)
            if shared > 0:
                notes.setdefault(id(a), []).append(f"dup {length_label(shared)} with #{index[id(b)]}")
                notes.setdefault(id(b), []).append(f"dup {length_label(shared)} with #{index[id(a)]}")
    cells = []
    for s in order:
        n = index[id(s)]
        label, outgoing = into.get(id(s), ("", None))
        host = 0
        if s.lane:
            under = next((h for h in spine if h.kind == "clip" and frame_of(h.tl_in) <= frame_of(s.tl_in) < frame_of(h.tl_out)), None)
            host = index.get(id(under), 0) if under is not None else 0
        cells.append(Cell(n, s.label, s.lane, frame_of(s.tl_in), frame_of(s.tl_out) - frame_of(s.tl_in),
                          s.source, s.src_in, s.src_out, s.file_time(s.duration / 2), s.missing,
                          label, index.get(outgoing, 0), host, flashed.get(id(s), ()),
                          tuple(notes.get(id(s), ()))))
    return cells


def _bowtie(r: rs.Raster, cx: int, cy: int, size: int) -> None:
    """The transition glyph: two triangles pointing at each other and meeting
    on the cut (the bowtie Final Cut draws on a transition), in the accent.
    ``size`` is its height; it is as wide."""
    half = size // 2
    for dx in range(-half, half + 1):
        span = int(round(abs(dx) * 0.9))
        r.fill(cx + dx, cy - span, 1, 2 * span + 1, ACCENT)


def glyph_size(lay: "SheetLayout") -> int:
    """The transition glyph's height (it is as wide): at least GLYPH_MIN at
    the delivered size, and narrower than the gutter it is centred in, so it
    covers neither picture."""
    return max(int(round(rs.GLYPH_MIN / lay.delivered)), min(20 * lay.scale, lay.gutter - 4))


def wrap_items(items: list, f: rs.Face, width: int, sep: str = " · ") -> list:
    """Join ``items`` with ``sep`` into lines no wider than ``width``,
    breaking only between items (an item longer than a line is wrapped by
    :func:`raster.fit`, never cut)."""
    lines, line = [], ""
    for item in items:
        candidate = f"{line}{sep}{item}" if line else item
        if f.measure(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        pieces = rs.fit(item, f, width)
        lines += pieces[:-1]
        line = pieces[-1]
    if line:
        lines.append(line)
    return lines


def _cell_text(cell: Cell, f: rs.Face, width: int, nominal: int) -> list:
    """The cell's label lines as (text, colour, right-aligned extra):
    name (wrapped, never cut) with its length at the right of its last line,
    then "rec <in>" (cyan: record time) and "src <in>" (an in-point and a
    length make the range: no out-point is printed, so no "to" can be read
    two ways), then, in grey, what only some cells have: "over #4" for a
    connected clip, "dissolve 12f in" for the clip a transition leads into,
    "ends 1f before #6", "dup 66f with #5"; and in red the flash this
    cell's picture is ("flash 1f 00:00:20:03", "· src 00:00:17:23")."""
    length = length_label(cell.frames)
    room = width - f.measure(length) - 2 * f.advance
    name_lines = rs.fit(cell.name, f, room)
    lines = [(n, INK, length if i == len(name_lines) - 1 else "") for i, n in enumerate(name_lines)]
    src_in = frames_timecode(int(cell.src_in * nominal + Fraction(1, 2)), nominal)
    lines.append((f"rec {frames_timecode(cell.start, nominal)}", ACCENT, ""))
    lines.append((f"src {src_in}", MUTE, ""))
    if cell.over:
        lines.append((f"over #{cell.over}", MUTE, ""))
    if cell.transition_in:
        lines.append((f"dissolve {cell.transition_in} in", MUTE, ""))
    for note in cell.notes:
        lines += [(t, MUTE, "") for t in rs.fit(note, f, width)]
    if cell.flash:
        for i, item in enumerate(cell.flash):
            text = item if i == 0 else "· " + item  # a continuation, never read as the card's own src
            lines += [(t, DANGER, "") for t in rs.fit(text, f, width)]  # red: failure (a flash frame)
    return lines


def contact_sheet(timeline, graph, out_dir: Path, *, layout: str = "desktop", filepath: str = "",
                  today: Optional[str] = None) -> dict:
    """Compose the sheet's pages. Returns {"pages": [paths], "rasters": [...],
    "cells": [Cell], "problems": [...], "layout": SheetLayout, ...}."""
    lay = LAYOUTS[layout]
    label = rs.face(24, lay.scale)
    title = rs.face(32, 1) if lay.scale == 1 else rs.face(24, lay.scale)
    fps = getattr(timeline, "frame_rate", 24) or 24
    nominal = nominal_fps(fps)
    cells = sheet_cells(graph)
    inner = lay.width - 2 * lay.margin
    cell_w = (inner - (lay.columns - 1) * lay.gutter) // lay.columns
    # even, so every text row of the phone page (drawn at 2x) sits on the
    # even pixel grid and halves to the exact 1x glyph at 390 px
    cell_h = cell_w * 9 // 16 // 2 * 2
    lines_max = max([len(_cell_text(c, label, cell_w, nominal)) for c in cells] or [4])
    band = lines_max * label.height + 8
    row_h = cell_h + band + lay.gutter

    project = str(getattr(timeline, "name", "") or "timeline")
    meta = [frames_timecode(graph.total_frames, nominal),
            f"{float(rational_fps(fps)):.3f}".rstrip("0").rstrip(".") + " fps",
            counts_label(timeline), f"sha256 {short_hash(filepath)}",
            today or datetime.date.today().isoformat()]

    def header(tag: str) -> list:
        """(text, face, colour) lines: the project name (beside the page
        count, when there is one), then the facts, broken between items."""
        room = inner - (label.measure(tag) + 3 * lay.gutter if tag else 0)
        lines = [(line, title, INK) for line in rs.fit(project, title, room)]
        return lines + [(line, label, MUTE) for line in wrap_items(meta, label, inner)]

    def head_height(tag: str) -> int:
        return sum(f.height for _, f, _ in header(tag)) + lay.margin

    head_h = head_height("page 9/9" if len(cells) > rs.CELLS_MAX else "")
    rows_fit = max(1, (lay.max_height - lay.margin * 3 - head_h - label.height + lay.gutter) // row_h)
    per_page = min(rs.CELLS_MAX, rows_fit * lay.columns)
    pages = [cells[i:i + per_page] for i in range(0, len(cells), per_page)] or [[]]
    head_h = head_height(f"page {len(pages)}/{len(pages)}" if len(pages) > 1 else "")
    size = glyph_size(lay)
    out = {"pages": [], "rasters": [], "cells": cells, "problems": [], "layout": lay, "per_page": per_page,
           "glyphs": [], "boxes": [],
           "geometry": {"cell_w": cell_w, "cell_h": cell_h, "band": band, "row_h": row_h, "head_h": head_h,
                        "glyph": size}}
    for p, page in enumerate(pages):
        rows = max(1, -(-len(page) // lay.columns))
        rest = pages[p + 1:]
        foot = ""
        if rest:
            first_rest, last_rest = rest[0][0], rest[-1][-1]
            span = f"#{first_rest.index}" + (f"-#{last_rest.index}" if last_rest is not first_rest else "")
            joined = any(c.transition_in for pg in rest for c in pg)
            foot = f"page {p + 2}: {span}{' and the dissolve' if joined else ''}"
        foot_lines = rs.fit(foot, label, lay.width - 2 * lay.margin) if foot else []  # wrapped, never cut
        height = (lay.margin + head_h + rows * row_h - lay.gutter + lay.margin
                  + (len(foot_lines) * label.height + lay.margin if foot else 0))
        r = rs.Raster.filled(lay.width, height, GROUND)
        for i, line in enumerate(foot_lines):
            r.text(line, label, lay.margin, height - lay.margin - (len(foot_lines) - i) * label.height, MUTE)
        tag = f"page {p + 1}/{len(pages)}" if len(pages) > 1 else ""
        y = lay.margin
        for line, f, colour in header(tag):
            r.text(line, f, lay.margin, y, colour)
            y += f.height
        if tag:
            r.text(tag, label, lay.width - lay.margin - label.measure(tag), lay.margin, MUTE)
        top = lay.margin + head_h
        glyphs = []
        placed = {}
        for k, cell in enumerate(page):
            col, row = k % lay.columns, k // lay.columns
            x = lay.margin + col * (cell_w + lay.gutter)
            cy = top + row * row_h
            placed[cell.index] = (col, row, x, cy)
            picture = None if cell.missing else rs.frame(cell.source, cell.at, cell_w, cell_h)
            if picture is not None:
                r.paste(picture, x, cy)
            else:
                r.fill(x, cy, cell_w, cell_h, PANEL)
                note = "media missing"
                r.text(note, label, x + (cell_w - label.measure(note)) // 2, cy + (cell_h - label.height) // 2,
                       DANGER)  # red: failure (the clip's media is missing)
            chip = f"#{cell.index}" + (f" L{cell.lane}" if cell.lane else "")
            pad = 4 * lay.scale
            r.fill(x, cy, label.measure(chip) + 2 * pad, label.height + pad, GROUND)
            r.text(chip, label, x + pad, cy + pad // 2, INK)  # cyan is record time only
            ly = cy + cell_h + 4
            for text, colour, right in _cell_text(cell, label, cell_w, nominal):
                r.text(text, label, x, ly, colour)
                if right:
                    r.text(right, label, x + cell_w - label.measure(right), ly, MUTE)
                ly += label.height
            # the transition INTO this cell sits on its cut: the bowtie alone,
            # centred in the gutter when the cell it comes out of is just
            # before it on the row (it fits there and covers neither
            # picture), else on a chip at this picture's left edge; the
            # card's "dissolve 12f in" line says what it is
            if cell.transition_in:
                gy = cy + cell_h // 2
                beside = cell.after in placed and placed[cell.after][1] == row and placed[cell.after][0] == col - 1
                if beside:
                    gx = x - (lay.gutter + 1) // 2
                else:
                    r.fill(x, gy - size // 2 - pad, size + 2 * pad, size + 2 * pad, GROUND)
                    gx = x + pad + size // 2
                _bowtie(r, gx, gy, size)
                glyphs.append({"into": cell.index, "x": gx, "y": gy, "size": size, "page": p + 1,
                               "label": cell.transition_in, "beside": beside})
            # the picture that flashes: a red frame round it, its badge in
            # red under the card (over the picture it hid half of it)
            if cell.flash:
                edge = 2 * lay.scale
                for bx, by, bw, bh in ((x, cy, cell_w, edge), (x, cy + cell_h - edge, cell_w, edge),
                                       (x, cy, edge, cell_h), (x + cell_w - edge, cy, edge, cell_h)):
                    r.fill(bx, by, bw, bh, DANGER)  # red: failure (a flash frame in the picture)
            out["boxes"].append({"page": p + 1, "index": cell.index, "x": x, "y": cy, "w": cell_w,
                                 "h": cell_h + band})
        out["glyphs"] += glyphs
        out["problems"] += [f"page {p + 1}: {x}" for x in
                            rs.legibility_problems(r, lay.delivered, cells=len(page)) + rs.collisions(r)]
        path = out_dir / f"sheet_{lay.name}_{p + 1:02d}.png"
        if r.save(str(path)):
            out["pages"].append(str(path))
        out["rasters"].append(r)
    return out


# ---- the filmstrip and waveform check -------------------------------------------------

CHECK_WIDTH = 1232          # 44 patches wide: 35 rows (980 px) keep it inside the budget
CHECK_FRAME_W, CHECK_FRAME_H = 240, 135
CHECK_PER_BAND = 4          # frames in one band, each in a 286 px slot
CHECK_FRAMES = 8            # default: two bands of four
DB_COLUMN = 192             # the dB scale's labels, right of each waveform ("-40 silence")
JOIN_W, JOIN_H = 160, 90    # the join's two frames: each centred over its 240 px rec/src caption
RULER_LABEL_PAD = 3         # a ruler label starts this far right of its tick
JOIN_GAP = 32               # the join sits this far under the last band, under a rule
SILENCE_DB = -40.0          # a window this quiet or quieter is silence
SILENCE_MIN = Fraction(3, 10)  # seconds: shorter quiet is a pause, not a span to label
FLOOR_DB = -60.0            # the waveform's bottom: quieter than this draws nothing
AUDIO_RATE = 8000
WAVE_H = 104                # 60 dB over 102 px: 20 dB is 34 px, so 32 px labels on 0/-20/-40 clear each other


def nice_step(duration: Fraction, width_px: int, min_px: int = 200) -> Fraction:
    """A tick step (seconds) from 1/24 s up, so ticks are at least min_px apart."""
    for step in (Fraction(1, 24), Fraction(1, 12), Fraction(1, 4), Fraction(1, 2), 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        step = Fraction(step)
        if duration == 0 or step / duration * width_px >= min_px:
            return step
    return Fraction(1200)


def ruler_ticks(start: Fraction, end: Fraction, x0: int, x1: int) -> list:
    """(time, x) for every tick of the ruler over [start, end] drawn on [x0, x1]:
    x is the time's own position, rounded to the nearest pixel."""
    step = nice_step(end - start, x1 - x0)
    first = math.ceil(start / step) * step
    out, t = [], Fraction(first)
    while t <= end:
        out.append((t, x0 + int(round(float((t - start) / (end - start) * (x1 - x0))))))
        t += step
    return out


def _samples(source: str, start: Fraction, end: Fraction) -> Optional[list]:
    """Mono 16-bit samples of the range at AUDIO_RATE, or None (no audio)."""
    if shutil.which("ffmpeg") is None:
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(float(start)),
             "-t", str(float(end - start)), "-i", str(source), "-map", "0:a:0", "-ac", "1", "-ar", str(AUDIO_RATE),
             "-f", "s16le", "pipe:1"], capture_output=True, timeout=rs.FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    data = result.stdout
    a = array.array("h")
    a.frombytes(data[: len(data) // 2 * 2])
    return list(a)


def silent_spans(samples: list, start: Fraction, rate: int = AUDIO_RATE, threshold_db: float = SILENCE_DB) -> list:
    """(span start, span end) in source seconds where 50 ms windows stay at or
    below ``threshold_db`` for SILENCE_MIN or longer."""
    window = rate // 20
    limit = 32768 * 10 ** (threshold_db / 20)
    quiet = []
    for i in range(0, len(samples), window):
        chunk = samples[i:i + window]
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk)) if chunk else 0
        quiet.append(rms <= limit)
    spans, run_start = [], None
    for i, q in enumerate(quiet + [False]):
        if q and run_start is None:
            run_start = i
        elif not q and run_start is not None:
            a = start + Fraction(run_start * window, rate)
            b = start + Fraction(min(i * window, len(samples)), rate)
            if b - a >= SILENCE_MIN:
                spans.append((a, b))
            run_start = None
    return spans


def db(level: float) -> float:
    """A 16-bit level in dBFS (silence floors at FLOOR_DB)."""
    return max(FLOOR_DB, 20 * math.log10(level / 32768)) if level > 0 else FLOOR_DB


def db_height(value_db: float, level: int) -> int:
    """Pixels above the baseline for a level: 0 at FLOOR_DB, ``level`` at 0 dBFS."""
    return int(round((max(FLOOR_DB, min(0.0, value_db)) - FLOOR_DB) / -FLOOR_DB * level))


def kept_ranges(graph, source: str) -> list:
    """[(file in, file out, record in)] for every clip of the timeline that
    plays ``source``, in file order: the parts of the file the edit keeps."""
    want = str(Path(source).resolve())
    out = []
    for s in video_segments(graph) + [s for s in graph.segments
                                      if s.kind == "clip" and not s.has_video and getattr(s, "enabled", True)]:
        if s.source and str(Path(s.source).resolve()) == want:
            out.append((s.file_in, s.file_in + s.duration, s.tl_in))
    return sorted(set(out))


def removed_spans(kept: list, start: Fraction, end: Fraction) -> list:
    """The stretches of [start, end] between two kept ranges: what a cut took out."""
    out = []
    for (a0, a1, _), (b0, _b1, _r) in zip(kept, kept[1:]):
        lo, hi = max(a1, start), min(b0, end)
        if hi > lo:
            out.append((lo, hi))
    return out


def check_cut(kept: list, start: Fraction, end: Fraction) -> Optional[dict]:
    """The first cut inside [start, end] where the edit leaves the file and
    comes back: {"out": file out, "in": next file in, "rec": record in of
    the next piece, "rec_out": record out of the piece before}."""
    for (a0, a1, ra), (b0, _b1, rb) in zip(kept, kept[1:]):
        if start <= a1 <= end and b0 >= a1:
            return {"out": a1, "in": b0, "rec": rb, "rec_out": ra + (a1 - a0)}
    return None


MARK_CLEAR = 8  # px a label keeps, left and right, from every line or mark it is not the leader of


def mark_problems(runs, marks, clear: int = MARK_CLEAR) -> list:
    """Every text run whose box, widened by ``clear`` px each side, meets a
    drawn line or mark (a cut line, a tick, a ruler, a removed-span bar, a
    highlight edge): a label must not sit on a line. A tick's own label sits
    under it, not beside it, so it never meets it here."""
    out = []
    for run in runs:
        x0, y0, x1, y1 = run.box()
        for (mx, my, mw, mh, kind) in marks:
            if mx < x1 + clear and x0 - clear < mx + mw and my < y1 and y0 < my + mh:
                out.append(f"{run.text!r} meets the {kind} at x={mx}")
                break
    return out


def clear_x(x: int, width: int, y: int, height: int, marks: list, low: int, high: int,
            clear: int = MARK_CLEAR) -> Optional[int]:
    """The x nearest ``x`` (rightward first) where a label ``width`` wide
    keeps ``clear`` px from every mark on its rows and stays inside [low,
    high]; None when there is no such place."""
    rows = [m for m in marks if m[1] < y + height and y < m[1] + m[3]]

    def free(at):
        return low <= at and at + width <= high and not any(
            mx < at + width + clear and at - clear < mx + mw for mx, _my, mw, _mh, _k in rows)

    spots = [x] + [mx + mw + clear for mx, _my, mw, _mh, _k in rows] + [mx - clear - width for mx, _my, _mw, _mh, _k in rows]
    spots = sorted({min(max(at, low), high - width) for at in spots}, key=lambda at: (at < x, abs(at - x)))
    return next((at for at in spots if free(at)), None)


def join_records(kept: list, before_t: Fraction, after_t: Fraction) -> list:
    """[record time of the last kept frame, record time of the first kept
    frame], each read through the kept piece (clip) that holds it: its
    record in plus how far into the piece the frame is. None where no piece
    holds it (a ``cut`` given without the timeline)."""
    out = []
    for t in (before_t, after_t):
        piece = next(((a, b, rec) for a, b, rec in kept if a <= t < b), None)
        out.append(piece[2] + (t - piece[0]) if piece else None)
    return out


def kept_sides(gap: tuple, spans: list, nominal: int) -> Optional[str]:
    """How the removed span sits inside the silence it came from, read from
    the edit and the measured audio: "silence 36f, 1f kept each side"."""
    a, b = gap
    fa, fb = int(a * nominal + Fraction(1, 2)), int(b * nominal + Fraction(1, 2))
    for sa, sb in spans:
        if sa < b and a < sb:
            fsa, fsb = int(sa * nominal + Fraction(1, 2)), int(sb * nominal + Fraction(1, 2))
            before, after = fa - fsa, fsb - fb
            if before == after and before >= 0:
                side = f"{length_label(before)} kept each side"
            else:
                def word(n, where):
                    return f"{length_label(n)} kept {where}" if n >= 0 else f"{length_label(-n)} of sound cut {where}"
                side = f"{word(before, 'before')}, {word(after, 'after')}"
            return f"silence {length_label(fsb - fsa)}, {side}"
    return None


def ink_centre(f: rs.Face, text: str) -> float:
    """Where a run's ink is centred, in pixels below the top of its cell: the
    middle of its first and last inked rows. A label is centred on a tick by
    putting this on the tick, not the cell's top."""
    rows = [r for ch in text for r, bits in enumerate(f.rows(ch)) if bits]
    if not rows:
        return f.height / 2
    return (min(rows) + max(rows) + 1) * f.scale / 2 - 0.5


def scale_ticks(threshold_db: float) -> list:
    """(dB, label) for the level scale: 0 and -20 dB, and the silence
    detector's own threshold labelled as what it is ("-40 silence"); a
    round tick within 12 dB of the threshold gives way to it."""
    ticks = [(v, f"{v:g} dB" if v == 0 else f"{v:g}") for v in (0.0, -20.0, -40.0) if abs(v - threshold_db) >= 12]
    ticks.append((threshold_db, f"{threshold_db:g} silence"))
    return sorted(ticks, key=lambda t: -t[0])


def visual_check(source: str, start: Fraction, end: Fraction, out_path: str, *, cut: Optional[Fraction] = None,
                 frames: int = CHECK_FRAMES, fps=None, kept: Optional[list] = None,
                 threshold_db: float = SILENCE_DB) -> dict:
    """Compose the check: two bands, each = its frames, its own ruler and
    its own waveform on that band's time axis, then (when there is a cut)
    the join as played, off the band grid under a rule and a heading.
    ``kept`` (from :func:`kept_ranges`) marks what the edit removed and finds
    the cut; its out-point and in-point are marked on the bands they fall in."""
    if end <= start:
        raise ValueError(f"end must be after start (got {start} -> {end})")
    if not 2 <= frames <= 2 * CHECK_PER_BAND or frames % 2:
        raise ValueError(f"frames must be an even number from 2 to {2 * CHECK_PER_BAND} "
                         f"(two bands of up to {CHECK_PER_BAND}), got {frames}")
    if cut is not None and not start <= cut <= end:
        raise ValueError(f"cut must be inside the range (got {cut} outside {start} -> {end})")
    if shutil.which("ffmpeg") is None or not Path(source).is_file():
        return {"path": None}
    fps = fps or 24
    nominal = nominal_fps(fps)
    one = Fraction(1, nominal)
    f = rs.face(32)
    margin, fw, fh = 16, CHECK_FRAME_W, CHECK_FRAME_H
    x0, x1 = margin, CHECK_WIDTH - margin - DB_COLUMN
    span = x1 - 1 - x0
    per = frames // 2
    removed = removed_spans(kept, start, end) if kept else []
    at_cut = check_cut(kept, start, end) if kept else None
    if at_cut is None and cut is not None:
        at_cut = {"out": cut, "in": cut, "rec": None, "rec_out": None}
    samples = _samples(source, start, end)
    spans = silent_spans(samples, start, threshold_db=threshold_db) if samples else []

    name = Path(source).name
    head_lines = wrap_items([name, f"src {timecode(start, fps)}-{timecode(end, fps)}",
                             length_label(int(end * nominal + Fraction(1, 2)) - int(start * nominal + Fraction(1, 2))),
                             "RMS cyan over peak grey"], f, CHECK_WIDTH - 2 * margin)
    band_top = margin + len(head_lines) * f.height + 8
    label_gap = 4
    band_h = fh + label_gap + f.height + 8 + 8 + f.height + 4 + WAVE_H + 12
    join_top = band_top + 2 * band_h - 12 + JOIN_GAP
    join_h = (1 + 8 + f.height + 4 + max(JOIN_H + 4 + 2 * f.height, 4 * f.height)) if at_cut else 0
    height = (join_top + join_h if at_cut else band_top + 2 * band_h - 12) + margin
    r = rs.Raster.filled(CHECK_WIDTH, height, GROUND)
    for i, line in enumerate(head_lines):
        r.text(line, f, margin, margin + i * f.height, INK if i == 0 else MUTE)
    marks = []  # (x, y, w, h, kind): every line and mark a label must keep clear of

    duration = end - start
    half_d = duration / 2
    bands, frames_out, joins = [], [], []
    for b in range(2):
        b0, b1 = start + b * half_d, start + (b + 1) * half_d
        y_f = band_top + b * band_h
        y_label = y_f + fh + label_gap
        y_ruler = y_label + f.height + 8
        y_wave = y_ruler + 8 + f.height + 4
        base = y_wave + WAVE_H - 1

        def to_x(t, b0=b0, b1=b1):
            return x0 + int(round(float((t - b0) / (b1 - b0) * span)))

        # the frames: each centred on its own time on this band's axis; a
        # frame the edit removed is dimmed and says so. None is lit: the
        # join is marked on the ruler at its own time, not on a nearby frame
        for i in range(per):
            t = b0 + (b1 - b0) * (2 * i + 1) / (2 * per)
            cx = to_x(t)
            fx = cx - fw // 2
            pic = exact_frame(source, t, fps, fw, fh)
            if pic is not None:
                r.paste(pic, fx, y_f)
            else:
                r.fill(fx, y_f, fw, fh, PANEL)
            gone = any(a <= t < c for a, c in removed)
            if gone:
                r.blend(fx, y_f, fw, fh, GROUND, 0.65)
                tag = "removed"
                r.fill(fx, y_f, f.measure(tag) + 8, f.height + 2, GROUND)
                r.text(tag, f, fx + 4, y_f + 1, MUTE)
            tc = timecode(t, fps)
            r.text(tc, f, cx - f.measure(tc) // 2, y_label, MUTE)
            frames_out.append({"t": t, "x": fx, "y": y_f, "w": fw, "h": fh, "band": b, "removed": gone})
        # the ruler: a tick at each time's own pixel, its label anchored at
        # the tick's right (as Final Cut anchors them), every k-th tick
        # labelled at one fixed step so no two labels meet
        r.fill(x0, y_ruler, x1 - x0, 1, LINE_STRONG)
        marks.append((x0, y_ruler, x1 - x0, 1, "ruler"))
        for a, c in removed:  # the intended edit: grey, not red
            lo, hi = max(a, b0), min(c, b1)
            if hi > lo:
                r.fill(to_x(lo), y_ruler - 2, max(1, to_x(hi) - to_x(lo)), 5, MUTE)
                marks.append((to_x(lo), y_ruler - 2, max(1, to_x(hi) - to_x(lo)), 5, "removed bar"))
        ticks = ruler_ticks(b0, b1, x0, x1 - 1)
        label_w = f.measure("00:00:00:00") + RULER_LABEL_PAD
        gaps = [bx - ax for (_t, ax), (_u, bx) in zip(ticks, ticks[1:])]
        step = 1
        while gaps and min(gaps) * step < label_w + 8:
            step += 1
        for n, (t, x) in enumerate(ticks):
            r.fill(x, y_ruler + 1, 1, 8, ACCENT)
            marks.append((x, y_ruler + 1, 1, 8, "tick"))
            # a label stays on its band's axis: one that would run past the
            # band's end (into the level scale) is not drawn
            if n % step == 0 and x + RULER_LABEL_PAD + f.measure(timecode(t, fps)) <= x1:
                r.text(timecode(t, fps), f, x + RULER_LABEL_PAD, y_ruler + 10, MUTE)
        # the waveform, rectified as Final Cut draws it: RMS in the accent over
        # the peak in grey, up from a baseline, in dB from -60 to 0; 0 and
        # -20 dB ticked at the band's edge, the -40 dB silence line dashed
        for a, c in removed + spans:
            lo, hi = max(a, b0), min(c, b1)
            if hi > lo:
                r.fill(to_x(lo), y_wave, max(1, to_x(hi) - to_x(lo)), WAVE_H, PANEL)
        level = WAVE_H - 2
        if samples:
            n_all = len(samples)
            part = samples[int((b0 - start) / duration * n_all):int((b1 - start) / duration * n_all)] or [0]
            cols, m = x1 - x0, len(part)
            for col in range(cols):
                chunk = part[col * m // cols:(col + 1) * m // cols] or [0]
                peak = max(abs(v) for v in chunk)
                rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
                hp, hr = db_height(db(peak), level), db_height(db(rms), level)
                if hp:
                    r.fill(x0 + col, base - hp, 1, hp, MUTE)
                if hr:
                    r.fill(x0 + col, base - hr, 1, hr, ACCENT)
            ht = db_height(threshold_db, level)  # the line IS the detector's threshold
            for xx in range(x0, x1, 8):
                r.fill(xx, base - ht, 4, 1, INK)
        r.fill(x0, base, x1 - x0, 1, LINE_STRONG)
        marks.append((x0, base, x1 - x0, 1, "baseline"))
        scale = []
        for value, text in scale_ticks(threshold_db):
            y = base - db_height(value, level)
            r.fill(x1 - 6, y, 6 + 6, 1, INK)  # a tick at the band's edge, into the scale column
            # the label's ink centred on its tick (a cell hung from the tick
            # read half a division low: the -40 line read as -30)
            ly = y - int(round(ink_centre(f, text)))
            r.text(text, f, x1 + 10, ly, MUTE)
            scale.append((value, y))
        # the cut's two sides on the band each falls in: the out-point
        # (where the played picture leaves the file) and the in-point (where
        # it comes back), each a mark on the ruler and a line through the waveform
        if at_cut is not None:
            for t, which in ((at_cut["out"], "out"), (at_cut["in"], "in")):
                if b0 <= t <= b1 and (which == "out" or at_cut["in"] != at_cut["out"]):
                    cx = to_x(t)
                    for k in range(7):
                        r.fill(cx - k, y_ruler - 7 + (6 - k), 2 * k + 1, 1, ACCENT)
                    marks.append((cx - 6, y_ruler - 7, 13, 7, f"{which} mark"))
                    r.fill(cx - 1, y_wave, 3, WAVE_H, ACCENT)
                    marks.append((cx - 1, y_wave, 3, WAVE_H, f"{which} line"))
                    joins.append({"band": b, "x": cx, "t": t, "which": which})
        if not samples and b == 0:
            note = "no audio in this source"
            nx = clear_x(x0 + 8, f.measure(note), y_wave + 4, f.height, marks, x0, x1)
            if nx is not None:
                r.text(note, f, nx, y_wave + 4, MUTE)
        # a silence named once, in the band it starts in, clear of every mark
        last_right = x0 - 12
        for a, c in spans:
            lo, hi = max(a, b0), min(c, b1)
            if hi <= lo:
                continue
            frames_long = int(c * nominal + Fraction(1, 2)) - int(a * nominal + Fraction(1, 2))
            if a >= b0:  # named once, in the band it starts in
                texts = (f"silence {_seconds_label(c - a)} ({length_label(frames_long)})",
                         f"silence {_seconds_label(c - a)}", _seconds_label(c - a))
            else:  # carried over from the band before: where it ends
                texts = (f"silence to {timecode(c, fps)}", f"to {timecode(c, fps)}")
            for text in texts:
                want = min(max(x0, to_x(lo) + 4), x1 - f.measure(text))
                tx = clear_x(want, f.measure(text), y_wave + 2, f.height, marks, max(x0, last_right + 12), x1)
                if tx is not None:
                    r.fill(tx - 2, y_wave + 2, f.measure(text) + 4, f.height, PANEL)
                    r.text(text, f, tx, y_wave + 2, INK)
                    last_right = tx + f.measure(text)
                    break
        bands.append({"start": b0, "end": b1, "y_frames": y_f, "y_label": y_label, "y_ruler": y_ruler,
                      "y_wave": y_wave, "ticks": ticks, "base": base, "level": level, "scale": scale,
                      "label_step": step, "bottom": y_wave + WAVE_H})
    # the join as played: off the band grid, under a rule and a heading,
    # the last kept frame and the first kept frame side by side
    pair = None
    if at_cut is not None:
        y = join_top
        r.fill(margin, y, CHECK_WIDTH - 2 * margin, 1, LINE_STRONG)
        y += 1 + 8
        # cyan is record time only: the words are white, "rec <tc>" cyan
        if at_cut["rec"] is not None:
            lead, rec_tc = "the join, as played: cut at ", "rec " + timecode(at_cut["rec"], fps)
            r.text(lead, f, margin, y, INK)
            r.text(rec_tc, f, margin + f.measure(lead), y, ACCENT)
            heading = lead + rec_tc
        else:
            heading = "the cut at src " + timecode(at_cut["out"], fps)
            r.text(heading, f, margin, y, INK)
        y += f.height + 4
        before_t, after_t = at_cut["out"] - one, at_cut["in"]
        recs = join_records(kept or [], before_t, after_t)
        cap_w = f.measure("rec 00:00:00:00")
        for j, t in enumerate((before_t, after_t)):
            px = margin + j * (cap_w + JOIN_GAP)
            fx = px + (cap_w - JOIN_W) // 2
            pic = exact_frame(source, t, fps, JOIN_W, JOIN_H)
            if pic is not None:
                r.paste(pic, fx, y)
            else:
                r.fill(fx, y, JOIN_W, JOIN_H, PANEL)
            # each frame says where it plays (rec, read through its clip)
            # and what it is in the file (src)
            cy = y + JOIN_H + 4
            fw_ = cap_w
            if recs[j] is not None:
                rec_line = "rec " + timecode(recs[j], fps)
                r.text(rec_line, f, px + (fw_ - f.measure(rec_line)) // 2, cy, ACCENT)
                cy += f.height
            src_line = "src " + timecode(t, fps)
            r.text(src_line, f, px + (fw_ - f.measure(src_line)) // 2, cy, MUTE)
        tx = margin + 2 * (cap_w + JOIN_GAP)
        lines = [(f"last kept  src {timecode(before_t, fps)}", INK), (f"first kept src {timecode(after_t, fps)}", INK)]
        gap = at_cut["in"] - at_cut["out"]
        if gap > 0:
            removed_f = int(at_cut["in"] * nominal + Fraction(1, 2)) - int(at_cut["out"] * nominal + Fraction(1, 2))
            lines.append((f"removed {timecode(at_cut['out'], fps)}-{timecode(at_cut['in'], fps)} · "
                          f"{length_label(removed_f)}", MUTE))
            sides = kept_sides((at_cut["out"], at_cut["in"]), spans, nominal)
            if sides:
                lines.append((sides, MUTE))
        ly = y
        for text, colour in lines:
            for line in rs.fit(text, f, CHECK_WIDTH - margin - tx):  # wrapped, never cut
                r.text(line, f, tx, ly, colour)
                ly += f.height
        pair = {"y": y, "rule": join_top, "heading": heading, "before": before_t, "after": after_t,
                "records": recs, "frame_w": JOIN_W, "frame_h": JOIN_H, "caption_w": cap_w,
                "lines": [heading] + [t for t, _ in lines]}
    problems = rs.legibility_problems(r) + rs.collisions(r) + mark_problems(r.runs, marks)
    path = r.save(out_path)
    return {"path": path, "raster": r, "bands": bands, "frames": frames_out, "spans": spans, "threshold_db": threshold_db,
            "removed": removed, "cut": at_cut, "pair": pair, "joins": joins, "problems": problems,
            "audio": samples is not None, "marks": marks,
            "geometry": {"x0": x0, "x1": x1, "span": span, "face": f}}


# ---- the scenes strip -----------------------------------------------------------------

def scene_strip(rows: list, out_dir: Path, fps=24, *, scenes: Optional[list] = None,
                threshold=None, backend: str = "") -> dict:
    """rows: [(clip name, source path, source cut seconds, timeline seconds,
    score or None)]. One row per cut: the frame before it and the frame after
    it, each with its own timecode, and the cut's numbers; the scene list with
    lengths under the last page. Pages of up to 3 cuts. Returns {"pages",
    "rasters", "problems"}."""
    f = rs.face(32)
    margin, gap = 16, 12
    fw, fh = 400, 225
    width = 1344
    tx = margin + 2 * (fw + gap)
    text_w = width - margin - tx
    nominal = nominal_fps(fps)
    one = Fraction(1, nominal)
    same = all(Fraction(row[2]) == Fraction(row[3]) for row in rows)
    clips = sorted({row[0] for row in rows})
    out = {"pages": [], "rasters": [], "problems": []}
    head_lines = 2
    head_h = head_lines * f.height + 8
    row_h = fh + gap
    scene_lines = (len(scenes) + 1) if scenes else 0
    per_page = 3
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)]
    for p, page in enumerate(pages):
        last = p == len(pages) - 1
        list_h = (scene_lines * f.height + 8) if (last and scenes) else 0
        height = margin + head_h + len(page) * row_h - gap + (gap + list_h if list_h else 0) + margin
        r = rs.Raster.filled(width, height, GROUND)
        first, final = p * per_page + 1, p * per_page + len(page)
        clip_word = f"in clip {clips[0]}" if len(clips) == 1 else f"in {plural(len(clips), 'clip')}"
        head = f"Cuts {first} to {final} of {len(rows)} {clip_word}: before | after"
        r.text(rs.fit(head, f, width - 2 * margin)[0], f, margin, margin, INK)
        sub = [f"{backend or 'scene'} score vs threshold {threshold:g}" if threshold is not None else
               f"{backend or 'scene'} detector"]
        if same:
            sub.append("source = timeline")
        r.text(" · ".join(sub), f, margin, margin + f.height, MUTE)
        y = margin + head_h
        for k, row in enumerate(page):
            name, source, at, tl = row[:4]
            score = row[4] if len(row) > 4 else None
            n = p * per_page + k + 1
            at, tl = Fraction(at), Fraction(tl)
            before_t, after_t = max(Fraction(0), at - one), at
            for j, t in enumerate((before_t, after_t)):
                x = margin + j * (fw + gap)
                pic = exact_frame(source, t, fps, fw, fh)
                if pic is not None:
                    r.paste(pic, x, y)
                else:
                    r.fill(x, y, fw, fh, PANEL)
                tc = timecode(t, fps)
                r.fill(x, y + fh - f.height - 4, f.measure(tc) + 12, f.height + 4, GROUND)
                r.text(tc, f, x + 6, y + fh - f.height - 2, WHITE)  # a source time: cyan is record time only
            r.fill(margin + fw + gap // 2 - 1, y, 2, fh, ACCENT)
            lines = [(f"cut #{n}", INK), (f"rec {timecode(tl, fps)}", ACCENT)]
            if not same:
                lines.append((f"src {timecode(at, fps)}", MUTE))
            if score is not None and threshold is not None:
                lines.append((f"score {score:.2f} > {threshold:g}", MUTE))
            elif threshold is not None:
                lines.append(("score not recorded", MUTE))
            for line in rs.fit(f"clip {name}", f, text_w):
                lines.append((line, INK))
            for i, (line, colour) in enumerate(lines[: fh // f.height]):
                r.text(line, f, tx, y + i * f.height, colour)
            y += row_h
        if last and scenes:
            y += 0
            r.text(f"Scenes ({len(scenes)}), record in, length:", f, margin, y, INK)
            for i, (a, b) in enumerate(scenes, 1):
                frames = int(Fraction(b) * nominal + Fraction(1, 2)) - int(Fraction(a) * nominal + Fraction(1, 2))
                line = f"{i}  {timecode(Fraction(a), fps)}  {length_label(frames)}"
                r.text(line, f, margin, y + i * f.height, MUTE)
        out["problems"] += rs.legibility_problems(r, cells=2 * len(page)) + rs.collisions(r)
        path = out_dir / f"scenes_{p + 1:02d}.png"
        if r.save(str(path)):
            out["pages"].append(str(path))
        out["rasters"].append(r)
    return out


# ---- burn-in -----------------------------------------------------------------------------

BURN_PAD = 8
SAFE = Fraction(5, 100)  # action safe: a chip stays this far in from the left and bottom edges


def action_safe(width: int, height: int) -> tuple:
    """(x, bottom margin) of 5% action safe: (64, 36) at 1280x720, (32, 18)
    at 640x360. A player's control bar covers the edge, not this."""
    return int(round(width * SAFE)), int(round(height * SAFE))


def burn_face(height: int) -> rs.Face:
    """The burn-in face for a frame this tall: 16x32 at 720 (20 px letters),
    doubled from 1080 up so the letters stay legible when the frame is
    downscaled for the model."""
    return rs.face(32, max(1, round(height / 720)))


def _chip(lines: list, f: rs.Face, width: Optional[int] = None, rows: int = 0) -> rs.Raster:
    """One burn-in chip: opaque true black, each line a list of (text,
    colour) pieces drawn left to right. Transparent outside the box when a
    fixed ``width`` or ``rows`` is larger than the text: the box sits at the
    bottom left of the strip, so a two-line frame and a three-line frame
    share one strip and one baseline."""
    used = max(sum(f.measure(t) for t, _ in line) for line in lines) + 2 * BURN_PAD
    w = max(width or 0, used)
    h = max(rows, len(lines)) * f.height + BURN_PAD
    box = len(lines) * f.height + BURN_PAD
    r = rs.Raster.filled(w, h, (0, 0, 0), alpha=0)
    r.fill(0, h - box, used, box, GROUND, 255)
    for i, line in enumerate(lines):
        x = BURN_PAD
        for text, colour in line:
            if text:
                r.text(text, f, x, h - box + BURN_PAD // 2 + i * f.height, colour, alpha=255)
            x += f.measure(text)
    return r


def burn_chip(picture: rs.Raster, lines: list) -> rs.Raster:
    """Draw a chip of ``lines`` (lists of (text, colour)) bottom-left on
    ``picture``, in place; returns the chip. A frame under 540 px tall takes
    the 12x24 face (15 px capitals), so the chip stays a label, not a band."""
    f = burn_face(picture.height) if picture.height >= 540 else rs.face(24)
    chip = _chip(lines, f)
    x, bottom = action_safe(picture.width, picture.height)
    y = picture.height - bottom - chip.height
    for yy in range(chip.height):
        for xx in range(chip.width):
            i = (yy * chip.width + xx) * 4
            if chip.px[i + 3]:
                j = ((y + yy) * picture.width + (x + xx)) * picture.channels
                picture.px[j:j + 3] = chip.px[i:i + 3]
    for run in chip.runs:
        picture.runs.append(rs.TextRun(run.text, run.x + x, run.y + y, run.face))
    return chip


def frame_lines(source: str, at: Fraction, fps) -> list:
    """The frame burn-in: "SRC Squat.mov 00:00:07:06 · f174"."""
    nominal = nominal_fps(fps)
    frame = int(Fraction(at) * rational_fps(fps) + Fraction(1, 2))
    return [[("SRC ", MUTE), (Path(source).name + " ", WHITE), (frames_timecode(frame, nominal), WHITE),
             (f" · f{frame}", MUTE)]]  # a source time: white; cyan is record time only


def fit_for_model(picture: Optional[rs.Raster]) -> Optional[rs.Raster]:
    """The picture scaled down (never up) to stay inside the vision budget
    (long edge and patch count), so the inline copy is what the model reads
    without a second downscale. The file at its path keeps full size."""
    if picture is None:
        return None
    w, h = picture.width, picture.height
    scale = 1.0
    while max(w * scale, h * scale) > rs.LONG_EDGE_MAX or rs.visual_tokens(int(w * scale), int(h * scale)) > rs.VISUAL_TOKENS_MAX:
        scale *= 0.97
    if scale == 1.0:
        return picture
    return rs.resize(picture, max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2))


def burn_text(graph, k: int, fps, height: int = 720) -> tuple:
    """The lines burned on output frame ``k``: "REC <tc> · f<k>  <what is on
    screen, top first>", "SRC <file> <source tc>" of the top layer, and,
    when it covers a clip, "over <that clip's file> <its source tc>", each
    read through its own clip: ("REC 00:00:18:00 · f432  Bench (L1)",
    "SRC Bench.mov 00:00:00:21", "over Squat.mov 00:00:15:20")."""
    nominal = nominal_fps(fps)
    layers = layers_at(graph, k, height)
    stack = stack_at(graph, k)
    hidden = stack[len(layers)] if len(stack) > len(layers) else None
    rec = f"REC {frames_timecode(k, nominal)} · f{k}  {picture_name(layers)}"
    if not layers:
        return rec, "SRC none (black)", ""
    top = layers[0]
    src = f"SRC {source_name(top)} {source_tc(top, max(Fraction(0), Fraction(k, fg.FPS) - top.tl_in), fps)}"
    over = ""
    if hidden is not None:
        over = f"over {source_name(hidden)} {source_tc(hidden, max(Fraction(0), Fraction(k, fg.FPS) - hidden.tl_in), fps)}"
    return rec, src, over


def burn_lines(texts: tuple) -> list:
    """The chip's lines as (text, colour) pieces: the record timecode cyan
    (cyan is record time only), everything else white or grey."""
    rec, src, over = texts
    head, _, what = rec.partition("  ")
    label, tc_frame = head[:4], head[4:]
    tc, _, frame = tc_frame.partition(" · ")
    # white on opaque black survives H.264 where grey thins out ("f18" read "f13")
    lines = [[(label, WHITE), (tc, ACCENT), (f" · {frame}  {what}", WHITE)], [(src, WHITE)]]
    if over:
        lines.append([(over, WHITE)])
    return lines


def burn_frames(graph, height: int, fps):
    """(frame index, RGBA strip) for every output frame: the chip naming
    what is on screen at that frame and the top layer's source timecode."""
    f = burn_face(height)
    texts = [burn_text(graph, k, fps, height) for k in range(graph.total_frames)]
    width = max([max(f.measure(t) for t in three) for three in texts] or [0]) + 2 * BURN_PAD
    rows = burn_rows(graph)
    for k, three in enumerate(texts):
        yield k, _chip(burn_lines(three), f, width, rows)


def burn_rows(graph) -> int:
    """Lines in the burn-in strip: three when a lane can cover a clip (the
    "over" line), else two."""
    return 3 if graph.lanes else 2


def burn_in(graph, clean_path: str, out_path: str, height: int, fps) -> Optional[str]:
    """Composite the burn-in over the CLEAN, already-verified render: one
    opaque chip bottom left on every frame, "REC <tc>  <what is on screen,
    top layer first>" over "SRC <file> <source tc>". Returns an error
    string, or None on success."""
    if shutil.which("ffmpeg") is None:
        return "ffmpeg is not on PATH"
    canvas_w, canvas_h = fg.canvas_size(graph, height)
    safe_x, safe_bottom = action_safe(canvas_w, canvas_h)
    frames = list(burn_frames(graph, canvas_h, fps))
    if not frames:
        return "the timeline is empty"
    strip_w, strip_h = frames[0][1].width, frames[0][1].height
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(clean_path), "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{strip_w}x{strip_h}",
            "-framerate", str(fg.FPS), "-i", "pipe:0",
            "-filter_complex", f"[0:v][1:v]overlay={safe_x}:{canvas_h - safe_bottom - strip_h}:format=auto[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-frames:v", str(len(frames)), str(out_path)]
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert proc.stdin is not None
        for _k, strip in frames:
            proc.stdin.write(bytes(strip.px))
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        code = proc.wait(timeout=600)
    except (OSError, subprocess.TimeoutExpired, BrokenPipeError) as exc:
        return f"the burn-in pass failed ({type(exc).__name__})"
    if code != 0 or not Path(out_path).is_file():
        return "the burn-in pass failed: " + " / ".join(err.strip().splitlines()[-2:])
    return None


def burned_strips(path: str, width: int, height: int, lines: int = 2) -> list:
    """Every frame's burn-in region (inside action safe, bottom left, the
    width of the picture less its margins, ``lines`` text lines tall) read
    back from a burned proxy in ONE decode: the instrument for the burn-in
    gate, which checks every frame, not a sample."""
    f = burn_face(height)
    safe_x, safe_bottom = action_safe(width, height)
    h = lines * f.height + BURN_PAD
    w = width - 2 * safe_x
    y = height - safe_bottom - h
    if shutil.which("ffmpeg") is None:
        return []
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                              "-vf", f"crop={w}:{h}:{safe_x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24",
                              "pipe:1"], capture_output=True, timeout=600).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    size = w * h * 3
    return [rs.Raster(w, h, 3, bytearray(out[i:i + size])) for i in range(0, len(out) - size + 1, size)]


def read_burn(strip: rs.Raster, height: int, line: int, chars: int, alphabet: str = rs.READABLE) -> str:
    """Read ``chars`` characters of line ``line`` (from the strip's top) off a burn-in strip."""
    f = burn_face(height)
    return rs.read_text(strip, f, BURN_PAD, BURN_PAD // 2 + line * f.height, chars, alphabet=alphabet)


# ---- the text timeline ---------------------------------------------------------------

def track_kind(segment) -> str:
    """"AV" for a clip with picture and sound, "V" picture only, "A" sound only."""
    return ("A" if not segment.has_video else "AV" if segment.has_audio else "V")


LEGEND = "^ marker, [=] clip, X dissolve (D in the list), ! flash"


def text_timeline(timeline, graph, width: int = 64, filepath: str = "") -> str:
    """The inline timeline a Claude Code user reads: a time row and a ruler
    (its last tick labelled), a marker row, the lanes above the spine, the
    spine (a transition is an X on its cut), the lanes below it, each clip a
    bar at its place with its name inside when it fits (after it, on a lane,
    when the row is free there); then every event in record order, EDL
    style: number, track, record in and out, length in frames, clip, source
    file, source in and out. Markers and transitions sit at their place in
    that list. Render notes are not part of it (preview_render reports them)."""
    fps = getattr(timeline, "frame_rate", 24) or 24
    nominal = nominal_fps(fps)
    total = graph.total
    name = str(getattr(timeline, "name", "timeline") or "timeline")
    rate = f"{float(rational_fps(fps)):.3f}".rstrip("0").rstrip(".")
    head = (f"{name} — {frames_timecode(graph.total_frames, nominal)} · {length_label(graph.total_frames)} · "
            f"{rate} fps · {counts_label(timeline)}" + (f" · sha256 {short_hash(filepath)}" if filepath else ""))
    if total <= 0:
        return head + "\n(empty timeline)"
    gutter = 10

    def col(t: Fraction) -> int:
        return min(width - 1, int(Fraction(t) / total * width))

    def bar(segment) -> str:
        a, b = col(segment.tl_in), max(col(segment.tl_in) + 1, int(segment.tl_out / total * width))
        span = b - a
        if segment.kind == "gap":
            return "." * span
        inner = segment.label if len(segment.label) <= span - 2 else ""
        # a disabled clip is drawn dashed: there, and off
        fill = "=" if getattr(segment, "enabled", True) else "-"
        return ("[" + inner.ljust(span - 2, fill) + "]") if span >= 2 else "|"

    def row(label: str, segments, name_after: bool = False) -> str:
        cells = [" "] * width
        for s in sorted(segments, key=lambda s: s.tl_in):
            piece = bar(s)
            a = col(s.tl_in)
            for i, ch in enumerate(piece):
                if a + i < width:
                    cells[a + i] = ch
            if name_after and s.kind == "clip" and s.label not in piece:
                after = a + len(piece) + 1
                text = s.label
                if after + len(text) <= width and all(c == " " for c in cells[after - 1:after + len(text)]):
                    cells[after:after + len(text)] = list(text)
        return f"{label:<{gutter}}" + "".join(cells).rstrip()

    lines = [head]
    step = nice_step(total, width, min_px=14)
    ruler, labels = [" "] * width, [" "] * (width + 8)
    ticks = []
    t = Fraction(0)
    while t <= total:
        ticks.append((t, col(t)))
        t += step
    for t, c in ticks:
        ruler[c] = "|"
        text = _seconds_label(t)
        if all(ch == " " for ch in labels[max(0, c - 1):c + len(text) + 1]):
            labels[c:c + len(text)] = list(text)
    lines.append(f"{'time':<{gutter}}" + "".join(labels).rstrip())
    lines.append(f"{'':<{gutter}}" + "".join(ruler).rstrip())
    off = any(not getattr(s, "enabled", True) for s in graph.segments if s.kind == "clip")
    lines.append(f"{'legend':<{gutter}}" + LEGEND + (", [-] off (disabled)" if off else ""))

    # markers on the timeline and on its clips, at their record time
    origin = Fraction(str(getattr(timeline, "origin_seconds", 0) or 0)).limit_denominator(100000)
    placed = []
    for m in list(getattr(timeline, "markers", []) or []):
        placed.append((m, getattr(m, "timeline_start", None) or getattr(m, "position", None)))
    for c in list(getattr(timeline, "clips", []) or []) + list(getattr(timeline, "connected_clips", []) or []):
        for m in getattr(c, "markers", []) or []:
            placed.append((m, getattr(m, "position", None)))
    markers = []
    for m, at in placed:
        seconds = getattr(at, "seconds", at)
        if seconds is None:
            continue
        # a marker the parser placed (timeline_start) is on the sequence clock
        # already; only a raw one (a sequence marker at 3600s) needs the origin off
        shift = origin if getattr(m, "timeline_start", None) is None else 0
        t = Fraction(str(seconds)).limit_denominator(100000) - shift
        if 0 <= t <= total:
            markers.append((t, m))
    if markers:
        mrow = [" "] * width
        for t, _m in markers:
            mrow[col(t)] = "^"
        lines.append(f"{'markers':<{gutter}}" + "".join(mrow).rstrip())
    lanes = sorted({s.lane for s in graph.segments})
    for lane in sorted((x for x in lanes if x > 0), reverse=True):
        lines.append(row(f"lane {lane}", [s for s in graph.segments if s.lane == lane], name_after=True))
    spine = [s for s in graph.segments if s.lane == 0]
    spine_row = row("spine", spine)
    if graph.transitions:
        cells = list(spine_row.ljust(gutter + width))
        for tr in graph.transitions:
            if tr.boundary + 1 < len(spine):
                cells[gutter + col(spine[tr.boundary + 1].tl_in)] = "X"
        spine_row = "".join(cells).rstrip()
    lines.append(spine_row)
    for lane in sorted((x for x in lanes if x < 0), reverse=True):
        lines.append(row(f"lane {lane}", [s for s in graph.segments if s.lane == lane], name_after=True))

    # the list, EDL style: every row in the same columns. A transition is
    # "D <length>" in the tr column of the clip it leads into (CMX's
    # convention) and its own row gives its span; a flash in the picture is a
    # "!" row at its time
    lines.append("")
    clips = [s for s in graph.segments if s.kind == "clip"]
    order = sorted(clips, key=lambda s: (frame_of(s.tl_in), s.lane != 0, s.lane))
    into = {}
    for tr in graph.transitions:
        if tr.boundary + 1 < len(spine):
            into[id(spine[tr.boundary + 1])] = tr
    flashes = []
    runs = picture_runs(graph)
    for i, (fa, fb, layers) in enumerate(runs):
        if fb - fa < 6 and layers and 0 < i < len(runs) - 1:
            flashes.append((fa, fb, layers, runs[i - 1][2], runs[i + 1][2]))
    def tr_name(tr) -> str:
        return tr.name or ("Cross Dissolve" if tr.kind == "fade" else tr.kind)

    storylines = list(getattr(timeline, "storylines", None) or [])
    name_w = max([len(s.label) for s in order] + [4] + [len(tr_name(t)) for t in graph.transitions]
                 + [len(t.name or "Transition") for s in storylines for t in s.transitions])
    file_w = max([len(source_name(s)) for s in order] + [6])

    def tcs(frames):
        return frames_timecode(frames, nominal)

    def src_tc(seconds):
        return frames_timecode(int(Fraction(seconds) * nominal + Fraction(1, 2)), nominal)

    def cols(num, track, rin, rout, length, tr, clip, source, sin, sout, note=""):
        return (f"{num:<4}{track:<8}{rin:<13}{rout:<13}{length:<8}{tr:<7}{clip:<{name_w + 2}}"
                f"{source:<{file_w + 2}}{sin:<13}{sout:<13}{note}")

    lines.append(cols("#", "track", "rec in", "rec out", "length", "tr", "clip", "source", "src in", "src out", "note"))
    events = []
    for n, s in enumerate(order, 1):
        fa, fb = frame_of(s.tl_in), frame_of(s.tl_out)
        tr = into.get(id(s))
        track = track_kind(s) if s.lane == 0 else f"L{s.lane} {track_kind(s)}"
        note = ", ".join(x for x in ("off (disabled: not seen, not heard)" if not getattr(s, "enabled", True)
                                     else "", "MEDIA MISSING" if s.missing else "") if x)
        events.append((fa, 3, cols(str(n), track, tcs(fa), tcs(fb), length_label(fb - fa),
                                   f"D {length_label(tr.end - tr.start)}" if tr else "", s.label, source_name(s),
                                   src_tc(s.src_in), src_tc(s.src_out), note)))
    for t, m in markers:
        kind = getattr(getattr(m, "marker_type", None), "value", "") or "marker"
        # a marker has no clip or source: its name and kind are its note
        events.append((frame_of(t), 0, cols("^", "marker", tcs(frame_of(t)), "", "", "", "", "", "", "",
                                            f"{m.name} ({kind})")))
    for tr in graph.transitions:
        if tr.boundary + 1 >= len(spine):
            continue
        out_seg, in_seg = spine[tr.boundary], spine[tr.boundary + 1]
        cut = frame_of(in_seg.tl_in)
        where = ("centred on " if tr.start + tr.end == 2 * cut
                 else f"starts {cut - tr.start}f before the cut ") + tcs(cut)
        kind = "AV" if out_seg.has_audio and in_seg.has_audio else "V"
        events.append((tr.start, 2, cols("D", kind, tcs(tr.start), tcs(tr.end), length_label(tr.end - tr.start), "",
                                         tr_name(tr), "", "", "", f"{out_seg.label} > {in_seg.label}, {where}")))
    # a transition inside a connected storyline: a D row on its lane, the
    # same columns as the spine's (the proxy draws it as a cut and says so)
    for s in storylines:
        for tr in s.transitions:
            cut = frame_of(fg._seconds(tr.start))
            length = frame_of(fg._seconds(tr.duration))
            lead = frame_of(fg._seconds(tr.lead)) if tr.lead is not None else length // 2
            outgoing = next((c.name for c in s.clips
                             if c.timeline_start.frames + c.duration.frames == tr.start.frames), None)
            incoming = next((c.name for c in s.clips if c.timeline_start.frames == tr.start.frames), None)
            sides = (f"{outgoing} > {incoming}" if outgoing and incoming
                     else f"into {incoming}" if incoming else f"out of {outgoing}" if outgoing else "")
            where = ("centred on " if 2 * lead == length else "starts on the cut " if lead == 0
                     else f"starts {lead}f before the cut ") + tcs(cut)
            events.append((cut - lead, 2, cols("D", f"L{tr.lane} V", tcs(cut - lead), tcs(cut - lead + length),
                                                length_label(length), "", tr.name or "Transition", "", "", "",
                                                f"{sides}, {where}" if sides else where)))
    for fa, fb, layers, before, after in flashes:
        top = layers[0]
        events.append((fa, 1, cols("!", track_kind(top) if top.lane == 0 else f"L{top.lane} {track_kind(top)}",
                                   tcs(fa), tcs(fb), length_label(fb - fa), "", top.label,
                                   source_name(top),
                                   src_tc(top.src_in + Fraction(fa, fg.FPS) - top.tl_in),
                                   src_tc(top.src_in + Fraction(fb, fg.FPS) - top.tl_in),
                                   f"flash between {picture_name(before)} and {picture_name(after)}")))
    for _a, _o, text in sorted(events, key=lambda e: (e[0], e[1])):
        lines.append(text.rstrip())
    return "\n".join(lines)
