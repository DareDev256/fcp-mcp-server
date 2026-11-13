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
// screenshot cannot drift the animation. Frames are 860x600 CSS pixels at a
// device scale of 2 (1720x1200): the app gets 500 CSS pixels, so its own
// 10-11px type is 20-22 px in a README GIF only 1000 px wide.
//
// The story opens on the imported cut (states.start_state), so there is no
// empty stage; each write cross-fades (180 ms) to the re-rendered timeline.
// Every colour is a palette token handed over in states.json
// (fcpxml/palette.py); none is typed here. Frames are captured from
// states.loop_start round to it, so frame 0 of the MP4 and the GIF is the
// finished cut. Written beside the frames: gif-frames/, the same frames
// cut to the timeline (header, ruler, lanes) with every state clipped at its
// own timeline's foot, for the README GIF. The assets dir gets
// hero-still.png (the finished frame) and
// card-frame.png (four calls beside the ruler and lanes at 1.75x).
//
// A mocked B-roll tile (states.mock_clip) carries a solid MOCK pill at its
// top right, measured onto the tile, never over its label.
//
// It REFUSES (exit 1) rather than record a frame anyone would have to excuse:
// a chat line wider than its column, a clip or marker name the app had to
// hide, a SERVER panel cut by the window, the caption pill outside it or
// over its text, the MOCK pill off its tile or over its label, the footer
// over the tool calls, or any text that would go stale in an image (a
// price, a model id, a frame size, a version: scripts/image_text.py,
// handed over in states.stale).
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
const FPS = 30, W = 860, H = 600, SCALE = 2, FADE = 0.18;
const last = states.states.length - 1;

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
  .note { color:var(--mute); font-size:11.5px; margin-left:14px; margin-top:1px; white-space:nowrap; }
  .pill { position:absolute; font: 700 8px/1 ui-monospace, Menlo, monospace; letter-spacing:.08em; color:var(--accent-ink);
    background:var(--accent-fill); padding:2px 4px; border-radius:3px; z-index:7; white-space:nowrap; }`;

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
  .app { position:absolute; inset:0; width:100%; height:100%; border:0; opacity:0; background:var(--ground); }
  body.gif .app { clip-path: inset(0 0 var(--below, 0px) 0); -webkit-mask-image: var(--fade-mask, none); mask-image: var(--fade-mask, none); }
  #cap { position:absolute; font: 600 11px/1 ui-monospace, Menlo, monospace; color:var(--accent); white-space:nowrap;
    background:var(--panel); padding:5px 8px; border-radius:6px; border:1px solid var(--line-strong); opacity:0; z-index:5; }
</style></head><body>
<div id="top"><span><b>fcp-mcp-server</b> · watch Claude cut</span><span>ui://fcp/timeline · MCP Apps</span></div>
<div id="chat">
  <div id="you"><div class="who">You</div><div class="prompt" id="prompt"></div></div>
  <div id="claude"><div class="who" id="cwho" style="opacity:0">Claude · tool calls</div>
    ${states.lines.map((l, i) => `<div class="line" id="l${i}"><div class="call">${l.call}</div><div class="note">${l.note}</div></div>`).join("")}
  </div>
  <div id="mock">${states.footer}</div>
</div>
<div id="stage"><div id="frame">${states.states.map((s, i) => appFrame("app" + i, i)).join("")}<div id="cap"></div><div class="pill" id="mockpill" style="opacity:0">MOCK</div></div></div>
<script>
  ${protocol}
  const LINES = ${JSON.stringify(states.lines)};
  const PROMPT = ${JSON.stringify(states.prompt)};
  const START = ${JSON.stringify(states.start_state)}, LAST = ${last};
  window.__capAt = [];   // per state: where the SERVER panel sits inside #frame (measured by the recorder)
  window.__pillAt = null; // where the MOCK pill sits on the mocked tile, in the last state
  window.__footRel = null; // per state: its timeline's foot from the iframe's top (the GIF pass)
  const frames = STATES.map((s, i) => document.getElementById("app" + i));
  const clamp = (v) => Math.max(0, Math.min(1, v));
  const ease = (v) => { v = clamp(v); return v < .5 ? 2 * v * v : 1 - Math.pow(-2 * v + 2, 2) / 2; };
  const T = { youIn: 0.3, typeStart: 0.5, typeEnd: 3.0, claudeIn: 3.3, fade: ${FADE} };
  window.__seek = function (ms) {
    const t = ms / 1000;
    document.getElementById("you").style.opacity = ease((t - T.youIn) / 0.3);
    const n = Math.round(clamp((t - T.typeStart) / (T.typeEnd - T.typeStart)) * PROMPT.length);
    const caret = t >= T.youIn && t < T.typeEnd + 0.6 && (Math.floor(t * 2.5) % 2 === 0) ? '<span class="caret"></span>' : "";
    document.getElementById("prompt").innerHTML = PROMPT.slice(0, n) + caret;
    document.getElementById("cwho").style.opacity = ease((t - T.claudeIn) / 0.4);
    // The story opens on the imported cut, cross-fading in from the finished
    // one, so the loop's seam is a 180 ms dissolve, never an empty stage.
    let shown = START, prevShown = LAST, since = t, lineShown = null;
    LINES.forEach((l, i) => {
      const p = ease((t - l.at) / 0.45);
      const el = document.getElementById("l" + i);
      el.style.opacity = p; el.style.transform = "translateY(" + (6 - 6 * p) + "px)";
      if (l.state != null && t >= l.at + 0.25) { prevShown = shown; shown = l.state; since = t - (l.at + 0.25); lineShown = l; }
    });
    const prog = ease(since / T.fade);
    frames.forEach((f, i) => {
      let o = 0;
      if (i === shown) o = prog;
      if (i === prevShown && prevShown !== shown && prog < 1) o = 1;
      if (i === shown && prevShown === shown) o = 1;
      f.style.opacity = o;
      f.style.zIndex = i === shown ? 2 : (i === prevShown ? 1 : 0);
      // GIF pass only (body.gif): going to a SHORTER timeline, the outgoing
      // one fades out below the incoming one's foot instead of staying whole
      // under it, so its lower lanes never show as a second row. A mask reads
      // only alpha: the ground token is just an opaque stop.
      const fr = window.__footRel;
      let mask = "none";
      if (fr && i === prevShown && prevShown !== shown && prog < 1 && fr[shown] < fr[prevShown]) {
        const keep = "color-mix(in srgb, var(--ground) " + ((1 - prog) * 100).toFixed(1) + "%, transparent)";
        mask = "linear-gradient(to bottom, var(--ground) 0, var(--ground) " + fr[shown] + "px, " + keep + " " + fr[shown] + "px)";
      }
      f.style.setProperty("--fade-mask", mask);
    });
    // The caption sits inside the app's SERVER panel, top right, where the
    // server's own log speaks: it says which call's output is on screen.
    const cap = document.getElementById("cap");
    if (lineShown && window.__capAt[shown]) {
      const at = window.__capAt[shown];
      cap.textContent = (shown === 0 ? "rendered from " : "re-rendered from ") + lineShown.call + " output";
      cap.style.top = at.top + "px"; cap.style.right = at.right + "px";
      cap.style.opacity = ease((since - 0.2) / 0.4);
    } else cap.style.opacity = 0;
    const pill = document.getElementById("mockpill");
    if (window.__pillAt) {
      pill.style.left = window.__pillAt.left + "px"; pill.style.top = window.__pillAt.top + "px";
      pill.style.opacity = shown === LAST ? (prevShown === LAST ? 1 : prog) : (prevShown === LAST ? 1 - prog : 0);
    }
  };
</script></body></html>`;

// The social card's picture: four calls beside the finished timeline's ruler
// and lanes only (no header, no buttons), scaled 1.75x so it reads at phone
// width. Real text, the real app, one frame.
const CARD_W = 1100, CHAT_W = 214, APP_W = 480, ZOOM = 1.75;
const cardHost = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; background:var(--ground); color:var(--ink); font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #card { display:flex; gap:22px; align-items:center; width:max-content; box-sizing:border-box; padding:18px;
    background:var(--ground); }
  #cchat { width:${CHAT_W}px; flex:none; }
  #cchat .prompt { margin-bottom:16px; }
  #cchat .line { margin-bottom:7px; }
  ${chatCss}
  #cstage { position:relative; flex:none; overflow:hidden; border:1px solid var(--line); border-radius:10px; background:var(--ground); }
  #czoom { position:absolute; left:0; top:0; transform-origin:0 0; transform:scale(${ZOOM}); }
  #czoom iframe { position:absolute; width:${APP_W}px; height:900px; border:0; background:var(--ground); }
</style></head><body><div id="card">
  <div id="cchat"><div class="who">You</div><div class="prompt">${states.prompt}</div>
    <div class="who">Claude · tool calls</div>
    ${states.card_calls.map((c) => `<div class="line"><div class="call">${c}</div></div>`).join("")}
  </div>
  <div id="cstage"><div id="czoom">${appFrame("capp", last)}</div><div class="pill" id="cpill" style="display:none">MOCK</div></div>
</div><script>${protocol}</script></body></html>`;

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

// Labels the app had to hide in one iframe: a name the viewer cannot read.
async function hiddenNames(frame) {
  return frame.locator("body").evaluate(() =>
    [...document.querySelectorAll(".clip .label.unfit, .mlabel.unfit")].map((e) => e.textContent || e.title));
}

// The mocked tile and its label's text box, in the iframe's own coordinates.
async function mockTile(frame) {
  if (!states.mock_clip) return null;
  return frame.locator("body").evaluate((b, name) => {
    const clip = [...document.querySelectorAll(".clip")].find((c) => (c.title || "").startsWith(name + " ·"));
    if (!clip) return null;
    const r = clip.getBoundingClientRect(), label = clip.querySelector(".label"), range = document.createRange();
    range.selectNodeContents(label);
    const t = range.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height, text: { x: t.left, y: t.top, w: t.width, h: t.height } };
  }, states.mock_clip);
}

function overlaps(a, b, pad) {
  return a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
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

  // The MOCK pill: top right of the mocked tile, 4px in, never over its label.
  const tile = await mockTile(page.frameLocator("#app" + last));
  if (states.mock_clip && !tile) await refuse(`no clip named "${states.mock_clip}" in the last state to mark as mocked`);
  if (tile) {
    const pw = await page.locator("#mockpill").evaluate((e) => [e.offsetWidth, e.offsetHeight]);
    const pill = { x: tile.x + tile.w - pw[0] - 4, y: tile.y + 4, w: pw[0], h: pw[1] };
    if (pill.x < tile.x + 4 || pill.y + pill.h > tile.y + tile.h - 4) await refuse("the MOCK pill does not fit on its tile: " + JSON.stringify({ tile, pill }));
    if (overlaps(pill, tile.text, 2)) await refuse("the MOCK pill would cover the tile's label: " + JSON.stringify({ pill, label: tile.text }));
    await page.evaluate((p) => { window.__pillAt = { left: p.x, top: p.y }; }, pill);
  }

  // Every text check runs on the finished frame BEFORE the capture, so a
  // refusal costs seconds, not a full recording.
  await page.evaluate((ms) => window.__seek(ms), states.loop_start * 1000);
  await refuseStale("the hero", await page.evaluate(() => document.body.innerText), "host");
  for (let i = 0; i < states.states.length; i++) {
    await refuseStale(`app state ${i}`, await page.frameLocator("#app" + i).locator("body").evaluate((b) => b.innerText), "app");
  }
  const footerHit = await page.evaluate(() => {
    const f = document.getElementById("mock").getBoundingClientRect();
    const lines = [...document.querySelectorAll("#claude .line")].map((e) => e.getBoundingClientRect());
    return lines.some((l) => l.bottom > f.top - 6);
  });
  if (footerHit) await refuse("the footer note runs into the tool calls");
  const overflow = await page.evaluate(() => [...document.querySelectorAll("#chat .call, #chat .note")]
    .filter((e) => e.getBoundingClientRect().right > document.getElementById("chat").getBoundingClientRect().right + 0.5)
    .map((e) => e.textContent));
  if (overflow.length) await refuse("chat lines wider than their column: " + JSON.stringify(overflow));
  const cap = await page.locator("#cap").boundingBox(), sv = capAt[last].box;
  const ox = frameBox.x + 1, oy = frameBox.y + 1;
  const inside = cap.x >= ox + sv.x && cap.x + cap.width <= ox + sv.x + sv.w && cap.y >= oy + sv.y && cap.y + cap.height <= oy + sv.y + sv.h;
  if (!inside) await refuse("the caption pill is not inside the SERVER panel: " + JSON.stringify({ cap, sv }));
  const hit = sv.words.find((q) => cap.x < ox + q.x + q.w && ox + q.x < cap.x + cap.width &&
                                   cap.y < oy + q.y + q.h && oy + q.y < cap.y + cap.height);
  if (hit) await refuse("the caption pill covers the SERVER panel's own text: " + JSON.stringify(hit));

  // The README GIF is the timeline alone: header, ruler and lanes of the
  // finished state, down to 6px under its timeline (half the gap to the
  // SELECTION panel, so not a sliver of that panel shows). Earlier states
  // have shorter timelines, so in the GIF pass each state's iframe is
  // clipped at its OWN foot: a cross-fade then blends two clean timelines,
  // and no panel is ever sliced by the crop's bottom edge.
  const foot = [];
  for (let i = 0; i < states.states.length; i++) {
    const sb = await page.frameLocator("#app" + i).locator("#scroller").boundingBox();
    foot.push(sb.y + sb.height + 6);
  }
  const taller = foot.findIndex((f) => f > foot[last] + 0.5);
  if (taller >= 0) await refuse(`state ${taller}'s timeline is taller than the finished one: the GIF crop would cut it`);
  await page.evaluate((f) => {
    document.querySelectorAll("iframe.app").forEach((el) => {
      el.style.setProperty("--below", (el.getBoundingClientRect().bottom - f[Number(el.dataset.state)]) + "px");
    });
    const top = document.querySelector("iframe.app").getBoundingClientRect().top;
    window.__footRel = f.map((y) => y - top);
  }, foot);
  const gifClip = { x: frameBox.x, y: frameBox.y, width: frameBox.width, height: foot[last] - frameBox.y };
  const gifDir = path.join(work, "gif-frames");
  fs.rmSync(gifDir, { recursive: true, force: true });
  fs.mkdirSync(gifDir);

  await page.waitForTimeout(500);  // fonts and thumbnails settle before the first frame
  const total = Math.round(states.duration * FPS);
  const offset = Math.round(states.loop_start * FPS) % total;  // frame 0 = the finished cut
  for (let k = 0; k < total; k++) {
    await page.evaluate((ms) => window.__seek(ms), (((k + offset) % total) / FPS) * 1000);
    const name = String(k).padStart(5, "0") + ".png";
    await page.screenshot({ path: path.join(framesDir, name), animations: "disabled" });
    await page.evaluate(() => document.body.classList.add("gif"));
    await page.screenshot({ path: path.join(gifDir, name), clip: gifClip, animations: "disabled" });
    await page.evaluate(() => document.body.classList.remove("gif"));
    if (k % 150 === 0) console.log("frame", k, "/", total);
  }
  if (assetsDir) {
    await page.evaluate((ms) => window.__seek(ms), states.loop_start * 1000);
    await page.screenshot({ path: path.join(assetsDir, "hero-still.png"), animations: "disabled" });
    await writeCardFrame(browser);
  }
  await browser.close();
  console.log("frames:", total);
})().catch((e) => { console.error(e); process.exit(1); });

async function writeCardFrame(browser) {
  const card = await browser.newPage({ viewport: { width: CARD_W, height: 900 }, deviceScaleFactor: SCALE, colorScheme: "dark" });
  await card.setContent(cardHost);
  await card.waitForFunction(() => window.__ready.capp === true, null, { timeout: 20000 });
  const cf = card.frameLocator("#capp");
  await cf.locator("#scroller").waitFor({ state: "visible", timeout: 10000 });
  await card.waitForTimeout(300);
  // Crop to the scroller (ruler + lanes), down to the last lane's border:
  // shift the iframe so it fills the stage, with no empty band at its foot.
  const sc = await cf.locator("#scroller").evaluate((el) => { const r = el.getBoundingClientRect();
    const lanes = [...el.querySelectorAll(".lane")].map((l) => l.getBoundingClientRect().bottom);
    return { x: r.left, y: r.top, w: r.width, h: Math.min(r.height, Math.max(...lanes) - r.top + 1) }; });
  await card.evaluate(({ sc, zoom }) => {
    const stage = document.getElementById("cstage"), iframe = document.querySelector("#czoom iframe");
    iframe.style.left = -sc.x + "px"; iframe.style.top = -sc.y + "px";
    stage.style.width = Math.round(sc.w * zoom) + "px"; stage.style.height = Math.round(sc.h * zoom) + "px";
  }, { sc, zoom: ZOOM });
  await card.waitForTimeout(300);
  const hidden = await hiddenNames(cf);
  if (hidden.length) await refuse("card: the app had to hide " + JSON.stringify(hidden));
  const tile = await mockTile(cf);
  if (tile) {
    await card.evaluate(({ tile, sc, zoom }) => {
      const pill = document.getElementById("cpill");
      pill.style.display = "block";
      pill.style.left = ((tile.x + tile.w - sc.x) * zoom - pill.offsetWidth - 5) + "px";
      pill.style.top = ((tile.y - sc.y) * zoom + 5) + "px";
    }, { tile, sc, zoom: ZOOM });
    const clash = await card.evaluate(({ tile, sc, zoom }) => {
      const p = document.getElementById("cpill").getBoundingClientRect(), s = document.getElementById("cstage").getBoundingClientRect();
      const t = { x: s.left + (tile.text.x - sc.x) * zoom, y: s.top + (tile.text.y - sc.y) * zoom, w: tile.text.w * zoom, h: tile.text.h * zoom };
      return p.left < t.x + t.w + 2 && t.x < p.right + 2 && p.top < t.y + t.h + 2 && t.y < p.bottom + 2;
    }, { tile, sc, zoom: ZOOM });
    if (clash) await refuse("card: the MOCK pill would cover the tile's label");
  }
  await refuseStale("the card frame", await card.evaluate(() => document.body.innerText), "host");
  await refuseStale("the card's app", await cf.locator("body").evaluate((b) => b.innerText), "app");
  const cardOverflow = await card.evaluate(() => [...document.querySelectorAll("#cchat .call")]
    .filter((e) => e.getBoundingClientRect().right > document.getElementById("cchat").getBoundingClientRect().right + 0.5)
    .map((e) => e.textContent));
  if (cardOverflow.length) await refuse("card: chat lines wider than their column: " + JSON.stringify(cardOverflow));
  await card.locator("#card").screenshot({ path: path.join(assetsDir, "card-frame.png"), animations: "disabled", omitBackground: false });
  await card.close();
  console.log("still + card frame written to", assetsDir);
}
