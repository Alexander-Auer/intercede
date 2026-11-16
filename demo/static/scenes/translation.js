// Scene 3: one SubmissionSpec, and what each backend turns it into (a dry run of the real submit()).

const PRESETS = {
  plain: { title: "A job", form: { executable: "python3", arguments: "-c\nprint('hello')", environment: "SITE=demo", copies: 3, copy_variable: "COPY_INDEX", secret: "pilot.secret", resources: { cpus: 2, memory_mb: 2000, wall_time_s: 3600 } } },
  container: { title: "In a container", form: { executable: "python3", arguments: "-c\nprint('hello')", container: "docker://almalinux:9", copies: 2 } },
  url: { title: "URL input", form: { executable: "python3", inputs: "events.root=https://example.org/events.root", copies: 1 } },
  glob: { title: "Outputs by pattern", form: { executable: "python3", outputs: "result-*.txt", copies: 1 } },
  duplicate: { title: "Two inputs, one name", form: { executable: "python3", inputs: "data=/etc/hosts\ndata=/etc/hostname", copies: 1 } },
  clash: { title: "Secret named like an input", form: { executable: "python3", inputs: "pilot.secret=/etc/hosts", secret: "pilot.secret", copies: 1 } },
};

let form = structuredClone(PRESETS.plain.form);
let launcher = false;
let result = null;
let timer = null;

const FIELDS = [
  ["executable", "Executable", "input"],
  ["arguments", "Arguments, one per line", "textarea"],
  ["environment", "Environment, NAME=value per line", "textarea"],
  ["inputs", "Inputs, name=source per line", "textarea"],
  ["outputs", "Outputs, pattern [URL] per line", "textarea"],
  ["container", "Container image", "input"],
];

export default {
  title: "Translation",
  caption: "One specification, many dialects: the real submit() of each backend, with the resource replaced by a recorder.",
  sources: () => ["DRY RUN"],

  mount(el, ctx) {
    el.innerHTML = `
      <div style="display:grid; grid-template-columns: 400px minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items:start">
        <div class="panel spec-form">
          <div class="row" style="gap:6px">${Object.entries(PRESETS).map(([k, p]) => `<button class="ghost small-btn" data-preset="${k}">${ctx.escape(p.title)}</button>`).join("")}</div>
          ${FIELDS.map(([name, label, kind]) => `<label>${label}${kind === "textarea" ? `<textarea data-field="${name}" rows="2"></textarea>` : `<input data-field="${name}">`}</label>`).join("")}
          <div class="row" style="gap:8px">
            <label>cpus <input data-res="cpus" type="number" min="1" style="width:70px"></label>
            <label>memory MB <input data-res="memory_mb" type="number" min="1" style="width:90px"></label>
            <label>wall s <input data-res="wall_time_s" type="number" min="1" style="width:90px"></label>
          </div>
          <div class="row" style="gap:8px">
            <label>copies <input data-field="copies" type="number" min="1" max="20" style="width:70px"></label>
            <label>per copy <input data-field="copy_variable" placeholder="variable" style="width:120px"></label>
            <label>secret <input data-field="secret" placeholder="file name" style="width:120px"></label>
          </div>
          <label class="row" style="gap:8px"><input type="checkbox" id="launcher"> the site configures a container_launcher</label>
          <div id="spec-error"></div>
        </div>
        <div class="panel" id="t-htcondor-ce"></div>
        <div class="panel" id="t-slurm"></div>
      </div>`;
    el.addEventListener("click", (e) => {
      const key = e.target.closest("[data-preset]")?.dataset.preset;
      if (!key) return;
      form = structuredClone(PRESETS[key].form);
      fill(el);
      translate(el, ctx);
    });
    el.addEventListener("input", (e) => {
      const field = e.target.dataset.field;
      const res = e.target.dataset.res;
      if (e.target.id === "launcher") launcher = e.target.checked;
      else if (field) form[field] = e.target.value;
      else if (res) form.resources = { ...(form.resources ?? {}), [res]: e.target.value ? Number(e.target.value) : "" };
      clearTimeout(timer);
      timer = setTimeout(() => translate(el, ctx), 300);
    });
    fill(el);
    if (result) draw(el, ctx); else translate(el, ctx);
  },
};

function fill(el) {
  for (const input of el.querySelectorAll("[data-field]")) input.value = form[input.dataset.field] ?? "";
  for (const input of el.querySelectorAll("[data-res]")) input.value = form.resources?.[input.dataset.res] ?? "";
  el.querySelector("#launcher").checked = launcher;
}

async function translate(el, ctx) {
  try {
    result = await ctx.api("/api/translate", { form, launcher });
  } catch (err) {
    ctx.toast(err.message);
    return;
  }
  draw(el, ctx);
}

function draw(el, ctx) {
  el.querySelector("#spec-error").innerHTML = result.spec_error
    ? `<div class="refusal"><strong>ValidationError</strong> before any backend sees it<pre>${ctx.escape(result.spec_error.join("\n"))}</pre></div>`
    : "";
  for (const [name, title] of [["htcondor-ce", "HTCondor-CE"], ["slurm", "Slurm (any transport)"]]) {
    const box = el.querySelector(`#t-${name}`);
    const target = result.targets?.[name];
    if (!target) { box.innerHTML = `<h2 style="margin:0">${title}</h2><p class="muted">no specification</p>`; continue; }
    const head = `<h2 style="margin:0">${title} <span class="mono muted" style="font-size:15px">${ctx.escape(target.class)}</span></h2>`;
    if (target.raised) {
      box.innerHTML = `${head}<div class="refusal"><strong>${ctx.escape(target.raised)}</strong> at submit, before anything runs<pre>${ctx.escape(target.message)}</pre></div>`;
      continue;
    }
    box.innerHTML = `${head}<div class="muted" style="font-size:16px; margin: 4px 0 8px">${target.handles} copies, one submission</div>${target.steps.map((s) => step(s, ctx)).join("")}`;
  }
}

function step(s, ctx) {
  if (s.call === "condor_submit") {
    const file = s.files?.[0];
    return `<div class="muted" style="font-size:15px">$ ${ctx.escape(s.argv.filter((a) => a !== "-debug").map((a) => a.endsWith(".sub") ? "<description>" : a).join(" "))}</div>
      <pre class="code dialect">${ctx.escape(file?.text ?? "")}</pre>`;
  }
  if (s.call === "put") {
    return `<div class="muted" style="font-size:15px; margin-top:8px">staged with the transport, then:</div>
      ${s.files.filter((f) => f.text && f.name !== "job.sh").map((f) => `<div class="mono" style="font-size:14px">${ctx.escape(f.name)}</div><pre class="code dialect">${ctx.escape(f.text)}</pre>`).join("")}
      <div class="muted" style="font-size:14px">${s.files.filter((f) => !f.text).map((f) => `<code>${ctx.escape(f.name)}</code>`).join(" ")} (contents not shown)</div>`;
  }
  if (s.call === "submit_command") {
    return `<div class="muted mono" style="font-size:14px; overflow-wrap:anywhere">$ ${ctx.escape(s.argv.join(" "))}</div>
      ${(s.files ?? []).map((f) => `<div class="mono" style="font-size:14px">${ctx.escape(f.name)}</div><pre class="code dialect">${ctx.escape(f.text)}</pre>`).join("")}`;
  }
  return "";
}
