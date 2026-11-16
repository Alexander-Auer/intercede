// Scene 7: partial failure, reported per job; only a failure of the whole operation raises.

let column = "fake";

export default {
  title: "Partial failure",
  caption: "At grid scale partial failure is normal: the contract reports it per job, and raises only for the whole operation.",

  sources(store) {
    const scenario = store.scenario;
    const shown = store.columns.find((c) => c.id === (scenario?.column ?? column));
    return shown ? [shown.source] : [];
  },

  mount(el, ctx) {
    el.innerHTML = `
      <div style="display:grid; grid-template-columns: 360px 1fr; gap: 18px">
        <div class="panel" style="display:grid; gap:10px; align-content:start">
          <strong>On the Fake backend's test hooks</strong>
          <div id="fake-scenarios" style="display:grid; gap:6px"></div>
          <strong style="margin-top:10px">On any backend</strong>
          <label class="row">Backend <select id="column-pick" style="font-size:18px; padding:4px 8px; border-radius:8px"></select></label>
          <div id="any-scenarios" style="display:grid; gap:6px"></div>
        </div>
        <div class="panel" id="scenario" style="min-height: 60vh"></div>
      </div>`;
    const buttons = (needs) => ctx.store.meta.scenarios.filter((s) => s.needs === needs)
      .map((s) => `<button class="ghost" data-scenario="${s.name}" data-needs="${s.needs}" style="text-align:left">${ctx.escape(s.title)}</button>`).join("");
    el.querySelector("#fake-scenarios").innerHTML = buttons("fake");
    el.querySelector("#any-scenarios").innerHTML = buttons("any");
    el.querySelector("#column-pick").addEventListener("change", (e) => { column = e.target.value; });
    el.addEventListener("click", async (e) => {
      const button = e.target.closest("[data-scenario]");
      if (!button) return;
      const target = button.dataset.needs === "fake" ? "fake" : column;
      await ctx.api("/api/scenario", { name: button.dataset.scenario, column: target }).catch((err) => ctx.toast(err.message));
    });
    this.update(ctx);
  },

  update(ctx) {
    const el = document.getElementById("stage");
    const pick = el.querySelector("#column-pick");
    if (!pick) return;
    const online = ctx.store.columns.filter((c) => c.state === "online");
    if (!online.some((c) => c.id === column)) column = online[0]?.id ?? "fake";
    const options = online.map((c) => `<option value="${c.id}" ${c.id === column ? "selected" : ""}>${ctx.escape(c.title)}</option>`).join("");
    if (pick.dataset.html !== options) { pick.innerHTML = options; pick.dataset.html = options; }
    drawScenario(el.querySelector("#scenario"), ctx);
  },
};

function drawScenario(box, ctx) {
  const scenario = ctx.store.scenario;
  if (!scenario) {
    box.innerHTML = `<div class="placeholder" style="height:auto; padding: 60px 0">Run a scenario on the left.</div>`;
    return;
  }
  const meta = ctx.store.meta.scenarios.find((s) => s.name === scenario.name);
  const title = ctx.store.columns.find((c) => c.id === scenario.column)?.title ?? scenario.column;
  box.innerHTML = `<h2 style="margin:0 0 12px">${ctx.escape(meta?.title ?? scenario.name)} <span class="muted" style="font-size:20px">on ${ctx.escape(title)}</span>
      ${scenario.done ? "" : `<span class="muted" style="font-size:18px"> · running…</span>`}</h2>
    ${scenario.steps.map((step, i) => `<div style="margin: 0 0 16px">
      <div style="font-size:20px; ${step.error ? "color: var(--st-failed)" : ""}">${i + 1}. ${ctx.escape(step.title)}</div>
      ${detail(step, ctx)}
    </div>`).join("")}`;
}

function detail(step, ctx) {
  if (step.raised) {
    return `<div style="margin-top:6px; padding:10px 14px; border-radius:10px; background: var(--st-failed); color:#fff; font: 600 18px var(--mono)">raise ${ctx.escape(step.raised)}</div>`;
  }
  if (step.submission) {
    const tiles = [
      ...Object.entries(step.submission.handles).map(([i, id]) => [Number(i), outcomeTile(`copy ${i}`, true, `handle ${id}`, ctx)]),
      ...Object.entries(step.submission.failures).map(([i, why]) => [Number(i), outcomeTile(`copy ${i}`, false, why, ctx)]),
    ].sort((a, b) => a[0] - b[0]);
    return `<div class="tiles" style="margin-top:8px">${tiles.map(([, html]) => html).join("")}</div>`;
  }
  if (step.outcomes) {
    return `<div class="tiles" style="margin-top:8px">${Object.entries(step.outcomes).map(([id, o]) => outcomeTile(id, o.ok, o.reason, ctx)).join("")}</div>`;
  }
  if (step.statuses) {
    return `<div class="tiles" style="margin-top:8px">${Object.entries(step.statuses).map(([id, s]) =>
      `<div class="tile" data-status="${ctx.escape(s)}" style="cursor:default"><div class="id">${ctx.escape(id)}</div><div class="st">${ctx.escape(s)}</div></div>`).join("")}</div>`;
  }
  return "";
}

function outcomeTile(id, ok, reason, ctx) {
  return `<div class="tile" data-status="${ok ? "Done" : "Failed"}" style="cursor:default">
    <div class="id">${ctx.escape(id)}</div><div class="st">${ok ? "✓ ok" : "✗ not ok"}</div>
    ${reason ? `<div class="why">${ctx.escape(reason)}</div>` : ""}</div>`;
}
