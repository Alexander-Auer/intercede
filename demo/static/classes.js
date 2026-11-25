// The class diagram page. The model comes from /api/classes (demo/classes.py, read from
// the package); this page only turns it into Mermaid text, draws it, and lets you look around.

/* global mermaid */

const $ = (id) => document.getElementById(id);
const escape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
// A docstring: escaped, with its ``literals`` as code.
const prose = (s) => escape(s).replace(/``(.+?)``/g, "<code>$1</code>");

const settings = {
  members: false, // the whole package reads best as names and arrows; a class's panel lists its members
  private: false,
  uses: false,
  exceptions: true,
  hidden: [], // areas switched off
};
let data = null;
let byId = new Map();
let focus = null; // a class id: only it and its neighbours are drawn
let chosen = null; // the class the detail panel shows
let text = ""; // the Mermaid text of what is drawn
const view = { x: 0, y: 0, k: 1 };

// -- theme, as the talk's shell keeps it --------------------------------------

let theme = new URLSearchParams(location.search).get("theme");
try { theme ??= localStorage.getItem("theme"); } catch { /* private window */ }
if (theme) document.documentElement.dataset.theme = theme;

try {
  Object.assign(settings, JSON.parse(localStorage.getItem("classes-view") ?? "{}"));
} catch { /* private window, or nothing saved */ }

function save() {
  try { localStorage.setItem("classes-view", JSON.stringify(settings)); } catch { /* private window */ }
}

// -- what is drawn --------------------------------------------------------------

function shownClasses() {
  if (focus) {
    const near = new Set([focus]);
    for (const r of data.relations) {
      if (r.kind === "uses" && !settings.uses) continue;
      if (r.from === focus) near.add(r.to);
      if (r.to === focus) near.add(r.from);
    }
    return data.classes.filter((c) => near.has(c.id));
  }
  return data.classes.filter(
    (c) => !settings.hidden.includes(c.area) && (settings.exceptions || c.kind !== "exception"),
  );
}

function shownRelations(ids) {
  return data.relations.filter(
    (r) => ids.has(r.from) && ids.has(r.to) && (r.kind !== "uses" || settings.uses),
  );
}

// -- Mermaid text ------------------------------------------------------------------

// Member text as Python writes it. Mermaid would read ~ as a generic, __ and * as
// markdown, and { } " < > as syntax, so those go in as its entity codes.
const ENTITIES = { "~": "#126;", _: "#95;", "*": "#42;", "{": "#123;", "}": "#125;", '"': "#34;", "<": "#60;", ">": "#62;" };
const type = (s) => String(s).replace(/[~_*{}"<>]/g, (c) => ENTITIES[c]);
const short = (area) => area === data.package ? area : area.slice(data.package.length + 1);
const namespace = (area) => short(area).replace(/[^\w.]+/g, "_");

function member(c) {
  const lines = [];
  if (!settings.members) return lines;
  for (const f of c.fields) {
    if (f.private && !settings.private) continue;
    lines.push(`${f.private ? "-" : "+"}${type(f.name)}${f.type ? `: ${type(f.type)}` : ""}`);
  }
  for (const m of c.methods) {
    if (m.private && !settings.private) continue;
    const returns = m.returns && m.returns !== "None" ? ` ${type(m.returns)}` : "";
    lines.push(
      `${m.private ? "-" : "+"}${m.async ? "async " : ""}${type(m.name)}(${type(m.parameters.join(", "))})${returns}${m.static ? "$" : ""}`,
    );
  }
  return lines;
}

function mermaidText(classes, relations) {
  const out = ["classDiagram", "direction TB"];
  const areas = new Map();
  for (const c of classes) {
    if (!areas.has(c.area)) areas.set(c.area, []);
    areas.get(c.area).push(c);
  }
  for (const [area, members] of areas) {
    out.push(`namespace ${namespace(area)} {`);
    for (const c of members) {
      const body = member(c);
      out.push(`  class ${c.id}["${c.name}"] {`);
      if (c.kind !== "class") out.push(`    <<${c.kind}>>`);
      for (const line of body) out.push(`    ${line}`);
      out.push("  }");
    }
    out.push("}");
  }
  const arrow = { inherits: "<|--", implements: "<|..", holds: "-->", uses: "..>" };
  for (const r of relations) {
    // inherits and implements point from the general to the special in Mermaid's notation
    const [a, b] = r.kind === "inherits" || r.kind === "implements" ? [r.to, r.from] : [r.from, r.to];
    const label = r.label ? ` : ${type(r.label)}` : "";
    out.push(`${a} ${arrow[r.kind]} ${b}${label}`);
  }
  return out.join("\n");
}

// -- drawing ------------------------------------------------------------------------

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

let look = ""; // Mermaid's settings, colours included: part of a kept drawing's key

function setUpMermaid() {
  const config = {
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    maxTextSize: 1_000_000,
    maxEdges: 5000,
    fontFamily: css("--sans"),
    themeVariables: {
      fontFamily: css("--sans"),
      fontSize: "14px",
      background: css("--bg"),
      primaryColor: css("--panel"),
      primaryTextColor: css("--ink"),
      primaryBorderColor: css("--ink-2"),
      secondaryColor: css("--panel-2"),
      tertiaryColor: css("--panel-2"),
      lineColor: css("--ink-2"),
      textColor: css("--ink"),
      clusterBkg: css("--panel-2"),
      clusterBorder: css("--line"),
      titleColor: css("--ink-2"),
      edgeLabelBackground: css("--bg"),
    },
    class: { hideEmptyMembersBox: true },
  };
  look = JSON.stringify(config);
  mermaid.initialize(config);
}

// -- drawings, kept ---------------------------------------------------------------
// Mermaid lays a diagram out in one block of one to three seconds, in which the page cannot
// respond. The same text always gives the same drawing, so each one is kept: in memory for this
// visit, and the last few in localStorage, so a reload or a view seen before shows at once. The
// key is the text and Mermaid's settings, so a change to the code (a different text) or to the
// theme never gets an old drawing.

const KEPT = 4; // drawings kept across reloads; one is about 300 KB
const drawings = new Map(); // key -> svg

// A 53-bit hash of the text (cyrb53), with its length: a key short enough to store.
function keyOf(s) {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < s.length; i++) {
    const ch = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return `${(4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36)}-${s.length}`;
}

function stored() {
  try { return JSON.parse(localStorage.getItem("classes-drawings") ?? "[]"); } catch { return []; }
}

function keep(key, svg) {
  drawings.set(key, svg);
  const kept = [[key, svg], ...stored().filter(([k]) => k !== key)];
  for (let n = Math.min(kept.length, KEPT); n > 0; n--) {
    try {
      localStorage.setItem("classes-drawings", JSON.stringify(kept.slice(0, n)));
      return;
    } catch { /* full, or a private window: keep fewer */ }
  }
}

async function drawingOf(text) {
  const key = keyOf(`${look}\n${text}`);
  const known = drawings.get(key) ?? stored().find(([k]) => k === key)?.[1];
  if (known) {
    drawings.set(key, known);
    return known;
  }
  // Let the browser show "Drawing…" before Mermaid takes the page.
  await new Promise((done) => requestAnimationFrame(() => setTimeout(done)));
  // The id is the key: a drawing kept from an earlier visit cannot share it with a new one.
  const { svg } = await mermaid.render(`diagram-${key}`, text);
  keep(key, svg);
  return svg;
}

async function render({ fit = false } = {}) {
  const classes = shownClasses();
  const ids = new Set(classes.map((c) => c.id));
  const relations = shownRelations(ids);
  text = mermaidText(classes, relations);
  const drawing = $("drawing");
  if (!classes.length) {
    drawing.innerHTML = "";
    $("status").textContent = "Nothing to draw: switch an area on.";
    return;
  }
  $("status").textContent = "Drawing…";
  try {
    drawing.innerHTML = await drawingOf(text);
  } catch (error) {
    $("status").textContent = `Mermaid could not draw this: ${error.message ?? error}`;
    console.error(error, text);
    return;
  }
  for (const node of nodes()) {
    const c = byId.get(node.dataset.cid);
    node.classList.add(`k-${c.kind.replace(/\s+/g, "-")}`);
    node.setAttribute("aria-label", `${c.kind} ${c.name}`);
  }
  $("status").textContent =
    `${classes.length} classes, ${relations.length} arrows · drag to move, wheel to zoom, click a class`;
  mark();
  if (fit) fitView(); else apply();
}

// Each class box, tagged with the class's id: Mermaid names its nodes classId-<id>-<n>.
function nodes() {
  const found = [];
  for (const node of $("drawing").querySelectorAll("g.node")) {
    const match = /classId-(c\d+)-/.exec(node.id);
    if (match && byId.has(match[1])) {
      node.dataset.cid = match[1];
      found.push(node);
    }
  }
  return found;
}

function nodeOf(id) {
  return $("drawing").querySelector(`g.node[data-cid="${id}"]`);
}

function mark() {
  const query = $("search").value.trim().toLowerCase();
  const drawing = $("drawing");
  drawing.classList.toggle("searching", Boolean(query));
  for (const node of drawing.querySelectorAll("g.node[data-cid]")) {
    const c = byId.get(node.dataset.cid);
    node.classList.toggle("found", Boolean(query) && c.name.toLowerCase().includes(query));
    node.classList.toggle("chosen", c.id === chosen);
  }
}

// -- pan and zoom ---------------------------------------------------------------------

function apply() {
  $("drawing").style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
}

// Dragging and the wheel move the drawing at most once a frame, as a layer of its own while it
// moves; when it stops, the layer goes, so the text is drawn sharp again at the new scale.
let moving = null;
let settle = null;
function applySoon() {
  if (moving) return;
  $("drawing").style.willChange = "transform";
  moving = requestAnimationFrame(() => {
    moving = null;
    apply();
  });
  clearTimeout(settle);
  settle = setTimeout(() => { $("drawing").style.willChange = ""; }, 200);
}

function size() {
  const svg = $("drawing").querySelector("svg");
  if (!svg) return null;
  const box = svg.viewBox.baseVal;
  svg.setAttribute("width", box.width);
  svg.setAttribute("height", box.height);
  return { w: box.width, h: box.height };
}

function fitView() {
  const s = size();
  if (!s) return;
  const canvas = $("canvas").getBoundingClientRect();
  view.k = Math.min(canvas.width / s.w, canvas.height / s.h, 1.5) * 0.95;
  view.x = (canvas.width - s.w * view.k) / 2;
  view.y = (canvas.height - s.h * view.k) / 2;
  apply();
}

function zoomAt(factor, cx, cy, { soon = false } = {}) {
  const k = Math.min(Math.max(view.k * factor, 0.05), 4);
  view.x = cx - ((cx - view.x) * k) / view.k;
  view.y = cy - ((cy - view.y) * k) / view.k;
  view.k = k;
  if (soon) applySoon(); else apply();
}

function centreOn(id) {
  const node = nodeOf(id);
  if (!node) return;
  const svg = $("drawing").querySelector("svg");
  const canvas = $("canvas").getBoundingClientRect();
  const box = node.getBoundingClientRect();
  const origin = svg.getBoundingClientRect();
  // the node's centre in the drawing's own units
  const x = (box.left + box.width / 2 - origin.left) / view.k;
  const y = (box.top + box.height / 2 - origin.top) / view.k;
  view.k = Math.max(view.k, 0.8);
  view.x = canvas.width / 2 - x * view.k;
  view.y = canvas.height / 2 - y * view.k;
  apply();
}

function setUpCanvas() {
  const canvas = $("canvas");
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    view.x = drag.vx + dx;
    view.y = drag.vy + dy;
    applySoon();
  });
  canvas.addEventListener("pointerup", (e) => {
    const wasDrag = drag?.moved;
    drag = null;
    if (wasDrag) return;
    const hit = document.elementsFromPoint(e.clientX, e.clientY)
      .map((el) => el.closest?.("g.node[data-cid]"))
      .find(Boolean);
    if (hit) choose(hit.dataset.cid);
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const box = canvas.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - box.left, e.clientY - box.top, { soon: true });
  }, { passive: false });
  const middle = () => {
    const box = canvas.getBoundingClientRect();
    return [box.width / 2, box.height / 2];
  };
  $("zoom-in").addEventListener("click", () => zoomAt(1.25, ...middle()));
  $("zoom-out").addEventListener("click", () => zoomAt(1 / 1.25, ...middle()));
  $("zoom-fit").addEventListener("click", fitView);
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, dialog")) return;
    if (e.key === "+" || e.key === "=") zoomAt(1.25, ...middle());
    else if (e.key === "-") zoomAt(1 / 1.25, ...middle());
    else if (e.key === "0") fitView();
    else if (e.key === "Escape") choose(null);
  });
}

// -- the detail panel ----------------------------------------------------------------

const HEADINGS = [
  ["inherits", "from", "Inherits from"],
  ["inherits", "to", "Inherited by"],
  ["implements", "from", "Implements"],
  ["implements", "to", "Implemented by"],
  ["holds", "from", "Holds"],
  ["holds", "to", "Held by"],
  ["uses", "from", "Uses"],
  ["uses", "to", "Used by"],
];

function choose(id) {
  chosen = id;
  mark();
  const name = id ? byId.get(id).name : "";
  if (decodeURIComponent(location.hash.slice(1)) !== name) {
    history.replaceState(null, "", name ? `#${encodeURIComponent(name)}` : location.pathname + location.search);
  }
  const panel = $("detail");
  if (!id) {
    panel.hidden = true;
    return;
  }
  const c = byId.get(id);
  const related = HEADINGS.map(([kind, end, title]) => {
    const other = end === "from" ? "to" : "from";
    const items = data.relations
      .filter((r) => r.kind === kind && r[end] === id)
      .map((r) => ({ target: byId.get(r[other]), label: r.label }));
    if (!items.length) return "";
    return `<h3>${title}</h3><ul class="rel">${items.map((i) => `<li><button data-go="${i.target.id}">${escape(i.target.name)}</button>${i.label ? ` <small>${escape(i.label)}</small>` : ""}</li>`).join("")}</ul>`;
  }).join("");
  const fields = c.fields.map((f) => `<li class="${f.private ? "private" : ""}"><span class="sig">${escape(f.name)}${f.type ? `: ${escape(f.type)}` : ""}</span></li>`).join("");
  const methods = c.methods.map((m) => `<li class="${m.private ? "private" : ""}"><span class="sig">${m.async ? "async " : ""}${escape(m.signature)}</span>${m.doc ? `<small>${prose(m.doc)}</small>` : ""}</li>`).join("");
  panel.innerHTML = `
    <button class="ghost close" data-act="close" title="Close (Esc)">✕</button>
    <h2>${escape(c.name)}</h2>
    <div class="row tight"><span class="chip">${escape(c.kind)}</span></div>
    <div class="where">${escape(c.module)}<br>${escape(c.where)}</div>
    <div class="actions">
      <button class="ghost" data-act="focus">${focus === id ? "Show everything" : "Only its neighbours"}</button>
      <button class="ghost" data-act="source">Source</button>
      <button class="ghost" data-act="centre">Centre</button>
    </div>
    ${c.doc ? `<p class="doc">${prose(c.doc)}</p>` : `<p class="doc muted">No docstring.</p>`}
    ${fields ? `<h3>${c.kind === "enumeration" ? "Members" : "Fields"}</h3><ul>${fields}</ul>` : ""}
    ${methods ? `<h3>Methods</h3><ul>${methods}</ul>` : ""}
    ${related}`;
  panel.hidden = false;
}

async function go(id) {
  if (!nodeOf(id)) {
    // not drawn under the current filters: show it with its neighbours
    focus = id;
    showFocus();
    await render({ fit: true });
  }
  choose(id);
  centreOn(id);
}

function showFocus() {
  const bar = $("focus-bar");
  bar.hidden = !focus;
  if (focus) {
    bar.innerHTML = `<span>Only <strong class="mono">${escape(byId.get(focus).name)}</strong> and its neighbours</span>
      <button class="ghost" id="unfocus">Show everything</button>`;
    $("unfocus").addEventListener("click", async () => {
      focus = null;
      showFocus();
      await render({ fit: true });
      if (chosen) choose(chosen);
    });
  }
}

async function showSource(id) {
  const c = byId.get(id);
  $("source-title").textContent = c.where;
  $("source-text").textContent = "…";
  $("source-dialog").showModal();
  const answer = await fetch(`/api/classes/source?id=${encodeURIComponent(id)}`);
  $("source-text").textContent = answer.ok ? (await answer.json()).source : "The source could not be read.";
}

function setUpDetail() {
  $("detail").addEventListener("click", async (e) => {
    const target = e.target.closest("[data-go], [data-act]");
    if (!target) return;
    if (target.dataset.go) return go(target.dataset.go);
    const act = target.dataset.act;
    if (act === "close") choose(null);
    else if (act === "source") showSource(chosen);
    else if (act === "centre") centreOn(chosen);
    else if (act === "focus") {
      focus = focus === chosen ? null : chosen;
      showFocus();
      await render({ fit: true });
      choose(chosen);
    }
  });
}

// -- the side bar --------------------------------------------------------------------

function setUpSide() {
  $("caption").textContent =
    `Every class in ${data.package}: ${data.classes.length} classes in ${data.areas.length} areas, read from the code when the app started.`;
  const counts = new Map();
  for (const c of data.classes) counts.set(c.area, (counts.get(c.area) ?? 0) + 1);
  $("areas").innerHTML = data.areas.map((a) => `
    <label title="${escape(a.doc)}">
      <input type="checkbox" data-area="${escape(a.name)}" ${settings.hidden.includes(a.name) ? "" : "checked"}>
      <span class="area-name">${escape(short(a.name)).replace(/\./g, ".<wbr>")}</span>
      <button class="link only" data-only="${escape(a.name)}" title="Show only this area">only</button>
      <small>${counts.get(a.name) ?? 0}</small>
    </label>`).join("");
  $("names").innerHTML = [...new Set(data.classes.map((c) => c.name))]
    .sort().map((n) => `<option value="${escape(n)}">`).join("");

  const rerender = async () => { save(); await render({ fit: true }); };
  $("areas").addEventListener("click", (e) => {
    const area = e.target.closest("[data-only]")?.dataset.only;
    if (!area) return;
    e.preventDefault();
    settings.hidden = data.areas.map((a) => a.name).filter((a) => a !== area);
    for (const box of $("areas").querySelectorAll("input")) box.checked = box.dataset.area === area;
    focus = null;
    showFocus();
    rerender();
  });
  $("areas").addEventListener("change", (e) => {
    const area = e.target.dataset.area;
    settings.hidden = e.target.checked
      ? settings.hidden.filter((a) => a !== area)
      : [...settings.hidden, area];
    rerender();
  });
  const setAll = (on) => {
    settings.hidden = on ? [] : data.areas.map((a) => a.name);
    for (const box of $("areas").querySelectorAll("input")) box.checked = on;
    rerender();
  };
  $("all-areas").addEventListener("click", () => setAll(true));
  $("no-areas").addEventListener("click", () => setAll(false));
  for (const [box, key] of [
    ["show-members", "members"], ["show-private", "private"],
    ["show-uses", "uses"], ["show-exceptions", "exceptions"],
  ]) {
    $(box).checked = settings[key];
    $(box).addEventListener("change", () => { settings[key] = $(box).checked; rerender(); });
  }

  $("search").addEventListener("input", () => {
    mark();
    const exact = data.classes.find((c) => c.name === $("search").value.trim());
    if (exact) {
      $("search").value = ""; // found: stop dimming the rest
      go(exact.id);
    }
  });
  $("search").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const query = $("search").value.trim().toLowerCase();
    const hit = data.classes.find((c) => c.name.toLowerCase() === query)
      ?? data.classes.find((c) => c.name.toLowerCase().includes(query));
    if (hit) {
      $("search").value = "";
      go(hit.id);
    }
  });

  $("copy").addEventListener("click", async () => {
    await navigator.clipboard.writeText(text);
    $("copy").textContent = "Copied";
    setTimeout(() => { $("copy").textContent = "Copy"; }, 1500);
  });
  $("download").addEventListener("click", () => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    link.download = `${data.package}-classes.mmd`;
    link.click();
    URL.revokeObjectURL(link.href);
  });
  $("view-source").addEventListener("click", () => {
    $("source-title").textContent = "The Mermaid text of what is drawn";
    $("source-text").textContent = text;
    $("source-dialog").showModal();
  });
}

// -- start ----------------------------------------------------------------------------

data = await (await fetch("/api/classes")).json();
byId = new Map(data.classes.map((c) => [c.id, c]));
settings.hidden = settings.hidden.filter((a) => data.areas.some((x) => x.name === a));
setUpMermaid();
setUpSide();
setUpCanvas();
setUpDetail();
await render({ fit: true });

// /classes#Name opens that class: a link to one class survives a reload.
async function fromHash() {
  const name = decodeURIComponent(location.hash.slice(1));
  const c = data.classes.find((x) => x.name === name);
  if (c && c.id !== chosen) await go(c.id);
}
window.addEventListener("hashchange", fromHash);
await fromHash();
