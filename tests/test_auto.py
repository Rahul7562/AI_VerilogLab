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


def test_golden_bar_only_applies_to_repaired_candidates(tmp_path, monkeypatch):
    """An independent candidate must not be rejected for having fewer checks.

    The golden pass count was global, so whichever candidate was generated
    first set the floor and every later candidate below it was discarded with
    a "repair weakened TB" note it did not earn — biasing selection toward
    candidate #1 instead of toward the best RTL.
    """
    import vlab.auto as auto_mod
    import vlab.sim as sim_mod

    proj = _mk_fix_session(tmp_path)
    sims = [
        {"status": "pass", "pass_count": 12, "fail_count": 0},  # candidate 1
        {"status": "pass", "pass_count": 5, "fail_count": 0},   # candidate 2, 0 repairs
    ]

    def fake_sim(*a, **k):
        (proj / "runs" / "s1" / "sim.log").write_text("PASS: ok\nDONE\n")
        return sims.pop(0)

    monkeypatch.setattr(sim_mod, "run_simulate", fake_sim)
    golden = {}
    first = auto_mod._verify_candidate(proj, "s1", 0, golden)
    assert first["status"] == "pass" and first["passes"] == 12
    assert golden["passes"] == 12
    # second, unrepaired candidate with fewer checks is still a pass
    second = auto_mod._verify_candidate(proj, "s1", 0, golden)
    assert second["status"] == "pass", second
    assert second["passes"] == 5


def test_golden_bar_still_catches_a_weakened_repair(tmp_path, monkeypatch):
    """The anti-weakening guard must still fire for a repaired candidate."""
    import vlab.auto as auto_mod
    import vlab.llm as llm_mod
    import vlab.sim as sim_mod

    proj = _mk_fix_session(tmp_path)
    sims = [
        {"status": "fail", "pass_count": 0, "fail_count": 1, "rc": 0},
        # 6 checks: above the minimum-checks floor, so this test exercises
        # the anti-weakening bar rather than the under-verified gate
        {"status": "pass", "pass_count": 6, "fail_count": 0, "rc": 0},
    ]

    def fake_sim(*a, **k):
        (proj / "runs" / "s1" / "sim.log").write_text("FAIL: nope\nDONE\n")
        return sims.pop(0)

    monkeypatch.setattr(sim_mod, "run_simulate", fake_sim)
    # a repair that actually changes the TB (so the no-op guard does not trip)
    monkeypatch.setattr(
        llm_mod, "chat",
        lambda *a, **k: "```verilog\nmodule tb_dut1; reg a; $finish; "
                        "$display(\"PASS: fixed\"); endmodule\n```",
    )
    out = auto_mod._verify_candidate(proj, "s1", 1, {"passes": 9})
    assert out["status"] == "fail"
    assert "weakened" in out["note"]


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


def test_session_ids_never_collide_across_threads():
    """The web dashboard runs jobs on parallel threads; two runs starting in
    the same second used to share a runs/<sid> directory and clobber each
    other's spec.json and candidates."""
    import threading

    from vlab.auto import _sid

    out: list[str] = []
    lock = threading.Lock()

    def grab() -> None:
        s = _sid("d")
        with lock:
            out.append(s)

    ts = [threading.Thread(target=grab) for _ in range(25)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(out) == 25
    assert len(set(out)) == 25, f"session id collision: {len(out) - len(set(out))}"


def test_session_id_avoids_existing_dir(tmp_path):
    from vlab.auto import _sid

    (tmp_path / "runs" / "d_20200101_000000").mkdir(parents=True)
    sid = _sid("d_20200101_000000", tmp_path)
    assert sid != "d_20200101_000000"


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


def test_compile_error_attribution():
    """A compile error must be blamed on the file the compiler actually named.

    Regression: a testbench assigning to a DUT output declared `wire` is a
    TESTBENCH bug, but the old rc-only check routed repair to the design, so
    the LLM re-emitted the unchanged DUT every round and the run died.
    """
    from vlab.fix import compile_error_file

    tb_bug = (
        "/p/tb_cnt8.v:82: error: count is not a valid l-value in tb_cnt8.\n"
        "/p/tb_cnt8.v:8:      : count is declared here as wire.\n"
    )
    assert compile_error_file(tb_bug) == "testbench"
    assert compile_error_file("design.v:14: syntax error near foo") == "design"
    # design blamed too: a broken DUT breaks TB elaboration, so fix the DUT
    assert compile_error_file("design.v:1: error: x\ntb_a.v:2: error: y") == "design"
    assert compile_error_file("tb_x.v:3: error: nope") == "testbench"
    assert compile_error_file("rtl/other.v:3: error: bad") == "design"
    assert compile_error_file("no file references here") is None


def test_repair_target_routes_tb_bug_to_testbench():
    from vlab.fix import repair_target

    sim = {"rc": 1, "pass_count": 0, "fail_count": 0}
    tb_bug = "/p/tb_cnt8.v:82: error: count is not a valid l-value in tb_cnt8."
    assert repair_target(sim, tb_bug) == "testbench"
    assert repair_target(sim, "design.v:3: syntax error") == "design"
    # ran but asserted wrong -> the expectations are wrong, not the DUT
    assert repair_target({"rc": 1}, "PASS: a\nFAIL: b") == "testbench"


def test_verify_stops_when_repair_repeats_itself(tmp_path, monkeypatch):
    """A repair that reproduces the file byte-for-byte must not burn rounds."""
    import vlab.auto as auto_mod
    import vlab.llm as llm_mod
    import vlab.sim as sim_mod

    proj = _mk_fix_session(tmp_path)
    calls = {"n": 0}

    def fake_sim(*a, **k):
        (proj / "runs" / "s1" / "sim.log").write_text(
            "/p/tb_dut1.v:9: error: bad l-value")
        return {"status": "fail", "pass_count": 0, "fail_count": 0, "rc": 1}

    def same_every_time(*a, **k):
        calls["n"] += 1
        return "```verilog\nmodule tb_dut1; $finish; endmodule\n```"

    monkeypatch.setattr(sim_mod, "run_simulate", fake_sim)
    monkeypatch.setattr(llm_mod, "chat", same_every_time)
    out = auto_mod._verify_candidate(proj, "s1", 5, {})
    assert out["status"] == "fail"
    assert "unchanged" in out["note"]
    assert calls["n"] == 1  # stopped after the first no-op repair


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
    """A compile error blamed on design.v must route the DUT to the LLM."""
    import vlab.fix as fix_mod
    import vlab.llm as llm_mod
    import vlab.sim as sim_mod

    proj = _mk_fix_session(tmp_path)
    # start from a design that genuinely fails to compile
    broken = "module dut1(input a, output y);\nassign y = = a;\nendmodule"
    (proj / "runs" / "s1" / "design.v").write_text(broken)
    states = [{"status": "fail", "pass_count": 0, "fail_count": 0, "rc": 30},
              {"status": "pass", "pass_count": 1, "fail_count": 0, "rc": 0}]

    def fake_sim(*a, **k):
        (proj / "runs" / "s1" / "sim.log").write_text("design.v:2: syntax error")
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
    assert out["rounds"][0]["target"] == "design"
    assert (proj / "runs" / "s1" / "design.v").read_text() == (
        "module dut1(input a, output y);\nassign y = a;\nendmodule")
