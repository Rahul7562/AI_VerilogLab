"""Unit tests for SELECT+LEARN (optimize.py). No EDA, no LLM."""
from vlab.optimize import (
    candidate_metrics,
    check_constraints,
    dominates,
    memory_append,
    memory_read,
    pareto_frontier,
    pick_winner,
    rank_candidates,
)


def _c(status="pass", cells=10, crit=2.0, power=1e-4, feasible=None):
    c = {"status": status, "cells": cells, "crit_ns": crit,
         "power_w": power, "fmax_mhz": 500.0}
    if feasible is not None:
        c["constraints"] = {"feasible": feasible}
    return c


def test_dominates():
    a = {"cells": 5, "crit_ns": 1.0, "power_w": 1e-4}
    b = {"cells": 9, "crit_ns": 2.0, "power_w": 2e-4}
    assert dominates(a, b) and not dominates(b, a)
    assert not dominates(a, dict(a))  # equal => no dominance
    assert not dominates({"cells": None, "crit_ns": 1.0, "power_w": 1e-4}, b)


def test_pareto_frontier():
    cands = [_c(cells=5, crit=3.0), _c(cells=9, crit=1.0), _c(cells=9, crit=3.0)]
    assert sorted(pareto_frontier(cands)) == [0, 1]
    cands[0]["status"] = "fail"
    assert pareto_frontier(cands) == [1]


def test_rank_prefers_feasible_then_power():
    cands = [_c(power=5e-4, feasible=True), _c(power=1e-4, feasible=False),
             {"status": "fail", "power_w": 1e-9, "crit_ns": 0.1, "cells": 1}]
    assert rank_candidates(cands)[0] == 0


def test_pick_winner_never_failed():
    assert pick_winner([{"status": "fail"}, {"status": "error"}]) is None
    assert pick_winner([]) is None
    cands = [_c(power=5e-4, feasible=True), _c(power=1e-4, feasible=False)]
    assert pick_winner(cands) is cands[0]


def test_check_constraints():
    m = {"fmax_mhz": 200.0, "cells": 50, "power_w": 1e-4}
    r = check_constraints(m, {"min_freq_mhz": 100, "max_cells": 60, "max_power_w": 1e-3})
    assert r == {"freq_ok": True, "area_ok": True, "power_ok": True, "feasible": True}
    r = check_constraints(m, {"min_freq_mhz": 300})
    assert r["freq_ok"] is False and r["feasible"] is False
    r = check_constraints({"fmax_mhz": None, "cells": None, "power_w": None}, {})
    assert r["feasible"] is None


def test_candidate_metrics_fmax():
    m = candidate_metrics("pass", {"cell_count": 10}, {"crit_ns": 4.0}, {"power_w": 2e-5})
    assert m["fmax_mhz"] == 250.0
    m = candidate_metrics("pass", {}, {"crit_ns": None}, {})
    assert m["fmax_mhz"] is None and m["cells"] is None


def test_memory_roundtrip(tmp_path):
    p = tmp_path / "m.jsonl"
    assert memory_read(p) == []
    memory_append(p, {"a": 1})
    memory_append(p, {"a": 2})
    assert [e["a"] for e in memory_read(p)] == [1, 2]


def test_assess_prompt(monkeypatch):
    import vlab.auto as auto_mod
    import vlab.llm as llm_mod

    assert auto_mod.assess_prompt("anything", False) == []
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: '{"complete": true, "questions": []}')
    assert auto_mod.assess_prompt("8-bit counter with enable", True) == []
    monkeypatch.setattr(
        llm_mod, "chat",
        lambda *a, **k: '{"complete": false, "questions": ["Sync or async reset?", "Width?"]}')
    assert auto_mod.assess_prompt("counter", True) == ["Sync or async reset?", "Width?"]
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert auto_mod.assess_prompt("counter", True) == []


def test_expand_offline_defaults():
    from vlab.auto import expand_spec

    spec = expand_spec("8-bit adder", "alu8", None, False)
    assert spec["data_width"] == 8
    assert spec["suggested_candidates"] == 1
