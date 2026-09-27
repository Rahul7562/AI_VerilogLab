"""Offline auto-loop test (template RTL + real EDA, no LLM)."""
import json

import pytest
from typer.testing import CliRunner

from vlab.cli import app
from vlab.config import resolve_tool

runner = CliRunner()


def test_auto_offline_single(tmp_path):
    if resolve_tool("iverilog") is None or resolve_tool("yosys") is None:
        pytest.skip("need iverilog+yosys")
    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    r = runner.invoke(
        app,
        ["auto", "--project", str(tmp_path), "--prompt", "8-bit adder",
         "--dut", "alu8", "--no-llm"],
    )
    assert r.exit_code == 0, r.stdout
    assert "winner=" in r.stdout
    runs = tmp_path / "runs"
    sids = [d for d in runs.iterdir() if d.is_dir() and (d / "auto.json").exists()]
    assert len(sids) == 1
    auto = json.loads((sids[0] / "auto.json").read_text())
    assert auto["winner"] and auto["final"]
    assert (sids[0] / "final" / "PPA.md").exists()
    assert (sids[0] / "COMPARISON.md").exists()
    assert (sids[0] / "memory.jsonl").exists()


def test_find_spec_ignores_auto_candidates(tmp_path):
    from vlab.config import find_spec

    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    (tmp_path / "sessions" / "alu8_20250101_000000").mkdir(parents=True)
    (tmp_path / "sessions" / "alu8_20250101_000000" / "spec.json").write_text("{}")
    cand = tmp_path / "runs" / "alu8_20250101_000000__i1c1"
    cand.mkdir(parents=True)
    (cand / "spec.json").write_text("{}")
    sid, _ = find_spec(tmp_path, None)
    assert sid == "alu8_20250101_000000"
