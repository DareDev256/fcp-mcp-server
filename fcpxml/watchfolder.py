"""Detect an FCPXML export landing in a directory.

Apple ships a fully scriptable import (odoc + <import-options>) and no
programmatic export, verified unchanged across FCP 11.0 to 12.2. This module is
how one Cmd-E becomes something the server notices, closing the loop without
touching an unofficial surface.

Deliberately stat-and-hash polling rather than a watchdog observer. Polling a
directory once a second costs nothing next to an FCPXML parse, adds no
dependency, and cannot miss an event during observer setup.

The snapshot digests CONTENT, not just (mtime, size). Re-exporting over the
same filename is the normal iteration loop, and two exports of the same byte
count inside one filesystem timestamp tick produce an identical stat pair — so
a stat-only watcher silently reports "no export" for the change the operator
just made. The thing that changed is the content, so content is what is read.
"""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WATCH_EXTENSIONS = (".fcpxml", ".fcpxmld")

# Past this, digest the stat pair instead of the bytes. An FCPXML this large is
# not a thing FCP produces; the cap exists so a stray huge file in the watch
# folder cannot stall the poll loop.
MAX_DIGEST_BYTES = 64 * 1024 * 1024


def default_watch_dir() -> Optional[str]:
    """The configured export destination, or None when unset."""
    value = os.environ.get("FCP_WATCH_DIR", "").strip()
    return value or None


def _digest_file(path: Path, digest: "hashlib._Hash") -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > MAX_DIGEST_BYTES:
        digest.update(f"{size}:{path.stat().st_mtime}".encode())
        return
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(block)
    except OSError:
        return


def _fingerprint(entry: Path) -> str:
    """A content fingerprint for a .fcpxml file or a .fcpxmld bundle."""
    digest = hashlib.sha256()
    if entry.is_dir():
        # A bundle. Its own mtime moves when FCP rewrites it, but the sidecars
        # are the payload, so walk them in a stable order.
        for child in sorted(entry.rglob("*")):
            if child.is_file():
                digest.update(str(child.relative_to(entry)).encode())
                _digest_file(child, digest)
    else:
        _digest_file(entry, digest)
    return digest.hexdigest()


class Watcher:
    """Snapshot a directory, then report what changed since the snapshot."""

    def __init__(self, directory: str):
        self.directory = Path(directory)
        self._snapshot: dict[str, str] = {}
        self._baselined = False

    def _scan(self) -> dict[str, str]:
        if not self.directory.is_dir():
            raise ValueError(f"{self.directory} is not a directory")
        found: dict[str, str] = {}
        for entry in self.directory.iterdir():
            if not entry.name.endswith(WATCH_EXTENSIONS):
                continue
            found[str(entry)] = _fingerprint(entry)
        return found

    def baseline(self) -> None:
        """Record what is already there so it is not reported as an export."""
        self._snapshot = self._scan()
        self._baselined = True

    def changed(self) -> list[str]:
        """Paths that are new or modified since the last baseline.

        A deleted export is NOT a change: the operator removing a stale file is
        not them exporting one, and reporting it would send us off to diff a
        path that no longer exists.
        """
        if not self._baselined:
            self.baseline()
            return []
        current = self._scan()
        return sorted(
            path for path, fingerprint in current.items()
            if self._snapshot.get(path) != fingerprint
        )

    def pull(self, timeout: float = 120.0, interval: float = 1.0) -> Optional[str]:
        """Block until an export lands, then return its path.

        Returns None on timeout rather than raising: waiting and not getting an
        export is a normal outcome — the operator got distracted — not a fault.
        Re-baselines on success so the same export is not returned twice.
        """
        if not 0 < timeout <= 3600:
            raise ValueError(f"timeout must be between 0 and 3600s, got {timeout}")
        if not 0 < interval <= 60:
            raise ValueError(f"interval must be between 0 and 60s, got {interval}")

        deadline = time.monotonic() + timeout
        while True:
            found = self.changed()
            if found:
                self._snapshot = self._scan()
                return found[-1]
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
