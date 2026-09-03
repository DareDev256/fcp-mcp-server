"""Register MCP handlers against either the 1.x or the 2.x ``Server`` API.

Why this exists
---------------

``mcp`` 2.0.0 (PyPI, 2026-07-28) removed the low-level decorator API this
server was built on. All six decorators are gone::

    list_resources  read_resource  list_prompts  get_prompt  list_tools  call_tool

Any 2.x install failed at import with ``AttributeError: 'Server' object has
no attribute 'list_resources'``, which took six test modules down during
collection. v0.13.2 pinned ``mcp<2.0.0`` to unbreak installs; this module is
the actual port, and it keeps the pin off rather than trading one broken
half of the ecosystem for the other. An MCP server that only runs on the SDK
version its author happened to have installed is not much of a server.

What changed, and what this module hides
----------------------------------------

Two shape changes come with 2.x:

1. Handlers move from decorators to ``on_*`` constructor kwargs, or to
   ``add_request_handler(method, params_type, handler)``. This module uses
   the latter, because it registers *after* construction — the handler
   functions in ``server.py`` are defined below the ``Server(...)`` call and
   restructuring that would be a much larger diff for no behavioural gain.
   Capabilities in 2.x derive from ``Server._request_handlers``, which
   ``add_request_handler`` populates, so post-hoc registration advertises
   exactly what constructor kwargs would.

2. Handlers take ``(ctx, params)`` and return a full Result model rather
   than loose positional arguments and a bare list.

The handler functions themselves are left in their 1.x shape — plain
arguments in, bare list out. The adapters below unwrap params and wrap
results. That keeps one readable implementation of each handler instead of
two, and keeps the diff against the 1.x behaviour auditable.

What did not change: the transport. ``stdio_server``, ``server.run`` and
``create_initialization_options`` all survive 2.0 unchanged, so ``main()``
is untouched.
"""

from __future__ import annotations

import contextvars
from typing import Any, Awaitable, Callable

from mcp.server import Server

__all__ = [
    "MCP_API_VERSION",
    "current_request",
    "is_legacy_api",
    "register_handlers",
    "resource_mime_type",
    "tool_input_schema",
]


def is_legacy_api() -> bool:
    """True on ``mcp`` 1.x, where ``Server`` still carries the decorators.

    Probed by attribute rather than by version string: a fork, a vendored
    copy, or a 2.x pre-release that restores the decorators should be
    treated by what it exposes, not by what it calls itself.
    """
    return hasattr(Server, "list_tools")


MCP_API_VERSION = "1.x" if is_legacy_api() else "2.x"

# 2.x hands the per-request context to the handler as an argument and has no
# ``Server.request_context`` property; 1.x has the property and no argument.
# The 2.x adapter parks the context here so callers deeper in the stack
# (progress reporting) can find it the same way on both.
_REQUEST_CTX: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "fcp_mcp_request_ctx", default=None
)


def current_request(server: Server | None = None) -> Any | None:
    """The in-flight request context, or ``None`` outside a request.

    Returns an object carrying ``.session`` and ``.meta`` on either SDK.
    Never raises: a handler asking for progress outside a request (a unit
    test, a direct call) must simply get nothing to report to.
    """
    ctx = _REQUEST_CTX.get()
    if ctx is not None:
        return ctx
    if server is None:
        return None
    try:
        return server.request_context
    except (LookupError, AttributeError):
        return None


def tool_input_schema(tool: Any) -> dict:
    """Read a ``Tool``'s JSON schema across both SDK generations.

    2.x renamed the field to ``input_schema`` and kept ``inputSchema`` only
    as a serialisation alias, so *constructing* a Tool with ``inputSchema=``
    still works while *reading* ``.inputSchema`` raises AttributeError. That
    asymmetry is easy to miss — it broke the missing-argument help path,
    turning a recoverable "you forgot media_path" into an exception, while
    every tool definition kept building fine.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema or {}


def resource_mime_type(contents: Any) -> str | None:
    """Read a resource contents entry's MIME type across both SDK generations.

    Same alias asymmetry as :func:`tool_input_schema`: 2.x renamed the field
    to ``mime_type`` and kept ``mimeType`` as a serialisation alias only.
    """
    value = getattr(contents, "mime_type", None)
    if value is None:
        value = getattr(contents, "mimeType", None)
    return value


def _to_resource_contents(uri: str, result: Any) -> list[Any]:
    """Normalise a read_resource return into 2.x ``contents`` entries.

    The handler may return a plain string (1.x allowed it, with a
    deprecation warning) or a list of ``ReadResourceContents``. Both become
    ``TextResourceContents``; a bare string is text/plain, matching what the
    1.x server did with it.
    """
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.types import TextResourceContents

    if isinstance(result, str):
        return [TextResourceContents(uri=uri, mimeType="text/plain", text=result)]

    contents = []
    for item in result:
        if isinstance(item, ReadResourceContents):
            contents.append(
                TextResourceContents(
                    uri=uri,
                    mimeType=item.mime_type or "text/plain",
                    text=item.content,
                )
            )
        else:
            # Already a wire model (TextResourceContents / Blob…) — pass through.
            contents.append(item)
    return contents


def register_handlers(
    server: Server,
    *,
    list_resources: Callable[[], Awaitable[list]],
    read_resource: Callable[[str], Awaitable[Any]],
    list_prompts: Callable[[], Awaitable[list]],
    get_prompt: Callable[[str, dict | None], Awaitable[Any]],
    list_tools: Callable[[], Awaitable[list]],
    call_tool: Callable[[str, dict[str, Any]], Awaitable[Any]],
) -> None:
    """Wire the six handlers onto *server* using whichever API it exposes.

    The callables are the 1.x-shaped handler functions from ``server.py``.
    On 1.x they are handed straight to the decorators; on 2.x they are
    wrapped so that params come in unpacked and results go out as Result
    models.
    """
    if is_legacy_api():
        server.list_resources()(list_resources)
        server.read_resource()(read_resource)
        server.list_prompts()(list_prompts)
        server.get_prompt()(get_prompt)
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
        return

    import mcp.types as types

    async def _on_list_resources(_ctx, _params):
        return types.ListResourcesResult(resources=await list_resources())

    async def _on_read_resource(_ctx, params):
        # 2.x hands over a parsed AnyUrl; the handler wants the string form
        # it got from 1.x. Percent-decoding stays the handler's job
        # (``_uri_to_path``) — unquoting here as well would double-decode a
        # library path whose name legitimately contains a '%'.
        uri = str(params.uri)
        return types.ReadResourceResult(
            contents=_to_resource_contents(uri, await read_resource(uri))
        )

    async def _on_list_prompts(_ctx, _params):
        return types.ListPromptsResult(prompts=await list_prompts())

    async def _on_get_prompt(_ctx, params):
        return await get_prompt(params.name, params.arguments)

    async def _on_list_tools(_ctx, _params):
        return types.ListToolsResult(tools=await list_tools())

    async def _on_call_tool(ctx, params):
        token = _REQUEST_CTX.set(ctx)
        try:
            content = await call_tool(params.name, params.arguments or {})
        finally:
            _REQUEST_CTX.reset(token)
        return types.CallToolResult(content=list(content))

    server.add_request_handler(
        "resources/list", types.PaginatedRequestParams, _on_list_resources
    )
    server.add_request_handler(
        "resources/read", types.ReadResourceRequestParams, _on_read_resource
    )
    server.add_request_handler(
        "prompts/list", types.PaginatedRequestParams, _on_list_prompts
    )
    server.add_request_handler(
        "prompts/get", types.GetPromptRequestParams, _on_get_prompt
    )
    server.add_request_handler(
        "tools/list", types.PaginatedRequestParams, _on_list_tools
    )
    server.add_request_handler(
        "tools/call", types.CallToolRequestParams, _on_call_tool
    )
