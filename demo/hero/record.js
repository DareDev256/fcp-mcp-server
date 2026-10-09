// Stub MCP Apps host for the README hero. Run by build.py; not meant to be run alone.
//
//   node demo/hero/record.js <work dir> <frames dir>
//
// Loads the ui://fcp/timeline shell (work/app.html, exactly as the server
// serves it) once per timeline state, speaks the MCP Apps draft protocol to
// each copy (ui/initialize -> ui/notifications/initialized -> the real
// view_timeline result as ui/notifications/tool-result), and lays a chat
// column beside it. The page is a pure function of time: window.__seek(ms)
// positions every element, so each captured frame is exact and a slow
// screenshot cannot drift the animation. Frames are 960x540 CSS pixels at a
// device scale of 2 (1920x1080) so the app's own 13px type stays legible at
// README width.
"use strict";
const fs = require("fs");
const path = require("path");

function loadPlaywright() {
  const hint = process.env.HERO_PLAYWRIGHT;
  for (const candidate of [hint, "playwright"].filter(Boolean)) {
    try { return require(candidate); } catch (e) { /* try the next one */ }
  }
  throw new Error("playwright not found: npm i playwright, or HERO_PLAYWRIGHT=/path/to/node_modules/playwright");
}

const [work, framesDir] = process.argv.slice(2);
const states = JSON.parse(fs.readFileSync(path.join(work, "states.json"), "utf8"));
const app = fs.readFileSync(path.join(work, "app.html"), "utf8");
const FPS = 30;

const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
const iframes = states.states.map((s, i) =>
  `<iframe class="app" id="app${i}" sandbox="allow-scripts" srcdoc="${esc(app)}"></iframe>`).join("");

const host = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { --accent:#00e5ff; --mute:#8c8c96; --ink:#f2f2f5; --line:#26262c; }
  html,body { margin:0; width:960px; height:540px; background:#000; color:var(--ink); overflow:hidden;
    font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #top { position:absolute; left:28px; right:28px; top:16px; display:flex; justify-content:space-between;
    font: 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--mute); letter-spacing:.04em; }
  #top b { color:var(--ink); font-weight:600; }
  #chat { position:absolute; left:28px; top:48px; width:292px; bottom:28px; }
  #you { opacity:0; }
  .who { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--mute); margin-bottom:6px; }
  #prompt { font-size:16px; line-height:1.35; font-weight:500; letter-spacing:-.01em; min-height:66px; }
  #prompt .caret { display:inline-block; width:2px; height:16px; background:var(--accent); vertical-align:-2px; margin-left:1px; }
  #claude { margin-top:18px; }
  .line { opacity:0; transform:translateY(6px); margin-bottom:8px; }
  .call { font: 600 12.5px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--ink); white-space:nowrap; }
  .call:before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--accent);
    margin-right:8px; vertical-align:1px; }
  .note { color:var(--mute); font-size:11.5px; margin-left:14px; margin-top:1px; white-space:nowrap; }
  #mock { position:absolute; left:0; bottom:0; width:292px; color:var(--mute); font-size:10.5px; line-height:1.35;
    opacity:0; border-top:1px solid var(--line); padding-top:7px; }
  #stage { position:absolute; left:332px; top:48px; right:28px; bottom:28px; }
  #frame { position:absolute; inset:0; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:#09090b; }
  .app { position:absolute; inset:0; width:100%; height:100%; border:0; opacity:0; background:#09090b; }
  #wipe { position:absolute; top:0; bottom:0; width:2px; background:var(--accent); opacity:0; z-index:6;
    box-shadow:0 0 12px 2px rgba(0,229,255,.55); }
  #cap { position:absolute; right:14px; bottom:10px; font: 600 11px/1 ui-monospace, Menlo, monospace; color:var(--accent);
    background:rgba(9,9,11,.92); padding:6px 9px; border-radius:6px; border:1px solid var(--line); opacity:0; z-index:5; }
  #empty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--mute);
    font-size:13px; background:#09090b; }
</style></head><body>
<div id="top"><span><b>fcp-mcp-server</b> · watch Claude cut</span><span>ui://fcp/timeline · MCP Apps · 1920×1080</span></div>
<div id="chat">
  <div id="you"><div class="who">You</div><div id="prompt"></div></div>
  <div id="claude"><div class="who" id="cwho" style="opacity:0">Claude · tool calls</div>
    ${states.lines.map((l, i) => `<div class="line" id="l${i}"><div class="call">${l.call}</div><div class="note">${l.note}</div></div>`).join("")}
  </div>
  <div id="mock">${states.mock_note}</div>
</div>
<div id="stage"><div id="frame"><div id="empty">Waiting for a timeline. Call the <b>&nbsp;view&nbsp;</b> tool with view_timeline and a filepath.</div>${iframes}<div id="wipe"></div><div id="cap"></div></div></div>
<script>
  const STATES = ${JSON.stringify(states.states)};
  const LINES = ${JSON.stringify(states.lines)};
  const PROMPT = ${JSON.stringify(states.prompt)};
  window.__ready = {};
  const frames = STATES.map((s, i) => document.getElementById("app" + i));
  // Each iframe is a separate app instance; route replies by source window.
  window.addEventListener("message", (ev) => {
    const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
    const i = frames.findIndex((f) => f.contentWindow === ev.source); if (i < 0) return;
    const post = (msg) => frames[i].contentWindow.postMessage(msg, "*");
    if (m.method === "ui/initialize") { post({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2025-11-21", hostContext: { theme: "dark" } } }); return; }
    if (m.method === "ui/notifications/initialized") {
      post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { result: { content: [{ type: "text", text: STATES[i].result }] } } });
      window.__ready[i] = true; return;
    }
    if (m.method === "tools/call") post({ jsonrpc: "2.0", id: m.id, result: { content: [{ type: "text", text: "(stub host: not recorded)" }] } });
  });
  const clamp = (v) => Math.max(0, Math.min(1, v));
  const ease = (v) => { v = clamp(v); return v < .5 ? 2 * v * v : 1 - Math.pow(-2 * v + 2, 2) / 2; };
  const T = { youIn: 0.8, typeStart: 1.1, typeEnd: 3.9, claudeIn: 4.3, fade: 0.55 };
  window.__seek = function (ms) {
    const t = ms / 1000;
    document.getElementById("you").style.opacity = ease((t - T.youIn) / 0.4);
    const n = Math.round(clamp((t - T.typeStart) / (T.typeEnd - T.typeStart)) * PROMPT.length);
    const caret = t >= T.youIn && t < T.typeEnd + 0.6 && (Math.floor(t * 2.5) % 2 === 0) ? '<span class="caret"></span>' : "";
    document.getElementById("prompt").innerHTML = PROMPT.slice(0, n) + caret;
    document.getElementById("cwho").style.opacity = ease((t - T.claudeIn) / 0.4);
    let shown = -1, prevShown = -1, since = 99;
    LINES.forEach((l, i) => {
      const p = ease((t - l.at) / 0.45);
      const el = document.getElementById("l" + i);
      el.style.opacity = p; el.style.transform = "translateY(" + (6 - 6 * p) + "px)";
      if (l.state != null && t >= l.at + 0.25) { prevShown = shown; shown = l.state; since = t - (l.at + 0.25); }
    });
    const mockAt = LINES.find((l) => /gen_quote/.test(l.call)).at;
    document.getElementById("mock").style.opacity = ease((t - mockAt) / 0.5);
    // A left-to-right wipe: the new state is revealed over the old one, so a
    // layout that shifts (a lane appearing) never double-exposes mid-change.
    const prog = ease(since / T.fade);
    const wipe = document.getElementById("wipe");
    frames.forEach((f, i) => {
      let o = 0, clip = "none";
      if (i === shown) { o = 1; clip = prog < 1 ? "inset(0 " + ((1 - prog) * 100).toFixed(2) + "% 0 0)" : "none"; }
      else if (i === prevShown && prog < 1) o = 1;
      f.style.opacity = o; f.style.clipPath = clip;
      f.style.zIndex = i === shown ? 2 : (i === prevShown ? 1 : 0);
    });
    if (shown >= 0 && prog > 0 && prog < 1) { wipe.style.opacity = 1; wipe.style.left = "calc(" + (prog * 100).toFixed(2) + "% - 1px)"; }
    else wipe.style.opacity = 0;
    document.getElementById("empty").style.opacity = shown < 0 ? 1 : 1 - ease(since / 0.3);
    const cap = document.getElementById("cap");
    if (shown >= 0) {
      const l = LINES.find((x) => x.state === shown);
      cap.textContent = (shown === 0 ? "rendered from " : "re-rendered from ") + l.call + " output";
      cap.style.opacity = ease((since - 0.2) / 0.4); cap.style.zIndex = 5;
    } else cap.style.opacity = 0;
  };
</script></body></html>`;

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 960, height: 540 }, deviceScaleFactor: 2, colorScheme: "dark" });
  page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
  await page.setContent(host);
  await page.waitForFunction((n) => Object.keys(window.__ready).length === n, states.states.length, { timeout: 20000 });
  for (let i = 0; i < states.states.length; i++) {
    const f = page.frameLocator("#app" + i);
    await f.locator("#scroller").waitFor({ state: "visible", timeout: 10000 });
    const logged = await f.locator("#log").textContent();
    if (!/timeline loaded/.test(logged)) throw new Error("state " + i + " did not load a payload: " + logged.slice(0, 200));
    console.log("state", i, "app log:", logged.split("\n")[0]);
  }
  // Fonts and thumbnails settle before the first frame.
  await page.waitForTimeout(500);
  const total = Math.round(states.duration * FPS);
  for (let k = 0; k < total; k++) {
    await page.evaluate((ms) => window.__seek(ms), (k / FPS) * 1000);
    await page.screenshot({ path: path.join(framesDir, String(k).padStart(5, "0") + ".png"), animations: "disabled" });
    if (k % 150 === 0) console.log("frame", k, "/", total);
  }
  await browser.close();
  console.log("frames:", total);
})().catch((e) => { console.error(e); process.exit(1); });
