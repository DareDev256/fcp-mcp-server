"""Grouped tool facade: 7 verbs dispatching into the existing 62 handlers."""
import asyncio

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
