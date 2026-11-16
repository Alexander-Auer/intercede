// The presenter shell: one store fed by the server's event stream, and a row of scenes.
// Every view is drawn from the store, so a replayed recording draws exactly as live.

import board from "./scenes/board.js";
import trace from "./scenes/trace.js";
import lifecycle from "./scenes/lifecycle.js";
import failure from "./scenes/failure.js";

const placeholder = (title, caption) => ({
  title,
  caption,
  mount(el) {
    el.innerHTML = `<div class="placeholder">${title}: not built yet</div>`;
  },
});

export const SCENES = [
  placeholder("Architecture", "One contract between the WMS and every resource."),
  placeholder("Composition", "Every filled cell is a working backend without a class of its own."),
  placeholder("Translation", "One specification, many dialects."),
  board,
  trace,
  lifecycle,
  failure,
  placeholder("Verified", "Tested against the real thing, in CI."),
];

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
  connected: false,
};

const listeners = new Set();
export function onEvent(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function apply(event) {
  switch (event.type) {
    case "hello":
      store.mode = event.mode;
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
      break;
    case "reset":
      store.ops.clear();
      store.outcomes = store.restart = store.forged = store.scenario = null;
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
    const event = JSON.parse(message.data);
    apply(event);
    for (const fn of listeners) fn(event);
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
  const cls = label === "DRY RUN" ? "DRY" : label;
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
  history.replaceState(null, "", `#${index + 1}`);
  drawChrome();
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
    mounted?.update?.(context());
    drawChrome();
  });
}

function sourcesOf(scene) {
  if (store.replaying) return ["REPLAY"];
  return scene?.sources?.(store) ?? [];
}

function drawChrome() {
  document.getElementById("scene-badges").innerHTML = sourcesOf(mounted).map((s) => badge(s)).join("");
  document.getElementById("scene-nav").innerHTML = SCENES.map(
    (scene, i) => `<button class="${i === current ? "active" : ""}" data-scene="${i}" title="${escape(scene.title)}">${i + 1}</button>`,
  ).join("");
  document.getElementById("stacks").innerHTML = store.columns
    .filter((c) => c.stack)
    .map((c) => `<span class="stack-dot ${c.state}" title="${escape(c.reason || c.resource || "")}">${escape(c.title)}</span>`)
    .join("");
  for (const button of document.querySelectorAll(".mode button")) {
    button.classList.toggle("active", button.dataset.mode === store.mode);
  }
}

document.getElementById("scene-nav").addEventListener("click", (e) => {
  const target = e.target.closest("[data-scene]");
  if (target) show(Number(target.dataset.scene));
});

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select")) return;
  if (e.key === "ArrowRight" || e.key === "PageDown") show(current + 1);
  else if (e.key === "ArrowLeft" || e.key === "PageUp") show(current - 1);
  else if (/^[1-9]$/.test(e.key)) show(Number(e.key) - 1);
});

document.querySelector(".mode").addEventListener("click", async (e) => {
  const mode = e.target.closest("[data-mode]")?.dataset.mode;
  if (mode) await api("/api/mode", { mode }).catch((err) => toast(err.message));
});

document.getElementById("reset").addEventListener("click", async () => {
  await api("/api/reset", {}).catch((err) => toast(err.message));
});

const theme = localStorage.getItem("theme");
if (theme) document.documentElement.dataset.theme = theme;
document.getElementById("theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch { /* private window */ }
});

store.meta = await api("/api/meta");
connect();
show(Number(location.hash.slice(1) || 4) - 1);
