"""Scenario matrix: full offline pipeline over diverse DUT shapes.

No LLM, real EDA (skipped when tools absent). Each case runs
new -> gen -> simulate -> synth -> ppa -> report end to end.
"""
import json

import pytest

from vlab.config import resolve_tool
from vlab.spec import validate_spec

NEEDS_EDA = pytest.mark.skipif(
    resolve_tool("iverilog") is None or resolve_tool("yosys") is None,
    reason="need iverilog+yosys",
)

CASES = [
    ("adder8", 8, ["clk", "rst_n", "a", "b", "y"]),
    ("comb8", 8, ["a", "b", "y"]),
    ("rsthi", 8, ["clk", "rst", "a", "b", "y"]),
    ("tiny1", 1, ["clk", "rst_n", "a", "b", "y"]),
    ("wide32", 32, ["clk", "rst_n", "a", "b", "y"]),
    ("weird", 4, ["clk", "rst_n", "din", "dout"]),
    ("twoout", 8, ["clk", "rst_n", "a", "y1", "y2"]),
    ("clockname", 8, ["clock", "rst_n", "a", "b", "y"]),
    ("noclk", 8, ["a", "b", "y"]),
    ("noreset", 8, ["clk", "a", "b", "y"]),
    ("cmpish", 4, ["a", "b", "gt", "lt", "eq"]),
    ("rstword", 8, ["clk", "reset", "a", "b", "y"]),
    ("dq", 8, ["clk", "rst_n", "d", "q"]),
    ("w16", 16, ["clk", "rst_n", "a", "b", "y"]),
    ("tri8", 8, ["clk", "rst_n", "a", "b", "y", "bus"]),
]


def _ports(names, width):
    out = []
    for n in names:
        if n == "bus":
            out.append({"name": n, "direction": "inout", "width": width})
        elif n in ("clk", "clock", "rst", "rst_n", "reset"):
            out.append({"name": n, "direction": "input", "width": 1})
        elif n in ("y", "dout", "y1", "y2", "q", "gt", "lt", "eq"):
            out.append({"name": n, "direction": "output",
                        "width": 1 if n in ("gt", "lt", "eq") else width})
        else:
            out.append({"name": n, "direction": "input", "width": width})
    return out


@NEEDS_EDA
@pytest.mark.parametrize("dut,width,names", CASES)
def test_matrix_case(tmp_path, dut, width, names):
    from typer.testing import CliRunner

    from vlab.cli import app

    runner = CliRunner()
    proj = tmp_path / "p"
    assert runner.invoke(app, ["init", "--path", str(proj)]).exit_code == 0
    spec = {
        "dut_name": dut, "description": f"{dut} matrix case",
        "category": "sequential", "data_width": width,
        "pdk_choice": "auto", "ports": _ports(names, width),
    }
    validate_spec(spec)
    spec["pdk_selected"] = "nangate45"
    spec["pdk_reason"] = "matrix"
    sdir = proj / "sessions" / "s1"
    rdir = proj / "runs" / "s1"
    sdir.mkdir(parents=True)
    rdir.mkdir(parents=True)
    (sdir / "spec.json").write_text(json.dumps(spec))
    (rdir / "spec.json").write_text(json.dumps(spec))

    assert runner.invoke(app, ["gen", "--project", str(proj),
                               "--session", "s1", "--no-llm"]).exit_code == 0
    r = runner.invoke(app, ["simulate", "--project", str(proj), "--session", "s1"])
    assert r.exit_code == 0, r.stdout
    assert runner.invoke(app, ["synth", "--project", str(proj),
                               "--session", "s1"]).exit_code == 0
    assert runner.invoke(app, ["ppa", "--project", str(proj),
                               "--session", "s1"]).exit_code == 0
    r = runner.invoke(app, ["report", "--project", str(proj), "--session", "s1"])
    assert r.exit_code == 0
    assert (rdir / "PPA.md").exists() and (rdir / "wave.vcd").exists()
    assert (rdir / "netlist.v").exists()
