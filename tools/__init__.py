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

from types import ModuleType
from typing import Any, Awaitable, Callable, Optional

Handler = Callable[[dict], Awaitable[list]]

# The server module object, handed over by server.py at import.
#
# Group modules need server's helpers — _text_result, _parse_project, and the
# sandbox-enforcing path validators. They must NOT `import server` to get them:
# in production server.py runs as __main__, so that import would execute a
# SECOND copy of the whole module under a different name, with its own
# TOOL_HANDLERS and its own sandbox state. Binding the live module object
# instead means there is exactly one, whatever it happens to be called, and the
# security helpers are shared rather than duplicated into a copy that drifts.
_SERVER: Optional[ModuleType] = None


def bind_server(module: ModuleType) -> None:
    """Hand the live server module to the group modules. Called by server.py."""
    global _SERVER
    _SERVER = module


def server_module() -> ModuleType:
    """The bound server module.

    Raises rather than importing a fallback: a missing binding is a wiring bug,
    and importing our way out of it would produce the duplicate-module state
    this exists to prevent.
    """
    if _SERVER is None:
        raise RuntimeError(
            "tools.bind_server() was never called — the server module is not "
            "bound, so tool handlers cannot reach its helpers."
        )
    return _SERVER

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
    from tools import index as _idx
    from tools import preview as _preview
    from tools import watch as _watch

    register_group("preview", _preview.DESCRIPTION, _preview.ACTIONS)
    register_group("watch", _watch.DESCRIPTION, _watch.ACTIONS)
    register_group("index", _idx.DESCRIPTION, _idx.ACTIONS)


_register_all()
