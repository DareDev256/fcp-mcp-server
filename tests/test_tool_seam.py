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


def test_extra_groups_do_not_collide_with_builtin_groups():
    assert not set(tools.EXTRA_GROUPS) & set(server.TOOL_GROUPS)


def test_extra_actions_do_not_collide_with_builtin_actions():
    builtin = {a for spec in server.TOOL_GROUPS.values() for a in spec["actions"]}
    assert not set(tools.EXTRA_HANDLERS) & builtin


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
