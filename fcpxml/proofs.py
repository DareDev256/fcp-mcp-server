"""The proof images: pictures of an edit that carry its names, timecodes and cuts.

These are the only pictures a Claude Code user gets of a timeline, and they
are what the model itself looks at (it takes no video), so every one is laid
out to be READ: by a person at 1:1 or on a phone, and by the model at the
size it receives. Each one is a :class:`raster.Raster` composed here, written
as a PNG, and checked against the legibility rule
(:func:`raster.legibility_problems`) before it is returned.

- :func:`contact_sheet`: one composed sheet per page, 16:9 cells, each with
  its cut index (top left), clip name and timeline timecode (bottom left),
  a transition glyph between cells where there is one, and a header (project,
  duration, frame rate, a short hash of the file, the date). ``layout="phone"``
  draws two columns at twice the size for a 390 px column.
- :func:`visual_check`: two rows of frames from the SOURCE media, a labelled
  time ruler, the cut marked, the waveform in the accent on black with a
  baseline, silent spans shaded and labelled.
- :func:`scene_strip`: the frame before and the frame after each detected cut.
- :func:`burn_in`: the proxy's burn-in, drawn as RGBA overlays and composited
  by ffmpeg onto the clean proxy (render.py calls it AFTER verify_lanes() has
  read the clean file); :func:`burned_strips` reads every frame of it back.
- :func:`text_timeline`: the inline text picture of the cut.

Everything here is drawn with the bundled bitmap face; nothing calls
``drawtext``.
"""

from __future__ import annotations

import datetime
import hashlib
import math
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

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
    5.25 s as 5.2): "5.3s", "1m02.5s"."""
    tenths = int(Fraction(seconds) * 10 + Fraction(1, 2))
    whole, tenth = divmod(tenths, 10)
    if whole < 60:
        return f"{whole}.{tenth}s"
    return f"{whole // 60}m{whole % 60:02d}.{tenth}s"


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
    name_lines: int = 2


DESKTOP = SheetLayout("desktop", 1344, 896, 4, 16, 24, 1, 1.0)
PHONE = SheetLayout("phone", 780, 1568, 2, 16, 32, 2, 0.5)
LAYOUTS = {"desktop": DESKTOP, "phone": PHONE}


@dataclass(frozen=True)
class Cell:
    """One spine clip on the sheet."""

    index: int            # cut index, 1-based, in timeline order
    name: str
    start: Fraction       # timeline time of its first frame (the cut)
    duration: Fraction
    source: str
    at: Fraction          # file time of the frame shown (the clip's middle)
    missing: bool
    transition_after: str = ""  # the transition joining it to the next cell, if any


def sheet_cells(graph) -> list:
    """The spine clips of a compiled timeline, in order, with the transition
    that follows each (if any)."""
    spine = [s for s in graph.segments if s.lane == 0]
    after = {t.boundary: t.name or t.kind for t in graph.transitions}
    cells, n = [], 0
    for i, s in enumerate(spine):
        if s.kind != "clip":
            continue
        n += 1
        cells.append(Cell(n, s.label, s.tl_in, s.duration, s.source, s.file_time(s.duration / 2),
                          s.missing, after.get(i, "")))
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
    """The transition glyph's height: at least GLYPH_MIN at the delivered
    size, and no wider than the gutter it sits in (the bowtie is about as
    wide as it is tall)."""
    return max(int(round(rs.GLYPH_MIN / lay.delivered)), min(20 * lay.scale, lay.gutter - 2))


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


def _cell_lines(cell: Cell, f: rs.Face, width: int, fps) -> tuple:
    """The cell's label: the name (wrapped, never cut), the timecode of its
    cut, and its length, with where the length goes: after the timecode,
    else at the right of the name's last line, else (no room) nowhere.
    Returns (name lines, timecode, length text, "tc" | "name" | "")."""
    name_lines = rs.fit(cell.name, f, width)
    tc = timecode(cell.start, fps)
    length = _seconds_label(cell.duration)
    gap = 2 * f.advance
    if f.measure(tc) + gap + f.measure(length) <= width:
        return name_lines, tc, length, "tc"
    if f.measure(name_lines[-1]) + gap + f.measure(length) <= width:
        return name_lines, tc, length, "name"
    return name_lines, tc, length, ""


def contact_sheet(timeline, graph, out_dir: Path, *, layout: str = "desktop", filepath: str = "",
                  today: Optional[str] = None) -> dict:
    """Compose the sheet's pages. Returns {"pages": [paths], "rasters": [...],
    "cells": [Cell], "problems": [...], "layout": SheetLayout}."""
    lay = LAYOUTS[layout]
    label = rs.face(24, lay.scale)
    title = rs.face(32, 1) if lay.scale == 1 else rs.face(24, lay.scale)
    fps = getattr(timeline, "frame_rate", 24) or 24
    cells = sheet_cells(graph)
    inner = lay.width - 2 * lay.margin
    cell_w = (inner - (lay.columns - 1) * lay.gutter) // lay.columns
    cell_h = cell_w * 9 // 16
    name_lines = max([len(_cell_lines(c, label, cell_w, fps)[0]) for c in cells] or [1])
    band = (name_lines + 1) * label.height + 8
    row_h = cell_h + band + lay.gutter

    project = str(getattr(timeline, "name", "") or "timeline")
    meta = [_seconds_label(graph.total), f"{float(rational_fps(fps)):.3f}".rstrip("0").rstrip(".") + " fps",
            short_hash(filepath), today or datetime.date.today().isoformat()]

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
        for k, cell in enumerate(page):
            col, row = k % lay.columns, k // lay.columns
            x = lay.margin + col * (cell_w + lay.gutter)
            cy = top + row * row_h
            picture = None if cell.missing else rs.frame(cell.source, cell.at, cell_w, cell_h)
            if picture is not None:
                r.paste(picture, x, cy)
            else:
                r.fill(x, cy, cell_w, cell_h, PANEL)
                note = "media missing"
                r.text(note, label, x + (cell_w - label.measure(note)) // 2, cy + (cell_h - label.height) // 2, DANGER)
            chip = f"#{cell.index}"
            pad = 4 * lay.scale
            r.fill(x, cy, label.measure(chip) + 2 * pad, label.height + pad, GROUND)
            r.text(chip, label, x + pad, cy + pad // 2, INK)
            lines, tc, length, where = _cell_lines(cell, label, cell_w, fps)
            ly = cy + cell_h + 4
            for line in lines:
                r.text(line, label, x, ly, INK)
                ly += label.height
            r.text(tc, label, x, ly, ACCENT)
            if where == "tc":
                r.text(length, label, x + label.measure(tc) + 2 * label.advance, ly, MUTE)
            elif where == "name":
                r.text(length, label, x + cell_w - label.measure(length), ly - label.height, MUTE)
            # the transition INTO this cell sits on its cut: in the gutter
            # when the cell before it is beside it, else on a chip at this
            # picture's left edge (a new row or a new page)
            before = cells[cell.index - 2] if cell.index > 1 else None
            if before is not None and before.transition_after:
                gy = cy + cell_h // 2
                if col > 0:
                    gx = x - lay.gutter // 2 - 1
                else:
                    w = size + 4 * lay.scale
                    r.fill(x, gy - size // 2 - 2 * lay.scale, w, size + 4 * lay.scale, GROUND)
                    gx = x + w // 2 - 1
                _bowtie(r, gx, gy, size)
                glyphs.append({"into": cell.index, "x": gx, "y": gy, "size": size, "page": p + 1})
        out["glyphs"] += glyphs
        out["problems"] += [f"page {p + 1}: {x}" for x in
                            rs.legibility_problems(r, lay.delivered, cells=len(page)) + rs.collisions(r)]
        path = out_dir / f"sheet_{lay.name}_{p + 1:02d}.png"
        if r.save(str(path)):
            out["pages"].append(str(path))
        out["rasters"].append(r)
    return out


# ---- the filmstrip and waveform check -------------------------------------------------

CHECK_WIDTH = 1512          # 6 frames of 240 px, gutters, margins: 54 patches wide
SILENCE_DB = -40.0          # a window this quiet or quieter is silence
SILENCE_MIN = Fraction(3, 10)  # seconds: shorter quiet is a pause, not a span to label
AUDIO_RATE = 8000


def nice_step(duration: Fraction, width_px: int, min_px: int = 170) -> Fraction:
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
    import array
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


def visual_check(source: str, start: Fraction, end: Fraction, out_path: str, *, cut: Optional[Fraction] = None,
                 frames: int = 12, fps=None) -> dict:
    """Compose the check. Returns {"path", "raster", "ticks", "spans", "problems", "audio"}."""
    if end <= start:
        raise ValueError(f"end must be after start (got {start} -> {end})")
    if not 2 <= frames <= 12 or frames % 2:
        raise ValueError(f"frames must be an even number from 2 to 12 (two rows), got {frames}")
    if cut is not None and not start <= cut <= end:
        raise ValueError(f"cut must be inside the range (got {cut} outside {start} -> {end})")
    if shutil.which("ffmpeg") is None or not Path(source).is_file():
        return {"path": None}
    fps = fps or 24
    label, title = rs.face(24), rs.face(32)
    margin, gap = 16, 8
    per_row = frames // 2
    fw = (CHECK_WIDTH - 2 * margin - (per_row - 1) * gap) // per_row
    fh = fw * 9 // 16
    x0, x1 = margin, CHECK_WIDTH - margin
    head_h = title.height + 8
    frame_row_h = fh + label.height + 6
    ruler_y = margin + head_h + 2 * frame_row_h + 6
    ruler_h = label.height + 14
    wave_y = ruler_y + ruler_h + 4
    wave_h = 150
    span_label_h = label.height + 4
    height = wave_y + wave_h + span_label_h + margin
    r = rs.Raster.filled(CHECK_WIDTH, height, GROUND)
    name = Path(source).name
    head = f"{name} · {timecode(start, fps)} to {timecode(end, fps)} · {_seconds_label(end - start)}"
    r.text(rs.fit(head, title, x1 - x0)[0], title, margin, margin, INK)
    duration = end - start
    times = [start + duration * (2 * i + 1) / (2 * frames) for i in range(frames)]
    for i, t in enumerate(times):
        row, col = divmod(i, per_row)
        x = margin + col * (fw + gap)
        y = margin + head_h + row * frame_row_h
        pic = rs.frame(source, t, fw, fh)
        if pic is not None:
            r.paste(pic, x, y)
        else:
            r.fill(x, y, fw, fh, PANEL)
        first_after = cut is not None and t >= cut and (i == 0 or times[i - 1] < cut)
        if first_after:  # the first frame after the cut: an accent edge and an accent time
            r.fill(x, y, 4, fh, ACCENT)
        r.text(timecode(t, fps), label, x, y + fh + 3, ACCENT if first_after else MUTE)
    # the ruler: a tick hangs from the line at each time's own pixel; its time
    # is centred under it (kept inside the picture at the ends), and a time
    # that would sit under the cut's mark gives way to the cut's own label
    r.fill(x0, ruler_y, x1 - x0, 1, LINE_STRONG)
    ticks = ruler_ticks(start, end, x0, x1 - 1)
    cut_x = None if cut is None else x0 + int(round(float((cut - start) / duration * (x1 - 1 - x0))))
    last_right = x0 - 24
    for t, x in ticks:
        r.fill(x, ruler_y + 1, 1, 9, ACCENT)
        text = timecode(t, fps)
        tx = min(max(x0, x - label.measure(text) // 2), x1 - label.measure(text))
        if cut_x is not None and tx - 6 <= cut_x <= tx + label.measure(text) + 6:
            continue
        if tx < last_right + 24:  # an end label pulled inside the picture would touch its neighbour
            continue
        r.text(text, label, tx, ruler_y + 12, MUTE)
        last_right = tx + label.measure(text)
    # the waveform
    samples = _samples(source, start, end)
    mid = wave_y + wave_h // 2
    spans = []
    if samples:
        spans = silent_spans(samples, start)
        for a, b in spans:
            sx0 = x0 + int(round(float((a - start) / duration * (x1 - 1 - x0))))
            sx1 = x0 + int(round(float((b - start) / duration * (x1 - 1 - x0))))
            r.fill(sx0, wave_y, max(1, sx1 - sx0), wave_h, PANEL)
        cols = x1 - x0
        n = len(samples)
        for c in range(cols):
            chunk = samples[c * n // cols:(c + 1) * n // cols] or [0]
            lo, hi = min(chunk), max(chunk)
            top = mid - int(hi / 32768 * (wave_h // 2 - 2))
            bot = mid - int(lo / 32768 * (wave_h // 2 - 2))
            r.fill(x0 + c, min(top, bot), 1, max(1, abs(bot - top) + 1), ACCENT)
        r.fill(x0, mid, x1 - x0, 1, LINE_STRONG)
        # every span gets a label under it: "silence 1.2 s", or just "1.2 s"
        # where the long one would run into its neighbour's (never truncated,
        # never overlapping; the shaded span still shows where it is)
        last_right = x0 - 12
        for a, b in spans:
            sx0 = x0 + int(round(float((a - start) / duration * (x1 - 1 - x0))))
            sx1 = x0 + int(round(float((b - start) / duration * (x1 - 1 - x0))))
            for text in (f"silence {_seconds_label(b - a)}", _seconds_label(b - a)):
                tx = min(max(x0, (sx0 + sx1) // 2 - label.measure(text) // 2), x1 - label.measure(text))
                if tx >= last_right + 12:
                    r.text(text, label, tx, wave_y + wave_h + 2, INK)
                    last_right = tx + label.measure(text)
                    break
    else:
        r.fill(x0, mid, x1 - x0, 1, LINE_STRONG)
        note = "no audio in this source"
        r.text(note, label, x0 + 8, wave_y + 8, MUTE)
    if cut_x is not None:
        # a mark on the ruler (above its line, clear of the ticks), the line
        # through the waveform, and the cut's time beside it
        for k in range(8):
            r.fill(cut_x - k, ruler_y - 9 + (7 - k), 2 * k + 1, 1, ACCENT)
        r.fill(cut_x - 1, wave_y, 3, wave_h, ACCENT)
        text = f"cut {timecode(cut, fps)}"
        tx = min(max(x0, cut_x + 8), x1 - label.measure(text))
        r.fill(tx - 4, wave_y + 4, label.measure(text) + 8, label.height + 4, GROUND)
        r.text(text, label, tx, wave_y + 6, ACCENT)
    # the cut's label may sit over the waveform: it is drawn on its own ground
    problems = rs.legibility_problems(r) + rs.collisions(r)
    path = r.save(out_path)
    return {"path": path, "raster": r, "ticks": ticks, "spans": spans, "problems": problems,
            "audio": samples is not None, "cut_x": cut_x, "geometry": {"x0": x0, "x1": x1, "ruler_y": ruler_y,
                                                                         "wave_y": wave_y, "wave_h": wave_h}}


# ---- the scenes strip -----------------------------------------------------------------

def scene_strip(rows: list, out_dir: Path, fps=24) -> dict:
    """rows: [(clip name, source path, source cut seconds, timeline seconds)].
    One row per cut: the frame before it, the frame after it, and the label.
    Pages of up to 6 cuts (12 frames). Returns {"pages", "rasters", "problems"}."""
    label, title = rs.face(24), rs.face(32)
    margin, gap = 16, 12
    fw, fh = 400, 225
    text_w = 1344 - 2 * margin - 2 * fw - 2 * gap
    width = 1344
    row_h = fh + gap
    out = {"pages": [], "rasters": [], "problems": []}
    per_page = 6
    per_page = max(1, min(per_page, (rs.VISUAL_TOKENS_MAX // -(-width // rs.PATCH) * rs.PATCH - 2 * margin - title.height - 8) // row_h))
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)]
    for p, page in enumerate(pages):
        height = margin + title.height + 8 + len(page) * row_h - gap + margin
        r = rs.Raster.filled(width, height, GROUND)
        head = f"Cuts {p * per_page + 1} to {p * per_page + len(page)} of {len(rows)}: before | after"
        r.text(head, title, margin, margin, INK)
        y = margin + title.height + 8
        for k, (name, source, at, tl) in enumerate(page):
            n = p * per_page + k + 1
            step = Fraction(1, int(nominal_fps(fps)))
            before = rs.frame(source, max(Fraction(0), Fraction(at) - step), fw, fh)
            after = rs.frame(source, Fraction(at) + step / 2, fw, fh)
            for j, pic in enumerate((before, after)):
                x = margin + j * (fw + gap)
                if pic is not None:
                    r.paste(pic, x, y)
                else:
                    r.fill(x, y, fw, fh, PANEL)
            r.fill(margin + fw + gap // 2 - 1, y, 2, fh, ACCENT)
            tx = margin + 2 * (fw + gap)
            lines = [f"cut #{n}", timecode(Fraction(tl), fps), f"source {timecode(Fraction(at), fps)}"]
            lines += rs.fit(name, label, text_w)
            for i, line in enumerate(lines[: fh // label.height]):
                r.text(line, label, tx, y + i * label.height, ACCENT if i == 1 else (MUTE if i == 2 else INK))
            y += row_h
        out["problems"] += rs.legibility_problems(r, cells=2 * len(page)) + rs.collisions(r)
        path = out_dir / f"scenes_{p + 1:02d}.png"
        if r.save(str(path)):
            out["pages"].append(str(path))
        out["rasters"].append(r)
    return out


# ---- burn-in -----------------------------------------------------------------------------

BURN_OPACITY = 0.7
BURN_ALPHA = int(round(255 * BURN_OPACITY))
BURN_MARGIN = 16
BURN_PAD = 8
MAX_BURN_BADGES = 24  # lane clips labelled per proxy; past this the report says so


def burn_face(height: int) -> rs.Face:
    """The burn-in face for a frame this tall: 16x32 at 720 (20 px letters),
    doubled from 1080 up so the letters stay legible when the frame is
    downscaled for the model."""
    return rs.face(32, max(1, round(height / 720)))


def burn_timecode(picture: rs.Raster, tc: str) -> rs.TextRun:
    """Draw ``tc`` bottom-left on a black box at 70% opacity, in place."""
    f = burn_face(picture.height)
    w, h = f.measure(tc) + 2 * BURN_PAD, f.height + BURN_PAD
    x, y = BURN_MARGIN, picture.height - BURN_MARGIN - h
    picture.blend(x, y, w, h, GROUND, BURN_OPACITY)
    return picture.text(tc, f, x + BURN_PAD, y + BURN_PAD // 2, WHITE)


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


def _bar(text: str, width: int, f: rs.Face) -> rs.Raster:
    """A label bar: black at 70% opacity, the text in white at full strength.
    As wide as ``width`` (a clip's width) or the text, whichever is wider:
    a name is never cut to fit."""
    w = max(width, f.measure(text) + 2 * BURN_PAD)
    r = rs.Raster.filled(w, f.height + BURN_PAD, GROUND, alpha=BURN_ALPHA)
    r.text(text, f, BURN_PAD, BURN_PAD // 2, WHITE, alpha=255)
    return r


def burn_frames(graph, height: int, fps):
    """(frame index, RGBA strip) for every output frame: the timeline
    timecode and the spine clip under the playhead, bottom left."""
    from .filtergraph import FPS

    f = burn_face(height)
    spine = [s for s in graph.segments if s.lane == 0]
    longest = max([len(s.label) for s in spine if s.kind == "clip"] or [0])
    width = f.measure("00:00:00:00") + 2 * BURN_PAD + (f.measure(" " * (longest + 1)) if longest else 0) + BURN_PAD
    for k in range(graph.total_frames):
        t = Fraction(k, FPS)
        current = next((s for s in spine if s.tl_in <= t < s.tl_out), None)
        name = current.label if current is not None and current.kind == "clip" else ""
        strip = rs.Raster.filled(width, f.height + BURN_PAD, (0, 0, 0), alpha=0)
        tc = timecode(t, fps)
        used = f.measure(tc) + 2 * BURN_PAD + (f.measure(" " + name) if name else 0)
        strip.fill(0, 0, used, f.height + BURN_PAD, GROUND, BURN_ALPHA)
        strip.text(tc, f, BURN_PAD, BURN_PAD // 2, WHITE, alpha=255)
        if name:
            strip.text(" " + name, f, BURN_PAD + f.measure(tc), BURN_PAD // 2, ACCENT, alpha=255)
        yield k, strip


def burn_in(graph, clean_path: str, out_path: str, height: int, fps) -> Optional[str]:
    """Composite the burn-in over the CLEAN, already-verified render: the
    timecode and clip name bottom left (every frame), and a bar naming each
    lane clip across the top of its picture while it is on screen. Returns an
    error string, or None on success."""
    from .filtergraph import FPS, canvas_size, placement

    if shutil.which("ffmpeg") is None:
        return "ffmpeg is not on PATH"
    canvas_w, canvas_h = canvas_size(graph, height)
    small = rs.face(24, max(1, round(canvas_h / 720)))
    frames = list(burn_frames(graph, canvas_h, fps))
    if not frames:
        return "the timeline is empty"
    strip_w, strip_h = frames[0][1].width, frames[0][1].height
    work = Path(out_path).with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    inputs = ["-i", str(clean_path), "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{strip_w}x{strip_h}",
              "-framerate", str(FPS), "-i", "pipe:0"]
    chain = [f"[0:v][1:v]overlay={BURN_MARGIN}:{canvas_h - BURN_MARGIN - strip_h}:format=auto[v1]"]
    label = "v1"
    badges = 0
    for n, piece in enumerate(graph.lanes[:MAX_BURN_BADGES]):
        segment = graph.segments[piece.segment]
        w, h, x, y = placement(graph, segment, canvas_h)
        x0, y0 = max(0, x), max(0, y)
        w0 = min(canvas_w, x + w) - x0
        if w0 < 8:
            continue
        bar = _bar(f"L{segment.lane}  {segment.label}", w0, small)
        path = work / f"badge_{n:03d}.png"
        if not bar.save(str(path)):
            return "a lane badge could not be written"
        inputs += ["-loop", "1", "-i", str(path)]
        index = 2 + badges
        last = piece.start + piece.frames - 1
        nxt = f"v{index}"
        chain.append(f"[{label}][{index}:v]overlay={min(x0, canvas_w - bar.width)}:{y0}:format=auto"
                     f":enable='between(n,{piece.start},{last})'[{nxt}]")
        label, badges = nxt, badges + 1
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *inputs,
            "-filter_complex", ";".join(chain), "-map", f"[{label}]", "-map", "0:a?",
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
    finally:
        for p in work.glob("badge_*.png"):
            p.unlink()
        work.rmdir()
    if code != 0 or not Path(out_path).is_file():
        return "the burn-in pass failed: " + " / ".join(err.strip().splitlines()[-2:])
    return None


def burned_strips(path: str, width: int, height: int) -> list:
    """Every frame's burn-in strip (bottom left, the width of the picture
    less its margins) read back from a burned proxy in ONE decode: the
    instrument for the burn-in gate, which checks every frame, not a sample."""
    f = burn_face(height)
    h = f.height + BURN_PAD
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


def read_burn(strip: rs.Raster, height: int, name_chars: int = 0, alphabet: str = rs.READABLE) -> tuple:
    """(timecode, clip name) read off one burn-in strip from
    :func:`burned_strips`; ``name_chars`` is how many characters of name to
    read after the timecode and its space."""
    f = burn_face(height)
    tc = rs.read_text(strip, f, BURN_PAD, BURN_PAD // 2, 11, alphabet="0123456789:")
    name = rs.read_text(strip, f, BURN_PAD + f.measure(tc + " "), BURN_PAD // 2, name_chars,
                        alphabet=alphabet) if name_chars else ""
    return tc, name


# ---- the text timeline ---------------------------------------------------------------

def text_timeline(timeline, graph, width: int = 64) -> str:
    """The inline timeline a Claude Code user reads: a ruler row with times,
    a marker row, the lanes above the spine, the spine (a transition is an X
    on its cut), the lanes below it, each clip a bar at its place with its
    name inside when it fits; then every event in time order with its
    timecode: clips, markers, transitions."""
    fps = getattr(timeline, "frame_rate", 24) or 24
    total = graph.total
    name = str(getattr(timeline, "name", "timeline") or "timeline")
    head = f"{name} — {_seconds_label(total)} · {len([s for s in graph.segments if s.kind == 'clip'])} clips"
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

    def row(label: str, segments) -> str:
        cells = [" "] * width
        for s in sorted(segments, key=lambda s: s.tl_in):
            piece = bar(s)
            a = col(s.tl_in)
            for i, ch in enumerate(piece):
                if a + i < width:
                    cells[a + i] = ch
        return f"{label:<{gutter}}" + "".join(cells).rstrip()

    lines = [head]
    step = nice_step(total, width, min_px=14)
    ruler, labels = [" "] * width, [" "] * width
    t = Fraction(0)
    while t <= total:
        c = col(t)
        ruler[c] = "|"
        text = _seconds_label(t)
        if all(ch == " " for ch in labels[c:c + len(text) + 1]) and c + len(text) <= width:
            labels[c:c + len(text)] = list(text)
        t += step
    lines.append(f"{'time':<{gutter}}" + "".join(labels).rstrip())
    lines.append(f"{'':<{gutter}}" + "".join(ruler).rstrip())
    markers = []
    for m in list(getattr(timeline, "markers", []) or []):
        markers.append((m.name, getattr(m, "timeline_start", None) or getattr(m, "position", None)))
    for c in list(getattr(timeline, "clips", []) or []):
        for m in getattr(c, "markers", []) or []:
            markers.append((m.name, getattr(m, "position", None)))
    origin = Fraction(str(getattr(timeline, "origin_seconds", 0) or 0)).limit_denominator(100000)
    mrow = [" "] * width
    placed = []
    for mname, at in markers:
        seconds = getattr(at, "seconds", at)
        if seconds is None:
            continue
        t = Fraction(str(seconds)).limit_denominator(100000) - origin
        if not 0 <= t <= total:
            continue
        c = col(t)
        mrow[c] = "^"
        placed.append((t, mname))
    if placed:
        lines.append(f"{'markers':<{gutter}}" + "".join(mrow).rstrip())
    lanes = sorted({s.lane for s in graph.segments})
    for lane in sorted((x for x in lanes if x > 0), reverse=True):
        lines.append(row(f"lane {lane}", [s for s in graph.segments if s.lane == lane]))
    spine_row = row("spine", [s for s in graph.segments if s.lane == 0])
    spine = [s for s in graph.segments if s.lane == 0]
    if graph.transitions:
        cells = list(spine_row.ljust(gutter + width))
        for tr in graph.transitions:
            if tr.boundary + 1 < len(spine):
                cells[gutter + col(spine[tr.boundary + 1].tl_in)] = "X"
        spine_row = "".join(cells).rstrip()
    lines.append(spine_row)
    for lane in sorted((x for x in lanes if x < 0), reverse=True):
        lines.append(row(f"lane {lane}", [s for s in graph.segments if s.lane == lane]))
    lines.append("")
    # one list in time order; at one instant the marker, then the transition
    # onto the cut, then the clip that starts there, then lanes top to bottom
    events = []
    for t, mname in placed:
        events.append((t, 0, 0, f"marker   {mname}"))
    for tr in graph.transitions:
        if tr.boundary + 1 < len(spine):
            a, b = spine[tr.boundary], spine[tr.boundary + 1]
            events.append((b.tl_in, 1, 0, f"X        {tr.name or tr.kind} ({_seconds_label(tr.duration)}), "
                                         f"{a.label} into {b.label}"))
    for seg in graph.segments:
        if seg.kind != "clip":
            continue
        where = "spine" if seg.lane == 0 else f"lane {seg.lane}"
        flag = "  [MEDIA MISSING]" if seg.missing else ""
        events.append((seg.tl_in, 2, -seg.lane if seg.lane else 0,
                       f"{where:<8} {seg.label} ({_seconds_label(seg.duration)}){flag}"))
    for t, _order, _lane, text in sorted(events, key=lambda e: e[:3]):
        lines.append(f"{timecode(t, fps)}  {text}")
    for note in graph.substitutions:
        lines.append(f"Substituted: {note}")
    return "\n".join(lines)
