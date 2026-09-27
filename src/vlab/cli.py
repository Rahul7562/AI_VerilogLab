"""vlab CLI — M0: init, check-tools, check-llm. M1-M5 commands stubbed."""
from __future__ import annotations

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
        from .utils import which

        detail = r.version_hint or (which(r.binary) or "")
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
    console.print(f"model={config.DEFAULT_MODEL}")
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


def _todo(milestone: str):
    console.print(f"[yellow]Not yet implemented — lands in {milestone}.[/yellow]")
    raise typer.Exit(code=2)


@app.command("new")
def new_cmd():
    """M1: autonomous DUT Q&A -> spec.json (with PDK auto-select)."""
    _todo("M1")


@app.command("gen")
def gen_cmd():
    """M2: generate design.v + tb from spec.json via OpenRouter."""
    _todo("M2")


@app.command("simulate")
def simulate_cmd():
    """M3: iverilog/verilator -> wave.vcd + pass/fail."""
    _todo("M3")


@app.command("synth")
def synth_cmd():
    """M4: yosys+abc -> netlist.v/json + area."""
    _todo("M4")


@app.command("ppa")
def ppa_cmd():
    """M5: openSTA timing + power (VCD) + yosys area -> report."""
    _todo("M5")


@app.command("run")
def run_cmd():
    """M5: full pipeline sim->synth->ppa."""
    _todo("M3-M5")


@app.command("report")
def report_cmd():
    """M5: consolidate PPA.md."""
    _todo("M5")


def main():
    app()


if __name__ == "__main__":
    main()
