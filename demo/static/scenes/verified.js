// Scene 9: the integration CI, stacks × versions × configs, from real workflow runs on GitHub.
// "Check the latest CI runs" reads GitHub through the server, which holds the token.

const CLASS = {
  success: "Done", failure: "Failed", cancelled: "Aborted", timed_out: "Failed", skipped: "Unknown",
  in_progress: "Running", queued: "Waiting", waiting: "Waiting", pending: "Waiting", requested: "Waiting",
};
const WORDS = { Done: "passed", Failed: "failed", Aborted: "cancelled", Running: "running", Waiting: "queued", Unknown: "skipped" };
const AUTO_S = 60;

let auto = null; // the timer of the automatic check, while this scene is shown and it is on

export default {
  title: "Verified",
  caption: "Every backend is tested against the real system in CI, on every pull request, across the versions sites run.",
  sources: (store) => (store.ci?.fetched_at ? [`SNAPSHOT ${time(store.ci.fetched_at)}`] : []),

  async mount(el, ctx) {
    ctx.store.ci ??= await ctx.api("/api/ci");
    el.innerHTML = `
      <div class="panel row" style="margin-bottom:14px">
        <button class="primary" id="ci-check">Check the latest CI runs</button>
        <label class="row" style="gap:6px; font-size:17px"><input type="checkbox" id="ci-auto" ${auto ? "checked" : ""}> every ${AUTO_S} s while this scene is shown</label>
        <span class="muted" id="ci-status" style="font-size:17px"></span>
      </div>
      <div id="ci-latest" class="latest"></div>
      <div id="ci-heat"></div>`;
    el.querySelector("#ci-check").addEventListener("click", () => check(ctx));
    el.querySelector("#ci-auto").addEventListener("change", (e) => {
      stopAuto();
      if (e.target.checked) auto = setInterval(() => check(ctx), AUTO_S * 1000);
    });
    this.update(ctx);
  },

  unmount() {
    stopAuto();
  },

  update(ctx) {
    const el = document.getElementById("stage");
    if (!el.querySelector("#ci-heat")) return;
    const status = ctx.store.ciStatus;
    el.querySelector("#ci-check").disabled = status?.state === "checking" || ctx.store.replaying;
    el.querySelector("#ci-status").textContent = {
      checking: "asking GitHub…",
      failed: status?.message ?? "",
      done: `checked at ${time(ctx.store.ci?.fetched_at)}`,
    }[status?.state] ?? (ctx.store.ci?.fetched_at ? `snapshot of ${new Date(ctx.store.ci.fetched_at).toLocaleString()}` : "");
    const data = ctx.store.ci;
    if (!data || data.missing) {
      el.querySelector("#ci-heat").innerHTML = `<div class="placeholder" style="height:auto; padding:40px">No snapshot yet: press the button above.</div>`;
      el.querySelector("#ci-latest").innerHTML = "";
      return;
    }
    el.querySelector("#ci-latest").innerHTML = Object.entries(data.repositories).map(([repo, runs]) => latest(repo, runs[0], data.axes, ctx)).join("");
    el.querySelector("#ci-heat").innerHTML = heat(data, ctx);
  },
};

async function check(ctx) {
  try {
    await ctx.api("/api/ci/refresh", {});
  } catch (err) {
    ctx.toast(err.message.replace(/^.*?: \d+ /, ""), "warning");
  }
}

function stopAuto() {
  if (auto) clearInterval(auto);
  auto = null;
}

function time(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function ago(iso) {
  const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

function latest(repo, run, axes, ctx) {
  if (!run) return "";
  const state = CLASS[run.conclusion] ?? "Unknown";
  const jobs = axes.filter((axis) => run.jobs[axis]);
  return `<div class="panel latest-run">
    <div class="muted" style="font-size:16px">${ctx.escape(repo)} · latest run</div>
    <div class="row" style="gap:10px; margin: 4px 0 8px">
      <span class="tile" data-status="${state}" style="min-height:0; cursor:default"><span class="st">${WORDS[state] ?? ctx.escape(run.conclusion)}</span></span>
      <a href="${ctx.escape(run.url)}" target="_blank" rel="noopener" style="font-size:22px; font-weight:700">#${run.number}</a>
      <span class="mono" style="font-size:16px">${ctx.escape(run.branch)}</span>
      <span class="muted" style="font-size:15px">${ctx.escape(run.event)} · ${ago(run.created)}</span>
    </div>
    <div class="row" style="gap:6px">${jobs.map((axis) => {
      const s = CLASS[run.jobs[axis]] ?? "Unknown";
      return `<span class="tile" data-status="${s}" style="min-height:0; cursor:default; font-size:14px"><span class="mono">${ctx.escape(axis)}</span> ${WORDS[s] ?? ctx.escape(run.jobs[axis])}</span>`;
    }).join("") || `<span class="muted">none of the stacks ran in this run</span>`}</div>
  </div>`;
}

function heat(data, ctx) {
  const repos = Object.entries(data.repositories);
  return `<div class="panel" style="overflow:auto">
    <div class="muted">Workflow <code>${ctx.escape(data.workflow)}</code>: one job per stack × config × version in <code>tests/integration/stacks.yml</code>, newest run first</div>
    <table class="heat">
      <thead>
        <tr><th></th>${repos.map(([repo, runs]) => `<th colspan="${runs.length}" class="repo">${ctx.escape(repo)}</th>`).join("")}</tr>
        <tr><th></th>${repos.map(([, runs]) => runs.map((run) => `<th class="run" title="${ctx.escape(run.branch)}\n${ctx.escape(run.created)}"><a href="${ctx.escape(run.url)}" target="_blank" rel="noopener">#${run.number}</a></th>`).join("")).join("")}</tr>
      </thead>
      <tbody>
        ${data.axes.map((axis) => `<tr><th class="axis mono">${ctx.escape(axis)}</th>${repos.map(([, runs]) => runs.map((run) => {
          const conclusion = run.jobs[axis];
          return `<td><span class="heat-cell" data-status="${CLASS[conclusion] ?? "none"}" title="${ctx.escape(axis)} in run #${run.number} (${ctx.escape(run.branch)}): ${ctx.escape(conclusion ?? "not part of this run")}"></span></td>`;
        }).join("")).join("")}</tr>`).join("")}
      </tbody>
    </table>
    <div class="row" style="margin-top:12px; font-size:16px">
      ${[["Done", "passed"], ["Failed", "failed"], ["Aborted", "cancelled"], ["Running", "running now"], ["none", "not in that run (the stack had not landed on that branch yet)"]].map(([s, t]) => `<span><span class="heat-cell" data-status="${s}"></span> ${t}</span>`).join("")}
    </div>
    ${data.not_ready.length ? `<p class="muted" style="font-size:16px">Listed in stacks.yml, not run yet (no stack folder): ${data.not_ready.map((s) => `<code>${ctx.escape(s)}</code>`).join(", ")}</p>` : ""}
  </div>`;
}
