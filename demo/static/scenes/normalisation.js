// Scene 6: what each resource answered, next to what interCEde returned for it.
// Drawn from the traces: the latest call of one operation on every backend.

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
  const exchanges = op.steps.filter((s) => (s.lane === "transport" && s.call === "run") || s.lane === "tools");
  if (!exchanges.length) {
    return op.column === "fake"
      ? `<div class="muted" style="font-size:16px">In process: the Fake has no tool to call and no text to parse.</div>`
      : `<div class="muted" style="font-size:16px">No command was needed${op.outcome?.raised ? "" : " (nothing to ask the resource about)"}.</div>`;
  }
  return exchanges.map((s) => {
    const argv = (s.argv ?? []).filter((a) => a !== "-debug").map(shorten).join(" ");
    const out = lines(s.stdout);
    const rules = op.name === "get_status" ? decided(s.stdout, ctx) : [];
    const err = s.returncode ? lines(s.stderr, 3) : "";
    return `<div class="exchange">
      <div class="mono cmd">$ ${ctx.escape(argv)} ${s.returncode !== undefined ? `<span class="exit ${s.returncode ? "bad" : ""}">exit ${s.returncode}</span>` : ""}</div>
      ${out ? `<pre class="code said">${op.name === "get_status" ? mark(ctx.escape(out)) : ctx.escape(out)}</pre>` : `<div class="muted" style="font-size:14px">(printed nothing)</div>`}
      ${rules.length ? `<div class="rules">${rules.join("")}</div>` : ""}
      ${err ? `<pre class="code said err">${ctx.escape(err)}</pre>` : ""}
      ${s.raised ? `<div class="exit bad">${ctx.escape(s.raised)}: ${ctx.escape(s.message)}</div>` : ""}
    </div>`;
  }).join("");
}

// -- which value decided the status, and the rule that turned it into one ---------------

/** Highlight the value a backend reads a job's state from: Slurm's state, HTCondor's JobStatus. */
function mark(html) {
  return html
    .replace(/^([^|\s]+\|)([A-Z_]+)/gm, '$1<mark>$2</mark>')
    .replace(/(&quot;(?:JobStatus|ExitCode|ExitBySignal)&quot;: )([^,\n]+)/g, '$1<mark>$2</mark>');
}

/** The rules, from the backends' own tables, behind the states in this output. */
function decided(text, ctx) {
  const maps = ctx.store.meta.mappings;
  const rules = new Set();
  for (const [, state] of (text ?? "").matchAll(/^[^|\s]+\|([A-Z_]+)/gm)) {
    rules.add(`<span class="rule"><mark>${ctx.escape(state)}</mark> → ${ctx.escape(maps.slurm[state] ?? "Unknown")}</span>`);
  }
  for (const [, code] of (text ?? "").matchAll(/"JobStatus": (\d+)/g)) {
    rules.add(maps.htcondor[code]
      ? `<span class="rule">JobStatus <mark>${code}</mark> → ${ctx.escape(maps.htcondor[code])}</span>`
      : code === "4"
        ? `<span class="rule" title="${ctx.escape(maps.htcondor_finished)}">JobStatus <mark>4</mark> (completed) → Done or Failed, by ExitCode</span>`
        : `<span class="rule">JobStatus <mark>${code}</mark> → by the backend's own rule (held, removed)</span>`);
  }
  return [...rules];
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
