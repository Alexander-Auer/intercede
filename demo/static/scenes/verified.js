// Scene 8: the integration CI, stacks × versions × configs, from a snapshot of real workflow runs.

const CLASS = { success: "Done", failure: "Failed", cancelled: "Aborted", skipped: "Unknown" };
let data = null;

export default {
  title: "Verified",
  caption: "Every backend is tested against the real system in CI, on every pull request, across the versions sites run.",
  sources: () => (data?.fetched_at ? [`SNAPSHOT ${data.fetched_at.slice(0, 10)}`] : []),

  async mount(el, ctx) {
    data = await ctx.api("/api/ci");
    if (data.missing) {
      el.innerHTML = `<div class="placeholder">No snapshot yet: run <code>pixi run -e demo python demo/scripts/fetch_ci.py</code></div>`;
      return;
    }
    const repos = Object.entries(data.repositories);
    el.innerHTML = `
      <div class="panel" style="overflow:auto">
        <div class="row" style="justify-content: space-between">
          <span class="muted">Workflow <code>${ctx.escape(data.workflow)}</code>: one job per stack × config × version in <code>tests/integration/stacks.yml</code></span>
          <span class="muted">snapshot taken ${ctx.escape(new Date(data.fetched_at).toLocaleString())}</span>
        </div>
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
          ${[["Done", "passed"], ["Failed", "failed"], ["Aborted", "cancelled"], ["none", "not in that run (the stack had not landed on that branch yet)"]].map(([s, t]) => `<span><span class="heat-cell" data-status="${s}"></span> ${t}</span>`).join("")}
        </div>
        ${data.not_ready.length ? `<p class="muted" style="font-size:16px">Listed in stacks.yml, not run yet (no stack folder): ${data.not_ready.map((s) => `<code>${ctx.escape(s)}</code>`).join(", ")}</p>` : ""}
      </div>`;
  },
};
