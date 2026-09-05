"""Live mode v1 — officially-supported control of a running Final Cut Pro.

This module is the first piece of the dual-mode (XML + Live) architecture
(see docs/CAPABILITY-AUDIT-2026-06.md).  Everything here rides Apple's
sanctioned surfaces only — no injection, no private APIs, no accessibility
scripting:

- **push_to_fcp** — programmatic FCPXML *import* via the Open Document
  Apple event, Apple's documented zero-click ingestion path.  Behavior is
  steered by an ``<import-options>`` element injected into the document
  (library location, suppress warnings, copy assets).

Live-verified findings that shape the contract:

1. **An import with no library location does nothing, silently.**
   *Re-verified FCP 12.3, 2026-09-05; this replaces the 12.2 finding, which
   said a missing location raises a modal "Open Library" picker that blocks
   the Apple event.* On 12.3 there is no picker and no block: osascript
   returns 0, FCP stays exactly as it was, and nothing is imported. The
   12.2 behaviour cannot be relied on, so the contract no longer assumes a
   human will be asked. ``push_to_fcp`` resolves a real target up front via
   ``resolve_library_target`` and refuses when it cannot, rather than
   sending a document with nowhere to land.

   Given a ``.fcpbundle`` path that does not exist, FCP still silently
   creates the library plus a dated event and imports (12.3, confirmed) —
   which is why library creation stays an explicit argument and is never a
   default. ``inject_import_options`` normalises the location to
   ``.fcpbundle``; a bare path or ``.fcplibrary`` does not auto-create.
2. **Media-identity collisions** — importing a project whose media already
   exists in the target library fails with "the media already exists with a
   unique identifier".  Push into a fresh library, or reuse the exact asset
   IDs FCP already holds.
- **list_fcp_libraries** — FCP 12's AppleScript dictionary is read-only
  library inspection (suite ``com.apple.FinalCut.library.inspection``);
  we use it to enumerate open libraries → events → projects.

The asymmetry is structural: import is scriptable, but Apple offers NO
programmatic export — reading back the user's current timeline still
requires a manual File > Export XML.  Live mode therefore *pushes*;
round-trips come back through the XML tools.

macOS notes: ``osascript`` targeting Final Cut Pro requires the host
process to hold an Apple Events automation grant (System Settings →
Privacy & Security → Automation) — the first call triggers the consent
prompt.  ``tell application "Final Cut Pro"`` launches FCP if it is not
already running; ``list_fcp_libraries`` checks first and declines to
launch, while ``push_to_fcp`` launching FCP is the point.
"""

import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_OSASCRIPT_TIMEOUT_SECONDS = 120  # FCP cold-launch + import can be slow
_FCP_BUNDLE_ID = "com.apple.FinalCut"

# Field separators for AppleScript list output — ASCII unit/record
# separators cannot appear in user-facing library/project names.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def _applescript_quote(value: str) -> str:
    """Escape a string for embedding in a double-quoted AppleScript literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=_OSASCRIPT_TIMEOUT_SECONDS,
    )


def fcp_is_running() -> bool:
    """True when a Final Cut Pro process is active (no launch side-effect)."""
    proc = subprocess.run(
        ["pgrep", "-x", "Final Cut Pro"], capture_output=True, text=True
    )
    return proc.returncode == 0


def inject_import_options(
    fcpxml_path: str,
    output_path: str,
    library_location: Optional[str] = None,
    suppress_warnings: bool = True,
    copy_assets: Optional[bool] = None,
) -> str:
    """Write a copy of *fcpxml_path* with an ``<import-options>`` element.

    The DTD requires ``import-options`` as the FIRST child of ``<fcpxml>``
    (``<!ELEMENT fcpxml (import-options?, resources?, ...)>``).  Any
    existing import-options element is replaced.

    Args:
        fcpxml_path: Source ``.fcpxml`` document.
        output_path: Where to write the import-ready copy.
        library_location: Path or ``file://`` URL of the target library
            (FCP creates the library if none exists there).
        suppress_warnings: Suppress non-fatal import warning dialogs.
        copy_assets: True = copy media into the library, False = link
            in place, None = let FCP use its default.

    Returns:
        *output_path*.
    """
    from .safe_xml import safe_parse
    from .writer import write_fcpxml

    tree = safe_parse(fcpxml_path)
    root = tree.getroot()

    for stale in root.findall('import-options'):
        root.remove(stale)

    options = ET.Element('import-options')
    if library_location:
        loc = library_location
        if not loc.startswith('file://'):
            from urllib.parse import quote
            resolved = Path(loc).expanduser()
            # FCP auto-creates a library ONLY when the location carries the
            # .fcpbundle extension (12.2, still true on 12.3). A bare
            # directory or a .fcplibrary path does not auto-create; on 12.3
            # that is a silent no-op rather than the modal picker 12.2
            # raised. Normalise to .fcpbundle.
            if resolved.suffix.lower() != '.fcpbundle':
                resolved = resolved.with_suffix('.fcpbundle')
            loc = 'file://' + quote(str(resolved.resolve()))
        ET.SubElement(options, 'option', key='library location', value=loc)
    ET.SubElement(
        options, 'option',
        key='suppress warnings', value='1' if suppress_warnings else '0',
    )
    if copy_assets is not None:
        ET.SubElement(
            options, 'option',
            key='copy assets', value='1' if copy_assets else '0',
        )
    root.insert(0, options)

    write_fcpxml(root, output_path)
    return output_path



class ImportNotObservedError(RuntimeError):
    """The Apple event was delivered but the import was never seen to land.

    Distinct from a delivery failure: osascript returned 0, FCP accepted the
    Open Document event, and the projects still did not appear in the target
    library. This is the failure that used to be reported as success.
    """


def _projects_in(fcpxml_path: str) -> List[str]:
    """Every ``<project name=...>`` in a document, in order."""
    from .safe_xml import safe_parse

    root = safe_parse(fcpxml_path).getroot()
    return [p.get("name", "") for p in root.iter("project") if p.get("name")]


def _same_bundle(a: str, b: str) -> bool:
    """Compare two library paths ignoring trailing slash and case."""
    if not a or not b:
        return False
    return str(Path(a)).rstrip("/").lower() == str(Path(b)).rstrip("/").lower()


def resolve_library_target(library_location: Optional[str] = None) -> str:
    """Decide which library an import should land in.

    Order:
      1. An explicit *library_location* — always wins, and is the ONLY way to
         have FCP create a library that does not exist yet.
      2. Exactly one open library in a running FCP — use its own path.
      3. Refuse, naming the open libraries and the argument to pass.

    Never launches FCP, and never invents a ``~/Movies/<name>.fcpbundle``
    convenience default: creating a library in someone's Movies folder is a
    side effect they did not ask for, and doing it on every push would leave
    an editor with a folder full of libraries they never opened.

    Raises:
        RuntimeError: when no target can be resolved. The message carries the
            open libraries and the exact argument that would disambiguate.
    """
    if library_location:
        return library_location

    if not fcp_is_running():
        raise RuntimeError(
            "No library_location given and Final Cut Pro is not running, so "
            "there is no open library to infer one from. Pass "
            "library_location=/path/to/Your.fcpbundle (a path that does not "
            "exist yet will be created by FCP)."
        )

    libs = [lib for lib in list_fcp_libraries() if lib.get("file")]
    if len(libs) == 1:
        return libs[0]["file"]

    if not libs:
        raise RuntimeError(
            "Final Cut Pro is running but reports no open library with a "
            "resolvable path. Pass library_location=/path/to/Your.fcpbundle."
        )

    names = "\n".join(f"  - {lib['name']}  →  {lib['file']}" for lib in libs)
    raise RuntimeError(
        f"{len(libs)} libraries are open and none was named, so the import "
        f"target is ambiguous. AppleScript orders `libraries` by internal "
        f"application order, not by which one is frontmost, so guessing here "
        f"would pick the wrong library some of the time. Pass one of:\n"
        f"{names}\n"
        f"as library_location."
    )


def _library_contains(libraries: List[Dict[str, Any]], target: str,
                      projects: Sequence[str]) -> bool:
    """True when *target* holds every one of *projects*."""
    for lib in libraries:
        if not _same_bundle(lib.get("file", ""), target):
            continue
        present = {
            name
            for event in lib.get("events", [])
            for name in event.get("projects", [])
        }
        return all(p in present for p in projects)
    return False


def push_to_fcp(
    fcpxml_path: str,
    library_location: Optional[str] = None,
    suppress_warnings: bool = True,
    copy_assets: Optional[bool] = None,
    import_copy_path: Optional[str] = None,
    verify: bool = True,
    verify_timeout: float = 60.0,
) -> Dict[str, Any]:
    """Send an FCPXML document to Final Cut Pro via the Open Document event.

    This is Apple's documented programmatic-import path: FCP ingests the
    document without any clicks, creating libraries/events as directed by
    ``<import-options>``.  Launches FCP when it isn't running.

    Args:
        fcpxml_path: ``.fcpxml`` file or ``.fcpxmld`` bundle to import.
        library_location: Target library path/URL (created if absent).
        suppress_warnings: Suppress non-fatal import warning dialogs.
        copy_assets: Copy media into the library vs. link in place.
        import_copy_path: Where to write the options-injected copy for
            flat files (defaults handled by the caller; required when
            options are used on a flat file).

    Returns:
        Dict with ``sent`` (path actually opened), ``launched_fcp``
        (whether FCP was started by this call), and ``stdout``.

    Raises:
        RuntimeError: When osascript fails (most commonly a missing
            Automation permission grant for the host process).
    """
    path = Path(fcpxml_path)
    send_path = path

    # Resolve the target BEFORE anything is sent. Until v0.25.0 an absent
    # library_location meant no `library location` option was written at all,
    # so FCP received a document with no import target, did nothing, and this
    # function still returned success.
    target = resolve_library_target(library_location)

    # Bundles: open directly (option injection inside a copied bundle is
    # a v0.10 refinement); flat files get an import-ready copy so the
    # user's original is never touched.
    if path.suffix.lower() != '.fcpxmld' and import_copy_path:
        send_path = Path(
            inject_import_options(
                str(path),
                import_copy_path,
                library_location=target,
                suppress_warnings=suppress_warnings,
                copy_assets=copy_assets,
            )
        )

    # What must show up in the target for this to have worked.
    try:
        expected_projects = _projects_in(str(send_path))
    except Exception:
        expected_projects = []

    before = []
    preexisting = []
    if verify and fcp_is_running():
        try:
            before = list_fcp_libraries()
        except RuntimeError:
            before = []
        preexisting = [
            name for name in expected_projects
            if _library_contains(before, target, [name])
        ]

    was_running = fcp_is_running()
    script = (
        'tell application "Final Cut Pro"\n'
        'activate\n'
        f'open POSIX file "{_applescript_quote(str(send_path.resolve()))}"\n'
        'end tell'
    )
    proc = _run_osascript(script)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        hint = ""
        if "-1743" in stderr or "not allowed" in stderr.lower():
            hint = (
                " — grant Automation permission: System Settings → "
                "Privacy & Security → Automation → allow your terminal/MCP "
                "host to control Final Cut Pro, then retry"
            )
        raise RuntimeError(f"osascript failed: {stderr}{hint}")

    result: Dict[str, Any] = {
        "sent": str(send_path),
        "library_location": target,
        "launched_fcp": not was_running,
        "stdout": proc.stdout.strip(),
        "expected_projects": expected_projects,
    }

    # osascript returning 0 means the Apple event was DELIVERED. It says
    # nothing about whether FCP imported anything. The only reading that
    # separates "imported" from "silently did nothing" is whether the
    # project names now appear in the target library.
    if not verify:
        result["verified"] = None
        return result

    if not expected_projects:
        result["verified"] = None
        result["verification_note"] = (
            "no <project> elements in the document, so there is no name to "
            "watch for; import was not verified"
        )
        return result

    if preexisting:
        result["verified"] = None
        result["verification_note"] = (
            "project name(s) "
            + ", ".join(repr(n) for n in preexisting)
            + f" already existed in {target} before this push, so their "
            "presence afterwards proves nothing; import was not verified"
        )
        return result

    deadline = time.monotonic() + verify_timeout
    while True:
        try:
            after = list_fcp_libraries()
        except RuntimeError:
            after = []
        if _library_contains(after, target, expected_projects):
            result["verified"] = True
            return result
        if time.monotonic() >= deadline:
            raise ImportNotObservedError(
                f"Final Cut Pro accepted the document but "
                f"{', '.join(repr(n) for n in expected_projects)} was never "
                f"observed in {target} within {verify_timeout:.0f}s. The "
                f"Apple event was delivered; the import was not seen to land. "
                f"Common causes: a media-identity collision (the same media "
                f"already exists in that library under a different id), or a "
                f"modal dialog waiting in FCP.\n"
                f"Sent: {send_path}\n"
                f"Libraries before: {[lib.get('name') for lib in before]}\n"
                f"Libraries now:    {[lib.get('name') for lib in after]}"
            )
        time.sleep(2.0)


def list_fcp_libraries(allow_launch: bool = False) -> List[Dict[str, Any]]:
    """Enumerate open libraries → events → projects via AppleScript.

    Uses FCP 12's read-only scripting dictionary.  By default this
    refuses to launch FCP (``tell application`` would start it);
    pass ``allow_launch=True`` to override.

    Returns:
        List of ``{name, file, events: [{name, projects: [str, ...]}, ...]}``.
        ``file`` is the library bundle's POSIX path, or "" when FCP
        declines to answer for that library.

    Raises:
        RuntimeError: FCP not running (and *allow_launch* False), or
            osascript failure.
    """
    if not allow_launch and not fcp_is_running():
        raise RuntimeError(
            "Final Cut Pro is not running (pass allow_launch=true to start it)"
        )

    # `POSIX path of (file of lib)` raises -1700 on FCP 12.3: `file of lib`
    # answers an HFS alias, not something POSIX path accepts directly. Coerce
    # `as text` first. `out` is also declared OUTSIDE the tell block — inside
    # it, FCP resolves `out` against its own scripting terms and the repeat
    # silently yields nothing.
    script = (
        'set fieldSep to (ASCII character 31)\n'
        'set recSep to (ASCII character 30)\n'
        'set out to ""\n'
        'tell application "Final Cut Pro"\n'
        '  repeat with lib in libraries\n'
        '    set libName to name of lib\n'
        '    set libPath to ""\n'
        '    try\n'
        '      set libPath to POSIX path of ((file of lib) as text)\n'
        '    end try\n'
        '    repeat with evt in (events of lib)\n'
        '      set evtName to name of evt\n'
        '      set projNames to ""\n'
        '      repeat with proj in (projects of evt)\n'
        '        set projNames to projNames & (name of proj) & fieldSep\n'
        '      end repeat\n'
        '      set out to out & libName & fieldSep & libPath & fieldSep '
        '& evtName & fieldSep & projNames & recSep\n'
        '    end repeat\n'
        '    if (count of events of lib) is 0 then\n'
        '      set out to out & libName & fieldSep & libPath & fieldSep & recSep\n'
        '    end if\n'
        '  end repeat\n'
        'end tell\n'
        'return out'
    )
    proc = _run_osascript(script)
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {proc.stderr.strip()}")

    libraries: Dict[str, Dict[str, Any]] = {}
    for record in proc.stdout.strip().split(_RECORD_SEP):
        record = record.strip('\n')
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        lib_name = fields[0]
        lib_path = fields[1] if len(fields) > 1 else ""
        lib = libraries.setdefault(
            lib_name, {"name": lib_name, "file": lib_path, "events": []}
        )
        if not lib.get("file") and lib_path:
            lib["file"] = lib_path
        if len(fields) >= 3 and fields[2]:
            projects = [p for p in fields[3:] if p]
            lib["events"].append({"name": fields[2], "projects": projects})
    return list(libraries.values())
