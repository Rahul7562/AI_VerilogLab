"""vlab CLI: init, Q&A, gen, simulate, fix, synth, ppa, run, report, auto."""
from __future__ import annotations

import json
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


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Show version."),
):
    if version:
        console.print(f"vlab {__version__}")
        raise typer.Exit()


@app.command("check-tools")
def check_tools_cmd():
    """Check EDA binaries (iverilog, verilator, yosys, abc, sta)."""
    rows = check_tools()
    table = Table(title="EDA tool status")
    table.add_column("Tool")
    table.add_column("Binary")
    table.add_column("Found")
    table.add_column("Version / path")
    table.add_column("Purpose")
    missing = 0
    for r in rows:
        from .config import resolve_tool

        detail = r.version_hint or (resolve_tool(r.binary) or "")
        table.add_row(
            r.name, r.binary, "yes" if r.found else "NO",
            detail, r.purpose,
        )
        if not r.found:
            missing += 1
    console.print(table)
    if missing:
        console.print(
            f"[yellow]{missing} tool(s) missing. "
            "See README for apt install line. M3-M5 need them.[/yellow]"
        )
    else:
        console.print("[green]All tools found.[/green]")


@app.command("check-llm")
def check_llm_cmd():
    """Validate OpenRouter key + model with a tiny call."""
    from .llm import get_model

    console.print(f"model={get_model()}")
    console.print(f"base={config.OPENROUTER_BASE_URL}")
    ok, msg = check_llm()
    if ok:
        console.print(f"[green]LLM OK: {msg}[/green]")
    else:
        console.print(f"[red]LLM FAILED: {msg}[/red]")
        raise typer.Exit(code=1)


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
    sid, spec_path = run_new(
        pdir, dut=dut, non_interactive=non_interactive, desc=desc,
        width=width, pdk=pdk, use_llm=use_llm, category=category,
    )
    spec = json.loads(Path(spec_path).read_text())
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
    from .config import find_spec
    from .rtl_gen import run_gen

    pdir = config.resolve_project_dir(project)
    sid, spec_path = find_spec(pdir, session)
    m = run_gen(pdir, sid, spec_path, use_llm=use_llm, force=force)
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
    from .config import find_spec
    from .sim import run_simulate

    pdir = config.resolve_project_dir(project)
    sid, _ = find_spec(pdir, session)
    try:
        r = run_simulate(pdir, sid, tool=tool)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    color = "green" if r["status"] == "pass" else "red"
    console.print(f"[{color}]SIM {r['status'].upper()}[/{color}] tool={r['tool']} "
                  f"pass={r['pass_count']} fail={r['fail_count']} vcd={r.get('vcd')}")
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
    from .config import find_spec
    from .synth import run_synth

    pdir = config.resolve_project_dir(project)
    sid, _ = find_spec(pdir, session)
    try:
        r = run_synth(pdir, sid, top=top, lib=lib)
    except (RuntimeError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
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
    from .config import find_spec
    from .ppa import run_ppa

    pdir = config.resolve_project_dir(project)
    sid, _ = find_spec(pdir, session)
    try:
        r = run_ppa(pdir, sid, freq_mhz=freq_mhz, clk_port=clk)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]PPA OK:[/green] area_cells={r['area'].get('cell_count')} "
                  f"timing={r['timing'].get('method')} power={r['power'].get('method')} "
                  f"({r['power'].get('power_w')})")


@app.command("auto")
def auto_cmd(
    prompt: str = typer.Option(None, "--prompt", help="One-line design request."),
    project: str = typer.Option(None, "--project"),
    dut: str = typer.Option(None, "--dut", help="Override module name."),
    width: int = typer.Option(8, "--width"),
    candidates: int = typer.Option(1, "--candidates", help="N designs per iteration."),
    iterations: int = typer.Option(1, "--iterations", help="Search generations."),
    fix_rounds: int = typer.Option(3, "--fix-rounds", help="TB repair rounds each."),
    use_llm: bool = typer.Option(True, "--use-llm/--no-llm"),
    freq_mhz: float = typer.Option(100.0, "--freq-mhz"),
):
    """Single prompt in -> spec -> candidates -> verify/fix -> synth/ppa -> final."""
    from .auto import run_auto

    if not prompt:
        prompt = typer.prompt("Describe the DUT to build")
    if not prompt.strip():
        console.print("[red]Empty prompt.[/red]")
        raise typer.Exit(code=1)
    pdir = config.resolve_project_dir(project)
    try:
        s = run_auto(
            pdir, prompt, dut=dut, width=width, candidates=candidates,
            iterations=iterations, max_fix_rounds=fix_rounds,
            use_llm=use_llm, freq_mhz=freq_mhz,
        )
    except (RuntimeError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Session:[/green] {s['session']}  "
                  f"winner={s['winner']} final={s['final']}")
    for c in s["candidates"]:
        console.print(f"  {c['id'].split('__')[-1]}: {c['status']} "
                      f"cells={c.get('cells')} crit={c.get('crit_ns')} "
                      f"power={c.get('power_w')}")
    if not s["final"]:
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
    from .config import find_spec
    from .fix import run_fix

    pdir = config.resolve_project_dir(project)
    sid, _ = find_spec(pdir, session)
    try:
        r = run_fix(pdir, sid, rounds=rounds)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    for h in r["rounds"]:
        console.print(f"round {h['round']}: {h['status']} "
                      f"(pass={h['pass']} fail={h['fail']})")
    if r["final"] != "pass":
        raise typer.Exit(code=1)


@app.command("report")
def report_cmd(
    session: str = typer.Option(None, "--session"),
    project: str = typer.Option(None, "--project"),
):
    """M5: consolidate PPA.md."""
    from .config import find_spec, session_dir
    from .report import build_report

    pdir = config.resolve_project_dir(project)
    sid, _ = find_spec(pdir, session)
    out = build_report(session_dir(pdir, sid))
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
    from .config import find_spec, session_dir
    from .ppa import run_ppa
    from .report import build_report
    from .rtl_gen import run_gen
    from .sim import run_simulate
    from .synth import run_synth

    pdir = config.resolve_project_dir(project)
    sid, spec_path = find_spec(pdir, session)
    outdir = session_dir(pdir, sid)
    if not (outdir / "design.v").exists():
        run_gen(pdir, sid, spec_path, use_llm=use_llm, force=force)
        console.print("[green]Gen OK[/green]")
    sim = run_simulate(pdir, sid, tool=tool)
    console.print(f"SIM {sim['status'].upper()} pass={sim['pass_count']} fail={sim['fail_count']}")
    if sim["status"] != "pass":
        raise typer.Exit(code=1)
    syn = run_synth(pdir, sid)
    console.print(f"Synth cells={syn['area']['cell_count']}")
    ppa = run_ppa(pdir, sid, freq_mhz=freq_mhz)
    console.print(f"PPA timing={ppa['timing'].get('method')} power={ppa['power'].get('power_w')}")
    rep = build_report(outdir)
    console.print(f"[green]Done:[/green] {rep}")


def main():
    app()


if __name__ == "__main__":
    main()
