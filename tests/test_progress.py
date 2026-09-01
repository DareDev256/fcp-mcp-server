"""Progress must be seen when it can be, and must never sink the operation."""

from types import SimpleNamespace

import pytest

from fcpxml import mcp_compat, progress


class FakeSession:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send_progress_notification(self, token, progress, total=None, message=None, **_):
        if self.fail:
            raise RuntimeError("transport closed")
        self.calls.append((token, progress, total, message))


def _ctx(session, token="tok-1"):
    meta = SimpleNamespace(progressToken=token) if token is not None else None
    return SimpleNamespace(session=session, meta=meta, request_id=7)


@pytest.mark.asyncio
async def test_outside_a_request_is_a_silent_noop():
    p = progress.start(total=3, server=None)
    assert p.live is False
    await p.step("one")
    await p.step("two")
    assert p.sent == []
    assert p.current == 2


@pytest.mark.asyncio
async def test_a_token_gets_notifications():
    session = FakeSession()
    token = mcp_compat._REQUEST_CTX.set(_ctx(session))
    try:
        p = progress.start(total=2)
        assert p.live is True
        await p.step("clip 1")
        await p.step("clip 2")
    finally:
        mcp_compat._REQUEST_CTX.reset(token)
    assert session.calls == [("tok-1", 1.0, 2.0, "clip 1"), ("tok-1", 2.0, 2.0, "clip 2")]


@pytest.mark.asyncio
async def test_no_token_means_nothing_is_sent():
    session = FakeSession()
    token = mcp_compat._REQUEST_CTX.set(_ctx(session, token=None))
    try:
        p = progress.start(total=1)
        await p.step("x")
    finally:
        mcp_compat._REQUEST_CTX.reset(token)
    assert session.calls == []


@pytest.mark.asyncio
async def test_a_failing_transport_is_swallowed_once_and_muted():
    session = FakeSession(fail=True)
    token = mcp_compat._REQUEST_CTX.set(_ctx(session))
    try:
        p = progress.start(total=2)
        await p.step("a")
        await p.step("b")
    finally:
        mcp_compat._REQUEST_CTX.reset(token)
    assert p.live is False
    assert p.current == 2


def test_current_request_falls_back_to_server_property_on_1x():
    class Srv:
        @property
        def request_context(self):
            raise LookupError("no request")

    assert mcp_compat.current_request(Srv()) is None
    ctx = _ctx(FakeSession())
    token = mcp_compat._REQUEST_CTX.set(ctx)
    try:
        assert mcp_compat.current_request(Srv()) is ctx
    finally:
        mcp_compat._REQUEST_CTX.reset(token)


@pytest.mark.asyncio
async def test_2x_adapter_parks_the_context_for_the_duration_of_the_call():
    """On mcp 2.x the ctx arrives as a handler argument; it must be visible
    from inside call_tool and gone afterwards."""
    if mcp_compat.is_legacy_api():
        pytest.skip("1.x exposes request_context as a property; nothing to park")
    import mcp.types as types
    from mcp.server import Server

    seen = {}

    async def call_tool(name, args):
        seen["ctx"] = mcp_compat.current_request()
        return [types.TextContent(type="text", text="ok")]

    async def nothing():
        return []

    async def one(_x, _y=None):
        return None

    srv = Server("t")
    mcp_compat.register_handlers(
        srv, list_resources=nothing, read_resource=one, list_prompts=nothing,
        get_prompt=one, list_tools=nothing, call_tool=call_tool,
    )
    entry = srv._request_handlers["tools/call"]
    handler = getattr(entry, "handler", entry)
    ctx = _ctx(FakeSession())
    await handler(ctx, types.CallToolRequestParams(name="x", arguments={}))
    assert seen["ctx"] is ctx
    assert mcp_compat.current_request() is None
