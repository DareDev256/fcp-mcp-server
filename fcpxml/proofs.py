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
    return [s for s in graph.segments if s.kind == "clip" and s.has_video]


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
    cells = []
    for s in order:
        n = index[id(s)]
        label, outgoing = into.get(id(s), ("", None))
        cells.append(Cell(n, s.label, s.lane, frame_of(s.tl_in), frame_of(s.tl_out) - frame_of(s.tl_in),
                          s.source, s.src_in, s.src_out, s.file_time(s.duration / 2), s.missing,
                          label, index.get(outgoing, 0)))
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
    """The transition glyph's height: at least GLYPH_MIN at the delivered size."""
    return max(int(round(rs.GLYPH_MIN / lay.delivered)), 20 * lay.scale)


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
    then "rec <in>", "src <in>", "to  <out>"."""
    length = length_label(cell.frames)
    room = width - f.measure(length) - 2 * f.advance
    name_lines = rs.fit(cell.name, f, room)
    lines = [(n, INK, length if i == len(name_lines) - 1 else "") for i, n in enumerate(name_lines)]
    src_in = frames_timecode(int(cell.src_in * nominal + Fraction(1, 2)), nominal)
    src_out = frames_timecode(int(cell.src_out * nominal + Fraction(1, 2)), nominal)
    lines.append((f"rec {frames_timecode(cell.start, nominal)}", ACCENT, ""))
    lines.append((f"src {src_in}", MUTE, ""))
    lines.append((f"to  {src_out}", MUTE, ""))
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
    rows_fit = max(1, (lay.max_height - lay.margin * 2 - head_h + lay.gutter) // row_h)
    per_page = min(rs.CELLS_MAX, rows_fit * lay.columns)
    pages = [cells[i:i + per_page] for i in range(0, len(cells), per_page)] or [[]]
    head_h = head_height(f"page {len(pages)}/{len(pages)}" if len(pages) > 1 else "")
    size = glyph_size(lay)
    out = {"pages": [], "rasters": [], "cells": cells, "problems": [], "layout": lay, "per_page": per_page,
           "glyphs": [],
           "geometry": {"cell_w": cell_w, "cell_h": cell_h, "band": band, "row_h": row_h, "head_h": head_h,
                        "glyph": size}}
    for p, page in enumerate(pages):
        rows = max(1, -(-len(page) // lay.columns))
        height = lay.margin + head_h + rows * row_h - lay.gutter + lay.margin
        r = rs.Raster.filled(lay.width, height, GROUND)
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
                r.text(note, label, x + (cell_w - label.measure(note)) // 2, cy + (cell_h - label.height) // 2, DANGER)
            chip = f"#{cell.index}" + (f" L{cell.lane}" if cell.lane else "")
            pad = 4 * lay.scale
            r.fill(x, cy, label.measure(chip) + 2 * pad, label.height + pad, GROUND)
            r.text(chip, label, x + pad, cy + pad // 2, ACCENT if cell.lane else INK)
            ly = cy + cell_h + 4
            for text, colour, right in _cell_text(cell, label, cell_w, nominal):
                r.text(text, label, x, ly, colour)
                if right:
                    r.text(right, label, x + cell_w - label.measure(right), ly, MUTE)
                ly += label.height
            # the transition INTO this cell sits on its cut: in the gutter
            # when the cell it comes out of is just before it on the row,
            # else on the left edge of this picture; its length under it
            if cell.transition_in:
                gy = cy + cell_h // 2
                beside = cell.after in placed and placed[cell.after][1] == row and placed[cell.after][0] == col - 1
                text = cell.transition_in
                cw = max(size, label.measure(text)) + 2 * pad
                ch = size + label.height + 3 * pad
                cx = (x - lay.gutter // 2) if beside else (x + cw // 2)
                r.fill(cx - cw // 2, gy - size // 2 - pad, cw, ch, GROUND)
                _bowtie(r, cx - 1, gy, size)
                r.text(text, label, cx - label.measure(text) // 2, gy + size // 2 + pad, ACCENT)
                glyphs.append({"into": cell.index, "x": cx - 1, "y": gy, "size": size, "page": p + 1,
                               "label": text})
        out["glyphs"] += glyphs
        out["problems"] += [f"page {p + 1}: {x}" for x in
                            rs.legibility_problems(r, lay.delivered, cells=len(page)) + rs.collisions(r)]
        path = out_dir / f"sheet_{lay.name}_{p + 1:02d}.png"
        if r.save(str(path)):
            out["pages"].append(str(path))
        out["rasters"].append(r)
    return out


# ---- the filmstrip and waveform check -------------------------------------------------

CHECK_WIDTH = 1344          # 48 patches wide: 32 rows (896 px) keep it inside the budget
CHECK_FRAME_W, CHECK_FRAME_H = 240, 135
CHECK_FRAMES = 10           # default: two bands of five
SILENCE_DB = -40.0          # a window this quiet or quieter is silence
SILENCE_MIN = Fraction(3, 10)  # seconds: shorter quiet is a pause, not a span to label
FLOOR_DB = -60.0            # the waveform's bottom: quieter than this draws nothing
AUDIO_RATE = 8000
WAVE_H = 72


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


def silent_spans(samples: list, start: Fraction, rate: int = AUDIO_RATE) -> list:
    """(span start, span end) in source seconds where 50 ms windows stay at or
    below SILENCE_DB for SILENCE_MIN or longer."""
    window = rate // 20
    limit = 32768 * 10 ** (SILENCE_DB / 20)
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


def db_height(value_db: float, half: int) -> int:
    """Pixels above the baseline for a level: 0 at FLOOR_DB, ``half`` at 0 dBFS."""
    return int(round((max(FLOOR_DB, min(0.0, value_db)) - FLOOR_DB) / -FLOOR_DB * half))


def kept_ranges(graph, source: str) -> list:
    """[(file in, file out, record in)] for every clip of the timeline that
    plays ``source``, in file order: the parts of the file the edit keeps."""
    want = str(Path(source).resolve())
    out = []
    for s in video_segments(graph) + [s for s in graph.segments if s.kind == "clip" and not s.has_video]:
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


def visual_check(source: str, start: Fraction, end: Fraction, out_path: str, *, cut: Optional[Fraction] = None,
                 frames: int = CHECK_FRAMES, fps=None, kept: Optional[list] = None) -> dict:
    """Compose the check: two bands, each = its frames, its own ruler and
    its own waveform on that band's time axis. ``kept`` (from
    :func:`kept_ranges`) marks what the edit removed and finds the cut."""
    if end <= start:
        raise ValueError(f"end must be after start (got {start} -> {end})")
    if not 2 <= frames <= 10 or frames % 2:
        raise ValueError(f"frames must be an even number from 2 to 10 (two bands of up to five), got {frames}")
    if cut is not None and not start <= cut <= end:
        raise ValueError(f"cut must be inside the range (got {cut} outside {start} -> {end})")
    if shutil.which("ffmpeg") is None or not Path(source).is_file():
        return {"path": None}
    fps = fps or 24
    nominal = nominal_fps(fps)
    one = Fraction(1, nominal)
    f = rs.face(32)
    margin, fw, fh = 16, CHECK_FRAME_W, CHECK_FRAME_H
    x0, x1 = margin, CHECK_WIDTH - margin
    span = x1 - 1 - x0
    per = frames // 2
    removed = removed_spans(kept, start, end) if kept else []
    at_cut = check_cut(kept, start, end) if kept else None
    if at_cut is None and cut is not None:
        at_cut = {"out": cut, "in": cut, "rec": None, "rec_out": None}
    samples = _samples(source, start, end)
    spans = silent_spans(samples, start) if samples else []

    title_y = margin
    legend_y = title_y + f.height + 4
    band_top = legend_y + f.height + 8
    label_gap = 4
    band_h = fh + f.height + label_gap + 8 + f.height + 4 + WAVE_H + 12
    pair_h = (fh + f.height + 4 + 8) if at_cut else 0
    height = band_top + 2 * band_h + pair_h + margin - 12
    r = rs.Raster.filled(CHECK_WIDTH, height, GROUND)
    name = Path(source).name
    head = f"{name} · src {timecode(start, fps)} to {timecode(end, fps)} · {length_label(int((end - start) * nominal + Fraction(1, 2)))}"
    r.text(rs.fit(head, f, x1 - x0)[0], f, margin, title_y, INK)
    legend = f"RMS cyan · peak grey · dashed {SILENCE_DB:.0f} dB = silence"
    r.text(legend, f, margin, legend_y, MUTE)

    duration = end - start
    half_d = duration / 2
    bands, frames_out, ticks_all = [], [], []
    first_kept = None
    for b in range(2):
        b0, b1 = start + b * half_d, start + (b + 1) * half_d
        y_f = band_top + b * band_h
        y_label = y_f + fh + label_gap
        y_ruler = y_label + f.height + 8
        y_wave = y_ruler + 8 + f.height + 4

        def to_x(t, b0=b0, b1=b1):
            return x0 + int(round(float((t - b0) / (b1 - b0) * span)))

        # the frames: each centred on its own time on this band's axis
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
            highlight = (not gone and at_cut is not None and first_kept is None and t >= at_cut["in"])
            if gone:
                r.blend(fx, y_f, fw, fh, GROUND, 0.65)
                tag = "removed"
                r.fill(fx, y_f, f.measure(tag) + 8, f.height + 2, GROUND)
                r.text(tag, f, fx + 4, y_f + 1, MUTE)
            if highlight:
                first_kept = t
                r.fill(fx, y_f, 4, fh, ACCENT)
            tc = timecode(t, fps)
            r.text(tc, f, cx - f.measure(tc) // 2, y_label, ACCENT if highlight else MUTE)
            frames_out.append({"t": t, "x": fx, "y": y_f, "w": fw, "h": fh, "band": b, "removed": gone,
                               "highlight": highlight})
        # the ruler: ticks at each time's own pixel, labels centred under them
        r.fill(x0, y_ruler, x1 - x0, 1, LINE_STRONG)
        for a, c in removed:
            lo, hi = max(a, b0), min(c, b1)
            if hi > lo:
                r.fill(to_x(lo), y_ruler - 2, max(1, to_x(hi) - to_x(lo)), 5, DANGER)
        ticks = ruler_ticks(b0, b1, x0, x1 - 1)
        last_right = x0 - 24
        for t, x in ticks:
            r.fill(x, y_ruler + 1, 1, 8, ACCENT)
            text = timecode(t, fps)
            tx = min(max(x0, x - f.measure(text) // 2), x1 - f.measure(text))
            if tx < last_right + 24:
                continue
            r.text(text, f, tx, y_ruler + 10, MUTE)
            last_right = tx + f.measure(text)
        ticks_all.append(ticks)
        # the waveform: RMS in the accent over the peak in grey, mirrored on
        # a baseline, in dB; the silence threshold dashed
        half = WAVE_H // 2 - 2
        mid = y_wave + WAVE_H // 2
        for a, c in removed:
            lo, hi = max(a, b0), min(c, b1)
            if hi > lo:
                r.fill(to_x(lo), y_wave, max(1, to_x(hi) - to_x(lo)), WAVE_H, PANEL)
        for a, c in spans:
            lo, hi = max(a, b0), min(c, b1)
            if hi > lo:
                r.fill(to_x(lo), y_wave, max(1, to_x(hi) - to_x(lo)), WAVE_H, PANEL)
        if samples:
            n = len(samples)
            lo_i = int((b0 - start) / duration * n)
            hi_i = int((b1 - start) / duration * n)
            part = samples[lo_i:hi_i] or [0]
            cols = x1 - x0
            m = len(part)
            for col in range(cols):
                chunk = part[col * m // cols:(col + 1) * m // cols] or [0]
                peak = max(abs(v) for v in chunk)
                rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
                hp, hr = db_height(db(peak), half), db_height(db(rms), half)
                if hp:
                    r.fill(x0 + col, mid - hp, 1, 2 * hp + 1, MUTE)
                if hr:
                    r.fill(x0 + col, mid - hr, 1, 2 * hr + 1, ACCENT)
            ht = db_height(SILENCE_DB, half)
            for xx in range(x0, x1, 8):
                r.fill(xx, mid - ht, 4, 1, INK)
                r.fill(xx, mid + ht, 4, 1, INK)
        r.fill(x0, mid, x1 - x0, 1, LINE_STRONG)
        if not samples and b == 0:
            r.text("no audio in this source", f, x0 + 8, y_wave + 4, MUTE)
        # the silence and the removed span named inside the waveform band
        last_right = x0 - 12
        for a, c in spans:
            lo, hi = max(a, b0), min(c, b1)
            if hi <= lo or a < b0:  # named in the band it starts in
                continue
            for text in (f"silence {_seconds_label(c - a)}", _seconds_label(c - a)):
                tx = min(max(x0, to_x(lo) + 4), x1 - f.measure(text))
                if tx >= last_right + 12:
                    r.fill(tx - 2, y_wave + 2, f.measure(text) + 4, f.height, PANEL)
                    r.text(text, f, tx, y_wave + 2, INK)
                    last_right = tx + f.measure(text)
                    break
        if at_cut is not None and b0 <= at_cut["out"] <= b1:
            cx = to_x(at_cut["out"])
            for k in range(8):
                r.fill(cx - k, y_ruler - 10 + (7 - k), 2 * k + 1, 1, ACCENT)
            r.fill(cx - 1, y_wave, 3, WAVE_H, ACCENT)
        bands.append({"start": b0, "end": b1, "y_frames": y_f, "y_label": y_label, "y_ruler": y_ruler,
                      "y_wave": y_wave, "ticks": ticks, "wave_mid": mid})
    # the cut: the last kept frame and the first kept frame, side by side,
    # with the record time of the cut and what was taken out between them
    pair = None
    if at_cut is not None:
        y_p = band_top + 2 * band_h
        before_t = at_cut["out"] - one
        after_t = at_cut["in"]
        for j, (t, word) in enumerate(((before_t, "last"), (after_t, "first"))):
            px = margin + j * (fw + 8)
            pic = exact_frame(source, t, fps, fw, fh)
            if pic is not None:
                r.paste(pic, px, y_p)
            else:
                r.fill(px, y_p, fw, fh, PANEL)
            if j == 1:
                r.fill(px, y_p, 4, fh, ACCENT)
            tc = timecode(t, fps)
            r.text(tc, f, px + (fw - f.measure(tc)) // 2, y_p + fh + 4, ACCENT if j == 1 else MUTE)
        tx = margin + 2 * (fw + 8) + 16
        lines = []
        if at_cut["rec"] is not None:
            lines.append((f"cut at rec {timecode(at_cut['rec'], fps)}", ACCENT))
        else:
            lines.append((f"cut at src {timecode(at_cut['out'], fps)}", ACCENT))
        lines.append((f"last kept  src {timecode(before_t, fps)}", INK))
        lines.append((f"first kept src {timecode(after_t, fps)}", INK))
        gap = at_cut["in"] - at_cut["out"]
        if gap > 0:
            lines.append((f"removed {length_label(int(gap * nominal + Fraction(1, 2)))} "
                          f"({timecode(at_cut['out'], fps)} to {timecode(at_cut['in'] - one, fps)})", MUTE))
        for i, (text, colour) in enumerate(lines):
            r.text(rs.fit(text, f, x1 - tx)[0], f, tx, y_p + i * f.height, colour)
        pair = {"y": y_p, "before": before_t, "after": after_t, "lines": [t for t, _ in lines]}
    problems = rs.legibility_problems(r) + rs.collisions(r)
    path = r.save(out_path)
    return {"path": path, "raster": r, "bands": bands, "frames": frames_out, "spans": spans,
            "removed": removed, "cut": at_cut, "pair": pair, "problems": problems,
            "audio": samples is not None, "first_kept": first_kept,
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
        clip_word = f"in clip {clips[0]}" if len(clips) == 1 else f"in {len(clips)} clips"
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
                r.text(tc, f, x + 6, y + fh - f.height - 2, ACCENT if j == 1 else WHITE)
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

BURN_MARGIN = 16
BURN_PAD = 8


def burn_face(height: int) -> rs.Face:
    """The burn-in face for a frame this tall: 16x32 at 720 (20 px letters),
    doubled from 1080 up so the letters stay legible when the frame is
    downscaled for the model."""
    return rs.face(32, max(1, round(height / 720)))


def _chip(lines: list, f: rs.Face, width: Optional[int] = None) -> rs.Raster:
    """One burn-in chip: opaque true black, each line a list of (text,
    colour) pieces drawn left to right. Transparent outside the box when a
    fixed ``width`` is wider than the text."""
    used = max(sum(f.measure(t) for t, _ in line) for line in lines) + 2 * BURN_PAD
    w = max(width or 0, used)
    h = len(lines) * f.height + BURN_PAD
    r = rs.Raster.filled(w, h, (0, 0, 0), alpha=0)
    r.fill(0, 0, used, h, GROUND, 255)
    for i, line in enumerate(lines):
        x = BURN_PAD
        for text, colour in line:
            if text:
                r.text(text, f, x, BURN_PAD // 2 + i * f.height, colour, alpha=255)
            x += f.measure(text)
    return r


def burn_chip(picture: rs.Raster, lines: list) -> rs.Raster:
    """Draw a chip of ``lines`` (lists of (text, colour)) bottom-left on
    ``picture``, in place; returns the chip. A frame under 540 px tall takes
    the 12x24 face (15 px capitals), so the chip stays a label, not a band."""
    f = burn_face(picture.height) if picture.height >= 540 else rs.face(24)
    chip = _chip(lines, f)
    x, y = BURN_MARGIN, picture.height - BURN_MARGIN - chip.height
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
    return [[("SRC ", MUTE), (Path(source).name + " ", WHITE), (frames_timecode(frame, nominal), ACCENT),
             (f" · f{frame}", MUTE)]]


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
    """The two lines burned on output frame ``k``: ("REC <tc>  <what is on
    screen, top first> over <the clip it hides>", "SRC <file> <source tc>"),
    the top layer's source: "REC 00:00:18:00  Bench (L1) over Squat"."""
    nominal = nominal_fps(fps)
    layers = layers_at(graph, k, height)
    stack = stack_at(graph, k)
    hidden = stack[len(layers)] if len(stack) > len(layers) else None
    rec = f"REC {frames_timecode(k, nominal)}  {picture_name(layers)}" + (f" over {lane_name(hidden)}" if hidden else "")
    if not layers:
        return rec, "SRC none (black)"
    top = layers[0]
    into = Fraction(k, fg.FPS) - top.tl_in
    return rec, f"SRC {source_name(top)} {source_tc(top, max(Fraction(0), into), fps)}"


def burn_frames(graph, height: int, fps):
    """(frame index, RGBA strip) for every output frame: the chip naming
    what is on screen at that frame and the top layer's source timecode."""
    f = burn_face(height)
    texts = [burn_text(graph, k, fps, height) for k in range(graph.total_frames)]
    width = max([max(f.measure(a), f.measure(b)) for a, b in texts] or [0]) + 2 * BURN_PAD
    for k, (rec, src) in enumerate(texts):
        head, _, what = rec.partition("  ")
        lines = [[(head + "  ", WHITE), (what, ACCENT)], [(src, WHITE)]]
        yield k, _chip(lines, f, width)


def burn_in(graph, clean_path: str, out_path: str, height: int, fps) -> Optional[str]:
    """Composite the burn-in over the CLEAN, already-verified render: one
    opaque chip bottom left on every frame, "REC <tc>  <what is on screen,
    top layer first>" over "SRC <file> <source tc>". Returns an error
    string, or None on success."""
    if shutil.which("ffmpeg") is None:
        return "ffmpeg is not on PATH"
    canvas_w, canvas_h = fg.canvas_size(graph, height)
    frames = list(burn_frames(graph, canvas_h, fps))
    if not frames:
        return "the timeline is empty"
    strip_w, strip_h = frames[0][1].width, frames[0][1].height
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(clean_path), "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{strip_w}x{strip_h}",
            "-framerate", str(fg.FPS), "-i", "pipe:0",
            "-filter_complex", f"[0:v][1:v]overlay={BURN_MARGIN}:{canvas_h - BURN_MARGIN - strip_h}:format=auto[v]",
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
    """Every frame's burn-in region (bottom left, the width of the picture
    less its margins, ``lines`` text lines tall) read back from a burned
    proxy in ONE decode: the instrument for the burn-in gate, which checks
    every frame, not a sample."""
    f = burn_face(height)
    h = lines * f.height + BURN_PAD
    w = width - 2 * BURN_MARGIN
    y = height - BURN_MARGIN - h
    if shutil.which("ffmpeg") is None:
        return []
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                              "-vf", f"crop={w}:{h}:{BURN_MARGIN}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24",
                              "pipe:1"], capture_output=True, timeout=600).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    size = w * h * 3
    return [rs.Raster(w, h, 3, bytearray(out[i:i + size])) for i in range(0, len(out) - size + 1, size)]


def read_burn(strip: rs.Raster, height: int, line: int, chars: int, alphabet: str = rs.READABLE) -> str:
    """Read ``chars`` characters of line ``line`` (0 or 1) off a burn-in strip."""
    f = burn_face(height)
    return rs.read_text(strip, f, BURN_PAD, BURN_PAD // 2 + line * f.height, chars, alphabet=alphabet)


# ---- the text timeline ---------------------------------------------------------------

def text_timeline(timeline, graph, width: int = 64) -> str:
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
            f"{rate} fps · {counts_label(timeline)}")
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
        return ("[" + inner.ljust(span - 2, "=") + "]") if span >= 2 else "|"

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
    for i, (t, c) in enumerate(ticks):
        ruler[c] = "|"
        text = _seconds_label(t)
        last = i == len(ticks) - 1
        a = c - len(text) + 1 if last and c + len(text) > width else c
        if all(ch == " " for ch in labels[max(0, a - 1):a + len(text) + 1]):
            labels[a:a + len(text)] = list(text)
    lines.append(f"{'time':<{gutter}}" + "".join(labels).rstrip())
    lines.append(f"{'':<{gutter}}" + "".join(ruler).rstrip())

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
        t = Fraction(str(seconds)).limit_denominator(100000) - origin
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

    # the list, EDL style
    lines.append("")
    clips = [s for s in graph.segments if s.kind == "clip"]
    order = sorted(clips, key=lambda s: (frame_of(s.tl_in), s.lane != 0, s.lane))
    name_w = max([len(s.label) for s in order] + [4])
    file_w = max([len(source_name(s)) for s in order] + [6])
    lines.append(f"{'#':<4}{'track':<7}{'rec in':<13}{'rec out':<13}{'length':<8}{'clip':<{name_w + 2}}"
                 f"{'source':<{file_w + 2}}{'src in':<13}src out")
    events = []
    for n, s in enumerate(order, 1):
        a, b = frame_of(s.tl_in), frame_of(s.tl_out)
        flag = "  [MEDIA MISSING]" if s.missing else ""
        src_in = frames_timecode(int(s.src_in * nominal + Fraction(1, 2)), nominal)
        src_out = frames_timecode(int(s.src_out * nominal + Fraction(1, 2)), nominal)
        track = "V" if s.lane == 0 else f"L{s.lane}"
        events.append((a, 2, f"{n:<4}{track:<7}{frames_timecode(a, nominal):<13}{frames_timecode(b, nominal):<13}"
                             f"{length_label(b - a):<8}{s.label:<{name_w + 2}}{source_name(s):<{file_w + 2}}"
                             f"{src_in:<13}{src_out}{flag}"))
    for t, m in markers:
        kind = getattr(getattr(m, "marker_type", None), "value", "") or "marker"
        events.append((frame_of(t), 0, f"{'':<4}{'marker':<7}{frames_timecode(frame_of(t), nominal):<13}"
                                       f"{'':<13}{'':<8}{m.name} ({kind})"))
    for tr in graph.transitions:
        if tr.boundary + 1 >= len(spine):
            continue
        out_seg, in_seg = spine[tr.boundary], spine[tr.boundary + 1]
        cut = frame_of(in_seg.tl_in)
        where = ("centred on the cut" if tr.start + tr.end == 2 * cut
                 else f"starting {cut - tr.start}f before the cut")
        events.append((tr.start, 1, f"{'X':<4}{'V':<7}{frames_timecode(tr.start, nominal):<13}"
                                    f"{frames_timecode(tr.end, nominal):<13}{length_label(tr.end - tr.start):<8}"
                                    f"{tr.name or tr.kind}, {out_seg.label} into {in_seg.label}, {where} "
                                    f"{frames_timecode(cut, nominal)}"))
    for _a, _o, text in sorted(events, key=lambda e: (e[0], e[1])):
        lines.append(text.rstrip())
    return "\n".join(lines)
