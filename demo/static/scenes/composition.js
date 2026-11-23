// Scene 2: transports × schedulers. Every filled cell is a working backend without a class of its own.

export default {
  title: "Composition",
  caption: "Every filled cell is a working backend, and none of them has a class of its own.",
  sources: () => ["FROM CODE"],

  async mount(el, ctx) {
    const data = await ctx.api("/api/composition");
    const label = { verified: "✓ verified in CI", implemented: "implemented", planned: "planned" };
    const cell = (c) => `<td class="cell ${c.state}">${label[c.state]}${c.stacks.length ? `<small class="mono">${c.stacks.map(ctx.escape).join(", ")}</small>` : ""}</td>`;
    el.innerHTML = `
      <div style="display:grid; grid-template-columns: minmax(0, 2.2fr) minmax(0, 1fr); gap: 22px; align-items:start">
        <div class="panel">
          <div class="layer-title">${ctx.escape(data.composed_class)} <span class="muted">= one transport × one scheduler, chosen by configuration</span></div>
          <table class="grid">
            <thead><tr><th></th>${data.schedulers.map((s) => `<th>${ctx.escape(s.scheduler)}<small class="mono">${ctx.escape(s.class ?? "no class yet")}</small></th>`).join("")}</tr></thead>
            <tbody>${data.transports.map((t) => `<tr><th>${ctx.escape(t.label)}<small class="mono">${ctx.escape(t.class ?? "no class yet")}</small></th>${t.cells.map(cell).join("")}</tr>`).join("")}</tbody>
          </table>
          <p class="muted" style="font-size:17px; margin: 12px 0 0">
            ${data.transports.length} transport rows and ${data.schedulers.length} schedulers: N + M classes, not N × M.
            A new scheduler fills a whole column at once.</p>
        </div>
        <div class="panel">
          <div class="layer-title">Backends of their own</div>
          ${data.others.map((o) => `<div class="cell ${o.state}" style="display:block; margin: 8px 0; padding: 10px 14px; border-radius: 10px">
              <strong class="mono">${ctx.escape(o.kind)}</strong> <span class="muted">${ctx.escape(o.family)}</span><br>
              ${label[o.state]}${o.stacks.length ? ` <small class="mono">${o.stacks.map(ctx.escape).join(", ")}</small>` : ""}</div>`).join("")}
          ${data.listed_without_folder.length ? `<p class="muted" style="font-size:15px">In stacks.yml without a stack folder yet: ${data.listed_without_folder.map((s) => `<code>${ctx.escape(s)}</code>`).join(", ")}</p>` : ""}
          <p class="muted" style="font-size:15px">Read from the configuration models, the registry's tables and tests/integration/stacks.yml.</p>
        </div>
      </div>`;
  },
};
