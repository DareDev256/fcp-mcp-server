"""The tools/ package is how new groups reach the MCP surface.

New subsystems register here instead of growing server.py's 4,585-line
dispatch. The existing surface is asserted unchanged: a group that fails to
register must not silently shadow a working tool, and the seven builtin
groups must advertise exactly as they did before.
"""

import asyncio

import server
import tools


def test_extra_groups_have_the_same_shape_as_builtin_groups():
    for name, spec in tools.EXTRA_GROUPS.items():
        assert isinstance(spec.get("description"), str) and spec["description"], name
        assert isinstance(spec.get("actions"), list) and spec["actions"], name


def test_every_extra_action_resolves_to_a_real_handler():
    for name, spec in tools.EXTRA_GROUPS.items():
        for action in spec["actions"]:
            assert action in tools.EXTRA_HANDLERS, f"{name}.{action} has no handler"


BUILTIN_GROUPS = {
    "inspect", "diagnose", "edit", "mark", "generate", "transcript", "deliver",
}


def test_the_merge_keeps_every_builtin_group():
    """server.py folds tools/ groups in. Nothing may be displaced doing it."""
    assert BUILTIN_GROUPS <= set(server.TOOL_GROUPS)


def test_every_extra_group_reached_the_server_intact():
    for name, spec in tools.EXTRA_GROUPS.items():
        assert server.TOOL_GROUPS[name] == spec


def test_every_extra_handler_reached_the_server():
    for action, handler in tools.EXTRA_HANDLERS.items():
        assert server.TOOL_HANDLERS[action] is handler


def test_an_extra_group_may_not_shadow_a_builtin_one():
    """The merge raises rather than overwriting. Exercise the real guard."""
    import pytest

    async def noop(args):
        return []

    into_groups = {"edit": {"description": "builtin", "actions": ["trim_clip"]}}
    with pytest.raises(RuntimeError, match="shadows a builtin group"):
        server._merge_extra_tools(
            {"edit": {"description": "impostor", "actions": ["x"]}}, {},
            into_groups, {},
        )
    assert into_groups["edit"]["description"] == "builtin", "must not partially apply"


def test_an_extra_action_may_not_shadow_a_builtin_one():
    import pytest

    async def noop(args):
        return []

    into_handlers = {"trim_clip": noop}
    with pytest.raises(RuntimeError, match="shadows a builtin action"):
        server._merge_extra_tools({}, {"trim_clip": noop}, {}, into_handlers)


def test_a_clean_merge_applies_both_registries():
    async def noop(args):
        return []

    groups, handlers = {}, {}
    server._merge_extra_tools(
        {"newgroup": {"description": "d", "actions": ["newaction"]}},
        {"newaction": noop}, groups, handlers,
    )
    assert groups["newgroup"]["actions"] == ["newaction"]
    assert handlers["newaction"] is noop


def test_the_server_module_is_bound():
    """Group handlers reach server through the bound module, never an import."""
    assert tools.server_module() is server


def test_registering_a_duplicate_group_raises():
    """A silent overwrite would shadow a working tool with no symptom."""
    import pytest

    async def noop(args):
        return []

    tools.register_group("seam_probe_group", "probe", {"seam_probe_action": noop})
    try:
        with pytest.raises(ValueError, match="already registered"):
            tools.register_group("seam_probe_group", "probe", {"other": noop})
        with pytest.raises(ValueError, match="already registered"):
            tools.register_group("seam_probe_two", "probe", {"seam_probe_action": noop})
    finally:
        tools.EXTRA_GROUPS.pop("seam_probe_group", None)
        tools.EXTRA_HANDLERS.pop("seam_probe_action", None)


def test_unknown_group_still_reports_valid_groups():
    result = asyncio.run(server.handle_group("nope", {}))
    text = result[0].text
    assert "Unknown tool group" in text
    for name in server.TOOL_GROUPS:
        assert name in text


def test_builtin_groups_are_all_still_advertised():
    advertised = {t.name for t in asyncio.run(server.list_tools())}
    for name in server.TOOL_GROUPS:
        assert name in advertised
    for name in tools.EXTRA_GROUPS:
        assert name in advertised
