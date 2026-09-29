# AI VerilogLab (Phase 1: CLI + backend)

AI-driven Verilog flow: autonomous DUT Q&A -> `design.v` + testbench (Nemotron
via OpenRouter) -> simulate (`iverilog`/`verilator`, `wave.vcd`) ->
synth (`yosys`+`abc`, netlist) -> PPA (`yosys` area, `openSTA` timing +
VCD-derived power) -> `PPA.md`.

## Setup

```bash
uv sync
uv run vlab --help
uv run vlab check-tools
cp .env.example .env   # set OPENROUTER_API_KEY (never commit .env)
uv run vlab check-llm
```

Default model: `poolside/laguna-s-2.1:free`
(`OPENROUTER_MODEL` overrides).

**Free-tier model rotation.** OpenRouter's free tier rate-limits *per model*,
and the popular free models are frequently saturated. vlab tries the primary
model first and, on a 429/5xx/"no endpoints available" error, rotates through a
fallback pool rather than sleeping through the limit. Key-level problems
(401 auth, 402 no credits) are never masked by rotation — they surface
immediately.

```bash
uv run vlab models               # show the rotation order
uv run vlab models --probe       # live-test which models answer right now
uv run vlab check-llm --probe    # same sweep, inside the health check
```

Set `VLAB_LLM_FALLBACKS=a:free,b:free` to override the pool, or
`VLAB_LLM_FALLBACKS=` (empty) to disable rotation.

Other knobs (`.env` or environment):

| Variable | Default | Meaning |
|---|---|---|
| `VLAB_LLM_RETRIES` | `2` | attempts **per model** before rotating |
| `VLAB_LLM_MIN_INTERVAL` | `3.0` | client-side seconds between calls |
| `VLAB_REASONING_EFFORT` | `low` | `off` / `exclude` / `low` / `medium` / `high` / `tokens` |
| `VLAB_REASONING_MAX_TOKENS` | `512` | only used with `VLAB_REASONING_EFFORT=tokens` |

> `VLAB_REASONING_EFFORT` sends either `effort` **or** `max_tokens`, never
> both — OpenRouter rejects a request carrying both with HTTP 400.

## EDA tools

Debian/Ubuntu with root:

```bash
sudo apt install iverilog verilator yosys yosys-abc opensta
```

Without root, `vlab` auto-detects a user-space toolchain in
`~/.local/edatools` (no `PATH`/`LD_LIBRARY_PATH` exports needed).
To (re)provision it:

```bash
mkdir -p /tmp/edatools && cd /tmp/edatools
apt download iverilog yosys yosys-abc verilator opensta libtcl8.6
mkdir -p extract && for f in *.deb; do dpkg-deb -x "$f" extract/; done
cp -r extract/usr/bin/verilator* extract/usr/bin/sta ~/.local/edatools/usr/bin/
cp -r extract/usr/share/verilator ~/.local/edatools/usr/share/
```

`VLAB_EDA_ROOT` overrides the toolchain root. Finer overrides:
`VLAB_IVERILOG_BIN`, `VLAB_VVP_BIN`, `VLAB_VERILATOR_BIN`,
`VLAB_YOSYS_BIN`, `VLAB_STA_BIN`, `VLAB_ABC_BIN`, `VLAB_IVERILOG_BASE`.
Bundled OpenSTA is 2.0.x: no `read_vcd`, so power uses VCD-derived switching
activity via `set_power_activity` + `report_power` (method `opensta+vcd-activity`).

`iverilog` is the default simulator and is the only one that needs nothing
extra. `verilator` is an alternative but its `--binary` mode shells out to
`make` and a C++ compiler, so it needs `build-essential`; without them vlab
says so instead of failing with an opaque build error.

## Flow

```bash
uv run vlab init --path ./my_proj --dut cnt8
uv run vlab new --project ./my_proj            # interactive Q&A, PDK auto-select
uv run vlab gen --project ./my_proj            # LLM design.v + tb
uv run vlab simulate --project ./my_proj       # iverilog -> wave.vcd
uv run vlab fix --project ./my_proj            # LLM testbench repair loop (on FAIL)
uv run vlab synth --project ./my_proj          # yosys+abc -> netlist + area
uv run vlab ppa --project ./my_proj            # openSTA timing/power
uv run vlab report --project ./my_proj         # PPA.md
uv run vlab run --project ./my_proj            # full pipeline in one go
```

## Autonomous mode (one prompt in, results out)

```bash
uv run vlab auto --project ./my_proj \
  --prompt "8-bit up/down counter with enable and sync reset" \
  --dut cnt8 --candidates 2 --iterations 1
```

`auto` runs DEFINE (LLM expands the prompt to `spec.json`) → GENERATE
(N candidates) → VERIFY (sim + `fix` repair of the working TB) → SYNTHESIZE
→ MEASURE PPA → SELECT+LEARN (constraint check, pareto frontier,
`memory.jsonl`, LLM search guidance for the next generation) → OUTPUT
(`final/` with winning RTL + TB + netlist + `PPA.md`, plus `COMPARISON.md`).

Correctness guard: the first passing testbench is frozen as
`golden_tb_<dut>.v`, and repairs that drop below its PASS count are rejected,
so optimization cannot win by weakening the test.

## Web dashboard (localhost GUI)

```bash
uv run vlab serve --project ./my_proj --port 8000
# open http://127.0.0.1:8000
```

Enter a prompt → 7-step progress bar (Define → Generate → Verify →
Synthesize → Measure PPA → Select+Learn → Output) with realtime tool logs,
a live candidate table, then results: metric cards, in-browser waveform
rendered from `wave.vcd`, auto-laid-out netlist schematic from `netlist.json`,
RTL/netlist source, `PPA.md` + comparison, and file downloads. API key stays
server-side in `.env`; the browser never sees it.

Non-interactive (CI/tests):

```bash
uv run vlab new --project ./my_proj --non-interactive --dut alu8 \
  --desc "8-bit adder" --pdk auto --no-llm
uv run vlab gen --project ./my_proj --no-llm   # offline template RTL
```

## PDKs

`pdks/registry.yaml`: `sky130`, `nangate45`, `asap7`.
Q&A offers `auto (recommended)` — Nemotron picks from the spec and records
choice + reason in `spec.json`. Synthesis/STA use `pdks/generic.lib`
(demo liberty with basic cells — numbers are relative, not signoff;
drop a real `<pdk>.lib` into `pdks/` or pass `--lib` for real mapping).

## PPA method (locked)

* Area: `yosys stat` cell/wire counts (`area.json`).
* Timing: `openSTA report_checks -path_delay max -group_count 50` with a
  generated SDC (clock + 20% input/output delay margins) -> WNS/TNS/crit
  (`ppa.json`). Data-input/output port lists are built by an explicit Tcl
  walk, because OpenSTA 2.0.x evaluates `[all_inputs -no_clocks]` to an
  *empty* set and silently drops every input delay — which hid all
  input->register paths and made a flip-flop->output hop look like the
  critical path. `fmax` comes from the achievable period (target period
  minus WNS), not `1/crit_ns`, so it accounts for setup and the I/O budget.
* Power: `openSTA report_power` with VCD-derived activity
  (`set_power_activity`), plus VCD toggle fallback. STA always reads the
  same liberty synthesis mapped against.
* Waveform: `wave.vcd` (GTKWave-compatible). Netlist: `netlist.v` + `netlist.json`.

A purely combinational netlist has no startpoint->capture path: `crit_ns`
and `wns_ns` come back `None` and `method` is `opensta-incomplete`.

## Troubleshooting

**Every candidate failed on an LLM call.** The free tier is rate-limited or
unreachable. `vlab models --probe` shows which models are answering right
now; vlab rotates the pool automatically, and a paid `OPENROUTER_MODEL`
removes the constraint.

**A candidate failed with a compile error.** Read
`runs/<sid>__i<c>c<d>/sim.log`. vlab blames the file iverilog named in the
error, so a testbench bug is repaired in the testbench and a DUT bug in the
DUT. A repair that returns the file unchanged stops immediately instead of
burning the remaining rounds.

## Layout

```
src/vlab/{cli,ask,spec,rtl_gen,sim,synth,ppa,report,fix,llm,tools,config,utils}.py
pdks/{registry.yaml,generic.lib}  tests/  runs/  sessions/
```

Every run lives in `runs/<dut>_<timestamp>/`:
`spec.json design.v tb_*.v sim.log wave.vcd synth.ys netlist.v netlist.json
area.json design.sdc sta.tcl timing.rpt ppa.json PPA.md`.
