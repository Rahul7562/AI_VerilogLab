"""Regression tests for the six failure classes found in run history.

Each test names the concrete run artifact / failure bucket it protects, so a
regression points back at the run that exposed it rather than at a number.
"""
from __future__ import annotations

import json

import pytest


# --------------------------------------------------------------------------
# F1/F2: reply truncation. 10 "Could not parse LLM output" + 7
# "empty model reply (finish=length)" candidate failures out of 34.
# --------------------------------------------------------------------------


def test_truncated_nonempty_reply_is_not_returned(monkeypatch):
    """A `finish_reason: length` reply must be retried, never handed back.

    Truncation almost always returns PARTIAL text, so the old check -- which
    only fired inside `if not text.strip()` -- returned the partial JSON to
    the parser, which then rejected it and killed the candidate.
    """
    from vlab import llm as llm_mod

    calls: list[int] = []

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content, finish):
            self.message = _Msg(content)
            self.finish_reason = finish

    class _Resp:
        def __init__(self, content, finish):
            self.choices = [_Choice(content, finish)]

    class _Completions:
        def create(self, *, max_tokens, **kw):
            calls.append(max_tokens)
            if len(calls) == 1:
                # partial JSON, cut off mid-string: truthy content!
                return _Resp('{"design": "module foo(input a', "length")
            return _Resp("COMPLETE", "stop")

    class _Client:
        def __init__(self, **kw):
            self.chat = type(
                "C", (), {"completions": _Completions()}
            )()

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "get_model", lambda: "m")
    monkeypatch.setattr(llm_mod, "fallback_models", lambda: [])
    monkeypatch.setattr(llm_mod, "_throttle", lambda *a, **k: None)
    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kw: _Client()
    )

    out = llm_mod.chat([{"role": "user", "content": "x"}], max_tokens=4096)
    assert out == "COMPLETE", "partial truncated text was returned to the caller"
    assert calls[1] > calls[0], "retry did not raise the token budget"


def test_truncation_retry_capped_not_infinite(monkeypatch):
    """A model that truncates forever must terminate via the ceiling."""
    from vlab import llm as llm_mod

    seen: list[int] = []

    class _Msg:
        content = '{"design": "partial'

    class _Choice:
        message = _Msg()
        finish_reason = "length"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, *, max_tokens, **kw):
            seen.append(max_tokens)
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "get_model", lambda: "m")
    monkeypatch.setattr(llm_mod, "fallback_models", lambda: [])
    monkeypatch.setattr(llm_mod, "_throttle", lambda *a, **k: None)
    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setenv("VLAB_LLM_MAX_TOKENS_CAP", "8192")
    monkeypatch.setattr("openai.OpenAI", lambda **kw: _Client())

    with pytest.raises(RuntimeError):
        llm_mod.chat([{"role": "user", "content": "x"}], max_tokens=4096, retries=1)
    assert max(seen) <= 8192
    assert seen == sorted(seen), "budget must only grow"


def test_truncated_json_is_salvaged():
    """A mid-JSON cutoff should still yield a usable design fragment."""
    from vlab.rtl_gen import parse_llm_output

    cut = (
        '{"design": "module foo(input a, output y);\\n'
        "  always @* y = a;\\nendmodule\\n"
        '", "testbench": "module tb_foo;'
    )
    design, _tb, _n = parse_llm_output(cut, "foo")
    assert "module foo" in design
    assert "endmodule" in design, "escape sequences must be decoded"


def test_gen_budget_exceeds_measured_need():
    """The measured traffic-light reply needed ~7.6k tokens."""
    from vlab.rtl_gen import gen_max_tokens

    assert gen_max_tokens() >= 16384, (
        "budget must clear the 7,657-token reply that was truncated at 4096"
    )


# --------------------------------------------------------------------------
# F4: `output wire` driven procedurally -- candidate i1c2 of the latest run
# produced 18 elaboration errors and scored 0 passes.
# --------------------------------------------------------------------------


WIRE_OUTPUT_DESIGN = """module traffic_light_controller (
    input  wire clk,
    output wire red_main,
    output wire green_main
);
    always @* begin
        red_main = 1'b0;
        green_main = 1'b1;
    end
endmodule
"""


def test_wire_output_driven_in_always_is_promoted_to_reg():
    from vlab.fix import sanitize_design

    fixed, warns = sanitize_design(WIRE_OUTPUT_DESIGN, "traffic_light_controller")
    assert "output reg red_main" in fixed
    assert "output reg green_main" in fixed
    assert "output wire" not in fixed
    assert any("reg" in w for w in warns)


def test_assign_driven_output_stays_wire():
    """Flipping an assign-driven port to `reg` would be a semantic change."""
    from vlab.fix import sanitize_design

    src = (
        "module m(input wire a, output wire sum, output wire carry);\n"
        "  assign sum = a;\n"
        "  assign carry = a;\n"
        "endmodule\n"
    )
    fixed, warns = sanitize_design(src, "m")
    assert fixed == src
    assert warns == []


def test_unrelated_wire_is_never_touched():
    from vlab.fix import sanitize_design

    src = (
        "module m(input wire a, output wire y);\n"
        "  wire internal;\n"
        "  assign y = a & internal;\n"
        "endmodule\n"
    )
    fixed, warns = sanitize_design(src, "m")
    assert fixed == src
    assert warns == []


def test_wire_output_fix_matches_real_failing_artifact():
    """Regression against runs/...124547__i1c2/design.v verbatim shape."""
    from vlab.fix import sanitize_design

    src = """module traffic_light_controller (
    input  wire clk,
    input  wire rst_n,
    output wire red_main,
    output wire yellow_main
);
    always @* begin
        red_main = 1'b0;
        yellow_main = 1'b1;
    end
endmodule
"""
    fixed, _ = sanitize_design(src, "traffic_light_controller")
    assert "wire red_main" not in fixed
    assert "wire yellow_main" not in fixed


# --------------------------------------------------------------------------
# F5: repair targeting. repair_target() returned "testbench" for every
# non-compile failure, so a spec-violating DUT got its TB rewritten to match
# and the wrong pair was published as a winner.
# --------------------------------------------------------------------------


def test_adjudicate_rejects_testbench_bias(monkeypatch):
    """A DUT resetting to green when the spec says all-red must blame the DUT."""
    from vlab import fix as fix_mod

    spec = {
        "dut_name": "tlc",
        "features": ["Asynchronous active-low reset forces all red lights on"],
    }
    design = "always @(posedge clk) if (!rst_n) begin green_main <= 1'b1; end"
    tb = 'check("reset", green_main == 1)'
    monkeypatch.setattr(
        fix_mod, "chat", lambda *a, **k: '{"culprit": "design", "reason": "x"}',
        raising=False,
    )
    # adjudicate_target imports llm.chat lazily; patch the real module
    from vlab import llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: json.dumps(
        {"culprit": "design", "reason": "spec says all-red"}
    ))
    target, reason = fix_mod.adjudicate_target(design, tb, "FAIL: reset", spec, "tlc")
    assert target == "design"
    assert "all-red" in reason


def test_adjudicate_can_return_both(monkeypatch):
    from vlab import fix as fix_mod
    from vlab import llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: '{"culprit": "both"}')
    target, _ = fix_mod.adjudicate_target("d", "t", "FAIL", {}, "x")
    assert target == "both"


def test_adjudicate_defaults_to_both_not_testbench(monkeypatch):
    """Ambiguity must regenerate the pair, never silently blame the TB."""
    from vlab import fix as fix_mod
    from vlab import llm as llm_mod

    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_mod, "chat", boom)
    target, reason = fix_mod.adjudicate_target("d", "t", "FAIL", {}, "x")
    assert target == "both"
    assert reason


def test_extract_pair_requires_both_files():
    from vlab.fix import _extract_pair

    good = json.dumps({"design": "module a; endmodule", "testbench": "module tb_a; endmodule"})
    d, t = _extract_pair(good, "a")
    assert "module a" in d and "tb_a" in t
    with pytest.raises(ValueError):
        _extract_pair('{"design": "module a; endmodule"}', "a")


SPEC_FIX = {
    "dut_name": "tlc",
    "ports": [
        {"name": "clk", "direction": "input", "width": 1},
        {"name": "green_main", "direction": "output", "width": 1},
    ],
    "features": ["reset forces all red lights on"],
}


def test_behavior_repair_prompt_orders_a_behaviour_change():
    """A compiling-but-wrong DUT must NOT be sent a compile-only prompt.

    `design_repair_prompt` tells the model "fix ONLY the compile errors; do
    not change behavior". For a DUT that compiles and merely misbehaves, the
    correct reply is the file unchanged, which trips the loop's no-op abort.
    """
    from vlab.fix import design_behavior_repair_prompt, design_repair_prompt

    p = design_behavior_repair_prompt(
        "module tlc; endmodule", "module tb_tlc; endmodule",
        "FAIL: reset", SPEC_FIX, "tlc", "DUT asserts green on reset",
    )
    assert "BEHAVIOUR is wrong" in p
    assert "ground truth" in p
    assert "all red lights on" in p, "spec must reach the model as JSON"
    # the compile-only prompt would forbid exactly the needed change
    compile_only = design_repair_prompt(
        "module tlc; endmodule", "FAIL: x", "tlc", SPEC_FIX["ports"]
    )
    assert "do not change behavior" in compile_only


def test_repair_prompts_accept_a_dict_spec():
    """A JSON string passed where a dict is expected raised at repair time."""
    from vlab.fix import design_behavior_repair_prompt, pair_repair_prompt

    design_behavior_repair_prompt(
        "module tlc; endmodule", "", "FAIL", SPEC_FIX, "tlc", "reason")
    pair_repair_prompt("module tlc; endmodule", "", "FAIL", SPEC_FIX, "tlc")
    # and the rendered spec must be valid JSON, not a Python dict repr
    out = pair_repair_prompt("module tlc; endmodule", "", "FAIL", SPEC_FIX, "tlc")
    assert "{'dut_name'" not in out, "spec rendered as a Python repr"


def test_repair_prompt_carries_history():
    """Rounds used to retry blind, re-proposing the same wrong wait count."""
    from vlab.fix import repair_prompt

    with_hist = repair_prompt(
        "module d; endmodule", "module tb_d; endmodule", "FAIL: x", "d",
        history=["round 1: repaired the testbench; still failing: reset check"],
    )
    assert "already tried" in with_hist
    assert "reset check" in with_hist
    assert "already tried" not in repair_prompt(
        "module d; endmodule", "module tb_d; endmodule", "FAIL: x", "d")


def test_repair_prompt_teaches_transition_waits():
    """Off-by-one phase checks are the dominant residual failure."""
    from vlab.fix import repair_prompt

    p = repair_prompt("module d; endmodule", "module tb_d; endmodule", "F", "d")
    assert "WAIT FOR THE TRANSITION" in p


# --------------------------------------------------------------------------
# F6: a synth/STA failure used to flip a sim-PASSING candidate to "error",
# which made pick_winner discard it and turned a winnable run into
# "no winner".
# --------------------------------------------------------------------------


def test_measurement_failure_keeps_passing_status(tmp_path, monkeypatch):
    from vlab import auto as auto_mod
    from vlab.optimize import pick_winner

    proj = tmp_path / "p"
    (proj / "runs").mkdir(parents=True)
    monkeypatch.setattr(auto_mod, "expand_spec", lambda *a, **k: {
        "dut_name": "d", "ports": [], "features": [], "test_plan": [],
        "data_width": 8, "suggested_candidates": 1, "limits": {},
    })
    monkeypatch.setattr(auto_mod, "review_spec", lambda s, u: (s, []))
    monkeypatch.setattr(
        auto_mod, "_offline_verify",
        lambda *a, **k: {"status": "pass", "passes": 9, "fails": 0, "repairs": 0},
    )
    monkeypatch.setattr("vlab.rtl_gen.run_gen", lambda *a, **k: {})
    monkeypatch.setattr("vlab.synth.run_synth",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yosys rc=124")))
    summary = auto_mod.run_auto(proj, "p", use_llm=False)

    cands = summary["candidates"]
    assert cands[0]["status"] == "pass", "measurement failure must not void a verified design"
    assert "measure_error" in cands[0]
    assert pick_winner(cands) is not None, "winner was discarded by an STA hiccup"


# --------------------------------------------------------------------------
# F7: a simulation PASS only proves DUT==TB. A candidate passed 19/19 checks
# while using one shared phase constant for every state (spec: green 10s,
# yellow 2s), never implementing the 5s pedestrian extension, and having no
# enable gating at all. Nothing noticed, because nothing compared the spec's
# feature list against what the testbench actually verified.
# --------------------------------------------------------------------------


TMPL_SPEC = {
    "dut_name": "tlc",
    "ports": [
        {"name": "clk", "direction": "input", "width": 1},
        {"name": "green_main", "direction": "output", "width": 1},
    ],
    "features": [
        "Green-main phase lasts 10 seconds",
        "Yellow phase lasts 2 seconds",
        "main_enable holds the current state when low",
    ],
}


def test_coverage_flags_shared_phase_constant(monkeypatch):
    from vlab import conform
    from vlab import llm as llm_mod

    # one constant for every phase == distinct durations not implemented
    design = "localparam PHASE_CYCLES = 10;\n// same limit for all states"
    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: json.dumps({
        "gaps": [{"feature": "Yellow phase lasts 2 seconds",
                  "kind": "unimplemented",
                  "why": "shares PHASE_CYCLES with green"}],
        "verdict": "gaps",
    }))
    ok, gaps = conform.review_coverage(design, "module tb_tlc; endmodule",
                                        "PASS: x", TMPL_SPEC)
    assert ok is False
    assert gaps[0]["kind"] == "unimplemented"


def test_coverage_fails_open_when_provider_down(monkeypatch):
    """Refusing to publish anything would make the tool useless offline."""
    from vlab import conform
    from vlab import llm as llm_mod

    def boom(*a, **k):
        raise RuntimeError("429")

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "chat", boom)
    ok, gaps = conform.review_coverage("d", "t", "log", TMPL_SPEC)
    assert ok is True and gaps == []


def test_coverage_skipped_without_key(monkeypatch):
    from vlab import conform
    from vlab import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "")
    assert conform.review_coverage("d", "t", "log", TMPL_SPEC) == (True, [])


def test_coverage_skipped_without_features(monkeypatch):
    from vlab import conform
    from vlab import llm as llm_mod

    def must_not_run(*a, **k):
        raise AssertionError("should not call the model without features")

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "chat", must_not_run)
    assert conform.review_coverage("d", "t", "log", {"dut_name": "x"}) == (True, [])


def test_passing_but_nonconformant_candidate_is_not_published(tmp_path, monkeypatch):
    """A sim pass with spec gaps must not become the run's winner."""
    from vlab import auto as auto_mod

    proj = tmp_path / "p"
    run = proj / "runs" / "s__i1c1"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps(TMPL_SPEC))
    (run / "design.v").write_text("module tlc; endmodule")
    (run / "tb_tlc.v").write_text("module tb_tlc; endmodule")
    (run / "sim.log").write_text("PASS: something\n")

    monkeypatch.setattr(
        "vlab.sim.run_simulate",
        lambda *a, **k: {"status": "pass", "pass_count": 19, "fail_count": 0},
    )
    monkeypatch.setattr(
        "vlab.conform.review_coverage",
        lambda *a, **k: (False, [{"feature": "main_enable holds state",
                                  "kind": "unimplemented", "why": "no enable port"}]),
    )
    res = auto_mod._verify_candidate(proj, "s__i1c1", 0, {})
    assert res["status"] == "fail", "a spec-violating design must not pass"
    assert "spec gaps" in res["note"]


def test_conformant_pass_is_published(tmp_path, monkeypatch):
    from vlab import auto as auto_mod

    proj = tmp_path / "p"
    run = proj / "runs" / "s__i1c1"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps(TMPL_SPEC))
    (run / "design.v").write_text("module tlc; endmodule")
    (run / "tb_tlc.v").write_text("module tb_tlc; endmodule")

    monkeypatch.setattr(
        "vlab.sim.run_simulate",
        lambda *a, **k: {"status": "pass", "pass_count": 12, "fail_count": 0},
    )
    monkeypatch.setattr("vlab.conform.review_coverage",
                        lambda *a, **k: (True, []))
    res = auto_mod._verify_candidate(proj, "s__i1c1", 0, {})
    assert res["status"] == "pass"


def test_gap_prompt_requires_duration_assertions():
    """The transition-wait fix removed off-by-one but lost timing checks."""
    from vlab.fix import repair_prompt

    p = repair_prompt("module d; endmodule", "module tb_d; endmodule", "F", "d")
    assert "WAIT FOR THE TRANSITION" in p
    assert "MUST also assert the DURATIONS" in p, (
        "a transition-wait alone cannot catch phases that are all too long"
    )


def test_prompts_forbid_task_per_phase_duration_measurement():
    """Phase-by-phase tasks lose the handover cycle.

    A real run measured every phase after the first exactly one cycle short
    (green 3 ok, yellow 1 vs 2, south-green 2 vs 3) and burned three repair
    rounds "fixing" a DUT that was already correct. The measurement was the
    bug. Both prompts must steer to a free-running monitor instead.
    """
    from vlab.fix import repair_prompt
    from vlab.rtl_gen import GEN_PROMPT

    p = repair_prompt("module d; endmodule", "module tb_d; endmodule", "F", "d")
    assert "FREE-RUNNING" in p
    assert "loses" in p and "one cycle short" in p
    assert "MEASUREMENT is wrong, not the DUT" in p
    assert "hand off to a task" in GEN_PROMPT


def test_task_arity_is_padded_not_trimmed():
    """A short task call must gain the missing argument, never lose the real ones.

    Verilog does not check task arity at the call site, so a missing argument
    surfaces only at elaboration as `error: Missing argument N of call to task`
    and kills the whole candidate. An earlier version of this helper matched
    the arity pattern greedily and reported 0 args, which "fixed" the compile
    error by DELETING both arguments -- the testbench then compiled and
    silently checked nothing.
    """
    from vlab.fix import fix_task_arity

    src = (
        "module tb_d;\n"
        "  task check_outputs;\n"
        "    input expected_val;\n"
        "    input [8*32-1:0] label;\n"
        "    begin end\n"
        "  endtask\n"
        "  initial begin\n"
        "    check_outputs(8'd5);\n"
        "    check_outputs(8'd7, \"second\");\n"
        "  end\n"
        "endmodule\n"
    )
    fixed, warns = fix_task_arity(src)
    assert "check_outputs(8'd5, 1'b0);" in fixed, "missing arg must be padded in"
    assert "check_outputs(8'd7, \"second\");" in fixed, "a correct call is untouched"
    assert warns


def test_correct_testbench_is_left_alone():
    from vlab.fix import fix_task_arity

    src = (
        "module tb_d;\n"
        "  task t1; input a; input b; begin end endtask\n"
        "  initial begin t1(1, 2); end\n"
        "endmodule\n"
    )
    fixed, warns = fix_task_arity(src)
    assert fixed == src
    assert warns == []


def test_task_declaration_is_not_mistaken_for_a_call():
    from vlab.fix import fix_task_arity

    src = (
        "module tb_d;\n"
        "  task noargs; begin $display(\"hi\"); end endtask\n"
        "  initial begin noargs(); $finish; end\n"
        "endmodule\n"
    )
    fixed, warns = fix_task_arity(src)
    assert fixed == src, "a 0-arg task declaration must not be 'repaired'"
    assert warns == []


def test_task_with_three_args_is_counted_correctly():
    from vlab.fix import fix_task_arity

    src = (
        "module tb_d;\n"
        "  task m;\n    input a; input [7:0] b; input [8*16-1:0] c;\n"
        "    begin end\n  endtask\n"
        "  initial begin m(1'b1, 8'd2); end\n"
        "endmodule\n"
    )
    fixed, warns = fix_task_arity(src)
    assert "m(1'b1, 8'd2, 1'b0);" in fixed
    assert warns


# --------------------------------------------------------------------------
# The loop learned nothing and gave up too early. memory.jsonl was written
# but memory_read() was never called by the pipeline, and a run in which every
# candidate failed simply returned "no winner" -- discarding most of the
# available probability on a hard design.
# --------------------------------------------------------------------------


def test_failure_feedback_names_the_dead_ends():
    from vlab.auto import _failure_feedback

    cands = [
        {"status": "fail", "note": "exhausted 3 rounds; 11 checks failing"},
        {"status": "fail", "note": "exhausted 3 rounds; 11 checks failing"},
        {"status": "fail", "note": "watchdog fired: a wait never completed"},
        {"status": "pass", "note": ""},
        {"status": "fail", "note": ""},
    ]
    fb = _failure_feedback(cands)
    assert "EARLIER ATTEMPTS" in fb
    assert "11 checks failing" in fb
    assert "watchdog fired" in fb
    # identical failures collapse to one line; blanks and passes are dropped
    assert fb.count("11 checks failing") == 1


def test_failure_feedback_empty_when_nothing_failed():
    from vlab.auto import _failure_feedback

    assert _failure_feedback([{"status": "pass", "note": ""}]) == ""
    assert _failure_feedback([]) == ""


def _auto_harness(tmp_path, monkeypatch, *, pass_after):
    """Wire run_auto with a controllable candidate outcome; count generations."""
    from vlab import auto as auto_mod

    proj = tmp_path / "p"
    (proj / "runs").mkdir(parents=True)
    monkeypatch.setattr(auto_mod, "expand_spec", lambda *a, **k: {
        "dut_name": "d", "ports": [], "features": [], "test_plan": [],
        "data_width": 8, "suggested_candidates": 1, "limits": {},
    })
    monkeypatch.setattr(auto_mod, "review_spec", lambda s, u: (s, []))
    gen: list[str] = []
    n = {"i": 0}

    def fake_gen(project_dir, sid, spec_path, use_llm=True, **kw):
        gen.append(kw.get("feedback", ""))
        n["i"] += 1
        cdir = auto_mod.session_dir(project_dir, sid)
        (cdir / "design.v").write_text("module d; endmodule")
        (cdir / "tb_d.v").write_text("module tb_d; endmodule")

    monkeypatch.setattr("vlab.rtl_gen.run_gen", fake_gen)

    def fake_verify(project_dir, cid, rounds, golden):
        if n["i"] > pass_after:
            return {"status": "pass", "passes": 5, "fails": 0, "repairs": 0}
        return {"status": "fail", "passes": 1, "fails": 2, "repairs": 2,
                "note": f"candidate {n['i']} failed check X"}

    monkeypatch.setattr(auto_mod, "_verify_candidate", fake_verify)
    monkeypatch.setattr("vlab.synth.run_synth", lambda *a, **k: {
        "area": {"cell_count": 10}})
    monkeypatch.setattr("vlab.ppa.run_ppa", lambda *a, **k: {
        "timing": {"crit_ns": 1.0, "period_ns": 10.0, "wns_ns": 0.0},
        "power": {"power_w": 1e-6}})
    return auto_mod, proj, gen, n


def test_escalates_when_every_candidate_fails(tmp_path, monkeypatch):
    """A run that fails everything must retry, not return 'no winner'."""
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=4)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    rounds = {c["iter"] for c in s["candidates"]}
    assert len(rounds) > 1, "no escalation happened after a fully failed round"
    assert s["winner"] is not None, "escalation still produced no winner"
    assert s["rounds_run"] == max(rounds)


def test_escalation_widens_the_candidate_count(tmp_path, monkeypatch):
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=99)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    per_round = {}
    for c in s["candidates"]:
        per_round[c["iter"]] = per_round.get(c["iter"], 0) + 1
    assert per_round[1] == 2
    assert per_round[2] > per_round[1], "escalation did not widen the search"
    assert per_round[3] > per_round[2]


def test_escalation_feeds_failures_forward(tmp_path, monkeypatch):
    """This is the loop that never learned anything: attempt N+1 was blind."""
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=99)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    auto_mod.run_auto(proj, "p", candidates=1, iterations=1, use_llm=True)
    assert gen[0] == "", "the first attempt has nothing to learn from"
    assert any("EARLIER ATTEMPTS" in f for f in gen[1:]), (
        "a later attempt was generated without the earlier failures"
    )
    assert any("failed check X" in f for f in gen[1:])


def test_no_escalation_after_a_success(tmp_path, monkeypatch):
    """Escalating past a winner would multiply API spend for nothing."""
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=0)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    assert n["i"] == 2, f"expected 2 generations, ran {n['i']}"
    assert s["rounds_run"] == 1
    assert s["winner"] is not None


def test_escalation_can_be_disabled(tmp_path, monkeypatch):
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=99)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "0")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    assert s["rounds_run"] == 1
    assert n["i"] == 2, "VLAB_ESCALATION_ROUNDS=0 must keep the old behaviour"
    assert s["winner"] is None


def test_exploration_still_honours_requested_iterations(tmp_path, monkeypatch):
    """iterations>1 keeps exploring after a pass, to find a better point."""
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=0)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=3, use_llm=True)
    assert s["rounds_run"] == 3
    assert n["i"] == 6


# --------------------------------------------------------------------------
# Two defect classes that compile, simulate, and yet can never converge.
# Found by running the traffic-light prompt 5x and reading the failures.
# --------------------------------------------------------------------------


def test_white_box_testbench_is_detected():
    """`while (dut.state !== target)` ties the TB to one state encoding."""
    from vlab.fix import white_box_refs

    tb = (
        "module tb_tlc;\n"
        "  task wait_for; input t; begin\n"
        "    while (dut.state !== t && timeout < 20) @(posedge clk);\n"
        "  end endtask\n"
        "  always @(posedge clk) #1 if (dut.state !== prev) prev = dut.state;\n"
        "endmodule\n"
    )
    refs = white_box_refs(tb, "")
    assert "state" in refs
    assert "prev" not in refs, "a local signal is not a DUT internal"


def test_clean_black_box_testbench_is_not_flagged():
    from vlab.fix import white_box_refs

    tb = (
        "module tb_tlc;\n"
        "  wire [1:0] light;\n"
        "  always @(posedge clk) #1 if (light !== prev) prev = light;\n"
        "endmodule\n"
    )
    assert white_box_refs(tb, "") == []


def test_undersized_counter_is_detected():
    """`reg [1:0] count` cannot represent a 10-cycle phase."""
    from vlab.fix import undersized_counters

    design = (
        "module tlc(input clk, output reg [1:0] light);\n"
        "  reg [1:0] count;\n"
        "  reg [1:0] NORTH_LIMIT;\n"
        "endmodule\n"
    )
    bad = undersized_counters(design)
    assert any("count" in b for b in bad)
    assert any("NORTH_LIMIT" in b for b in bad)


def test_adequate_counter_is_not_flagged():
    from vlab.fix import undersized_counters

    design = (
        "module tlc(input clk, output reg [1:0] light);\n"
        "  reg [7:0] phase_count;\n"
        "  reg [31:0] total_ticks;\n"
        "  reg [1:0] state;\n"   # a state register is not a counter
        "endmodule\n"
    )
    assert undersized_counters(design) == []


def test_escalation_stops_at_the_wall_clock_budget(tmp_path, monkeypatch):
    """A run must not sit for 50 minutes. Measured: 4 of 8 runs were still
    in flight after 50 minutes and had to be killed."""
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=99)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "5")
    # a budget that has certainly already elapsed by the time round 1 ends
    monkeypatch.setenv("VLAB_TIME_BUDGET_S", "0.000001")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    assert s["timed_out"] is True
    assert s["rounds_run"] == 1, "escalated past the time budget"


def test_zero_budget_means_unlimited(tmp_path, monkeypatch):
    """0 is the way to opt out of the bound, not 'already expired'."""
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=99)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    monkeypatch.setenv("VLAB_TIME_BUDGET_S", "0")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    assert s["timed_out"] is False
    assert s["rounds_run"] > 1, "a 0 budget must not disable escalation"


def test_time_budget_is_recorded_even_on_success(tmp_path, monkeypatch):
    auto_mod, proj, gen, n = _auto_harness(tmp_path, monkeypatch, pass_after=0)
    monkeypatch.setenv("VLAB_ESCALATION_ROUNDS", "2")
    s = auto_mod.run_auto(proj, "p", candidates=2, iterations=1, use_llm=True)
    assert s["timed_out"] is False
    assert isinstance(s["elapsed_s"], (int, float))
    assert s["winner"] is not None


def test_time_budget_is_configurable_and_never_negative():
    import os

    from vlab.auto import _time_budget_s

    os.environ["VLAB_TIME_BUDGET_S"] = "123"
    assert _time_budget_s() == 123.0
    os.environ["VLAB_TIME_BUDGET_S"] = "-5"
    assert _time_budget_s() == 0.0, "a negative budget must be clamped, not honoured"
    os.environ["VLAB_TIME_BUDGET_S"] = "not-a-number"
    assert _time_budget_s() > 0, "garbage must fall back to the default"


def test_min_checks_never_below_five():
    from vlab.auto import _min_checks

    assert _min_checks({"features": []}) == 5
    assert _min_checks({"features": ["a", "b"]}) == 5
    assert _min_checks({"features": [f"f{i}" for i in range(9)]}) == 9


def test_one_check_testbench_is_not_a_winner(tmp_path, monkeypatch):
    """Measured: a "winner" shipped with 1 check and 0 failures.

    sim._parse_result accepts any run with at least one PASS, so a
    testbench asserting once and never failing looked perfect. It is not
    verification, and it is exactly the kind of green that gets trusted.
    """
    from vlab import auto as auto_mod

    proj = tmp_path / "p"
    run = proj / "runs" / "s1"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps(TMPL_SPEC))
    (run / "design.v").write_text("module tlc(input clk); endmodule")
    (run / "tb_tlc.v").write_text("module tb_tlc; $finish; endmodule")

    monkeypatch.setattr(
        "vlab.sim.run_simulate",
        lambda *a, **k: {"status": "pass", "pass_count": 1, "fail_count": 0},
    )
    monkeypatch.setattr("vlab.conform.review_coverage", lambda *a, **k: (True, []))
    from vlab import llm as llm_mod
    monkeypatch.setattr(
        llm_mod, "chat",
        lambda *a, **k: "```verilog\nmodule tb_tlc; initial begin "
                        "$display(\"PASS: a\"); $display(\"PASS: b\"); "
                        "$display(\"PASS: c\"); $display(\"PASS: d\"); "
                        "$display(\"PASS: e\"); $finish; end\n```")
    res = auto_mod._verify_candidate(proj, "s1", 0, {})
    assert res["status"] == "fail", "a 1-check testbench must not win"
    assert "under-verified" in res["note"]


# --------------------------------------------------------------------------
# Rate-limit guard rails. OpenRouter enforces a per-model RPS ceiling and
# answers anything above it with 429 rather than queueing. Switching to
# poolside/laguna-s-2.1:free stalled a 3-candidate run: the throttle kept a
# single global timestamp, so parallel candidates on the same model issued
# calls ~0.7s apart and the run produced no output for minutes.
# --------------------------------------------------------------------------


def test_rps_ceiling_never_exceeds_the_configured_limit(monkeypatch):
    import os

    from vlab import llm as llm_mod

    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    for rps, expect_gap in (("15", 1 / 15), ("10", 0.1), ("5", 0.2), ("1", 1.0)):
        monkeypatch.setenv("VLAB_LLM_RPS", rps)
        gap = llm_mod._min_interval()
        assert gap >= expect_gap - 1e-9, f"RPS={rps} allowed {1/gap:.1f} req/s"
        assert 1 / gap <= float(rps) + 1e-6


def test_garbage_rps_falls_back_to_a_safe_default(monkeypatch):
    from vlab import llm as llm_mod

    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setenv("VLAB_LLM_RPS", "not-a-number")
    gap = llm_mod._min_interval()
    assert gap > 0, "a bad RPS must not disable the guard rail"
    assert 1 / gap <= 15.0, "fallback must stay under the 15 RPS ceiling"


def test_parallel_candidates_on_one_model_are_serialised(monkeypatch):
    """The bug: one global timestamp let N threads burst one model."""
    import threading
    import time

    from vlab import llm as llm_mod

    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setenv("VLAB_LLM_RPS", "10")
    llm_mod._last_call_ts = 0.0
    llm_mod._last_call_by_model.clear()

    stamps: list[float] = []

    def hit() -> None:
        llm_mod._throttle("shared-model")
        stamps.append(time.monotonic())

    ths = [threading.Thread(target=hit) for _ in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    stamps.sort()
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    assert all(g >= 0.09 for g in gaps), f"burst not throttled: {gaps}"


def test_different_models_also_respect_the_global_ceiling(monkeypatch):
    import threading
    import time

    from vlab import llm as llm_mod

    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setenv("VLAB_LLM_RPS", "10")
    llm_mod._last_call_ts = 0.0
    llm_mod._last_call_by_model.clear()

    stamps: list[float] = []

    def hit(i: int) -> None:
        llm_mod._throttle(f"model-{i}")
        stamps.append(time.monotonic())

    ths = [threading.Thread(target=hit, args=(i,)) for i in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    stamps.sort()
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    assert all(g >= 0.09 for g in gaps), f"global ceiling breached: {gaps}"


def test_repeated_429_abandons_a_saturated_model(monkeypatch):
    """Rotating beats re-hitting a rate limit with the same cadence."""
    from vlab import llm as llm_mod

    monkeypatch.setenv("VLAB_LLM_429_COOLDOWN", "2")
    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)

    calls = {"n": 0}
    models: list[str] = []

    class _Msg:
        content = ""

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, *, model, **kw):
            calls["n"] += 1
            models.append(model)
            raise RuntimeError("429 rate limit exceeded")

    class _Client:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "get_model", lambda: "saturated")
    monkeypatch.setattr(llm_mod, "fallback_models", lambda: ["m1", "m2"])
    monkeypatch.setattr("openai.OpenAI", lambda **kw: _Client())

    with pytest.raises(RuntimeError):
        llm_mod.chat([{"role": "user", "content": "x"}], retries=4, timeout=5)
    saturated = models.count("saturated")
    assert saturated <= 2, (
        f"hit the saturated model {saturated} times before rotating; "
        "the cooldown is not working"
    )
    assert "m1" in models and "m2" in models, "did not rotate to fallbacks"


def test_thin_tb_is_repaired_not_merely_rejected(tmp_path, monkeypatch):
    """It should get a chance to grow, not be discarded on first sight."""
    from vlab import auto as auto_mod

    proj = tmp_path / "p"
    run = proj / "runs" / "s1"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps(TMPL_SPEC))
    (run / "design.v").write_text("module tlc(input clk); endmodule")
    (run / "tb_tlc.v").write_text("module tb_tlc; $finish; endmodule")

    calls = {"n": 0}

    def fake_sim(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pass", "pass_count": 1, "fail_count": 0}
        return {"status": "pass", "pass_count": 12, "fail_count": 0}

    monkeypatch.setattr("vlab.sim.run_simulate", fake_sim)
    monkeypatch.setattr("vlab.conform.review_coverage", lambda *a, **k: (True, []))
    from vlab import llm as llm_mod
    monkeypatch.setattr(
        llm_mod, "chat",
        lambda *a, **k: "```verilog\nmodule tb_tlc; initial begin "
                        "$display(\"PASS: a\"); $display(\"PASS: b\"); "
                        "$display(\"PASS: c\"); $display(\"PASS: d\"); "
                        "$display(\"PASS: e\"); $finish; end\n```")
    res = auto_mod._verify_candidate(proj, "s1", 2, {})
    assert calls["n"] == 2, "a thin testbench must be given a repair round"
    assert res["status"] == "pass"


def test_structural_rules_reach_every_verilog_prompt():
    """A repair round told only "FAIL: x" re-introduces the black-box bug.

    Observed: a clean first draft (no `dut.state`, no 2-bit counter) was
    repaired into a white-box testbench because the black-box rule lived only
    in the generation prompt. Every prompt that emits Verilog must carry it.
    """
    from vlab.fix import (
        design_behavior_repair_prompt,
        design_repair_prompt,
        pair_repair_prompt,
        repair_prompt,
    )

    prompts = {
        "repair": repair_prompt("module d; endmodule", "module tb_d; endmodule", "F", "d"),
        "design_compile": design_repair_prompt(
            "module d; endmodule", "F", "d", [{"name": "a", "direction": "input", "width": 1}]),
        "design_behaviour": design_behavior_repair_prompt(
            "module d; endmodule", "module tb_d; endmodule", "F",
            {"ports": []}, "d", "wrong reset"),
        "pair": pair_repair_prompt(
            "module d; endmodule", "module tb_d; endmodule", "F",
            {"ports": []}, "d"),
    }
    for name, p in prompts.items():
        assert "BLACK-BOX" in p, f"{name} prompt lost the black-box rule"
        assert "dut.<internal signal>" in p, f"{name} prompt lost the example"
        assert "COUNTER WIDTH" in p, f"{name} prompt lost the counter rule"
        assert "no timeout" in p or "bound every wait" in p, \
            f"{name} prompt lost the unbounded-wait rule"


def test_unchecked_gap_does_not_discard_a_verified_design(monkeypatch):
    """A design that passed 24 checks is not spec-violating just because the
    testbench was not exhaustive. Only `unimplemented`/`incorrect` block."""
    from vlab import conform
    from vlab import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")

    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: json.dumps({
        "gaps": [{"feature": "configurable durations",
                  "kind": "unchecked", "why": "only default parameters tried"}],
        "verdict": "gaps",
    }))
    ok, gaps = conform.review_coverage("d", "t", "PASS: x", TMPL_SPEC)
    assert ok is True, "an incomplete testbench must not void a verified DUT"
    assert gaps == []


def test_unimplemented_gap_still_blocks(monkeypatch):
    """The correctness value of the gate must survive that relaxation."""
    from vlab import conform
    from vlab import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: json.dumps({
        "gaps": [{"feature": "yellow phase",
                  "kind": "unimplemented", "why": "shares a constant with green"}],
        "verdict": "gaps",
    }))
    ok, gaps = conform.review_coverage("d", "t", "PASS: x", TMPL_SPEC)
    assert ok is False
    assert gaps[0]["kind"] == "unimplemented"


def test_incorrect_gap_still_blocks(monkeypatch):
    from vlab import conform
    from vlab import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: json.dumps({
        "gaps": [{"feature": "request latching", "kind": "incorrect",
                  "why": "reads the request combinationally"}],
        "verdict": "gaps",
    }))
    ok, _ = conform.review_coverage("d", "t", "PASS: x", TMPL_SPEC)
    assert ok is False


def test_repair_drift_is_caught_and_recorded(tmp_path, monkeypatch):
    """A repair that reintroduces `dut.state` must be named, not absorbed."""
    from vlab import auto as auto_mod

    proj = tmp_path / "p"
    run = proj / "runs" / "s1"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps(TMPL_SPEC))
    (run / "design.v").write_text("module tlc(input clk); endmodule")
    (run / "tb_tlc.v").write_text("module tb_tlc; $finish; endmodule")

    # always failing sim, so every round repairs
    monkeypatch.setattr(
        "vlab.sim.run_simulate",
        lambda *a, **k: {"status": "fail", "pass_count": 0, "fail_count": 1},
    )
    monkeypatch.setattr(
        "vlab.fix.adjudicate_target", lambda *a, **k: ("testbench", "wrong")

    )
    # repair 1 returns a clean TB; repair 2 reintroduces the white-box ref
    n = {"i": 0}

    def fake_chat(*a, **k):
        n["i"] += 1
        if n["i"] == 1:
            body = "module tb_tlc; initial $display(\"PASS: a\"); $finish; end"
        else:
            body = ("module tb_tlc; while (dut.state !== 3'd2) @(posedge clk);"
                    " initial $display(\"PASS: a\"); $finish; end")
        return "```verilog\n" + body + "\n```"

    from vlab import llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    auto_mod._verify_candidate(proj, "s1", 2, {})
    tb = (run / "tb_tlc.v").read_text()
    assert "dut.state" in tb, "expected the 2nd repair to reintroduce it"
    from vlab.fix import white_box_refs
    assert "state" in white_box_refs(tb, ""), "detector must catch the drift"


def test_structural_defects_explain_a_dead_end():
    """The repair round must be told WHY, not just see a failing check."""
    from vlab.auto import _structural_defects

    design = "module tlc; reg [1:0] count; endmodule\n"
    tb = "module tb_tlc; while (dut.state !== 3'd2) @(posedge clk); endmodule\n"
    d = _structural_defects(design, tb)
    assert len(d) == 2
    assert any("too narrow" in x for x in d)
    assert any("white-box" in x for x in d)


def test_generation_warns_about_both_defects(tmp_path, monkeypatch):
    """They must reach the user in gen.json, not stay invisible."""
    import json as _json

    from vlab import llm as llm_mod
    from vlab.rtl_gen import run_gen

    proj = tmp_path / "p"
    spec = {
        "dut_name": "tlc",
        "data_width": 1,
        "ports": [{"name": "clk", "direction": "input", "width": 1},
                  {"name": "light", "direction": "output", "width": 1}],
    }
    sd = proj / "runs" / "s1"
    sd.mkdir(parents=True)
    (sd / "spec.json").write_text(_json.dumps(spec))

    payload = _json.dumps({
        "design": "module tlc(input clk, output reg [1:0] light);\n"
                  "  reg [1:0] count;\n"
                  "  always @(posedge clk) count <= count + 1'b1;\n"
                  "endmodule",
        "testbench": "module tb_tlc;\n"
                     "  reg clk=0; wire [1:0] light;\n"
                     "  tlc dut(.clk(clk), .light(light));\n"
                     "  always #5 clk=~clk;\n"
                     "  initial begin while (dut.state !== 3'd2) @(posedge clk);\n"
                     "    $display(\"PASS: x\"); $finish; end\n"
                     "endmodule",
        "notes": "",
    })
    monkeypatch.setattr(llm_mod, "get_api_key", lambda: "k")
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: payload)

    m = run_gen(proj, "s1", sd / "spec.json", use_llm=True)
    joined = " ".join(m["warnings"])
    assert "too narrow" in joined
    assert "white-box" in joined
    assert "dut.state" in joined


def test_prompt_states_both_rules():
    from vlab.rtl_gen import GEN_PROMPT

    assert "dut.<internal signal>" in GEN_PROMPT
    assert "wraps at 3" in GEN_PROMPT
    assert "observe ONLY the DUT's output ports" in GEN_PROMPT


def test_watchdog_makes_a_hanging_tb_reportable():
    """A bare `wait` that never completes must not silently time out.

    Mutation testing found a testbench that hung forever once a mutant
    removed a phase the TB waited for. The simulator was killed by an
    external timeout and the log contained no PASS and no FAIL, so the run
    could not say what happened.
    """
    from vlab.fix import add_sim_watchdog

    tb = (
        "`timescale 1ns/1ps\nmodule tb_d;\n"
        "  reg x;\n"
        "  task wait_for_phase; input p; begin wait (x === p); end endtask\n"
        "  initial begin $dumpfile(\"wave.vcd\"); $dumpvars(0, tb_d); $finish; end\n"
        "endmodule\n"
    )
    fixed, warns = add_sim_watchdog(tb, "d")
    assert "vlab watchdog" in fixed
    assert warns
    # the guard must be legal Verilog-2005: no adjacent string-literal
    # concatenation, which is SystemVerilog and is a syntax error in iverilog
    assert '" "' not in fixed.split("vlab watchdog fired")[-1][:200]
    # idempotent: sanitizing an already-watched TB must not double up
    again, w2 = add_sim_watchdog(fixed, "d")
    assert again == fixed
    assert w2 == []


def test_watchdog_budget_scales_with_timescale():
    """A fixed budget fired mid-run on fast testbenches and invented failures.

    Every offline testbench regression at once traced back to the watchdog
    using a constant that was shorter than some healthy runs.
    """
    from vlab.fix import add_sim_watchdog

    fast = "`timescale 1ns/1ps\nmodule tb_a; initial $finish; endmodule\n"
    slow = "`timescale 1ns/1ps\nmodule tb_b; initial $finish; endmodule\n"
    b1 = add_sim_watchdog(fast, "a")[0]
    b2 = add_sim_watchdog(slow, "b")[0]
    assert "#2000000" in b1 and "#2000000" in b2
    # a 1s-scale file must get a proportionally larger budget
    big = "`timescale 1s/1ns\nmodule tb_c; initial $finish; endmodule\n"
    assert "#2000000000000" in add_sim_watchdog(big, "c")[0]


def test_watchdog_lands_after_a_trailing_comment():
    """builtin_template ends its module with a comment; inserting after it
    produced `// ... initial begin` and broke every offline testbench."""
    from vlab.fix import add_sim_watchdog

    tb = (
        "`timescale 1ns/1ps\nmodule tb_d;\n"
        "  initial begin $display(\"DONE\"); $finish; end\n"
        "  // trailing comment right before endmodule\n"
        "endmodule\n"
    )
    fixed, _ = add_sim_watchdog(tb, "d")
    for line in fixed.splitlines():
        if "initial begin" in line:
            assert not line.strip().startswith("//"), (
                f"watchdog was swallowed by a comment: {line!r}"
            )


def test_watchdog_survives_sanitization_end_to_end():
    """A synthesized watchdog must actually compile and run."""
    import shutil
    import subprocess

    from vlab.config import resolve_tool, tool_env
    from vlab.fix import sanitize_testbench

    iverilog, vvp = resolve_tool("iverilog"), resolve_tool("vvp")
    if not (iverilog and vvp):
        pytest.skip("iverilog/vvp not installed")
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    try:
        (d / "design.v").write_text(
            "module d(input clk, input [7:0] a, output reg [7:0] y);\n"
            "  always @(posedge clk) y <= a + 1;\nendmodule\n"
        )
        tb = (
            "`timescale 1ns/1ps\nmodule tb_d;\n"
            "  reg clk=0; reg [7:0] a=0; wire [7:0] y;\n"
            "  d dut(.clk(clk), .a(a), .y(y));\n"
            "  always #5 clk=~clk;\n"
            "  initial begin $dumpfile(\"wave.vcd\"); $dumpvars(0, tb_d);\n"
            "    a=8'd4; #20;\n"
            "    if (y===8'd5) $display(\"PASS: inc\"); else $display(\"FAIL: inc\");\n"
            "    $finish; end\n"
            "endmodule\n"
        )
        fixed, _ = sanitize_testbench(tb, "d")
        (d / "tb_d.v").write_text(fixed)
        c = subprocess.run([iverilog, "-g2012", "-o", "s.vvp", "design.v", "tb_d.v"],
                           cwd=d, capture_output=True, text=True, env=tool_env())
        assert c.returncode == 0, f"watched TB failed to compile: {c.stderr}"
        v = subprocess.run([vvp, "s.vvp"], cwd=d, capture_output=True,
                           text=True, env=tool_env())
        assert "PASS: inc" in v.stdout
        assert "watchdog" not in v.stdout, "watchdog fired on a healthy run"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_watchdog_survives_sanitization():
    """sanitize_testbench is the path every TB takes, so wire it there."""
    from vlab.fix import sanitize_testbench

    tb = (
        "`timescale 1ns/1ps\nmodule tb_d;\n"
        "  initial begin $finish; end\n"
        "endmodule\n"
    )
    fixed, warns = sanitize_testbench(tb, "d")
    assert "vlab watchdog" in fixed


def test_sim_timeout_is_named_in_the_result():
    from vlab import sim as sim_mod

    r = sim_mod._parse_result("$ vvp rc=124\ntimeout\n", rc=124)
    assert r["status"] == "fail"
    assert "never terminated" in r["note"], (
        "a killed simulator must say why, not just report 0/0"
    )


def test_watchdog_fire_is_named_in_the_result():
    from vlab import sim as sim_mod

    log = ("FAIL: testbench watchdog fired after 2000000 ns\n"
           "$finish called at 2000000\n")
    r = sim_mod._parse_result(log, rc=0)
    assert r["status"] == "fail"
    assert "watchdog" in r["note"]


def test_synth_timeout_default_is_generous():
    """180s was tight enough to lose verified candidates."""
    import inspect

    from vlab import synth

    sig = inspect.signature(synth.run_synth)
    assert sig.parameters["timeout"].default is None  # env-driven, defaults 600
