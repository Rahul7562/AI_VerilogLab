"""AI repair loop: feed sim failures back to Nemotron to fix the testbench."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _extract_tb(text: str, dut: str) -> str:
    """Robust testbench extraction: fences first, then tb module slice."""
    blocks = re.findall(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", text, re.S)
    cands = [b.strip() for b in blocks if f"tb_{dut}" in b]
    if cands:
        return max(cands, key=len)
    if blocks:  # fence without module name match — take largest plausible
        big = max(blocks, key=len)
        if "module" in big and "$finish" in big:
            return big.strip()
    # raw Verilog without fences: slice tb module..endmodule
    start = text.find(f"module tb_{dut}")
    if start >= 0:
        end = text.find("endmodule", start)
        if end > start:
            return text[start : end + len("endmodule")].strip()
    raise ValueError("No testbench found in LLM output")


def repair_prompt(design: str, tb: str, log: str, dut: str) -> str:
    return (
        f"Output ONLY a Verilog testbench in one verilog code fence, no prose, "
        f"no analysis — the code fence must be the first thing in your reply.\n"
        f"Fix this failing iverilog testbench for DUT {dut} (keep module tb_{dut}, "
        f"`timescale 1ns/1ps, $dumpfile(\"wave.vcd\"), $dumpvars(0,tb_{dut}), $finish).\n"
        f"Rules: drive stimulus ONLY at negedge clk; sample outputs with #1 after "
        f"posedge; separate clock block (initial clk=0; always #5 clk=~clk;); "
        f"ONE stimulus+check initial block; >=5 PASS/FAIL $displays. "
        f"Watch for off-by-one cycle counting: when checking a value N cycles "
        f"after an event, count from the first edge where the DUT samples it.\n"
        f"When a check samples the wrong STATE (not X), suspect your wait counts "
        f"vs the DUT's counter/parameter semantics and align waits to the DUT's "
        f"declared parameters.\n"
        f"DUT:\n```verilog\n{design}\n```\n"
        f"Failing testbench:\n```verilog\n{tb}\n```\n"
        f"Sim log:\n```\n{log[:4000]}\n```"
    )


def design_repair_prompt(
    design: str, log: str, dut: str, ports: list[dict]
) -> str:
    names = ", ".join(f"{p['direction']} {p['name']}" for p in ports)
    return (
        f"Output ONLY synthesizable Verilog-2005 in one verilog code fence, "
        f"no prose — the fence must be the first thing in your reply.\n"
        f"This DUT fails to COMPILE under iverilog. Fix ONLY the compile errors; "
        f"do not change behavior. Hard constraints: module MUST stay named {dut}, "
        f"port list MUST stay exactly ({names}), Verilog-2005 only "
        f"(no SystemVerilog: no `logic`, no `always_ff`/`always_comb`, "
        f"no `unique`/`priority`, no `.inside`, no unpacked arrays).\n"
        f"Failing design:\n```verilog\n{design}\n```\n"
        f"Compiler log:\n```\n{log[:4000]}\n```"
    )


def _extract_design(text: str, dut: str) -> str:
    """Extract `module <dut>..endmodule` from fences or raw text."""
    import re as _re

    blocks = _re.findall(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", text, re.S)
    cands = [b.strip() for b in blocks if f"module {dut}" in b]
    if cands:
        return _slice_module(max(cands, key=len), dut)
    if blocks:
        for b in sorted(blocks, key=len, reverse=True):
            if "module" in b and "endmodule" in b:
                try:
                    return _slice_module(b, dut)
                except ValueError:
                    continue
    start = text.find(f"module {dut}")
    if start >= 0:
        return _slice_module(text[start:], dut)
    raise ValueError("No fixed design found in LLM output")


def _slice_module(text: str, dut: str) -> str:
    start = text.find(f"module {dut}")
    end = text.find("endmodule", start)
    if start < 0 or end < 0:
        raise ValueError(f"module {dut}..endmodule not found")
    return text[start : end + len("endmodule")].strip()


def sanitize_testbench(tb: str, dut: str) -> tuple[str, list[str]]:
    """Deterministic TB fixes (no LLM): `timescale must precede the module.

    Returns (fixed_tb, warnings). Raises ValueError when the TB is
    structurally unusable (wrong module, no $finish -> would hang sim).
    """
    warns: list[str] = []
    lines = tb.splitlines()
    ts_idx = [i for i, ln in enumerate(lines) if ln.strip().startswith("`timescale")]
    first_content = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if ts_idx and ts_idx == [first_content]:
        out = tb  # already correct: single timescale as first line
    else:
        kept = [ln for i, ln in enumerate(lines) if i not in ts_idx]
        if not ts_idx:
            warns.append("added missing `timescale 1ns/1ps")
        else:
            warns.append("moved `timescale outside module definition")
        out = "`timescale 1ns/1ps\n" + "\n".join(kept).lstrip("\n")
        if tb.endswith("\n"):
            out += "\n"
    if f"module tb_{dut}" not in out:
        raise ValueError(f"sanitized TB names wrong module (want tb_{dut})")
    if "$finish" not in out:
        raise ValueError("sanitized TB missing $finish (would hang the simulator)")
    return out, warns


def is_compile_fail(sim: dict, log: str) -> bool:
    """True when simulation never ran (compile errors) — repair the DUT."""
    if sim.get("rc", 0) != 0 and "PASS" not in log and "FAIL" not in log:
        return True
    return "syntax error" in log or "Incomprehensible" in log
    return (
        f"Output ONLY a Verilog testbench in one verilog code fence, no prose, "
        f"no analysis — the code fence must be the first thing in your reply.\n"
        f"Fix this failing iverilog testbench for DUT {dut} (keep module tb_{dut}, "
        f"`timescale 1ns/1ps, $dumpfile(\"wave.vcd\"), $dumpvars(0,tb_{dut}), $finish).\n"
        f"Rules: drive stimulus ONLY at negedge clk; sample outputs with #1 after "
        f"posedge; separate clock block (initial clk=0; always #5 clk=~clk;); "
        f"ONE stimulus+check initial block; >=5 PASS/FAIL $displays. "
        f"Watch for off-by-one cycle counting: when checking a value N cycles "
        f"after an event, count from the first edge where the DUT samples it.\n"
        f"When a check samples the wrong STATE (not X), suspect your wait counts "
        f"vs the DUT's counter/parameter semantics and align waits to the DUT's "
        f"declared parameters.\n"
        f"DUT:\n```verilog\n{design}\n```\n"
        f"Failing testbench:\n```verilog\n{tb}\n```\n"
        f"Sim log:\n```\n{log[:4000]}\n```"
    )


def run_fix(
    project_dir: Path,
    session_id: str,
    rounds: int = 3,
    timeout: int = 240,
) -> dict[str, Any]:
    """Attempt LLM testbench repair until sim passes. Returns summary."""
    from .config import session_dir
    from .llm import chat as llm_chat
    from .sim import run_simulate

    outdir = session_dir(project_dir, session_id)
    spec = json.loads((outdir / "spec.json").read_text())
    dut = spec["dut_name"]
    tb_files = sorted(outdir.glob("tb_*.v"))
    if not tb_files:
        raise FileNotFoundError(f"No testbench in {outdir}. Run `vlab gen` first.")
    history: list[dict[str, Any]] = []
    last_status = "unknown"
    for rnd in range(1, rounds + 1):
        design = (outdir / "design.v").read_text()
        tb = tb_files[0].read_text()
        sim = run_simulate(project_dir, session_id, tool="auto")
        last_status = sim["status"]
        entry: dict[str, Any] = {
            "round": rnd,
            "pass": sim["pass_count"],
            "fail": sim["fail_count"],
            "status": sim["status"],
        }
        history.append(entry)
        if sim["status"] == "pass":
            break
        log = (outdir / "sim.log").read_text()
        if is_compile_fail(sim, log):
            entry["target"] = "design"
            prompt = design_repair_prompt(design, log, dut, spec.get("ports", []))
        else:
            entry["target"] = "testbench"
            prompt = repair_prompt(design, tb, log, dut)
        try:
            raw = llm_chat(
                [{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.2,
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001 - record and stop
            entry["note"] = f"llm call failed: {e}"[:200]
            break
        (outdir / f"fix_r{rnd}_raw.txt").write_text(raw)
        try:
            if entry["target"] == "design":
                fixed = _extract_design(raw, dut)
                _require_design_intact(fixed, dut, spec.get("ports", []))
                (outdir / "design.v").write_text(fixed)
            else:
                fixed, san_warns = sanitize_testbench(_extract_tb(raw, dut), dut)
                if san_warns:
                    entry["note"] = "; ".join(san_warns)
                (outdir / f"tb_{dut}.v").write_text(fixed)
        except ValueError as e:
            entry["note"] = str(e)[:200]
            continue
        entry["repaired_bytes"] = len(fixed)
    return {"session": session_id, "dut": dut, "rounds": history, "final": last_status}


def _require_design_intact(design: str, dut: str, ports: list[dict]) -> None:
    """Reject DUT repairs that rename the module or drop ports."""
    if f"module {dut}" not in design:
        raise ValueError("repair renamed the DUT module")
    missing = [p["name"] for p in ports if p["name"] not in design]
    if missing:
        raise ValueError(f"repair dropped ports: {missing}")
