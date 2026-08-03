"""Grouped tool facade: 7 verbs dispatching into the existing 62 handlers."""
import asyncio
import json

import pytest

import server


class TestGroupDispatch:
    def test_inspect_group_exists_with_actions(self):
        assert "inspect" in server.TOOL_GROUPS
        actions = server.TOOL_GROUPS["inspect"]["actions"]
        assert "list_clips" in actions
        assert "analyze_timeline" in actions

    def test_every_group_action_resolves_to_a_real_handler(self):
        """A typo in an action list must fail loudly here, not at runtime."""
        for group, spec in server.TOOL_GROUPS.items():
            for action in spec["actions"]:
                assert action in server.TOOL_HANDLERS, f"{group}.{action} has no handler"

    def test_dispatch_forwards_args_to_the_underlying_handler(self):
        captured = {}

        async def fake_handler(args):
            captured.update(args)
            return server._text_result("ok")

        server.TOOL_HANDLERS["__test_action__"] = fake_handler
        server.TOOL_GROUPS["inspect"]["actions"].append("__test_action__")
        try:
            result = asyncio.run(server.handle_group(
                "inspect", {"action": "__test_action__", "args": {"filepath": "/x.fcpxml"}}
            ))
            assert captured == {"filepath": "/x.fcpxml"}
            assert result[0].text == "ok"
        finally:
            del server.TOOL_HANDLERS["__test_action__"]
            server.TOOL_GROUPS["inspect"]["actions"].remove("__test_action__")

    def test_missing_args_defaults_to_empty_dict(self):
        """Actions that take no parameters must work without an args key."""
        captured = {}

        async def fake_handler(args):
            captured["got"] = args
            return server._text_result("ok")

        server.TOOL_HANDLERS["__test_noargs__"] = fake_handler
        server.TOOL_GROUPS["inspect"]["actions"].append("__test_noargs__")
        try:
            asyncio.run(server.handle_group("inspect", {"action": "__test_noargs__"}))
            assert captured["got"] == {}
        finally:
            del server.TOOL_HANDLERS["__test_noargs__"]
            server.TOOL_GROUPS["inspect"]["actions"].remove("__test_noargs__")

    def test_unknown_action_lists_the_valid_ones(self):
        """The error must teach the model what it should have called."""
        result = asyncio.run(server.handle_group("inspect", {"action": "nope"}))
        text = result[0].text
        assert "nope" in text
        assert "list_clips" in text

    def test_action_from_another_group_is_rejected(self):
        """trim_clip is real, but it is not an inspect action."""
        result = asyncio.run(server.handle_group("inspect", {"action": "trim_clip"}))
        assert "trim_clip" in result[0].text
        assert "list_clips" in result[0].text

    def test_missing_action_is_rejected(self):
        result = asyncio.run(server.handle_group("inspect", {}))
        assert "action" in result[0].text.lower()

    def test_unknown_group_is_rejected(self):
        result = asyncio.run(server.handle_group("nonexistent", {"action": "list_clips"}))
        text = result[0].text
        assert "nonexistent" in text
        # The error must enumerate the real groups, not just echo the bad
        # name back — this is what catches a regression in the group list
        # as more groups are added.
        assert "inspect" in text
        assert "diagnose" in text

    @pytest.mark.parametrize("bad_args", ["foo", ["a", "b"], 42])
    def test_non_dict_args_is_rejected_not_raised(self, bad_args):
        """A non-dict 'args' must return a teaching error, never raise."""
        result = asyncio.run(server.handle_group(
            "inspect", {"action": "list_clips", "args": bad_args}
        ))
        text = result[0].text
        assert "inspect" in text
        assert "list_clips" in text
        assert type(bad_args).__name__ in text


class TestGroupTool:
    def test_group_tool_schema_for_inspect(self):
        tool = server._group_tool("inspect")
        actions = server.TOOL_GROUPS["inspect"]["actions"]

        assert tool.name == "inspect"
        assert any(action in tool.description for action in actions)
        assert tool.inputSchema["properties"]["action"]["enum"] == actions
        assert tool.inputSchema["required"] == ["action"]
        assert tool.inputSchema["properties"]["args"]["type"] == "object"


class TestGroupCoverage:
    EXPECTED_GROUPS = {
        "inspect", "diagnose", "edit", "mark", "generate", "transcript", "deliver",
    }

    def test_all_seven_groups_present(self):
        assert set(server.TOOL_GROUPS) == self.EXPECTED_GROUPS

    def test_every_handler_belongs_to_exactly_one_group(self):
        """No orphaned tool, no tool reachable from two groups."""
        seen = {}
        for group, spec in server.TOOL_GROUPS.items():
            for action in spec["actions"]:
                assert action not in seen, f"{action} in both {seen.get(action)} and {group}"
                seen[action] = group
        missing = set(server.TOOL_HANDLERS) - set(seen)
        assert not missing, f"handlers in no group: {sorted(missing)}"

    def test_group_count_is_a_real_reduction(self):
        assert len(server.TOOL_GROUPS) <= 8
        assert len(server.TOOL_HANDLERS) >= 62


class TestLegacyGating:
    def test_default_advertises_only_groups(self, monkeypatch):
        monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
        tools = asyncio.run(server.list_tools())
        assert {t.name for t in tools} == set(server.TOOL_GROUPS)

    def test_opt_in_advertises_groups_plus_legacy(self, monkeypatch):
        monkeypatch.setenv("FCP_MCP_LEGACY_TOOLS", "1")
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        assert "trim_clip" in names
        assert "edit" in names
        assert len(names) >= 62 + len(server.TOOL_GROUPS)

    def test_hidden_tools_still_dispatch(self, monkeypatch):
        """The whole compat story: every one of the 62 flat tools that
        list_tools() no longer advertises must still reach its OWN handler
        through call_tool(). A version of call_tool that rejected every
        flat name would still pass a check that only inspects TOOL_HANDLERS
        membership — so this drives call_tool() itself, for every name.
        """
        monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
        tools = asyncio.run(server.list_tools())
        advertised = {t.name for t in tools}
        assert not (set(server.TOOL_HANDLERS) & advertised), (
            "a flat tool is still advertised — nothing to prove hidden"
        )

        original_handlers = dict(server.TOOL_HANDLERS)
        reached: dict[str, str] = {}
        try:
            for tool_name in original_handlers:
                async def sentinel(arguments, _name=tool_name):
                    reached[_name] = _name
                    return server._text_result(f"__sentinel__:{_name}")

                server.TOOL_HANDLERS[tool_name] = sentinel

            for tool_name in original_handlers:
                result = asyncio.run(server.call_tool(tool_name, {}))
                assert result[0].text == f"__sentinel__:{tool_name}", (
                    f"{tool_name} did not reach its own handler: {result[0].text!r}"
                )
        finally:
            server.TOOL_HANDLERS.clear()
            server.TOOL_HANDLERS.update(original_handlers)

        assert reached.keys() == original_handlers.keys()
        assert "list_projects" not in advertised
        assert "list_projects" in server.TOOL_HANDLERS

    def test_schema_payload_is_substantially_smaller(self, monkeypatch):
        monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
        grouped = asyncio.run(server.list_tools())
        monkeypatch.setenv("FCP_MCP_LEGACY_TOOLS", "1")
        legacy = asyncio.run(server.list_tools())

        def size(ts):
            return len(json.dumps(
                [{"n": t.name, "d": t.description, "s": t.inputSchema} for t in ts]
            ))

        assert size(grouped) < size(legacy) * 0.35, (
            f"grouped={size(grouped)} legacy={size(legacy)}"
        )

    def test_group_names_never_collide_with_a_flat_tool_name(self):
        """call_tool() checks TOOL_GROUPS before TOOL_HANDLERS. If a future
        flat tool were ever named 'edit', 'inspect', 'mark', 'generate',
        'diagnose', 'transcript', or 'deliver', the group branch would win
        and silently shadow it — the exact compatibility break this task
        exists to prevent, forever uncallable and with no error raised.
        """
        assert not (set(server.TOOL_GROUPS) & set(server.TOOL_HANDLERS))


class TestCallToolGroupDispatch:
    def test_call_tool_routes_group_name_to_handle_group(self):
        """A group name reaches handle_group through call_tool, not 'Unknown tool'."""
        direct = asyncio.run(server.handle_group("inspect", {}))
        via_call_tool = asyncio.run(server.call_tool("inspect", {}))
        assert via_call_tool[0].text == direct[0].text
        assert via_call_tool[0].text != "Unknown tool: inspect"

    def test_call_tool_flat_names_still_work(self, tmp_path):
        result = asyncio.run(server.call_tool("list_projects", {"directory": str(tmp_path)}))
        assert result and hasattr(result[0], "text")
        assert result[0].text != "Unknown tool: list_projects"
