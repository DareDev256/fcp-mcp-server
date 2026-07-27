# MCP Ecosystem & Companion Tools

How **FCPXML MCP** fits into the broader Model Context Protocol ecosystem, and which companion servers pair well with video editing workflows.

---

## What Is MCP?

The [Model Context Protocol](https://modelcontextprotocol.io/) is an open standard that lets AI models (like Claude) call tools exposed by local servers. Each MCP server is a specialist — it owns one domain and exposes tools for that domain. Claude Desktop (or any MCP client) can connect to multiple servers simultaneously, composing their capabilities in a single conversation.

```
Claude Desktop
├── fcp-mcp-server       → Final Cut Pro timeline operations
├── gitnexus             → Codebase knowledge graph + architecture analysis
├── filesystem           → General file read/write
└── your-custom-server   → Whatever you build
```

This is the key insight: **MCP servers compose**. You don't need one server that does everything. You need focused servers that each do one thing well, and an AI client that orchestrates them.

---

## Companion: GitNexus

**What it does:** GitNexus indexes a codebase into a knowledge graph — files, functions, classes, imports, dependencies — and exposes that graph via CLI, MCP tools, and a browser-based Web UI. It enables deep architectural understanding without manually tracing call chains.

**Why it's relevant (relevance: 85%):**

| Capability | How It Helps FCPXML MCP Development |
|------------|-------------------------------------|
| **Knowledge graph indexing** | Maps all 47 tool handlers, their dependencies on parser/writer/models, and cross-module call chains — useful for onboarding contributors |
| **Architecture visualization** | Web UI renders the `server.py → fcpxml/*.py` dispatch tree visually, showing which handlers touch which modules |
| **Impact analysis** | Before changing `TimeValue` arithmetic in `models.py`, query the graph to see every downstream consumer — prevents regressions |
| **MCP tool exposure** | Runs as a sibling MCP server alongside FCPXML MCP — Claude can query the codebase graph *and* manipulate timelines in the same session |
| **CLI for CI** | Index the repo in CI, query for orphan functions or circular imports as part of the lint pipeline |

**Example workflow — using both servers together:**

```
User: "I want to add a new export format. Show me how the existing
       export pipeline works, then generate a template FCPXML."

Claude:
  1. [GitNexus] Query knowledge graph for export.py call chain
  2. [GitNexus] Show all functions that call writer.write_fcpxml()
  3. [FCPXML MCP] Generate a sample timeline via auto_rough_cut
  4. [FCPXML MCP] Export it via export_resolve_xml to see the pattern
  → Claude synthesizes the architecture + a working example
```

**Setup (alongside FCPXML MCP):**

```json
{
  "mcpServers": {
    "fcpxml": {
      "command": "uv",
      "args": ["--directory", "/path/to/fcp-mcp-server", "run", "server.py"],
      "env": { "FCP_PROJECTS_DIR": "/Users/you/Movies" }
    },
    "gitnexus": {
      "command": "gitnexus",
      "args": ["mcp", "--repo", "/path/to/fcp-mcp-server"]
    }
  }
}
```

---

## Other Useful Companion Servers

| Server | Domain | Pairing Use Case |
|--------|--------|-----------------|
| **filesystem** | File read/write | Read raw FCPXML files, write export outputs |
| **memory** | Persistent context | Remember project preferences across sessions |
| **fetch** | HTTP requests | Pull beat analysis JSON from remote APIs |
| **sqlite** | Database queries | Track edit history, QC results over time |

---

## Building Your Own MCP Server

If GitNexus and FCPXML MCP inspire you to build a domain-specific server, the pattern is straightforward:

1. **Pick a domain** — one data format, one API, one workflow
2. **Define tools** — each tool is a function with typed inputs and text outputs
3. **Use the MCP SDK** — `pip install mcp`, subclass `Server`, register handlers
4. **Test locally** — `uv run server.py` starts the server, Claude Desktop connects

See [server.py](../server.py) in this repo for a production example of the dispatch-dict pattern with 47 tools.

---

*Last updated: 2026-02-25*
