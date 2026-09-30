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
| `rel` | Measure a prompt's real win rate over N runs |
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

### It retries instead of giving up, and it learns

If a whole round produces zero passing candidates, the run does not return
"no winner". It escalates: each extra round widens the search (more
candidates) and the concrete failures from the previous rounds are fed into
the next generation prompt, so attempt *n+1* knows what attempts *1..n* got
wrong instead of repeating them. Per-attempt success on a hard FSM is well
under 50%, so stopping after one failed round discards most of the available
probability.

Escalation is bounded by `VLAB_ESCALATION_ROUNDS` (default `2`, `0` restores
the old give-up-after-`--iterations` behaviour). A round that produces a
winner is never escalated past — that would multiply API spend for no
reliability gain. The requested `--iterations` still means "keep exploring for
a better point on the frontier", which is independent of escalation.

Escalation is also bounded by wall clock. A round is `candidates` generations
plus up to `--fix-rounds` repair calls each, so the worst case is long;
`VLAB_TIME_BUDGET_S` (default `2400`, `0` disables) stops escalation adding
candidates once it is spent, and `auto.json` records `timed_out` and
`elapsed_s` so a run that ended on the clock explains itself instead of
looking like the search simply gave up.

### How a winner is chosen

Candidates that pass simulation are ranked by: feasible-under-limits
first, then lowest power, then shortest critical path, then fewest cells.
A missing measurement is treated as *unknown*, never as a violation.

A candidate is only published as a winner if it clears three bars, not one:
simulation passes, **at least `max(5, len(features))` checks actually ran**,
and the spec-coverage audit finds no feature the DUT gets wrong. The check
floor matters: `sim._parse_result` accepts any run with one PASS, and a
candidate was measured shipping as a "winner" with a single assertion.

A coverage gap of kind `unimplemented` or `incorrect` blocks publication — the
design violates the spec. A gap of kind `unchecked` does not: the testbench is
merely not exhaustive, and rejecting there threw away candidates that had
already passed 24–50 real checks.

Two guards protect against winning by cheating:

- **Repair routing.** A compile error is repaired in whichever file the
  compiler actually named. For a *runtime* failure the culprit is decided
  against the **spec**, not against the other file: the model is asked which
  single file is wrong given the spec, the DUT, the testbench and the failing
  checks. Repairing the wrong file can never converge, and always blaming the
  testbench is worse than useless — it converges a spec-violating DUT with a
  testbench rewritten to match it and publishes the pair.
- **Anti-weakening.** The first passing testbench is frozen as
  `golden_tb_<dut>.v`. If a candidate you *repaired* then passes fewer
  checks than that bar, it is rejected. Independent candidates are not
  penalised for having a different number of checks.
- **Spec coverage.** Passing simulation only proves the DUT agrees with the
  testbench. Before a candidate can be published it is audited against the
  spec's `features`: a feature that is unimplemented, or implemented but
  never actually checked, is reported as a gap and sent back through repair
  rather than shipped. (Without this, a design can pass 19/19 checks using
  one shared counter for every phase while the spec demanded four different
  durations.)

A repair that returns its input byte-for-byte (modulo comments/whitespace
and the sanitizer's own injected lines) stops the loop immediately rather
than burning the remaining rounds.

### What is repaired without asking the model

These are mechanical, semantics-preserving fixes applied before a candidate is
simulated, because spending an LLM repair round on a one-word slip is slow,
costs rate-limit budget, and does not reliably work:

| Fix | Symptom it prevents |
|---|---|
| missing `` `timescale `` backtick | `design.v:1: syntax error` |
| `output wire` promoted to `output reg` | `x is not a valid l-value` (18 errors from one modelling slip) |
| task-call arity padded to match the declaration | `error: Missing argument N of call to task` |
| simulator watchdog appended | a bare `wait` that never completes hangs until the tool timeout, leaving a log with no PASS and no FAIL |
| white-box testbench flagged | a testbench reading `dut.state` is tied to one internal encoding and never converges |
| narrow counter flagged | `reg [1:0] count` cannot hold a 10-cycle phase, so the design can never satisfy the spec |

The last two are *not* auto-fixed — a testbench cannot be mechanically
rewritten into a black-box check, and widening a counter is a design change.
They are detected, reported in `gen.json` warnings, and fed into the repair
prompt as the diagnosed cause, because a repair round told "the counter is
too narrow" converges in one step while the same round told only
`FAIL: north green` can burn all of them rediscovering the same thing.

The watchdog budget is derived from the testbench's own `` `timescale ``, so
it cannot fire during a healthy run.

### Measuring, not just waiting

For a phase/timer check the prompts steer the testbench to a free-running
monitor that timestamps state transitions and records each state's duration,
rather than waiting a hard-coded number of cycles. Both matter: a
transition-wait alone is immune to off-by-one but cannot tell a correct
design from one whose phases are all 5x too long, and measuring each phase
with a separate task silently loses the cycle in which the handover happened,
making every phase after the first read one cycle short.

## Measuring reliability

A single green run says nothing about reliability. Before trusting a workflow
on a new design, measure it:

```bash
uv run vlab rel "8-bit up/down counter with synchronous reset" --runs 5
```

Each attempt is an independent full `auto` run. The command reports the win
rate, how many rounds each attempt needed, and the recorded reason for every
failure — so a prompt's difficulty is a number instead of an impression, and
a regression shows up as a falling rate rather than one unlucky run.

Reliability is a property of the prompt, not just the tool. A design with
timed phases, a handshake, or several interacting modes needs the generated
testbench to agree with the generated DUT about exact cycle boundaries, and
that is much harder than a combinational block. `rel` tells you which is
which.

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

Default: `stealth/space-bunny-alpha` (`OPENROUTER_MODEL` overrides) — free,
1M context, and verified to support `reasoning_effort`.

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
| `VLAB_LLM_RPS` | `10` | Hard ceiling on requests/sec; the interval grows to respect it |
| `VLAB_LLM_429_COOLDOWN` | `2` | Consecutive 429s before abandoning a saturated model |
| `VLAB_REASONING_EFFORT` | `low` | `off`/`exclude`/`low`/`medium`/`high`/`tokens` |
| `VLAB_REASONING_MAX_TOKENS` | `512` | Only used with `VLAB_REASONING_EFFORT=tokens` |
| `VLAB_GEN_MAX_TOKENS` | `16384` | Budget for one design+testbench reply |
| `VLAB_GEN_ATTEMPTS` | `3` | Re-asks before a candidate is abandoned |
| `VLAB_LLM_MAX_TOKENS_CAP` | `32768` | Ceiling for the automatic length-cutoff boost |
| `VLAB_SYNTH_TIMEOUT` | `600` | Seconds before yosys is killed |
| `VLAB_ESCALATION_ROUNDS` | `2` | Extra rounds after a round where every candidate failed (`0` disables) |
| `VLAB_TIME_BUDGET_S` | `2400` | Wall-clock seconds before escalation stops widening the search (`0` disables) |

> `VLAB_REASONING_EFFORT` sends **either** `effort` **or** `max_tokens`,
> never both — OpenRouter returns HTTP 400 for a request carrying both.
> Prefer `low`/`medium`/`high`; `exclude` asks the provider to strip
> reasoning, which some thinking models answer with empty content.

### Rate-limit guard rails

OpenRouter answers anything above a model's request-rate ceiling with `429`
rather than queueing, so a burst does not get absorbed — it gets rejected.
Three defences:

- **Rate ceiling.** `VLAB_LLM_RPS` (default `10`, under the 15 RPS free-tier
  limit) sets a hard ceiling; the call interval grows to respect it. A garbage
  value falls back to a safe default rather than disabling the guard.
- **Per-model *and* global gates.** The throttle tracks the last call per
  model as well as globally, because parallel candidates all call the same
  primary model. With only a global timestamp, N candidates issued calls N
  times too close together and a whole run stalled with no output.
- **Rotate out of saturation.** `VLAB_LLM_429_COOLDOWN` (default `2`)
  consecutive 429s abandons that model for the call instead of re-hitting the
  same wall with the same cadence.

Measure the real ceiling rather than guessing:

```bash
uv run vlab models --probe   # live availability of every model right now
```

### Truncated replies

A reply cut off by the token limit arrives with `finish_reason: length` and
is **partial JSON**, not an empty one. vlab treats that as truncation
wherever it appears and retries with a larger budget rather than handing the
fragment to the parser, and it recovers partial replies as a last resort.
This matters more than it sounds: the DUT and its testbench travel as one
JSON string, so every newline costs an escape and a non-trivial pair runs
~7.6k tokens. Under a 4k budget that is not an occasional failure but a
guaranteed one, and it surfaces as `Could not parse LLM output`.

### Model quirks vlab handles for you

Deterministic sanitizers run on generated output, so a model slip costs a
warning rather than a failed candidate:

- **Missing backtick on `timescale`.** A bare `timescale 1ns/1ps` is an
  undefined identifier, not a directive, and iverilog rejects the file with
  `design.v:1: syntax error`. Both `design.v` and the testbench are repaired
  automatically; the repair is recorded in `gen.json` warnings.
- **`output wire` assigned inside `always`.** A bare `output` defaults to
  `wire`, which is not a procedural l-value, so iverilog reports
  `x is not a valid l-value` for every assignment. Fixed mechanically; the
  repair is recorded in `gen.json` warnings.
- **Task called with the wrong number of arguments.** Verilog does not check
  arity at the call site, so this only appears at elaboration and kills the
  candidate. The call is padded or trimmed to match the declaration.
- **Unparsable JSON or a wrong module name** — the reply is retried with the
  reason it was rejected, up to `VLAB_GEN_ATTEMPTS` times, before the
  candidate is marked failed.
- **Ports the model invented** are rejected against the spec, in both the
  design header and the testbench.

Two defects are caught because they look like a normal failing check but
cannot be repaired into a passing one, and both are reported in `gen.json`:

- **A testbench that reads DUT internals.** `while (dut.state !== 3'd2)`
  ties the check to one state encoding. The model is free to number its FSM
  states any way it likes, so a testbench whose own `S_PED` constant happens
  to differ waits forever. The testbench must observe output ports only.
- **A counter too narrow for the spec's durations.** A generated
  `reg [1:0] count; reg [1:0] NORTH_LIMIT;` cannot represent a 10-cycle
  phase — it wraps at 3 — so the design is impossible from the start and the
  testbench can never agree with it. Counters need `ceil(log2(max+1))` bits;
  8 is cheap in area and removes the failure class.

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
