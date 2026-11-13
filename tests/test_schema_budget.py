"""The advertised tool schema has a budget, and the budget only moves down.

Issue #2 cut the advertised surface to about 5,000 characters by grouping
62 flat tools into seven verbs. By 0.30 it had grown back to 30,714
characters for 15 tools, `edit` alone 15,684, and no test noticed, because
nothing held a number. Every character here is paid by every client on
every conversation, before a single tool is called.

The measure is SDK-independent: for each advertised tool, the compact JSON
of what this server defines for it (name, description, input schema and
``_meta`` when present), summed. The two SDK generations serialise their
``Tool`` models differently; the server's own definitions do not change
with them.

A ratchet: growing past BUDGET fails. Shrinking well under it also fails,
with the new number to write in, so a diet is locked in the release that
earns it instead of becoming room for the next regrowth.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server
from fcpxml.mcp_compat import tool_input_schema, tool_meta

# Measured 2026-11-12 on 0.31.0: 15 tools. Lower it when the schema shrinks;
# never raise it to make a test pass. 0.32 plans the `edit` diet (under 12,000).
BUDGET = 30_714
SLACK = 500  # how far under BUDGET a release may land before it must tighten it


def tool_chars(tool) -> int:
    doc = {"name": tool.name, "description": tool.description or "",
           "inputSchema": tool_input_schema(tool)}
    meta = tool_meta(tool)
    if meta:
        doc["_meta"] = meta
    return len(json.dumps(doc, separators=(",", ":")))


def measure(tools) -> int:
    return sum(tool_chars(t) for t in tools)


def verdict(size: int, budget: int = BUDGET) -> str:
    if size > budget:
        return f"advertised schema is {size} characters, over the budget of {budget}"
    if size < budget - SLACK:
        return f"advertised schema shrank to {size}; lower BUDGET in tests/test_schema_budget.py to {size}"
    return ""


@pytest.fixture
def advertised(monkeypatch):
    monkeypatch.delenv("FCP_MCP_LEGACY_TOOLS", raising=False)
    return asyncio.run(server.list_tools())


def test_the_advertised_schema_is_within_budget(advertised):
    assert len(advertised) == len(server.TOOL_GROUPS)
    assert verdict(measure(advertised)) == ""


def test_the_budget_goes_red_on_one_more_character(advertised):
    """Mutation check: one character added to one description is caught."""
    from fcpxml.mcp_compat import build_tool

    first = advertised[0]
    grown = build_tool(first.name, (first.description or "") + "x", tool_input_schema(first), tool_meta(first))
    size = measure([grown, *advertised[1:]])
    assert size == measure(advertised) + 1
    assert verdict(size, budget=measure(advertised)).startswith("advertised schema is")


def test_the_ratchet_asks_to_be_tightened():
    assert "lower BUDGET" in verdict(BUDGET - SLACK - 1)
    assert verdict(BUDGET) == "" and verdict(BUDGET - SLACK) == ""
