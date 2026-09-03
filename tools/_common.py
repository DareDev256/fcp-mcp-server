"""Helpers shared by the tool-group modules.

Everything here routes through the bound server module rather than
reimplementing it. The path validators in particular enforce the sandbox roots
that confine every read and write; a second copy of that logic in this package
would be a second thing to keep correct, and the copy is what would drift.
"""

from typing import Any

import tools


def text_result(text: str) -> list:
    """Wrap a string in the MCP TextContent list every handler returns."""
    return tools.server_module()._text_result(text)


def parse_project(filepath: str) -> tuple[Any, Any]:
    """Parse an FCPXML file into (project, primary_timeline).

    Goes through the server's validator, so the extension whitelist and the
    sandbox roots apply exactly as they do to every builtin tool.
    """
    return tools.server_module()._parse_project(filepath)
