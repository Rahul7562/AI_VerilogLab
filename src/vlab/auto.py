"""vlab auto: single prompt in -> spec -> N candidates -> verify/fix ->
synth/ppa -> select/learn loop -> final. Board phases 1-7 in one command."""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import runs_dir, session_dir
from .optimize import (
    candidate_metrics,
    check_constraints,
    memory_append,
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


SPEC_REVIEW_PROMPT = """You review a DUT spec for internal contradictions. Check: reset behavior
vs initial state, port directions vs features, test plan vs described behavior,
clock/reset presence vs category. Output ONLY JSON with exactly two keys:
{{"issues": ["<each contradiction found>"], "spec": <the FULL corrected spec JSON>}}.
Fix contradictions minimally (prefer explicit test-plan statements). If none found,
return the spec unchanged with "issues": [].
Spec:
{spec}"""


def review_spec(spec: dict[str, Any], use_llm: bool) -> tuple[dict[str, Any], list[str]]:
    """LLM self-review of the spec; falls back to the original on any failure."""
    if not use_llm:
        return spec, []
    try:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            return spec, []
        raw = llm_chat(
            [{"role": "user",
              "content": SPEC_REVIEW_PROMPT.format(spec=json.dumps(spec))}],
            max_tokens=1500,
            temperature=0.1,
        )
        obj = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        fixed = validate_spec(obj["spec"])
        issues = [str(i) for i in obj.get("issues", [])]
        fixed["review_notes"] = issues
        return fixed, issues
    except Exception:
        return spec, []


# session ids claimed in this process, so two threads starting in the same
# second cannot both pick the free one
_SID_CLAIMED: set[str] = set()
_SID_LOCK = threading.Lock()


def _sid(dut: str, project_dir: Path | None = None) -> str:
    """Claim a unique session id: <dut>_<timestamp>[_n].

    Two-second resolution alone collides when two runs start in the same
    second — which the web dashboard can do, since it runs them on parallel
    threads. A collision makes both runs share one directory and overwrite
    each other's spec/candidates. The check-and-claim happens under one lock
    so two threads cannot both select the same free id.
    """
    base = f"{dut}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    with _SID_LOCK:
        sid = base
        n = 0
        while sid in _SID_CLAIMED or (
            project_dir is not None and (runs_dir(project_dir) / sid).exists()
        ):
            n += 1
            if n > 50:  # pathological; use something certainly unique
                sid = f"{base}_{uuid.uuid4().hex[:6]}"
                break
            sid = f"{base}_{n}"
        _SID_CLAIMED.add(sid)
        return sid


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
        # A free-tier model can reply with a list, a bare string, or an object
        # missing keys. Dereferencing before validating turned those into
        # TypeError/KeyError, which escaped as a raw traceback.
        if not isinstance(spec, dict):
            raise RuntimeError(
                f"LLM spec expand returned {type(spec).__name__}, not an object"
            )
        if dut_hint:
            from .spec import validate_module_name

            spec["dut_name"] = validate_module_name(dut_hint)
        spec.setdefault("description", prompt.strip()[:500] or "unspecified DUT")
        spec.setdefault("ports", [])
        spec.setdefault("features", [])
        if not spec["ports"]:
            raise RuntimeError(
                "LLM spec expand returned no ports; cannot generate RTL. "
                "Re-run, or pass --dut/--width and use `vlab new` instead."
            )
        spec["data_width"] = width or spec.get("data_width", 8)
        spec.setdefault("suggested_candidates", 1)
        spec["pdk_choice"] = "auto"
        feats = ", ".join(str(f) for f in spec.get("features", []))
        selected, reason = llm_pick_pdk(str(spec["description"]), feats, "auto")
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
    """Simulate + LLM-repair (DUT on compile errors, else working TB)."""
    from .fix import (
        _extract_design,
        _extract_pair,
        _extract_tb,
        _failing_checks,
        _norm,
        _require_design_intact,
        adjudicate_target,
        design_behavior_repair_prompt,
        design_repair_prompt,
        is_compile_fail,
        pair_repair_prompt,
        repair_prompt,
        repair_target,
        sanitize_design,
        sanitize_testbench,
    )
    from .llm import chat as llm_chat
    from .rtl_gen import gen_max_tokens
    from .sim import run_simulate

    outdir = session_dir(project_dir, cid)
    spec = json.loads((outdir / "spec.json").read_text())
    dut = spec["dut_name"]
    repairs = 0
    last_err = ""
    passes = fails = 0
    history: list[str] = []
    # Set when the spec-coverage gate rejects an otherwise-passing candidate:
    # `target`/`why` are then already chosen and the round-budget check below
    # has already been applied, so adjudication must not overwrite them.
    rounds_left = False
    # freeze golden on first PASS (tb + pass count as anti-weakening bar)
    for _ in range(max_fix_rounds + 1):
        design = (outdir / "design.v").read_text()
        sim = run_simulate(project_dir, cid, tool="auto")
        status, passes, fails = sim["status"], sim["pass_count"], sim["fail_count"]
        tb_files = sorted(outdir.glob("tb_*.v"))
        tb_path = next(iter(tb_files))
        tb = tb_path.read_text()
        simlog = outdir / "sim.log"
        log = simlog.read_text() if simlog.exists() else "(no sim log)"
        rounds_left = False

        # Structural defects that compile and simulate but cannot converge.
        # Detecting them here means the repair round is told the real cause
        # instead of staring at a check failure that no edit can fix.
        structural = _structural_defects(design, tb)
        if structural and status != "pass":
            history.append(
                "structural defect: " + "; ".join(structural)[:220]
            )

        if status == "pass":
            # A single passing assertion is not verification. Measured: a
            # candidate was published as a winner with 1 check and 0
            # failures, because sim._parse_result accepts any run with at
            # least one PASS. Force it back through repair with the shortfall
            # as the diagnosis rather than shipping an unverified design.
            need = _min_checks(spec)
            if passes < need:
                if repairs >= max_fix_rounds:
                    return {
                        "status": "fail", "passes": passes, "fails": fails,
                        "repairs": repairs,
                        "note": (f"under-verified: only {passes} passing check(s), "
                                 f"need >= {need} — the testbench is too thin to "
                                 f"establish the design")[:300],
                    }
                target, why = (
                    "testbench",
                    f"the testbench ran only {passes} check(s); a verified "
                    f"candidate needs at least {need} directed checks "
                    f"covering every spec feature — add the missing ones",
                )
                rounds_left = True
                history.append(why)
            # A simulation pass only proves the DUT agrees with the
            # testbench. Before a candidate can be published, check that
            # the pair actually covers the spec -- otherwise a design can
            # pass 19/19 checks while missing every timing requirement the
            # testbench never bothered to measure.
            spec_gaps: list[dict[str, Any]] = []
            if spec.get("features") and not rounds_left:
                from .conform import review_coverage

                conform, spec_gaps = review_coverage(design, tb, log, spec)
                if not conform:
                    # Do NOT publish it, but also do not give up: fall
                    # through to the repair path with the gaps as the
                    # diagnosis so the loop gets a chance to close them.
                    gap_text = "; ".join(
                        f"{g.get('kind', 'gap')}: {g.get('feature', '')}"
                        + (f" ({g.get('why', '')})" if g.get("why") else "")
                        for g in spec_gaps
                    )
                    if repairs >= max_fix_rounds:
                        return {
                            "status": "fail", "passes": passes, "fails": fails,
                            "repairs": repairs,
                            "note": f"spec gaps remain: {gap_text}"[:300],
                            "spec_gaps": spec_gaps[:8],
                        }
                    # An unimplemented feature lives in the DUT; an
                    # unverified one lives in the testbench. Re-derive both
                    # when the gaps are mixed.
                    kinds = {g.get("kind") for g in spec_gaps}
                    target = (
                        "design" if kinds == {"unimplemented"}
                        else "testbench" if kinds <= {"unchecked"}
                        else "both"
                    )
                    why = f"spec coverage gaps: {gap_text}"[:300]
                    history.append(
                        f"round {repairs + 1}: spec gaps -> {gap_text[:200]}"
                    )
                    rounds_left = True
            if not rounds_left:
                if not golden.get("passes"):
                    # first passing candidate defines the bar
                    shutil.copy(tb_path, outdir / f"golden_tb_{dut}.v")
                    golden.update({"passes": passes, "tb": str(tb_path)})
                elif repairs and passes < golden["passes"]:
                    # Anti-weakening only applies to a candidate WE repaired.
                    # Comparing an independent candidate against the first
                    # candidate's check count just biased selection toward
                    # whichever design happened to be generated first — an
                    # untouched TB that legitimately has fewer checks is not a
                    # weakened one.
                    return {
                        "status": "fail", "passes": passes, "fails": fails,
                        "repairs": repairs,
                        "note": f"repair weakened TB ({passes} < golden {golden['passes']} passes)",
                    }
                return {"status": "pass", "passes": passes, "fails": fails,
                        "repairs": repairs}
        elif repairs >= max_fix_rounds:
            # Report WHY the candidate ran out of rounds. Returning a bare
            # fail with no note made every exhausted candidate look identical
            # in the dashboard, which is exactly when the operator most needs
            # to know whether it was a stuck loop or a wrong design.
            remaining = _failing_checks(log) if log else []
            return {
                "status": "fail", "passes": passes, "fails": fails,
                "repairs": repairs,
                "note": (
                    f"exhausted {max_fix_rounds} repair round(s); "
                    f"{len(remaining)} check(s) still failing"
                    + (f": {remaining[0][:120]}" if remaining else "")
                )[:300],
            }

        # --- choose which file to repair, judged against the SPEC ---
        # A runtime FAIL used to mean "blame the testbench", which converged a
        # spec-violating DUT with a TB rewritten to match it and then published
        # the pair as a winner. Adjudicate against the spec instead. A compile
        # failure has an unambiguous culprit, so it skips the extra call.
        # `target` is preset only when the spec-coverage gate above rejected a
        # passing candidate; otherwise pick it here.
        if is_compile_fail(sim, log):
            target = repair_target(sim, log)
            why = "compile error attribution"
        elif not rounds_left:
            target, reason = adjudicate_target(design, tb, log, spec, dut)
            why = f"adjudicated: {reason}"
        # else: target/why were set by the spec-coverage gate

        entry_note = ""
        try:
            if target == "both":
                raw = llm_chat(
                    [{"role": "user", "content": pair_repair_prompt(
                        design, tb, log, spec, dut)}],
                    max_tokens=gen_max_tokens(),
                    temperature=0.2,
                )
            elif target == "design":
                # A design verdict has two very different meanings. If it
                # does not compile, that is a syntax job. If it compiles and
                # merely behaves wrongly, the compile-error prompt is
                # actively harmful: it orders the model not to change
                # behaviour, so it returns the file unchanged and the loop
                # aborts on a no-op.
                design_prompt = (
                    design_repair_prompt(design, log, dut, spec.get("ports", []))
                    if is_compile_fail(sim, log)
                    else design_behavior_repair_prompt(
                        design, tb, log, spec, dut, reason, history=history,
                    )
                )
                raw = llm_chat(
                    [{"role": "user", "content": design_prompt}],
                    max_tokens=gen_max_tokens(),
                    temperature=0.2,
                )
            else:
                raw = llm_chat(
                    [{"role": "user", "content": repair_prompt(
                        design, tb, log, dut, history=history)}],
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
            if target == "both":
                new_design, new_tb = _extract_pair(raw, dut)
                _require_design_intact(new_design, dut, spec.get("ports", []))
                new_design, dw = sanitize_design(new_design, dut)
                new_tb, tw = sanitize_testbench(new_tb, dut)
                (outdir / "design.v").write_text(new_design)
                (outdir / f"tb_{dut}.v").write_text(new_tb)
                entry_note = "; ".join(dw + tw)
            elif target == "design":
                fixed = _extract_design(raw, dut)
                _require_design_intact(fixed, dut, spec.get("ports", []))
                fixed, _ = sanitize_design(fixed, dut)
                (outdir / "design.v").write_text(fixed)
            else:
                fixed, san_warns = sanitize_testbench(_extract_tb(raw, dut), dut)
                if san_warns:
                    entry_note = "; ".join(san_warns)
                (outdir / f"tb_{dut}.v").write_text(fixed)
        except ValueError as e:
            last_err = str(e)[:200]
            repairs += 1  # truncated/garbled output: count round, retry
            continue
        # unchanged output means the model is not converging on this file;
        # further identical rounds just burn rate-limit budget. Compare on
        # normalized text: sanitize_testbench always prepends a `timescale.
        if target == "both":
            changed = (
                _norm((outdir / "design.v").read_text()) != _norm(design)
                or _norm((outdir / f"tb_{dut}.v").read_text()) != _norm(tb)
            )
        elif target == "design":
            changed = _norm((outdir / "design.v").read_text()) != _norm(design)
        else:
            changed = _norm((outdir / f"tb_{dut}.v").read_text()) != _norm(tb)
        if not changed:
            return {"status": "fail", "passes": passes, "fails": fails,
                    "repairs": repairs,
                    "note": f"repair reproduced the {target} unchanged ({why})"}

        # Re-check the structural rules on what the repair ACTUALLY produced.
        # Generation is prompt-driven, but a repair round is a local edit, and
        # observed in practice: a clean first draft was repaired into a
        # white-box testbench (`while (dut.state !== ...)`) because the
        # repair prompt did not carry the black-box rule. Catching it here
        # feeds it straight back into the next round.
        new_def = (outdir / "design.v").read_text()
        new_tb = (outdir / f"tb_{dut}.v").read_text()
        drift = _structural_defects(new_def, new_tb)
        if drift:
            history.append(
                f"round {repairs + 1}: repair reintroduced -> "
                + "; ".join(drift)[:220]
            )
        repairs += 1
        last_err = f"{target}: {why}{('; ' + entry_note) if entry_note else ''}"
        history.append(
            f"round {repairs}: repaired the {target}"
            + (f" ({entry_note})" if entry_note else "")
            + f"; still failing: {_failing_checks(log)[:2] or 'unknown'}"
        )
    # `passes`/`fails` are the live values from the last simulation; do not
    # fabricate zeros here or auto.json records a result that never happened
    return {"status": "fail", "passes": passes, "fails": fails,
            "repairs": repairs, "note": last_err}


def _min_checks(spec: dict[str, Any]) -> int:
    """How many passing checks a candidate must show to count as verified.

    `sim._parse_result` treats any run with at least one PASS as a pass, so a
    testbench that asserts once and never fails was published as a winner —
    measured: a "winner" carrying a single check and zero failures. That is
    not verification, and it is exactly the kind of green that gets trusted.

    The floor is the testbench contract the prompt already states (>= 5
    directed checks) and never fewer than there are spec features, since one
    feature with no check is unverified by definition.
    """
    feats = spec.get("features") or []
    return max(5, len(feats))


def _structural_defects(design: str, tb: str) -> list[str]:
    """Compile-clean defects that make a candidate unable to converge.

    Both are invisible to the simulator's exit code and to the repair loop's
    view of a check failure, so without this the loop spends its rounds
    editing a testbench or a counter that could never have worked.
    """
    from .fix import undersized_counters, white_box_refs

    out: list[str] = []
    narrow = undersized_counters(design)
    if narrow:
        out.append(
            "the DUT's counter/limit registers are too narrow to hold the "
            f"spec's phase durations ({', '.join(narrow)}); it can never "
            "reach the states the testbench waits for -- widen them"
        )
    peek = white_box_refs(tb, "")
    if peek:
        out.append(
            "the testbench is white-box: it reads DUT internals ("
            + ", ".join(f"dut.{p}" for p in peek)
            + ") instead of the output ports, so it is tied to this "
            "particular internal state encoding -- rewrite it to observe "
            "outputs only"
        )
    return out


def _time_budget_s(default: float = 2400.0) -> float:
    """Wall-clock seconds after which escalation stops adding candidates.

    Escalation is bounded by rounds, but not by time, and rounds are not
    cheap: a round is `candidates` generations plus up to `fix_rounds` repair
    calls each, so the worst case measured was over 50 minutes of wall clock
    before the run was abandoned mid-flight. For a student or researcher that
    is unusable, and it is the failure mode nobody notices until they give up.

    A budget of 0 disables the bound entirely.
    """
    import os

    try:
        return max(0.0, float(os.getenv("VLAB_TIME_BUDGET_S", str(default))))
    except ValueError:
        return default


def _escalation_rounds() -> int:
    """Extra rounds allowed after a round in which every candidate failed."""
    import os

    try:
        return max(0, min(5, int(os.getenv("VLAB_ESCALATION_ROUNDS", "2"))))
    except ValueError:
        return 2


def _failure_feedback(cands: list[dict[str, Any]]) -> str:
    """Summarise what already failed, as guidance for the next attempt.

    memory.jsonl recorded every candidate but nothing ever read it back, so
    each attempt re-derived the design from scratch with no idea what the
    previous ones got wrong. Feeding the concrete failures forward turns
    "try N times and hope" into "try N times, each informed by the last".

    Deduplicated by leading text: independent candidates usually fail for
    the same reason, and three copies of one failure is noise, not signal.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for c in cands:
        if c.get("status") == "pass":
            continue
        note = str(c.get("note") or "").strip()
        if not note:
            continue
        key = re.sub(r"\d+", "N", note)[:90]
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {note[:260]}")
        if len(lines) >= 6:
            break
    if not lines:
        return ""
    return (
        "EARLIER ATTEMPTS IN THIS RUN FAILED. Do not reproduce these "
        "mistakes — change the design or the testbench so they cannot recur:\n"
        + "\n".join(lines)
    )


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
    on_session: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Full autonomous loop. width/candidates None = decided by the LLM.

    on_session, when given, is called with the run dir as soon as the
    session id exists (before any candidate work starts).
    """
    from .ppa import run_ppa
    from .report import build_report
    from .rtl_gen import run_gen
    from .synth import run_synth

    # EDA tools run with cwd=<run dir>, so every path handed to them must be
    # absolute; a relative project_dir would resolve against the wrong root
    project_dir = Path(project_dir).resolve()

    if candidates is not None and candidates < 1:
        raise ValueError("candidates must be >= 1")
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if max_fix_rounds < 0:
        raise ValueError("max_fix_rounds must be >= 0")

    spec = expand_spec(prompt, dut, width, use_llm)
    spec, spec_issues = review_spec(spec, use_llm)
    # None = LLM decided: width lives in spec, candidates in suggested_candidates
    width = width or int(spec.get("data_width", 8))
    try:
        candidates = candidates or int(spec.get("suggested_candidates", 1))
    except (TypeError, ValueError):
        candidates = 1
    candidates = max(1, min(8, candidates))
    spec["data_width"] = width
    sid = _sid(spec["dut_name"], project_dir)
    rdir = runs_dir(project_dir) / sid
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "spec.json").write_text(json.dumps(spec, indent=2))
    sdir = project_dir / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "spec.json").write_text(json.dumps(spec, indent=2))
    # publish the session id as soon as it exists so live UIs can attach
    # to this run without guessing from a directory scan
    if on_session is not None:
        try:
            on_session(rdir)
        except Exception:  # noqa: BLE001 - a UI hook must never break the run
            pass
    mem = rdir / "memory.jsonl"
    golden: dict[str, Any] = {}
    guidance = ""
    feedback = ""
    all_cands: list[dict[str, Any]] = []
    rounds_run = 0
    timed_out = False
    escalation_rounds = _escalation_rounds()
    time_budget = _time_budget_s()
    started_at = time.monotonic()

    # The original loop ran exactly `iterations` rounds and then gave up. If
    # every candidate in a round failed, the run returned "no winner" even
    # though an extra round costs one generation and frequently succeeds --
    # measured per-attempt success on a hard FSM is well under 50%, so
    # stopping early discards most of the available probability. Escalate
    # instead: each extra round adds candidates and carries the previous
    # round's concrete failures forward.
    while True:
        rounds_run += 1
        # base rounds keep the requested width; escalation rounds widen it
        n_this_round = (
            candidates if rounds_run <= iterations
            else min(8, candidates + 2 * (rounds_run - iterations))
        )
        round_start = len(all_cands)
        for c in range(1, n_this_round + 1):
            cid = f"{sid}__i{rounds_run}c{c}"
            entry: dict[str, Any] = {
                "id": cid, "iter": rounds_run, "cand": c,
            }
            try:
                cdir = session_dir(project_dir, cid)
                shutil.copy(rdir / "spec.json", cdir / "spec.json")
                if use_llm:
                    run_gen(project_dir, cid, cdir / "spec.json",
                            use_llm=True, guidance=guidance, feedback=feedback)
                else:
                    run_gen(project_dir, cid, cdir / "spec.json", use_llm=False)
                ver = (_verify_candidate(project_dir, cid, max_fix_rounds, golden)
                       if use_llm else _offline_verify(project_dir, cid))
                entry.update({
                    "status": ver["status"], "passes": ver.get("passes", 0),
                    "repairs": ver.get("repairs", 0), "note": ver.get("note", ""),
                })
                if ver.get("spec_gaps"):
                    entry["spec_gaps"] = ver["spec_gaps"]
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
                except Exception as e:  # noqa: BLE001 - one candidate's PPA
                    # must not abort measurement for the others
                    # A candidate that PASSED simulation stays a pass even if
                    # synthesis or STA failed. Flipping it to "error" made
                    # pick_winner discard it, so a yosys/STA hiccup (or a
                    # synth timeout) silently threw away a verified design
                    # and could turn an otherwise-winning run into "no
                    # winner". Metrics stay None, which check_constraints
                    # already treats as unchecked rather than violated.
                    entry.setdefault("status", "pass")
                    entry["measure_error"] = f"{type(e).__name__}: {e}"[:200]
            all_cands.append(entry)
            memory_append(mem, {"sid": sid, **entry})
        round_cands = all_cands[round_start:]
        passed = any(x.get("status") == "pass" for x in round_cands)
        if passed:
            if rounds_run < iterations:
                # a pass is in hand, but the caller asked for more rounds:
                # keep exploring to find a better point on the frontier
                guidance = search_guidance(
                    spec, pareto_frontier(all_cands), all_cands
                )
                continue
            # winner secured and no further exploration requested: stop.
            # Escalating past a success would multiply API spend for no
            # reliability gain.
            break
        if rounds_run >= iterations + escalation_rounds:
            break
        # nothing passed this round: widen the search and tell the model
        # exactly what already failed so it does not repeat the dead ends
        feedback = _failure_feedback(all_cands)
        if time_budget and (time.monotonic() - started_at) >= time_budget:
            # Out of time. Record why, so the summary explains a run that
            # ended with candidates still in flight rather than looking like
            # the search simply gave up.
            timed_out = True
            break

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
        # rounds actually run, which can exceed `iterations` when every
        # candidate failed and the run escalated instead of giving up
        "rounds_run": rounds_run,
        "escalation_rounds": escalation_rounds,
        "timed_out": timed_out,
        "elapsed_s": round(time.monotonic() - started_at, 1),
        "spec_issues": spec.get("review_notes", []),
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
