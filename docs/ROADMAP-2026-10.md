# Roadmap — October 2026 (v0.26.0 "See the cut")

The idea set behind v0.26.0: what shipped, what was left out on purpose, and
where each idea came from. Sources are dated 2026-10-09 unless stated;
third-party prices and competitor claims are reported, not verified.

| # | Idea | Status | Where |
|---|------|--------|-------|
| 1 | MCP Apps timeline (`ui://fcp/timeline`) | **Shipped** | `view` group, `fcpxml/timeline_app.py` |
| 2 | Generative fill, quote-then-confirm | **Shipped** (fal Kling/Wan, Gemini Veo; Runway/Luma stubs) | `gen` group, `fcpxml/gen.py` |
| 3 | Project guard (`expected_sha256`) | **Shipped** | `server._project_guard` |
| 4 | OpenTimelineIO export | Not built | — |
| 5 | Text-based transcript editing inside the app | Not built | — |
| 6 | `@`-pinning a clip in a prompt | Not built | — |
| 7 | Captions for FCP 12.3 (Generate Captions round-trip) | Not built | — |
| 8 | Edit Detection import (FCP 12.3) | Not built | — |
| 9 | FCPXML 1.14 writing | Not built (reads 1.14, writes 1.13) | — |
| 10 | Runway Aleph / Luma Ray Modify video-to-video | Quoted, refuses honestly | `gen.NotImplementedProvider` |
| 11 | OTIO / Resolve handoff | Not built | — |
| 12 | Cost ledger for generated media | Not built | — |

## 1. See the cut — the MCP Apps timeline (shipped)

**What.** A `view` tool whose every result a host with MCP Apps renders inline:
lanes, clips sized to duration with names and roles, markers, gaps, a
timecode ruler with a playhead and range selection, cached transcript words
under the clip they belong to, per-clip filmstrip thumbnails when ffmpeg can
make them. Clicking a clip shows its details; the buttons call back through
the host (`tools/call`) — `view_clip` and `find_shots` for reads,
`mark.add_marker` and `edit.delete_clips` for edits — and after an edit the
app re-renders from the new output file. Hosts without Apps get a summary and
the JSON payload as text.

**Why a new group.** `_meta.ui.resourceUri` binds the UI to a *tool*. On
`preview` it would open the iframe for `preview_render` and `preview_check`
too, whose results carry no payload. A tool whose every result is renderable
keeps the contract simple.

**What was verified and what was not.** `_meta.ui.resourceUri`, the MIME type
`text/html;profile=mcp-app`, the extension id `io.modelcontextprotocol/ui`
and the client gate (`capabilities.extensions[id].mimeTypes`) are read from
the installed 2.x SDK's `mcp/server/apps.py`. The in-iframe postMessage
method names (`ui/initialize`, `ui/notifications/initialized`,
`ui/notifications/tool-result`, `ui/notifications/tool-input`,
`tools/call`) come from the SEP-1865 spec text and the
`@modelcontextprotocol/ext-apps` client; the Python SDK ships only the server
half, so they could not be checked against installed source. The app was
rendered and driven in headless Chromium through a stub host
(`docs/screenshots/2026-10-09_timeline-app-*.png`); it has not been rendered
by a real host in this release. On `mcp` 1.x the tool still carries
`_meta.ui` and the resource is served, but `ServerCapabilities` has no
`extensions` slot, so the up-front handshake cannot be advertised there.

**Source.** MCP Apps SEP-1865; python-sdk 2.x `docs/advanced/apps.md` and
`mcp/server/apps.py`; `@modelcontextprotocol/ext-apps` v2.0.3 (npm).

## 2. Generative fill, quote-then-confirm (shipped)

**What.** `gen_quote` prices a job from a dated table and returns an id;
`gen_fill_gap` / `gen_broll` / `gen_extend_clip` refuse without
`confirm=true`, a matching quote id, the provider's key and a price under
`FCP_MCP_GEN_MAX_USD` (default 5). The media is ffprobed for its real fps and
length, lands in `generated/` beside the project, and is attached as a
connected clip with role `generated` and a marker naming prompt and provider.

**Prices (per generated second, upper end of the range seen, unverified):**
Veo 3.1 $0.40 (Gemini API; $0.10–0.40 by tier/audio), Kling 2.5 $0.07 (fal;
$0.04–0.07), Wan 2.5 $0.15 (fal; $0.05–0.15), Runway Aleph $0.15 (15
credits/s at $0.01), Luma Ray Modify — no per-second figure published, so no
price on file and the quote says so. `prices_as_of` is in every quote.

**Left out.** Runway and Luma are video-to-video: the input is a clip, not a
prompt, and doing it cleanly means uploading source media to a third party
plus a different conform path. They quote and refuse rather than pretend.

**Source.** Google Gemini API pricing page; fal.ai model pages; Runway credit
table; Luma Ray Modify page. All third-party, read 2026-10-09.

## 3. Project guard (shipped)

**What.** Every action that takes a filepath accepts `expected_sha256` and
refuses on mismatch; every write answers with the hash of what it wrote;
`analyze_timeline`, `view_timeline` and `history` report the current one.

**Source.** A Premiere MCP issue proposing an `expected_project_path` guard
against editing the wrong project. A content hash guards against that and
against the subtler case where the path is right and the file changed.

## 4 / 11. OpenTimelineIO export and the Resolve handoff (not built)

Optional item 4 of this release; left out to ship 1–3 fully tested on both
SDK generations instead. The clean shape is an `export_otio` deliver action
behind an `[otio]` extra, converting the spine/lane model to OTIO tracks the
way `fcpxml/export.py` already converts to XMEML — and it must go through the
review gate like the other exports. `davinci-resolve-mcp` (3,430★) is the
reason to care: OTIO is the interchange Resolve reads natively.

## 5. Text-based transcript editing in the app (not built)

Descript's Underlord edits video by editing a transcript. The payload already
carries words positioned on the timeline; the missing pieces are selection
of a word range in the app and a call to `transcript.edit_by_transcript`
(which exists) with that range. A small addition on the app side; left out
to keep the first app release reviewable.

## 6. `@`-pinning a clip in prompts (not built)

Descript lets a prompt reference a clip with `@`. For an MCP server this is
host-side UX (the model already addresses clips by name); the server-side
half would be a stable clip id in the payload (it has `s0`/`c3` ids now) that
the groups accept in place of a name. Names collide; ids would not.

## 7. Captions for FCP 12.3 (not built)

FCP 12.3 added Generate Captions. The parser already reads `<caption>`; a
round-trip would write cached transcript words as captions with roles.
Needs a DTD-valid fixture from a real 12.3 export first.

## 8. Edit Detection import (not built)

FCP 12.3's Edit Detection cuts a flattened render at its edits. `scenes`
already detects shot boundaries from pixels; an import would read FCP's
detected cuts out of an exported FCPXML instead. No automation API was added
in 12.0–12.4, so this stays an XML read.

## 9. FCPXML 1.14 writing (not built)

Reads 1.14, writes 1.13. Writing 1.14 needs the 1.14 DTD differences
enumerated (cinematic/stereo-3D/colorConform elements are already in the
child-order table) and a fixture from FCP 12.4.

## 12. Cost ledger (not built)

Every `gen_*` call journals the media it wrote and the result text carries the
estimated cost, but nothing sums them. A `gen_ledger` action reading the
journal's gen rows and totalling `estimated_usd` per folder is the obvious
next step, and belongs in the journal rather than a second file.

## Competitive context (reported, not verified)

SpliceKit (in-process FCP injection, 166★), dreliq9/fcp-mcp, the Premiere MCPs
(668★), davinci-resolve-mcp (3,430★). This server's position is unchanged:
XML in, XML out, everything journaled and hash-checked, live push through
Apple events, and now an inline view of the cut.
