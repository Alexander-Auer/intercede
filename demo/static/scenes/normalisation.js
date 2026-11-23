// Scene 6: what each resource answered, next to what interCEde returned for it.
// Drawn from the traces: the latest call of one operation on every backend.

import { isExchange } from "./trace.js";

const OPERATIONS = [
  ["get_status", "Status"],
  ["submit", "Submit"],
  ["kill", "Kill"],
  ["purge", "Purge"],
  ["get_output", "Output"],
];
const MAX_LINES = 14;

let operation = "get_status";

export default {
  title: "Normalisation",
  caption: "Each resource answers in its own format; interCEde returns the same types for every one of them.",

  sources(store) {
    return [...new Set(latest(store).map(([, op]) => op.source))];
  },

  mount(el, ctx) {
    el.innerHTML = `
      <div class="panel row" style="margin-bottom:14px">
        <span class="muted">The latest</span>
        <div class="row" style="gap:6px" id="norm-ops">${OPERATIONS.map(([name, title]) => `<button class="ghost" data-norm="${name}">${title}</button>`).join("")}</div>
        <span class="muted">on every backend</span>
      </div>
      <div class="norm-head"><span></span><strong>What the resource said</strong><span></span><strong>What interCEde returned</strong></div>
      <div id="norm-rows"></div>`;
    el.querySelector("#norm-ops").addEventListener("click", (e) => {
      const name = e.target.closest("[data-norm]")?.dataset.norm;
      if (name) { operation = name; this.update(ctx); }
    });
    this.update(ctx);
  },

  update(ctx) {
    const el = document.getElementById("stage");
    if (!el.querySelector("#norm-rows")) return;
    for (const button of el.querySelectorAll("[data-norm]")) button.classList.toggle("on", button.dataset.norm === operation);
    const rows = latest(ctx.store);
    el.querySelector("#norm-rows").innerHTML = rows.length
      ? rows.map(([column, op]) => row(column, op, ctx)).join("")
      : `<div class="placeholder" style="height:auto; padding: 50px">No ${escapeName(operation)} yet: run one on the job board.</div>`;
  },
};

function escapeName(name) {
  return OPERATIONS.find(([n]) => n === name)?.[1].toLowerCase() ?? name;
}

/** The latest finished call of the chosen operation, per column, in the board's order. */
function latest(store) {
  const found = new Map();
  for (const op of store.ops.values()) {
    if (op.name === operation && op.outcome && !op.column.startsWith("dry-run")) found.set(op.column, op);
  }
  return store.columns.filter((c) => found.has(c.id)).map((c) => [c, found.get(c.id)]);
}

function row(column, op, ctx) {
  return `<div class="norm-row">
    <div class="norm-backend"><strong>${ctx.escape(column.title)}</strong> ${ctx.badge(op.source, true)}
      <div class="mono muted" style="font-size:13px">${ctx.escape(column.backend_class ?? "")}</div></div>
    <div class="norm-raw">${raw(op, ctx)}</div>
    <div class="norm-arrow">→</div>
    <div class="norm-out">${normalised(op, ctx)}</div>
  </div>`;
}

// -- the resource's own words ----------------------------------------------------

function raw(op, ctx) {
  // What crossed to the resource and what it answered; a copy of files gets no answer.
  const exchanges = op.steps.filter((s) => isExchange(s) && (s.said !== undefined || s.stdout !== undefined || s.raised));
  if (!exchanges.length) {
    return op.column === "fake"
      ? `<div class="muted" style="font-size:16px">In process: the Fake has no resource to ask and nothing to parse.</div>`
      : `<div class="muted" style="font-size:16px">Nothing was asked of the resource${op.outcome?.raised ? "" : " (nothing to ask it about)"}.</div>`;
  }
  return exchanges.map((s) => {
    const sent = s.sent ?? `$ ${(s.argv ?? []).join(" ")}`;
    const answer = s.answer ?? (s.returncode !== undefined ? `exit ${s.returncode}` : "");
    const bad = Boolean(s.raised) || (s.returncode ?? 0) !== 0 || /^[45]\d\d/.test(answer);
    const out = lines(s.said ?? s.stdout);
    const err = bad ? lines(s.said_err ?? s.stderr, 3) : "";
    return `<div class="exchange">
      <div class="mono cmd">${ctx.escape(shortened(sent))} ${answer ? `<span class="exit ${bad ? "bad" : ""}">${ctx.escape(answer)}</span>` : ""}</div>
      ${out ? `<pre class="code said">${ctx.escape(out)}</pre>` : `<div class="muted" style="font-size:14px">(said nothing)</div>`}
      ${err ? `<pre class="code said err">${ctx.escape(err)}</pre>` : ""}
      ${s.raised ? `<div class="exit bad">${ctx.escape(s.raised)}: ${ctx.escape(s.message)}</div>` : ""}
    </div>`;
  }).join("");
}

function shortened(line) {
  return line.split(" ").map(shorten).join(" ");
}

function shorten(arg) {
  // A long path or ID list says nothing on a slide; its end does.
  return arg.length > 48 ? `…${arg.slice(-40)}` : arg;
}

function lines(text, keep = MAX_LINES) {
  const all = (text ?? "").trimEnd().split("\n").filter((l) => l.trim());
  return all.length > keep ? [...all.slice(0, keep), `… ${all.length - keep} more lines`].join("\n") : all.join("\n");
}

// -- the same types, whatever the resource ----------------------------------------

function idOf(key) {
  // A handle's key is backend:resource:id, each part escaped.
  return decodeURIComponent(String(key).split(":").pop());
}

function normalised(op, ctx) {
  const o = op.outcome ?? {};
  if (o.raised) return `<div class="refusal" style="margin:0"><strong>raise ${ctx.escape(o.raised)}</strong><pre>${ctx.escape(o.message)}</pre></div>`;
  const type = { submit: "Submission", get_status: "Mapping[JobHandle, JobStatus]" }[op.name] ?? `Mapping[JobHandle, OpOutcome[${op.name === "get_output" ? "JobOutput" : "None"}]]`;
  let body = "";
  if (op.name === "submit") {
    body = [
      ...Object.entries(o.handles ?? {}).map(([i, key]) => tile("Done", `copy ${i}`, `handle ${idOf(key)}`, ctx)),
      ...Object.entries(o.failures ?? {}).map(([i, why]) => tile("Failed", `copy ${i}`, why, ctx)),
    ].join("");
  } else if (op.name === "get_status") {
    body = Object.entries(o.per_job ?? {}).map(([key, status]) => tile(status, idOf(key), status, ctx)).join("");
  } else {
    body = Object.entries(o.per_job ?? {}).map(([key, r]) => {
      const files = r.value?.files ? Object.keys(r.value.files) : null;
      const what = r.ok ? (files ? `${files.length} files${r.value.stdout ? " + stdout" : ""}` : r.reason ?? "ok") : r.reason;
      return tile(r.ok ? "Done" : "Failed", idOf(key), `${r.ok ? "ok" : "not ok"}${what ? `: ${what}` : ""}`, ctx);
    }).join("");
  }
  return `<div class="mono muted" style="font-size:14px; margin-bottom:6px">${ctx.escape(type)}</div><div class="tiles">${body || `<span class="muted">empty</span>`}</div>`;
}

function tile(status, id, text, ctx) {
  return `<div class="tile" data-status="${ctx.escape(status)}" style="cursor:default; min-height:0">
    <div class="id">${ctx.escape(id)}</div><div class="why" style="font-size:14px">${ctx.escape(text)}</div></div>`;
}
