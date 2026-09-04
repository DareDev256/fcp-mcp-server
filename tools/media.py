"""Media intelligence and transcript-driven editing.

Silence detection and removal, beat detection, and everything that starts
from a transcript: transcribe_media, edit_by_transcript, transcript_pack,
remove_filler_words, plus the XML-only silence-candidate heuristics.

Moved out of server.py, which held every handler in one file. The handlers are
unchanged: server.py re-exports them under their original names, so
`TOOL_HANDLERS`, the flat tool list and every test that reaches for
`server.handle_detect_media_silence` keep working against one definition.

`detect_silence`, `detect_beats` and `transcribe` are reached through the
bound server module (`srv.X`), never imported here. Tests monkeypatch those
three on the server module to keep ffmpeg and Whisper out of the suite; an
import would bind a copy at import time that no patch could reach, and the
guard would keep passing while guarding nothing. The same goes for the
sandbox validators and `_setup_modifier`.
"""

import json
import os
from pathlib import Path
from typing import Sequence

from mcp.types import TextContent

from fcpxml import index as _index
from fcpxml import progress as _progress
from fcpxml import transcript_pack as _tpack
from fcpxml.media_intel import map_silence_to_timeline, media_src_to_path
from fcpxml.models import TimeValue
from fcpxml.transcribe import (
    BACKENDS,
    DEFAULT_FILLERS,
    SCRIBE_KEY_ENV,
    SCRIBE_MODEL,
    find_filler_spans,
    find_phrase_spans,
    invert_ranges,
    is_diarized,
    merge_ranges,
    segments_to_srt,
)
from fcpxml.writer import FCPXMLModifier
from tools import _common

# ----- SILENCE DETECTION HANDLERS (v0.5.0) -----

def _silence_cached(media_path: str, noise_db: float, min_silence: float):
    """``detect_silence`` through the index. Same answer with the index off."""
    srv = _common.tools.server_module()
    kind = f"silence@{noise_db}dB/{min_silence}s"
    ix = _index.Index.open()
    if ix is None:
        return srv.detect_silence(media_path, noise_db=noise_db, min_duration=min_silence)
    with ix:
        rows = ix.get_analysis(media_path, kind)
        if rows is not None:
            return [(float(r["start"]), float(r["end"])) for r in rows]
        silences = srv.detect_silence(media_path, noise_db=noise_db, min_duration=min_silence)
        if silences is not None:
            ix.put_analysis(
                media_path, kind, [{"start": a, "end": b, "payload": None} for a, b in silences]
            )
        return silences


def _beats_cached(media_path: str):
    """``detect_beats`` through the index. Same answer with the index off."""
    srv = _common.tools.server_module()
    ix = _index.Index.open()
    if ix is None:
        return srv.detect_beats(media_path)
    with ix:
        rows = ix.get_analysis(media_path, "beat")
        if rows is not None and rows:
            return {
                "bpm": float(rows[0]["payload"]["bpm"]),
                "beats": [float(r["start"]) for r in rows],
            }
        result = srv.detect_beats(media_path)
        if result is not None:
            bpm = result["bpm"]
            ix.put_analysis(
                media_path, "beat",
                [{"start": b, "end": b, "payload": {"bpm": bpm}} for b in result["beats"]],
            )
        return result


async def handle_detect_media_silence(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    noise_db = float(arguments.get("noise_db", -30.0))
    min_silence = float(arguments.get("min_silence", 0.5))
    # Same bounds srv.detect_silence() enforces — validated here so a bad request
    # fails before any media file is opened.
    if not (-120.0 <= noise_db <= 0.0):
        raise ValueError(f"noise_db must be between -120 and 0 dB, got {noise_db}")
    if not (0 < min_silence <= 3600):
        raise ValueError(f"min_silence must be between 0 and 3600 seconds, got {min_silence}")

    _, tl = srv._require_timeline(arguments["filepath"])
    clip_filter = arguments.get("clip_name")

    max_media_probes = 100
    findings: list[tuple[str, float, float]] = []
    skipped: list[tuple[str, str]] = []
    probe_cache: dict[str, list | None] = {}
    clips = tl.media_clips()
    prog = _progress.start(total=len(clips))
    for clip in clips:
        await prog.step(f"silence: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path not in probe_cache:
            if len(probe_cache) >= max_media_probes:
                skipped.append((clip.name, f"probe cap reached ({max_media_probes} media files)"))
                continue
            probe_cache[media_path] = _silence_cached(media_path, noise_db, min_silence)
        silences = probe_cache[media_path]
        if silences is None:
            skipped.append((clip.name, "unanalyzable (ffmpeg missing or media unreadable)"))
            continue
        source_start = clip.source_start.seconds if clip.source_start else 0.0
        mapped = map_silence_to_timeline(
            silences, source_start, clip.duration.seconds, clip.start.seconds,
            min_mapped=(1.0 / tl.frame_rate if tl.frame_rate else 0.0),
        )
        findings.extend((clip.name, start, end) for start, end in mapped)

    total_silence = sum(end - start for _, start, end in findings)
    result = f"""# Media Silence Detection (real audio analysis)

## Summary
- **Threshold**: {noise_db} dB for >= {min_silence}s
- **Media Files Probed**: {len(probe_cache)}
- **Silence Spans Found**: {len(findings)} ({srv.format_duration(total_silence)} total)
"""
    if findings:
        result += "\n## Silence Spans (timeline time)\n"
        result += srv._markdown_table(
            ["Clip", "Start", "End", "Duration"],
            [[name, f"{start:.2f}s", f"{end:.2f}s", f"{end - start:.2f}s"]
             for name, start, end in findings],
        ) + "\n"
        result += "\n*To remove: `split_clip` at each boundary, then `delete_clips` with ripple.*"
    if skipped:
        result += "\n## Skipped Clips\n"
        result += srv._markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    if not findings and not skipped:
        result += "\nNo silence detected in any clip's source audio."
    return srv._text_result(result)


async def handle_remove_media_silence(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    noise_db = float(arguments.get("noise_db", -30.0))
    min_silence = float(arguments.get("min_silence", 0.5))
    padding = float(arguments.get("padding", 0.05))
    if not (-120.0 <= noise_db <= 0.0):
        raise ValueError(f"noise_db must be between -120 and 0 dB, got {noise_db}")
    if not (0 < min_silence <= 3600):
        raise ValueError(f"min_silence must be between 0 and 3600 seconds, got {min_silence}")
    if not (0 <= padding <= 5):
        raise ValueError(f"padding must be between 0 and 5 seconds, got {padding}")

    filepath, output_path, modifier = srv._setup_modifier(arguments, "_silence_removed")
    clip_filter = arguments.get("clip_name")
    fps = modifier._detect_fps()

    def to_frame_timevalue(seconds: float) -> TimeValue:
        # Snap cut boundaries to the frame grid in the 2400-tick timebase so
        # output stays frame-aligned and DTD-friendly.
        return TimeValue(int(round(seconds * fps) * round(2400 / fps)), 2400)

    max_media_probes = 100
    cuts_made: list[tuple[str, int, float]] = []
    skipped: list[tuple[str, str]] = []
    probe_cache: dict[str, list | None] = {}
    spine_clips = [el for _, el in modifier._iter_spine_clips()]
    for el in spine_clips:
        name = el.get("name", "")
        if clip_filter and name != clip_filter:
            continue
        src = modifier.resources.get(el.get("ref", ""), {}).get("src", "")
        media_path = media_src_to_path(src)
        if not media_path or not Path(media_path).is_file():
            skipped.append((name, "media file missing"))
            continue
        if media_path not in probe_cache:
            if len(probe_cache) >= max_media_probes:
                skipped.append((name, f"probe cap reached ({max_media_probes} media files)"))
                continue
            probe_cache[media_path] = _silence_cached(media_path, noise_db, min_silence)
        silences = probe_cache[media_path]
        if silences is None:
            skipped.append((name, "unanalyzable (ffmpeg missing or media unreadable)"))
            continue

        clip_source_start = modifier._parse_time(el.get("start", "0s")).to_seconds()
        clip_duration = modifier._parse_time(el.get("duration", "0s")).to_seconds()
        cut_ranges = []
        for sil_start, sil_end in silences:
            # Source time -> clip-relative, padded so cuts breathe.
            cut_start = max(sil_start, clip_source_start) - clip_source_start + padding
            cut_end = min(sil_end, clip_source_start + clip_duration) - clip_source_start - padding
            if cut_end > cut_start:
                cut_ranges.append((to_frame_timevalue(cut_start), to_frame_timevalue(cut_end)))
        if not cut_ranges:
            continue
        removed = modifier.cut_clip_ranges(el, cut_ranges)
        if removed > TimeValue.zero():
            cuts_made.append((name, len(cut_ranges), removed.to_seconds()))

    if not cuts_made:
        text = "# Media Silence Removal\n\nNo silence found to remove — file unchanged (nothing saved)."
        if skipped:
            text += "\n\n## Skipped Clips\n" + srv._markdown_table(
                ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
            )
        return srv._text_result(text)

    modifier.save(output_path)
    total_removed = sum(seconds for _, _, seconds in cuts_made)
    result = f"""# Media Silence Removal (real audio analysis)

## Summary
- **Threshold**: {noise_db} dB for >= {min_silence}s, padding {padding}s
- **Clips Cut**: {len(cuts_made)}
- **Total Removed**: {srv.format_duration(total_removed)}

## Cuts
"""
    result += srv._markdown_table(
        ["Clip", "Silence Spans Cut", "Removed"],
        [[name, str(count), f"{seconds:.2f}s"] for name, count, seconds in cuts_made],
    ) + "\n"
    if skipped:
        result += "\n## Skipped Clips\n" + srv._markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    result += f"\nSaved to: {output_path}\n\n*Preview first next time with `detect_media_silence`. Original file untouched.*"
    return srv._text_result(result)


AUDIO_MEDIA_EXTENSIONS = (
    '.wav', '.aif', '.aiff', '.mp3', '.m4a', '.aac', '.flac', '.mov', '.mp4',
)


async def handle_detect_beats(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    media_path = srv._validate_filepath(arguments["media_path"], AUDIO_MEDIA_EXTENSIONS)

    result = _beats_cached(media_path)
    if result is None:
        return srv._text_result(
            "Beat detection unavailable — librosa is not installed or the file "
            "could not be analyzed.\n\nInstall the optional media-intelligence "
            "extra:\n\n    pip install 'fcp-mcp-server[intelligence]'"
        )

    bpm, beats = result["bpm"], result["beats"]
    beats_data = {
        "source": str(Path(media_path).name),
        "bpm": round(bpm, 2),
        "beats": [round(b, 4) for b in beats],
        "downbeats": [round(b, 4) for b in beats[::4]],
    }
    json_path = srv._validate_output_path(
        str(Path(media_path).with_name(Path(media_path).stem + "_beats.json")),
        anchor_dir=str(Path(media_path).parent),
    )
    with open(json_path, "w") as f:
        json.dump(beats_data, f, indent=2)

    preview = beats[:16]
    result_text = f"""# Beat Detection

## Summary
- **Source**: {Path(media_path).name}
- **Estimated Tempo**: {bpm:.1f} BPM
- **Beats Detected**: {len(beats)} ({srv.format_duration(beats[-1]) if beats else '0s'} span)
- **Beats JSON**: {json_path}

## First Beats
"""
    result_text += srv._markdown_table(
        ["#", "Time"],
        [[str(i + 1), f"{b:.3f}s"] for i, b in enumerate(preview)],
    ) + "\n"
    result_text += (
        f"\n*Next: `import_beat_markers` with beats_path=\"{json_path}\" to place "
        "markers, then `snap_to_beats` to align your cuts.*"
    )
    return srv._text_result(result_text)


# ===== TRANSCRIPT INTELLIGENCE (v0.13.1) =====

TRANSCRIBE_MAX_MEDIA = 10
TRANSCRIBE_BACKENDS = BACKENDS
_EGRESS_NOTE = (
    "- **Audio left this machine**: sent to api.elevenlabs.io (Scribe) for "
    "speakers and audio events. The local backend never does this.\n"
)

_TRANSCRIBE_INSTALL_HINT = (
    "\n\nInstall the optional transcription extra:\n\n"
    "    pip install 'fcp-mcp-server[transcribe]'\n\n"
    "or run via uvx:\n\n"
    "    uvx --from \"fcp-mcp-server[transcribe]\" fcp-mcp-server"
)


def _transcript_json_path(media_path: str) -> Path:
    p = Path(media_path)
    return p.with_name(p.stem + "_transcript.json")


def _backend_arg(arguments: dict) -> str:
    backend = arguments.get("backend", "local")
    if backend not in TRANSCRIBE_BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(TRANSCRIBE_BACKENDS)}, got {backend!r}")
    return backend


def _cache_satisfies(data: dict, backend: str) -> bool:
    """A local transcript answers a local request. A request for speakers is
    only answered by a transcript that has them."""
    return backend == "local" or is_diarized(data)


def _load_or_transcribe(
    media_path: str, model: str, language: str | None, backend: str = "local",
) -> tuple[dict | None, str]:
    """Load a cached ``_transcript.json`` for a media file, else transcribe and cache it.

    Returns ``(transcript, "")`` or ``(None, reason)``. The cache makes
    transcription a one-time cost per media file across all transcript tools.
    """
    srv = _common.tools.server_module()
    json_path = _transcript_json_path(media_path)
    if json_path.is_file():
        try:
            with open(json_path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("words"), list) \
                    and _cache_satisfies(data, backend):
                return data, ""
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass  # unreadable cache falls through to re-transcribe
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            data = ix.get_transcript(media_path)
        if data is not None and _cache_satisfies(data, backend):
            return data, ""
    result = srv.transcribe(media_path, model_size=model, language=language, backend=backend)
    if result is None:
        if backend == "elevenlabs":
            if not os.environ.get(SCRIBE_KEY_ENV, "").strip():
                return None, f"elevenlabs backend needs {SCRIBE_KEY_ENV} set in the server's environment"
            return None, "untranscribable (api.elevenlabs.io request failed or media unreadable)"
        return None, "untranscribable (faster-whisper not installed or media unreadable)"
    out_path = srv._validate_output_path(str(json_path), anchor_dir=str(Path(media_path).parent))
    with open(out_path, "w") as f:
        json.dump({"source": Path(media_path).name, **result}, f, indent=2)
    ix = _index.Index.open()
    if ix is not None:
        with ix:
            ix.put_transcript(media_path, result)
    return result, ""


def _cut_transcript_spans(modifier, clip_filter, model, language, padding, spans_fn, keep_only=False,
                          backend="local"):
    """Shared cut engine for transcript-driven editing.

    ``spans_fn(words) -> [(start, end), ...]`` in source seconds. Spans are
    padded, clamped to each clip's used source window, optionally inverted
    (keep_only), snapped to the frame grid, and cut with ripple.
    """
    fps = modifier._detect_fps()
    tick = round(2400 / fps)

    def to_frame(seconds: float) -> TimeValue:
        return TimeValue(int(round(seconds * fps) * tick), 2400)

    cache: dict[str, tuple] = {}
    cuts_made: list[tuple[str, int, float]] = []
    skipped: list[tuple[str, str]] = []
    spine_clips = [el for _, el in modifier._iter_spine_clips()]
    for el in spine_clips:
        name = el.get("name", "")
        if clip_filter and name != clip_filter:
            continue
        src = modifier.resources.get(el.get("ref", ""), {}).get("src", "")
        media_path = media_src_to_path(src)
        if not media_path or not Path(media_path).is_file():
            skipped.append((name, "media file missing"))
            continue
        if media_path not in cache:
            if len(cache) >= TRANSCRIBE_MAX_MEDIA:
                skipped.append((name, f"transcription cap reached ({TRANSCRIBE_MAX_MEDIA} media files)"))
                continue
            cache[media_path] = _load_or_transcribe(media_path, model, language, backend)
        data, reason = cache[media_path]
        if data is None:
            skipped.append((name, reason))
            continue

        clip_source_start = modifier._parse_time(el.get("start", "0s")).to_seconds()
        clip_duration = modifier._parse_time(el.get("duration", "0s")).to_seconds()
        window_start = clip_source_start
        window_end = clip_source_start + clip_duration

        spans = spans_fn(data.get("words", []))
        padded = merge_ranges([(s - padding, e + padding) for s, e in spans])
        clamped = [
            (max(s, window_start), min(e, window_end))
            for s, e in padded
            if min(e, window_end) > max(s, window_start)
        ]
        if keep_only:
            if not clamped:
                # Never delete a whole clip just because nothing matched in it.
                skipped.append((name, "no phrase matches — left untouched (keep_only)"))
                continue
            cut_source = invert_ranges(clamped, window_start, window_end)
        else:
            cut_source = clamped
        cut_ranges = [
            (to_frame(s - clip_source_start), to_frame(e - clip_source_start))
            for s, e in cut_source
        ]
        cut_ranges = [(a, b) for a, b in cut_ranges if b > a]
        if not cut_ranges:
            continue
        removed = modifier.cut_clip_ranges(el, cut_ranges)
        if removed > TimeValue.zero():
            cuts_made.append((name, len(cut_ranges), removed.to_seconds()))
    return cuts_made, skipped


def _transcript_cut_report(title, summary_lines, cuts_made, skipped, output_path, footer):
    srv = _common.tools.server_module()
    if not cuts_made:
        text = f"# {title}\n\nNo cuts to make — file unchanged (nothing saved)."
        if skipped:
            text += "\n\n## Skipped Clips\n" + srv._markdown_table(
                ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
            )
        if any("faster-whisper" in reason for _, reason in skipped):
            text += _TRANSCRIBE_INSTALL_HINT
        return srv._text_result(text)
    total_removed = sum(seconds for _, _, seconds in cuts_made)
    result = f"# {title}\n\n## Summary\n"
    result += "\n".join(summary_lines) + "\n"
    result += f"- **Clips Cut**: {len(cuts_made)}\n- **Total Removed**: {srv.format_duration(total_removed)}\n"
    result += "\n## Cuts\n"
    result += srv._markdown_table(
        ["Clip", "Ranges Cut", "Removed"],
        [[name, str(count), f"{seconds:.2f}s"] for name, count, seconds in cuts_made],
    ) + "\n"
    if skipped:
        result += "\n## Skipped Clips\n" + srv._markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    result += f"\nSaved to: {output_path}\n\n{footer}"
    return srv._text_result(result)


async def handle_transcribe_media(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)
    write_srt = bool(arguments.get("write_srt", False))
    _, tl = srv._require_timeline(arguments["filepath"])
    clip_filter = arguments.get("clip_name")

    done: dict[str, dict | None] = {}
    skipped: list[tuple[str, str]] = []
    rows: list[list[str]] = []
    srt_paths: list[str] = []
    clips = tl.media_clips()
    prog = _progress.start(total=len(clips))
    for clip in clips:
        await prog.step(f"transcribe: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path in done:
            continue
        if len(done) >= TRANSCRIBE_MAX_MEDIA:
            skipped.append((clip.name, f"transcription cap reached ({TRANSCRIBE_MAX_MEDIA} media files)"))
            continue
        data, reason = _load_or_transcribe(media_path, model, language, backend)
        done[media_path] = data
        if data is None:
            skipped.append((clip.name, reason))
            continue
        if write_srt and data.get("segments"):
            srt_path = srv._validate_output_path(
                str(Path(media_path).with_name(Path(media_path).stem + "_transcript.srt")),
                anchor_dir=str(Path(media_path).parent),
            )
            with open(srt_path, "w") as f:
                f.write(segments_to_srt(data["segments"]))
            srt_paths.append(srt_path)
        preview = data.get("text", "")[:160]
        rows.append([
            Path(media_path).name,
            data.get("language", "?"),
            str(len(data.get("words", []))),
            srv.format_duration(float(data.get("duration", 0.0))),
            preview + ("…" if len(data.get("text", "")) > 160 else ""),
        ])

    title = "ElevenLabs Scribe" if backend == "elevenlabs" else "local Whisper"
    result = f"""# Media Transcription ({title})

## Summary
- **Model**: {SCRIBE_MODEL if backend == "elevenlabs" else model}
- **Media Files Transcribed**: {len(rows)}
"""
    if backend == "elevenlabs" and rows:
        result += _EGRESS_NOTE
    if rows:
        result += "\n## Transcripts (saved as _transcript.json next to each media file)\n"
        result += srv._markdown_table(
            ["Media", "Language", "Words", "Duration", "Preview"], rows
        ) + "\n"
        result += (
            "\n*Next: `edit_by_transcript` to cut by what was said, or "
            "`remove_filler_words` to clean ums/uhs. Transcripts are cached — "
            "media is only transcribed once.*"
        )
    if srt_paths:
        result += "\n\n## SRT Files\n" + "\n".join(f"- {p}" for p in srt_paths)
    if skipped:
        result += "\n## Skipped Clips\n" + srv._markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    if not rows and any("faster-whisper" in reason for _, reason in skipped):
        result += _TRANSCRIBE_INSTALL_HINT
    return srv._text_result(result)


async def handle_edit_by_transcript(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    phrases = arguments.get("phrases") or []
    if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
        raise ValueError("phrases must be a list of strings")
    phrases = [p for p in phrases if p.strip()]
    if not phrases:
        raise ValueError("phrases must contain at least one non-empty string")
    mode = arguments.get("mode", "remove")
    if mode not in ("remove", "keep_only"):
        raise ValueError(f"mode must be 'remove' or 'keep_only', got {mode!r}")
    padding = float(arguments.get("padding", 0.0))
    if not (0 <= padding <= 2):
        raise ValueError(f"padding must be between 0 and 2 seconds, got {padding}")
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)

    filepath, output_path, modifier = srv._setup_modifier(arguments, "_transcript_edit")

    def spans_fn(words):
        return merge_ranges(
            [span for phrase in phrases for span in find_phrase_spans(words, phrase)]
        )

    cuts_made, skipped = _cut_transcript_spans(
        modifier, arguments.get("clip_name"), model, language, padding,
        spans_fn, keep_only=(mode == "keep_only"), backend=backend,
    )
    if cuts_made:
        modifier.save(output_path)
    verb = "kept only" if mode == "keep_only" else "removed"
    return _transcript_cut_report(
        "Transcript Edit",
        [f"- **Mode**: {mode} ({verb} the matched phrases)",
         f"- **Phrases**: {', '.join(repr(p) for p in phrases)}",
         f"- **Padding**: {padding}s"],
        cuts_made, skipped, output_path,
        "*Transcripts are cached as _transcript.json. Original file untouched.*",
    )


async def handle_transcript_pack(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath = arguments["filepath"]
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)
    gap = float(arguments.get("gap", _tpack.DEFAULT_GAP))
    if not 0.1 <= gap <= 5.0:
        raise ValueError("gap must be between 0.1 and 5 seconds")
    write = bool(arguments.get("write", False))
    _, tl = srv._require_timeline(filepath)
    clip_filter = arguments.get("clip_name")

    sources: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []
    clips = tl.media_clips()
    prog = _progress.start(total=len(clips))
    for clip in clips:
        await prog.step(f"pack: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path in sources:
            continue
        if len(sources) >= TRANSCRIBE_MAX_MEDIA:
            skipped.append((clip.name, f"transcription cap reached ({TRANSCRIBE_MAX_MEDIA} media files)"))
            continue
        data, reason = _load_or_transcribe(media_path, model, language, backend)
        if data is None:
            skipped.append((clip.name, reason))
            continue
        sources[media_path] = {
            "name": Path(media_path).name,
            "words": data.get("words", []),
            "events": data.get("events", []),
        }

    full = _tpack.pack(list(sources.values()), gap=gap)
    size = _tpack.pack_size(full)
    written = ""
    if write and sources:
        fp = Path(srv._validate_filepath(filepath, ('.fcpxml', '.fcpxmld')))
        out = srv._validate_output_path(
            str(fp.with_name(fp.stem + "_pack.md")), anchor_dir=str(fp.parent),
        )
        with open(out, "w", encoding="utf-8") as f:
            f.write(full)
        written = out

    result = (
        "# Transcript Pack\n\n"
        f"- **Sources**: {len(sources)}\n"
        f"- **Utterances**: {sum(1 for ln in full.splitlines() if ln.startswith('['))}\n"
        f"- **Size**: {size} bytes"
        + (f" (shown truncated to {_tpack.PACK_LIMIT_BYTES})" if size > _tpack.PACK_LIMIT_BYTES else "")
        + "\n"
    )
    if written:
        result += f"- **Written**: {written}\n"
    if backend == "elevenlabs" and sources:
        result += _EGRESS_NOTE
    if sources:
        result += "\n" + _tpack.truncate(full, limit=_tpack.PACK_LIMIT_BYTES)
    if skipped:
        result += "\n## Skipped Clips\n" + srv._markdown_table(
            ["Clip", "Reason"], [[name, reason] for name, reason in skipped]
        ) + "\n"
    if not sources and any("faster-whisper" in reason for _, reason in skipped):
        result += _TRANSCRIBE_INSTALL_HINT
    return srv._text_result(result)


async def handle_remove_filler_words(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    fillers = arguments.get("fillers") or list(DEFAULT_FILLERS)
    if not isinstance(fillers, list) or not all(isinstance(f, str) for f in fillers):
        raise ValueError("fillers must be a list of strings")
    padding = float(arguments.get("padding", 0.02))
    if not (0 <= padding <= 2):
        raise ValueError(f"padding must be between 0 and 2 seconds, got {padding}")
    model = arguments.get("model", "base")
    language = arguments.get("language")
    backend = _backend_arg(arguments)

    filepath, output_path, modifier = srv._setup_modifier(arguments, "_defillered")

    cuts_made, skipped = _cut_transcript_spans(
        modifier, arguments.get("clip_name"), model, language, padding,
        lambda words: merge_ranges(find_filler_spans(words, fillers)),
        backend=backend,
    )
    if cuts_made:
        modifier.save(output_path)
    return _transcript_cut_report(
        "Filler Word Removal",
        [f"- **Fillers**: {', '.join(fillers)}", f"- **Padding**: {padding}s"],
        cuts_made, skipped, output_path,
        "*Transcripts are cached as _transcript.json. Original file untouched.*",
    )


async def handle_detect_silence_candidates(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath = srv._validate_filepath(arguments["filepath"], ('.fcpxml', '.fcpxmld'))
    modifier = FCPXMLModifier(filepath)
    candidates = modifier.detect_silence_candidates(
        min_gap_seconds=arguments.get("min_gap_seconds", 0.5),
        patterns=arguments.get("patterns"),
    )

    if not candidates:
        return srv._text_result("No silence candidates detected.")

    result = f"# Silence Candidates Detected\n\n**Found**: {len(candidates)}\n\n"
    result += "| # | Timecode | Duration | Reason | Confidence | Clip |\n"
    result += "|---|----------|----------|--------|------------|------|\n"
    for i, c in enumerate(candidates, 1):
        result += (
            f"| {i} | {c['start_timecode']} | {srv.format_duration(c['duration_seconds'])} | "
            f"{c['reason']} | {c['confidence']:.0%} | {c.get('clip_name') or '-'} |\n"
        )
    result += (
        "\n**Note**: Detection uses timeline heuristics (gaps, ultra-short clips, name patterns). "
        "Review candidates before removing — some may be intentional."
    )
    return srv._text_result(result)


async def handle_remove_silence_candidates(arguments: dict) -> Sequence[TextContent]:
    srv = _common.tools.server_module()
    filepath, output_path, modifier = srv._setup_modifier(arguments, "_silence_cleaned")
    actions = modifier.remove_silence_candidates(
        mode=arguments.get("mode", "mark"),
        min_gap_seconds=arguments.get("min_gap_seconds", 0.5),
        min_confidence=arguments.get("min_confidence", 0.7),
    )
    modifier.save(output_path)

    if not actions:
        return srv._text_result("No silence candidates met the confidence threshold.")

    mode = arguments.get("mode", "mark")
    result = f"# Silence Candidates {'Marked' if mode == 'mark' else 'Removed'}\n\n"
    result += f"**Actions taken**: {len(actions)}\n\n"
    for a in actions:
        result += f"- **{a['action']}** {a.get('clip_name', 'gap')} ({a['reason']})\n"
    result += f"\nSaved to: `{output_path}`"
    return srv._text_result(result)
