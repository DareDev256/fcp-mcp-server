// The hero's UNEDITED recording: the app inside a chat client, from the same
// run (the same states.json, the same results and footage) as the staged
// close-up that record.js makes. Run by build.py; not meant to be run alone.
//
//   node demo/hero/record_client.js <work dir> <frames dir>
//
// One stub host window, 860x640 CSS at 2x (1720x1280), 30 fps: the prompt and
// Claude's tool calls on the left (each call with its result note, the same
// words as the staged chips), the REAL ui://fcp/timeline app on the right with
// its SELECTION and SERVER panels, one app instance fed the real
// view_timeline results in order as each call lands, so every change is the
// app's own motion. No camera, no crop, no close-up: the whole client, as a
// viewer of the chat would see it. It writes work/shown-client.json (the
// calls, result digests and thumbnail digests it showed) for build.py's
// truth check, and it refuses a name the app had to hide, a chat line wider
// than its column, a SERVER panel cut by the window, and stale text.
"use strict";
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

function loadPlaywright() {
  const hint = process.env.HERO_PLAYWRIGHT;
  for (const candidate of [hint, "playwright"].filter(Boolean)) {
    try { return require(candidate); } catch (e) { /* try the next one */ }
  }
  throw new Error("playwright not found: npm i playwright, or HERO_PLAYWRIGHT=/path/to/node_modules/playwright");
}

const [work, framesDir] = process.argv.slice(2);
const states = JSON.parse(fs.readFileSync(path.join(work, "states.json"), "utf8"));
const client = states.client;
const app = fs.readFileSync(path.join(work, "app.html"), "utf8");
const FPS = 30, W = 860, H = 640, SCALE = 2, LEAD = 0.1;  // a re-render moves LEAD s after its call lands
const last = states.states.length - 1;
const LINES = client.lines;  // [{at, call, note, state?}], the same calls and notes as the stage's chips
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");

const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const tokens = Object.entries(states.palette).map(([k, v]) => `--${k}:${v};`).join(" ");
const appFrame = (id, style = "") => `<iframe class="app" id="${id}" style="${style}" sandbox="allow-scripts" srcdoc="${esc(app)}"></iframe>`;

const protocol = `
  window.__ready = {};
  window.addEventListener("message", (ev) => {
    const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
    const f = [...document.querySelectorAll("iframe.app")].find((x) => x.contentWindow === ev.source); if (!f) return;
    const post = (msg) => f.contentWindow.postMessage(msg, "*");
    if (m.method === "ui/initialize") { post({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2025-11-21", hostContext: { theme: "dark" } } }); return; }
    if (m.method === "ui/notifications/initialized") { window.__ready[f.id] = true; return; }
    if (m.method === "tools/call") post({ jsonrpc: "2.0", id: m.id, result: { content: [{ type: "text", text: "(stub host: not recorded)" }] } });
  });`;

const host = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; width:${W}px; height:${H}px; background:var(--ground); color:var(--ink); overflow:hidden;
    font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #top { position:absolute; left:28px; right:28px; top:16px; display:flex; justify-content:space-between;
    font: 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--mute); letter-spacing:.04em; }
  #top b { color:var(--ink); font-weight:600; }
  #chat { position:absolute; left:28px; top:48px; width:292px; bottom:28px; }
  #you { opacity:0; }
  .who { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--mute); margin-bottom:6px; }
  .prompt { font-size:16px; line-height:1.35; font-weight:500; letter-spacing:-.01em; }
  #prompt { min-height:44px; }
  #prompt .caret { display:inline-block; width:2px; height:16px; background:var(--accent); vertical-align:-2px; margin-left:1px; }
  #claude { margin-top:18px; }
  .line { opacity:0; transform:translateY(6px); margin-bottom:8px; }
  .call { font: 600 12.5px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--ink); white-space:nowrap; }
  .call:before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--accent);
    margin-right:8px; vertical-align:1px; }
  .note { color:var(--mute); font-size:11.5px; margin-left:14px; margin-top:1px; white-space:nowrap; }
  #foot { position:absolute; left:0; bottom:0; width:292px; color:var(--mute); font-size:10.5px; line-height:1.35;
    border-top:1px solid var(--line); padding-top:7px; }
  #stage { position:absolute; left:332px; top:48px; right:28px; bottom:28px; }
  #frame { position:absolute; inset:0; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--ground); }
  .app { position:absolute; inset:0; width:100%; height:100%; border:0; background:var(--ground); }
</style></head><body>
<div id="top"><span><b>fcp-mcp-server</b> · watch Claude cut</span><span>ui://fcp/timeline · MCP Apps</span></div>
<div id="chat">
  <div id="you"><div class="who">You</div><div class="prompt" id="prompt"></div></div>
  <div id="claude"><div class="who" id="cwho" style="opacity:0">Claude · tool calls</div>
    ${LINES.map((l, i) => `<div class="line" id="l${i}"><div class="call">${esc(l.call)}</div><div class="note">${esc(l.note)}</div></div>`).join("")}
  </div>
  <div id="foot">${esc(client.footer)}</div>
</div>
<div id="stage"><div id="frame">${appFrame("app", "visibility:hidden")}</div></div>
<script>
  ${protocol}
  const LINES = ${JSON.stringify(LINES)};
  const PROMPT = ${JSON.stringify(states.prompt)};
  const clamp = (v) => Math.max(0, Math.min(1, v));
  const ease = (v) => { v = clamp(v); return v < .5 ? 2 * v * v : 1 - Math.pow(-2 * v + 2, 2) / 2; };
  const T = ${JSON.stringify(client.prompt)};
  window.__seek = function (t, shown) {
    document.getElementById("you").style.opacity = ease((t - T.you) / 0.15);
    const n = Math.round(clamp((t - T.type[0]) / (T.type[1] - T.type[0])) * PROMPT.length);
    const caret = t < T.type[1] + 0.3 && (Math.floor(t * 5) % 2 === 0) ? '<span class="caret"></span>' : "";
    document.getElementById("prompt").innerHTML = PROMPT.slice(0, n).replace(/&/g, "&amp;").replace(/</g, "&lt;") + caret;
    document.getElementById("cwho").style.opacity = ease((t - T.claude) / 0.2);
    LINES.forEach((l, i) => {
      const p = ease((t - l.at) / 0.25), el = document.getElementById("l" + i);
      el.style.opacity = p; el.style.transform = "translateY(" + (6 - 6 * p) + "px)";
    });
    document.getElementById("app").style.visibility = shown >= 0 ? "visible" : "hidden";
  };
</script></body></html>`;

async function refuse(msg) { console.error("REFUSED: " + msg); process.exit(1); }
function staleIn(text, scope) {
  const out = [];
  for (const [what, src] of Object.entries(states.stale[scope])) {
    for (const m of text.matchAll(new RegExp(src, "gi"))) out.push(what + ": " + m[0]);
  }
  return out;
}

const toolResult = (i) => ({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
  params: { result: { content: [{ type: "text", text: states.states[i].result }] } } });
async function appFrameOf(page, id) { return (await page.$("#" + id)).contentFrame(); }
async function waitReady(page, id) { await page.waitForFunction((id) => window.__ready[id] === true, id, { timeout: 20000 }); }

// A fresh app (the loop starts over): navigate the iframe and handshake again.
async function reload(page, ctx) {
  await page.evaluate((id) => {
    window.__ready[id] = false;
    const f = document.getElementById(id), html = f.getAttribute("srcdoc");
    f.setAttribute("srcdoc", html.endsWith(" ") ? html.trimEnd() : html + " ");
  }, ctx.id);
  await waitReady(page, ctx.id);
  ctx.cur = -1;
}
async function deliver(frame, i) {
  await frame.evaluate((msg) => window.dispatchEvent(new MessageEvent("message", { data: msg })), toolResult(i));
  if (states.mock_clip && i === last && !(await injectPill(frame))) await refuse(`no clip named "${states.mock_clip}" to mark as mocked`);
  await frame.evaluate(() => { window.__anims = document.getAnimations(); window.__anims.forEach((a) => { a.pause(); a.currentTime = 0; }); });
  await frame.evaluate(() => Promise.all([...document.querySelectorAll(".thumbs")].map((t) => {
    const m = /url\("(.*)"\)/.exec(t.style.backgroundImage); if (!m) return null;
    const im = new Image(); im.src = m[1]; return im.decode().catch(() => null);
  })));
}
// Show state `target` with its motion run `ms` milliseconds in (a large ms is
// the settled picture). Earlier states are delivered first and settled.
async function showState(page, ctx, target, ms) {
  if (target < ctx.cur) await reload(page, ctx);
  const frame = await appFrameOf(page, ctx.id);
  while (ctx.cur < target) {
    ctx.cur += 1;
    await deliver(frame, ctx.cur);
    if (ctx.cur < target) await frame.evaluate(() => (window.__anims || []).forEach((a) => { a.currentTime = 1e6; }));
  }
  if (target >= 0) await frame.evaluate((ms) => (window.__anims || document.getAnimations()).forEach((a) => { a.currentTime = ms; }), ms);
  return frame;
}
async function injectPill(frame) {
  return frame.evaluate(({ name, pal }) => {
    const clip = [...document.querySelectorAll("#canvas .clip:not(.ghost)")].find((c) => (c.title || "").startsWith(name + " ·"));
    if (!clip) return false;
    const pill = document.createElement("div");
    pill.className = "hero-mock"; pill.textContent = "MOCK";
    Object.assign(pill.style, { position: "absolute", top: "4px", right: "4px", zIndex: "3",
      font: "700 8px/1 ui-monospace, Menlo, monospace", letterSpacing: ".08em", color: pal["accent-ink"],
      background: pal["accent-fill"], padding: "2px 4px", borderRadius: "3px", whiteSpace: "nowrap" });
    clip.appendChild(pill);
    const m = clip.__motion;
    if (m) window.__fcpApp.revealWhenWide(pill, m.from, m.to, pill.offsetWidth + 8, true, m.delay);
    return true;
  }, { name: states.mock_clip, pal: states.palette });
}

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: SCALE, colorScheme: "dark" });
  page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
  await page.setContent(host);
  await waitReady(page, "app");
  const ctx = { id: "app", cur: -1 };
  // Each state settled: no hidden name, nothing stale.
  for (let i = 0; i <= last; i++) {
    const frame = await showState(page, ctx, i, 1e6);
    const hidden = await frame.evaluate(() => [...document.querySelectorAll(".clip:not(.ghost) .label.unfit, .mlabel.unfit")].map((e) => e.textContent || e.title));
    if (hidden.length) await refuse(`state ${i}: the app had to hide ${JSON.stringify(hidden)}`);
    const hits = staleIn(await frame.evaluate(() => document.body.innerText), "app");
    if (hits.length) await refuse(`state ${i} would bake stale text: ${JSON.stringify(hits)}`);
  }
  const appFr = await appFrameOf(page, "app");
  const server = await appFr.evaluate(() => {
    const card = document.getElementById("log").closest(".card"), r = card.getBoundingClientRect();
    return { bottom: r.bottom, viewH: document.documentElement.clientHeight };
  });
  if (server.bottom > server.viewH - 12) await refuse(`the SERVER panel ends at ${server.bottom}px, the window at ${server.viewH}px`);
  const overflow = await page.evaluate(() => [...document.querySelectorAll("#chat .call, #chat .note")]
    .filter((e) => e.getBoundingClientRect().right > document.getElementById("chat").getBoundingClientRect().right + 0.5)
    .map((e) => e.textContent));
  if (overflow.length) await refuse("chat lines wider than their column: " + JSON.stringify(overflow));
  const hostHits = staleIn(await page.evaluate(() => document.body.innerText), "host");
  if (hostHits.length) await refuse("the client would bake stale text: " + JSON.stringify(hostHits));

  const lineOf = (t) => {
    let shown = -1, since = 0;
    LINES.forEach((l) => { if (l.state != null && t >= l.at + LEAD) { shown = l.state; since = t - (l.at + LEAD); } });
    return { shown, since };
  };
  // From the finished cut (frame 0, every line in) round the loop.
  const total = Math.round(client.duration * FPS), offset = Math.round(client.loop_start * FPS) % total;
  for (let k = 0; k < total; k++) {
    const t = ((k + offset) % total) / FPS, s = lineOf(t);
    await showState(page, ctx, s.shown, s.since * 1000);
    await page.evaluate(({ t, s }) => window.__seek(t, s), { t, s: s.shown });
    await page.screenshot({ path: path.join(framesDir, String(k).padStart(5, "0") + ".png") });
    if (k % 150 === 0) console.log("client frame", k, "/", total);
  }
  // What it showed: the calls and notes as drawn, the results it was fed, the thumbnails in the last one.
  await showState(page, ctx, last, 1e6);
  await page.evaluate(({ t, s }) => window.__seek(t, s), { t: client.loop_start, s: last });
  const calls = await page.evaluate(() => [...document.querySelectorAll("#chat .line")].map((e) => [e.querySelector(".call").textContent, e.querySelector(".note").textContent]));
  const payload = await (await appFrameOf(page, "app")).evaluate(() => window.__fcpApp.state.payload);
  fs.writeFileSync(path.join(work, "shown-client.json"), JSON.stringify({
    calls,
    results: [...new Set(LINES.filter((l) => l.state != null).map((l) => l.state))].sort().map((i) => sha(states.states[i].result)),
    thumbs: Object.fromEntries(payload.clips.filter((c) => c.thumb).map((c) => [c.name + " @ " + c.start, sha(c.thumb)])),
  }, null, 1));
  console.log("client frames:", total);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
