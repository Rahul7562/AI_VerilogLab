"""vlab auto: single prompt in -> spec -> N candidates -> verify/fix ->
synth/ppa -> select/learn loop -> final. Board phases 1-7 in one command."""
from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path
from typing import Any

from .config import runs_dir, session_dir
from .optimize import (
    candidate_metrics,
    check_constraints,
    memory_append,
    memory_read,
    pareto_frontier,
    pick_winner,
    search_guidance,
)
from .spec import heuristic_pdk, llm_pick_pdk, validate_spec

SPEC_EXPAND_PROMPT = """You expand a one-line RTL design request into a full DUT spec.
Output ONLY JSON, no prose, with exactly these keys:
{{"dut_name": "<valid Verilog identifier>", "description": "<1-2 sentences>",
"category": "<combinational|sequential|fsm|memory|interface>",
"data_width": <int>, "clock": "<clk port or ''>", "reset": "<e.g. 'rst_n low' or ''>",
"ports": [{{"name": "...", "direction": "input|output|inout", "width": <int>}}],
"features": ["..."], "test_plan": ["..."], "target_freq_mhz": <number>,
"limits": {{"min_freq_mhz": <number>}},
"suggested_candidates": <int 1-4: 1 for trivial blocks, 2-3 when architecture
trade-offs exist (speed vs area vs power), 4 for complex datapaths>}}
Rules: ports must include clock/reset when sequential; combinational designs omit them.
Decide data_width from the request (default 8 when unspecified).
Request: {prompt}
Width hint: {width}"""


def _sid(dut: str) -> str:
    return f"{dut}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"


CLARIFY_PROMPT = """You review a one-line RTL design request. If it lacks what an
RTL engineer needs (function, interface/widths, clock/reset, key behaviors),
reply with ONLY JSON: {{"complete": false, "questions": ["<at most 4 short, specific questions>"]}}.
If it is sufficient, reply with ONLY JSON: {{"complete": true, "questions": []}}.
Request: {prompt}"""


def assess_prompt(prompt: str, use_llm: bool) -> list[str]:
    """Return clarifying questions when the prompt is under-specified."""
    if not use_llm:
        return []
    try:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            return []
        raw = llm_chat(
            [{"role": "user", "content": CLARIFY_PROMPT.format(prompt=prompt)}],
            max_tokens=300,
            temperature=0.2,
        )
        obj = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        if obj.get("complete", True):
            return []
        return [str(q) for q in obj.get("questions", []) if str(q).strip()][:4]
    except Exception:
        return []


def expand_spec(
    prompt: str, dut_hint: str | None, width: int | None, use_llm: bool
) -> dict[str, Any]:
    """Build a validated spec from a one-line prompt (LLM or template).

    width None = let the LLM decide (offline: default 8).
    """
    if use_llm:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            raise RuntimeError("OPENROUTER_API_KEY not set. Use --no-llm.")
        raw = llm_chat(
            [{"role": "user", "content": SPEC_EXPAND_PROMPT.format(
                prompt=prompt, width=width if width else "auto")}],
            max_tokens=1200,
            temperature=0.2,
        )
        try:
            spec = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(
                f"LLM spec expand parse failed. Raw head: {raw[:200]!r}"
            )
        if dut_hint:
            from .spec import validate_module_name

            spec["dut_name"] = validate_module_name(dut_hint)
        spec["data_width"] = width or spec.get("data_width", 8)
        spec.setdefault("suggested_candidates", 1)
        spec["pdk_choice"] = "auto"
        feats = ", ".join(spec.get("features", []))
        selected, reason = llm_pick_pdk(spec["description"], feats, "auto")
    else:
        from .spec import default_spec

        w = width or 8
        spec = default_spec(dut_hint or "my_dut")
        spec.update(
            {
                "description": prompt,
                "data_width": w,
                "ports": [
                    {"name": "clk", "direction": "input", "width": 1},
                    {"name": "rst_n", "direction": "input", "width": 1},
                    {"name": "a", "direction": "input", "width": w},
                    {"name": "b", "direction": "input", "width": w},
                    {"name": "y", "direction": "output", "width": w},
                ],
                "features": ["datapath", "reset"],
                "test_plan": ["directed basics", "reset check"],
                "target_freq_mhz": 100,
                "limits": {"min_freq_mhz": 100},
                "suggested_candidates": 1,
            }
        )
        selected, reason = heuristic_pdk(prompt, "")
    spec["pdk_selected"] = selected
    spec["pdk_reason"] = reason
    spec.setdefault("limits", {"min_freq_mhz": spec.get("target_freq_mhz", 100)})
    return validate_spec(spec)


def _sim_passes(outdir: Path) -> tuple[str, int, int]:
    try:
        sim = json.loads((outdir / "sim.json").read_text())
        return sim.get("status", "unknown"), sim.get("pass_count", 0), sim.get("fail_count", 0)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return "unknown", 0, 0


def _verify_candidate(
    project_dir: Path,
    cid: str,
    max_fix_rounds: int,
    golden: dict[str, Any],
) -> dict[str, Any]:
    """Simulate + LLM-repair working TB (never the frozen golden)."""
    from .fix import _extract_tb, repair_prompt
    from .llm import chat as llm_chat
    from .sim import run_simulate

    outdir = session_dir(project_dir, cid)
    dut = json.loads((outdir / "spec.json").read_text())["dut_name"]
    design = (outdir / "design.v").read_text()
    repairs = 0
    # freeze golden on first PASS (tb + pass count as anti-weakening bar)
    for _ in range(max_fix_rounds + 1):
        sim = run_simulate(project_dir, cid, tool="auto")
        status, passes, fails = sim["status"], sim["pass_count"], sim["fail_count"]
        if status == "pass":
            if not golden.get("passes"):
                tb_src = next(iter(sorted(outdir.glob("tb_*.v"))))
                shutil.copy(tb_src, outdir / f"golden_tb_{dut}.v")
                golden.update({"passes": passes, "tb": str(tb_src)})
            elif passes < golden["passes"]:
                return {
                    "status": "fail", "passes": passes, "fails": fails,
                    "repairs": repairs,
                    "note": f"repair weakened TB ({passes} < golden {golden['passes']} passes)",
                }
            return {"status": "pass", "passes": passes, "fails": fails, "repairs": repairs}
        if repairs >= max_fix_rounds:
            return {"status": "fail", "passes": passes, "fails": fails, "repairs": repairs}
        tb = next(iter(sorted(outdir.glob("tb_*.v")))).read_text()
        simlog = outdir / "sim.log"
        log = simlog.read_text() if simlog.exists() else "(no sim log)"
        try:
            raw = llm_chat(
                [{"role": "user", "content": repair_prompt(design, tb, log, dut)}],
                max_tokens=6000,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001 - LLM outage ends repairs
            return {
                "status": "fail", "passes": passes, "fails": fails,
                "repairs": repairs, "note": f"llm call failed: {e}"[:200],
            }
        (outdir / f"fix_r{repairs + 1}_raw.txt").write_text(raw)
        try:
            fixed = _extract_tb(raw, dut)
        except ValueError:
            repairs += 1  # truncated/garbled output: count round, retry
            continue
        (outdir / f"tb_{dut}.v").write_text(fixed)
        repairs += 1
    return {"status": "fail", "passes": 0, "fails": 0, "repairs": repairs}


def run_auto(
    project_dir: Path,
    prompt: str,
    dut: str | None = None,
    width: int | None = None,
    candidates: int | None = None,
    iterations: int = 1,
    max_fix_rounds: int = 3,
    use_llm: bool = True,
    freq_mhz: float = 100.0,
) -> dict[str, Any]:
    """Full autonomous loop. width/candidates None = decided by the LLM."""
    from .ppa import run_ppa
    from .report import build_report
    from .rtl_gen import run_gen
    from .synth import run_synth

    if candidates is not None and candidates < 1:
        raise ValueError("candidates must be >= 1")
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if max_fix_rounds < 0:
        raise ValueError("max_fix_rounds must be >= 0")

    spec = expand_spec(prompt, dut, width, use_llm)
    # None = LLM decided: width lives in spec, candidates in suggested_candidates
    width = width or int(spec.get("data_width", 8))
    try:
        candidates = candidates or int(spec.get("suggested_candidates", 1))
    except (TypeError, ValueError):
        candidates = 1
    candidates = max(1, min(8, candidates))
    spec["data_width"] = width
    sid = _sid(spec["dut_name"])
    rdir = runs_dir(project_dir) / sid
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "spec.json").write_text(json.dumps(spec, indent=2))
    sdir = project_dir / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "spec.json").write_text(json.dumps(spec, indent=2))
    mem = rdir / "memory.jsonl"
    golden: dict[str, Any] = {}
    guidance = ""
    all_cands: list[dict[str, Any]] = []

    for it in range(1, iterations + 1):
        for c in range(1, candidates + 1):
            cid = f"{sid}__i{it}c{c}"
            entry: dict[str, Any] = {"id": cid, "iter": it, "cand": c}
            try:
                cdir = session_dir(project_dir, cid)
                shutil.copy(rdir / "spec.json", cdir / "spec.json")
                if use_llm:
                    run_gen(project_dir, cid, cdir / "spec.json", use_llm=True, guidance=guidance)
                else:
                    run_gen(project_dir, cid, cdir / "spec.json", use_llm=False)
                ver = _verify_candidate(project_dir, cid, max_fix_rounds, golden) if use_llm else _offline_verify(project_dir, cid)
                entry.update({
                    "status": ver["status"], "passes": ver.get("passes", 0),
                    "repairs": ver.get("repairs", 0), "note": ver.get("note", ""),
                })
            except Exception as e:  # noqa: BLE001 - one bad candidate must not kill the run
                entry.update({"status": "error", "passes": 0, "repairs": 0,
                              "note": f"{type(e).__name__}: {e}"[:200]})
            if entry.get("status") == "pass":
                try:
                    syn = run_synth(project_dir, cid)
                    ppa = run_ppa(project_dir, cid, freq_mhz=freq_mhz)
                    m = candidate_metrics("pass", syn["area"], ppa["timing"], ppa["power"])
                    entry.update(m)
                    entry["constraints"] = check_constraints(m, spec.get("limits", {}))
                except (RuntimeError, FileNotFoundError) as e:
                    entry["status"] = "error"
                    entry["note"] = str(e)[:200]
            all_cands.append(entry)
            memory_append(mem, {"sid": sid, **entry})
        front = pareto_frontier(all_cands)
        if it < iterations:
            guidance = search_guidance(spec, memory_read(mem), front, all_cands)

    winner = pick_winner(all_cands)
    final_dir = None
    if winner and winner["status"] == "pass":
        final_dir = rdir / "final"
        final_dir.mkdir(exist_ok=True)
        wdir = session_dir(project_dir, winner["id"])
        artifacts = ["spec.json", "design.v", "gen.json", "sim.json", "sim.log",
                     "wave.vcd", "synth.json", "synth.log", "netlist.v",
                     "netlist.json", "area.json", "ppa.json"]
        for f in artifacts:
            src = wdir / f
            if src.exists():
                shutil.copy(src, final_dir / f)
                if f != "spec.json":
                    shutil.copy(src, rdir / f)
        for tb in wdir.glob("tb_*.v"):
            shutil.copy(tb, final_dir / tb.name)
            shutil.copy(tb, rdir / tb.name)
        for gtb in wdir.glob("golden_tb_*.v"):
            shutil.copy(gtb, final_dir / gtb.name)
        build_report(final_dir)
    summary = {
        "session": sid,
        "dut": spec["dut_name"],
        "iterations": iterations,
        "candidates": all_cands,
        "pareto": pareto_frontier(all_cands),
        "winner": winner["id"] if winner else None,
        "final": str(final_dir) if final_dir else None,
    }
    (rdir / "auto.json").write_text(json.dumps(summary, indent=2))
    _write_comparison(rdir, all_cands, summary)
    return summary


def _offline_verify(project_dir: Path, cid: str) -> dict[str, Any]:
    from .sim import run_simulate

    sim = run_simulate(project_dir, cid, tool="auto")
    return {"status": sim["status"], "passes": sim["pass_count"],
            "fails": sim["fail_count"], "repairs": 0}


def _write_comparison(rdir: Path, cands: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [f"# Auto run {summary['session']} — candidate comparison", "",
             "| id | status | passes | repairs | cells | crit_ns | power_w | fmax | feasible |",
             "|---|---|---|---|---|---|---|---|---|"]
    for c in cands:
        lines.append(
            f"| {c['id'].split('__')[-1]} | {c['status']} | {c.get('passes', '-')} | "
            f"{c.get('repairs', 0)} | {c.get('cells', '-')} | {c.get('crit_ns', '-')} | "
            f"{c.get('power_w', '-')} | {c.get('fmax_mhz', '-')} | "
            f"{(c.get('constraints') or {}).get('feasible', '-')} |"
        )
    lines += ["", f"Winner: `{summary['winner']}`" if summary["winner"]
              else "Winner: none — all candidates failed (see sim.log in each run dir)",
              f"Pareto: {[cands[i]['id'].split('__')[-1] for i in summary['pareto']]}"]
    (rdir / "COMPARISON.md").write_text("\n".join(lines) + "\n")
