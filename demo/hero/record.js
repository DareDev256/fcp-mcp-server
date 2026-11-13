// Stub MCP Apps host for the README hero. Run by build.py; not meant to be run alone.
//
//   node demo/hero/record.js <work dir> <frames dir> <assets dir>
//
// Loads the ui://fcp/timeline shell (work/app.html, exactly as the server
// serves it) once per timeline state, speaks the MCP Apps draft protocol to
// each copy (ui/initialize -> ui/notifications/initialized -> the real
// view_timeline result as ui/notifications/tool-result), and lays a chat
// column beside it. The page is a pure function of time: window.__seek(ms)
// positions every element, so each captured frame is exact and a slow
// screenshot cannot drift the animation. Frames are 960x600 CSS pixels at a
// device scale of 2 (1920x1200): tall enough that the app's SERVER panel ends
// inside the window, and the app's own 13px type stays legible at README width.
//
// Every colour is a palette token handed over in states.json
// (fcpxml/palette.py); none is typed here. Frames are written in story order;
// build.py rotates the GIF so it opens on the finished cut and gives the MP4
// that frame as its poster. The assets dir gets hero-still.png (the finished
// frame) and card-frame.png (the conversation beside the finished timeline,
// for scripts/social_card.py).
//
// It REFUSES (exit 1) rather than record a frame anyone would have to excuse:
// a chat line wider than its column, a clip or marker name the app had to
// hide, a SERVER panel cut by the window, the caption pill outside it.
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

const [work, framesDir, assetsDir] = process.argv.slice(2);
const states = JSON.parse(fs.readFileSync(path.join(work, "states.json"), "utf8"));
const app = fs.readFileSync(path.join(work, "app.html"), "utf8");
const FPS = 30, W = 960, H = 600, SCALE = 2;

const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
const tokens = Object.entries(states.palette).map(([k, v]) => `--${k}:${v};`).join(" ");
const appFrame = (id, i) => `<iframe class="app" id="${id}" data-state="${i}" sandbox="allow-scripts" srcdoc="${esc(app)}"></iframe>`;

// The host side of the MCP Apps handshake, shared by both pages.
const protocol = `
  const STATES = ${JSON.stringify(states.states)};
  window.__ready = {};
  window.addEventListener("message", (ev) => {
    const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
    const f = [...document.querySelectorAll("iframe.app")].find((x) => x.contentWindow === ev.source); if (!f) return;
    const i = Number(f.dataset.state), post = (msg) => f.contentWindow.postMessage(msg, "*");
    if (m.method === "ui/initialize") { post({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2025-11-21", hostContext: { theme: "dark" } } }); return; }
    if (m.method === "ui/notifications/initialized") {
      post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { result: { content: [{ type: "text", text: STATES[i].result }] } } });
      window.__ready[f.id] = true; return;
    }
    if (m.method === "tools/call") post({ jsonrpc: "2.0", id: m.id, result: { content: [{ type: "text", text: "(stub host: not recorded)" }] } });
  });`;

const chatCss = `
  .who { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--mute); margin-bottom:6px; }
  .prompt { font-size:16px; line-height:1.35; font-weight:500; letter-spacing:-.01em; }
  .call { font: 600 12.5px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--ink); white-space:nowrap; }
  .call:before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--accent);
    margin-right:8px; vertical-align:1px; }
  .note { color:var(--mute); font-size:11.5px; margin-left:14px; margin-top:1px; white-space:nowrap; }`;

const host = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; width:${W}px; height:${H}px; background:var(--ground); color:var(--ink); overflow:hidden;
    font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #top { position:absolute; left:28px; right:28px; top:16px; display:flex; justify-content:space-between;
    font: 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--mute); letter-spacing:.04em; }
  #top b { color:var(--ink); font-weight:600; }
  #chat { position:absolute; left:28px; top:48px; width:292px; bottom:28px; }
  #you { opacity:0; }
  #prompt { min-height:66px; }
  #prompt .caret { display:inline-block; width:2px; height:16px; background:var(--accent); vertical-align:-2px; margin-left:1px; }
  #claude { margin-top:18px; }
  .line { opacity:0; transform:translateY(6px); margin-bottom:8px; }
  ${chatCss}
  #mock { position:absolute; left:0; bottom:0; width:292px; color:var(--mute); font-size:10.5px; line-height:1.35;
    opacity:0; border-top:1px solid var(--line); padding-top:7px; }
  #stage { position:absolute; left:332px; top:48px; right:28px; bottom:28px; }
  #frame { position:absolute; inset:0; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--ground); }
  .app { position:absolute; inset:0; width:100%; height:100%; border:0; opacity:0; background:var(--ground); }
  #wipe { position:absolute; top:0; bottom:0; width:2px; background:var(--accent); opacity:0; z-index:6;
    box-shadow:0 0 12px 2px var(--accent-wash); }
  #cap { position:absolute; font: 600 11px/1 ui-monospace, Menlo, monospace; color:var(--accent); white-space:nowrap;
    background:var(--panel); padding:5px 8px; border-radius:6px; border:1px solid var(--line-strong); opacity:0; z-index:5; }
  #empty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--mute);
    font-size:13px; background:var(--ground); }
</style></head><body>
<div id="top"><span><b>fcp-mcp-server</b> · watch Claude cut</span><span>ui://fcp/timeline · MCP Apps · ${W * SCALE}×${H * SCALE}</span></div>
<div id="chat">
  <div id="you"><div class="who">You</div><div class="prompt" id="prompt"></div></div>
  <div id="claude"><div class="who" id="cwho" style="opacity:0">Claude · tool calls</div>
    ${states.lines.map((l, i) => `<div class="line" id="l${i}"><div class="call">${l.call}</div><div class="note">${l.note}</div></div>`).join("")}
  </div>
  <div id="mock">${states.mock_note}</div>
</div>
<div id="stage"><div id="frame"><div id="empty">Waiting for a timeline. Call the <b>&nbsp;view&nbsp;</b> tool with view_timeline and a filepath.</div>${states.states.map((s, i) => appFrame("app" + i, i)).join("")}<div id="wipe"></div><div id="cap"></div></div></div>
<script>
  ${protocol}
  const LINES = ${JSON.stringify(states.lines)};
  const PROMPT = ${JSON.stringify(states.prompt)};
  window.__capAt = [];   // per state: where the SERVER panel sits inside #frame (measured by the recorder)
  const frames = STATES.map((s, i) => document.getElementById("app" + i));
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
    // The caption sits inside the app's SERVER panel, top right, where the
    // server's own log speaks: it says which call's output is on screen.
    const cap = document.getElementById("cap");
    if (shown >= 0 && window.__capAt[shown]) {
      const l = LINES.find((x) => x.state === shown), at = window.__capAt[shown];
      cap.textContent = (shown === 0 ? "rendered from " : "re-rendered from ") + l.call + " output";
      cap.style.top = at.top + "px"; cap.style.right = at.right + "px";
      cap.style.opacity = ease((since - 0.2) / 0.4); cap.style.zIndex = 5;
    } else cap.style.opacity = 0;
  };
</script></body></html>`;

// The social card's picture: the same conversation beside the same finished
// timeline, without the idle selection panel. Real text, real app, one frame.
const CARD_W = 880, CHAT_W = 236;
const last = states.states.length - 1;
const cardHost = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; background:var(--ground); color:var(--ink); font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #card { display:flex; gap:18px; align-items:center; width:${CARD_W}px; box-sizing:border-box; padding:18px;
    background:var(--ground); }
  #cchat { width:${CHAT_W}px; flex:none; }
  #cchat .prompt { margin-bottom:16px; }
  #cchat .line { margin-bottom:7px; }
  ${chatCss}
  #cstage { flex:1; min-width:0; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--ground); }
  #cstage iframe { display:block; width:100%; height:420px; border:0; background:var(--ground); }
</style></head><body><div id="card">
  <div id="cchat"><div class="who">You</div><div class="prompt">${states.prompt}</div>
    <div class="who">Claude · tool calls</div>
    ${states.lines.map((l) => `<div class="line"><div class="call">${l.call}</div></div>`).join("")}
  </div>
  <div id="cstage">${appFrame("capp", last)}</div>
</div><script>${protocol}</script></body></html>`;

async function refuse(msg) { console.error("REFUSED: " + msg); process.exit(1); }

// Labels the app had to hide in one iframe: a name the viewer cannot read.
async function hiddenNames(frame) {
  return frame.locator("body").evaluate(() =>
    [...document.querySelectorAll(".clip .label.unfit, .mlabel.unfit")].map((e) => e.textContent || e.title));
}

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: SCALE, colorScheme: "dark" });
  page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
  await page.setContent(host);
  await page.waitForFunction((n) => Object.keys(window.__ready).length === n, states.states.length, { timeout: 20000 });
  const frameBox = await page.locator("#frame").boundingBox();
  const capAt = [];
  for (let i = 0; i < states.states.length; i++) {
    const f = page.frameLocator("#app" + i);
    await f.locator("#scroller").waitFor({ state: "visible", timeout: 10000 });
    const logged = await f.locator("#log").textContent();
    if (!/timeline loaded/.test(logged)) throw new Error("state " + i + " did not load a payload: " + logged.slice(0, 200));
    console.log("state", i, "app log:", logged.split("\n")[0]);
    const hidden = await hiddenNames(f);
    if (hidden.length) await refuse(`state ${i}: the app had to hide ${JSON.stringify(hidden)}; give the clips room or shorter names`);
    const server = await f.locator("#log").evaluate((el) => {
      // Text boxes, not element boxes: the heading's block is the card's full width.
      const r = el.closest(".card").getBoundingClientRect();
      const rects = (node) => { const range = document.createRange(); range.selectNodeContents(node);
        return [...range.getClientRects()].map((q) => ({ x: q.left, y: q.top, w: q.width, h: q.height })); };
      return { x: r.left, y: r.top, w: r.width, h: r.height, viewH: document.documentElement.clientHeight,
               words: [...rects(el.closest(".card").querySelector("h2")), ...rects(el)] };
    });
    if (server.y + server.h > server.viewH - 12) await refuse(`state ${i}: the SERVER panel ends at ${server.y + server.h}px, the window at ${server.viewH}px`);
    capAt.push({ top: server.y + 7, right: frameBox.width - 2 - (server.x + server.w) + 10, box: server });
  }
  await page.evaluate((c) => { window.__capAt = c; }, capAt);
  const overflow = await page.evaluate(() => [...document.querySelectorAll("#chat .call, #chat .note")]
    .filter((e) => e.getBoundingClientRect().right > document.getElementById("chat").getBoundingClientRect().right + 0.5)
    .map((e) => e.textContent));
  if (overflow.length) await refuse("chat lines wider than their column: " + JSON.stringify(overflow));
  // Fonts and thumbnails settle before the first frame.
  await page.waitForTimeout(500);
  const total = Math.round(states.duration * FPS);
  for (let k = 0; k < total; k++) {
    await page.evaluate((ms) => window.__seek(ms), (k / FPS) * 1000);
    await page.screenshot({ path: path.join(framesDir, String(k).padStart(5, "0") + ".png"), animations: "disabled" });
    if (k % 150 === 0) console.log("frame", k, "/", total);
  }
  // The finished frame: the caption must sit inside the SERVER panel it names.
  await page.evaluate((ms) => window.__seek(ms), states.loop_start * 1000);
  const cap = await page.locator("#cap").boundingBox(), sv = capAt[last].box;
  const inside = cap.x >= frameBox.x + 1 + sv.x && cap.x + cap.width <= frameBox.x + 1 + sv.x + sv.w &&
                 cap.y >= frameBox.y + 1 + sv.y && cap.y + cap.height <= frameBox.y + 1 + sv.y + sv.h;
  if (!inside) await refuse("the caption pill is not inside the SERVER panel: " + JSON.stringify({ cap, sv }));
  const ox = frameBox.x + 1, oy = frameBox.y + 1;
  const hit = sv.words.find((q) => cap.x < ox + q.x + q.w && ox + q.x < cap.x + cap.width &&
                                   cap.y < oy + q.y + q.h && oy + q.y < cap.y + cap.height);
  if (hit) await refuse("the caption pill covers the SERVER panel's own text: " + JSON.stringify(hit));
  if (assetsDir) {
    await page.screenshot({ path: path.join(assetsDir, "hero-still.png"), animations: "disabled" });
    const card = await browser.newPage({ viewport: { width: CARD_W, height: 800 }, deviceScaleFactor: SCALE, colorScheme: "dark" });
    await card.setContent(cardHost);
    await card.waitForFunction(() => window.__ready.capp === true, null, { timeout: 20000 });
    const cf = card.frameLocator("#capp");
    await cf.locator("#scroller").waitFor({ state: "visible", timeout: 10000 });
    const bottom = await cf.locator("#scroller").evaluate((el) => el.getBoundingClientRect().bottom);
    await card.evaluate((h) => { document.querySelector("#cstage iframe").style.height = h + "px"; }, Math.ceil(bottom + 12));
    await card.waitForTimeout(400);
    const hidden = await hiddenNames(cf);
    if (hidden.length) await refuse("card: the app had to hide " + JSON.stringify(hidden));
    const cardOverflow = await card.evaluate(() => [...document.querySelectorAll("#cchat .call")]
      .filter((e) => e.getBoundingClientRect().right > document.getElementById("cchat").getBoundingClientRect().right + 0.5)
      .map((e) => e.textContent));
    if (cardOverflow.length) await refuse("card: chat lines wider than their column: " + JSON.stringify(cardOverflow));
    await card.locator("#card").screenshot({ path: path.join(assetsDir, "card-frame.png"), animations: "disabled" });
    await card.close();
    console.log("still + card frame written to", assetsDir);
  }
  await browser.close();
  console.log("frames:", total);
})().catch((e) => { console.error(e); process.exit(1); });
