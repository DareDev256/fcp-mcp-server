"""Print the version the MCP registry marks isLatest for this server.

Reads the search-endpoint JSON on stdin. The endpoint returns EVERY version
of a server, oldest first, so "the first match" is whichever version was
published first (0.13.1 here, forever); the registry marks exactly one entry
isLatest and that is the only one that answers "what does the registry serve".

A file rather than an inline ``python3 -c`` block: v0.22.0's check died of
an IndentationError because the inline body was indented to match the YAML
around it, and Python received that indentation intact.
"""

import json
import sys

NAME = "io.github.DareDev256/fcpxml-mcp-server"
KEY = "io.modelcontextprotocol.registry/official"


def latest_version(payload: dict) -> str:
    latest = [
        e for e in payload.get("servers", [])
        if e["server"]["name"] == NAME
        and e.get("_meta", {}).get(KEY, {}).get("isLatest")
    ]
    return latest[0]["server"]["version"] if latest else ""


if __name__ == "__main__":
    print(latest_version(json.load(sys.stdin)))
