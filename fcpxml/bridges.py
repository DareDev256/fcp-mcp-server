"""Detect optional third-party Final Cut Pro control surfaces on loopback.

SpliceKit (JSON-RPC :9876) and CommandPost (WebSocket :27480) can each close
the export half of the loop. Neither is bundled, vendored, required, or
installed by us: this server never patches or injects anything, and that
position is the product — it is what makes it run on a managed Mac and survive
an FCP update.

DETECTION ONLY. Triggering an export through either surface needs their RPC
signatures verified against a live install. Writing a call without that would
be inventing an API rather than integrating with one, so describe() states
plainly that a detected bridge is still not being called.
"""

import logging
import socket
from typing import Dict

logger = logging.getLogger(__name__)

BRIDGES: Dict[str, int] = {
    "splicekit": 9876,
    "commandpost": 27480,
}

LOOPBACK = "127.0.0.1"
PROBE_TIMEOUT_SECONDS = 0.25

_CACHE: Dict[str, bool] = {}


def probe(port: int, host: str = LOOPBACK, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """True when something accepts a TCP connection on *port*.

    Loopback only, short timeout. A probe failure is never an error — it is the
    expected result, and it means the manual export path.
    """
    if host != LOOPBACK:
        raise ValueError(f"bridge probes are loopback-only, refused: {host}")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect(refresh: bool = False) -> Dict[str, bool]:
    """Which bridges are reachable. Cached for the session."""
    if _CACHE and not refresh:
        return dict(_CACHE)
    _CACHE.clear()
    for name, port in BRIDGES.items():
        _CACHE[name] = probe(port=port)
    logger.info("bridge detection: %s", _CACHE)
    return dict(_CACHE)


def describe() -> str:
    """One operator-readable line about the export path actually in force."""
    found = detect()
    live = sorted(name for name, up in found.items() if up)
    if not live:
        return (
            "No control bridge detected. Export from Final Cut Pro with "
            "File > Export XML (Cmd-E) into the watched folder; watch_pull "
            "will pick it up."
        )
    return (
        f"Detected: {', '.join(live)}. Automatic export triggering is not "
        "implemented — this server does not call either bridge. Export with "
        "Cmd-E as usual."
    )
