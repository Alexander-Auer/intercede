// The presenter shell: one store fed by the server's event stream, and a row of scenes.
// Every view is drawn from the store, so a replayed recording draws exactly as live.

import architecture from "./scenes/architecture.js";
import board from "./scenes/board.js";
import composition from "./scenes/composition.js";
import translation from "./scenes/translation.js";
import verified from "./scenes/verified.js";
import trace from "./scenes/trace.js";
import lifecycle from "./scenes/lifecycle.js";
import failure from "./scenes/failure.js";
import normalisation from "./scenes/normalisation.js";

// The talk, in order. Each scene's id is its address (#job-board), so links survive new scenes.
export const SCENES = [architecture, composition, translation, board, trace, normalisation, lifecycle, failure, verified];
for (const scene of SCENES) scene.id ??= scene.title.toLowerCase().replace(/[^a-z0-9]+/g, "-");

// -- the store --------------------------------------------------------------

export const store = {
  mode: "live",
  columns: [],
  jobs: [],
  database: {},
  ops: new Map(),
  outcomes: null,
  restart: null,
  forged: null,
  scenario: null,
  meta: null,
  replaying: false,
  replay: null,
  recording: false,
  recordings: [],
  ci: null,
  ciStatus: null,
  connected: false,
};

const listeners = new Set();
export function onEvent(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function apply(event) {
  if (event.replayed) store.replaying = true;
  switch (event.type) {
    case "hello":
      if (!event.replayed) {
        store.replaying = false;
        store.mode = event.mode;
        store.recording = event.recording;
      }
      store.columns = event.columns;
      store.jobs = event.jobs;
      store.database = event.database;
      store.ops = new Map(event.operations.map((op) => [op.id, op]));
      store.restart = event.last.restart ?? null;
      store.forged = event.last.forged ?? null;
      store.scenario = event.last.scenario ?? null;
      break;
    case "columns":
      store.columns = event.columns;
      break;
    case "board":
      store.jobs = event.jobs;
      store.database = event.database;
      break;
    case "op.start":
      store.ops.set(event.op.id, event.op);
      trimOps();
      break;
    case "op.step": {
      const op = store.ops.get(event.id);
      if (op) op.steps.push(event.step);
      break;
    }
    case "op.end": {
      const op = store.ops.get(event.id);
      if (op) Object.assign(op, { ended: event.ended, outcome: event.outcome });
      break;
    }
    case "outcomes":
      store.outcomes = event;
      break;
    case "restart":
      store.restart = event;
      break;
    case "forged":
      store.forged = event;
      break;
    case "scenario":
      store.scenario = event;
      break;
    case "mode":
      store.mode = event.mode;
      if (event.mode === "replay") loadRecordings();
      break;
    case "replay":
      store.replay = event;
      store.replaying = Boolean(event.name);
      break;
    case "recording":
      if (event.replayed) break; // the recording's own start, played back
      store.recording = event.on;
      if (event.saved) toast(`Saved demo/recordings/${event.saved}`, "info");
      break;
    case "reset":
      store.ops.clear();
      store.outcomes = store.restart = store.forged = store.scenario = null;
      break;
    case "ci.status":
      store.ciStatus = event;
      if (event.snapshot) store.ci = event.snapshot;
      break;
    case "alert":
      toast(`${event.column}: ${event.message}`, event.level);
      break;
  }
}

function trimOps() {
  while (store.ops.size > 400) store.ops.delete(store.ops.keys().next().value);
}

// -- talking to the server ---------------------------------------------------

export async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

function connect() {
  const source = new EventSource("/api/events");
  source.onopen = () => { store.connected = true; };
  source.onerror = () => { store.connected = false; };
  source.onmessage = (message) => {
    // One bad event must never freeze the presentation: show it, and go on.
    try {
      const event = JSON.parse(message.data);
      apply(event);
      for (const fn of listeners) fn(event);
    } catch (err) {
      toast(`event not shown: ${err.message}`, "warning");
    }
    schedule();
  };
}

// -- presenter chrome ---------------------------------------------------------

export function toast(text, level = "error") {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), 7000);
}

export function escape(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function badge(label, small = false) {
  // Played back, nothing may look live: every data badge then says where it really comes from.
  if (store.replaying && (label === "LIVE" || label === "FAKE")) label = "REPLAY";
  const cls = { "DRY RUN": "DRY", "FROM CODE": "CODE" }[label] ?? (label.startsWith("SNAPSHOT") ? "CODE" : label);
  return `<span class="badge ${cls}${small ? " small" : ""}">${escape(label)}</span>`;
}

let current = -1;
let mounted = null;
const stage = document.getElementById("stage");

function show(index) {
  index = Math.max(0, Math.min(SCENES.length - 1, index));
  if (index === current) return;
  mounted?.unmount?.();
  current = index;
  mounted = SCENES[index];
  stage.innerHTML = "";
  document.getElementById("scene-title").textContent = `${index + 1}. ${mounted.title}`;
  document.getElementById("scene-caption").textContent = mounted.caption;
  mounted.mount(stage, context());
  history.replaceState(null, "", `${location.search}#${mounted.id}`);
  overview(false);
  drawTabs();
  drawChrome();
}

function sceneOf(hash) {
  const key = decodeURIComponent(hash.replace(/^#/, ""));
  if (/^[0-9]+$/.test(key)) return Number(key) - 1;
  const found = SCENES.findIndex((scene) => scene.id === key);
  return found >= 0 ? found : SCENES.indexOf(board);
}

// -- finding your way: tabs, previous and next, and an overview -----------------

function drawTabs() {
  document.getElementById("scene-tabs").innerHTML = SCENES.map((scene, i) =>
    `<button class="tab ${i === current ? "active" : ""} ${i < current ? "seen" : ""}" data-scene="${i}" title="${escape(scene.caption)}">
      <span class="tab-no">${i + 1}</span>${escape(scene.title)}</button>`).join("");
  const previous = SCENES[current - 1];
  const next = SCENES[current + 1];
  document.getElementById("previous").innerHTML = previous ? `‹ <span>${escape(previous.title)}</span>` : "";
  document.getElementById("previous").disabled = !previous;
  document.getElementById("next").innerHTML = next ? `<span>${escape(next.title)}</span> ›` : "";
  document.getElementById("next").disabled = !next;
  document.getElementById("overview").innerHTML = `
    <div class="overview-head"><strong>The talk</strong><span class="muted">← → to step · 1–9 to jump · Esc to close</span></div>
    <div class="overview-grid">${SCENES.map((scene, i) => `
      <button class="overview-card ${i === current ? "active" : ""}" data-scene="${i}">
        <span class="tab-no">${i + 1}</span><strong>${escape(scene.title)}</strong>
        <span class="muted">${escape(scene.caption)}</span></button>`).join("")}</div>`;
}

function overview(open = document.getElementById("overview").hidden) {
  document.getElementById("overview").hidden = !open;
}

function context() {
  return { store, api, toast, escape, badge };
}

let pending = false;
function schedule() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    try {
      mounted?.update?.(context());
    } catch (err) {
      toast(`${mounted.title}: ${err.message}`, "warning");
    }
    drawChrome();
  });
}

function sourcesOf(scene) {
  if (store.replaying) return ["REPLAY"];
  return scene?.sources?.(store) ?? [];
}

function drawChrome() {
  document.getElementById("scene-badges").innerHTML = sourcesOf(mounted).map((s) => badge(s)).join("");
  document.getElementById("stacks").innerHTML = store.columns
    .filter((c) => c.stack)
    .map((c) => `<span class="stack-dot ${c.state}" title="${escape(c.reason || c.resource || "")}">${escape(c.title)}</span>`)
    .join("");
  for (const button of document.querySelectorAll(".mode button")) {
    button.classList.toggle("active", button.dataset.mode === store.mode);
  }
  const record = document.getElementById("record");
  record.classList.toggle("on", store.recording);
  record.textContent = store.recording ? "● Recording… stop" : "● Record";
  record.disabled = store.replaying;
  drawReplayBar();
}

document.addEventListener("click", (e) => {
  const target = e.target.closest("[data-scene]");
  if (target) show(Number(target.dataset.scene));
});
document.getElementById("previous").addEventListener("click", () => show(current - 1));
document.getElementById("next").addEventListener("click", () => show(current + 1));
document.getElementById("open-overview").addEventListener("click", () => overview());
window.addEventListener("hashchange", () => show(sceneOf(location.hash)));

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select")) return;
  if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") show(current + 1);
  else if (e.key === "ArrowLeft" || e.key === "PageUp") show(current - 1);
  else if (/^[1-9]$/.test(e.key)) show(Number(e.key) - 1);
  else if (e.key === "Escape" || e.key === "o") overview();
  else return;
  e.preventDefault();
});

document.querySelector(".mode").addEventListener("click", async (e) => {
  const mode = e.target.closest("[data-mode]")?.dataset.mode;
  if (mode) await api("/api/mode", { mode }).catch((err) => toast(err.message));
});

async function loadRecordings() {
  store.recordings = await api("/api/recordings").catch(() => []);
  schedule();
}

function drawReplayBar() {
  const bar = document.getElementById("replay-bar");
  bar.hidden = store.mode !== "replay";
  if (bar.hidden) return;
  const r = store.replay;
  const playing = r?.name && !r.done;
  const options = store.recordings.map((x) => `<option value="${escape(x.name)}" ${x.name === r?.name ? "selected" : ""}>${escape(x.name)} · ${Math.round(x.duration_s)} s</option>`).join("");
  const html = `${badge("REPLAY")}
    <select id="replay-pick">${options || "<option value=''>no recordings in demo/recordings/</option>"}</select>
    <button class="ghost" data-replay="play">▶ Play</button>
    <button class="ghost" data-replay="${r?.paused ? "resume" : "pause"}" ${playing ? "" : "disabled"}>${r?.paused ? "Resume" : "Pause"}</button>
    <span class="speeds">${[0.5, 1, 2, 4, 8].map((x) => `<button class="ghost ${r?.speed === x ? "on" : ""}" data-speed="${x}">${x}×</button>`).join("")}</span>
    <span class="mono">${r?.name ? `${Math.round(r.position)} / ${Math.round(r.duration)} s${r.done ? " · finished" : ""}` : "choose a recording"}</span>`;
  if (bar.dataset.html !== html) { bar.innerHTML = html; bar.dataset.html = html; }
}

document.getElementById("replay-bar").addEventListener("click", async (e) => {
  const action = e.target.closest("[data-replay]")?.dataset.replay;
  const speed = e.target.closest("[data-speed]")?.dataset.speed;
  const name = document.getElementById("replay-pick")?.value;
  try {
    if (action === "play" && name) await api("/api/replay", { action: "play", name });
    else if (action) await api("/api/replay", { action });
    else if (speed) await api("/api/replay", { action: "speed", speed: Number(speed) });
  } catch (err) { toast(err.message); }
});

document.getElementById("record").addEventListener("click", async () => {
  await api("/api/record", { on: !store.recording }).catch((err) => toast(err.message));
});

document.getElementById("reset").addEventListener("click", async () => {
  await api("/api/reset", {}).catch((err) => toast(err.message));
});

let theme = new URLSearchParams(location.search).get("theme");
try { theme ??= localStorage.getItem("theme"); } catch { /* private window */ }
if (theme) document.documentElement.dataset.theme = theme;
document.getElementById("theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch { /* private window */ }
});

store.meta = await api("/api/meta");
connect();
show(sceneOf(location.hash || "#job-board"));
