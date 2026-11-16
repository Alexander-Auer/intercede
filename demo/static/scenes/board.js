// Scene 4: the job board. The same consumer code submits to and polls every backend at once.

const selection = new Set();
let payload = "quick";
let copies = 3;
const targets = new Set(["fake"]);
let follow = null; // {key, timer}: diagnostics re-read while a job runs
const seen = new Map(); // key -> last drawn status, to animate changes
let viewer = null; // {title, text}

export default {
  title: "Job board",
  caption: "The same consumer code, unchanged, submits to and polls every backend.",

  sources(store) {
    return [...new Set(visibleColumns(store).filter((c) => c.state === "online").map((c) => c.source))];
  },

  mount(el, ctx) {
    el.innerHTML = `
      <div class="board-head">
        <div class="panel">
          <div class="row" style="justify-content: space-between">
            <strong>The consumer</strong>
            <span class="muted mono" style="font-size:14px">${ctx.escape(ctx.store.meta.consumer.path)}</span>
          </div>
          <div class="code" id="consumer-code" style="max-height: 230px; margin-top: 8px"></div>
        </div>
        <div class="panel submit-form">
          <div class="payloads" id="payloads"></div>
          <div class="muted" id="payload-blurb" style="font-size: 17px"></div>
          <div class="row">
            <label class="copies">Copies <input id="copies" type="number" min="1" max="50"></label>
            <span class="targets" id="targets"></span>
          </div>
          <div class="row"><button class="primary" id="submit">Submit</button><span class="muted" id="submit-note"></span></div>
        </div>
      </div>
      <div class="columns" id="columns"></div>
      <div class="actions panel" id="actions">
        <span class="count" id="selected-count"></span>
        <button class="action" data-op="kill">Kill</button>
        <button class="action" data-op="purge">Purge</button>
        <button class="action" data-op="get_output">Get output</button>
        <button class="action" data-op="get_diagnostics">Diagnostics</button>
        <button class="ghost" id="select-none">Clear selection</button>
      </div>
      <div class="outcomes" id="outcomes"></div>`;

    el.querySelector("#consumer-code").innerHTML = highlight(excerpt(ctx.store.meta.consumer.source));
    el.querySelector("#copies").value = copies;
    el.querySelector("#copies").addEventListener("change", (e) => { copies = Number(e.target.value) || 1; });
    el.querySelector("#payloads").addEventListener("click", (e) => {
      const name = e.target.closest("[data-payload]")?.dataset.payload;
      if (name) { payload = name; drawForm(el, ctx); }
    });
    el.querySelector("#targets").addEventListener("change", (e) => {
      const id = e.target.dataset.column;
      if (e.target.checked) targets.add(id); else targets.delete(id);
    });
    el.querySelector("#submit").addEventListener("click", async () => {
      const chosen = visibleColumns(ctx.store).filter((c) => targets.has(c.id) && c.state === "online").map((c) => c.id);
      if (!chosen.length) return ctx.toast("Choose at least one online backend.", "warning");
      await ctx.api("/api/submit", { columns: chosen, payload, copies }).catch((err) => ctx.toast(err.message));
    });
    el.querySelector("#columns").addEventListener("click", (e) => {
      const tile = e.target.closest(".tile[data-key]");
      if (!tile) return;
      const key = tile.dataset.key;
      if (!e.shiftKey && !e.ctrlKey && !e.metaKey) {
        if (selection.has(key) && selection.size === 1) selection.clear();
        else { selection.clear(); selection.add(key); }
      } else if (selection.has(key)) selection.delete(key);
      else selection.add(key);
      this.update(ctx);
    });
    el.querySelector("#columns").addEventListener("click", async (e) => {
      const stack = e.target.closest("[data-start]")?.dataset.start;
      if (stack) await ctx.api("/api/stack", { stack, action: "up" }).catch((err) => ctx.toast(err.message));
    });
    el.querySelector("#columns").addEventListener("dblclick", (e) => {
      const column = e.target.closest(".column[data-column]")?.dataset.column;
      if (!column) return;
      for (const job of ctx.store.jobs) if (job.column === column && job.id) selection.add(job.key);
      this.update(ctx);
    });
    el.querySelector("#select-none").addEventListener("click", () => { selection.clear(); this.update(ctx); });
    el.querySelector("#actions").addEventListener("click", (e) => {
      const op = e.target.closest("[data-op]")?.dataset.op;
      if (op) run(op, el, ctx);
    });
    el.querySelector("#outcomes").addEventListener("click", async (e) => {
      const link = e.target.closest("[data-file]");
      if (!link) return;
      e.preventDefault();
      const text = await (await fetch(`/api/file?path=${encodeURIComponent(link.dataset.file)}`)).text();
      viewer = { title: link.dataset.file, text };
      drawOutcomes(el, ctx);
    });
    drawForm(el, ctx);
    this.update(ctx);
  },

  unmount() {
    stopFollow();
  },

  update(ctx) {
    const el = document.getElementById("stage");
    if (!el.querySelector("#columns")) return;
    drawTargets(el, ctx);
    drawColumns(el, ctx);
    drawOutcomes(el, ctx);
    const selectable = [...selection].filter((key) => ctx.store.jobs.some((j) => j.key === key));
    el.querySelector("#selected-count").textContent = selectable.length
      ? `${selectable.length} selected`
      : "Click tiles to select (shift for more, double-click a column for all)";
    for (const button of el.querySelectorAll("#actions [data-op]")) button.disabled = !selectable.length || ctx.store.replaying;
    el.querySelector("#submit").disabled = ctx.store.replaying;
  },
};

function visibleColumns(store) {
  if (store.mode === "fake") return store.columns.filter((c) => c.source === "FAKE");
  return store.columns;
}

function drawForm(el, ctx) {
  el.querySelector("#payloads").innerHTML = ctx.store.meta.payloads
    .map((p) => `<button data-payload="${p.name}" class="${p.name === payload ? "active" : ""}">${ctx.escape(p.title)}</button>`)
    .join("");
  const chosen = ctx.store.meta.payloads.find((p) => p.name === payload);
  el.querySelector("#payload-blurb").textContent = chosen?.blurb ?? "";
}

function drawTargets(el, ctx) {
  const html = visibleColumns(ctx.store)
    .map((c) => `<label title="${ctx.escape(c.reason)}"><input type="checkbox" data-column="${c.id}" ${targets.has(c.id) ? "checked" : ""} ${c.state === "online" ? "" : "disabled"}>${ctx.escape(c.title)}</label>`)
    .join("");
  const box = el.querySelector("#targets");
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
}

const OPS = ["Submitter", "StatusReporter", "OutputRetriever", "Killable", "Purgeable", "Diagnosable"];

function drawColumns(el, ctx) {
  const container = el.querySelector("#columns");
  const columns = visibleColumns(ctx.store);
  for (const column of columns) {
    let card = container.querySelector(`.column[data-column="${column.id}"]`);
    if (!card) {
      card = document.createElement("section");
      card.className = "column";
      card.dataset.column = column.id;
      card.innerHTML = `<h2></h2><div class="meta"></div><div class="caps"></div><div class="offline-note"></div><div class="tiles"></div>`;
      container.append(card);
    }
    card.classList.toggle("offline", column.state !== "online");
    card.querySelector("h2").innerHTML = `${ctx.escape(column.title)} ${ctx.badge(column.source, true)}`;
    card.querySelector(".meta").innerHTML = column.state === "online"
      ? `<span class="mono">${ctx.escape(column.backend_class)}</span> · <span class="mono">${ctx.escape(column.resource)}</span>`
      : "offline";
    card.querySelector(".caps").innerHTML = column.state !== "online" ? "" : OPS.map((op) => `<span class="chip ${column.capabilities.includes(op) ? "on" : ""}" title="isinstance(backend, ${op})">${op}</span>`).join("");
    card.querySelector(".offline-note").innerHTML = column.state === "online" ? "" :
      `${ctx.escape(column.reason)}${column.start ? `<code>${ctx.escape(column.start)}</code><button class="ghost" data-start="${column.stack}" style="margin-top:8px">Start this stack</button>` : ""}`;
    drawTiles(card.querySelector(".tiles"), ctx.store.jobs.filter((j) => j.column === column.id), ctx);
  }
  for (const card of container.querySelectorAll(".column[data-column]")) {
    if (!columns.some((c) => c.id === card.dataset.column)) card.remove();
  }
}

function drawTiles(container, jobs, ctx) {
  const keys = new Set(jobs.map((j) => j.key));
  for (const tile of container.querySelectorAll(".tile")) if (!keys.has(tile.dataset.key)) tile.remove();
  for (const job of jobs) {
    let tile = container.querySelector(`.tile[data-key="${CSS.escape(job.key)}"]`);
    if (!tile) {
      tile = document.createElement("div");
      tile.className = "tile";
      tile.dataset.key = job.key;
      container.append(tile);
    }
    const before = seen.get(job.key);
    if (before !== undefined && before !== job.status) {
      tile.classList.remove("changed");
      void tile.offsetWidth; // restart the animation
      tile.classList.add("changed");
    }
    seen.set(job.key, job.status);
    tile.dataset.status = job.status;
    tile.classList.toggle("selected", selection.has(job.key));
    tile.classList.toggle("purged", job.purged);
    tile.title = job.reason || job.key;
    tile.innerHTML = `<div class="id">${ctx.escape(job.id ?? `copy ${job.copy}`)}</div>
      <div class="st">${ctx.escape(job.status)}</div>
      <div class="pl">${ctx.escape(job.payload)} · copy ${job.copy}</div>
      ${job.reason ? `<div class="why">${ctx.escape(job.reason)}</div>` : ""}`;
  }
}

async function run(op, el, ctx) {
  const keys = [...selection].filter((key) => ctx.store.jobs.some((j) => j.key === key && j.id));
  if (!keys.length) return;
  stopFollow();
  viewer = null;
  try {
    await ctx.api("/api/action", { operation: op, keys });
  } catch (err) {
    ctx.toast(err.message);
    return;
  }
  if (op === "get_diagnostics" && keys.length === 1) {
    const job = ctx.store.jobs.find((j) => j.key === keys[0]);
    if (job && ["Submitted", "Waiting", "Running"].includes(job.status)) {
      follow = {
        key: keys[0],
        timer: setInterval(async () => {
          const now = ctx.store.jobs.find((j) => j.key === keys[0]);
          if (!now || !["Submitted", "Waiting", "Running"].includes(now.status)) return stopFollow();
          await ctx.api("/api/action", { operation: op, keys }).catch(() => stopFollow());
        }, 1500),
      };
    }
  }
}

function stopFollow() {
  if (follow) clearInterval(follow.timer);
  follow = null;
}

function drawOutcomes(el, ctx) {
  const box = el.querySelector("#outcomes");
  const event = ctx.store.outcomes;
  if (!event) { box.innerHTML = ""; return; }
  const rows = Object.entries(event.results).map(([key, r]) => {
    const job = ctx.store.jobs.find((j) => j.key === key);
    const name = job ? `${job.column} · ${job.id}` : key;
    let detail = ctx.escape(r.reason ?? (r.ok ? "" : "failed"));
    if (r.value?.files !== undefined) {
      const links = [["stdout", r.value.stdout], ["stderr", r.value.stderr], ["log", r.value.log],
        ...Object.entries(r.value.files)].filter(([, p]) => p);
      detail = `<span class="files">${links.map(([n, p]) => `<a href="#" data-file="${ctx.escape(p)}">${ctx.escape(n)}</a>`).join("")}</span>`;
    } else if (r.value?.content !== undefined) {
      detail = `<div class="muted mono" style="font-size:14px">${ctx.escape(r.value.filename)}${follow?.key === key ? " · following" : ""}</div><pre class="code viewer">${ctx.escape(r.value.content)}</pre>`;
    }
    return `<div class="outcome"><span class="ok ${r.ok ? "yes" : "no"}">${r.ok ? "✓" : "✗"}</span><span class="mono">${ctx.escape(name)}</span><span>${detail}</span></div>`;
  });
  const view = viewer ? `<div class="panel"><div class="mono muted" style="font-size:14px">${ctx.escape(viewer.title)}</div><pre class="code viewer">${ctx.escape(viewer.text)}</pre></div>` : "";
  box.innerHTML = `<div class="muted">${ctx.escape(event.operation)}: one outcome per job</div>${rows.join("")}${view}`;
}

// -- the consumer's code, as shown -------------------------------------------

function excerpt(source) {
  // From the first function to the end: the module docstring and imports are on the slide anyway.
  const start = source.indexOf("async def submit");
  const end = source.indexOf("async def act");
  return source.slice(start, end > start ? end : undefined).trimEnd();
}

function highlight(source) {
  const esc = (t) => t.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
  return source
    .split("\n")
    .map((line) => {
      let html = esc(line);
      const hash = html.indexOf("#");
      let comment = "";
      if (hash >= 0) { comment = `<span class="cm">${html.slice(hash)}</span>`; html = html.slice(0, hash); }
      html = html
        .replace(/\b(async|def|await|return|for|in|if|and|raise)\b/g, '<span class="kw">$1</span>')
        .replace(/\b(submit|poll|get_status)\b(?=\()/g, '<span class="fn">$1</span>');
      if (/await backend\./.test(line)) html = `<span class="hl">${html}</span>`;
      return html + comment;
    })
    .join("\n");
}
