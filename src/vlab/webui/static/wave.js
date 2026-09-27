/* Minimal VCD parser + canvas waveform renderer. No dependencies. */
function parseVCD(text) {
  const id2sig = {};
  const order = [];
  let t = 0, timescale = "";
  const lines = text.split(/\r?\n/);
  let inDump = false;
  const changes = {}; // id -> [{t, v}]
  const push = (id, v) => {
    (changes[id] = changes[id] || []).push({ t, v });
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("$timescale")) { timescale = line; continue; }
    if (line.startsWith("$var ")) {
      const m = line.match(/\$var\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)/);
      if (m && !id2sig[m[2]]) { id2sig[m[2]] = { width: +m[1], name: m[3] }; order.push(m[2]); }
      continue;
    }
    if (line.startsWith("$dumpvars") || line.startsWith("$dumpon")) { inDump = true; continue; }
    if (line.startsWith("$dumpoff") || line.startsWith("$end")) { inDump = false; continue; }
    if (line[0] === "#") { t = parseInt(line.slice(1), 10) || 0; continue; }
    if (line[0] === "b" || line[0] === "B") {
      const [v, id] = line.slice(1).split(/\s+/);
      if (id2sig[id]) push(id, v);
    } else if ("01xXzZ".includes(line[0]) && line.length > 1) {
      const id = line.slice(1).split(/\s+/)[0];
      if (id2sig[id]) push(id, line[0]);
    }
  }
  const sigs = order.slice(0, 64).map(id => ({ id, ...id2sig[id], changes: changes[id] || [{ t: 0, v: "x" }] }));
  const tmax = Math.max(t, 1);
  return { sigs, tmax, timescale, truncated: order.length > 64, total: order.length };
}

function valueAt(ch, t) {
  let v = ch[0].v;
  for (const c of ch) { if (c.t > t) break; v = c.v; }
  return v;
}

function renderWave(canvas, wv, opts) {
  const zoom = (opts && opts.zoom) || 1;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 900, rowH = 22, nameW = 170;
  const H = wv.sigs.length * rowH + 24;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  ctx.font = "11px monospace";
  const tmax = wv.tmax * zoom;
  const X = tt => nameW + (tt / tmax) * (W - nameW - 10);
  wv.sigs.forEach((s, i) => {
    const y = 24 + i * rowH;
    ctx.fillStyle = "#9aa7b2";
    ctx.fillText(`${s.name}${s.width > 1 ? `[${s.width - 1}:0]` : ""}`, 6, y - 6);
    // gridline
    ctx.strokeStyle = "#1c242d"; ctx.beginPath(); ctx.moveTo(nameW, y - rowH + 4); ctx.lineTo(W - 4, y - rowH + 4); ctx.stroke();
    if (s.width === 1) {
      ctx.strokeStyle = "#3fb950"; ctx.beginPath();
      let started = false;
      const pts = [[0, valueAt(s.changes, 0)]];
      for (const c of s.changes) pts.push([c.t, c.v]);
      pts.push([wv.tmax, valueAt(s.changes, wv.tmax)]);
      pts.forEach(([tt, v], k) => {
        const x = X(tt / zoom), yy = (v === "1") ? y - 14 : y - 4;
        if (!started) { ctx.moveTo(x, yy); started = true; }
        else {
          const px = X(pts[k - 1][0] / zoom), py = (pts[k - 1][1] === "1") ? y - 14 : y - 4;
          ctx.lineTo(x, py); ctx.lineTo(x, yy);
        }
      });
      ctx.stroke();
    } else {
      // bus: boxes on change
      const ch = [...s.changes].sort((a, b) => a.t - b.t);
      ch.push({ t: wv.tmax, v: valueAt(s.changes, wv.tmax) });
      for (let k = 0; k < ch.length - 1; k++) {
        const x0 = X(ch[k].t / zoom), x1 = X(ch[k + 1].t / zoom);
        const bad = /[xXzZ]/.test(ch[k].v);
        ctx.strokeStyle = bad ? "#f85149" : "#3fb950";
        ctx.strokeRect(x0, y - 14, Math.max(2, x1 - x0), 10);
        if (x1 - x0 > 34) { ctx.fillStyle = "#e6e6e6"; ctx.fillText(ch[k].v.slice(0, 10), x0 + 3, y - 6); }
      }
    }
  });
  return `${wv.total} signals${wv.truncated ? " (first 64 shown)" : ""} · tmax=${wv.tmax} ${wv.timescale.replace(/\$timescale|\$end/g, "").trim()}`;
}
