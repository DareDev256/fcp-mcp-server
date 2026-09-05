"""Transcript intelligence — local Whisper transcription + text-driven editing.

v0.13 slice 1: transcript-based editing. Transcription runs locally via
faster-whisper (optional ``[transcribe]`` extra) with word-level timestamps;
without it, or when media is missing/unreadable, ``transcribe`` returns
``None`` so callers degrade to an install hint instead of crashing — the
same contract as ``media_intel.detect_beats``.

The matching helpers below are pure functions over word lists so they are
fully testable without any model installed, and so tools can accept
pre-computed transcripts (from a previous ``transcribe_media`` run) instead
of re-transcribing.
"""

import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Model names are used to resolve (and download) model weights, so they are
# validated against an allowlist, not trusted.
ALLOWED_MODELS = (
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v2", "large-v3", "distil-large-v3",
)

# Conservative by default: interjections that are near-universally filler.
# "like" / "so" / "actually" are speech, not noise, unless the user opts in.
DEFAULT_FILLERS = ("um", "uh", "uhh", "umm", "erm", "ehm", "mmm", "hmm", "mhm")

# Backends. "local" never leaves the machine. "elevenlabs" uploads the whole
# media file to api.elevenlabs.io (Scribe) and returns speakers and audio
# events in exchange; it is opt-in per call and needs ELEVENLABS_API_KEY.
BACKENDS = ("local", "elevenlabs")
SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
SCRIBE_MODEL = "scribe_v2"
SCRIBE_TIMEOUT_SECONDS = 600
SCRIBE_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
SCRIBE_KEY_ENV = "ELEVENLABS_API_KEY"

_NORM_RE = re.compile(r"[^\w']+")


def normalize_word(word: str) -> str:
    """Lowercase a word and strip punctuation so matching survives Whisper's
    tokenization quirks (leading spaces, trailing commas, case)."""
    return _NORM_RE.sub("", word.lower())


def find_phrase_spans(
    words: Sequence[dict], phrase: str
) -> List[Tuple[float, float]]:
    """Find every occurrence of ``phrase`` in a word-level transcript.

    ``words`` is a sequence of ``{"word", "start", "end"}`` dicts in source
    seconds. Matching is case- and punctuation-insensitive. Returns
    ``(start, end)`` source-second ranges spanning first to last matched word.
    """
    target = [normalize_word(w) for w in phrase.split()]
    target = [t for t in target if t]
    if not target:
        return []
    normed = [normalize_word(w.get("word", "")) for w in words]
    spans: List[Tuple[float, float]] = []
    i = 0
    n, m = len(normed), len(target)
    while i <= n - m:
        if normed[i:i + m] == target:
            spans.append((float(words[i]["start"]), float(words[i + m - 1]["end"])))
            i += m
        else:
            i += 1
    return spans


def find_filler_spans(
    words: Sequence[dict], fillers: Sequence[str] = DEFAULT_FILLERS
) -> List[Tuple[float, float]]:
    """Find filler-word occurrences (single- or multi-word fillers)."""
    spans: List[Tuple[float, float]] = []
    for filler in fillers:
        spans.extend(find_phrase_spans(words, filler))
    spans.sort()
    return spans


def merge_ranges(
    ranges: Sequence[Tuple[float, float]], min_gap: float = 0.0
) -> List[Tuple[float, float]]:
    """Merge overlapping (or nearly touching, within ``min_gap``) ranges."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + min_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def invert_ranges(
    ranges: Sequence[Tuple[float, float]], window_start: float, window_end: float
) -> List[Tuple[float, float]]:
    """Complement of ``ranges`` within ``[window_start, window_end]`` —
    turns keep-ranges into cut-ranges for keep_only mode."""
    if window_end <= window_start:
        return []
    kept = merge_ranges(
        [(max(s, window_start), min(e, window_end)) for s, e in ranges
         if min(e, window_end) > max(s, window_start)]
    )
    if not kept:
        return [(window_start, window_end)]
    out: List[Tuple[float, float]] = []
    cursor = window_start
    for start, end in kept:
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < window_end:
        out.append((cursor, window_end))
    return out


def is_diarized(data: dict) -> bool:
    """True when a transcript carries speaker tags — i.e. came from a
    diarizing backend. A cached local transcript cannot satisfy a request
    for speakers, and this is how the loaders tell the two apart."""
    return any(w.get("speaker") for w in data.get("words", []))


def _filename_params(name: str) -> str:
    """Render the ``filename`` parameters for a Content-Disposition header.

    A header is not a place for raw UTF-8. Writing a Chinese clip name
    straight into ``filename="..."`` puts non-ASCII bytes on the wire in a
    field the spec says is ASCII, and what happens next belongs to whichever
    server receives it: rejection, mojibake, or a truncated name that the
    transcript then carries. RFC 6266/2231 answers this with two parameters —
    an ASCII ``filename`` any parser can read, and a ``filename*`` carrying
    the real name percent-encoded as UTF-8. Senders emit both; receivers that
    understand ``filename*`` prefer it, and the rest still get something.

    The ASCII fallback keeps the extension, because that is the part a
    receiver is most likely to act on. A name that is entirely non-ASCII
    falls back to ``upload`` plus its suffix rather than an empty string.
    """
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    ascii_stem = "".join(c for c in stem if (c.isascii() and c.isalnum()) or c in "-_")
    # Punctuation alone is not a name. A stem of "зима_общий_план" survives the
    # filter as "__", and every Russian clip in a folder collapses onto the same
    # fallback. Require at least one alphanumeric before trusting what is left.
    if not any(c.isalnum() for c in ascii_stem):
        ascii_stem = "upload"
    ascii_suffix = "".join(c for c in suffix if c.isascii() and c.isalnum())
    fallback = f"{ascii_stem}.{ascii_suffix}" if ascii_suffix else ascii_stem
    params = f'filename="{fallback}"'
    if not name.isascii():
        params += f"; filename*=UTF-8''{quote(name, safe='')}"
    return params


def _multipart(fields: dict, file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = "----fcpmcp" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    out += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"{_filename_params(file_path.name)}\r\nContent-Type: {ctype}\r\n\r\n"
    ).encode()
    out += file_path.read_bytes()
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _transcribe_scribe(file_path: Path, language: Optional[str]) -> Optional[dict]:
    """Upload *file_path* to ElevenLabs Scribe; map its reply to our shape.

    The key travels in the ``xi-api-key`` header only. It is never placed in
    the URL, the body, a log line, or the returned dict.
    """
    key = os.environ.get(SCRIBE_KEY_ENV, "").strip()
    if not key:
        logger.info("%s not set; elevenlabs backend unavailable", SCRIBE_KEY_ENV)
        return None
    fields = {
        "model_id": SCRIBE_MODEL,
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if language:
        fields["language_code"] = language
    try:
        body, ctype = _multipart(fields, "file", file_path)
    except OSError:
        return None
    req = urllib.request.Request(
        SCRIBE_URL, data=body, method="POST",
        headers={"Content-Type": ctype, "xi-api-key": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SCRIBE_TIMEOUT_SECONDS) as resp:
            raw = resp.read(SCRIBE_MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("scribe request failed for %s: %s", file_path.name, type(exc).__name__)
        return None
    if len(raw) > SCRIBE_MAX_RESPONSE_BYTES:
        logger.warning("scribe response for %s exceeded %d bytes", file_path.name, SCRIBE_MAX_RESPONSE_BYTES)
        return None
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("words"), list):
        return None
    return _map_scribe(payload, file_path)


def _map_scribe(payload: dict, file_path: Path) -> dict:
    speakers: dict = {}
    words: List[dict] = []
    events: List[dict] = []
    for item in payload["words"]:
        kind = item.get("type")
        start, end = item.get("start"), item.get("end")
        if start is None or end is None:
            continue
        if kind == "audio_event":
            events.append({"start": float(start), "end": float(end),
                           "label": str(item.get("text", "")).strip("()[] ").lower() or "event"})
        elif kind == "word":
            sid = item.get("speaker_id")
            speaker = None
            if sid is not None:
                speaker = speakers.setdefault(sid, f"S{len(speakers)}")
            words.append({"word": str(item.get("text", "")).strip(), "start": float(start),
                          "end": float(end), "speaker": speaker})
    # Segments: one per run of the same speaker, so SRT export still works.
    segments: List[dict] = []
    for w in words:
        if segments and segments[-1]["_spk"] == w["speaker"]:
            segments[-1]["text"] += " " + w["word"]
            segments[-1]["end"] = w["end"]
        else:
            segments.append({"text": w["word"], "start": w["start"], "end": w["end"], "_spk": w["speaker"]})
    for seg in segments:
        seg.pop("_spk")
    duration = payload.get("audio_duration_secs")
    if duration is None:
        duration = max((w["end"] for w in words), default=0.0)
    return {
        "backend": "elevenlabs",
        "language": payload.get("language_code"),
        "duration": float(duration),
        "text": payload.get("text", ""),
        "segments": segments,
        "words": words,
        "events": events,
    }


def transcribe(
    path: str, model_size: str = "base", language: Optional[str] = None,
    backend: str = "local",
) -> Optional[dict]:
    """Transcribe an audio/video file with word-level timestamps.

    ``backend="local"`` runs faster-whisper (optional ``[transcribe]`` extra)
    and nothing leaves the machine. ``backend="elevenlabs"`` uploads the file
    to Scribe and adds ``speaker`` on each word plus an ``events`` list.
    Returns ``None`` when the backend is unavailable or the file is
    missing/unreadable.

    Returns:
        ``{"language": str, "duration": float, "text": str,
           "segments": [{"text", "start", "end"}, ...],
           "words": [{"word", "start", "end", "speaker"?}, ...],
           "events"?: [{"start", "end", "label"}, ...], "backend"?: str}``
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}, got {backend!r}")
    if model_size not in ALLOWED_MODELS:
        raise ValueError(
            f"model_size must be one of {', '.join(ALLOWED_MODELS)}, got {model_size!r}"
        )
    file_path = Path(path)
    if not file_path.is_file():
        return None
    if backend == "elevenlabs":
        return _transcribe_scribe(file_path, language)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.info("faster-whisper not installed; transcription unavailable")
        return None
    try:
        model = WhisperModel(model_size, compute_type="int8")
        segments_iter, info = model.transcribe(
            str(file_path), language=language, word_timestamps=True
        )
        segments: List[dict] = []
        words: List[dict] = []
        for seg in segments_iter:
            segments.append(
                {"text": seg.text.strip(), "start": float(seg.start), "end": float(seg.end)}
            )
            for w in seg.words or []:
                words.append(
                    {"word": w.word.strip(), "start": float(w.start), "end": float(w.end)}
                )
    except Exception:
        logger.warning("whisper transcription failed for %s", file_path)
        return None
    return {
        "language": info.language,
        "duration": float(info.duration),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "words": words,
    }


def segments_to_srt(segments: Sequence[dict]) -> str:
    """Render transcript segments as an SRT string (for captions import)."""

    def stamp(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for i, seg in enumerate(segments, 1):
        blocks.append(f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n{seg['text']}\n")
    return "\n".join(blocks)
