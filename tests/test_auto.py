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
    assert (sids[0] / "final" / "gen.json").exists()
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


def test_verify_survives_garbled_llm(tmp_path, monkeypatch):
    import vlab.auto as auto_mod
    import vlab.llm as llm_mod
    import vlab.sim as sim_mod

    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    r = runner.invoke(
        app, ["new", "--project", str(tmp_path), "--non-interactive",
              "--dut", "alu8", "--desc", "adder", "--no-llm"])
    assert r.exit_code == 0, r.stdout
    assert runner.invoke(app, ["gen", "--project", str(tmp_path), "--no-llm"]).exit_code == 0
    sid = next(d.name for d in (tmp_path / "runs").iterdir() if d.is_dir())
    monkeypatch.setattr(
        sim_mod, "run_simulate",
        lambda *a, **k: {"status": "fail", "pass_count": 0, "fail_count": 1},
    )
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: "truncated prose, no verilog")
    out = auto_mod._verify_candidate(tmp_path, sid, 1, {})
    assert out["status"] == "fail"  # must not raise


def test_auto_survives_candidate_error(tmp_path, monkeypatch):
    import vlab.rtl_gen as gen_mod
    from vlab.auto import run_auto

    real_gen = gen_mod.run_gen

    def flaky(project_dir, session_id, spec_path, **kw):
        if session_id.endswith("c2"):
            raise RuntimeError("boom")
        return real_gen(project_dir, session_id, spec_path, **kw)

    monkeypatch.setattr(gen_mod, "run_gen", flaky)
    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    s = run_auto(tmp_path, "8-bit adder", dut="alu8", candidates=2,
                 iterations=1, max_fix_rounds=0, use_llm=False)
    by_cand = {c["cand"]: c for c in s["candidates"]}
    assert by_cand[2]["status"] == "error"
    assert s["winner"] is not None  # c1 still carried the run
    assert (tmp_path / "runs" / s["session"] / "auto.json").exists()


def _mk_fix_session(tmp_path, dut="dut1"):
    import json as _json

    proj = tmp_path / "p"
    (proj / "runs" / "s1").mkdir(parents=True)
    spec = {
        "dut_name": dut, "description": "d", "category": "sequential",
        "data_width": 1, "pdk_choice": "auto",
        "ports": [{"name": "a", "direction": "input", "width": 1},
                  {"name": "y", "direction": "output", "width": 1}],
    }
    (proj / "runs" / "s1" / "spec.json").write_text(_json.dumps(spec))
    (proj / "runs" / "s1" / "design.v").write_text(
        f"module {dut}(input a, output y); assign y = a; endmodule")
    (proj / "runs" / "s1" / f"tb_{dut}.v").write_text(
        f"module tb_{dut}; $finish; endmodule")
    return proj


def test_is_compile_fail():
    from vlab.fix import is_compile_fail

    assert is_compile_fail({"rc": 30}, "syntax error somewhere")
    assert is_compile_fail({"rc": 1}, "some log")
    assert not is_compile_fail({"rc": 0}, "PASS: x\nFAIL: y")
    assert not is_compile_fail({"rc": 0}, "PASS: x")


def test_extract_design_and_intact():
    from vlab.fix import _extract_design, _require_design_intact

    raw = ("prose\n```verilog\nmodule dut1(input a, output y);\n"
           "assign y = a;\nendmodule\n```")
    d = _extract_design(raw, "dut1")
    assert "module dut1" in d
    _require_design_intact(d, "dut1", [{"name": "a"}, {"name": "y"}])
    with pytest.raises(ValueError, match="dropped ports"):
        _require_design_intact(d, "dut1", [{"name": "a"}, {"name": "zzz"}])
    with pytest.raises(ValueError):
        _extract_design("no code here", "dut1")


def test_fix_repairs_design_on_compile_error(tmp_path, monkeypatch):
    import vlab.fix as fix_mod
    import vlab.llm as llm_mod
    import vlab.sim as sim_mod

    proj = _mk_fix_session(tmp_path)
    states = [{"status": "fail", "pass_count": 0, "fail_count": 0, "rc": 30},
              {"status": "pass", "pass_count": 1, "fail_count": 0, "rc": 0}]

    def fake_sim(*a, **k):
        (proj / "runs" / "s1" / "sim.log").write_text("design.v:1: syntax error")
        return states.pop(0)

    fixed = ("```verilog\nmodule dut1(input a, output y);\n"
             "assign y = a;\nendmodule\n```")
    seen_prompts = []
    monkeypatch.setattr(sim_mod, "run_simulate", fake_sim)

    def fake_chat(messages, **kw):
        seen_prompts.append(messages[0]["content"])
        return fixed

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    out = fix_mod.run_fix(proj, "s1", rounds=2)
    assert out["final"] == "pass"
    assert "COMPILE" in seen_prompts[0]
    assert (proj / "runs" / "s1" / "design.v").read_text() == (
        "module dut1(input a, output y);\nassign y = a;\nendmodule")
