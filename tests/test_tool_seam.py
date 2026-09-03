"""The tools/ package is how new groups reach the MCP surface.

New subsystems register here instead of growing server.py's 4,811-line
dispatch, and existing families migrate out of it a slice at a time. The existing surface is asserted unchanged: a group that fails to
register must not silently shadow a working tool, and the seven builtin
groups must advertise exactly as they did before.
"""

import asyncio
from pathlib import Path

import server
import tools

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.fcpxml"


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


def test_every_flat_handler_is_named_by_some_test():
    """A handler with no test that names it is a handler nobody checks.

    A sweep on 2026-09-02 found 22 of 64 in exactly that state — the code
    underneath them was covered, the handlers were not, so a whole family
    could stop resolving while the suite stayed green. Closing the list once
    is worth little if the next handler lands the same way, so the floor is
    asserted here rather than remembered.

    This is a NAME check, not a coverage measurement: it proves a test
    mentions the tool, not that it asserts anything useful. It is the cheap
    guard that keeps the expensive one honest.
    """
    import pathlib
    import re

    source = pathlib.Path(server.__file__).read_text()
    block = re.search(r'^TOOL_HANDLERS = \{(.*?)^\}', source, re.S | re.M)
    assert block, "TOOL_HANDLERS is no longer a module-level literal"
    names = re.findall(r'"([a-z0-9_]+)":', block.group(1))
    assert len(names) > 50, f"only found {len(names)} handlers — the regex missed some"

    tests_dir = pathlib.Path(__file__).parent
    corpus = "\n".join(p.read_text() for p in tests_dir.glob("test_*.py"))
    unnamed = [
        n for n in names
        if f'"{n}"' not in corpus and f"'{n}'" not in corpus and f"handle_{n}" not in corpus
    ]
    assert not unnamed, (
        f"{len(unnamed)} handler(s) have no test naming them: {', '.join(unnamed)}"
    )


def test_group_call_accepts_arguments_sent_flat():
    """`{"action": ..., "filepath": ...}` instead of the nested `args` form.

    The schema asks for `args`, but a caller that sends the arguments flat
    got "Missing required argument: filepath" for a call that visibly passed
    filepath, which is the worst possible error: it points at the argument
    rather than at the nesting. The flat form is now taken as written.
    """
    result = asyncio.run(
        server.call_tool("inspect", {"action": "analyze_timeline",
                                     "filepath": str(SAMPLE)})
    )
    text = result[0].text
    assert "Missing required argument" not in text
    assert "clip" in text.lower()


def test_nested_args_still_win_over_flat_keys():
    """A control: with `args` present, the flat path must not run at all —
    otherwise the leniency above quietly changes what a correct caller sends.
    """
    result = asyncio.run(
        server.call_tool("inspect", {"action": "analyze_timeline",
                                     "args": {"filepath": str(SAMPLE)},
                                     "filepath": "/nonexistent/should-be-ignored.fcpxml"})
    )
    assert "should-be-ignored" not in result[0].text
