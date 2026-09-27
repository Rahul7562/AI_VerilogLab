/* Dashboard controller: status, run submit, SSE progress/logs, results. */
let runId = null, es = null, poll = null, zoom = 1, waveData = null;
let lastSeen = 0, stepTimes = {}, runOver = false, ticker = null;
const STALL_S = 240;

const $ = id => document.getElementById(id);

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

function setStatus(mode, text) {
  $("statedot").className = mode;
  $("statetext").textContent = text;
}

function tickAgo() {
  if (!lastSeen) return;
  const s = Math.floor(Date.now() / 1000 - lastSeen);
  const stalled = !runOver && s > STALL_S;
  $("ago").textContent = stalled
    ? `no updates for ${Math.floor(s / 60)}m — long model calls can take minutes`
    : `updated ${s}s ago`;
  if (stalled) setStatus("stalled", "stalled?");
  else if (!runOver) setStatus("running", "running");
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r;
}

async function loadStatus() {
  try {
    const s = await (await api("/api/status")).json();
    $("ver").textContent = "v" + s.version;
    const missing = s.tools.filter(t => !t.found).map(t => t.binary);
    $("tools").textContent = missing.length ? "missing: " + missing.join(", ") : "tools OK";
    $("llm").textContent = s.llm_configured ? "LLM ready" : "LLM key missing (.env)";
    if (!s.llm_configured) $("usellm").checked = false;
  } catch (e) { $("tools").textContent = "server error"; }
}

function logLine(src, text) {
  const el = $("log");
  el.textContent += `[${src}] ${text}\n`;
  el.scrollTop = el.scrollHeight;
}

function setStep(step, done, at) {
  document.querySelectorAll("#steps li").forEach(li => {
    if (li.dataset.step === step) {
      li.classList.add(done ? "done" : "on");
      if (done) li.classList.remove("on");
      if (at) {
        let sm = li.querySelector("small");
        if (!sm) { sm = document.createElement("small"); li.appendChild(sm); }
        sm.textContent = fmtTime(at);
      }
    }
  });
  const order = ["define", "generate", "verify", "synthesize", "ppa", "select", "done"];
  const labels = { define: "Define", generate: "Generate", verify: "Verify", synthesize: "Synthesize", ppa: "Measure PPA", select: "Select+Learn", done: "Output" };
  const idx = order.indexOf(step);
  const next = done ? order[idx + 1] : step;
  $("stage").textContent = next && labels[next]
    ? `stage: ${labels[next]}${done ? "" : " (in progress)"}`
    : "stage: done";
}

async function refreshState() {
  if (!runId) return;
  try {
    const s = await (await api(`/api/runs/${runId}`)).json();
    const tb = document.querySelector("#candtab tbody");
    tb.innerHTML = (s.candidates || []).map(c =>
      `<tr><td>${c.id.split("__").pop()}</td><td>${c.status || "pending"}</td>` +
      `<td>${c.pass_count ?? "-"}</td><td>${c.cells ?? "-"}</td>` +
      `<td>${c.crit_ns ?? "-"}</td><td>${c.power_w ?? "-"}</td></tr>`).join("");
    renderIssues(s.candidates || []);
    if (s.meta && s.meta.status === "error") {
      setStatus("error", "error: " + (s.meta.detail || "").slice(0, 120));
    }
  } catch (e) { /* run dir appears later */ }
}

function renderIssues(cands) {
  const box = $("issues");
  const rows = [];
  for (const c of cands) {
    const id = (c.id || "").split("__").pop();
    if (c.status === "error" || c.status === "fail") {
      rows.push(`<div class="err"><b>${id} ${c.status}</b>` +
        (c.note ? `: ${c.note}` : " — see sim.log") + `</div>`);
    } else if ((c.fail_count || 0) > 0) {
      rows.push(`<div class="warn"><b>${id}</b>: ${c.fail_count} failing check(s), ` +
        `${c.pass_count || 0} passing</div>`);
    }
    for (const w of (c.warnings || [])) {
      rows.push(`<div class="warn"><b>${id}</b>: ${w}</div>`);
    }
  }
  box.innerHTML = rows.length ? rows.join("") : '<span class="dim">none yet</span>';
}

async function fileExists(p) {
  try { const r = await fetch(`/api/runs/${runId}/files/${p}`); return r.ok; }
  catch (e) { return false; }
}
async function fileText(p) {
  const r = await api(`/api/runs/${runId}/files/${p}`);
  return await r.text();
}

function mdToHtml(md) {
  return md.split("\n").map(l => {
    if (l.startsWith("### ")) return `<h3>${l.slice(4)}</h3>`;
    if (l.startsWith("## ")) return `<h2>${l.slice(3)}</h2>`;
    if (l.startsWith("# ")) return `<h1>${l.slice(2)}</h1>`;
    if (/^[-*] /.test(l)) return `<li>${l.slice(2)}</li>`;
    return l.replace(/`([^`]+)`/g, "<code>$1</code>") + "<br>";
  }).join("\n");
}

async function loadResults() {
  $("resec").classList.remove("hidden");
  const st = await (await api(`/api/runs/${runId}`)).json();
  const base = st.winner ? "" : "";
  const fail = $("failnote");
  if (!st.winner) {
    const det = (st.candidates || []).map(c =>
      `${c.id.split("__").pop()}: ${c.status || "?"}`).join(", ");
    fail.textContent =
      `No passing candidate (${det}). Check the log above and per-candidate ` +
      `sim.log files, then re-run with more candidates, more fix rounds, or a tighter prompt.`;
    fail.classList.remove("hidden");
  } else {
    fail.classList.add("hidden");
  }
  // metric cards from winner candidate dir = final/
  let ppa = {};
  try { ppa = JSON.parse(await fileText("final/ppa.json")); } catch (e) {}
  const area = ppa.area || {}, timing = ppa.timing || {}, power = ppa.power || {};
  $("winner").textContent = st.winner ? `· winner ${st.winner.split("__").pop()}` : "";
  $("cards").innerHTML = [
    [area.cell_count ?? "-", "cells (yosys)"],
    [timing.wns_ns ?? "-", "WNS ns (openSTA)"],
    [timing.crit_ns ?? "-", "crit ns"],
    [power.power_w ?? "-", "power W"],
  ].map(([v, l]) => `<div class="metric"><b>${v}</b><span>${l}</span></div>`).join("");
  // waveform
  try {
    const vcd = await fileText("final/wave.vcd");
    waveData = parseVCD(vcd);
    drawWave();
  } catch (e) { $("winfo").textContent = "no wave.vcd"; }
  // schematic + rtl
  try {
    const nj = JSON.parse(await fileText("final/netlist.json"));
    const info = renderSchematic($("schem"), nj);
    $("schem").insertAdjacentHTML("afterbegin", `<div style="padding:6px;font-size:12px;color:#9aa7b2">${info}</div>`);
  } catch (e) { $("schem").textContent = "no netlist.json"; }
  try { $("design").textContent = await fileText("final/design.v"); } catch (e) {}
  try { $("netlist").textContent = (await fileText("final/netlist.v")).slice(0, 20000); } catch (e) {}
  try { $("ppa").innerHTML = mdToHtml(await fileText("final/PPA.md")); } catch (e) {}
  try { $("comp").innerHTML = mdToHtml(await fileText("COMPARISON.md")); } catch (e) {}
  // files (server listing first: covers any DUT name + per-candidate logs)
  const ul = $("files"); ul.innerHTML = "";
  const names = new Set();
  try {
    const listing = await (await api(`/api/runs/${runId}/files`)).json();
    for (const f of (listing.files || [])) {
      if (/\.(v|sv|vcd|json|md|log|rpt|tcl|sdc|ys|txt)$/.test(f)
          && !f.includes("sim.vvp")) names.add(f);
    }
  } catch (e) { /* fall back to known names */ }
  for (const n of ["final/design.v", "final/wave.vcd", "final/netlist.v",
      "final/netlist.json", "final/PPA.md", "COMPARISON.md", "auto.json",
      "memory.jsonl", "spec.json"]) names.add(n);
  for (const n of names) {
    if (await fileExists(n)) {
      const li = document.createElement("li");
      li.innerHTML = `<a href="/api/runs/${runId}/files/${n}" download>${n}</a>`;
      ul.appendChild(li);
    }
  }
  return !!st.winner;
}

function drawWave() {
  if (!waveData) return;
  $("winfo").textContent = renderWave($("wave"), waveData, { zoom });
}

async function start() {
  const numOrNull = id => {
    const v = $(id).value.trim();
    return v === "" ? null : +v;
  };
  const body = {
    prompt: $("prompt").value.trim(),
    dut: $("dut").value.trim() || null,
    width: numOrNull("width"),
    candidates: numOrNull("candidates"),
    iterations: +$("iterations").value || 1,
    fix_rounds: 2,
    use_llm: $("usellm").checked,
    freq_mhz: 100.0,
  };
  if (body.prompt.length < 3) { alert("Enter a design prompt first"); return; }
  $("start").disabled = true;
  $("clarify").classList.add("hidden");
  const r = await (await api("/api/runs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })).json();
  if (r.needs_input) { showQuestions(r.ticket, r.questions); $("start").disabled = false; return; }
  beginRun(r.run_id);
}

function showQuestions(ticket, questions) {
  const box = $("clarify");
  box.classList.remove("hidden");
  const ql = $("qlist");
  ql.innerHTML = "";
  questions.forEach((q, i) => {
    const div = document.createElement("div");
    div.innerHTML = `<label>${q}</label><br>`;
    const inp = document.createElement("input");
    inp.style.width = "100%";
    inp.id = "qa" + i;
    div.appendChild(inp);
    ql.appendChild(div);
  });
  $("answer").onclick = async () => {
    const answers = questions.map((_, i) => $("qa" + i).value || "");
    const r = await (await api("/api/runs/answer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket, answers }),
    })).json();
    box.classList.add("hidden");
    beginRun(r.run_id);
  };
}

function beginRun(run_id) {
  runId = run_id;
  lastSeen = Date.now() / 1000;
  stepTimes = {};
  runOver = false;
  setStatus("running", "running");
  if (ticker) clearInterval(ticker);
  ticker = setInterval(tickAgo, 1000);
  $("progsec").classList.remove("hidden");
  $("resec").classList.add("hidden");
  $("log").textContent = "";
  document.querySelectorAll("#steps li").forEach(li => li.classList.remove("on", "done"));
  $("runid").textContent = "run: " + runId;
  if (es) es.close();
  es = new EventSource(`/api/runs/${runId}/events`);
  es.onmessage = async ev => {
    const m = JSON.parse(ev.data);
    lastSeen = Date.now() / 1000;
    if (!runOver) setStatus("running", "running");
    if (m.type === "step") { stepTimes[m.step] = m.at; setStep(m.step, true, m.at); }
    else if (m.type === "log") m.lines.forEach(l => logLine(m.source, l));
    else if (m.type === "done") {
      runOver = true;
      await refreshState();
      let ok = false;
      try { ok = await loadResults(); }
      catch (e) { showFatal("Could not load results: " + e.message); }
      finish(ok);
    }
    else if (m.type === "error") {
      runOver = true;
      logLine("error", m.message);
      try { await loadResults(); } catch (e) { showFatal(String(e.message || e)); }
      finish(false);
    }
  };
  es.onerror = () => {
    if (runOver) return; // normal close at end of stream
    showFatal("Lost server connection — retrying automatically. " +
      "Refresh the page if this persists; completed artifacts stay on disk.");
  };
  poll = setInterval(refreshState, 2000);
  refreshState();
}

function showFatal(msg) {
  $("resec").classList.remove("hidden");
  const fail = $("failnote");
  fail.textContent = msg;
  fail.classList.remove("hidden");
}

function finish(ok) {
  runOver = true;
  if (ticker) clearInterval(ticker);
  $("start").disabled = false;
  if (poll) clearInterval(poll);
  if (es) es.close();
  if (ok) {
    setStatus("done", "finished");
    document.querySelectorAll("#steps li").forEach(li => {
      if (!li.classList.contains("done")) li.classList.add("done");
    });
  } else {
    setStatus("error", "finished with failures");
  }
}

document.querySelectorAll(".tabs button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(x => x.classList.remove("on"));
    document.querySelectorAll(".tab").forEach(x => x.classList.add("hidden"));
    b.classList.add("on");
    $(b.dataset.tab).classList.remove("hidden");
    if (b.dataset.tab === "t-wave") drawWave();
  };
});
$("zin").onclick = () => { zoom *= 1.5; drawWave(); };
$("zout").onclick = () => { zoom /= 1.5; drawWave(); };
$("start").onclick = start;
loadStatus();
