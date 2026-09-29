# AI VerilogLab

Generate Verilog RTL from a one-line prompt, verify it by simulation,
synthesize it, and measure area / timing / power — from the CLI or a
localhost web dashboard.

```
prompt  ->  spec.json  ->  design.v + tb_<dut>.v  ->  iverilog  ->  wave.vcd
                                        ->  yosys+abc  ->  netlist.v
                                        ->  openSTA    ->  PPA.md
```

`auto` runs the whole loop unattended: it expands your prompt into a spec,
generates N independent candidates, repairs whatever fails, measures each
one, and publishes the best result with the evidence behind it.

---

## Setup

```bash
uv sync
cp .env.example .env          # add OPENROUTER_API_KEY (never commit .env)
uv run vlab check-tools
uv run vlab check-llm --probe
```

Python 3.10+. All data files (`pdks/generic.lib`) ship inside the wheel, so
an installed `vlab` resolves its liberty file the same way a source
checkout does.

## Commands

| Command | What it does |
|---|---|
| `check-tools` | EDA tool status; distinguishes required from optional |
| `check-llm [--probe]` | Validate the API key; `--probe` sweeps the model pool |
| `models [--probe]` | Show the model rotation order and live availability |
| `init --path P --dut D` | Scaffold a project (`vlab_project.yaml`, `runs/`) |
| `new` | Interactive or scripted DUT Q&A -> `spec.json` |
| `gen` | Generate `design.v` + testbench from the spec |
| `simulate` | `iverilog`/`verilator` -> `wave.vcd` + pass/fail |
| `fix` | LLM repair loop for a failing testbench or DUT |
| `synth` | `yosys`+`abc` -> `netlist.v`/`.json` + area |
| `ppa` | `openSTA` timing/power + `yosys` area -> `ppa.json` |
| `report` | Consolidate `PPA.md` |
| `run` | gen -> simulate -> synth -> ppa -> report |
| `auto` | The full autonomous loop (below) |
| `serve` | Localhost web dashboard |

```bash
uv run vlab init --path ./my_proj --dut cnt8
uv run vlab new   --project ./my_proj --dut cnt8 --desc "8-bit up/down counter"
uv run vlab gen   --project ./my_proj
uv run vlab simulate --project ./my_proj
uv run vlab fix   --project ./my_proj        # only on FAIL
uv run vlab synth --project ./my_proj
uv run vlab ppa   --project ./my_proj
uv run vlab report --project ./my_proj
```

Omit `--project` inside a project directory and vlab finds the project by
walking up to the nearest `vlab_project.yaml`.

## Autonomous mode

```bash
uv run vlab auto --project ./my_proj \
  --prompt "8-bit up/down counter with enable and synchronous active-low reset" \
  --dut cnt8 --candidates 2 --iterations 1
```

Phases: **DEFINE** (prompt -> validated spec, plus an LLM self-review for
internal contradictions) -> **GENERATE** (N candidates) -> **VERIFY**
(simulate, then repair) -> **SYNTHESIZE** -> **MEASURE PPA** ->
**SELECT+LEARN** (constraint check, Pareto frontier, `memory.jsonl`, LLM
guidance for the next generation) -> **OUTPUT** (`final/` plus
`COMPARISON.md`).

Useful flags: `--width`, `--iterations`, `--fix-rounds`, `--freq-mhz`,
`--no-clarify` (skip the clarification prompt), `--no-llm`.

### How a winner is chosen

Candidates that pass simulation are ranked by: feasible-under-limits
first, then lowest power, then shortest critical path, then fewest cells.
A missing measurement is treated as *unknown*, never as a violation.

Two guards protect against winning by cheating:

- **Repair routing.** A compile error is repaired in whichever file the
  compiler actually named. A testbench bug is fixed in the testbench; a DUT
  bug in the DUT. (Repairing the wrong file can never converge — the model
  just re-emits the unchanged file.)
- **Anti-weakening.** The first passing testbench is frozen as
  `golden_tb_<dut>.v`. If a candidate you *repaired* then passes fewer
  checks than that bar, it is rejected. Independent candidates are not
  penalised for having a different number of checks.

A repair that returns its input byte-for-byte (modulo comments/whitespace)
stops the loop immediately rather than burning the remaining rounds.

## Web dashboard

```bash
uv run vlab serve --project . --port 8000
# open http://127.0.0.1:8000
```

Under-specified prompts get a clarification form before anything is
generated. A 7-step progress bar (Define -> Generate -> Verify ->
Synthesize -> Measure PPA -> Select+Learn -> Output) streams tool logs and
a live candidate table, then results: metric cards, an in-browser waveform
rendered from `wave.vcd`, a netlist schematic auto-laid-out from
`netlist.json`, RTL/netlist source, `PPA.md`, `COMPARISON.md`, and
downloads for every artifact. Runs already on disk stay browsable by
session id. The API key stays server-side; the browser never sees it.

No auth — bind to `127.0.0.1` (the default) or `serve` exposes the
pipeline to anything that can reach the port.

## Offline / CI mode

```bash
uv run vlab new --project ./my_proj --non-interactive \
  --dut alu8 --desc "8-bit adder" --pdk auto --no-llm
uv run vlab auto --project ./my_proj --prompt "..." --no-llm
```

> **`--no-llm` does not implement your design.** It emits a generic
> registered-adder template whose ports are derived from the spec, so
> asking it for a counter gives you an adder named `cnt8`. It exists to
> exercise the toolchain in CI and tests — never treat its output as a
> solution.

## EDA tools

Debian/Ubuntu:

```bash
sudo apt install iverilog verilator build-essential yosys yosys-abc opensta
```

`iverilog` is the default simulator and needs nothing else. `verilator`'s
`--binary` mode shells out to `make` and a C++ compiler, so it also needs
`build-essential`; without them vlab says so instead of failing with an
opaque build error. GTKWave is optional (viewing `wave.vcd`).

Without root, vlab auto-detects a user-space toolchain in
`~/.local/edatools` (no `PATH`/`LD_LIBRARY_PATH` exports needed):

```bash
mkdir -p /tmp/edatools && cd /tmp/edatools
apt download iverilog yosys yosys-abc verilator opensta libtcl8.6
mkdir -p extract && for f in *.deb; do dpkg-deb -x "$f" extract/; done
cp -r extract/usr/bin/verilator* extract/usr/bin/sta ~/.local/edatools/usr/bin/
cp -r extract/usr/share/verilator ~/.local/edatools/usr/share/
```

`VLAB_EDA_ROOT` overrides the toolchain root. Per-tool overrides:
`VLAB_IVERILOG_BIN`, `VLAB_VVP_BIN`, `VLAB_VERILATOR_BIN`, `VLAB_YOSYS_BIN`,
`VLAB_STA_BIN`, `VLAB_ABC_BIN`, `VLAB_IVERILOG_BASE`.

## Models

Default: `poolside/laguna-s-2.1:free` (`OPENROUTER_MODEL` overrides).

**Free-tier rotation.** OpenRouter rate-limits the free tier *per model*,
and the popular free models are frequently saturated — a 429 on one model
says nothing about your key's budget. vlab therefore tries the primary
model first and, on a 429/5xx/"no endpoints available", rotates through a
fallback pool ordered by measured RTL-generation quality rather than
sleeping through the limit. Key-level problems (401 auth, 402 no credits)
are never masked: they surface immediately.

```bash
uv run vlab models --probe      # which models answer right now
```

| Variable | Default | Meaning |
|---|---|---|
| `VLAB_LLM_FALLBACKS` | built-in pool | Comma-separated; empty disables rotation |
| `VLAB_LLM_RETRIES` | `2` | Attempts **per model** before rotating |
| `VLAB_LLM_MIN_INTERVAL` | `3.0` | Client-side seconds between calls |
| `VLAB_REASONING_EFFORT` | `low` | `off`/`exclude`/`low`/`medium`/`high`/`tokens` |
| `VLAB_REASONING_MAX_TOKENS` | `512` | Only used with `VLAB_REASONING_EFFORT=tokens` |

> `VLAB_REASONING_EFFORT` sends **either** `effort` **or** `max_tokens`,
> never both — OpenRouter returns HTTP 400 for a request carrying both.
> Prefer `low`/`medium`/`high`; `exclude` asks the provider to strip
> reasoning, which some thinking models answer with empty content.

## PDKs

`pdks/registry.yaml` lists `sky130`, `nangate45`, `asap7`. The Q&A offers
`auto (recommended)`; the LLM picks from the spec and records the choice and
reason in `spec.json` (`PPA.md` shows both).

Mapping and STA use `pdks/generic.lib` — a demo library with basic cells, so
**absolute numbers are relative, not signoff**. Drop a real `<pdk>.lib` into
`pdks/` or pass `--lib /path/to.lib`; STA then reads the same liberty
synthesis mapped against, which is required for the timing numbers to mean
anything.

## PPA method

- **Area** — `yosys stat` cell/wire counts (`area.json`).
- **Timing** — `openSTA report_checks -path_delay max -group_count 50`
  against a generated SDC (clock + 20% input/output delay margins) ->
  WNS/TNS/crit in `ppa.json`. Data port lists are built by an explicit Tcl
  walk: OpenSTA 2.0.x evaluates `[all_inputs -no_clocks]` to an *empty* set
  and silently drops every input delay, which hides all input->register
  paths and makes a flip-flop->output hop look like the critical path.
  `fmax` is derived from the achievable period (target period minus WNS)
  rather than `1/crit_ns`, so it accounts for setup time and the I/O budget.
- **Power** — `openSTA report_power` with VCD-derived switching activity via
  `set_power_activity`, plus a VCD-toggle fallback. Bundled OpenSTA is
  2.0.x and has no `read_vcd`; a newer STA would use it directly.
- **Waveform** — `wave.vcd` (GTKWave-compatible). Netlist — `netlist.v` +
  `netlist.json`.

A purely combinational netlist has no startpoint->capture path, so
`crit_ns`/`wns_ns` come back `None` and `method` is `opensta-incomplete`.
A failed STA run is never labelled a successful `opensta` measurement.

## Troubleshooting

**Every candidate failed on an LLM call.** The free tier is rate-limited or
unreachable. `uv run vlab models --probe` shows which models are live;
rotation handles this automatically, and a paid `OPENROUTER_MODEL` removes
the constraint. If rotation is disabled, check `VLAB_LLM_FALLBACKS`.

**"empty model reply".** The model returned nothing usable — usually a
thinking model with `VLAB_REASONING_EFFORT=exclude`. Switch to `low`, or set
`tokens` to cap reasoning explicitly.

**A candidate failed to compile.** Read
`runs/<sid>__i<c>c<d>/sim.log`; the error names the file and line.

**A candidate passed simulation but you don't trust it.** Compare
`runs/<sid>/COMPARISON.md` and each candidate's `sim.log` for the real
`PASS`/`FAIL` lines. A testbench that makes no assertions is reported as a
failure, not a pass.

**verilator fails to build.** `make` and a C++ compiler are missing
(`sudo apt install build-essential`), or use `--tool iverilog`, which needs
neither.

## Layout

```
src/vlab/
  cli.py        command surface
  auto.py       autonomous loop (define -> ... -> output)
  ask.py        DUT Q&A -> spec.json
  spec.py       spec schema/validation, PDK auto-select
  rtl_gen.py    design.v + testbench generation, output sanity gates
  sim.py        iverilog / verilator -> wave.vcd + pass/fail
  fix.py        LLM repair loop, compile-error attribution
  synth.py      yosys + abc -> netlist, area
  ppa.py        openSTA timing/power, VCD activity
  report.py     PPA.md
  optimize.py   constraints, Pareto, ranking, search guidance
  llm.py        OpenRouter client, model rotation, retries
  web.py        dashboard API + SSE
  tools.py      EDA tool detection
  config.py     paths, env, tool resolution
  utils.py      subprocess helpers
  webui/static/ dashboard (index.html, app.js, wave.js, schematic.js)
pdks/           registry.yaml, generic.lib
tests/          pytest suite
runs/           per-session artifacts
```

Each session writes `runs/<dut>_<timestamp>/` containing `spec.json`,
`design.v`, `tb_*.v`, `sim.log`, `sim.json`, `wave.vcd`, `synth.ys`,
`netlist.v`, `netlist.json`, `area.json`, `design.sdc`, `sta.tcl`,
`timing.rpt`, `ppa.json`, `auto.json`, `COMPARISON.md`, and `final/` for the
selected design. `auto` candidates live beside it as
`runs/<sid>__i<iter>c<cand>/`.

## Tests

```bash
uv run pytest -q
```

The suite covers the compile-error attribution, PPA/SDC generation, model
rotation, constraint checking, port conformance, and web concurrency paths.
Tests that need iverilog/yosys skip cleanly when they are absent.
