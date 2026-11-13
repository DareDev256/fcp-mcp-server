// The README hero's recorder: a composed stage around the REAL timeline view.
// Run by build.py; not meant to be run alone.
//
//   node demo/hero/record.js <work dir> <frames dir> <assets dir> [--card]
//
// What is real: the view is the ui://fcp/timeline shell exactly as the server
// serves it (work/app.html), one app instance driven through the MCP Apps
// draft protocol and fed the real view_timeline results in order; every
// change on screen is the app's own animation between two of them (Web
// Animations, which this recorder pauses and seeks per frame, so every frame
// is a pure function of the story clock).
// What is composed: the stage (only the view's ruler and lanes, drawn 2.24x
// on true black, no SELECTION or SERVER panels), the captions (the prompt,
// then one chip per call: the call lands whole and its result note types at
// a reading pace, then holds), the timing, an empty beat (the app is handed
// the imported cut with its clips taken out, so they leave, and then the
// real result again, so they are drawn in on the view_timeline call), and a
// virtual camera: a CSS transform on the stage that leads each edit by
// 0.3 s, pushes 1.25-1.4x onto what it changes and eases back out. Text the
// frame's edge would cut is faded out whole before it gets there.
//
// The story starts on the finished cut (frame 0, the poster), the prompt
// types over it, the app rewinds (its own undo motion), the stage empties,
// then the calls play in order and end on the finished cut again.
//
// HARD GATES: it REFUSES (exit 1) on any frame with a doubled, dropped or
// clipped name (cut by its clip, by the view's edge or by the frame's edge,
// leaving clips included), a thumbed clip without its scrim, a thumbnail not
// tiled at its own aspect, the view covering under 60% of the frame; on any
// text under 10 px when the frame is shown 358 px wide (measured from every
// text the view shows in any state, the MOCK badge included); on a push
// outside 1.25-1.4x or one whose subject does not fit the frame; on a caption
// wider than the frame, a result held fully typed under 1.5 s (the prompt
// under 1.8 s); on a split that closes in under 12 frames; on any frame that
// changes the number of clips on screen by more than one without motion on
// both sides; on a name the app had to hide in any state, and on any text
// that would go stale in an image (scripts/image_text.py). build.py refuses
// a hold over 1.5 s and a loop whose last frame does not match frame 0.
//
// With --card it also writes card-frame.png (the social card's picture: the
// finished marker rows and lanes at 1.75x, no ruler times). Without it the
// card is left alone: its bytes are pinned (tests/test_front_door.py).
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

const [work, framesDir, assetsDir] = process.argv.slice(2);
const WITH_CARD = process.argv.includes("--card");
const states = JSON.parse(fs.readFileSync(path.join(work, "states.json"), "utf8"));
const story = states.story;
const app = fs.readFileSync(path.join(work, "app.html"), "utf8");
const FPS = 30, W = 800, H = 460, DSF = 1.6, PHONE = 358;
const APP_W = 418;                         // the view's layout width: every name fits, cut and uncut
const STAGE_TOP = 80, SH = H - STAGE_TOP;  // the view's stage, under the captions
const BOX = { x: 32, y: 4, w: 736 };       // the view at rest, in stage coordinates (its height follows from the zoom)
const PUSH_MIN = 1.25, PUSH_MAX = 1.4, EDGE = 4, MARGIN = 6;  // EDGE: px over which text fades out before the frame's edge
const CAPTION_PX = 23, MOCK_PX = 10.5, CPS = story.cps;
const CARD_APP_W = 480, CARD_ZOOM = 1.75, GUTTER = 48;
const SPLIT_MIN_FRAMES = 12, HOLD_MIN = 1.5, PROMPT_HOLD_MIN = 1.8;
const last = states.states.length - 1;

const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const tokens = Object.entries(states.palette).map(([k, v]) => `--${k}:${v};`).join(" ");
const appFrame = (id, w) => `<iframe class="app" id="${id}" style="width:${w}px;height:420px" sandbox="allow-scripts" srcdoc="${esc(app)}"></iframe>`;
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");

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

// The prompt is laid out at its final wrap from the first character: the
// untyped rest is in the line, transparent, so no word moves while it types,
// and line 2 starts under line 1's text, not under "You".
const stage = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; width:${W}px; height:${H}px; background:var(--ground); color:var(--ink); overflow:hidden;
    font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #cap { position:absolute; left:${BOX.x}px; right:${W - BOX.x - BOX.w}px; top:10px; height:64px; display:flex; align-items:center; }
  #prompt { display:grid; grid-template-columns:auto 1fr; column-gap:.35em; font-size:${CAPTION_PX}px; line-height:1.25;
    font-weight:500; letter-spacing:-.01em; }
  #prompt b { color:var(--accent); font-weight:600; }
  #prompt .text { text-wrap:balance; }
  .rest { color:transparent; }
  #chip { display:flex; align-items:baseline; gap:16px; white-space:nowrap; }
  #chip .call { font: 600 ${CAPTION_PX}px/1 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--accent);
    border:1px solid var(--line-strong); border-radius:10px; padding:9px 14px; background:var(--panel); }
  #chip .note { font-size:${CAPTION_PX}px; color:var(--ink); }
  #stage { position:absolute; left:0; top:${STAGE_TOP}px; width:${W}px; height:${SH}px; overflow:hidden; }
  #cam { position:absolute; left:0; top:0; transform-origin:0 0; }
  #view { position:absolute; overflow:hidden; border-radius:10px; background:var(--panel); }
  #zoom { position:absolute; left:0; top:0; transform-origin:0 0; }
  #zoom iframe { position:absolute; border:0; background:var(--ground); }
</style></head><body>
<div id="cap"><div id="prompt"><b>You</b><span class="text"><span class="typed"></span><span class="rest"></span></span></div>
  <div id="chip"><span class="call"></span><span class="note"><span class="typed"></span><span class="rest"></span></span></div></div>
<div id="stage"><div id="cam"><div id="view"><div id="zoom">${appFrame("app", APP_W)}</div></div></div></div>
<script>${protocol}
  const set = (el, s, n) => { el.querySelector(".typed").textContent = s.slice(0, n); el.querySelector(".rest").textContent = s.slice(n); };
  // The captions at story time: the prompt with n characters typed, or one
  // chip (the call whole, its note with n characters typed).
  window.__caption = function (c) {
    const prompt = document.getElementById("prompt"), chip = document.getElementById("chip");
    prompt.style.display = c.prompt != null ? "" : "none"; chip.style.display = c.call != null ? "" : "none";
    if (c.prompt != null) set(prompt.querySelector(".text"), c.prompt, c.n);
    if (c.call != null) { chip.querySelector(".call").textContent = c.call; set(chip.querySelector(".note"), c.note, c.n); }
  };
  // What a viewer reads: the typed text of whichever caption shows.
  window.__readCaption = function () {
    const prompt = document.getElementById("prompt"), chip = document.getElementById("chip");
    if (prompt.style.display !== "none") return { kind: "prompt", typed: prompt.querySelector(".typed").textContent, rest: prompt.querySelector(".rest").textContent };
    return { kind: "chip", call: chip.querySelector(".call").textContent, typed: chip.querySelector(".note .typed").textContent,
             rest: chip.querySelector(".note .rest").textContent };
  };
  // The camera: the view's top left (x, y, stage px) and its scale.
  window.__camera = function (c) {
    document.getElementById("cam").style.transform = "translate(" + c.x + "px," + c.y + "px) scale(" + c.s + ")";
  };
</script></body></html>`;

// The social card's picture: the finished marker rows and lanes at CARD_ZOOM.
// The ruler times are left out (at 390 px a card shows them at 5 px) and
// their height goes to the lanes.
const cardHost = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; background:var(--ground); }
  #czoom { position:absolute; left:0; top:0; transform-origin:0 0; transform:scale(${CARD_ZOOM}); }
  #czoom iframe { position:absolute; left:0; top:0; border:0; background:var(--ground); }
</style></head><body><div id="czoom">${appFrame("capp", CARD_APP_W)}</div><script>${protocol}</script></body></html>`;
const CARD_CSS = ".tick { display: none !important; } .ruler { height: 30px !important; } .lane { height: 50px !important; } .lane.spine { height: 70px !important; }";

async function refuse(msg) { console.error("REFUSED: " + msg); process.exit(1); }

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

// EMPTY is the imported cut with its clips and markers taken out: delivered
// after the rewind, the clips leave (in turn); the real result after it draws
// them in again. It is the stage's empty beat, never a tool result.
const OPEN = "```json fcp-timeline-app", CLOSE = "```";
function emptied(text) {
  const a = text.indexOf(OPEN) + OPEN.length, b = text.indexOf(CLOSE, a);
  const p = JSON.parse(text.slice(a, b));
  p.clips = []; p.markers = []; p.gaps = []; p.lanes.forEach((l) => { l.clips = []; });
  return text.slice(0, a) + "\n" + JSON.stringify(p) + "\n" + text.slice(b);
}
const EMPTY = "empty";
const resultText = (i) => (i === EMPTY ? emptied(states.states[0].result) : states.states[i].result);
const toolResult = (i) => ({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
  params: { result: { content: [{ type: "text", text: resultText(i) }] } } });

async function appFrameOf(page, id) { return (await page.$("#" + id)).contentFrame(); }
async function waitReady(page, id) {
  await page.waitForFunction((id) => window.__ready[id] === true, id, { timeout: 20000 });
}

// The result for state i, as the host would send it, into the app AS IT IS
// NOW, so the app animates from whatever it was showing. Every animation is
// paused at once (at 0) for the recorder to seek; the set is kept, so a seek
// reaches animations that have run past their end.
async function deliver(frame, i) {
  await frame.evaluate((msg) => window.dispatchEvent(new MessageEvent("message", { data: msg })), toolResult(i));
  if (states.mock_clip && i === last && !(await injectPill(frame))) {
    await refuse(`no clip named "${states.mock_clip}" in the last state to mark as mocked`);
  }
  await fadeLeavingPill(frame);
  await frame.evaluate(() => { window.__anims = document.getAnimations(); window.__anims.forEach((a) => { a.pause(); a.currentTime = 0; }); });
  await frame.evaluate(() => Promise.all([...document.querySelectorAll(".thumbs")].map((t) => {
    const m = /url\("(.*)"\)/.exec(t.style.backgroundImage); if (!m) return null;
    const im = new Image(); im.src = m[1]; return im.decode().catch(() => null);
  })));
}
async function seek(frame, ms) {
  await frame.evaluate((ms) => (window.__anims || document.getAnimations()).forEach((a) => { a.currentTime = ms; }), ms);
}

// The MOCK badge goes INSIDE the mocked tile, so it moves with it, and is
// revealed (by colour, as the tile's own name is) once the tile is wide enough.
async function injectPill(frame) {
  return frame.evaluate(({ name, pal, px }) => {
    const clip = [...document.querySelectorAll("#canvas .clip:not(.ghost)")].find((c) => (c.title || "").startsWith(name + " ·"));
    if (!clip) return false;
    const pill = document.createElement("div");
    pill.className = "hero-mock"; pill.textContent = "MOCK";
    Object.assign(pill.style, { position: "absolute", top: "3px", right: "4px", zIndex: "3",
      font: `700 ${px}px/1 ui-monospace, Menlo, monospace`, letterSpacing: ".06em", color: pal["accent-ink"],
      background: pal["accent-fill"], padding: "1.5px 4px", borderRadius: "3px", whiteSpace: "nowrap" });
    clip.appendChild(pill);
    const m = clip.__motion;
    if (m) window.__fcpApp.revealWhenWide(pill, m.from, m.to, pill.offsetWidth + 8, true, m.delay);
    return true;
  }, { name: states.mock_clip, pal: states.palette, px: MOCK_PX });
}
// ...and fades out with it, before it is too narrow, when the tile leaves.
async function fadeLeavingPill(frame) {
  await frame.evaluate(() => document.querySelectorAll("#canvas .clip.ghost .hero-mock").forEach((pill) => {
    const m = pill.parentElement.__motion;
    if (m && !pill.__leaving) { pill.__leaving = true; window.__fcpApp.hideWhenNarrow(pill, m.from, m.to, pill.offsetWidth + 8, true, m.delay); }
  }));
}

// Settled-layout checks on one app: no hidden name, the badge on its tile.
async function layoutProblems(frame, where) {
  const out = await frame.evaluate(() => {
    const out = [...document.querySelectorAll(".clip:not(.ghost) .label.unfit, .mlabel.unfit")]
      .map((e) => "the app had to hide " + JSON.stringify(e.textContent || e.title));
    const pill = document.querySelector(".clip:not(.ghost) .hero-mock");
    if (pill) {
      const clip = pill.parentElement, c = clip.getBoundingClientRect(), p = pill.getBoundingClientRect();
      const r = document.createRange(); r.selectNodeContents(clip.querySelector(".label")); const t = r.getBoundingClientRect();
      if (p.left < c.left + 4 || p.bottom > c.bottom - 3) out.push("the MOCK badge does not fit on its tile");
      if (p.left < t.right + 2 && t.left < p.right + 2 && p.top < t.bottom + 2 && t.top < p.bottom + 2) out.push("the MOCK badge would cover the tile's label");
    }
    return out;
  });
  return out.map((s) => where + ": " + s);
}

// The view's region (iframe CSS px): just left of the playhead to just past
// the sequence and every ruler text, ruler top to the spine (lane tags and
// panels out). textRight is the rightmost ruler text over all states.
async function viewRegion(frame, gutter, textRight, foot) {
  return frame.evaluate(({ gutter, G, textRight, foot }) => {
    const sc = document.getElementById("scroller").getBoundingClientRect(), st = window.__fcpApp.state;
    const lanes = [...document.querySelectorAll(".lane")].map((l) => l.getBoundingClientRect().bottom);
    const [n, d] = String(st.payload.duration).split("/").map(Number), dur = d ? n / d : n;
    const left = sc.left + 1 + (gutter ? 0 : G - 8);
    const right = Math.min(sc.right - 9, Math.max(sc.left + 1 + G + st.pps * dur + (gutter ? 12 : 8), textRight + 2));
    return { x: left, y: sc.top + 1, w: right - left, h: Math.max(...lanes) + foot - (sc.top + 1) };
  }, { gutter, G: GUTTER, textRight, foot });
}
async function rulerTextRight(frame) {
  return frame.evaluate(() => Math.max(0, ...[...document.querySelectorAll(".tick, .mlabel")]
    .filter((e) => !e.classList.contains("unfit") && getComputedStyle(e).display !== "none").map((e) => {
      const r = document.createRange(); r.selectNodeContents(e); return r.getBoundingClientRect().right; })));
}
// Every visible piece of text in the timeline, settled: its own font size
// and its box (iframe CSS px). The type gate reads these, not an assumed size.
async function textSizes(frame) {
  return frame.evaluate(() => {
    const out = [];
    document.querySelectorAll("#scroller *").forEach((e) => {
      if (![...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) return;
      const cs = getComputedStyle(e);
      if (cs.visibility === "hidden" || cs.display === "none") return;
      const r = document.createRange(); r.selectNodeContents(e); const t = r.getBoundingClientRect();
      if (t.width > 0) out.push({ px: parseFloat(cs.fontSize), text: e.textContent.trim().slice(0, 24), l: t.left, r: t.right, t: t.top, b: t.bottom });
    });
    return out;
  });
}
// The box (iframe CSS px) of what a state shows that the camera should push onto.
async function boxOf(frame, selector) {
  return frame.evaluate((selector) => {
    const rs = [...document.querySelectorAll(selector)].filter((e) => getComputedStyle(e).visibility !== "hidden").map((e) => {
      if (e.classList.contains("mlabel")) { const r = document.createRange(); r.selectNodeContents(e); const t = r.getBoundingClientRect();
        const f = e.getBoundingClientRect(); return { left: f.left, right: t.right, top: f.top, bottom: f.bottom }; }
      return e.getBoundingClientRect(); });
    if (!rs.length) return null;
    return { x0: Math.min(...rs.map((r) => r.left)), x1: Math.max(...rs.map((r) => r.right)),
             y0: Math.min(...rs.map((r) => r.top)), y1: Math.max(...rs.map((r) => r.bottom)) };
  }, selector);
}

// Each state settled, in order, on a fresh app: every check that must hold.
// `sizes`, when given, collects every state's text sizes; `focus`, the boxes
// the camera pushes onto.
async function checkAllStates(page, id, where, sizes, focus) {
  const frame = await appFrameOf(page, id);
  let textRight = 0;
  for (let i = 0; i <= last; i++) {
    await deliver(frame, i);
    await seek(frame, 1e6);
    const problems = await layoutProblems(frame, `${where}, state ${i}`);
    if (problems.length) await refuse(problems.join("; "));
    await refuseStale(`${where}, state ${i}`, await frame.evaluate(() => document.body.innerText), "app");
    textRight = Math.max(textRight, await rulerTextRight(frame));
    if (sizes) sizes.push(...await textSizes(frame));
    if (focus) {
      if (i === 0) focus.squat = await boxOf(frame, `#canvas .clip:not(.ghost)[title^="${states.long_set} ·"]`);
      if (i === 2) {
        // The whole marker row is too wide to push 1.25x onto, so the camera
        // pans along it as the markers land, left to right: the first ones,
        // then the last ones, each subject whole in the frame.
        const titles = await frame.evaluate(() => [...document.querySelectorAll("#canvas .mlabel")]
          .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left).map((e) => e.title));
        const sel = (ts) => ts.map((t) => `#canvas .mlabel[title="${t.replace(/"/g, '\\"')}"]`).join(",");
        const k = Math.max(1, titles.length - 1);
        focus["markers<"] = await boxOf(frame, sel(titles.slice(0, k)));
        focus["markers>"] = await boxOf(frame, sel(titles.slice(titles.length - k)));
      }
      if (i === last) {
        const name = states.mock_clip || "Generated";
        const tile = await boxOf(frame, `#canvas .clip:not(.ghost)[title^="${name}"]`);
        const flags = await frame.evaluate((before) => { const n = [...document.querySelectorAll("#canvas .mlabel")].map((e) => e.textContent);
          return n.filter((x) => !before.includes(x)); }, focus.markerNames || []);
        const flag = flags.length ? await boxOf(frame, flags.map((f) => `#canvas .mlabel[title="${f.replace(/"/g, '\\"')}"]`).join(",")) : null;
        focus.broll = flag ? { x0: Math.min(tile.x0, flag.x0), x1: Math.max(tile.x1, flag.x1), y0: Math.min(tile.y0, flag.y0), y1: tile.y1 } : tile;
      }
      if (i === 2) focus.markerNames = await frame.evaluate(() => [...document.querySelectorAll("#canvas .mlabel")].map((e) => e.textContent));
    }
  }
  return textRight;
}

// Every name in this frame, against its clip, the view's region and the
// frame's edges: never shown cut, never invisible one frame after it fits,
// never two at once; every thumbed clip keeps its scrim and tiles its
// thumbnail. Text the frame's edge would cut is faded out first (opacity,
// by its distance from the edge). Returns the problems, which names fit,
// how many clips a viewer sees, where every clip is (for motion) and where
// the long set's pieces are (for the split).
const FRAME_CHECK = (arg) => {
  const { prev, region, zoom, cam, W, SH, EDGE, longSet } = arg, out = [], now = {}, shown = [];
  const alpha = (e) => { const m = getComputedStyle(e).color.match(/rgba?\(([^)]+)\)/); if (!m) return 1;
    const v = m[1].split(',').map(Number); return v.length > 3 ? v[3] : 1; };
  // iframe px -> stage px
  const toStage = (r) => ({ l: cam.x + cam.s * (r.left - region.x) * zoom, r: cam.x + cam.s * (r.right - region.x) * zoom,
                            t: cam.y + cam.s * (r.top - region.y) * zoom, b: cam.y + cam.s * (r.bottom - region.y) * zoom });
  const textBox = (e) => { const r = document.createRange(); r.selectNodeContents(e); return r.getBoundingClientRect(); };
  // fade text out whole as it nears the frame's edge
  document.querySelectorAll('#canvas .clip .label, #canvas .tick, #canvas .mlabel, #canvas .hero-mock').forEach((e) => {
    const b = e.classList.contains('hero-mock') ? e.getBoundingClientRect() : textBox(e);
    if (b.width === 0) { e.style.opacity = ''; return; }
    const s = toStage(b), d = Math.min(s.l, W - s.r, s.t, SH - s.b);
    e.style.opacity = d >= EDGE ? '' : String(Math.max(0, d / EDGE));
  });
  const seen = (e) => alpha(e) * parseFloat(getComputedStyle(e).opacity);
  const offFrame = (b) => { const s = toStage(b); return s.l < -0.5 || s.r > W + 0.5 || s.t < -0.5 || s.b > SH + 0.5; };
  const geometry = [], pieces = [];
  let count = 0;
  document.querySelectorAll('#canvas .clip').forEach((c) => {
    const cr = c.getBoundingClientRect(), vis = getComputedStyle(c).visibility !== 'hidden';
    geometry.push(Math.round(cr.left * 10) / 10, Math.round(cr.width * 10) / 10, vis ? 1 : 0);
    if (vis && cr.width > 2.5) { const s = toStage(cr); if (s.r > 0 && s.l < W && s.b > 0 && s.t < SH) count += 1; }
    if (vis && !c.classList.contains('ghost') && c.__c.name === longSet && c.parentElement.dataset.lane === '0') pieces.push([cr.left, cr.width]);
  });
  // a clip on its way out: its name hidden, or whole
  document.querySelectorAll('#canvas .clip.ghost').forEach((c) => {
    const lab = c.querySelector('.label'); if (!lab || lab.classList.contains('unfit')) return;
    if (getComputedStyle(lab).visibility === 'hidden' || getComputedStyle(c).visibility === 'hidden' || seen(lab) <= 0.01) return;
    const t = textBox(lab);
    if (c.getBoundingClientRect().width < t.width + 14 - 0.5) out.push('a cut name on ' + c.title + ' (leaving)');
    shown.push([lab.textContent, t]);
  });
  document.querySelectorAll('#canvas .clip:not(.ghost)').forEach((c) => {
    const th = c.querySelector('.thumbs');
    if (th) { const cs = getComputedStyle(th);
      if (cs.backgroundSize !== 'auto 100%' || !cs.backgroundRepeat.startsWith('repeat-x')) out.push('a thumbnail not tiled at its aspect on ' + c.title); }
    const lab = c.querySelector('.label'); if (!lab || lab.classList.contains('unfit')) return;
    const key = c.__c.id;
    if (getComputedStyle(c).visibility === 'hidden') { now[key] = false; return; }  // held back by the app (a merge before it takes over)
    const t = textBox(lab), need = t.width + 14, w = c.getBoundingClientRect().width, fits = w >= need - 0.5, a = alpha(lab);
    now[key] = fits;
    if (c.classList.contains('has-thumb')) { const cs = getComputedStyle(lab);
      if (cs.visibility === 'hidden' || !cs.backgroundImage.includes('gradient')) out.push('no scrim on ' + c.title); }
    if (a > 0.01 && !fits) out.push('a cut name on ' + c.title);
    if (fits && prev[key] && a < 0.01) out.push('a dropped name on ' + c.title);
    if (seen(lab) > 0.01) shown.push([lab.textContent, t]);
  });
  document.querySelectorAll('.tick, .mlabel').forEach((e) => {
    const cs = getComputedStyle(e); if (cs.visibility === 'hidden' || e.classList.contains('unfit') || seen(e) <= 0.01) return;
    const t = textBox(e);
    if (t.width > 0) shown.push([e.textContent, t]);
  });
  const pill = document.querySelector('#canvas .hero-mock');
  if (pill && getComputedStyle(pill).visibility !== 'hidden' && seen(pill) > 0.01) shown.push([pill.textContent, pill.getBoundingClientRect()]);
  shown.forEach(([s, t]) => {
    if (t.left < region.x - 0.5 || t.right > region.x + region.w + 0.5 || t.top < region.y - 0.5 || t.bottom > region.y + region.h + 0.5)
      out.push('a name cut by the view\'s edge: ' + JSON.stringify(s));
    if (offFrame(t)) out.push('a name cut by the frame\'s edge: ' + JSON.stringify(s));
  });
  shown.forEach(([s, a], i) => shown.slice(i + 1).forEach(([u, b]) => {
    if (s === 'MOCK' || u === 'MOCK') return;  // the badge sits beside its tile's name (layoutProblems checks it)
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) out.push('two names at once: ' + JSON.stringify([s, u]));
  }));
  return { out, now, count, geometry, pieces };
};

// ---- the story ------------------------------------------------------------

// Which delivery the app shows at t, and how far its motion has run.
function appAt(t) {
  let at = -Infinity, state = last, index = 0;
  story.deliveries.forEach((d, i) => { if (t >= d.at) { at = d.at; state = d.state; index = i + 1; } });
  return { state, since: t - at, index };
}
// The caption at t: the segment it falls in (the last one wraps round the
// loop's seam), typed at CPS.
function captionAt(t) {
  const segs = story.captions;
  let seg = segs.find((s) => t >= s.at && t < s.until), wrapped = false;
  if (!seg) { seg = segs[segs.length - 1]; wrapped = true; }  // the last result, held over the seam
  const startT = seg.type_at, full = seg.kind === "prompt" ? states.prompt : seg.note;
  // between a chip landing and its note starting, nothing is typed yet
  const into = wrapped ? t + story.duration - startT : Math.max(0, t - startT);
  const n = Math.min(full.length, Math.max(seg.kind === "prompt" ? 1 : 0, Math.ceil(into * CPS)));
  return seg.kind === "prompt" ? { id: "prompt", prompt: states.prompt, n } : { id: seg.call, call: seg.call, note: seg.note, n };
}
const ease = (v) => { v = Math.max(0, Math.min(1, v)); return v < 0.5 ? 4 * v * v * v : 1 - Math.pow(-2 * v + 2, 3) / 2; };
// The camera at t, between its keyframes (each "rest" or a push), eased.
function cameraAt(t, shots) {
  const ks = story.camera;
  let i = 0;
  while (i + 1 < ks.length && ks[i + 1].t <= t) i++;
  const a = shots[ks[i].on], b = i + 1 < ks.length ? shots[ks[i + 1].on] : a;
  if (i + 1 >= ks.length) return a;
  const p = ease((t - ks[i].t) / (ks[i + 1].t - ks[i].t));
  return { x: a.x + (b.x - a.x) * p, y: a.y + (b.y - a.y) * p, s: a.s + (b.s - a.s) * p };
}

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: DSF, colorScheme: "dark" });
  page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
  await page.setContent(stage);
  await waitReady(page, "app");
  const sizes = [], focus = {};
  const textRight = await checkAllStates(page, "app", "the stage", sizes, focus);
  const frame = await appFrameOf(page, "app");
  // The app now shows the finished cut, settled: the region and the zoom.
  const region = await viewRegion(frame, false, textRight, 3);
  const zoom = BOX.w / region.w, box = { x: BOX.x, y: BOX.y, w: BOX.w, h: region.h * zoom };
  await page.evaluate(({ region, zoom, box }) => {
    const view = document.getElementById("view"), z = document.getElementById("zoom");
    view.style.width = box.w + "px"; view.style.height = box.h + "px";
    z.style.transform = "scale(" + zoom + ")"; z.style.left = -region.x * zoom + "px"; z.style.top = -region.y * zoom + "px";
  }, { region, zoom, box });
  // Static gates, from the geometry: the view's share of the frame, the type size.
  const coverage = (box.w * box.h) / (W * H);
  if (coverage < 0.6) await refuse(`the view covers ${(coverage * 100).toFixed(1)}% of the frame at rest (60% or more)`);
  if (box.y + box.h > SH) await refuse(`the view runs past the frame's bottom at rest (${(STAGE_TOP + box.y + box.h).toFixed(0)} of ${H})`);
  const atPhone = (px) => px * PHONE / W;
  const inView = sizes.filter((s) => s.r > region.x && s.l < region.x + region.w && s.b > region.y && s.t < region.y + region.h);
  if (!inView.length) await refuse("no text found in the view to measure");
  const smallest = inView.reduce((a, b) => (b.px < a.px ? b : a));
  const typeMin = Math.min(atPhone(smallest.px * zoom), atPhone(CAPTION_PX));
  if (typeMin < 10) await refuse(`text would be ${typeMin.toFixed(2)} px at ${PHONE} wide (10 or more): ${JSON.stringify(smallest.text)} at ${smallest.px} px`);

  // The camera's shots: at rest, and one push per subject, as large as it can
  // be (up to PUSH_MAX) with the subject whole in the frame, centred on it,
  // and the view still covering the frame. Refused under PUSH_MIN.
  const shots = { rest: { x: box.x, y: box.y, s: 1 } }, pushes = {};
  for (const [name, f] of Object.entries(focus)) {
    if (name === "markerNames") continue;
    if (!f) await refuse(`nothing to push onto for "${name}"`);
    const v = { x0: (f.x0 - region.x) * zoom, x1: (f.x1 - region.x) * zoom, y0: (f.y0 - region.y) * zoom, y1: (f.y1 - region.y) * zoom };
    const s = Math.min(PUSH_MAX, (W - 2 * MARGIN) / (v.x1 - v.x0), (SH - 2 * MARGIN) / (v.y1 - v.y0));
    if (s < PUSH_MIN) await refuse(`the push onto "${name}" can only be ${s.toFixed(2)}x with it whole in the frame (at least ${PUSH_MIN})`);
    const clamp = (val, lo, hi) => Math.max(lo, Math.min(hi, val));
    const x = clamp(W / 2 - s * (v.x0 + v.x1) / 2, W - s * box.w, 0), y = clamp(SH / 2 - s * (v.y0 + v.y1) / 2, SH - s * box.h, 0);
    const inside = x + s * v.x0 >= MARGIN - 0.5 && x + s * v.x1 <= W - MARGIN + 0.5 && y + s * v.y0 >= 0 && y + s * v.y1 <= SH + 0.5;
    if (!inside) await refuse(`at full push the "${name}" subject is not whole in the frame`);
    shots[name] = { x, y, s };
    pushes[name] = +s.toFixed(3);
  }
  for (const k of story.camera) if (!shots[k.on]) {  // a gentler push on a subject: "broll@1.12"
    const [on, sc] = k.on.split("@"), full = shots[on], s = parseFloat(sc);
    if (!full || !s) await refuse(`unknown camera shot "${k.on}"`);
    const p = (s - 1) / (full.s - 1);
    shots[k.on] = { x: box.x + (full.x - box.x) * p, y: box.y + (full.y - box.y) * p, s };
  }
  // Captions: the widest chip and the prompt must fit their band.
  for (const c of story.captions.filter((c) => c.kind === "chip")) {
    await page.evaluate((c) => window.__caption({ call: c.call, note: c.note, n: c.note.length }), c);
    const wide = await page.evaluate(() => document.getElementById("chip").getBoundingClientRect().right);
    if (wide > W - BOX.x + 0.5) await refuse(`the chip "${c.call}  ${c.note}" is ${wide.toFixed(0)} px wide, past the frame`);
  }
  await page.evaluate((p) => window.__caption({ prompt: p, n: p.length }), states.prompt);
  const promptBox = await page.evaluate(() => document.getElementById("prompt").getBoundingClientRect());
  if (promptBox.bottom > STAGE_TOP - 2) await refuse(`the prompt runs to y=${promptBox.bottom.toFixed(0)}, into the stage at ${STAGE_TOP}`);
  await refuseStale("the captions", story.captions.map((c) => (c.call || "") + " " + (c.note || "")).join("\n") + "\n" + states.prompt, "host");

  // The frames. The app was left on the finished cut: frame 0 needs no delivery.
  const total = Math.round(story.duration * FPS);
  let delivered = 0, prev = {}, minCover = 1;
  const log = [], displayed = new Map();  // call -> its note as a viewer read it, fully typed
  for (let k = 0; k < total; k++) {
    const t = k / FPS, a = appAt(t);
    while (delivered < a.index) { await deliver(frame, story.deliveries[delivered].state); delivered += 1; }
    await seek(frame, a.index ? a.since * 1000 : 1e6);
    const cam = cameraAt(t, shots);
    await page.evaluate((c) => window.__camera(c), cam);
    const vx0 = Math.max(cam.x, 0), vx1 = Math.min(cam.x + cam.s * box.w, W), vy0 = Math.max(cam.y, 0), vy1 = Math.min(cam.y + cam.s * box.h, SH);
    const cover = Math.max(0, vx1 - vx0) * Math.max(0, vy1 - vy0) / (W * H);
    minCover = Math.min(minCover, cover);
    if (cover < 0.6) await refuse(`frame ${k}: the view covers ${(cover * 100).toFixed(1)}% of the frame`);
    const cap = captionAt(t);
    await page.evaluate((c) => window.__caption(c), cap);
    const read = await page.evaluate(() => window.__readCaption());
    if (read.kind === "chip" && read.rest === "" && read.typed) displayed.set(read.call, read.typed);
    const r = await frame.evaluate(FRAME_CHECK, { prev, region, zoom, cam, W, SH, EDGE, longSet: states.long_set });
    if (r.out.length) await refuse(`frame ${k} (t=${t.toFixed(2)}s): ${r.out.join("; ")}`);
    prev = r.now;
    log.push({ k, t, delivery: a.index, cam, count: r.count, geometry: r.geometry, pieces: r.pieces,
               caption: { id: cap.id, full: read.rest === "" && read.typed.length > 0 } });
    await page.screenshot({ path: path.join(framesDir, String(k).padStart(5, "0") + ".png") });
    if (k % 60 === 0) console.log("frame", k, "/", total);
  }

  // Gates over the whole recording, from what each frame showed.
  // 1. Every caption is held fully typed long enough to read (the last one wraps round the seam).
  //    Each is typed once and held once: a result shown whole, then typed
  //    again, is a flash (round 7's first cut showed every note whole for the
  //    frames between its chip landing and its typing).
  const holds = {}, runsOf = {};
  const n = log.length;
  for (let k = 0; k < n; k++) {
    const c = log[k].caption, p = log[(k - 1 + n) % n].caption;
    if (!c.full || (p.full && p.id === c.id)) continue;  // not the start of a held run
    let len = 0;
    while (len < n && log[(k + len) % n].caption.full && log[(k + len) % n].caption.id === c.id) len++;
    holds[c.id] = Math.max(holds[c.id] || 0, len / FPS);
    runsOf[c.id] = (runsOf[c.id] || 0) + 1;
  }
  for (const [id, runs] of Object.entries(runsOf)) {
    if (runs > 1) await refuse(`the "${id}" caption is shown whole ${runs} times (typed once, then held once)`);
  }
  for (const c of story.captions) {
    const id = c.kind === "prompt" ? "prompt" : c.call, need = c.kind === "prompt" ? PROMPT_HOLD_MIN : HOLD_MIN;
    if (!(holds[id] >= need - 1e-6)) await refuse(`the ${c.kind === "prompt" ? "prompt" : `"${id}" result`} is held fully typed for ${(holds[id] || 0).toFixed(2)} s (at least ${need} s)`);
  }
  // 2. The split moves for at least SPLIT_MIN_FRAMES frames, from the frame its result lands.
  const split = story.deliveries.findIndex((d) => d.state === 1) + 1;
  const ks = log.filter((f) => f.delivery === split).map((f) => f.k);
  const moving = (f, g) => f.pieces.length !== g.pieces.length || f.pieces.some((p, i) => Math.abs(p[0] - g.pieces[i][0]) > 0.2 || Math.abs(p[1] - g.pieces[i][1]) > 0.2);
  let lastMove = ks[0];
  for (const k of ks.slice(1)) if (moving(log[k], log[k - 1])) lastMove = k;
  const splitFrames = lastMove - ks[0] + 1;
  if (splitFrames < SPLIT_MIN_FRAMES) await refuse(`the split closes in ${splitFrames} frames (at least ${SPLIT_MIN_FRAMES})`);
  // 3. No frame changes the clips on screen by more than one without motion on both sides.
  const camMoved = (f, g) => Math.abs(f.cam.x - g.cam.x) > 0.05 || Math.abs(f.cam.y - g.cam.y) > 0.05 || Math.abs(f.cam.s - g.cam.s) > 1e-4;
  const moved = (f, g) => camMoved(f, g) || f.geometry.length !== g.geometry.length || f.geometry.some((v, i) => Math.abs(v - g.geometry[i]) > 0.15);
  let maxStep = 0;
  for (let k = 0; k < n; k++) {
    const f = log[k], g = log[(k - 1 + n) % n], step = Math.abs(f.count - g.count);
    maxStep = Math.max(maxStep, step);
    if (step <= 1) continue;
    const before = moved(g, log[(k - 2 + n) % n]), after = moved(log[(k + 1) % n], f);
    if (!before || !after) await refuse(`frame ${k} (t=${f.t.toFixed(2)}s): the clips on screen go from ${g.count} to ${f.count} with no motion ${!before ? "before" : "after"} it`);
  }

  // Frame 0 is the poster: the finished cut, framed in the stage.
  fs.copyFileSync(path.join(framesDir, "00000.png"), path.join(assetsDir, "hero-still.png"));
  fs.writeFileSync(path.join(assetsDir, "hero-layout.json"), JSON.stringify({
    stage: { size: [W * DSF, H * DSF], view: [box.x, STAGE_TOP + box.y, box.w, box.h].map((v) => Math.round(v * DSF)), zoom: +zoom.toFixed(4),
             fps: FPS, frames: total, min_coverage: +minCover.toFixed(4), min_type_px_at_358: +typeMin.toFixed(2),
             pushes, seam_s: story.seam, split_frames: splitFrames,
             caption_holds_s: Object.fromEntries(Object.entries(holds).map(([k, v]) => [k, +v.toFixed(2)])), max_clip_count_step: maxStep },
  }, null, 1) + "\n");
  // What this recording showed, for the truth check (build.py ties it to the other recording).
  const payload = await frame.evaluate(() => window.__fcpApp.state.payload);
  fs.writeFileSync(path.join(work, "shown-stage.json"), JSON.stringify({
    calls: story.captions.filter((c) => c.kind === "chip").map((c) => [c.call, displayed.get(c.call) || null]),
    results: [...new Set(story.deliveries.filter((d) => d.state !== EMPTY).map((d) => d.state))].sort().map((i) => sha(states.states[i].result)),
    thumbs: Object.fromEntries(payload.clips.filter((c) => c.thumb).map((c) => [c.name + " @ " + c.start, sha(c.thumb)])),
  }, null, 1));
  console.log(`stage: view ${box.w.toFixed(0)}x${box.h.toFixed(0)} of ${W}x${H} (${(coverage * 100).toFixed(1)}%), zoom ${zoom.toFixed(3)},`,
              `type ${typeMin.toFixed(2)} px at ${PHONE}, min cover ${(minCover * 100).toFixed(1)}%, pushes ${JSON.stringify(pushes)},`,
              `split ${splitFrames} frames, holds ${JSON.stringify(holds)}, max count step ${maxStep}, ${total} frames`);

  if (WITH_CARD) await writeCardFrame(browser);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });

async function writeCardFrame(browser) {
  const cp = await browser.newPage({ viewport: { width: Math.ceil(CARD_APP_W * CARD_ZOOM) + 8, height: Math.ceil(420 * CARD_ZOOM) },
                                     deviceScaleFactor: 2, colorScheme: "dark" });
  await cp.setContent(cardHost);
  await waitReady(cp, "capp");
  await (await appFrameOf(cp, "capp")).addStyleTag({ content: CARD_CSS });
  const textRight = await checkAllStates(cp, "capp", "the card");
  // to the spine's own border: no empty sliver under it (critic round 5)
  const box = await viewRegion(await appFrameOf(cp, "capp"), true, textRight, 0);
  await cp.screenshot({ path: path.join(assetsDir, "card-frame.png"),
                        clip: { x: box.x * CARD_ZOOM, y: box.y * CARD_ZOOM, width: box.w * CARD_ZOOM, height: box.h * CARD_ZOOM } });
  await cp.close();
  console.log("card frame written");
}
