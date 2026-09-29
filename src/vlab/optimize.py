"""SELECT + LEARN (board phase 6): constraints, pareto, memory, search."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

METRIC_KEYS = ("cells", "crit_ns", "power_w")


def candidate_metrics(
    status: str,
    area: dict[str, Any],
    timing: dict[str, Any],
    power: dict[str, Any],
) -> dict[str, Any]:
    """Normalize area/timing/power dicts into comparable metric floats.

    fmax comes from the *achievable* period (target period minus worst
    slack), not 1/crit_ns. 1/crit ignores the input/output delay budget and
    setup time, so it overstates fmax; period-minus-slack is the standard
    estimate for the next faster operating point. Falls back to 1/crit when
    the target period is unknown.
    """
    cells = area.get("cell_count", area.get("area_cells"))
    crit = timing.get("crit_ns")
    pw = power.get("power_w")
    fmax = None
    period = timing.get("period_ns")
    wns = timing.get("wns_ns")
    if (isinstance(period, (int, float)) and period > 0
            and isinstance(wns, (int, float))):
        budget = period - wns
        if budget > 0:
            fmax = round(1000.0 / budget, 2)
    if fmax is None and isinstance(crit, (int, float)) and crit > 0:
        fmax = round(1000.0 / crit, 2)
    return {
        "cells": cells if isinstance(cells, (int, float)) else None,
        "crit_ns": crit if isinstance(crit, (int, float)) else None,
        "power_w": pw if isinstance(pw, (int, float)) else None,
        "fmax_mhz": fmax,
        "wns_ns": timing.get("wns_ns"),
        "status": status,
    }


def check_constraints(
    metrics: dict[str, Any], limits: dict[str, Any]
) -> dict[str, Any]:
    """Compare metrics against spec limits. Missing metric/limit = unchecked (None).

    A metric that was never measured must NOT count as a violation. Without
    STA there is no fmax, and `expand_spec` always sets min_freq_mhz, so the
    old falsy check stamped every candidate feasible=false and ranked it
    below candidates that merely lacked constraint data.
    """
    res: dict[str, Any] = {}
    min_freq = limits.get("min_freq_mhz")
    max_cells = limits.get("max_cells")
    max_power = limits.get("max_power_w")
    fmax = metrics.get("fmax_mhz")
    cells = metrics.get("cells")
    power = metrics.get("power_w")
    res["freq_ok"] = None if fmax is None or min_freq is None else fmax >= min_freq
    res["area_ok"] = None if cells is None or max_cells is None else cells <= max_cells
    res["power_ok"] = None if power is None or max_power is None else power <= max_power
    checks = [v for v in res.values() if v is not None]
    res["feasible"] = all(checks) if checks else None
    return res


def dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if a is no-worse on all *comparable* metrics and strictly better on >=1.

    Lower is better for cells/crit/power. A metric missing on either side is
    skipped rather than treated as a disqualifier: bailing out on the first
    None put every candidate on the Pareto front whenever STA or the VCD was
    unavailable, which made search_guidance tell the model that all designs
    were simultaneously optimal. A dominance claim still requires at least
    one shared metric, so two fully-unmeasured candidates never "dominate".
    """
    better = False
    compared = 0
    for k in METRIC_KEYS:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            continue
        compared += 1
        if av > bv:
            return False
        if av < bv:
            better = True
    return better and compared > 0


def pareto_frontier(cands: list[dict[str, Any]]) -> list[int]:
    """Indices of non-dominated candidates (sim-passing only)."""
    passing = [i for i, c in enumerate(cands) if c.get("status") == "pass"]
    front = []
    for i in passing:
        if not any(
            dominates(cands[j], cands[i]) for j in passing if j != i
        ):
            front.append(i)
    return front


def pick_winner(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best passing candidate by rank order, else None (never a failed one)."""
    for i in rank_candidates(cands):
        if cands[i].get("status") == "pass":
            return cands[i]
    return None


def rank_candidates(cands: list[dict[str, Any]]) -> list[int]:
    """Best-first order: feasible > passing; then power, crit, cells (None last)."""

    def key(i: int) -> tuple:
        c = cands[i]
        feas = c.get("constraints", {}).get("feasible")
        return (
            0 if c.get("status") == "pass" else 1,
            0 if feas else (1 if feas is None else 2),
            c.get("power_w") if c.get("power_w") is not None else float("inf"),
            c.get("crit_ns") if c.get("crit_ns") is not None else float("inf"),
            c.get("cells") if c.get("cells") is not None else float("inf"),
        )

    return sorted(range(len(cands)), key=key)


def memory_append(mem_path: Path, entry: dict[str, Any]) -> None:
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mem_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def memory_read(mem_path: Path, tail: int = 20) -> list[dict[str, Any]]:
    if not mem_path.exists():
        return []
    lines = mem_path.read_text().splitlines()
    out = []
    for line in lines[-tail:]:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def search_guidance(
    spec: dict[str, Any],
    pareto_idx: list[int],
    cands: list[dict[str, Any]],
) -> str:
    """Ask the LLM what to try next from measured results. Falls back to heuristic."""
    summary = {
        "dut": spec.get("dut_name"),
        "limits": spec.get("limits", {}),
        "candidates": [
            {
                "id": i,
                "metrics": {k: c.get(k) for k in (*METRIC_KEYS, "fmax_mhz", "status")},
                "constraints": c.get("constraints"),
                "pareto": i in pareto_idx,
            }
            for i, c in enumerate(cands)
        ],
    }
    try:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            raise RuntimeError("no key")
        out = llm_chat(
            [
                {
                    "role": "user",
                    "content": (
                        "You guide RTL design-space exploration. Given measured "
                        "candidates (lower cells/crit_ns/power_w is better), reply "
                        "with 2-4 short concrete mutation directions for the next "
                        "generation (e.g. pipeline stage, resource sharing, "
                        "encoding change). Plain lines, no prose.\n"
                        f"{json.dumps(summary, indent=1)}"
                    ),
                }
            ],
            max_tokens=300,
            temperature=0.5,
        )
        lines = [l.strip("- *\t") for l in out.splitlines() if len(l.strip()) > 8]
        if lines:
            return "\n".join(lines[:4])
    except Exception:
        pass
    # heuristic fallback: push on worst violator
    return "Vary micro-architecture (pipelining, sharing, encoding) to trade off the worst metric."
