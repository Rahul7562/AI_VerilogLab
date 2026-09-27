"""End-to-end offline flow tests (template RTL, real EDA when present)."""
import json
import shutil

import pytest
from typer.testing import CliRunner

from vlab.cli import app
from vlab.config import resolve_tool

runner = CliRunner()
needs_iverilog = pytest.mark.skipif(
    resolve_tool("iverilog") is None, reason="iverilog not available"
)
needs_yosys = pytest.mark.skipif(
    resolve_tool("yosys") is None, reason="yosys not available"
)


def _mk_session(tmp_path, dut="alu8"):
    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    r = runner.invoke(
        app,
        ["new", "--project", str(tmp_path), "--non-interactive",
         "--dut", dut, "--desc", "8-bit adder demo", "--no-llm"],
    )
    assert r.exit_code == 0, r.stdout
    r = runner.invoke(app, ["gen", "--project", str(tmp_path), "--no-llm"])
    assert r.exit_code == 0, r.stdout
    sessions = list((tmp_path / "runs").iterdir())
    assert sessions, "no run dir created"
    return sessions[0].name


def test_spec_valid(tmp_path):
    sid = _mk_session(tmp_path)
    spec = json.loads((tmp_path / "runs" / sid / "spec.json").read_text())
    assert spec["dut_name"] == "alu8"
    assert spec["pdk_selected"] in ("sky130", "nangate45", "asap7")
    assert len(spec["ports"]) >= 3


def test_gen_template_content(tmp_path):
    sid = _mk_session(tmp_path)
    d = (tmp_path / "runs" / sid / "design.v").read_text()
    tb = (tmp_path / "runs" / sid / "tb_alu8.v").read_text()
    assert "module alu8" in d
    assert "tb_alu8" in tb
    assert "$dumpfile" in tb and "$finish" in tb


@needs_iverilog
def test_simulate_iverilog(tmp_path):
    sid = _mk_session(tmp_path)
    r = runner.invoke(
        app, ["simulate", "--project", str(tmp_path), "--tool", "iverilog"]
    )
    assert r.exit_code == 0, r.stdout
    assert "SIM PASS" in r.stdout
    sim = json.loads((tmp_path / "runs" / sid / "sim.json").read_text())
    assert sim["status"] == "pass"
    assert sim["vcd"] and sim["vcd_bytes"] > 0


@needs_yosys
def test_synth_ppa_report(tmp_path):
    sid = _mk_session(tmp_path)
    if resolve_tool("iverilog"):
        assert runner.invoke(
            app, ["simulate", "--project", str(tmp_path)]
        ).exit_code == 0
    r = runner.invoke(app, ["synth", "--project", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    assert (tmp_path / "runs" / sid / "netlist.v").exists()
    r = runner.invoke(app, ["ppa", "--project", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    r = runner.invoke(app, ["report", "--project", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    assert (tmp_path / "runs" / sid / "PPA.md").exists()
    ppa = json.loads((tmp_path / "runs" / sid / "ppa.json").read_text())
    assert "area" in ppa and "timing" in ppa and "power" in ppa


def test_parse_llm_output_json():
    from vlab.rtl_gen import parse_llm_output

    raw = '{"design": "module foo(input a, output b); endmodule", "testbench": "module tb_foo; endmodule"}'
    d, t, _ = parse_llm_output(raw, "foo")
    assert "module foo" in d


def test_parse_llm_output_fences():
    from vlab.rtl_gen import parse_llm_output

    raw = "```verilog\nmodule foo(input a); endmodule\n```\n```verilog\nmodule tb_foo; endmodule\n```"
    d, t, _ = parse_llm_output(raw, "foo")
    assert "module foo" in d and "tb_foo" in t
