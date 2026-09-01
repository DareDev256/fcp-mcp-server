"""Author FCPXML from a video-use style edl.json cut list.

browser-use/video-use reasons over footage and its pipeline terminates at a
flat mp4 — it never touches an NLE. This is the bridge that lets that reasoning
finish in Final Cut Pro instead, which is the only outcome that works for
anyone delivering a project file rather than a video.

The schema is read off that repo, not assumed:

    {"sources": {"name": "path.mp4", ...},
     "ranges":  [{"source": "name", "start": 0, "end": 1}, ...],
     "grade":   "auto" | preset | raw filter   (optional)}

``ranges[].source`` is a KEY into ``sources``, not a path. Relative paths
resolve against the edl.json's own directory, matching render.py's
resolve_path().

Authoring follows fcpxml/templates.py, which already builds a valid FCPXML from
raw media paths with no source project. rough_cut cannot be reused here: it
indexes clips out of an existing FCPXML, and an EDL has none.
"""

import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

from fcpxml.models import TimeValue
from fcpxml.rational import frame_duration_attr, nominal_fps
from fcpxml.writer import _create_asset_element, write_fcpxml


class EDLValidationError(ValueError):
    """The cut list is malformed. The message names the offending range."""


def _fraction(value: Any, index: int, field: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EDLValidationError(
            f"range {index}: {field} is not a number (got {value!r})"
        )
    try:
        return Fraction(str(value)).limit_denominator(1000000)
    except (ValueError, TypeError, ZeroDivisionError):
        raise EDLValidationError(
            f"range {index}: {field} is not a number (got {value!r})"
        ) from None


def _resolve(maybe_path: str, base: Path) -> str:
    """Absolute paths pass through; relative ones resolve against *base*."""
    path = Path(maybe_path)
    return str(path if path.is_absolute() else (base / path).resolve())


def parse_edl(data: dict, base_dir: str = ".") -> list[dict]:
    """Validate and normalize an edl.json payload into ordered cuts.

    Validation is strict and reports by range index. An EDL is machine
    generated, so a malformed one is a bug upstream — and a silently dropped
    range is a missing shot nobody notices until the review.
    """
    if not isinstance(data, dict):
        raise EDLValidationError("edl.json must be an object")
    sources = data.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise EDLValidationError(
            "edl.json must contain a non-empty 'sources' map of name -> path"
        )
    raw_ranges = data.get("ranges")
    if not isinstance(raw_ranges, list):
        raise EDLValidationError("edl.json must contain a 'ranges' list")
    if not raw_ranges:
        raise EDLValidationError("edl.json 'ranges' must hold at least one range")

    base = Path(base_dir)
    cuts: list[dict] = []
    for index, item in enumerate(raw_ranges):
        if not isinstance(item, dict):
            raise EDLValidationError(f"range {index}: expected an object")
        name = item.get("source")
        if name not in sources:
            raise EDLValidationError(
                f"range {index}: unknown source {name!r} — "
                f"known sources are {', '.join(sorted(sources))}"
            )
        src_in = _fraction(item.get("start", 0), index, "start")
        src_out = _fraction(item.get("end", 0), index, "end")
        if src_out <= src_in:
            raise EDLValidationError(
                f"range {index}: end ({src_out}) must be after start ({src_in})"
            )
        cuts.append({
            "source": _resolve(str(sources[name]), base),
            "src_in": src_in,
            "src_out": src_out,
            "label": str(name),
        })
    return cuts


def edl_to_fcpxml(
    data: dict,
    out_path: str,
    base_dir: Optional[str] = None,
    fps: float = 24.0,
    name: str = "EDL Import",
    width: int = 1920,
    height: int = 1080,
) -> dict:
    """Write an FCPXML timeline from an edl.json payload.

    Returns a dict with ``path``, ``clips``, ``missing`` (media that is not on
    this machine) and ``ignored`` (fields the FCPXML round-trip cannot carry).
    Both lists are reported rather than dropped: an operator who believes their
    grade came across, or that a relinked clip is present, finds out in Final
    Cut Pro otherwise.
    """
    if base_dir is None:
        base_dir = str(Path(out_path).parent)
    cuts = parse_edl(data, base_dir=base_dir)

    root = ET.Element('fcpxml', version='1.13')
    resources = ET.SubElement(root, 'resources')
    format_id = 'r1'
    ET.SubElement(
        resources, 'format', id=format_id,
        name=f"FFVideoFormat{height}p{nominal_fps(fps)}",
        frameDuration=frame_duration_attr(fps),
        width=str(width), height=str(height),
    )

    # One asset per distinct source, long enough for the furthest range that
    # uses it — an asset shorter than a clip drawn from it is invalid FCPXML.
    furthest: dict[str, Fraction] = {}
    for cut in cuts:
        current = furthest.get(cut["source"], Fraction(0))
        furthest[cut["source"]] = max(current, cut["src_out"])

    asset_ids: dict[str, str] = {}
    missing: list[str] = []
    for counter, (source, end) in enumerate(furthest.items(), start=2):
        asset_id = f"r{counter}"
        asset_ids[source] = asset_id
        if not Path(source).is_file():
            missing.append(source)
        _create_asset_element(
            resources, asset_id, Path(source).stem, f"file://{source}",
            duration=TimeValue.from_seconds(float(end), fps).to_fcpxml(),
        )

    library = ET.SubElement(
        root, 'library', location="file:///Users/editor/Movies/EDLImport.fcpbundle/"
    )
    event = ET.SubElement(library, 'event', name=name, uid=str(uuid.uuid4()).upper())
    project = ET.SubElement(
        event, 'project', name=name, uid=str(uuid.uuid4()).upper(),
        modDate=datetime.now().strftime("%Y-%m-%d %H:%M:%S -0500"),
    )

    total = TimeValue.zero()
    durations = []
    for cut in cuts:
        duration = TimeValue.from_seconds(float(cut["src_out"] - cut["src_in"]), fps)
        durations.append(duration)
        total = total + duration

    sequence = ET.SubElement(
        project, 'sequence', format=format_id, duration=total.to_fcpxml(),
        tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k",
    )
    spine = ET.SubElement(sequence, 'spine')

    offset = TimeValue.zero()
    for cut, duration in zip(cuts, durations):
        ET.SubElement(
            spine, 'asset-clip',
            ref=asset_ids[cut["source"]],
            offset=offset.to_fcpxml(),
            name=cut["label"],
            start=TimeValue.from_seconds(float(cut["src_in"]), fps).to_fcpxml(),
            duration=duration.to_fcpxml(),
            format=format_id, tcFormat="NDF",
        )
        offset = offset + duration

    write_fcpxml(root, out_path)

    ignored: list[str] = []
    if data.get("grade"):
        ignored.append(
            f"'grade' ({data['grade']!r}) was ignored: FCPXML carries the "
            "original media and colour is a call made inside Final Cut Pro."
        )

    return {
        "path": out_path,
        "clips": len(cuts),
        "missing": missing,
        "ignored": ignored,
    }
