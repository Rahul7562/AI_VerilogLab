"""vlab CLI: init, Q&A, gen, simulate, fix, synth, ppa, run, report, auto."""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__, config
from .llm import check_llm
from .tools import check_tools

app = typer.Typer(help="AI VerilogLab — RTL gen, sim, synth, PPA.")
console = Console()


def _fail(msg: str) -> None:
    """Print a clean error and exit 1 — never a traceback."""
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(code=1)


def _spec_or_exit(project: str | None, session: str | None) -> tuple:
    """resolve_project_dir + find_spec, with friendly failures."""
    pdir = config.resolve_project_dir(project)
    try:
        return (pdir, *config.find_spec(pdir, session))
    except FileNotFoundError as e:
        _fail(str(e))
        raise  # unreachable, keeps type checkers happy


def _run_or_fail(fn, *args, **kwargs):
    """Run a pipeline step, turning any failure into a clean CLI error."""
    try:
        return fn(*args, **kwargs)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        _fail(f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 - never surface a traceback
        _fail(f"{type(e).__name__}: {e}")


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Show version."),
):
    if version:
        console.print(f"vlab {__version__}")
        raise typer.Exit()


@app.command("check-tools")
def check_tools_cmd():
    """Check EDA binaries (iverilog, verilator, make, yosys, abc, sta)."""
    rows = check_tools()
    table = Table(title="EDA tool status")
    table.add_column("Tool")
    table.add_column("Binary")
    table.add_column("Found")
    table.add_column("Version / path")
    table.add_column("Purpose")
    # only these are required for the default flow; gtkwave/make/verilator
    # are optional alternatives, so naming them "missing" is misleading
    required = {"iverilog", "vvp", "yosys"}
    missing_required: list[str] = []
    missing_optional: list[str] = []
    for r in rows:
        from .config import resolve_tool

        detail = r.version_hint or (resolve_tool(r.binary) or "")
        table.add_row(
            r.name, r.binary, "yes" if r.found else "NO",
            detail, r.purpose,
        )
        if not r.found:
            (missing_required if r.binary in required
             else missing_optional).append(r.binary)
    console.print(table)
    if missing_required:
        console.print(
            f"[red]missing required: {', '.join(missing_required)}. "
            "Simulation and synthesis cannot run. See README for the apt "
            "install line or the user-space toolchain notes.[/red]"
        )
        raise typer.Exit(code=1)
    if missing_optional:
        console.print(
            f"[yellow]optional, not installed: {', '.join(missing_optional)}."
            + (" `make` is only needed for `--tool verilator`; the default "
               "iverilog path works without it." if "make" in missing_optional
               else "")
            + "[/yellow]"
        )
    else:
        console.print("[green]All tools found.[/green]")


@app.command("check-llm")
def check_llm_cmd(
    probe: bool = typer.Option(False, "--probe", help="Also sweep the fallback model pool."),
):
    """Validate OpenRouter key + model with a tiny call."""
    from .llm import get_model

    console.print(f"model={get_model()}")
    console.print(f"base={config.OPENROUTER_BASE_URL}")
    ok, msg = check_llm(probe_models=probe)
    if ok:
        console.print(f"[green]LLM OK: {msg}[/green]")
    else:
        console.print(f"[red]LLM FAILED: {msg}[/red]")
        console.print(
            "[yellow]A 429 means the free tier is rate-limited. vlab rotates "
            "through fallback models automatically; run `vlab check-llm --probe` "
            "to see which models are live right now.[/yellow]"
        )
        raise typer.Exit(code=1)


@app.command("models")
def models_cmd(
    probe: bool = typer.Option(False, "--probe", help="Live-test each model."),
):
    """Show the model + fallback pool vlab rotates through on rate limits."""
    from .llm import fallback_models, get_api_key, get_model

    primary = get_model()
    table = Table(title="LLM model rotation (free tier)")
    table.add_column("#")
    table.add_column("model")
    table.add_column("role")
    for i, m in enumerate([primary, *fallback_models()], 1):
        role = "primary" if i == 1 else f"fallback {i - 1}"
        if m == primary and i > 1:
            role += " (dup)"
        table.add_row(str(i), m, role)
    console.print(table)
    console.print(
        f"[dim]Reasoning effort: {os.getenv('VLAB_REASONING_EFFORT', 'low')} "
        f"| free-tier limits are per-model, so vlab rotates instead of "
        f"sleeping through them.[/dim]"
    )
    if probe:
        from .llm import _probe_pool

        if not get_api_key():
            _fail("OPENROUTER_API_KEY is not set (env or .env).")
        console.print(_probe_pool())
    else:
        console.print("[dim]Add --probe to test which models answer right now.[/dim]")


@app.command("init")
def init_cmd(
    path: str = typer.Option(".", "--path", help="Target directory."),
    dut: str = typer.Option("my_dut", "--dut", help="Default DUT name."),
):
    """Scaffold a vlab project: yaml + sessions/ + runs/."""
    target = Path(path).resolve()
    target.mkdir(parents=True, exist_ok=True)
    (target / "sessions").mkdir(exist_ok=True)
    (target / "runs").mkdir(exist_ok=True)
    proj = {
        "dut": dut,
        "model": config.DEFAULT_MODEL,
        "pdk_choice": config.DEFAULT_PDK_CHOICE,
        "supported_pdks": list(config.SUPPORTED_PDKS),
        "flow": {
            "simulator": "iverilog",
            "synth": "yosys+abc",
            "ppa": {"area": "yosys", "timing": "opensta", "power": "opensta+vcd"},
        },
    }
    with open(target / config.PROJECT_FILE, "w") as f:
        yaml.safe_dump(proj, f, sort_keys=False)
    console.print(f"[green]Init OK:[/green] {target / config.PROJECT_FILE}")


@app.command("new")
def new_cmd(
    dut: str = typer.Option(None, "--dut", help="DUT/module name."),
    project: str = typer.Option(None, "--project", help="Project dir."),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    desc: str = typer.Option("", "--desc", help="DUT description."),
    width: int = typer.Option(8, "--width", help="Default data width."),
    pdk: str = typer.Option("auto", "--pdk", help="auto|sky130|nangate45|asap7."),
    use_llm: bool = typer.Option(True, "--use-llm/--no-llm"),
    category: str = typer.Option("sequential", "--category"),
):
    """M1: autonomous DUT Q&A -> sessions/<id>/spec.json (PDK auto-select)."""
    from .ask import run_new

    pdir = config.resolve_project_dir(project)
    try:
        sid, spec_path = run_new(
            pdir, dut=dut, non_interactive=non_interactive, desc=desc,
            width=width, pdk=pdk, use_llm=use_llm, category=category,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        _fail(str(e))
    try:
        spec = json.loads(Path(spec_path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        _fail(f"Cannot read spec: {e}")
    console.print(f"[green]Session:[/green] {sid}")
    console.print(f"spec: {spec_path}")
    console.print(
        f"PDK: requested={spec['pdk_choice']} selected={spec['pdk_selected']} "
        f"({spec.get('pdk_reason','')})"
    )


@app.command("gen")
def gen_cmd(
    session: str = typer.Option(None, "--session", help="Session id (default latest)."),
    project: str = typer.Option(None, "--project"),
    use_llm: bool = typer.Option(True, "--use-llm/--no-llm"),
    force: bool = typer.Option(False, "--force"),
):
    """M2: generate design.v + testbench from spec.json via OpenRouter."""
    from .rtl_gen import run_gen

    pdir, sid, spec_path = _spec_or_exit(project, session)
    try:
        m = run_gen(pdir, sid, spec_path, use_llm=use_llm, force=force)
    except FileExistsError as e:
        _fail(f"{e} Use --force to overwrite.")
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        _fail(str(e))
    console.print(f"[green]Gen OK:[/green] {m['design']} + {m['testbench']}")
    for w in m.get("warnings", []):
        console.print(f"[yellow]WARN: {w}[/yellow]")


@app.command("simulate")
def simulate_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
    tool: str = typer.Option("auto", "--tool", help="auto|iverilog|verilator"),
):
    """M3: iverilog/verilator -> wave.vcd + pass/fail."""
    from .sim import run_simulate

    pdir, sid, _ = _spec_or_exit(project, session)
    r = _run_or_fail(run_simulate, pdir, sid, tool=tool)
    color = "green" if r["status"] == "pass" else "red"
    console.print(f"[{color}]SIM {r['status'].upper()}[/{color}] tool={r['tool']} "
                  f"pass={r['pass_count']} fail={r['fail_count']} vcd={r.get('vcd')}")
    if r.get("note"):
        console.print(f"[yellow]WARN: {r['note']}[/yellow]")
    if r["status"] != "pass":
        raise typer.Exit(code=1)


@app.command("synth")
def synth_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
    top: str = typer.Option(None, "--top"),
    lib: str = typer.Option(None, "--lib", help="Liberty file (default generic)."),
):
    """M4: yosys+abc -> netlist.v/json + area."""
    from .synth import run_synth

    pdir, sid, _ = _spec_or_exit(project, session)
    r = _run_or_fail(run_synth, pdir, sid, top=top, lib=lib)
    console.print(f"[green]Synth OK:[/green] {r['netlist']} "
                  f"cells={r['area']['cell_count']} wires={r['area']['wire_count']}")


@app.command("ppa")
def ppa_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
    freq_mhz: float = typer.Option(100.0, "--freq-mhz"),
    clk: str = typer.Option("", "--clk", help="Clock port (default: from spec)."),
):
    """M5: openSTA timing + power (VCD) + yosys area -> ppa.json."""
    from .ppa import run_ppa

    pdir, sid, _ = _spec_or_exit(project, session)
    r = _run_or_fail(run_ppa, pdir, sid, freq_mhz=freq_mhz, clk_port=clk)
    console.print(f"[green]PPA OK:[/green] area_cells={r['area'].get('cell_count')} "
                  f"timing={r['timing'].get('method')} power={r['power'].get('method')} "
                  f"({r['power'].get('power_w')})")
    if r["timing"].get("method") not in ("opensta",):
        console.print(f"[yellow]note: timing method={r['timing'].get('method')} "
                      f"{r['timing'].get('note') or r['timing'].get('error','')}[/yellow]")


@app.command("auto")
def auto_cmd(
    prompt: str = typer.Option(None, "--prompt", help="One-line design request."),
    project: str = typer.Option(None, "--project"),
    dut: str = typer.Option(None, "--dut", help="Override module name."),
    width: int = typer.Option(None, "--width", help="Data width (default: LLM decides)."),
    candidates: int = typer.Option(None, "--candidates", help="N designs/iter (default: LLM decides)."),
    iterations: int = typer.Option(1, "--iterations", help="Search generations."),
    fix_rounds: int = typer.Option(3, "--fix-rounds", help="TB repair rounds each."),
    use_llm: bool = typer.Option(True, "--use-llm/--no-llm"),
    freq_mhz: float = typer.Option(100.0, "--freq-mhz"),
    no_clarify: bool = typer.Option(False, "--no-clarify", help="Skip clarification questions."),
):
    """Single prompt in -> spec -> candidates -> verify/fix -> synth/ppa -> final."""
    import sys

    from .auto import assess_prompt, run_auto

    if not prompt:
        prompt = typer.prompt("Describe the DUT to build")
    if not prompt.strip():
        console.print("[red]Empty prompt.[/red]")
        raise typer.Exit(code=1)
    if use_llm and not no_clarify:
        questions = assess_prompt(prompt.strip(), True)
        if questions:
            if sys.stdin.isatty():
                console.print("[yellow]The LLM needs a few details:[/yellow]")
                answered = []
                for q in questions:
                    try:
                        a = typer.prompt(f"  {q}", default="")
                    except (EOFError, KeyboardInterrupt):
                        a = ""
                    if a.strip():
                        answered.append(f"{q} -> {a.strip()}")
                if answered:
                    prompt = prompt.strip() + "\nClarifications: " + "; ".join(answered)
            else:
                console.print("[yellow]Prompt under-specified; proceeding without clarification (non-interactive).[/yellow]")
    pdir = config.resolve_project_dir(project)
    try:
        s = run_auto(
            pdir, prompt, dut=dut, width=width, candidates=candidates,
            iterations=iterations, max_fix_rounds=fix_rounds,
            use_llm=use_llm, freq_mhz=freq_mhz,
        )
    except Exception as e:  # noqa: BLE001 - never surface a traceback
        _fail(f"{type(e).__name__}: {e}")
    console.print(f"[green]Session:[/green] {s['session']}  "
                  f"winner={s['winner']} final={s['final']}")
    for c in s["candidates"]:
        note = f" note={c['note']}" if c.get("note") else ""
        console.print(f"  {c['id'].split('__')[-1]}: {c['status']} "
                      f"cells={c.get('cells')} crit={c.get('crit_ns')} "
                      f"power={c.get('power_w')}{note}")
    if not s["final"]:
        cands = s["candidates"]
        llm_down = [c for c in cands if "LLM call failed" in (c.get("note") or "")
                    or "429" in (c.get("note") or "")]
        all_err = bool(cands) and all(
            c.get("status") in ("error", "fail") for c in cands)
        console.print(f"[red]No passing candidate out of {len(cands)}.[/red]")
        if llm_down and len(llm_down) == len(cands):
            console.print(
                "[yellow]Every candidate failed on an LLM call — the model "
                "tier is rate-limited or unreachable, so no RTL was produced. "
                "Run `vlab check-llm --probe` to see which free models are "
                "live, or set a paid OPENROUTER_MODEL.[/yellow]"
            )
        elif all_err:
            console.print(
                "[yellow]Every candidate failed. Read runs/<sid>__i*c*/sim.log "
                "for the compiler/simulation error — the per-candidate notes "
                "above say what the repair loop hit.[/yellow]"
            )
        else:
            console.print(
                "[yellow]Simulation ran but no candidate passed. Check "
                "runs/<sid>__i*c*/sim.log; try more --fix-rounds, more "
                "--candidates, or a more explicit prompt.[/yellow]"
            )
        console.print(f"artifacts: {s['session']} (COMPARISON.md has the table)")
        raise typer.Exit(code=1)


@app.command("serve")
def serve_cmd(
    project: str = typer.Option(None, "--project", help="Workspace dir."),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """Start the localhost dashboard (prompt -> live progress -> results)."""
    import uvicorn

    from .web import create_app

    console.print(f"[green]Serving AI VerilogLab on http://{host}:{port}[/green]")
    uvicorn.run(create_app(project), host=host, port=port, log_level="warning")


@app.command("fix")
def fix_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
    rounds: int = typer.Option(3, "--rounds", help="Max LLM repair rounds."),
):
    """AI-debug: feed sim failures to the LLM to repair the testbench."""
    from .fix import run_fix

    pdir, sid, _ = _spec_or_exit(project, session)
    r = _run_or_fail(run_fix, pdir, sid, rounds=rounds)
    for h in r["rounds"]:
        target = h.get("target", "")
        console.print(f"round {h['round']}: {h['status']} "
                      f"(pass={h['pass']} fail={h['fail']})"
                      + (f" target={target}" if target else "")
                      + (f" note={h['note']}" if h.get("note") else ""))
    if r["final"] != "pass":
        _fail(f"still failing after {rounds} round(s). See sim.log in the run dir.")
    console.print("[green]Simulation passing.[/green]")


@app.command("report")
def report_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
):
    """M5: consolidate PPA.md."""
    from .config import session_dir
    from .report import build_report

    pdir, sid, _ = _spec_or_exit(project, session)
    out = _run_or_fail(build_report, session_dir(pdir, sid))
    console.print(f"[green]Report:[/green] {out}")


@app.command("run")
def run_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
    tool: str = typer.Option("auto", "--tool"),
    freq_mhz: float = typer.Option(100.0, "--freq-mhz"),
    use_llm: bool = typer.Option(True, "--use-llm/--no-llm"),
    force: bool = typer.Option(False, "--force", help="Regenerate RTL."),
):
    """Full pipeline: gen (if missing) -> simulate -> synth -> ppa -> report."""
    from .config import session_dir
    from .ppa import run_ppa
    from .report import build_report
    from .rtl_gen import run_gen
    from .sim import run_simulate
    from .synth import run_synth

    pdir, sid, spec_path = _spec_or_exit(project, session)
    outdir = session_dir(pdir, sid)
    if not (outdir / "design.v").exists():
        try:
            run_gen(pdir, sid, spec_path, use_llm=use_llm, force=force)
        except FileExistsError as e:
            _fail(f"{e} Use --force to overwrite.")
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            _fail(str(e))
        console.print("[green]Gen OK[/green]")
    sim = _run_or_fail(run_simulate, pdir, sid, tool=tool)
    console.print(f"SIM {sim['status'].upper()} pass={sim['pass_count']} fail={sim['fail_count']}")
    if sim["status"] != "pass":
        console.print(f"[yellow]{sim.get('log')} — read that for the failure.[/yellow]")
        raise typer.Exit(code=1)
    syn = _run_or_fail(run_synth, pdir, sid)
    console.print(f"Synth cells={syn['area']['cell_count']}")
    ppa = _run_or_fail(run_ppa, pdir, sid, freq_mhz=freq_mhz)
    console.print(f"PPA timing={ppa['timing'].get('method')} power={ppa['power'].get('power_w')}")
    rep = _run_or_fail(build_report, outdir)
    console.print(f"[green]Done:[/green] {rep}")


def main():
    app()


if __name__ == "__main__":
    main()
