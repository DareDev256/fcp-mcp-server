// The README hero's recorder: a composed stage around the REAL timeline view.
// Run by build.py; not meant to be run alone.
//
//   node demo/hero/record.js <work dir> <frames dir> <assets dir> [--card]
//
// What is real: the view is the ui://fcp/timeline shell exactly as the server
// serves it (work/app.html), one app instance driven through the MCP Apps
// draft protocol, fed the four real view_timeline results in order; every
// change on screen is the app's own animation between two of them (it
// animates with Web Animations; this recorder pauses each one and sets its
// time per frame, so every frame is a pure function of the story clock).
// What is composed: the stage (only the view's ruler and lanes, drawn 2.25x
// on true black, no SELECTION or SERVER panels), the captions (the prompt,
// then one chip naming the call, with a short note), the timing, and a
// virtual camera (a CSS transform on the stage that pushes onto what each
// edit changes, at most 1.08x so the view never leaves the frame).
//
// The story runs from the finished cut (frame 0, the poster), rewinds to the
// imported cut (the app's own undo motion), then plays the calls in order and
// ends on the finished cut again; build.py crossfades the last 0.3 s into
// frame 0, the one crossfade. Frames: 800x460 CSS at 1.6x (1280x736), 30 fps.
//
// HARD GATES: it REFUSES (exit 1) on any frame with a doubled, dropped or
// clipped name (or one cut by the stage's edge), a thumbed clip without its
// scrim, a thumbnail not tiled at its own aspect, the view covering under 60%
// of the frame, any text under 10 px when the frame is shown 358 px wide
// (measured from every text the view shows in any state, the MOCK badge
// included), a caption wider than the frame, a name the app had
// to hide in any state, or any text that would go stale in an image
// (scripts/image_text.py). build.py refuses a hold over 1.5 s and a loop
// whose last frame does not match frame 0.
//
// With --card it also writes card-frame.png (the social card's picture: the
// finished ruler and lanes at 1.75x). Without it the card is left alone: the
// card and the logo are frozen and their bytes pinned (tests/test_front_door.py).
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
const WITH_CARD = process.argv.includes("--card");
const states = JSON.parse(fs.readFileSync(path.join(work, "states.json"), "utf8"));
const story = states.story;
const app = fs.readFileSync(path.join(work, "app.html"), "utf8");
const FPS = 30, W = 800, H = 460, DSF = 1.6, PHONE = 358;
const APP_W = 418;                     // the view's layout width: every name fits, cut and uncut
const BOX = { x: 32, y: 84, w: 736 };  // where the view sits at rest (its height follows from the zoom)
const PUSH = 1.08, CAPTION_PX = 23, MOCK_PX = 10.5;
const CARD_APP_W = 480, CARD_ZOOM = 1.75, GUTTER = 48;
const last = states.states.length - 1;

const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const tokens = Object.entries(states.palette).map(([k, v]) => `--${k}:${v};`).join(" ");
const appFrame = (id, w) => `<iframe class="app" id="${id}" style="width:${w}px;height:420px" sandbox="allow-scripts" srcdoc="${esc(app)}"></iframe>`;

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

const stage = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; width:${W}px; height:${H}px; background:var(--ground); color:var(--ink); overflow:hidden;
    font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
  #cap { position:absolute; left:${BOX.x}px; right:${W - BOX.x - BOX.w}px; top:12px; height:62px; display:flex; align-items:center; }
  #prompt { font-size:${CAPTION_PX}px; line-height:1.25; font-weight:500; letter-spacing:-.01em; text-wrap:balance; }
  #prompt b { color:var(--accent); font-weight:600; margin-right:.3em; }
  #chip { display:flex; align-items:baseline; gap:16px; white-space:nowrap; }
  #chip .call { font: 600 ${CAPTION_PX}px/1 ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--accent);
    border:1px solid var(--line-strong); border-radius:10px; padding:9px 14px; background:var(--panel); }
  #chip .note { font-size:${CAPTION_PX}px; color:var(--ink); }
  #cam { position:absolute; left:0; top:0; transform-origin:0 0; }
  #view { position:absolute; overflow:hidden; border-radius:10px; background:var(--panel); }
  #zoom { position:absolute; left:0; top:0; transform-origin:0 0; }
  #zoom iframe { position:absolute; border:0; background:var(--ground); }
</style></head><body>
<div id="cap"><div id="prompt"></div><div id="chip"><span class="call"></span><span class="note"></span></div></div>
<div id="cam"><div id="view"><div id="zoom">${appFrame("app", APP_W)}</div></div></div>
<script>${protocol}
  const typed = (s, p) => s.slice(0, Math.round(Math.max(0, Math.min(1, p)) * s.length));
  // The captions at story time t: the prompt, or one chip (call, then note).
  window.__caption = function (c) {
    const prompt = document.getElementById("prompt"), chip = document.getElementById("chip");
    prompt.style.display = c.prompt != null ? "" : "none"; chip.style.display = c.call != null ? "" : "none";
    if (c.prompt != null) prompt.innerHTML = "<b>You</b>" + c.prompt.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    if (c.call != null) { chip.querySelector(".call").textContent = c.call; chip.querySelector(".note").textContent = c.note || ""; }
  };
  window.__typed = typed;
  // The camera: the stage's box scaled by s about the point that keeps the
  // focus (fx, in box pixels) where it was, clamped so the box stays in frame.
  window.__camera = function (box, s, fx) {
    const cam = document.getElementById("cam");
    let left = box.x + fx - fx * s;
    left = Math.max(4, Math.min(${W} - 4 - box.w * s, left));
    cam.style.transform = "translate(" + left + "px," + box.y + "px) scale(" + s + ")";
    return { x: left, y: box.y, w: box.w * s, h: box.h * s };
  };
</script></body></html>`;

// The social card's picture: the finished ruler and lanes at CARD_ZOOM.
const cardHost = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root { ${tokens} }
  html,body { margin:0; background:var(--ground); }
  #czoom { position:absolute; left:0; top:0; transform-origin:0 0; transform:scale(${CARD_ZOOM}); }
  #czoom iframe { position:absolute; left:0; top:0; border:0; background:var(--ground); }
</style></head><body><div id="czoom">${appFrame("capp", CARD_APP_W)}</div><script>${protocol}</script></body></html>`;

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

const toolResult = (i) => ({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
  params: { result: { content: [{ type: "text", text: states.states[i].result }] } } });

async function appFrameOf(page, id) { return (await page.$("#" + id)).contentFrame(); }
async function waitReady(page, id) {
  await page.waitForFunction((id) => window.__ready[id] === true, id, { timeout: 20000 });
}

// The result for state i, as the host would send it, into the app AS IT IS
// NOW, so the app animates from whatever it was showing. Every animation is
// paused at once (at 0) for the recorder to seek.
async function deliver(frame, i) {
  await frame.evaluate((msg) => window.dispatchEvent(new MessageEvent("message", { data: msg })), toolResult(i));
  if (states.mock_clip && i === last && !(await injectPill(frame))) {
    await refuse(`no clip named "${states.mock_clip}" in the last state to mark as mocked`);
  }
  await frame.evaluate(() => document.getAnimations().forEach((a) => { a.pause(); a.currentTime = 0; }));
  await frame.evaluate(() => Promise.all([...document.querySelectorAll(".thumbs")].map((t) => {
    const m = /url\("(.*)"\)/.exec(t.style.backgroundImage); if (!m) return null;
    const im = new Image(); im.src = m[1]; return im.decode().catch(() => null);
  })));
}
async function seek(frame, ms) {
  await frame.evaluate((ms) => document.getAnimations().forEach((a) => { a.currentTime = ms; }), ms);
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
    if (m) window.__fcpApp.revealWhenWide(pill, m.from, m.to, pill.offsetWidth + 8, true);
    return true;
  }, { name: states.mock_clip, pal: states.palette, px: MOCK_PX });
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
    .filter((e) => !e.classList.contains("unfit")).map((e) => {
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

// Each state settled, in order, on a fresh app: every check that must hold.
// `sizes`, when given, collects every state's text sizes.
async function checkAllStates(page, id, where, sizes) {
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
  }
  return textRight;
}

// Every name in this frame, against its clip and the stage's region: never
// shown cut, never invisible one frame after it fits, never two at once, and
// every thumbed clip keeps its scrim and tiles its thumbnail.
const FRAME_CHECK = (arg) => {
  const { prev, region } = arg, out = [], now = {}, shown = [];
  const alpha = (e) => { const m = getComputedStyle(e).color.match(/rgba?\(([^)]+)\)/); if (!m) return 1;
    const v = m[1].split(',').map(Number); return v.length > 3 ? v[3] : 1; };
  // A clip on its way out (shrinking to nothing): its name must be hidden, or whole.
  document.querySelectorAll('#canvas .clip.ghost').forEach((c) => {
    const lab = c.querySelector('.label'); if (!lab || lab.classList.contains('unfit')) return;
    if (getComputedStyle(lab).visibility === 'hidden' || getComputedStyle(c).visibility === 'hidden' || alpha(lab) <= 0.01) return;
    const r = document.createRange(); r.selectNodeContents(lab); const t = r.getBoundingClientRect();
    if (c.getBoundingClientRect().width < t.width + 14 - 0.5) out.push('a cut name on ' + c.title + ' (leaving)');
    shown.push([lab.textContent, t]);
  });
  document.querySelectorAll('#canvas .clip:not(.ghost)').forEach((c) => {
    const th = c.querySelector('.thumbs');
    if (th) { const cs = getComputedStyle(th);
      if (cs.backgroundSize !== 'auto 100%' || !cs.backgroundRepeat.startsWith('repeat-x')) out.push('a thumbnail not tiled at its aspect on ' + c.title); }
    const lab = c.querySelector('.label'); if (!lab || lab.classList.contains('unfit')) return;
    const r = document.createRange(); r.selectNodeContents(lab); const t = r.getBoundingClientRect();
    const need = t.width + 14, w = c.getBoundingClientRect().width, fits = w >= need - 0.5, a = alpha(lab), key = c.__c.id;
    now[key] = fits;
    if (c.classList.contains('has-thumb')) { const cs = getComputedStyle(lab);
      if (cs.visibility === 'hidden' || !cs.backgroundImage.includes('gradient')) out.push('no scrim on ' + c.title); }
    if (a > 0.01 && !fits) out.push('a cut name on ' + c.title);
    if (fits && prev[key] && a < 0.01) out.push('a dropped name on ' + c.title);
    if (a > 0.01) shown.push([lab.textContent, t]);
  });
  document.querySelectorAll('.tick, .mlabel').forEach((e) => {
    const cs = getComputedStyle(e); if (cs.visibility === 'hidden' || e.classList.contains('unfit')) return;
    const r = document.createRange(); r.selectNodeContents(e); const t = r.getBoundingClientRect();
    if (t.width > 0) shown.push([e.textContent, t]);
  });
  shown.forEach(([s, t]) => {
    if (t.left < region.x - 0.5 || t.right > region.x + region.w + 0.5 || t.top < region.y - 0.5 || t.bottom > region.y + region.h + 0.5)
      out.push('a name cut by the stage edge: ' + JSON.stringify(s));
  });
  shown.forEach(([s, a], i) => shown.slice(i + 1).forEach(([u, b]) => {
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) out.push('two names at once: ' + JSON.stringify([s, u]));
  }));
  return [out, now];
};

// ---- the story ------------------------------------------------------------

// Which result the app shows at t, and how far its motion has run.
function appAt(t) {
  let at = -Infinity, state = last;
  for (const d of story.deliveries) if (t >= d.at) { at = d.at; state = d.state; }
  return { state, since: t - at, index: story.deliveries.filter((d) => t >= d.at).length };
}
// The caption at t: the prompt while it types, else the latest chip.
function captionAt(t) {
  if (t >= story.prompt[0] && t < story.calls[0].at) {
    const p = (t - story.prompt[0]) / (story.prompt[1] - story.prompt[0]);
    return { prompt: typedText(states.prompt, p) };
  }
  let call = story.calls[story.calls.length - 1], ended = true;
  for (const c of story.calls) if (t >= c.at) { call = c; ended = false; }
  if (t < story.calls[0].at && t < story.prompt[0]) { call = story.calls[story.calls.length - 1]; ended = true; }
  if (ended) return { call: call.call, note: call.note };
  const name = typedText(call.call, (t - call.at) / 0.25);
  const note = t >= call.note_at ? typedText(call.note, (t - call.note_at) / 0.35) : "";
  return { call: name, note };
}
// At least one character from the frame typing starts on: an empty chip or a bare
// "You" for a frame reads as a glitch.
function typedText(s, p) { return s.slice(0, Math.max(1, Math.ceil(Math.max(0, Math.min(1, p)) * s.length))); }
const ease = (v) => { v = Math.max(0, Math.min(1, v)); return v < 0.5 ? 4 * v * v * v : 1 - Math.pow(-2 * v + 2, 3) / 2; };
// The camera at t: a push onto each edit's focus, eased in and back out.
function cameraAt(t) {
  for (const p of story.pushes) {
    const rise = ease((t - p.in) / 0.4), fall = ease((t - p.out) / 0.45);
    if (t >= p.in && t < p.out + 0.45) return { s: 1 + (PUSH - 1) * (rise - fall), focus: p.focus };
  }
  return { s: 1, focus: null };
}

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: DSF, colorScheme: "dark" });
  page.on("pageerror", (e) => { console.error("page error:", e.message); process.exitCode = 1; });
  await page.setContent(stage);
  await waitReady(page, "app");
  const sizes = [];
  const textRight = await checkAllStates(page, "app", "the stage", sizes);
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
  // The smallest text the stage shows: every text in the view's region in any
  // state (the MOCK badge included), measured, and the captions.
  const atPhone = (px) => px * PHONE / W;
  const inView = sizes.filter((s) => s.r > region.x && s.l < region.x + region.w && s.b > region.y && s.t < region.y + region.h);
  if (!inView.length) await refuse("no text found in the view to measure");
  const smallest = inView.reduce((a, b) => (b.px < a.px ? b : a));
  const typeMin = Math.min(atPhone(smallest.px * zoom), atPhone(CAPTION_PX));
  if (typeMin < 10) await refuse(`text would be ${typeMin.toFixed(2)} px at ${PHONE} wide (10 or more): ${JSON.stringify(smallest.text)} at ${smallest.px} px`);
  // Focus points for the camera, in box pixels, from the settled layouts.
  const focusOf = async (selector) => frame.evaluate(({ selector, region, zoom }) => {
    const els = [...document.querySelectorAll(selector)]; if (!els.length) return null;
    const rs = els.map((e) => e.getBoundingClientRect());
    const l = Math.min(...rs.map((r) => r.left)), r = Math.max(...rs.map((r) => r.right));
    return ((l + r) / 2 - region.x) * zoom;
  }, { selector, region, zoom });
  const focus = {
    squat: await focusOf(`#canvas .clip:not(.ghost)[title^="${states.long_set} ·"]`),
    markers: await focusOf("#canvas .mlabel"),
    broll: states.mock_clip ? await focusOf(`#canvas .clip:not(.ghost)[title^="${states.mock_clip} ·"]`)
                            : await focusOf("#canvas .clip:not(.ghost)[title^=\"Generated\"]"),
  };
  for (const p of story.pushes) p.focus = focus[p.on] == null ? box.w / 2 : focus[p.on];
  // Caption widths: the widest prompt and chip must fit the frame.
  for (const c of story.calls) {
    await page.evaluate((c) => window.__caption({ call: c.call, note: c.note }), c);
    const wide = await page.evaluate(() => document.getElementById("chip").getBoundingClientRect().right);
    if (wide > W - BOX.x + 0.5) await refuse(`the chip "${c.call}  ${c.note}" is ${wide.toFixed(0)} px wide, past the frame`);
  }
  await page.evaluate((p) => window.__caption({ prompt: p }), states.prompt);
  const promptBox = await page.evaluate(() => document.getElementById("prompt").getBoundingClientRect());
  if (promptBox.bottom > BOX.y - 4) await refuse(`the prompt runs to y=${promptBox.bottom.toFixed(0)}, into the view at ${BOX.y}`);
  await refuseStale("the captions", states.story.calls.map((c) => c.call + " " + c.note).join("\n") + "\n" + states.prompt, "host");

  // The frames. The app was left on the finished cut: frame 0 needs no delivery.
  const total = Math.round(story.duration * FPS);
  let delivered = 0, prev = {}, minCover = 1;
  for (let k = 0; k < total; k++) {
    const t = k / FPS, a = appAt(t);
    while (delivered < a.index) { await deliver(frame, story.deliveries[delivered].state); delivered += 1; }
    await seek(frame, a.index ? a.since * 1000 : 1e6);
    const cam = cameraAt(t);
    const shown = await page.evaluate(({ box, cam }) => window.__camera(box, cam.s, cam.focus == null ? box.w / 2 : cam.focus), { box, cam });
    const cover = (Math.min(shown.x + shown.w, W) - Math.max(shown.x, 0)) * (Math.min(shown.y + shown.h, H) - Math.max(shown.y, 0)) / (W * H);
    minCover = Math.min(minCover, cover);
    if (cover < 0.6) await refuse(`frame ${k}: the view covers ${(cover * 100).toFixed(1)}% of the frame`);
    if (shown.x < -0.5 || shown.x + shown.w > W + 0.5 || shown.y + shown.h > H + 0.5) await refuse(`frame ${k}: the camera put the view past the frame`);
    await page.evaluate((c) => window.__caption(c), captionAt(t));
    const [problems, now] = await frame.evaluate(FRAME_CHECK, { prev, region });
    if (problems.length) await refuse(`frame ${k} (t=${t.toFixed(2)}s): ${problems.join("; ")}`);
    prev = now;
    await page.screenshot({ path: path.join(framesDir, String(k).padStart(5, "0") + ".png") });
    if (k % 60 === 0) console.log("frame", k, "/", total);
  }
  // Frame 0 is the poster: the finished cut, framed in the stage.
  fs.copyFileSync(path.join(framesDir, "00000.png"), path.join(assetsDir, "hero-still.png"));
  fs.writeFileSync(path.join(assetsDir, "hero-layout.json"), JSON.stringify({
    stage: { size: [W * DSF, H * DSF], view: [box.x, box.y, box.w, box.h].map((v) => Math.round(v * DSF)), zoom: +zoom.toFixed(4),
             fps: FPS, frames: total, min_coverage: +minCover.toFixed(4), min_type_px_at_358: +typeMin.toFixed(2),
             push: PUSH, seam_s: story.seam },
  }, null, 1) + "\n");
  console.log(`stage: view ${box.w.toFixed(0)}x${box.h.toFixed(0)} of ${W}x${H} (${(coverage * 100).toFixed(1)}%), zoom ${zoom.toFixed(3)},`,
              `type ${typeMin.toFixed(2)} px at ${PHONE}, min cover ${(minCover * 100).toFixed(1)}%, ${total} frames`);

  if (WITH_CARD) await writeCardFrame(browser);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });

async function writeCardFrame(browser) {
  const cp = await browser.newPage({ viewport: { width: Math.ceil(CARD_APP_W * CARD_ZOOM) + 8, height: Math.ceil(420 * CARD_ZOOM) },
                                     deviceScaleFactor: 2, colorScheme: "dark" });
  await cp.setContent(cardHost);
  await waitReady(cp, "capp");
  const textRight = await checkAllStates(cp, "capp", "the card");
  // to the spine's own border: no empty sliver under it (critic round 5)
  const box = await viewRegion(await appFrameOf(cp, "capp"), true, textRight, 0);
  await cp.screenshot({ path: path.join(assetsDir, "card-frame.png"),
                        clip: { x: box.x * CARD_ZOOM, y: box.y * CARD_ZOOM, width: box.w * CARD_ZOOM, height: box.h * CARD_ZOOM } });
  await cp.close();
  console.log("card frame written");
}
