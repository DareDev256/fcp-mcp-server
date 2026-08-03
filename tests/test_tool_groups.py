"""Grouped tool facade: 7 verbs dispatching into the existing 62 handlers."""
import asyncio

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
        assert "nonexistent" in result[0].text
