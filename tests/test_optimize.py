"""Unit tests for SELECT+LEARN (optimize.py). No EDA, no LLM."""
from vlab.optimize import (
    candidate_metrics,
    check_constraints,
    dominates,
    memory_append,
    memory_read,
    pareto_frontier,
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
