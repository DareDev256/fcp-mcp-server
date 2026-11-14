"""The scenes tool group — shot boundaries as a first-class analysis.

``detect_scenes`` reads each source once (through the index) and reports cuts
in timeline time. ``scenes_to_markers`` and ``scenes_split`` turn the same
cuts into markers or into separate clips, which is how a long take becomes
something an editor can rearrange by hand or a selects reel can draw from.
"""

from fractions import Fraction
from pathlib import Path

from fcpxml import index as _index
from fcpxml import progress as _progress
from fcpxml.media_intel import file_in_point, media_src_to_path
from fcpxml.scenes import BACKENDS, backends_available, detect_scenes
from fcpxml.words import plural
from tools import _common
from tools._common import parse_project, text_result

MAX_SCENE_MEDIA = 100

_INSTALL = (
    "\n\nFor better cuts on real footage install the scenes extra:\n\n"
    "    pip install 'fcp-mcp-server[scenes]'\n"
)


def scenes_cached(media_path: str, backend: str, threshold, min_scene_len: float):
    """``detect_scenes`` through the index. Same answer with the index off."""
    kind = f"scene@{backend}/{threshold}/{min_scene_len}"
    ix = _index.Index.open()
    if ix is None:
        return detect_scenes(media_path, backend=backend, threshold=threshold, min_scene_len=min_scene_len)
    with ix:
        rows = ix.get_analysis(media_path, kind)
        if rows is not None and rows:
            scenes = [(float(r["start"]), float(r["end"])) for r in rows]
            return {
                "backend": rows[0]["payload"]["backend"],
                "cuts": [s for s, _ in scenes[1:]],
                "scenes": scenes,
                "duration": scenes[-1][1],
                # the score of the cut that STARTS each scene (none for the first)
                "scores": [r["payload"].get("score") for r in rows[1:]],
                "threshold": rows[0]["payload"].get("threshold"),
            }
        result = detect_scenes(media_path, backend=backend, threshold=threshold, min_scene_len=min_scene_len)
        if result is not None and result["scenes"]:
            scores = [None] + list(result.get("scores") or [None] * (len(result["scenes"]) - 1))
            ix.put_analysis(
                media_path, kind,
                [{"start": a, "end": b, "payload": {"backend": result["backend"], "score": sc,
                                                    "threshold": result.get("threshold")}}
                 for (a, b), sc in zip(result["scenes"], scores)],
            )
        return result


def _args(args: dict):
    backend = args.get("backend", "auto")
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}, got {backend!r}")
    threshold = args.get("threshold")
    threshold = float(threshold) if threshold is not None else None
    min_scene_len = float(args.get("min_scene_len", 0.5))
    if not (0.0 < min_scene_len <= 3600):
        raise ValueError(f"min_scene_len must be between 0 and 3600 seconds, got {min_scene_len}")
    return backend, threshold, min_scene_len


async def _cuts_per_clip(tl, args: dict, details: dict | None = None):
    """[(clip, source_cuts_inside_used_window, backend)] plus skipped rows;
    ``details`` (when given) receives each clip's full detector result
    (scores, threshold) keyed by ``id(clip)``."""
    backend, threshold, min_scene_len = _args(args)
    clip_filter = args.get("clip_name")
    cache: dict = {}
    details = details if details is not None else {}
    found: list[tuple] = []
    skipped: list[tuple[str, str]] = []
    clips = tl.media_clips()
    prog = _progress.start(total=len(clips))
    for clip in clips:
        await prog.step(f"scenes: {clip.name}")
        if clip_filter and clip.name != clip_filter:
            continue
        media_path = media_src_to_path(clip.media_path or "")
        if not media_path or not Path(media_path).is_file():
            skipped.append((clip.name, "media file missing"))
            continue
        if media_path not in cache:
            if len(cache) >= MAX_SCENE_MEDIA:
                skipped.append((clip.name, f"probe cap reached ({plural(MAX_SCENE_MEDIA, 'media file')})"))
                continue
            cache[media_path] = scenes_cached(media_path, backend, threshold, min_scene_len)
        result = cache[media_path]
        if result is None:
            skipped.append((clip.name, "unanalyzable (no scene backend could read the media)"))
            continue
        src_start = file_in_point(clip)
        src_end = src_start + Fraction(clip.duration._exact_seconds)
        inside = [c for c in result["cuts"] if src_start < Fraction(c).limit_denominator(1_000_000) < src_end]
        found.append((clip, inside, result["backend"]))
        details[id(clip)] = result
    return found, skipped


def _timeline_seconds(clip, source_cut: float) -> Fraction:
    src_start = file_in_point(clip)
    return Fraction(clip.start._exact_seconds) + (Fraction(source_cut).limit_denominator(1_000_000) - src_start)


def _fmt(seconds: Fraction) -> str:
    return f"{float(seconds):.3f}s"


def _report_header(title: str, found, skipped) -> str:
    srv = _common.tools.server_module()
    backends = sorted({b for _, _, b in found})
    total_cuts = sum(len(c) for _, c, _ in found)
    out = f"# {title}\n\n- **Clips analysed**: {len(found)}\n- **Cuts found**: {total_cuts}\n"
    out += f"- **Backend**: {', '.join(backends) if backends else 'none'}\n"
    if skipped:
        out += "\n## Skipped Clips\n" + srv._markdown_table(
            ["Clip", "Reason"], [[n, r] for n, r in skipped]
        ) + "\n"
    if backends == ["ffmpeg"]:
        out += _INSTALL
    return out


async def handle_detect_scenes(args: dict):
    _, tl = parse_project(args["filepath"])
    details: dict = {}
    found, skipped = await _cuts_per_clip(tl, args, details)
    srv = _common.tools.server_module()
    out = _report_header("Scene Detection", found, skipped)
    rows, strip = [], []
    scenes_seen, threshold, backend_used = [], None, ""
    for clip, cuts, backend in found:
        result = details.get(id(clip)) or {}
        scores = dict(zip(result.get("cuts") or [], result.get("scores") or []))
        threshold = result.get("threshold", threshold)
        backend_used = backend
        src0 = file_in_point(clip)
        src1 = src0 + Fraction(clip.duration._exact_seconds)
        bounds = [src0] + [Fraction(c).limit_denominator(1_000_000) for c in cuts] + [src1]
        rec0 = _timeline_seconds(clip, float(src0))
        origin0 = Fraction(str(getattr(tl, "origin_seconds", 0) or 0)).limit_denominator(1_000_000)
        scenes_seen += [(rec0 - origin0 + (a - src0), rec0 - origin0 + (b - src0)) for a, b in zip(bounds, bounds[1:])]
        for c in cuts:
            # Shown on the asset's clock, as Final Cut shows source timecode;
            # matched and placed in file time (media_intel.file_in_point).
            asset_start = Fraction(clip.source_start._exact_seconds) if clip.source_start else Fraction(0)
            shown = Fraction(c).limit_denominator(1_000_000) + (asset_start - file_in_point(clip))
            rows.append([clip.name, f"{float(shown):.3f}s", _fmt(_timeline_seconds(clip, c))])
            # the strip's timecode is the timeline's from its first frame, as
            # the contact sheet and the app show it (an 01:00:00:00 start removed)
            origin = Fraction(str(getattr(tl, "origin_seconds", 0) or 0)).limit_denominator(1_000_000)
            strip.append((clip.name, media_src_to_path(clip.media_path or ""),
                          Fraction(c).limit_denominator(1_000_000), _timeline_seconds(clip, c) - origin,
                          scores.get(c)))
    pictures = []
    if rows:
        out += "\n## Cuts\n" + srv._markdown_table(["Clip", "Source time", "Timeline time"], rows) + "\n"
        # 0.32: the frame before and the frame after every cut, labelled, so
        # a cut can be judged from a picture, not only from its time.
        from fcpxml import proofs, render
        from tools.preview import IMAGE_BLOCKS_MAX, _with_images

        sheet = proofs.scene_strip(strip, render.cache_dir(), getattr(tl, "frame_rate", 24) or 24,
                                   scenes=scenes_seen, threshold=threshold, backend=backend_used)
        if sheet["pages"]:
            out += "\n## Before and after each cut\n" + "\n".join(f"- {p}" for p in sheet["pages"]) + "\n"
            pictures = sheet["rasters"][:IMAGE_BLOCKS_MAX]
        out += "\n*Next: `scenes_to_markers` to mark them, or `scenes_split` to cut the clips there.*"
    if not found and not skipped:
        avail = backends_available()
        out += "\nNo clips with reachable media. Backends: " + ", ".join(
            f"{k}={'yes' if v else 'no'}" for k, v in avail.items()
        )
    if pictures:
        return _with_images(out, pictures)
    return text_result(out)


async def handle_scenes_to_markers(args: dict):
    srv = _common.tools.server_module()
    _, tl = parse_project(args["filepath"])
    found, skipped = await _cuts_per_clip(tl, args)
    filepath, output_path, modifier = srv._setup_modifier(args, suffix="_scenes")
    markers = []
    for clip, cuts, _ in found:
        for i, c in enumerate(cuts, 1):
            t = _timeline_seconds(clip, c)
            markers.append({
                "timecode": f"{t.numerator}/{t.denominator}s",
                "name": f"{clip.name} cut {i}",
            })
    added = modifier.batch_add_markers(markers=markers) if markers else []
    modifier.save(output_path)
    out = _report_header("Scene Markers", found, skipped)
    out += f"\nAdded {plural(len(added), 'marker')}.\n\nSaved to: {output_path}"
    return text_result(out)


async def handle_scenes_split(args: dict):
    srv = _common.tools.server_module()
    _, tl = parse_project(args["filepath"])
    found, skipped = await _cuts_per_clip(tl, args)
    filepath, output_path, modifier = srv._setup_modifier(args, suffix="_scenes")
    pieces = 0
    split_clips = 0
    for clip, cuts, _ in found:
        if not cuts:
            continue
        src_start = file_in_point(clip)
        points = []
        for c in cuts:
            rel = Fraction(c).limit_denominator(1_000_000) - src_start
            points.append(f"{rel.numerator}/{rel.denominator}s")
        new_clips = modifier.split_clip(clip_id=clip.name, split_points=points)
        pieces += len(new_clips)
        split_clips += 1
    modifier.save(output_path)
    out = _report_header("Scene Split", found, skipped)
    out += f"\nSplit {plural(split_clips, 'clip')} into {plural(pieces, 'piece')}.\n\nSaved to: {output_path}"
    return text_result(out)


ACTIONS = {
    "detect_scenes": handle_detect_scenes,
    "scenes_to_markers": handle_scenes_to_markers,
    "scenes_split": handle_scenes_split,
}

DESCRIPTION = (
    "Shot boundaries. detect_scenes reads every source in a timeline and "
    "reports cuts in timeline time (args: filepath, clip_name?, backend "
    "auto|content|adaptive|ffmpeg, threshold?, min_scene_len=0.5); "
    "scenes_to_markers writes a marker at each cut; scenes_split cuts the "
    "clips there. PySceneDetect via the scenes extra, ffmpeg without it — "
    "the result names which one answered."
)
