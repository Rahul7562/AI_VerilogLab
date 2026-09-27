/* Layered SVG schematic from yosys netlist.json. No dependencies. */
function outPortsOf(type, ports) {
  // yosys JSON carries no port directions; infer by convention.
  if (ports.includes("Y")) return ["Y"];
  const outs = ports.filter(p => p === "Q" || p === "QN");
  if (outs.length) return outs;
  return [];
}
function renderSchematic(el, j) {
  const mods = j.modules || {};
  const topName = Object.keys(mods)[0];
  if (!topName) { el.textContent = "empty netlist"; return "empty"; }
  const top = mods[topName];
  const cells = Object.entries(top.cells || {});
  const CAP = 150;
  const shown = cells.slice(0, CAP);
  // net -> driver cell (outputs inferred: Y, or Q/QN)
  const driverOf = {};
  for (const [cn, c] of shown) {
    const conns = c.connections || {};
    const outs = new Set(outPortsOf(c.type, Object.keys(conns)));
    for (const [port, nets] of Object.entries(conns)) {
      if (!outs.has(port)) continue;
      nets.forEach(n => { driverOf[n] = cn; });
    }
  }
  // depth by BFS from undriven nets
  const depth = {};
  let changed = true, guard = 0;
  while (changed && guard++ < 50) {
    changed = false;
    for (const [cn, c] of shown) {
      if (depth[cn] !== undefined) continue;
      const conns = c.connections || {};
      const outs = new Set(outPortsOf(c.type, Object.keys(conns)));
      let d = -1, ready = true;
      for (const [port, nets] of Object.entries(conns)) {
        if (outs.has(port)) continue;
        for (const n of nets) {
          const drv = driverOf[n];
          if (drv === undefined) continue; // primary input / const
          if (depth[drv] === undefined) { ready = false; break; }
          d = Math.max(d, depth[drv]);
        }
        if (!ready) break;
      }
      if (ready) { depth[cn] = d + 1; changed = true; }
    }
  }
  shown.forEach(([cn]) => { if (depth[cn] === undefined) depth[cn] = 0; });
  const layers = {};
  shown.forEach(([cn]) => { (layers[depth[cn]] = layers[depth[cn]] || []).push(cn); });
  const L = Object.keys(layers).length;
  const BW = 150, BH = 30, GX = 190, GY = 40, PAD = 30;
  const maxRows = Math.max(...Object.values(layers).map(a => a.length));
  const W = L * GX + 260, H = maxRows * GY + 90;
  const pos = {};
  Object.entries(layers).forEach(([d, arr]) => {
    arr.forEach((cn, i) => { pos[cn] = { x: 130 + (+d) * GX, y: 50 + i * GY }; });
  });
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  // edges
  for (const [cn, c] of shown) {
    const conns = c.connections || {};
    const outs = new Set(outPortsOf(c.type, Object.keys(conns)));
    for (const [port, nets] of Object.entries(conns)) {
      if (outs.has(port)) continue;
      for (const n of nets) {
        const drv = driverOf[n];
        if (drv === undefined || !pos[drv]) continue;
        const a = pos[drv], b = pos[cn];
        const p = document.createElementNS(NS, "path");
        const x0 = a.x + BW, y0 = a.y + BH / 2, x1 = b.x, y1 = b.y + BH / 2;
        p.setAttribute("d", `M${x0},${y0} C${x0 + 40},${y0} ${x1 - 40},${y1} ${x1},${y1}`);
        p.setAttribute("stroke", "#58a6ff"); p.setAttribute("fill", "none");
        p.setAttribute("stroke-width", "1.2");
        svg.appendChild(p);
      }
    }
  }
  for (const [cn, c] of shown) {
    const { x, y } = pos[cn];
    const g = document.createElementNS(NS, "g");
    const r = document.createElementNS(NS, "rect");
    r.setAttribute("x", x); r.setAttribute("y", y);
    r.setAttribute("width", BW); r.setAttribute("height", BH);
    r.setAttribute("rx", 5); r.setAttribute("fill", "#161d24");
    r.setAttribute("stroke", "#3fb950");
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", x + 8); t.setAttribute("y", y + 19);
    t.setAttribute("fill", "#e6e6e6"); t.setAttribute("font-size", "11");
    t.setAttribute("font-family", "monospace");
    t.textContent = `${c.type} ${cn}`.slice(0, 24);
    const title = document.createElementNS(NS, "title");
    title.textContent = `${c.type} ${cn}`;
    g.appendChild(r); g.appendChild(t); g.appendChild(title);
    svg.appendChild(g);
  }
  // module ports labels
  const pin = document.createElementNS(NS, "text");
  pin.setAttribute("x", 10); pin.setAttribute("y", 24);
  pin.setAttribute("fill", "#9aa7b2"); pin.setAttribute("font-size", "12");
  pin.textContent = `module ${topName} — inputs left, outputs right`;
  svg.appendChild(pin);
  el.innerHTML = "";
  el.appendChild(svg);
  return `${cells.length} cells${cells.length > CAP ? ` (first ${CAP} drawn)` : ""}`;
}
