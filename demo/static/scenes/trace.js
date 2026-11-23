// Scene 5: what one contract call does underneath, drawn as a sequence diagram from the trace.

let chosen = null; // operation id shown on the left
let compared = null; // operation id shown on the right, in compare mode
let showPolls = false;
let detail = null; // {op, index}: the step whose details are open

const LANE_TITLES = {
  process: "a program",
  http: "HTTP",
};

/** A step that crossed to the resource: through a transport, or a door (a program, HTTP). */
export function isExchange(step) {
  return Boolean(step.exchange) || step.lane === "transport" || step.lane === "tools"; // "tools": older recordings
}

export default {
  title: "Trace",
  caption: "One call on the contract, and everything it causes underneath.",

  sources(store) {
    const ops = [chosen, compared].map((id) => store.ops.get(id)).filter(Boolean);
    return [...new Set(ops.map((op) => op.source))];
  },

  mount(el, ctx) {
    el.innerHTML = `
      <div style="display:grid; grid-template-columns: 330px 1fr; gap: 18px; height: 100%">
        <div class="panel" style="overflow:auto">
          <div class="row" style="justify-content: space-between">
            <strong>Operations</strong>
            <label style="font-size:15px"><input type="checkbox" id="polls"> polls</label>
          </div>
          <div class="muted" style="font-size:14px; margin: 4px 0 8px">Click to show; shift-click to compare.</div>
          <div id="op-list" style="display:grid; gap:4px"></div>
        </div>
        <div style="display:grid; grid-template-rows: auto 1fr auto; gap: 12px; min-height: 0">
          <div class="row">
            <button class="ghost" id="compare-submits">Compare the latest submit on two backends</button>
            <button class="ghost" id="uncompare">Single</button>
          </div>
          <div id="diagrams" style="display:grid; gap:16px; overflow:auto; min-height: 0"></div>
          <div id="step-detail"></div>
        </div>
      </div>`;
    el.querySelector("#polls").checked = showPolls;
    el.querySelector("#polls").addEventListener("change", (e) => { showPolls = e.target.checked; this.update(ctx); });
    el.querySelector("#op-list").addEventListener("click", (e) => {
      const id = Number(e.target.closest("[data-op]")?.dataset.op);
      if (!id) return;
      if (e.shiftKey) compared = id; else { chosen = id; detail = null; }
      this.update(ctx);
    });
    el.querySelector("#compare-submits").addEventListener("click", () => {
      const submits = [...ctx.store.ops.values()].filter((op) => op.name === "submit").reverse();
      const first = submits[0];
      const other = submits.find((op) => op.column !== first?.column);
      if (!first || !other) return ctx.toast("Submit the same payload to two backends first.", "warning");
      chosen = other.id;
      compared = first.id;
      detail = null;
      this.update(ctx);
    });
    el.querySelector("#uncompare").addEventListener("click", () => { compared = null; this.update(ctx); });
    el.querySelector("#diagrams").addEventListener("click", (e) => {
      const row = e.target.closest("[data-step]");
      if (!row) return;
      detail = { op: Number(row.dataset.opid), index: Number(row.dataset.step) };
      drawDetail(ctx);
    });
    this.update(ctx);
  },

  update(ctx) {
    const el = document.getElementById("stage");
    if (!el.querySelector("#op-list")) return;
    const ops = [...ctx.store.ops.values()].filter((op) => showPolls || op.name !== "get_status");
    if (!chosen || !ctx.store.ops.has(chosen)) chosen = ops.at(-1)?.id ?? null;
    el.querySelector("#op-list").innerHTML = ops.slice(-80).reverse().map((op) => opItem(op, ctx)).join("");
    const shown = [chosen, compared].filter((id) => id && ctx.store.ops.has(id));
    const diagrams = el.querySelector("#diagrams");
    diagrams.style.gridTemplateColumns = shown.length > 1 ? "1fr 1fr" : "1fr";
    diagrams.innerHTML = shown.length
      ? shown.map((id) => diagram(ctx.store.ops.get(id), ctx, (diagrams.clientWidth - 16 * (shown.length - 1)) / shown.length - 24)).join("")
      : `<div class="placeholder">Submit something on the job board; its trace appears here.</div>`;
    drawDetail(ctx);
  },
};

function opItem(op, ctx) {
  const ms = op.ended ? Math.round((op.ended - op.started) * 1000) : null;
  const failed = op.outcome?.raised;
  const active = op.id === chosen || op.id === compared;
  return `<button data-op="${op.id}" class="ghost" style="text-align:left; font-size:15px; ${active ? "border-color: var(--accent); border-width:2px" : ""}">
    <span class="mono">#${op.id}</span> <strong>${ctx.escape(op.name)}</strong> ${ctx.badge(op.source, true)}
    <div class="muted" style="font-size:13px">${ctx.escape(op.column)} · ${op.steps.length} steps · ${ms === null ? "running" : `${ms} ms`}${failed ? ` · <span style="color:var(--st-failed)">${ctx.escape(failed)}</span>` : ""}</div>
  </button>`;
}

// -- the diagram ----------------------------------------------------------------

const ROW = 40;
const TOP = 62;

function diagram(op, ctx, available) {
  const column = ctx.store.columns.find((c) => c.id === op.column);
  const base = column?.lanes ?? ["consumer", "backend", "resource"];
  // The doors a backend went through become lanes as it uses them, above the resource.
  const doors = [...new Set(op.steps.map((s) => s.lane))].filter((lane) => !base.includes(lane));
  const lanes = [...base.slice(0, -1), ...doors, base[base.length - 1]];
  const rows = rowsOf(op, lanes);
  const width = Math.max(520, Math.floor(available || 760));
  const margin = 60;
  const right = 90; // room for the timings
  const x = (lane) => margin + (lanes.indexOf(lane) * (width - margin - right)) / (lanes.length - 1);
  const height = TOP + rows.length * ROW + 30;
  const parts = [];
  for (const lane of lanes) {
    parts.push(`<line x1="${x(lane)}" y1="${TOP - 20}" x2="${x(lane)}" y2="${height - 10}" class="lifeline"/>`);
    parts.push(`<text x="${x(lane)}" y="28" class="lane">${ctx.escape(LANE_TITLES[lane] ?? lane)}</text>`);
  }
  rows.forEach((row, i) => {
    const y = TOP + i * ROW + 10;
    const x1 = x(row.from);
    const x2 = x(row.to);
    const cls = row.back ? "arrow back" : row.error ? "arrow error" : "arrow";
    parts.push(`<g class="step ${row.index !== undefined ? "clickable" : ""}" ${row.index !== undefined ? `data-step="${row.index}" data-opid="${op.id}"` : ""}>`);
    parts.push(`<rect x="0" y="${y - 26}" width="${width}" height="${ROW}" class="hit"/>`);
    parts.push(`<line x1="${x1}" y1="${y}" x2="${x2}" y2="${y}" class="${cls}" marker-end="url(#head)"/>`);
    if (row.via) parts.push(`<circle cx="${x(row.via)}" cy="${y}" r="6" class="dot"/>`);
    // Left-aligned from the arrow's start, and cut where the diagram ends: argv can be long.
    const labelX = Math.min(x1, x2) + (row.back ? 18 : 8);
    const fits = Math.max(12, Math.floor((width - labelX - 80) / 9.8));
    parts.push(`<text x="${labelX}" y="${y - 6}" class="label ${row.error ? "error" : ""}"><title>${ctx.escape(row.full ?? row.label)}</title>${ctx.escape(cut(row.label, fits))}</text>`);
    if (row.ms !== undefined) parts.push(`<text x="${width - 4}" y="${y + 4}" class="ms">${row.ms} ms</text>`);
    parts.push("</g>");
  });
  return `<div class="panel" style="padding:10px">
    <div class="row" style="justify-content: space-between">
      <strong>#${op.id} ${ctx.escape(op.name)} on ${ctx.escape(column?.title ?? op.column)}</strong>
      <span class="mono muted" style="font-size:14px">${ctx.escape(column?.backend_class ?? "")}</span>
    </div>
    <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="sequence" role="img" aria-label="sequence diagram">
      <defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="head"/></marker></defs>
      ${parts.join("")}
    </svg>
  </div>`;
}

function rowsOf(op, lanes) {
  const rows = [{ from: "consumer", to: "backend", label: requestLabel(op) }];
  op.steps.forEach((step, index) => {
    const lane = lanes.includes(step.lane) ? step.lane : "resource";
    const failed = step.raised ? `${step.raised}: ${step.message}` : null;
    if (isExchange(step)) {
      const what = step.sent ?? (step.call === "put" ? `put ${step.files?.length ?? ""} files → ${step.remote}`
        : step.call === "get" ? `get ${step.remote}` : `$ ${(step.argv ?? []).join(" ")}`);
      // One row through the transport (or the door) to the resource: it only passes the call on.
      rows.push({ from: "backend", to: "resource", via: lane, label: what, full: what, index });
      rows.push({
        from: "resource", to: "backend", back: true, index, ms: step.ms, error: Boolean(failed),
        label: failed ?? (step.answer !== undefined ? `${step.answer}${firstLine(step.said)}`
          : step.returncode !== undefined ? `exit ${step.returncode}${firstLine(step.stdout)}` : step.files ? `${step.files.length} files` : step.bytes !== undefined ? `${step.bytes ?? "?"} bytes` : "done"),
      });
    } else if (step.lane === "provider") {
      rows.push({ from: "backend", to: "provider", label: `get(${step.asked?.kind}, ${(step.asked?.scopes ?? []).join(" ") || "no scopes"})`, index });
      rows.push({ from: "provider", to: "backend", back: true, index, ms: step.ms, error: Boolean(failed), label: failed ?? `${step.returned?.kind}(**********)` });
    } else if (step.lane === "scheduler") {
      const built = step.argv ? step.argv.join(" ") : step.parsed !== undefined ? summary(step.parsed) : "";
      rows.push({ from: "backend", to: "scheduler", label: step.call, index });
      rows.push({ from: "scheduler", to: "backend", back: true, index, error: Boolean(failed), label: failed ?? built, full: built });
    } else {
      rows.push({ from: "backend", to: lane, label: step.call, index, ms: step.ms });
    }
  });
  if (op.outcome) {
    const raised = op.outcome.raised;
    rows.push({
      from: "backend", to: "consumer", back: true, error: Boolean(raised),
      ms: op.ended ? Math.round((op.ended - op.started) * 1000) : undefined,
      label: raised ? `raise ${raised}: ${op.outcome.message}` : outcomeLabel(op),
    });
  }
  return rows;
}

function requestLabel(op) {
  const r = op.request ?? {};
  if (op.name === "submit") {
    const copies = Array.isArray(r.copies) ? r.copies.length : r.copies;
    return `submit(spec, ${copies} copies)`;
  }
  return `${op.name}(${(r.handles ?? []).length} handles)`;
}

function outcomeLabel(op) {
  const o = op.outcome ?? {};
  if (o.handles) return `Submission: ${Object.keys(o.handles).length} handles, ${Object.keys(o.failures ?? {}).length} refused`;
  if (o.per_job) {
    const values = Object.values(o.per_job);
    if (values.every((v) => typeof v === "string")) return summary(o.per_job);
    const ok = values.filter((v) => v?.ok).length;
    return `${ok} ok, ${values.length - ok} not`;
  }
  return "done";
}

function summary(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const counts = {};
    for (const v of Object.values(value)) counts[v] = (counts[v] ?? 0) + 1;
    return Object.entries(counts).map(([k, n]) => `${n}× ${k}`).join(", ") || "nothing";
  }
  return Array.isArray(value) ? value.join(" ") : String(value);
}

function firstLine(text) {
  const line = (text ?? "").trim().split("\n")[0];
  return line ? ` · ${line}` : "";
}

function cut(text, n) {
  text = String(text ?? "");
  return text.length > n ? `${text.slice(0, n - 1)}…` : text;
}

// -- what one step carried --------------------------------------------------------

function drawDetail(ctx) {
  const box = document.getElementById("step-detail");
  if (!box) return;
  const op = detail && ctx.store.ops.get(detail.op);
  const step = op?.steps[detail.index];
  if (!step) {
    box.innerHTML = `<div class="muted" style="font-size:16px">Click an arrow to see exactly what crossed that layer.</div>`;
    return;
  }
  const parts = [`<strong>${ctx.escape(step.lane)} · ${ctx.escape(step.call)}</strong> <span class="muted">${step.ms} ms</span>`];
  if (step.sent) parts.push(block("sent", step.sent, ctx));
  if (step.argv) parts.push(block("argv", JSON.stringify(step.argv), ctx));
  if (step.asked) parts.push(block("asked for", JSON.stringify(step.asked, null, 1), ctx));
  if (step.returned) parts.push(block("returned", JSON.stringify(step.returned, null, 1), ctx));
  if (step.env) parts.push(block("environment (only what the backend sets)", Object.entries(step.env).map(([k, v]) => `${k}=${v}`).join("\n"), ctx));
  for (const file of step.files ?? []) {
    parts.push(block(`file ${file.name}${file.bytes !== undefined ? ` (${file.bytes} bytes)` : ""}`, file.text ?? "(contents not shown)", ctx));
  }
  if (step.parsed !== undefined) parts.push(block("parsed", JSON.stringify(step.parsed, null, 1), ctx));
  if (step.returncode !== undefined) parts.push(block(`exit ${step.returncode} · stdout`, step.stdout || "(empty)", ctx));
  else if (step.answer !== undefined && step.said !== undefined) parts.push(block(`answer · ${step.answer}`, step.said || "(empty)", ctx));
  if (step.stderr) parts.push(block("stderr", step.stderr, ctx));
  if (step.raised) parts.push(block(step.raised, step.message, ctx));
  box.innerHTML = `<div class="panel" style="max-height: 38vh; overflow:auto; display:grid; gap:8px">${parts.join("")}</div>`;
}

function block(title, text, ctx) {
  return `<div><div class="muted" style="font-size:14px">${ctx.escape(title)}</div><pre class="code" style="max-height: 240px">${ctx.escape(text)}</pre></div>`;
}
