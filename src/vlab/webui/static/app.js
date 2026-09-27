/* Dashboard controller: status, run submit, SSE progress/logs, results. */
let runId = null, es = null, poll = null, zoom = 1, waveData = null;

const $ = id => document.getElementById(id);

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

function setStep(step, done) {
  document.querySelectorAll("#steps li").forEach(li => {
    if (li.dataset.step === step) {
      li.classList.add(done ? "done" : "on");
      if (done) li.classList.remove("on");
    }
  });
}

async function refreshState() {
  if (!runId) return;
  try {
    const s = await (await api(`/api/runs/${runId}`)).json();
    const tb = document.querySelector("#candtab tbody");
    tb.innerHTML = (s.candidates || []).map(c =>
      `<tr><td>${c.id.split("__").pop()}</td><td>${c.status || "-"}</td>` +
      `<td>${c.pass_count ?? "-"}</td><td>${c.cells ?? "-"}</td>` +
      `<td>${c.crit_ns ?? "-"}</td><td>${c.power_w ?? "-"}</td></tr>`).join("");
  } catch (e) { /* run dir appears later */ }
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
  // files
  const names = ["final/design.v", "final/tb_cnt8.v", "final/wave.vcd", "final/netlist.v",
    "final/netlist.json", "final/PPA.md", "COMPARISON.md", "auto.json", "memory.jsonl",
    "final/sim.log", "final/synth.log", "spec.json"];
  const ul = $("files"); ul.innerHTML = "";
  for (const n of names) {
    if (await fileExists(n)) {
      const li = document.createElement("li");
      li.innerHTML = `<a href="/api/runs/${runId}/files/${n}" download>${n}</a>`;
      ul.appendChild(li);
    }
  }
  // tb file has dut-dependent name; discover via final listing fallback
  if (!ul.textContent.includes("final/tb_")) {
    for (const cand of ["final/tb_alu8.v", "final/tb_mydut.v"]) {
      if (await fileExists(cand)) {
        const li = document.createElement("li");
        li.innerHTML = `<a href="/api/runs/${runId}/files/${cand}" download>${cand}</a>`;
        ul.appendChild(li);
      }
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
  $("progsec").classList.remove("hidden");
  $("resec").classList.add("hidden");
  $("log").textContent = "";
  document.querySelectorAll("#steps li").forEach(li => li.classList.remove("on", "done"));
  $("runid").textContent = "run: " + runId;
  if (es) es.close();
  es = new EventSource(`/api/runs/${runId}/events`);
  es.onmessage = async ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "step") setStep(m.step, true);
    else if (m.type === "log") m.lines.forEach(l => logLine(m.source, l));
    else if (m.type === "done") { await refreshState(); finish(await loadResults()); }
    else if (m.type === "error") { logLine("error", m.message); await loadResults().catch(() => {}); finish(false); }
  };
  es.onerror = () => { /* server closes at end; keep polling once more */ };
  poll = setInterval(refreshState, 2000);
  refreshState();
}

function finish(ok) {
  $("start").disabled = false;
  if (poll) clearInterval(poll);
  if (es) es.close();
  if (ok) {
    document.querySelectorAll("#steps li").forEach(li => {
      if (!li.classList.contains("done")) li.classList.add("done");
    });
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
