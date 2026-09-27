# AI VerilogLab (Phase 1: CLI + backend)

AI-driven Verilog flow: autonomous DUT Q&A -> `design.v` + testbench ->
simulate (`iverilog`/`verilator`, `wave.vcd`) -> synth (`yosys`+`abc`,
netlist) -> PPA (`yosys` area, `openSTA` timing + power from VCD).

## M0 — repo + CLI skeleton (this milestone)

```bash
uv sync
uv run vlab --help
uv run vlab check-tools
uv run vlab check-llm   # needs OPENROUTER_API_KEY in env or .env
uv run vlab init --path ./my_proj
```

`vlab init` creates `vlab_project.yaml`, `sessions/`, `runs/` in the target dir.

## EDA tools (required for M3-M5, only checked in M0)

* `iverilog` + `vvp` (simulation, VCD)
* `verilator` (alt simulator)
* `yosys` + `abc` (synthesis, area via `stat`)
* `sta` (OpenSTA timing + power from VCD)
* `gtkwave` (optional waveform viewer)

Ubuntu example:
```bash
sudo apt install iverilog verilator yosys abc opensta gtkwave
```

## LLM (OpenRouter)

```bash
cp .env.example .env
# edit .env, set OPENROUTER_API_KEY (never commit .env)
export OPENROUTER_API_KEY=sk-or-v1-...
uv run vlab check-llm
```

Default model: `nvidia/nemotron-3-ultra-550b-a55b:free`.

## PDKs

See `pdks/registry.yaml`. Supported: `sky130`, `nangate45`, `asap7`.
Q&A offers `auto (recommended)` — AI picks the best optimized PDK from the spec
and records choice + reason in `spec.json`.

## PPA (locked)

* Area: `yosys stat`
* Timing: `openSTA`
* Power: `openSTA power` with switching activity from simulation VCD

## Commits

Every milestone is committed. No secrets in git history (`.env` is gitignored).
