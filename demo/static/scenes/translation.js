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
      <div style="display:grid; grid-template-columns: 400px minmax(0, 1fr); gap: 16px; align-items:start">
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
        <div id="t-targets" style="display:grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 16px; align-items:start"></div>
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
  const targets = Object.entries(result.targets ?? {});
  el.querySelector("#t-targets").innerHTML = targets.length
    ? targets.map(([, target]) => panel(target, ctx)).join("")
    : `<div class="panel muted">no specification</div>`;
}

function panel(target, ctx) {
  const head = `<h2 style="margin:0">${ctx.escape(target.title)} <span class="mono muted" style="font-size:15px">${ctx.escape(target.class)}</span></h2>`;
  if (target.raised) {
    return `<div class="panel">${head}<div class="refusal"><strong>${ctx.escape(target.raised)}</strong> at submit, before anything is sent<pre>${ctx.escape(target.message)}</pre></div></div>`;
  }
  const shown = new Set();
  const steps = target.steps.map((s) => step(s, shown, ctx)).join("");
  const stopped = target.stopped
    ? `<div class="muted" style="font-size:14px; margin-top:8px">The recorder answers with nothing, so submit() stopped at the first answer it needed: <span class="mono">${ctx.escape(target.stopped)}</span></div>`
    : "";
  return `<div class="panel">${head}${steps}${stopped}</div>`;
}

/** What one step sent, with the files or bodies that went with it; each text once. */
function step(s, shown, ctx) {
  const line = s.sent ?? (s.argv ? `$ ${s.argv.join(" ")}` : `${s.lane} · ${s.call}`);
  const files = (s.files ?? []).filter((f) => !(f.text && shown.has(f.text)));
  for (const f of files) if (f.text) shown.add(f.text);
  const texts = files.filter((f) => f.text).map((f) => `<div class="mono" style="font-size:14px">${ctx.escape(f.name)}</div><pre class="code dialect">${ctx.escape(f.text)}</pre>`).join("");
  const unshown = files.filter((f) => !f.text).map((f) => `<code>${ctx.escape(f.name)}</code>`).join(" ");
  return `<div class="muted mono" style="font-size:14px; margin-top:8px; overflow-wrap:anywhere">${ctx.escape(line)}</div>
    ${texts}${unshown ? `<div class="muted" style="font-size:14px">${unshown} (contents not shown)</div>` : ""}`;
}
