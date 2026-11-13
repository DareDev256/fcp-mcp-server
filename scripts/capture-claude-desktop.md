# Capture the timeline in Claude Desktop (0.31 baseline)

The `view` timeline has been drawn by the official `ext-apps` reference host
and by stub hosts, never by Claude Desktop. This is the one capture that
needs a person at the Mac: it opens a GUI app and its window. Until it is
done, the README's host table says "not yet captured here" for Claude
Desktop, and the release notes say "not verified in Claude Desktop".

Done when: two PNGs exist, the app drawn inline in a Claude Desktop
conversation at a wide and a narrow window, or a written cause for why it
does not draw. Either outcome is a result; "it probably works" is not.

## 1. A fixture with real thumbnails

From the repository root, on the branch being checked:

```bash
uv run --with pillow demo/hero/build.py      # rebuilds demo/hero/.work (media + four cuts)
ls demo/hero/.work/project/cut_broll.fcpxml   # the finished cut the hero ends on
```

`examples/music-video.fcpxml` also works (three lanes, no media, so no
thumbnails). Use both: one shows thumbnails, one shows lanes.

## 2. Point Claude Desktop at this checkout

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (keep
a copy of the file first) and add, with the absolute path of this checkout:

```json
{
  "mcpServers": {
    "fcpxml-dev": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/fcp-mcp-server", "run", "server.py"],
      "env": {
        "FCP_PROJECTS_DIR": "/ABSOLUTE/PATH/TO/fcp-mcp-server/demo/hero/.work/project"
      }
    }
  }
}
```

Quit Claude Desktop completely (Cmd-Q) and open it again; the server list in
Settings -> Developer should show `fcpxml-dev` as running.

## 3. Ask for the timeline

In a new conversation:

> Call the `view` tool with action `view_timeline` and filepath
> `/ABSOLUTE/PATH/TO/fcp-mcp-server/demo/hero/.work/project/cut_broll.fcpxml`.

Then the same for `examples/music-video.fcpxml`. Click one clip in the app
and press **Inspect clip**: that proves the app can call back into the
server through the host, not only draw.

## 4. Capture by window id, without taking focus

`screencapture -l` captures one window by its id and does not need the
window in front. Find the id:

```bash
uv run --with pyobjc-framework-Quartz python - <<'PY'
import Quartz
for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
    if w.get("kCGWindowOwnerName") == "Claude" and w.get("kCGWindowLayer") == 0:
        b = w["kCGWindowBounds"]
        print(w["kCGWindowNumber"], int(b["Width"]), "x", int(b["Height"]), w.get("kCGWindowName", ""))
PY
```

Resize the Claude window to about 1440 points wide, then:

```bash
screencapture -x -o -l <WINDOW_ID> docs/screenshots/2026-11-12_claude-desktop-1440.png
```

Narrow the window as far as Claude Desktop allows (note the minimum width it
stops at; it may be wider than 390) and capture again as
`docs/screenshots/2026-11-12_claude-desktop-narrow.png`. Open both PNGs and
look: the app inside the conversation, thumbnails present, no clipped labels
or overlapping boxes. Use the date of the capture in the file names.

## 5. If the app does not draw

Record which of these is true, in this order, before changing any code:

1. The tool result shows as text with a JSON block and no frame: the host did
   not treat `view` as an app tool. Read `~/Library/Logs/Claude/mcp-server-fcpxml-dev.log`
   for a `resources/read` of `ui://fcp/timeline`; none means the host never
   asked for the shell.
2. A frame appears but stays on "Waiting for a timeline": the shell loaded
   and the tool result never reached it. Note the Claude Desktop version
   (Claude -> About) and keep the log.
3. A frame appears and is blank: a sandbox or CSP refusal inside the iframe.
   The shell loads nothing from the network, so note any console error.

Then repeat step 3 with the server on `mcp` 2.x, which is the only SDK that
can advertise the MCP Apps extension in the handshake:

```json
"args": ["--directory", "/ABSOLUTE/PATH/TO/fcp-mcp-server", "run", "--with", "mcp>=2.0.0", "server.py"]
```

If 2.x draws and 1.x does not, that is the finding: the README host table
says so, and the 0.33 plan starts from it.

## 6. Afterwards

- Put the original `claude_desktop_config.json` back if `fcpxml-dev` should
  not stay.
- Update the Claude Desktop row of "Where the timeline draws" in `README.md`
  with the capture date and the two image paths (or the cause found).
- The PNGs are evidence in `docs/screenshots/`; they are not marketing.
