"""The clock Final Cut gives a media file, read from the file itself (#45).

A clip's ``start`` is on its asset's clock, which begins at ``<asset start>``
(see ``media_intel.file_in_point`` for the read side). Final Cut does not take
that origin from the FCPXML: it takes it from the MEDIA. A QuickTime file with
a timecode track (``tmcd``) of 18:03:22:05 at 30 fps is on a clock that starts
at 1950065/30s, whatever the document says. An asset written at ``start="0s"``
with clips at 5s and 20s therefore asks for frames before the file begins, and
Final Cut imports an empty timeline without a warning.

Only the ``tmcd`` track counts. Sony XAVC ``.mp4`` (FX3, FX2, A7R V) keeps
its timecode in an ``rtmd`` metadata track, ffprobe prints it as a
``timecode`` tag all the same, and Final Cut ignores it and starts the file
at 0: written at that timecode, every clip became a gap (#45, tested by the
reporter; Final Cut's own export of 35 Sony ``.MP4`` clips writes every
asset at ``start="0s"``). A timecode found anywhere else is reported and
never written.

Timecode labels count NOMINAL frames (30 at 29.97) and each frame lasts the
real frame duration, so 01:00:00:00 non-drop at 29.97 is 108000 x 1001/30000
= 3603.6s. Drop-frame (``;``) skips two labels a minute (four at 59.94),
except every tenth minute, so 01:00:00;00 is 107892 frames.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

from fcpxml.rational import format_exact_seconds, format_seconds, to_frames

PROBE_TIMEOUT_SECONDS = 30

# What a reading is: on the file's own timecode clock (tmcd); a file with no
# timecode (none); a timecode Final Cut does not use (ignored); or not read at
# all (unread: no file, no ffprobe, a probe that failed).
TMCD, NONE, IGNORED, UNREAD = "tmcd", "none", "ignored", "unread"


@dataclass(frozen=True)
class MediaClock:
    """Where a file's clock starts, and how that was decided.

    ``origin`` is exact seconds: the value for ``<asset start>``. It is 0 for
    every reading but ``tmcd``. ``timecode`` is the label as the file carries
    it ("18:03:22:05", "01:00:00;00"); for ``ignored`` it is the label that
    was NOT used. ``note`` says why, in words a reply can carry.
    """
    origin: Fraction = Fraction(0)
    source: str = NONE
    timecode: Optional[str] = None
    rate: Optional[Fraction] = None
    drop: bool = False
    quicktime: bool = False
    note: Optional[str] = None

    @property
    def start_attr(self) -> str:
        """``<asset start>``: on the timecode's own frame grid when there is one."""
        return clock_attr(self.origin, self.rate)

    def place(self, file_time: Fraction) -> Fraction:
        """A position in the FILE (from 0) as a clip ``start`` on this clock."""
        return self.origin + Fraction(file_time)

    def clip_start(self, file_time: Fraction, frame: Fraction) -> Fraction:
        """:meth:`place`, on the SEQUENCE's frame grid and never before the
        file begins.

        A clip's ``start`` is read back as a whole number of sequence frames
        (the parser's Timecode), so it is written on that grid, as the
        in-point always was. Where the file's clock is not on it (29.97
        drop-frame in a 30p cut) the snap moves it under a frame, and only
        ever later: a start before ``<asset start>`` asks for media the file
        does not have.
        """
        target = self.place(file_time)
        frame = Fraction(frame)
        snapped = to_frames(target, frame) * frame
        return snapped + frame if snapped < self.origin else snapped

    def clip_start_attr(self, file_time: Fraction, frame: Fraction) -> str:
        return format_seconds(self.clip_start(file_time, frame), Fraction(frame))


def clock_attr(value: Fraction, *rates: Optional[Fraction]) -> str:
    """Exact seconds as an FCPXML time, over a timebase Final Cut writes.

    ``1950065/30s`` rather than ``390013/6s``, and ``107999892/30000s`` for
    drop-frame 01:00:00;00: the first rate whose frame grid the value is on
    writes it in frames, else the first whose timebase holds it exactly.
    """
    value = Fraction(value)
    frames = [1 / Fraction(rate) for rate in rates if rate]
    for frame in frames:
        if (value / frame).denominator == 1:
            return format_seconds(value, frame)
    for frame in frames:
        scaled = value * frame.denominator
        if scaled.denominator == 1:
            return f"{scaled.numerator}/{frame.denominator}s"
    return format_exact_seconds(value)


def _timecode_tag(entry: Optional[dict]) -> Optional[str]:
    """The ``timecode`` tag of a stream or format, whatever its case: ffprobe
    prints a QuickTime one as ``timecode`` and a Matroska one as ``TIMECODE``."""
    for key, value in ((entry or {}).get("tags") or {}).items():
        if key.lower() == "timecode" and value:
            return str(value)
    return None


def _rate(text) -> Optional[Fraction]:
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


_NTSC = tuple(Fraction(n * 1000, 1001) for n in (24, 30, 48, 60, 120))


def exact_rate(rate: Fraction) -> Fraction:
    """A timecode track's rate as the exact NTSC fraction it stands for.

    Some files carry 29.97 as ``2997/100``: it is ``30000/1001``, and only
    the exact value counts drop-frame labels and lands on Final Cut's
    timebase (``107999892/30000s``, not ``10800000/2997s``)."""
    for ntsc in _NTSC:
        if abs(rate - ntsc) < Fraction(1, 100):
            return ntsc
    return rate


def nominal_rate(rate: Fraction) -> int:
    """Frames per labelled second: 30 at 29.97, 24 at 23.976."""
    return max(1, int(round(rate)))


def drop_per_minute(rate: Fraction) -> int:
    """Labels drop-frame timecode skips each minute: 2 at 29.97, 4 at 59.94,
    0 where drop-frame does not exist."""
    if rate.denominator == 1001 and nominal_rate(rate) in (30, 60):
        return nominal_rate(rate) // 15
    return 0


def smpte_frames(label: str, rate: Fraction) -> int:
    """The frame count a SMPTE label names at *rate*.

    ``;`` (or ``.``) before the frames field marks drop-frame. Raises
    ValueError on a label that is not HH:MM:SS:FF or names a frame the rate
    does not have.
    """
    text = str(label).strip()
    drop = ";" in text or "." in text
    parts = text.replace(";", ":").replace(".", ":").split(":")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a SMPTE timecode: {label!r}")
    h, m, s, f = map(int, parts)
    nominal = nominal_rate(rate)
    if m > 59 or s > 59 or f >= nominal:
        raise ValueError(f"{label!r} is not a valid timecode at {float(rate):g} fps")
    frames = (h * 3600 + m * 60 + s) * nominal + f
    skip = drop_per_minute(rate) if drop else 0
    if skip:
        minutes = h * 60 + m
        if s == 0 and f < skip and m % 10:
            raise ValueError(f"{label!r} does not exist in drop-frame timecode")
        frames -= skip * (minutes - minutes // 10)
    return frames


def smpte_label(frames: int, rate: Fraction, drop: bool = False) -> str:
    """The SMPTE label of frame *frames* at *rate*: the inverse of
    :func:`smpte_frames`. Drop-frame (29.97, 59.94) adds back the labels a
    minute skips, except every tenth minute."""
    nominal = nominal_rate(rate)
    skip = drop_per_minute(rate) if drop else 0
    if skip:
        per_ten = nominal * 600 - 9 * skip
        per_minute = nominal * 60 - skip
        tens, rest = divmod(frames, per_ten)
        frames += 9 * skip * tens + (skip * ((rest - skip) // per_minute) if rest >= skip else 0)
    seconds, f = divmod(frames, nominal)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}{';' if drop else ':'}{f:02d}"


def clock_from_probe(data: dict) -> MediaClock:
    """A file's clock from ffprobe's JSON (``-show_entries
    stream=codec_type,codec_tag_string,r_frame_rate,avg_frame_rate:stream_tags=timecode:format=format_name:format_tags=timecode``).

    Pure: the rule lives here, so it is tested on the shapes ffprobe prints
    for an iPhone ProRes, a Sony XAVC and a plain file without any of them.
    """
    streams = data.get("streams") or []
    form = data.get("format") or {}
    quicktime = "mov" in (form.get("format_name") or "").split(",")
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    tmcd = next((s for s in streams if (s.get("codec_tag_string") or "").lower() == "tmcd"), None)

    if tmcd is not None:
        label = _timecode_tag(tmcd)
        rate = _rate(tmcd.get("avg_frame_rate")) or _rate(video and video.get("r_frame_rate"))
        rate = exact_rate(rate) if rate else None
        if not label or rate is None:
            return MediaClock(source=UNREAD, quicktime=quicktime,
                              note="it has a timecode track whose start could not be read")
        try:
            frames = smpte_frames(label, rate)
        except ValueError as exc:
            return MediaClock(source=UNREAD, quicktime=quicktime, note=str(exc))
        drop = bool(drop_per_minute(rate)) and (";" in label or "." in label)
        return MediaClock(origin=frames / rate, source=TMCD, timecode=label, rate=rate,
                          drop=drop, quicktime=quicktime)

    other = next((t for t in [*(_timecode_tag(s) for s in streams), _timecode_tag(form)] if t), None)
    container = ((form.get("format_name") or "").split(",") or ["?"])[0] or "?"
    if other and quicktime:
        # The one case with evidence: a QuickTime-family file whose timecode
        # is not in a tmcd track (Sony XAVC .mp4, rtmd) starts at 0 in Final Cut.
        return MediaClock(
            source=IGNORED, timecode=other, quicktime=quicktime,
            note=(f"it carries timecode {other} outside a QuickTime timecode track (tmcd); "
                  "Final Cut starts such a file at 0 (Sony XAVC .mp4 does this), so it was written at 0s"),
        )
    if other:
        return MediaClock(
            source=UNREAD, timecode=other, quicktime=quicktime,
            note=(f"it carries timecode {other} in a {container} file, and how Final Cut clocks "
                  f"that container is not verified"),
        )
    reference = ((form.get("tags") or {}).get("time_reference") or "").strip()
    if reference.isdigit() and int(reference):
        return MediaClock(
            source=UNREAD, quicktime=quicktime,
            note=f"it carries a broadcast WAV time reference (sample {reference}), which Final Cut may start it at; not verified",
        )
    return MediaClock(source=NONE, quicktime=quicktime)


def read_clock(path: str) -> MediaClock:
    """The clock of the file at *path* (a filesystem path, not a URL)."""
    if not path or not Path(path).is_file():
        return MediaClock(source=UNREAD, note="the file is not on this machine")
    if shutil.which("ffprobe") is None:
        return MediaClock(source=UNREAD, note="ffprobe is not installed")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_tag_string,r_frame_rate,avg_frame_rate"
             ":stream_tags=timecode:format=format_name:format_tags=timecode,time_reference",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = None
    if not data:
        return MediaClock(source=UNREAD, note="ffprobe could not read it")
    return clock_from_probe(data)


def media_url(src: str) -> str:
    """A media-rep ``src`` the way Final Cut writes one.

    Final Cut writes a percent-encoded ``file://`` URL (``My%20Footage``):
    a raw space or a Chinese folder name is not a URL. An absolute path
    becomes one; anything already carrying a scheme, or a relative path
    whose base is unknown, is left exactly as given.
    """
    if not src or "://" in src:
        return src
    path = Path(src).expanduser()
    return path.as_uri() if path.is_absolute() else src


def clock_lines(clocks: list) -> list:
    """What a reply says about the clocks it wrote, from entries
    ``{"source": path, "clock": MediaClock}``: each file written on its
    timecode, each timecode not used, and each file whose clock was not
    read (written at 0s, with the reason; Final Cut shows its clips empty if
    the file's real clock is another)."""
    lines, unread = [], {}
    for entry in clocks:
        name, clock = Path(entry["source"]).name, entry["clock"]
        if clock.source == TMCD:
            lines.append(
                f"On its timecode clock: {name} starts at {clock.timecode} "
                f"({float(clock.rate):g} fps{' drop-frame' if clock.drop else ''}), "
                f"so <asset start> is {clock.start_attr} and its clips are placed from there."
            )
        elif clock.source == IGNORED:
            lines.append(f"Timecode not used: {name}: {clock.note}.")
        elif clock.source == UNREAD:
            unread.setdefault(clock.note, []).append(name)
    for note, names in unread.items():
        lines.append(f"Clock not read, written at 0s: {', '.join(names)} ({note}).")
    if unread:
        lines.append("If one of those files has another clock, Final Cut shows its clips empty; "
                     "author again where the media and ffprobe are present.")
    return lines
