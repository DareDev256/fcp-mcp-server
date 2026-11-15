"""Pictures the server draws itself: a raster, a bundled bitmap font, and
PNG/JPEG through the ffmpeg the preview tools already need.

Every proof image (the contact sheet, the filmstrip and waveform, the scenes
strip, the burn-in on a proxy) carries names and timecodes. They are drawn
here, pixel by pixel, from one bundled open-licence face, never by ffmpeg's
``drawtext`` (which needs a libfreetype build and a font on the machine) and
never by a new required dependency. The face is Spleen by Frederic Cambus
(BSD 2-Clause, ``fonts/LICENSE-spleen.txt``): its 12x24 and 16x32 bitmaps,
scaled by whole pixels. There is no TrueType path: the bitmaps meet the 14 px
rule at every size a picture is delivered at, and :func:`read_text` depends on
their exact glyphs. Nothing here imports Pillow.

Because the font is ours, text drawn here can be read back off the pixels
(:func:`read_text`): the tests read a burned timecode out of a PNG or a video
frame and compare it with the timeline, which is the only way to know the
picture says what the timeline says.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

from . import palette
from .words import plural

FONTS = Path(__file__).with_name("fonts")
FFMPEG_TIMEOUT_SECONDS = 120

RGB = tuple  # (r, g, b) 0-255


def colour(token: str) -> RGB:
    """A palette token (dark theme) as an (r, g, b) tuple."""
    return palette.rgb(palette.dark(token))[:3]


# ---- the font ------------------------------------------------------------------

@dataclass(frozen=True)
class Face:
    """One bitmap face at an integer scale. Monospace: every glyph advances
    ``cell_w`` pixels. ``glyphs`` maps a code point to the rows of its cell,
    top to bottom, as ints whose bit ``cell_w - 1 - x`` is pixel x."""

    name: str
    cell_w: int
    cell_h: int
    ascent: int
    cap: int  # height of "H" in the cell, in pixels: what a reader sees as the letter size
    glyphs: dict
    scale: int = 1

    @property
    def advance(self) -> int:
        return self.cell_w * self.scale

    @property
    def height(self) -> int:
        """The face's pixel size (its em) as drawn."""
        return self.cell_h * self.scale

    @property
    def cap_height(self) -> int:
        return self.cap * self.scale

    def measure(self, text: str) -> int:
        return len(text) * self.advance

    def rows(self, ch: str) -> list:
        return self.glyphs.get(ord(ch)) or self.glyphs.get(ord("?"))

    def scaled(self, scale: int) -> "Face":
        return Face(self.name, self.cell_w, self.cell_h, self.ascent, self.cap, self.glyphs, scale)


def _parse_bdf(path: Path) -> Face:
    """Read a BDF file into a Face (scale 1)."""
    cell_w = cell_h = ascent = 0
    glyphs: dict = {}
    code = None
    bbx = None
    bitmap: Optional[list] = None
    lines = path.read_text(encoding="ascii").splitlines()
    descent = 0
    for line in lines:
        key, _, rest = line.partition(" ")
        if key == "FONTBOUNDINGBOX":
            cell_w, cell_h = (int(v) for v in rest.split()[:2])
        elif key == "FONT_ASCENT":
            ascent = int(rest)
        elif key == "FONT_DESCENT":
            descent = int(rest)
        elif key == "ENCODING":
            code = int(rest.split()[0])
        elif key == "BBX":
            bbx = [int(v) for v in rest.split()]
        elif key == "BITMAP":
            bitmap = []
        elif key == "ENDCHAR":
            w, h, xoff, yoff = bbx
            pad = ((w + 7) // 8) * 8
            rows = [0] * cell_h
            top = ascent - (yoff + h)
            for i, hexrow in enumerate(bitmap or []):
                value = int(hexrow, 16)
                bits = 0
                for x in range(w):
                    if value >> (pad - 1 - x) & 1 and 0 <= x + xoff < cell_w:
                        bits |= 1 << (cell_w - 1 - (x + xoff))
                if 0 <= top + i < cell_h:
                    rows[top + i] = bits
            glyphs[code] = rows
            code, bbx, bitmap = None, None, None
        elif bitmap is not None and key:
            bitmap.append(key)
    if cell_h != ascent + descent:
        cell_h = ascent + descent
    h_rows = glyphs.get(ord("H"), [])
    cap = sum(1 for r in h_rows if r)
    return Face(path.stem, cell_w, cell_h, ascent, cap, glyphs)


@functools.lru_cache(maxsize=None)
def _bitmap(size: int) -> Face:
    return _parse_bdf(FONTS / {24: "spleen-12x24.bdf", 32: "spleen-16x32.bdf"}[size])


def face(size: int = 24, scale: int = 1) -> Face:
    """The bundled bitmap face of pixel size 24 or 32, at an integer scale."""
    return _bitmap(size).scaled(scale)


# ---- the raster ------------------------------------------------------------------

@dataclass
class TextRun:
    """One piece of text as drawn: where, in which face, at what size."""

    text: str
    x: int
    y: int
    face: Face

    @property
    def width(self) -> int:
        return self.face.measure(self.text)

    @property
    def height(self) -> int:
        return self.face.height

    def box(self) -> tuple:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


@dataclass
class Raster:
    """An image in memory: ``channels`` 3 (RGB) or 4 (RGBA), row-major bytes.

    It keeps a record of every text run drawn on it, so a legibility rule
    (:func:`legibility_problems`) and a collision rule read what was drawn
    rather than what was meant."""

    width: int
    height: int
    channels: int = 3
    px: bytearray = field(default=None)  # type: ignore[assignment]
    runs: list = field(default_factory=list)

    def __post_init__(self):
        if self.px is None:
            self.px = bytearray(self.width * self.height * self.channels)

    @classmethod
    def filled(cls, width: int, height: int, rgb: RGB, alpha: Optional[int] = None) -> "Raster":
        channels = 3 if alpha is None else 4
        r = cls(width, height, channels)
        r.fill(0, 0, width, height, rgb, alpha)
        return r

    def _pixel(self, rgb: RGB, alpha: Optional[int]) -> bytes:
        return bytes(rgb[:3]) + (bytes([255 if alpha is None else alpha]) if self.channels == 4 else b"")

    def fill(self, x: int, y: int, w: int, h: int, rgb: RGB, alpha: Optional[int] = None) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        run = self._pixel(rgb, alpha) * (x1 - x0)
        c = self.channels
        for yy in range(y0, y1):
            start = (yy * self.width + x0) * c
            self.px[start:start + len(run)] = run

    def blend(self, x: int, y: int, w: int, h: int, rgb: RGB, opacity: float) -> None:
        """Paint ``rgb`` over the area at ``opacity`` (RGB rasters)."""
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        c, a = self.channels, opacity
        for yy in range(y0, y1):
            base = (yy * self.width) * c
            for xx in range(x0, x1):
                i = base + xx * c
                for k in range(3):
                    self.px[i + k] = int(self.px[i + k] * (1 - a) + rgb[k] * a + 0.5)

    def paste(self, src: "Raster", x: int, y: int) -> None:
        """Copy ``src`` (same channel count) with its top left at (x, y)."""
        if src.channels != self.channels:
            raise ValueError("paste needs rasters with the same channels")
        c = self.channels
        for sy in range(src.height):
            yy = y + sy
            if not 0 <= yy < self.height:
                continue
            sx0, sx1 = max(0, -x), min(src.width, self.width - x)
            if sx1 <= sx0:
                return
            s = (sy * src.width + sx0) * c
            d = (yy * self.width + x + sx0) * c
            self.px[d:d + (sx1 - sx0) * c] = src.px[s:s + (sx1 - sx0) * c]

    def get(self, x: int, y: int) -> tuple:
        i = (y * self.width + x) * self.channels
        return tuple(self.px[i:i + self.channels])

    def text(self, s: str, f: Face, x: int, y: int, rgb: RGB, alpha: Optional[int] = None) -> TextRun:
        """Draw ``s`` with its cell's top left at (x, y). Returns the run."""
        pixel = self._pixel(rgb, alpha)
        c, sc = self.channels, f.scale
        block = pixel * sc
        for n, ch in enumerate(s):
            rows = f.rows(ch)
            gx = x + n * f.advance
            for ry, bits in enumerate(rows):
                if not bits:
                    continue
                for cx in range(f.cell_w):
                    if not bits >> (f.cell_w - 1 - cx) & 1:
                        continue
                    px0 = gx + cx * sc
                    if px0 < 0 or px0 + sc > self.width:
                        continue
                    for dy in range(sc):
                        yy = y + ry * sc + dy
                        if 0 <= yy < self.height:
                            i = (yy * self.width + px0) * c
                            self.px[i:i + len(block)] = block
        run = TextRun(s, x, y, f)
        self.runs.append(run)
        return run

    # ---- encode ----------------------------------------------------------------------

    def encode(self, fmt: str = "png", quality: int = 3) -> Optional[bytes]:
        """PNG or JPEG bytes through ffmpeg, or None without ffmpeg."""
        if shutil.which("ffmpeg") is None:
            return None
        pix = "rgba" if self.channels == 4 else "rgb24"
        codec = ["-vcodec", "png"] if fmt == "png" else ["-vcodec", "mjpeg", "-q:v", str(quality), "-pix_fmt", "yuvj444p"]
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix,
                 "-s", f"{self.width}x{self.height}", "-i", "pipe:0", "-frames:v", "1", *codec,
                 "-f", "image2pipe", "pipe:1"],
                input=bytes(self.px), capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 and result.stdout else None

    def save(self, path: str, fmt: str = "png") -> Optional[str]:
        data = self.encode(fmt)
        if data is None:
            return None
        Path(path).write_bytes(data)
        return str(path)


# ---- reading pictures in -------------------------------------------------------------

def _raw(args: list, size: int) -> Optional[bytes]:
    if shutil.which("ffmpeg") is None:
        return None
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args],
                                capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = result.stdout
    return out[:size] if result.returncode == 0 and len(out) >= size else None


def frame(source: str, at: Fraction, width: int, height: int) -> Optional[Raster]:
    """The frame of ``source`` at ``at`` seconds, fitted inside width x height
    (aspect kept, the rest black), or None."""
    if not Path(source).is_file():
        return None
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=rgb24")
    data = _raw(["-ss", str(float(at)), "-i", str(source), "-frames:v", "1", "-vf", vf,
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], width * height * 3)
    if data is None:
        return None
    return Raster(width, height, 3, bytearray(data))


def resize(r: Raster, width: int, height: int) -> Optional[Raster]:
    """``r`` scaled to width x height through ffmpeg (area filter: no ringing on text)."""
    pix = "rgba" if r.channels == 4 else "rgb24"
    if shutil.which("ffmpeg") is None:
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix,
             "-s", f"{r.width}x{r.height}", "-i", "pipe:0", "-vf", f"scale={width}:{height}:flags=area",
             "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"],
            input=bytes(r.px), capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    size = width * height * r.channels
    if result.returncode != 0 or len(result.stdout) < size:
        return None
    return Raster(width, height, r.channels, bytearray(result.stdout[:size]))


def image_size(path: str) -> Optional[tuple]:
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS).stdout
        w, h = (int(v) for v in out.strip().split(",")[:2])
        return w, h
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def load(path: str, at: Optional[Fraction] = None) -> Optional[Raster]:
    """A picture (PNG/JPEG) or a video frame read back as RGB, at its own size."""
    size = image_size(path)
    if size is None:
        return None
    w, h = size
    args = (["-ss", str(float(at))] if at is not None else []) + ["-i", str(path), "-frames:v", "1",
                                                                  "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    data = _raw(args, w * h * 3)
    return Raster(w, h, 3, bytearray(data)) if data is not None else None


# ---- reading text back off the pixels -------------------------------------------------

READABLE = "0123456789:;.,-+_/()[]#@%&*'\"!?=<>|abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ "


def read_text(r: Raster, f: Face, x: int, y: int, n: int, *, light_on_dark: bool = True,
              alphabet: str = READABLE, threshold: int = 128) -> str:
    """Read ``n`` characters drawn in face ``f`` with the top left of the
    first cell at (x, y): each cell is thresholded and matched against every
    glyph of the alphabet, nearest wins. Survives JPEG and H.264 because the
    faces are bitmaps on a solid ground."""
    sc = f.scale
    refs = {ch: f.rows(ch) for ch in alphabet}
    out = []
    for k in range(n):
        gx = x + k * f.advance
        cell = []
        for ry in range(f.cell_h):
            bits = 0
            for cx in range(f.cell_w):
                px, py = gx + cx * sc + sc // 2, y + ry * sc + sc // 2
                if not (0 <= px < r.width and 0 <= py < r.height):
                    continue
                i = (py * r.width + px) * r.channels
                luma = 0.2126 * r.px[i] + 0.7152 * r.px[i + 1] + 0.0722 * r.px[i + 2]
                on = luma >= threshold if light_on_dark else luma < threshold
                if on:
                    bits |= 1 << (f.cell_w - 1 - cx)
            cell.append(bits)
        best = min(refs, key=lambda ch: sum(bin(a ^ b).count("1") for a, b in zip(cell, refs[ch])))
        out.append(best)
    return "".join(out)


# ---- the rules every proof image keeps ------------------------------------------------

# Claude's vision input (platform.claude.com/docs/en/build-with-claude/vision,
# "Resolution and token cost", read 2026-11-13): the standard tier keeps an
# image whose long edge is at most 1,568 px AND whose ceil(w/28) x ceil(h/28)
# patches number at most 1,568; anything larger is scaled down before the model
# sees it, and small text is what a downscale loses first. The high-resolution
# tier (2,576 px / 4,784 patches) keeps everything the standard tier keeps.
LONG_EDGE_MAX = 1568
VISUAL_TOKENS_MAX = 1568
PATCH = 28
GLYPH_MIN = 14   # cap height of drawn text, in delivered pixels
CELLS_MAX = 12   # frames on one sheet page


def visual_tokens(width: int, height: int) -> int:
    return -(-width // PATCH) * -(-height // PATCH)


def legibility_problems(r: Raster, delivered_scale: float = 1.0, cells: int = 0) -> list:
    """Every way this picture breaks the legibility rule. Empty means it keeps it:
    every text run's cap height, at the size the picture is delivered, is at
    least GLYPH_MIN px; the long edge and the patch count are inside the
    budget that keeps the model from downscaling it; at most CELLS_MAX cells."""
    out = []
    long_edge = max(r.width, r.height)
    if long_edge > LONG_EDGE_MAX:
        out.append(f"long edge {long_edge} px (at most {LONG_EDGE_MAX})")
    tokens = visual_tokens(r.width, r.height)
    if tokens > VISUAL_TOKENS_MAX:
        out.append(f"{plural(tokens, 'visual token')} (at most {VISUAL_TOKENS_MAX}): it would be downscaled")
    for run in r.runs:
        shown = run.face.cap_height * delivered_scale
        if shown < GLYPH_MIN:
            out.append(f"{run.text!r} has {shown:.1f} px letters delivered (at least {GLYPH_MIN})")
            break
    if cells > CELLS_MAX:
        out.append(f"{plural(cells, 'cell')} on one page (at most {CELLS_MAX})")
    return out


def collisions(r: Raster) -> list:
    """Text runs that overlap each other or leave the picture."""
    out = []
    boxes = [run.box() for run in r.runs]
    for run, (x0, y0, x1, y1) in zip(r.runs, boxes):
        if x0 < 0 or y0 < 0 or x1 > r.width or y1 > r.height:
            out.append(f"{run.text!r} leaves the picture")
    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                out.append(f"{r.runs[i].text!r} overlaps {r.runs[j].text!r}")
    return out


def fit(text: str, f: Face, width: int) -> list:
    """``text`` broken into lines no wider than ``width`` (at spaces, or
    mid-word when one word is too long): never truncated."""
    per = max(1, width // f.advance)
    lines, line = [], ""
    for word in text.split(" "):
        while len(word) > per:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:per])
            word = word[per:]
        candidate = f"{line} {word}" if line else word
        if len(candidate) <= per:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]
