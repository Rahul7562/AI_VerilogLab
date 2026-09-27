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
    """Normalize area/timing/power dicts into comparable metric floats."""
    cells = area.get("cell_count", area.get("area_cells"))
    crit = timing.get("crit_ns")
    pw = power.get("power_w")
    fmax = round(1000.0 / crit, 2) if isinstance(crit, (int, float)) and crit > 0 else None
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
    """Compare metrics against spec limits. Missing metric/limit = unchecked (None)."""
    res: dict[str, Any] = {}
    min_freq = limits.get("min_freq_mhz")
    max_cells = limits.get("max_cells")
    max_power = limits.get("max_power_w")
    fmax = metrics.get("fmax_mhz")
    res["freq_ok"] = (fmax is not None and fmax >= min_freq) if min_freq else None
    res["area_ok"] = (
        metrics["cells"] is not None and metrics["cells"] <= max_cells
    ) if max_cells else None
    res["power_ok"] = (
        metrics["power_w"] is not None and metrics["power_w"] <= max_power
    ) if max_power else None
    checks = [v for v in res.values() if v is not None]
    res["feasible"] = all(checks) if checks else None
    return res


def dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if a is no-worse on all metrics and strictly better on >=1.

    Lower is better for cells/crit/power. None (missing) never dominates.
    """
    better = False
    for k in METRIC_KEYS:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            return False
        if av > bv:
            return False
        if av < bv:
            better = True
    return better


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
    memory: list[dict[str, Any]],
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
