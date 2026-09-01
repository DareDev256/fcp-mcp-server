"""New MCP tool groups, defined outside server.py.

server.py is 4,585 lines holding every tool definition, every handler, the
seven builtin TOOL_GROUPS, the QC helpers and the resource/prompt wiring. New
subsystems land here instead of growing it further; the existing handlers stay
where they are until a dedicated migration.

This package is a REGISTRY ONLY. server.py merges EXTRA_GROUPS and
EXTRA_HANDLERS into its own dicts at import time, so every existing code path
(list_tools, handle_group, call_tool, _action_param_help, the error messages)
keeps working against one source of truth rather than two that can disagree.

Nothing here may import server at module scope. server.py imports this package,
and in production server.py runs as __main__ — so a module-scope `import server`
would execute a SECOND copy of the whole module under a different name. Group
modules use tools._common instead.
"""

from typing import Any, Awaitable, Callable

Handler = Callable[[dict], Awaitable[list]]

EXTRA_GROUPS: dict[str, dict[str, Any]] = {}
EXTRA_HANDLERS: dict[str, Handler] = {}


def register_group(name: str, description: str, actions: dict[str, Handler]) -> None:
    """Register a tool group and its action handlers.

    Raises on a duplicate group or action name. A silent overwrite would shadow
    a working tool and produce a bug with no symptom at import time.
    """
    if name in EXTRA_GROUPS:
        raise ValueError(f"tool group already registered: {name}")
    for action in actions:
        if action in EXTRA_HANDLERS:
            raise ValueError(f"tool action already registered: {action}")
    EXTRA_GROUPS[name] = {"description": description, "actions": list(actions)}
    EXTRA_HANDLERS.update(actions)


def _register_all() -> None:
    """Import the group modules so their register_group() calls run.

    Called at import. Group modules are added here as they land.
    """
    return


_register_all()
