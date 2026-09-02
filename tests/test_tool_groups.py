"""Grouped tool facade: 7 verbs dispatching into the existing 62 handlers."""
import asyncio
import json
import re

import pytest

import server
from fcpxml.mcp_compat import tool_input_schema


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
        schema = tool_input_schema(tool)
        assert schema["properties"]["action"]["enum"] == actions
        assert schema["required"] == ["action"]
        assert schema["properties"]["args"]["type"] == "object"


class TestGroupCoverage:
    EXPECTED_GROUPS = {
        "inspect", "diagnose", "edit", "mark", "generate", "transcript", "deliver",
    }

    def test_all_seven_groups_present(self):
        """The seven builtin groups, plus whatever tools/ registered.

        server.py merges tools/ groups into TOOL_GROUPS at import, so this set
        grows as new subsystems land. The builtin seven must all still be here:
        a group vanishing is the regression this guards.
        """
        import tools

        assert self.EXPECTED_GROUPS <= set(server.TOOL_GROUPS)
        assert set(server.TOOL_GROUPS) == self.EXPECTED_GROUPS | set(tools.EXTRA_GROUPS)

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
        """Fourteen verbs is still a reduction from 62 flat tools.

        The cap exists so grouping does not quietly un-group itself one new
        verb at a time. It is not a limit on capability: TOOL_HANDLERS grows
        freely underneath. Raised 12 -> 14 in v0.19.0 for organize + find,
        each of which carries several actions — a verb, not a tool.
        """
        assert len(server.TOOL_GROUPS) <= 14
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
                [{"n": t.name, "d": t.description, "s": tool_input_schema(t)} for t in ts]
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


class TestPromptsUseGroupedToolNames:
    """The 5 MCP prompts must only name tools the model can actually see.

    Since v0.14.0 `list_tools` advertises 7 grouped verbs. A prompt that says
    "use `validate_timeline`" points the model at a name absent from its tool
    list — it still dispatches, but it teaches the model to guess at tools,
    which is the exact behaviour this branch exists to stop.
    """

    # "`group` with action `action`" — the only sanctioned shape.
    PAIR = re.compile(r"`([a-z_]+)`\s+with\s+action\s+`([a-z_0-9]+)`")

    @staticmethod
    def _prompt_texts():
        prompts = asyncio.run(server.list_prompts())
        assert prompts, "expected built-in prompts"
        texts = {}
        for p in prompts:
            args = {a.name: f"<{a.name}>" for a in (p.arguments or [])}
            result = asyncio.run(server.get_prompt(p.name, args))
            texts[p.name] = "\n".join(
                m.content.text for m in result.messages
            )
        return texts

    def test_every_named_action_is_reachable_from_the_named_group(self):
        for prompt_name, text in self._prompt_texts().items():
            pairs = self.PAIR.findall(text)
            assert pairs, f"prompt '{prompt_name}' names no grouped tool call"
            for group, action in pairs:
                assert group in server.TOOL_GROUPS, (
                    f"prompt '{prompt_name}' names unknown group '{group}'"
                )
                assert action in server.TOOL_GROUPS[group]["actions"], (
                    f"prompt '{prompt_name}' tells the model to call "
                    f"'{group}' with action '{action}', which is not reachable "
                    f"from that group"
                )
                assert action in server.TOOL_HANDLERS, (
                    f"prompt '{prompt_name}' names action '{action}' with no handler"
                )

    def test_no_prompt_names_a_bare_flat_tool(self):
        """A backticked flat tool name outside the grouped form is a regression."""
        flat_names = set(server.TOOL_HANDLERS)
        for prompt_name, text in self._prompt_texts().items():
            stripped = self.PAIR.sub("", text)
            for name in re.findall(r"`([a-z_][a-z_0-9]*)`", stripped):
                assert name not in flat_names, (
                    f"prompt '{prompt_name}' names flat tool '{name}' outside "
                    f"the grouped form — the model cannot see that tool"
                )


class TestMissingArgumentIsRecoverable:
    """A grouped call hides each action's required params, so the caller guesses.

    Most handlers take `filepath`; a few take something else (`media_path` on
    the beat tools). A bare `Error: KeyError` gives the caller nothing to
    correct, which is a dead end the flat schema never had.
    """

    def test_missing_arg_names_the_key_and_the_accepted_params(self):
        result = asyncio.run(
            server.call_tool("diagnose", {"action": "detect_beats", "args": {}})
        )
        text = result[0].text
        assert "KeyError" not in text, "the raw exception name is not actionable"
        assert "media_path" in text, "must name the parameter the action wants"
        assert "required" in text

    def test_wrong_arg_name_is_corrected(self):
        """Guessing `filepath` for a tool that wants `media_path` must teach."""
        result = asyncio.run(
            server.call_tool(
                "diagnose", {"action": "detect_beats", "args": {"filepath": "/x.wav"}}
            )
        )
        text = result[0].text
        assert "media_path" in text
        assert "KeyError" not in text

    def test_flat_call_gets_the_same_help(self):
        result = asyncio.run(server.call_tool("detect_beats", {}))
        assert "media_path" in result[0].text

    def test_help_marks_optional_params_as_optional(self):
        help_text = server._action_param_help("detect_silence_candidates")
        assert "filepath (required)" in help_text
        assert "(optional)" in help_text

    def test_unknown_action_yields_no_help_rather_than_raising(self):
        assert server._action_param_help("no_such_action") == ""
        assert server._action_param_help(None) == ""
