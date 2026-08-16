"""The six handlers register on whichever ``mcp`` Server API is installed.

Issue #9. mcp 2.0.0 removed the low-level decorator API, so every 2.x install
failed at import with ``AttributeError: 'Server' object has no attribute
'list_resources'``. v0.13.2 pinned ``mcp<2.0.0`` to unbreak installs; this
suite is what lets the pin come off.

These tests deliberately assert against *whichever* SDK is installed rather
than branching to a single expected answer, so the same file is meaningful on
both sides of the split. CI runs it at the declared floor and at 2.x.
"""

import asyncio

import pytest

import server as server_module
from fcpxml.mcp_compat import (
    MCP_API_VERSION,
    is_legacy_api,
    register_handlers,
    resource_mime_type,
    tool_input_schema,
)

# The six MCP methods this server implements, in 2.x wire-method form.
METHODS = [
    "resources/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
    "tools/list",
    "tools/call",
]


class TestApiDetection:
    def test_version_label_matches_the_probe(self):
        assert MCP_API_VERSION == ("1.x" if is_legacy_api() else "2.x")

    def test_probe_reads_the_class_not_a_version_string(self):
        """A fork or a 2.x that restores the decorators must be judged by
        what it exposes, not by what it calls itself."""
        from mcp.server import Server

        assert is_legacy_api() is hasattr(Server, "list_tools")


class TestRegistration:
    """Whatever the SDK, importing server.py must leave all six wired."""

    def test_all_six_methods_are_registered(self):
        if is_legacy_api():
            # 1.x keys `request_handlers` by request *type*; compare on the
            # method literal each type declares so this assertion reads the
            # same on both sides of the split.
            registered = {
                req.model_fields["method"].default
                for req in server_module.server.request_handlers
            }
        else:
            registered = set(server_module.server._request_handlers)
        for method in METHODS:
            assert method in registered, f"{method} not registered on {MCP_API_VERSION}"

    def test_handlers_are_still_importable_as_plain_functions(self):
        """The decorators used to consume these names. Nothing may shadow
        them — the shim needs the bare coroutine functions."""
        for name in (
            "list_resources",
            "read_resource",
            "list_prompts",
            "get_prompt",
            "list_tools",
            "call_tool",
        ):
            fn = getattr(server_module, name)
            assert asyncio.iscoroutinefunction(fn), name

    @pytest.mark.skipif(is_legacy_api(), reason="2.x registration path only")
    def test_registration_is_idempotent_per_method(self):
        """add_request_handler replaces rather than appends, so a re-register
        must not multiply entries or leave a stale handler behind."""
        registry = server_module.server._request_handlers
        before = dict(registry)
        register_handlers(
            server_module.server,
            list_resources=server_module.list_resources,
            read_resource=server_module.read_resource,
            list_prompts=server_module.list_prompts,
            get_prompt=server_module.get_prompt,
            list_tools=server_module.list_tools,
            call_tool=server_module.call_tool,
        )
        assert set(registry) == set(before)

    @pytest.mark.skipif(is_legacy_api(), reason="2.x capabilities derivation")
    def test_capabilities_advertise_tools_prompts_and_resources(self):
        """2.x derives capabilities from the handler registry, so post-hoc
        registration must advertise the same surface constructor kwargs would."""
        caps = server_module.server.get_capabilities()
        assert caps.tools is not None
        assert caps.prompts is not None
        assert caps.resources is not None


class TestFieldAliasAccessors:
    """2.x renamed fields and kept the old spelling as a *serialisation*
    alias, so constructing with ``inputSchema=`` still works while reading
    ``.inputSchema`` raises. That asymmetry broke the missing-argument help
    path while every tool definition kept building fine."""

    def test_tool_input_schema_reads_on_this_sdk(self):
        tools = asyncio.run(server_module.list_tools())
        assert tools
        for tool in tools:
            schema = tool_input_schema(tool)
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"

    def test_tool_input_schema_survives_a_tool_with_no_schema(self):
        class Bare:
            pass

        assert tool_input_schema(Bare()) == {}

    def test_resource_mime_type_reads_on_this_sdk(self):
        from mcp.types import TextResourceContents

        item = TextResourceContents(
            uri="file:///x.fcpxml", mimeType="text/html", text="<html>"
        )
        assert resource_mime_type(item) == "text/html"

    def test_resource_mime_type_is_none_when_absent(self):
        class Bare:
            pass

        assert resource_mime_type(Bare()) is None


class TestActionHelpSurvivesTheAliasRename:
    """The concrete regression: on 2.x, `.inputSchema` raising turned a
    recoverable "you forgot media_path" into an unhandled exception."""

    def test_missing_argument_still_yields_help_not_a_crash(self):
        result = asyncio.run(
            server_module.call_tool("diagnose", {"action": "detect_beats", "args": {}})
        )
        text = result[0].text
        assert "media_path" in text
        assert "Error: AttributeError" not in text
