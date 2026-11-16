// Scene 1: the layers, read from the code. Clicking a box shows its docstring.

let data = null;
let open = null; // {title, doc}

export default {
  title: "Architecture",
  caption: "One contract between the WMS and every resource; everything below it is interchangeable.",
  sources: () => ["FROM CODE"],

  async mount(el, ctx) {
    data ??= await ctx.api("/api/architecture");
    const essential = data.contract.filter((p) => p.essential);
    const optional = data.contract.filter((p) => !p.essential);
    const families = { monolithic: [], composed: [], provider: [] };
    for (const b of data.backends) families[b.family].push(b);
    const composition = await ctx.api("/api/composition");
    const op = (p) => `<button class="box op ${p.essential ? "essential" : "optional"}" data-doc="${p.protocol}">
        <span class="mono">${ctx.escape(p.methods[0].name)}()</span><small>${ctx.escape(p.protocol)}</small></button>`;
    const backend = (b) => `<button class="box ${b.implemented ? "" : "planned"}" data-doc="kind:${b.kind}">
        <span class="mono">${ctx.escape(b.kind)}</span><small>${b.implemented ? ctx.escape(b.class) : `planned · ${ctx.escape(b.config)}`}</small></button>`;
    el.innerHTML = `
      <div class="arch">
        <div class="layer consumer">
          <button class="box wide" data-doc="wms"><strong>WMS: DiracX</strong><small>the consumer: builds specs, stores handles, decides when to poll</small></button>
          <button class="box side" data-doc="provider"><strong>CredentialProvider</strong><small>the consumer's own; interCEde asks it when a credential is missing or expiring</small></button>
        </div>
        <div class="down">▼</div>
        <div class="layer contract">
          <div class="layer-title">The contract <span class="muted">essential, every backend</span></div>
          <div class="ops">${essential.map(op).join("")}</div>
          <div class="layer-title">optional capabilities <span class="muted">checked with isinstance()</span></div>
          <div class="ops">${optional.map(op).join("")}</div>
        </div>
        <div class="down">▼ <span class="mono" style="font-size:18px">registry.get_backend(config, credentials)</span></div>
        <div class="layer"><button class="box wide" data-doc="registry"><strong>Registry</strong><small>a closed table; imports only what the configuration asks for</small></button></div>
        <div class="down">▼</div>
        <div class="families">
          <div class="layer"><div class="layer-title">Monolithic <span class="muted">a gateway that schedules by itself</span></div>${families.monolithic.map(backend).join("")}</div>
          <div class="layer"><div class="layer-title">Composed <span class="muted">transport × scheduler</span></div>
            ${families.composed.map(backend).join("")}
            <div class="row" style="gap:6px; margin-top:8px">${composition.transports.filter((t) => !t.variant).map((t) => `<button class="box small ${t.class ? "" : "planned"}" data-doc="transport:${t.transport}">${ctx.escape(t.transport)}</button>`).join("")}
              <span style="font-size:22px">×</span>
              ${composition.schedulers.map((s) => `<button class="box small ${s.class ? "" : "planned"}" data-doc="scheduler:${s.scheduler}">${ctx.escape(s.scheduler)}</button>`).join("")}</div>
          </div>
          <div class="layer"><div class="layer-title">Provider <span class="muted">no queue</span></div>${families.provider.map(backend).join("")}</div>
        </div>
        <div class="panel doc" id="doc"></div>
      </div>`;
    el.querySelector(".arch").addEventListener("click", (e) => {
      const key = e.target.closest("[data-doc]")?.dataset.doc;
      if (!key) return;
      open = lookup(key, composition);
      draw(el, ctx);
    });
    draw(el, ctx);
  },
};

function lookup(key, composition) {
  if (key === "wms") return { title: "The consumer", doc: "DiracX, or any other WMS. It never names a backend class: it passes configuration and its credential provider to the registry, and from then on only the protocols." };
  if (key === "provider") return { title: "CredentialProvider", doc: data.provider.doc };
  if (key === "registry") return { title: "intercede.registry", doc: `${data.registry.doc}\n\nget_backend:\n${data.registry.get_backend}` };
  const protocol = data.contract.find((p) => p.protocol === key);
  if (protocol) return { title: protocol.protocol, doc: `${protocol.doc}\n\n${protocol.methods.map((m) => `${m.signature}\n\n${m.doc}`).join("\n\n")}` };
  const [group, name] = key.split(":");
  if (group === "kind") {
    const b = data.backends.find((x) => x.kind === name);
    return { title: b.class ?? `${b.config} (planned)`, doc: b.doc };
  }
  if (group === "transport") {
    const t = composition.transports.find((x) => x.transport === name && !x.variant);
    return { title: t.class ?? `${name} (planned)`, doc: t.doc || "Planned: the configuration accepts it, the registry has no class for it yet." };
  }
  const s = composition.schedulers.find((x) => x.scheduler === name);
  return { title: s.class ?? `${name} (planned)`, doc: s.doc || "Planned: the configuration accepts it, the registry has no class for it yet." };
}

function draw(el, ctx) {
  el.querySelector("#doc").innerHTML = open
    ? `<strong class="mono">${ctx.escape(open.title)}</strong><pre style="margin-top:8px; font-size:17px">${ctx.escape(open.doc)}</pre>`
    : `<span class="muted">Click a box to read its docstring, straight from the code.</span>`;
}
