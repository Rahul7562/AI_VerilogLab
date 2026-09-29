"""AI repair loop: feed sim failures back to Nemotron to fix the testbench."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _module_span(text: str, name: str) -> str | None:
    """Slice `module <name>` .. `endmodule`.

    The name is matched with a word boundary: a plain `find("module cnt")`
    also matches `module cnt_sub`, so a reply containing a submodule first
    had the submodule extracted as the DUT.
    """
    m = re.search(rf"\bmodule\s+{re.escape(name)}\b", text)
    if not m:
        return None
    end = text.find("endmodule", m.start())
    if end < 0:
        return None
    return text[m.start(): end + len("endmodule")].strip()


def _extract_tb(text: str, dut: str) -> str:
    """Robust testbench extraction: fences first, then tb module slice."""
    blocks = re.findall(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", text, re.S)
    name = f"tb_{dut}"
    for b in sorted(blocks, key=len, reverse=True):
        # slice even inside a fence: a fence can hold a helper submodule
        # before the real testbench, and returning the whole block would
        # hand two modules to a testbench file
        sliced = _module_span(b, name)
        if sliced:
            return sliced
    if blocks:  # fence without a matching module — take largest plausible
        big = max(blocks, key=len)
        if "module" in big and "$finish" in big:
            return big.strip()
    # raw Verilog without fences: slice tb module..endmodule
    sliced = _module_span(text, name)
    if sliced:
        return sliced
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
    blocks = re.findall(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", text, re.S)
    cands = [b.strip() for b in blocks if re.search(rf"\bmodule\s+{re.escape(dut)}\b", b)]
    if cands:
        return _slice_module(max(cands, key=len), dut)
    if blocks:
        for b in sorted(blocks, key=len, reverse=True):
            if "module" in b and "endmodule" in b:
                try:
                    return _slice_module(b, dut)
                except ValueError:
                    continue
    sliced = _module_span(text, dut)
    if sliced:
        return sliced
    raise ValueError("No fixed design found in LLM output")


def _slice_module(text: str, dut: str) -> str:
    sliced = _module_span(text, dut)
    if not sliced:
        raise ValueError(f"module {dut}..endmodule not found")
    return sliced


def _fix_backtick_timescale(src: str) -> tuple[str, list[str]]:
    """Restore a missing backtick on a `timescale directive.

    Some models emit a bare `timescale 1ns/1ps` with no leading backtick.
    That is not a directive, it is an undefined identifier, and iverilog
    rejects the file with `design.v:1: syntax error`. Since `timescale` is a
    reserved word it can never legitimately be a user identifier, so the
    repair is safe to make without consulting a model.
    """
    warns: list[str] = []
    out_lines: list[str] = []
    for line in src.splitlines():
        m = re.match(r"^(\s*)timescale\s+(\S+\s*/\s*\S+)\s*$", line)
        if m:
            out_lines.append(f"{m.group(1)}`{m.group(0).lstrip()}")
            warns.append("restored missing backtick on `timescale directive")
        else:
            out_lines.append(line)
    out = "\n".join(out_lines)
    if src.endswith("\n"):
        out += "\n"
    return out, warns


def sanitize_design(design: str) -> tuple[str, list[str]]:
    """Deterministic DUT fixes (no LLM). Currently just `timescale repair.

    The design is synthesized as well as simulated, and a botched directive
    is a hard compile error, so this runs before design.v is written.
    """
    return _fix_backtick_timescale(design)


def sanitize_testbench(tb: str, dut: str) -> tuple[str, list[str]]:
    """Deterministic TB fixes (no LLM): repair a botched `timescale, then make
    sure it precedes the module.

    Returns (fixed_tb, warnings). Raises ValueError when the TB is
    structurally unusable (wrong module, no $finish -> would hang sim).
    """
    warns: list[str] = []
    tb, ts_warns = _fix_backtick_timescale(tb)
    warns += ts_warns
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
    if not re.search(rf"\bmodule\s+tb_{re.escape(dut)}\b", out):
        raise ValueError(f"sanitized TB names wrong module (want tb_{dut})")
    if "$finish" not in out:
        raise ValueError("sanitized TB missing $finish (would hang the simulator)")
    return out, warns


def compile_error_file(log: str, design_name: str = "design.v") -> str | None:
    """Which file does the compiler blame? "design", "testbench", or None.

    iverilog prints `<path>:<line>: error: ...`. A testbench bug (assigning to
    a DUT output declared `wire`, wrong module name, bad port connection) looks
    identical to a design bug at the rc level, but repairing the wrong file
    cannot ever converge — the LLM dutifully re-emits the unchanged DUT.
    """
    # every file the compiler referenced in a `<file>:<line>:` position
    blamed: set[str] = {
        Path(m.group(1)).name
        for m in re.finditer(r"^\s*(\S+\.v(?:erilog)?):\d+:", log, re.M)
    }
    if not blamed:
        return None
    # a design error breaks elaboration of the TB too, so blame the design
    if design_name in blamed or "design.v" in blamed:
        return "design"
    tb_like = {n for n in blamed if n.startswith("tb_") or n.lower().startswith("tb")}
    if tb_like:
        return "testbench"
    # a single unknown .v file is the DUT: our pipeline only ever writes
    # design.v plus tb_<dut>.v, and a stray third file would be the DUT copy
    if len(blamed) == 1:
        return "design"
    return None


def is_compile_fail(sim: dict, log: str) -> bool:
    """True when simulation never ran (compile errors) — repair needed."""
    if sim.get("rc", 0) != 0 and "PASS" not in log and "FAIL" not in log:
        return True
    return "syntax error" in log or "Incomprehensible" in log


def repair_target(sim: dict, log: str, design_name: str = "design.v") -> str:
    """Which file to hand back to the LLM: "design" or "testbench"."""
    if not is_compile_fail(sim, log):
        return "testbench"  # ran but asserted wrong -> TB expectations
    blamed = compile_error_file(log, design_name)
    if blamed is None:
        # no file attribution: a pure elaboration/compile error with no
        # parseable path is far more often a DUT elaboration problem
        return "design" if "syntax error" in log or "Incomprehensible" in log \
            else "testbench"
    return blamed


def _norm(v: str) -> str:
    """Whitespace/comment/directive-insensitive form for no-op repair detection.

    sanitize_testbench always prepends a `timescale, so directives have to be
    ignored or every TB repair would look like a change.
    """
    stripped = re.sub(r"//[^\n]*", "", v)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r"^\s*`\w+[^\n]*$", "", stripped, flags=re.M)
    return re.sub(r"\s+", " ", stripped).strip()


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
        target = repair_target(sim, log)
        entry["target"] = target
        if target == "design":
            prompt = design_repair_prompt(design, log, dut, spec.get("ports", []))
        else:
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
            if target == "design":
                fixed = _extract_design(raw, dut)
                _require_design_intact(fixed, dut, spec.get("ports", []))
                fixed, _ = sanitize_design(fixed)
            else:
                fixed, san_warns = sanitize_testbench(_extract_tb(raw, dut), dut)
                if san_warns:
                    entry["note"] = "; ".join(san_warns)
        except ValueError as e:
            entry["note"] = str(e)[:200]
            continue
        # a repair that reproduces the file cannot converge; burning the
        # remaining rounds on it is pure waste. Compare normalized text:
        # sanitize_testbench always prepends a `timescale.
        prev = design if target == "design" else tb
        if _norm(fixed) == _norm(prev):
            entry["note"] = f"repair reproduced the {target} unchanged"
            break
        if target == "design":
            (outdir / "design.v").write_text(fixed)
        else:
            (outdir / f"tb_{dut}.v").write_text(fixed)
        entry["repaired_bytes"] = len(fixed)
    return {"session": session_id, "dut": dut, "rounds": history, "final": last_status}


def _require_design_intact(design: str, dut: str, ports: list[dict]) -> None:
    """Reject DUT repairs that rename the module or drop/rename ports.

    Uses the same header-aware port check as rtl_gen._require_ports: a plain
    substring test accepted a design declaring only `a_reg` for spec port
    `a`, so a repair could break the interface and still be written out.
    """
    from .rtl_gen import module_ports

    if not re.search(rf"\bmodule\s+{re.escape(dut)}\b", design):
        raise ValueError("repair renamed the DUT module")
    names = [str(p["name"]) for p in ports]
    if not names:
        return
    declared = module_ports(design, dut)
    if declared is None:
        raise ValueError("repair produced an unparsable module header")
    missing = [n for n in names if n not in declared]
    if missing:
        raise ValueError(f"repair dropped ports: {missing}")
