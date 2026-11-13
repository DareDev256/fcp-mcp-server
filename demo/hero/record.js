// Stub MCP Apps host for the README hero. Run by build.py; not meant to be run alone.
//
//   node demo/hero/record.js <work dir> <frames dir> <assets dir>
//
// Three pictures of the REAL ui://fcp/timeline shell (work/app.html, exactly
// as the server serves it). Each is ONE app instance driven through the MCP
// Apps draft protocol (ui/initialize -> ui/notifications/initialized, then
// the real view_timeline results as ui/notifications/tool-result, in order),
// so every change between results is the app's own motion: a split clip's
// pieces slide together, new markers drop in, the B-roll is laid in. The app
// animates with Web Animations; this recorder pauses each one and sets its
// time per frame, so every frame is a pure function of the story clock and
// no frame ever holds two renders at once.
// (No screenshot here passes `animations: "disabled"`: Playwright would
// fast-forward the app's motion to its end and every frame would be settled.)
//
// 1. <frames dir>      the MP4: the chat beside the app, 860x640 CSS at 2x
//                      (1720x1280). Each tool call lands in the chat as its
//                      re-render starts to move (0.1 s later); the timeline
//                      first appears with the view call, a second in.
// 2. work/gif-frames/  the README GIF, its own cut: the ruler and lanes only,
//                      cropped inside the panel, the app laid out at 440 CSS
//                      px (a phone's width once its gutter is cropped) and
//                      drawn at 3x, so its 10-11 px type is 30-33 px. One PNG
//                      per distinct picture; durations.json says how long
//                      each stays. Frame 0 is the finished cut.
// 3. <assets dir>      hero-still.png (the MP4's frame 0, the finished cut),
//                      card-frame.png (the finished ruler and lanes at 1.75x,
//                      for scripts/social_card.py) and hero-layout.json (where
//                      the timeline sits in the MP4, for tests/test_hero_assets.py).
//
// A mocked B-roll tile (states.mock_clip) carries a solid MOCK pill, placed
// INSIDE the tile so it moves with it, never over its label.
//
// It REFUSES (exit 1) rather than record a frame anyone would have to excuse:
// a clip or marker name the app had to hide (in any state, in any of the
// three layouts), a chat line wider than its column, a SERVER panel cut by
// the window, the caption outside it or over its text, the MOCK pill off its
// tile or over its label (or no tile to put it on), the footer over the tool
// calls, or any text that would go stale in an image (a price, a model id, a
// frame size, a version: scripts/image_text.py, handed over in states.stale).
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
const FPS = 30, W = 860, H = 640, SCALE = 2, LEAD = 0.1;  // a re-render moves LEAD s after its call lands
const GIF_APP_W = 440, GIF_ZOOM = 1.5, GIF_STEP_MS = 40;
const CARD_APP_W = 480, CARD_ZOOM = 1.75;
const GUTTER = 48;  // the app's lane-tag column (fcpxml/timeline_app.py)
const last = states.states.length - 1;

const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
const tokens = Object.entries(states.palette).map(([k, v]) => `--${k}:${v};`).join(" ");
const appFrame = (id, style = "") => `<iframe class="app" id="${id}" style="${style}" sandbox="allow-scripts" srcdoc="${esc(app)}"></iframe>`;

// The host side of the MCP Apps handshake. Results are not pushed here: the
// recorder delivers them one by one (deliver()), in order, to the same app.
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
  #prompt { min-height:44px; }
  #prompt .caret { display:inline-block; width:2px; height:16px; background:var(--accent); vertical-align:-2px; margin-left:1px; }
  #claude { margin-top:18px; }
  .line { opacity:0; transform:translateY(6px); margin-bottom:8px; }
  ${chatCss}
  #mock { position:absolute; left:0; bottom:0; width:292px; color:var(--mute); font-size:10.5px; line-height:1.35;
    border-top:1px solid var(--line); padding-top:7px; }
  #stage { position:absolute; left:332px; top:48px; right:28px; bottom:28px; }
  #frame { position:absolute; inset:0; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--ground); }
  .app { position:absolute; inset:0; width:100%; height:100%; border:0; background:var(--ground); }
  #cap { position:absolute; font: 600 11px/1 ui-monospace, Menlo, monospace; color:var(--accent); white-space:nowrap;
    background:var(--panel); padding:5px 8px; border-radius:6px; border:1px solid var(--line-strong); visibility:hidden; z-index:5; }
</style></head><body>
<div id="top"><span><b>fcp-mcp-server</b> · watch Claude cut</span><span>ui://fcp/timeline · MCP Apps</span></div>
<div id="chat">
  <div id="you"><div class="who">You</div><div class="prompt" id="prompt"></div></div>
  <div id="claude"><div class="who" id="cwho" style="opacity:0">Claude · tool calls</div>
    ${states.lines.map((l, i) => `<div class="line" id="l${i}"><div class="call">${l.call}</div><div class="note">${l.note}</div></div>`).join("")}
  </div>
  <div id="mock">${states.footer}</div>
</div>
<div id="stage"><div id="frame">${appFrame("app", "visibility:hidden")}<div id="cap"></div></div></div>
<script>
  ${protocol}
  const LINES = ${JSON.stringify(states.lines)};
  const PROMPT = ${JSON.stringify(states.prompt)};
  window.__capAt = null;  // where the SERVER panel's top right sits inside #frame (measured by the recorder)
  const clamp = (v) => Math.max(0, Math.min(1, v));
  const ease = (v) => { v = clamp(v); return v < .5 ? 2 * v * v : 1 - Math.pow(-2 * v + 2, 2) / 2; };
  const T = { youIn: 0.05, typeStart: 0.15, typeEnd: 0.85, claudeIn: 0.9 };
  // The chat and the caption at story time t; the app itself is set by the
  // recorder (which result it has, how far its motion has run).
  window.__seek = function (t, shown, since, lineIndex) {
    document.getElementById("you").style.opacity = ease((t - T.youIn) / 0.15);
    const n = Math.round(clamp((t - T.typeStart) / (T.typeEnd - T.typeStart)) * PROMPT.length);
    const caret = t < T.typeEnd + 0.3 && (Math.floor(t * 5) % 2 === 0) ? '<span class="caret"></span>' : "";
    document.getElementById("prompt").innerHTML = PROMPT.slice(0, n) + caret;
    document.getElementById("cwho").style.opacity = ease((t - T.claudeIn) / 0.2);
    LINES.forEach((l, i) => {
      const p = ease((t - l.at) / 0.25);
      const el = document.getElementById("l" + i);
      el.style.opacity = p; el.style.transform = "translateY(" + (6 - 6 * p) + "px)";
    });
    document.getElementById("app").style.visibility = shown >= 0 ? "visible" : "hidden";
    // The caption sits in the app's SERVER panel, top right, once the motion
    // has settled: it says which call's output is on screen.
    const cap = document.getElementById("cap");
    if (shown >= 0 && window.__capAt && since >= 0.45) {
      cap.textContent = (shown === 0 ? "rendered from " : "re-rendered from ") + LINES[lineIndex].call + " output";
      cap.style.top = window.__capAt.top + "px"; cap.style.right = window.__capAt.right + "px";
      cap.style.visibility = "visible";
    } else cap.style.visibility = "hidden";
  };
</script></body></html>`;

// The README GIF: the app laid out at GIF_APP_W and drawn GIF_ZOOM x larger.
const gifHost = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; background:var(--ground); }
  #gzoom { position:absolute; left:0; top:0; transform-origin:0 0; transform:scale(${GIF_ZOOM}); }
  #gzoom iframe { position:absolute; left:0; top:0; width:${GIF_APP_W}px; height:620px; border:0; background:var(--ground); }
</style></head><body><div id="gzoom">${appFrame("gapp")}</div><script>${protocol}</script></body></html>`;

// The social card's picture: the finished ruler and lanes at CARD_ZOOM.
const cardHost = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; background:var(--ground); }
  #czoom { position:absolute; left:0; top:0; transform-origin:0 0; transform:scale(${CARD_ZOOM}); }
  #czoom iframe { position:absolute; left:0; top:0; width:${CARD_APP_W}px; height:620px; border:0; background:var(--ground); }
</style></head><body><div id="czoom">${appFrame("capp")}</div><script>${protocol}</script></body></html>`;

async function refuse(msg) { console.error("REFUSED: " + msg); process.exit(1); }

// Text that would go stale baked into an image: host text against every
// pattern, the app's text against all but frame size (its header states the
// demo sequence's own size, which is data, not a release fact).
function staleIn(text, scope) {
  const out = [];
  for (const [what, src] of Object.entries(states.stale[scope])) {
    for (const m of text.matchAll(new RegExp(src, "gi"))) out.push(what + ": " + m[0]);
  }
  return out;
}
async function refuseStale(where, text, scope) {
  const hits = staleIn(text, scope);
  if (hits.length) await refuse(`${where} would bake stale text into the image: ${JSON.stringify(hits)}`);
}

// ---- one app instance, results delivered in order ---------------------------

const toolResult = (i) => ({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
  params: { result: { content: [{ type: "text", text: states.states[i].result }] } } });

async function appFrameOf(page, id) { return (await page.$("#" + id)).contentFrame(); }

async function waitReady(page, id) {
  await page.waitForFunction((id) => window.__ready[id] === true, id, { timeout: 20000 });
}

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

// The result for state i, as the host would send it; the app re-renders and
// starts its motion, which is paused at once (at 0) for the recorder to seek.
async function deliver(frame, i) {
  await frame.evaluate((msg) => {
    window.dispatchEvent(new MessageEvent("message", { data: msg }));
    document.getAnimations().forEach((a) => { a.pause(); a.currentTime = 0; });
  }, toolResult(i));
  await frame.evaluate(() => Promise.all([...document.images].map((im) => im.decode().catch(() => null))));
  if (states.mock_clip && i === last && !(await injectPill(frame))) {
    await refuse(`no clip named "${states.mock_clip}" in the last state to mark as mocked`);
  }
}

// Show state `target` with its motion run `ms` milliseconds in (a large ms is
// the settled picture). Earlier states are delivered first and settled, so
// the app's log and its motion are those of a real session.
async function showState(page, ctx, target, ms) {
  if (target < ctx.cur) await reload(page, ctx);
  const frame = await appFrameOf(page, ctx.id);
  while (ctx.cur < target) {
    ctx.cur += 1;
    await deliver(frame, ctx.cur);
    if (ctx.cur < target) await frame.evaluate(() => document.getAnimations().forEach((a) => a.finish()));
  }
  if (target >= 0) await frame.evaluate((ms) => document.getAnimations().forEach((a) => { a.currentTime = ms; }), ms);
  return frame;
}

async function motionLength(frame) {
  return frame.evaluate(() => Math.max(0, ...document.getAnimations().map((a) => a.effect.getComputedTiming().endTime)));
}

// The MOCK pill goes INSIDE the mocked tile, so it moves and grows with it.
async function injectPill(frame) {
  return frame.evaluate(({ name, pal }) => {
    const clip = [...document.querySelectorAll("#canvas .clip")].find((c) => (c.title || "").startsWith(name + " ·"));
    if (!clip) return false;
    const pill = document.createElement("div");
    pill.className = "hero-mock"; pill.textContent = "MOCK";
    Object.assign(pill.style, { position: "absolute", top: "4px", right: "4px", zIndex: "3",
      font: "700 8px/1 ui-monospace, Menlo, monospace", letterSpacing: ".08em", color: pal["accent-ink"],
      background: pal["accent-fill"], padding: "2px 4px", borderRadius: "3px", whiteSpace: "nowrap" });
    clip.appendChild(pill);
    // laid in with its tile: hidden, like the tile's own name, until the tile is whole
    const lay = clip.getAnimations()[0];
    if (lay) { const a = pill.animate(window.__fcpApp.hideUntilSettled, lay.effect.getTiming()); a.pause(); a.currentTime = lay.currentTime; }
    return true;
  }, { name: states.mock_clip, pal: states.palette });
}

// Settled-layout checks on one app: no hidden name, the pill on its tile.
async function layoutProblems(frame, where) {
  const out = await frame.evaluate(() => {
    const out = [...document.querySelectorAll(".clip .label.unfit, .mlabel.unfit")]
      .map((e) => "the app had to hide " + JSON.stringify(e.textContent || e.title));
    const pill = document.querySelector(".hero-mock");
    if (pill) {
      const clip = pill.parentElement, c = clip.getBoundingClientRect(), p = pill.getBoundingClientRect();
      const r = document.createRange(); r.selectNodeContents(clip.querySelector(".label")); const t = r.getBoundingClientRect();
      if (p.left < c.left + 4 || p.bottom > c.bottom - 4) out.push("the MOCK pill does not fit on its tile");
      if (p.left < t.right + 2 && t.left < p.right + 2 && p.top < t.bottom + 2 && t.top < p.bottom + 2) out.push("the MOCK pill would cover the tile's label");
    }
    return out;
  });
  return out.map((s) => where + ": " + s);
}

// Each state settled, in order, on a fresh app: every check that must hold.
// Returns the rightmost ruler text over all states.
async function checkAllStates(page, ctx, where) {
  await reload(page, ctx);
  let textRight = 0;
  for (let i = 0; i <= last; i++) {
    const frame = await showState(page, ctx, i, 1e6);
    const problems = await layoutProblems(frame, `${where}, state ${i}`);
    if (problems.length) await refuse(problems.join("; "));
    await refuseStale(`${where}, state ${i}`, await frame.evaluate(() => document.body.innerText), "app");
    textRight = Math.max(textRight, await rulerTextRight(frame));
  }
  return textRight;
}

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: SCALE, colorScheme: "dark" });
  page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
  await page.setContent(host);
  await waitReady(page, "app");
  const ctx = { id: "app", cur: -1 };
  await checkAllStates(page, ctx, "the MP4");

  // Settled on the finished cut: measure, then every host-side check.
  const frameBox = await page.locator("#frame").boundingBox();
  const appFr = await appFrameOf(page, "app");
  const server = await appFr.evaluate(() => {
    const card = document.getElementById("log").closest(".card"), r = card.getBoundingClientRect();
    const rects = (node) => { const range = document.createRange(); range.selectNodeContents(node);
      return [...range.getClientRects()].map((q) => ({ x: q.left, y: q.top, w: q.width, h: q.height })); };
    return { x: r.left, y: r.top, w: r.width, h: r.height, viewH: document.documentElement.clientHeight,
             words: [...rects(card.querySelector("h2")), ...rects(document.getElementById("log"))] };
  });
  if (server.y + server.h > server.viewH - 12) await refuse(`the SERVER panel ends at ${server.y + server.h}px, the window at ${server.viewH}px`);
  await page.evaluate((c) => { window.__capAt = c; }, { top: server.y + 7, right: frameBox.width - 2 - (server.x + server.w) + 10 });
  const lineOf = (t) => {
    let shown = -1, since = 0, li = 0;
    states.lines.forEach((l, i) => { if (l.state != null && t >= l.at + LEAD) { shown = l.state; since = t - (l.at + LEAD); li = i; } });
    return { shown, since, li };
  };
  const end = lineOf(states.loop_start);
  await page.evaluate(({ t, s }) => window.__seek(t, s.shown, s.since, s.li), { t: states.loop_start, s: end });
  await refuseStale("the hero", await page.evaluate(() => document.body.innerText), "host");
  const footerHit = await page.evaluate(() => {
    const f = document.getElementById("mock").getBoundingClientRect();
    return [...document.querySelectorAll("#claude .line")].some((e) => e.getBoundingClientRect().bottom > f.top - 6);
  });
  if (footerHit) await refuse("the footer note runs into the tool calls");
  const overflow = await page.evaluate(() => [...document.querySelectorAll("#chat .call, #chat .note")]
    .filter((e) => e.getBoundingClientRect().right > document.getElementById("chat").getBoundingClientRect().right + 0.5)
    .map((e) => e.textContent));
  if (overflow.length) await refuse("chat lines wider than their column: " + JSON.stringify(overflow));
  const cap = await page.locator("#cap").boundingBox();
  const ox = frameBox.x + 1, oy = frameBox.y + 1;
  const inside = cap && cap.x >= ox + server.x && cap.x + cap.width <= ox + server.x + server.w &&
                 cap.y >= oy + server.y && cap.y + cap.height <= oy + server.y + server.h;
  if (!inside) await refuse("the caption is not inside the SERVER panel: " + JSON.stringify({ cap, server }));
  const hit = server.words.find((q) => cap.x < ox + q.x + q.w && ox + q.x < cap.x + cap.width &&
                                       cap.y < oy + q.y + q.h && oy + q.y < cap.y + cap.height);
  if (hit) await refuse("the caption covers the SERVER panel's own text: " + JSON.stringify(hit));
  const sc = await appFr.locator("#scroller").boundingBox();
  fs.writeFileSync(path.join(assetsDir, "hero-layout.json"), JSON.stringify({
    mp4: { size: [W * SCALE, H * SCALE], timeline: [sc.x, sc.y, sc.width, sc.height].map((v) => Math.round(v * SCALE)) },
  }) + "\n");

  // The MP4, from the finished cut (frame 0) round the loop.
  await page.waitForTimeout(300);
  const total = Math.round(states.duration * FPS);
  const offset = Math.round(states.loop_start * FPS) % total;
  for (let k = 0; k < total; k++) {
    const t = ((k + offset) % total) / FPS, s = lineOf(t);
    await showState(page, ctx, s.shown, s.since * 1000);
    await page.evaluate(({ t, s }) => window.__seek(t, s.shown, s.since, s.li), { t, s });
    await page.screenshot({ path: path.join(framesDir, String(k).padStart(5, "0") + ".png") });
    if (k % 150 === 0) console.log("frame", k, "/", total);
  }
  await showState(page, ctx, end.shown, 1e6);
  await page.evaluate(({ t, s }) => window.__seek(t, s.shown, s.since, s.li), { t: states.loop_start, s: end });
  await page.screenshot({ path: path.join(assetsDir, "hero-still.png") });
  console.log("mp4 frames:", total);

  await writeGif(browser);
  await writeCardFrame(browser);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });

// The right edge of the ruler text the app shows in this state (ticks and
// marker names), in the iframe's coordinates: the GIF's crop must hold it.
async function rulerTextRight(frame) {
  return frame.evaluate(() => Math.max(0, ...[...document.querySelectorAll(".tick, .mlabel")]
    .filter((e) => !e.classList.contains("unfit")).map((e) => {
      const r = document.createRange(); r.selectNodeContents(e); return r.getBoundingClientRect().right; })));
}

// Inside the panel: from just left of the playhead to just past the end of
// the sequence and the ruler text (or the scroller's inner right edge), ruler
// top to the spine.
async function timelineBox(frame, gutter, textRight = 0) {
  return frame.evaluate(({ gutter, G, textRight }) => {
    const sc = document.getElementById("scroller").getBoundingClientRect(), st = window.__fcpApp.state;
    const lanes = [...document.querySelectorAll(".lane")].map((l) => l.getBoundingClientRect().bottom);
    const [n, d] = String(st.payload.duration).split("/").map(Number), dur = d ? n / d : n;
    const endX = G + st.pps * dur;  // the app's x(duration), in canvas pixels
    const left = sc.left + 1 + (gutter ? 0 : G - 8);
    // (clear of the scroller's rounded corners: radius 8)
    const right = Math.min(sc.right - 9, Math.max(sc.left + 1 + endX + (gutter ? 12 : 8), textRight + 2));
    return { x: left, y: sc.top + 1, w: right - left, h: Math.max(...lanes) + 3 - (sc.top + 1) };
  }, { gutter, G: GUTTER, textRight });
}

async function writeGif(browser) {
  const gp = await browser.newPage({ viewport: { width: Math.ceil(GIF_APP_W * GIF_ZOOM) + 8, height: Math.ceil(620 * GIF_ZOOM) },
                                     deviceScaleFactor: SCALE, colorScheme: "dark" });
  await gp.setContent(gifHost);
  await waitReady(gp, "gapp");
  const ctx = { id: "gapp", cur: -1 };
  const textRight = await checkAllStates(gp, ctx, "the GIF");
  const box = await timelineBox(await appFrameOf(gp, "gapp"), false, textRight);
  // The point of the GIF: the app's 10 px ruler type at 10 px or more when a
  // phone shows the GIF 358 px wide.
  const atPhone = 10 * 358 / box.w;
  if (atPhone < 10) await refuse(`the GIF's crop is ${box.w.toFixed(1)} CSS px wide: its 10 px type would be ${atPhone.toFixed(2)} px at 358`);
  const clip = { x: box.x * GIF_ZOOM, y: box.y * GIF_ZOOM, width: box.w * GIF_ZOOM, height: box.h * GIF_ZOOM };
  const dir = path.join(work, "gif-frames");
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir);
  const holds = [];
  const shoot = async (ms) => {
    const name = String(holds.length).padStart(5, "0") + ".png";
    await gp.screenshot({ path: path.join(dir, name), clip });
    holds.push([name, ms]);
  };
  for (const seg of states.gif_story) {
    const holdMs = Math.round(seg.hold * 1000);
    if (!seg.moves) {
      await showState(gp, ctx, seg.state, 1e6);  // an earlier state: a fresh app, settled
      await shoot(holdMs);
      continue;
    }
    const frame = await showState(gp, ctx, seg.state, 0);
    const len = Math.ceil(await motionLength(frame));
    let used = 0;
    for (let ms = 0; ms < len; ms += GIF_STEP_MS) {
      await frame.evaluate((ms) => document.getAnimations().forEach((a) => { a.currentTime = ms; }), ms);
      await shoot(GIF_STEP_MS);
      used += GIF_STEP_MS;
    }
    await frame.evaluate(() => document.getAnimations().forEach((a) => { a.currentTime = 1e6; }));
    await shoot(Math.max(GIF_STEP_MS, holdMs - used));
  }
  fs.writeFileSync(path.join(dir, "durations.json"), JSON.stringify(holds));
  console.log(`gif: ${holds.length} pictures, ${Math.round(clip.width * SCALE)}x${Math.round(clip.height * SCALE)} px,`,
              `type ${10 * GIF_ZOOM * SCALE}-${11 * GIF_ZOOM * SCALE} px, ${holds.reduce((a, h) => a + h[1], 0) / 1000}s`);
  await gp.close();
}

async function writeCardFrame(browser) {
  const cp = await browser.newPage({ viewport: { width: Math.ceil(CARD_APP_W * CARD_ZOOM) + 8, height: Math.ceil(620 * CARD_ZOOM) },
                                     deviceScaleFactor: SCALE, colorScheme: "dark" });
  await cp.setContent(cardHost);
  await waitReady(cp, "capp");
  const ctx = { id: "capp", cur: -1 };
  const textRight = await checkAllStates(cp, ctx, "the card");
  const box = await timelineBox(await appFrameOf(cp, "capp"), true, textRight);
  await cp.screenshot({ path: path.join(assetsDir, "card-frame.png"),
                        clip: { x: box.x * CARD_ZOOM, y: box.y * CARD_ZOOM, width: box.w * CARD_ZOOM, height: box.h * CARD_ZOOM } });
  await cp.close();
  console.log("card frame written");
}
