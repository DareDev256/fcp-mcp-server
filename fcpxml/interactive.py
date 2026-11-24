"""Ask the person, and run long work in the background (0.36).

Two MCP features meet here because they share one problem: a tool call that
needs something only the person at the keyboard can give.

Elicitation
-----------
:func:`ask` puts one question to the person and returns an :class:`Answer`.
The question reaches them by whichever route this request has, picked per
call:

1. **Inside a task** (the tasks extension, protocol 2026-07-28): the task goes
   ``input_required`` with the question in ``inputRequests``; the client
   answers with ``tasks/update`` and the task resumes where it stopped.
2. **A 2026-07-28 request**: that revision has no server-to-client requests,
   so the handler raises :class:`InputNeeded` and ``tools/call`` answers with
   an ``InputRequiredResult``. The client asks the person and retries the call
   with ``inputResponses``. Answers from earlier rounds ride back in a signed
   ``requestState``, so a call that asks twice does not loop.
3. **A handshake-era session** (2025-11-25 and older) whose client declared
   ``elicitation``: ``session.elicit_form``, on either SDK generation.

When none applies (no client support, ``claude -p``, a unit test) the answer
is ``unavailable`` and the caller keeps its old behaviour: refuse and name
the flag. Elicitation never replaces a gate; it lets the PERSON pass it
instead of the model.

Every question is asked before the work it guards, so re-running a handler
from the top on a retry repeats only reads.

Tasks
-----
:func:`start_task` runs a tool call in the background and returns the
``CreateTaskResult`` at once; ``tasks/get``, ``tasks/update`` and
``tasks/cancel`` (:func:`task_get` and friends) serve the rest. The store is
in memory: a stdio server is one process per client, and a task does not
outlive it. Progress reported through :mod:`fcpxml.progress` becomes the
task's ``statusMessage``.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

MODERN_PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"

TASK_TTL_MS = 60 * 60 * 1000       # a finished task is kept an hour
TASK_POLL_MS = 2000                # Claude Code's own default when absent

__all__ = [
    "Answer",
    "InputNeeded",
    "MODERN_PROTOCOL_VERSION",
    "TASKS_EXTENSION_ID",
    "ask",
    "choice_schema",
    "client_declares_tasks",
    "current_task",
    "is_modern",
    "start_task",
    "task_cancel",
    "task_get",
    "task_update",
]


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """What the person said. ``action`` is accept, decline, cancel or unavailable."""

    action: str
    content: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.action == "accept"

    @property
    def unavailable(self) -> bool:
        return self.action == "unavailable"

    def value(self, name: str) -> Any:
        """A submitted field, or None unless the form was accepted."""
        return self.content.get(name) if self.accepted else None


UNAVAILABLE = Answer("unavailable")


def choice_schema(name: str, title: str, options: list[tuple[str, str]]) -> dict:
    """A one-field form: a required single choice, each option ``(value, label)``.

    One explicit choice rather than a bare accept button: "accept" on an empty
    form says the dialog was submitted, not what was decided, and a spend or
    an unreviewed delivery needs the decision itself on record.
    """
    return {
        "type": "object",
        "properties": {
            name: {
                "type": "string",
                "title": title,
                "oneOf": [{"const": value, "title": label} for value, label in options],
            }
        },
        "required": [name],
    }


def _answer_from(raw: Any) -> Answer:
    """An ``ElicitResult`` (model or wire dict) as an :class:`Answer`."""
    if raw is None:
        return Answer("cancel")
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(by_alias=True, mode="json", exclude_none=True)
    if not isinstance(raw, dict):
        return Answer("cancel")
    action = raw.get("action")
    if action not in ("accept", "decline", "cancel"):
        return Answer("cancel")
    content = raw.get("content") if action == "accept" else None
    return Answer(action, dict(content) if isinstance(content, dict) else {})


# ---------------------------------------------------------------------------
# Who is asking: the request context on either SDK generation
# ---------------------------------------------------------------------------

def _current_ctx() -> Any:
    from fcpxml import mcp_compat

    try:
        import tools
        srv = tools.server_module().server
    except Exception:
        srv = None
    return mcp_compat.current_request(srv)


def is_modern(ctx: Any) -> bool:
    """True when this request speaks 2026-07-28 (no server-to-client requests)."""
    return getattr(ctx, "protocol_version", None) == MODERN_PROTOCOL_VERSION


def _request_caps(ctx: Any) -> dict:
    """The client capabilities this request declared, as a plain dict.

    2026-07-28 carries them per request in ``_meta``; the handshake era
    declared them once at ``initialize``. Read the raw form first and fall
    back to whatever the session parsed, on either SDK.
    """
    meta = getattr(ctx, "meta", None)
    caps: Any = None
    if isinstance(meta, dict):
        caps = meta.get(CLIENT_CAPABILITIES_META_KEY)
    elif meta is not None:
        extra = getattr(meta, "model_extra", None) or {}
        caps = extra.get(CLIENT_CAPABILITIES_META_KEY)
    if caps is None:
        session = getattr(ctx, "session", None)
        caps = getattr(session, "client_capabilities", None)
        if caps is None:
            params = getattr(session, "client_params", None)
            caps = getattr(params, "capabilities", None)
    if caps is None:
        return {}
    if hasattr(caps, "model_dump"):
        caps = caps.model_dump(by_alias=True, mode="json", exclude_none=True)
    return caps if isinstance(caps, dict) else {}


def _declares_elicitation(ctx: Any) -> bool:
    elicitation = _request_caps(ctx).get("elicitation")
    if elicitation is None:
        return False
    # `{}` is form mode only; `{"url": {}}` alone would be URL only.
    return not isinstance(elicitation, dict) or not elicitation or "form" in elicitation


def client_declares_tasks(ctx: Any) -> bool:
    """True when this request opted into the tasks extension (2026-07-28 only)."""
    if not is_modern(ctx):
        return False
    extensions = _request_caps(ctx).get("extensions") or {}
    return isinstance(extensions, dict) and TASKS_EXTENSION_ID in extensions


# ---------------------------------------------------------------------------
# The multi-round-trip state (2026-07-28)
# ---------------------------------------------------------------------------

# Answers already given for THIS call: input_responses of the retry, plus the
# earlier rounds carried in requestState. Set by the tools/call adapter.
_ANSWERS: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "fcp_mcp_answers", default=None
)

# The spec says to treat an echoed requestState as attacker-controlled. It
# carries only answers the client could assert directly in inputResponses,
# but it is still sealed, so a stale or edited one is dropped, not trusted.
_STATE_KEY = os.urandom(32)
_STATE_TTL_S = 15 * 60


class InputNeeded(BaseException):
    """Raised by :func:`ask` on a 2026-07-28 request that has no answer yet.

    A BaseException on purpose: handlers catch ``Exception`` broadly to turn
    failures into readable text, and this must pass through all of them to
    the ``tools/call`` adapter, which turns it into an ``InputRequiredResult``.
    """

    def __init__(self, key: str, request: dict):
        super().__init__(key)
        self.key = key
        self.request = request


def seal_state(answers: dict) -> str:
    body = json.dumps({"a": answers, "t": int(time.time())}, separators=(",", ":"), sort_keys=True)
    raw = body.encode()
    tag = hmac.new(_STATE_KEY, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(tag + raw).decode().rstrip("=")


def unseal_state(token: Optional[str]) -> dict:
    """The answers a sealed state carries, or ``{}`` for anything unverifiable."""
    if not token or not isinstance(token, str):
        return {}
    try:
        blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return {}
    tag, raw = blob[:32], blob[32:]
    if not hmac.compare_digest(tag, hmac.new(_STATE_KEY, raw, hashlib.sha256).digest()):
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(body, dict) or time.time() - body.get("t", 0) > _STATE_TTL_S:
        return {}
    answers = body.get("a")
    return answers if isinstance(answers, dict) else {}


def answers_for_call(request_state: Optional[str], input_responses: Any) -> dict:
    """Merge the sealed earlier rounds with this retry's responses."""
    answers = dict(unseal_state(request_state))
    if isinstance(input_responses, dict):
        for key, raw in input_responses.items():
            a = _answer_from(raw)
            answers[str(key)] = {"action": a.action, "content": a.content}
    return answers


def bind_answers(answers: dict) -> contextvars.Token:
    return _ANSWERS.set(answers)


def reset_answers(token: contextvars.Token) -> None:
    _ANSWERS.reset(token)


def elicit_request(message: str, schema: dict) -> dict:
    """The wire form of one ``elicitation/create`` input request."""
    return {
        "method": "elicitation/create",
        "params": {"mode": "form", "message": message, "requestedSchema": schema},
    }


# ---------------------------------------------------------------------------
# ask()
# ---------------------------------------------------------------------------

async def ask(key: str, message: str, schema: dict) -> Answer:
    """Put one question to the person. Never raises for a missing client.

    *key* names the question within one tool call; reuse it and the earlier
    answer is returned. *schema* is a flat form schema (see
    :func:`choice_schema`).
    """
    task = current_task()
    if task is not None:
        return await task.ask(key, message, schema)

    ctx = _current_ctx()
    if ctx is None or not _declares_elicitation(ctx):
        return UNAVAILABLE

    if is_modern(ctx):
        answers = _ANSWERS.get()
        if answers is None:
            # Not under the tools/call adapter (a resource read, a test):
            # there is no retry to carry an answer back.
            return UNAVAILABLE
        if key in answers:
            given = answers[key] or {}
            return Answer(given.get("action", "cancel"), dict(given.get("content") or {}))
        raise InputNeeded(key, elicit_request(message, schema))

    session = getattr(ctx, "session", None)
    elicit_form = getattr(session, "elicit_form", None)
    if elicit_form is None:
        return UNAVAILABLE
    try:
        # Positional: 1.x names the schema `requestedSchema`, 2.x `requested_schema`.
        result = await elicit_form(message, schema, related_request_id=getattr(ctx, "request_id", None))
    except Exception as exc:  # a client that cannot answer is not a failure of the tool
        logger.debug("elicitation unavailable: %s", exc)
        return UNAVAILABLE
    return _answer_from(result)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskError(Exception):
    """A tasks/* request naming a task this server does not hold."""


@dataclass
class TaskRecord:
    task_id: str
    status: str = "working"
    status_message: Optional[str] = None
    created_at: str = field(default_factory=_now)
    last_updated_at: str = field(default_factory=_now)
    created_mono: float = field(default_factory=time.monotonic)
    finished_mono: Optional[float] = None
    result: Optional[dict] = None
    error: Optional[dict] = None
    input_requests: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    job: Optional[asyncio.Task] = None

    def touch(self, status: Optional[str] = None, message: Optional[str] = None) -> None:
        if status is not None:
            self.status = status
            if status in ("completed", "failed", "cancelled"):
                self.finished_mono = time.monotonic()
        if message is not None:
            self.status_message = message
        self.last_updated_at = _now()

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def wire(self) -> dict:
        """The task as a ``DetailedTask`` (camelCase, status-specific fields inlined)."""
        out: dict[str, Any] = {
            "taskId": self.task_id,
            "status": self.status,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.last_updated_at,
            "ttlMs": TASK_TTL_MS,
            "pollIntervalMs": TASK_POLL_MS,
        }
        if self.status_message:
            out["statusMessage"] = self.status_message
        if self.status == "completed" and self.result is not None:
            out["result"] = self.result
        if self.status == "failed" and self.error is not None:
            out["error"] = self.error
        if self.status == "input_required":
            out["inputRequests"] = dict(self.input_requests)
        return out

    async def ask(self, key: str, message: str, schema: dict) -> Answer:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending[key] = future
        self.input_requests[key] = elicit_request(message, schema)
        self.touch("input_required", message)
        try:
            raw = await future
        finally:
            self.pending.pop(key, None)
            self.input_requests.pop(key, None)
            if not self.terminal:
                self.touch("working" if not self.input_requests else "input_required")
        return _answer_from(raw)

    def progress(self, current: float, total: Optional[float], message: str) -> None:
        if self.terminal:
            return
        if total:
            text = f"{message} ({int(current)}/{int(total)})" if message else f"{int(current)}/{int(total)}"
        else:
            text = message or None
        if text:
            self.touch(message=text)


_TASKS: dict[str, TaskRecord] = {}
_TASK: contextvars.ContextVar[Optional[TaskRecord]] = contextvars.ContextVar("fcp_mcp_task", default=None)


def current_task() -> Optional[TaskRecord]:
    return _TASK.get()


def _purge() -> None:
    cutoff = time.monotonic() - TASK_TTL_MS / 1000
    for tid in [t for t, rec in _TASKS.items() if rec.finished_mono is not None and rec.finished_mono < cutoff]:
        _TASKS.pop(tid, None)


def start_task(run: Callable[[], Awaitable[dict]], *, label: str = "") -> dict:
    """Run *run* in the background; return the ``CreateTaskResult`` wire dict.

    *run* returns the finished result as a wire dict (a dumped
    ``CallToolResult``). Must be called on the running event loop.
    """
    _purge()
    record = TaskRecord(task_id=f"fcp-{uuid.uuid4().hex[:16]}", status_message=label or None)

    async def _drive() -> None:
        token = _TASK.set(record)
        try:
            record.result = await run()
            record.touch("completed", record.status_message)
        except asyncio.CancelledError:
            record.touch("cancelled", "Cancelled before it finished.")
        except BaseException as exc:  # noqa: BLE001 - a task records its own failure
            logger.exception("task %s failed", record.task_id)
            record.error = {"code": -32603, "message": f"{type(exc).__name__}: {exc}"[:500]}
            record.touch("failed", record.error["message"])
        finally:
            _TASK.reset(token)
            for future in record.pending.values():
                if not future.done():
                    future.cancel()

    _TASKS[record.task_id] = record
    record.job = asyncio.get_running_loop().create_task(_drive())
    created = record.wire()
    created["resultType"] = "task"
    return created


def _record(task_id: Any) -> TaskRecord:
    _purge()
    record = _TASKS.get(str(task_id)) if task_id is not None else None
    if record is None:
        raise TaskError(f"No task {task_id!r} on this server (unknown, or past its {TASK_TTL_MS // 60000} min TTL).")
    return record


def task_get(task_id: Any) -> dict:
    out = _record(task_id).wire()
    out["resultType"] = "complete"
    return out


def task_update(task_id: Any, input_responses: Any) -> dict:
    """Deliver answers to an ``input_required`` task. Unknown keys are ignored."""
    record = _record(task_id)
    if isinstance(input_responses, dict):
        for key, raw in input_responses.items():
            future = record.pending.get(str(key))
            if future is not None and not future.done():
                future.set_result(raw)
    return {"resultType": "complete"}


def task_cancel(task_id: Any) -> dict:
    """Cooperative cancel: the job is cancelled; a finished task is left as it is."""
    record = _record(task_id)
    if not record.terminal:
        if record.job is not None and not record.job.done():
            record.job.cancel()
        record.touch("cancelled", "Cancelled at the client's request.")
    return {"resultType": "complete"}
