"""Progress notifications — one seam, both SDKs, never a hard failure.

Any operation that runs longer than a couple of seconds (a silence scan per
clip, a transcription per file, an index build) reports through a
``Progress``. If the client sent a ``progressToken`` the notification goes
out; if it did not, or the SDK has no session, or the send itself fails,
the report is dropped and the operation carries on. A stalled prompt with
no signal is the failure this exists to remove; a prompt that fails because
its progress message could not be delivered would be a worse one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fcpxml import mcp_compat

logger = logging.getLogger(__name__)


class Progress:
    """Counter that forwards to the current request's progress token."""

    def __init__(self, total: Optional[int] = None, *, session: Any = None, token: Any = None):
        self.total = total
        self.current = 0.0
        self._session = session
        self._token = token
        self.sent: list[tuple] = []

    @property
    def live(self) -> bool:
        return self._session is not None and self._token is not None

    async def step(self, message: str = "", advance: float = 1) -> None:
        self.current += advance
        await self.set(self.current, self.total, message)

    async def set(self, progress: float, total: Optional[float] = None, message: str = "") -> None:
        self.current = progress
        if total is not None:
            self.total = total
        if not self.live:
            return
        try:
            await self._session.send_progress_notification(
                self._token, float(progress), float(self.total) if self.total is not None else None,
                message or None,
            )
            self.sent.append((float(progress), self.total, message))
        except Exception as exc:  # the operation must outlive its progress bar
            logger.debug("progress notification dropped: %s", exc)
            self._session = None


def start(total: Optional[int] = None, server: Any = None) -> Progress:
    """A ``Progress`` bound to the in-flight request, or a silent one."""
    if server is None:
        try:
            import tools

            server = tools.server_module().server
        except Exception:
            server = None
    ctx = mcp_compat.current_request(server)
    session = getattr(ctx, "session", None)
    meta = getattr(ctx, "meta", None)
    token = getattr(meta, "progressToken", None) if meta is not None else None
    return Progress(total, session=session, token=token)
