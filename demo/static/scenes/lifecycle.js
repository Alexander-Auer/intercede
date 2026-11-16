// Scene 6: the JobStatus state machine, the handle a consumer stores, and a restart that finds every job again.

let chosen = null; // job key

// Where each JobStatus is drawn. The states themselves come from the code
// (/api/meta reads JobStatus); a state missing here is drawn in the spare row,
// so a new one in the library shows up instead of being lost.
const PLACES = {
  Submitted: [90, 90],
  Waiting: [270, 90],
  Running: [450, 90],
  Done: [660, 50],
  Failed: [660, 130],
  Aborted: [660, 230],
  Unknown: [270, 300],
};
// Transitions: the resource moves a job forward; the consumer's operations do the rest.
const EDGES = [
  ["Submitted", "Waiting", ""],
  ["Waiting", "Running", ""],
  ["Running", "Done", ""],
  ["Running", "Failed", ""],
];

export default {
  title: "Lifecycle",
  caption: "interCEde keeps no state: the consumer keeps the handle, and that is enough.",

  sources(store) {
    const job = store.jobs.find((j) => j.key === chosen);
    const column = job && store.columns.find((c) => c.id === job.column);
    return column ? [column.source] : [];
  },

  mount(el, ctx) {
    el.innerHTML = `
      <div style="display:grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 18px">
        <div style="display:grid; gap: 14px; align-content:start">
          <div class="panel row">
            <label>Job <select id="job-pick" style="font-size:18px; padding:4px 8px; border-radius:8px; max-width: 420px"></select></label>
            <span class="muted" id="job-status"></span>
          </div>
          <div class="panel"><div id="machine"></div></div>
          <div class="panel">
            <div class="row">
              <button class="primary" id="restart">Restart interCEde</button>
              <button class="ghost" id="forge">Ask about a forged handle</button>
            </div>
            <div id="restart-log" style="margin-top:10px"></div>
            <div id="forged" style="margin-top:10px"></div>
          </div>
        </div>
        <div style="display:grid; gap: 14px; align-content:start">
          <div class="panel">
            <strong>The job's handle</strong> <span class="muted" style="font-size:15px">JobHandle, as JSON</span>
            <pre class="code" id="handle" style="margin-top:8px; font-size:17px"></pre>
          </div>
          <div style="text-align:center; font-size: 30px; color: var(--ink-2)">⬇ stored by the consumer</div>
          <div class="panel" style="border: 3px solid var(--accent)">
            <strong>The consumer's database</strong> <span class="muted" style="font-size:15px">all interCEde ever needs again</span>
            <div id="database" class="code" style="margin-top:8px; max-height: 330px; font-size:14px"></div>
          </div>
        </div>
      </div>`;
    el.querySelector("#job-pick").addEventListener("change", (e) => { chosen = e.target.value; this.update(ctx); });
    el.querySelector("#restart").addEventListener("click", async () => {
      const job = ctx.store.jobs.find((j) => j.key === chosen);
      if (!job) return ctx.toast("Choose a job first.", "warning");
      await ctx.api("/api/restart", { column: job.column }).catch((err) => ctx.toast(err.message));
    });
    el.querySelector("#forge").addEventListener("click", async () => {
      if (!chosen) return ctx.toast("Choose a job first.", "warning");
      await ctx.api("/api/forge", { key: chosen }).catch((err) => ctx.toast(err.message));
    });
    this.update(ctx);
  },

  update(ctx) {
    const el = document.getElementById("stage");
    const pick = el.querySelector("#job-pick");
    if (!pick) return;
    const jobs = ctx.store.jobs.filter((j) => j.handle);
    if (!jobs.some((j) => j.key === chosen)) chosen = jobs.at(-1)?.key ?? null;
    const options = jobs.map((j) => `<option value="${ctx.escape(j.key)}" ${j.key === chosen ? "selected" : ""}>${ctx.escape(j.column)} · ${ctx.escape(j.id)} · ${ctx.escape(j.payload)}</option>`).join("");
    if (pick.dataset.html !== options) { pick.innerHTML = options || "<option>submit a job on the board first</option>"; pick.dataset.html = options; }
    const job = jobs.find((j) => j.key === chosen);
    el.querySelector("#job-status").innerHTML = job ? `now <strong>${ctx.escape(job.status)}</strong>${job.purged ? " · purged" : ""}` : "";
    el.querySelector("#machine").innerHTML = machine(ctx, job);
    el.querySelector("#handle").textContent = job ? JSON.stringify(job.handle, null, 2) : "";
    el.querySelector("#database").innerHTML = Object.keys(ctx.store.database).map((key) =>
      `<div style="${key === chosen ? "background: color-mix(in srgb, var(--focus) 30%, transparent); font-weight:700" : ""}">${ctx.escape(key)}</div>`).join("") || `<span class="muted">empty</span>`;
    drawRestart(el, ctx, job);
    drawForged(el, ctx);
  },
};

function machine(ctx, job) {
  const statuses = ctx.store.meta.statuses;
  let spare = 0;
  const at = {};
  for (const status of statuses) at[status] = PLACES[status] ?? [450 + 150 * spare++, 300];
  const path = (job?.history ?? []).map(([, s]) => s);
  const parts = [];
  // The groups the operations act on.
  parts.push(`<rect x="20" y="45" width="510" height="90" rx="14" class="group"/><text x="30" y="38" class="group-label">active</text>`);
  parts.push(`<rect x="590" y="15" width="140" height="255" rx="14" class="group"/><text x="600" y="290" class="group-label">finished</text>`);
  for (const [from, to] of EDGES) parts.push(arrow(at[from], at[to], "", path.includes(from) && path.includes(to)));
  parts.push(arrow([440, 135], [at.Aborted[0] - 62, at.Aborted[1]], "kill()", path.includes("Aborted"), true, [-30, 6]));
  parts.push(arrow([600, 262], [at.Unknown[0] + 62, at.Unknown[1]], "forgotten, e.g. purge()", path.includes("Unknown"), true, [0, 22]));
  for (const status of statuses) {
    const [x, y] = at[status];
    const visited = path.indexOf(status);
    const now = job?.status === status;
    parts.push(`<g class="state ${now ? "now" : ""} ${visited >= 0 ? "visited" : ""}" data-status="${status}">
      <rect x="${x - 62}" y="${y - 22}" width="124" height="44" rx="22"/>
      <text x="${x}" y="${y + 6}">${ctx.escape(status)}</text>
      ${visited >= 0 && !now ? `<text x="${x + 58}" y="${y - 14}" class="step-no">${visited + 1}</text>` : ""}
    </g>`);
  }
  return `<svg viewBox="0 0 760 330" class="machine" role="img" aria-label="JobStatus state machine">
    <defs><marker id="mhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>
    ${parts.join("")}</svg>`;
}

function arrow(a, b, label, lit, operation = false, nudge = [0, 0]) {
  const [x1, y1] = a;
  const [x2, y2] = b;
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  // Stop at the pill's edge, not its centre.
  const sx = x1 + (dx / len) * (operation ? 0 : 64);
  const sy = y1 + (dy / len) * (operation ? 0 : 20);
  const ex = x2 - (dx / len) * (operation ? 0 : 64);
  const ey = y2 - (dy / len) * (operation ? 0 : 20);
  const text = label ? `<text x="${(sx + ex) / 2 + nudge[0]}" y="${(sy + ey) / 2 - 8 + nudge[1]}" class="edge-label">${label}</text>` : "";
  return `<line x1="${sx}" y1="${sy}" x2="${ex}" y2="${ey}" class="edge ${lit ? "lit" : ""} ${operation ? "op" : ""}" marker-end="url(#mhead)"/>${text}`;
}

function drawRestart(el, ctx, job) {
  const box = el.querySelector("#restart-log");
  const event = ctx.store.restart;
  if (!event) {
    box.innerHTML = `<div class="muted" style="font-size:17px">Closes ${job ? `the ${ctx.escape(job.column)} backend` : "the backend"}, builds a new one from its configuration, and asks it about every handle in the database.</div>`;
    return;
  }
  const restarted = ctx.store.columns.find((c) => c.id === event.column)?.title ?? event.column;
  box.innerHTML = `<div class="muted" style="font-size:16px">Restart of ${ctx.escape(restarted)}</div>` + event.steps.map((step, i) => `
    <div style="font-size:18px; margin: 4px 0; ${step.error ? "color: var(--st-failed)" : ""}">${i + 1}. ${ctx.escape(step.text)}
      ${step.statuses ? `<div class="row" style="gap:6px; margin-top:6px">${Object.entries(step.statuses).map(([id, s]) => `<span class="tile" data-status="${ctx.escape(s)}" style="min-height:0; cursor:default"><span class="id">${ctx.escape(id)}</span> ${ctx.escape(s)}</span>`).join("")}</div>` : ""}
    </div>`).join("");
}

function drawForged(el, ctx) {
  const event = ctx.store.forged;
  el.querySelector("#forged").innerHTML = event ? `
    <div style="font-size:18px">A handle like a stored one, with an ID the resource never gave out: <code>${ctx.escape(event.handle.id)}</code></div>
    <div class="row" style="margin-top:6px"><span class="tile" data-status="${ctx.escape(event.status)}" style="min-height:0; cursor:default"><span class="st">${ctx.escape(event.status)}</span></span>
    <span class="muted">reported, never dropped</span></div>` : "";
}
