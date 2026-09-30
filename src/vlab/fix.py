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


def _failing_checks(log: str) -> list[str]:
    """The FAIL labels from a sim log, in order."""
    return [m.strip() for m in re.findall(r"(?m)^\s*(?:[^:\n]*:\s*)*FAIL\b.*$", log)]


def repair_prompt(
    design: str, tb: str, log: str, dut: str, history: list[str] | None = None
) -> str:
    hist = ""
    if history:
        # Each round used to resend the same prompt with no memory of what was
        # already tried, so a stuck loop re-proposed the same wrong wait count
        # until the rounds ran out. Show the model what it has already done.
        hist = (
            "\nYou have already tried these repairs and they still leave the "
            "same checks failing. DO NOT repeat them — take a different "
            "approach:\n"
            + "\n".join(f"- {h}" for h in history[-4:])
            + "\n"
        )
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
        f"PREFERRED TECHNIQUE for a phase/timer check: instead of waiting a "
        f"hard-coded number of cycles and hoping, WAIT FOR THE TRANSITION. Loop "
        f"on @(posedge clk) until the expected output appears, and measure the "
        f"duration by counting cycles between the observed edges. That is "
        f"immune to off-by-one in the DUT's counter and passes regardless of "
        f"whether the DUT decrements before or after the state change.\n"
        f"BUT a transition-wait alone verifies only the ORDER of states. You "
        f"MUST also assert the DURATIONS. Measure them with a FREE-RUNNING "
        f"MONITOR, not by measuring one phase after another: a task that waits "
        f"for a phase and then hands off to a task for the next phase loses "
        f"the cycle in which the handover happened, so every phase after the "
        f"first measures exactly one cycle short and the DUT looks wrong when "
        f"it is not.\n"
        f"  // monitor: runs forever, never blocks the stimulus\n"
        f"  always @(posedge clk) #1 begin\n"
        f"    if (state_snap != prev_state) begin\n"
        f"      t = $time / 10;                       // cycles, not ns\n"
        f"      if (prev_state != ST_IDLE && t - prev_enter_t != 0)\n"
        f"        dur[prev_state] = t - prev_enter_t; // measured on exit\n"
        f"      prev_enter_t = t; prev_state = state_snap;\n"
        f"    end\n"
        f"  end\n"
        f"Then assert the recorded durations at the end of the run:\n"
        f"  if (dur[ST_YELLOW] != YELLOW_CYCLES)\n"
        f"    $display(\"FAIL: yellow lasted %0d, want %0d\", dur[ST_YELLOW], YELLOW_CYCLES);\n"
        f"  else $display(\"PASS: yellow lasts %0d cycles\", YELLOW_CYCLES);\n"
        f"Mirroring the DUT's state decode into the monitor keeps the two in "
        f"sync. Only report a duration FAIL when the monitor disagrees with the "
        f"DUT's own constants — if a duration is off by exactly one on every "
        f"phase except the first, the MEASUREMENT is wrong, not the DUT.\n"
        f"Also check EVERY feature in the spec — gating/enable, hold/freeze, "
        f"and extension behaviour each need their own driven check.\n"
        f"Use the SAME clock convention the DUT declares (if the DUT has a "
        f"CLK_HZ-style parameter, derive every wait from it, or override it "
        f"identically in the testbench) — never hard-code a cycle count that "
        f"assumes a different frequency.\n"
        f"{STRUCTURAL_RULES}\n\n"
        f"{hist}"
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
        f"Most common cause of these errors: a bare `output` defaults to "
        f"`wire`, and a wire cannot be assigned inside an always block. Every "
        f"output driven in always/initial MUST be declared `output reg`; keep "
        f"`output wire` only for ports driven by a continuous `assign`.\n"
        f"Fix ONLY compile errors. Do not change the design's behaviour.\n"
        f"{STRUCTURAL_RULES}\n\n"
        f"Failing design:\n```verilog\n{design}\n```\n"
        f"Compiler log:\n```\n{log[:4000]}\n```"
    )


def design_behavior_repair_prompt(
    design: str,
    tb: str,
    log: str,
    spec: dict[str, Any],
    dut: str,
    verdict: str,
    history: list[str] | None = None,
) -> str:
    """Fix a DUT that COMPILES but violates the spec (wrong behaviour).

    This exists because adjudication can now blame the design, and the only
    design prompt we had was `design_repair_prompt` -- which tells the model
    the DUT "fails to COMPILE. Fix ONLY the compile errors; do not change
    behavior." Fed a design that compiles perfectly and merely behaves
    wrongly, that instruction makes the correct answer "return it unchanged",
    so the loop detected a no-op and gave up. Two historical runs died
    exactly that way ("repair reproduced the design unchanged").
    """
    names = ", ".join(
        f"{p['direction']} {p['name']}" for p in spec.get("ports", [])
    )
    hist = ""
    if history:
        hist = (
            "\nEarlier repair attempts that did NOT work — do not repeat them:\n"
            + "\n".join(f"- {h}" for h in history[-4:])
            + "\n"
        )
    return (
        f"Output ONLY synthesizable Verilog-2005 in one verilog code fence, "
        f"no prose — the code fence must be the first thing in your reply.\n"
        f"This DUT COMPILES but its BEHAVIOUR is wrong. Fix the behaviour.\n"
        f"Diagnosis: {verdict}\n"
        f"Hard constraints: module MUST stay named {dut}; port list MUST stay "
        f"exactly ({names}); synthesizable Verilog-2005 only (no `logic`, no "
        f"`always_ff`/`always_comb`, no `unique`/`priority`, no `.inside`, no "
        f"unpacked arrays). Any output assigned inside an always block MUST be "
        f"declared `output reg`, never `output wire`.\n"
        f"The SPEC below is the ground truth. Implement EVERY feature it "
        f"states, including any the current design omits. Change the internal "
        f"FSM/counters/limits as needed to match it, but keep the interface.\n"
        f"The testbench's expectations come from the spec; where the current "
        f"testbench disagrees with the spec, the SPEC WINS.\n"
        f"{STRUCTURAL_RULES}\n"
        f"{hist}"
        f"SPEC:\n{json.dumps(spec, indent=2)[:6000]}\n\n"
        f"CURRENT DUT:\n```verilog\n{design}\n```\n\n"
        f"CURRENT TESTBENCH (for context):\n```verilog\n{tb[:4000]}\n```\n\n"
        f"FAILING CHECKS (sim log):\n```\n{log[:4000]}\n```"
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


def _procedurally_assigned_names(design: str) -> set[str]:
    """Identifiers that receive a procedural assignment (`=` or `<=`).

    Deliberately excludes `=` inside a continuous `assign`, since a net may
    legally be driven that way and must stay a `wire`.
    """
    assigned: set[str] = set()
    # walk always/task blocks and take assignment statements at their top level
    for block in re.finditer(
        r"\b(?:always(?:_ff|_comb|_latch)?|initial|task|function)\b.*?\bend\b",
        design, re.S,
    ):
        body = block.group(0)
        body = re.sub(r"\bassign\b[^;]*;", "", body)  # continuous assign
        for m in re.finditer(
            r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:\[[^\]]*\])*\s*(?:<=|=)[^=]",
            body, re.M,
        ):
            assigned.add(m.group(1))
    return assigned


def _fix_wire_output_driven_as_variable(design: str, dut: str) -> tuple[str, list[str]]:
    """Promote `output wire X` to `output reg X` when X is assigned in a block.

    In Verilog-2001 a bare `output` defaults to `wire`, and a wire is not a
    procedural l-value. A model that declares `output wire red_main` and then
    drives it inside `always @*` produces:

        design.v:97: error: red_main is not a valid l-value

    which is 18 elaboration errors for one modelling slip. This is a purely
    mechanical, semantics-preserving fix — `reg` and `wire` differ only in
    what may drive them — so it must never consume an LLM repair round.
    Outputs left on `assign` keep their `wire`, which would be wrong to flip.
    """
    m = re.search(rf"\bmodule\s+{re.escape(dut)}\b", design)
    if not m:
        return design, []
    header_end = design.find(";", m.end())
    if header_end < 0:
        return design, []
    header = design[m.start():header_end]
    body_start = re.search(r"\bendmodule\b", design[m.end():])
    body = design[header_end + 1: header_end + 1 + (body_start.start() if body_start else len(design))]
    driven = _procedurally_assigned_names(design)

    promoted: list[str] = []
    out_lines = header.split("\n")
    for i, line in enumerate(out_lines):
        dm = re.match(
            r"^(\s*)(?:output)\s+(?:wire)(?:\s+(?:signed))?\s*"
            r"(?:\[[^\]]*\])?\s*([A-Za-z_][A-Za-z0-9_$]*)",
            line,
        )
        if not dm:
            continue
        name = dm.group(2)
        # only touch it if the body actually assigns to it procedurally and
        # no continuous assign drives it
        if name in driven and not re.search(
            rf"\bassign\b[^;]*\b{re.escape(name)}\b", body
        ):
            out_lines[i] = line.replace("wire", "reg", 1)
            promoted.append(name)
    if not promoted:
        return design, []
    new_header = "\n".join(out_lines)
    return (
        design[: m.start()] + new_header + design[header_end:],
        [f"declared output reg (was wire, assigned procedurally): "
         f"{', '.join(promoted)}"],
    )


def sanitize_design(design: str, dut: str = "") -> tuple[str, list[str]]:
    """Deterministic DUT fixes (no LLM): `timescale repair + wire-output fix.

    Both files are compiled by iverilog and yosys, so a mechanical slip fails
    the whole candidate. Fixing them here is strictly cheaper and more
    reliable than spending an LLM repair round on a one-word mistake.
    """
    design, warns = _fix_backtick_timescale(design)
    if dut:
        design, w2 = _fix_wire_output_driven_as_variable(design, dut)
        warns = warns + w2
    return design, warns


def add_sim_watchdog(tb: str, dut: str, budget_ns: int = 2_000_000) -> tuple[str, list[str]]:
    """Guarantee the testbench terminates, whatever it is waiting for.

    A testbench that waits for a phase with a bare `wait (...)` hangs forever
    if the DUT never reaches that phase — which happens every time a repair
    attempt removes a phase. The simulator then has to be killed by an
    external timeout, and the run records an empty log with no PASS and no
    FAIL, so the dashboard cannot say what went wrong.

    This appends a watchdog process that force-finishes the simulation and
    prints a diagnosable FAIL if the testbench has not finished in time. The
    budget is scaled by the file's own `timescale, so a fast 1ns/1ps
    testbench and a slow one both get a guard far beyond any healthy run; a
    fixed constant fired mid-run on the fast ones and invented false failures.
    """
    if "vlab watchdog" in tb:
        return tb, []
    m = re.search(r"\bendmodule\b", tb)
    if not m:
        return tb, []
    # Trim trailing whitespace and comments that sit between the last
    # statement and `endmodule`. builtin_template ends its module with a
    # comment, and inserting after it produced `// ... initial begin` -- a
    # syntax error that broke every offline testbench at once.
    head = tb[: m.start()].rstrip()
    # anything after the module (rare, but legal in a concatenated file)
    after = tb[m.end():]
    # Derive the budget from the testbench's own timescale. Guessing a
    # constant here breaks every short testbench: a budget smaller than a
    # healthy run's real duration fires mid-run and reports a false FAIL.
    scale = 1
    ts = re.search(r"`timescale\s+(\d+)\s*(s|ms|us|ns|ps)\s*/", tb)
    if ts:
        mult = {"s": 10**9, "ms": 10**6, "us": 10**3, "ns": 1, "ps": 10**-3}
        scale = int(ts.group(1)) * mult[ts.group(2)]
    if scale <= 0:
        scale = 1
    budget = int(budget_ns * scale)
    guard = f"""    // vlab watchdog: a testbench that waits for a phase the DUT never
    // reaches would otherwise hang until the external simulator timeout,
    // producing a log with no PASS and no FAIL. The budget is derived from
    // this file's own timescale directive, so it cannot fire mid-run.
    // The message stays on ONE line: Verilog-2005 has no adjacent
    // string-literal concatenation, so a wrapped $display is a syntax error.
    initial begin
        #{budget};
        $display("FAIL: vlab watchdog fired after {budget} time units (a wait never completed)");
        $finish;
    end
"""
    out = head + "\n" + guard + "endmodule\n" + after
    return out, [f"added simulator watchdog ({budget} time units)"]


def white_box_refs(tb: str, dut: str = "") -> list[str]:
    """Internal DUT signals the testbench reaches into, e.g. `dut.state`.

    A testbench is a black-box check against the spec. Reading `dut.<internal>`
    couples it to one particular state encoding, and nothing makes the TB's own
    localparams match the DUT's: the model is free to number its states 0..7
    either way. When the two disagree the TB waits for a state that never
    arrives, the simulation stalls until the watchdog, and no number of repair
    rounds converges because both sides are each internally consistent.

    Observed in a real run: `while (dut.state !== target)` against a TB-local
    `S_PED` that the DUT had numbered differently -> "timed out waiting for
    south green (state=2)".
    """
    found: list[str] = []
    pat = rf"\b{re.escape(dut) if dut else 'dut'}\s*\.\s*([A-Za-z_]\w*)"
    for m in re.finditer(pat, tb):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


# counter/limit registers that must be wide enough to hold a phase duration
_COUNTERISH = re.compile(r"count|cnt|timer|dwell|duration|limit|cycles|age|len", re.I)


def undersized_counters(design: str) -> list[str]:
    """Counter/limit registers too narrow to hold a plausible duration.

    `reg [1:0] count` with `reg [1:0] NORTH_LIMIT` can never represent a
    10-cycle phase: it wraps at 3. The DUT then cannot satisfy the spec and no
    repair round can ever make the testbench agree, because the design was
    impossible from the start -- yet the run reports it as a normal check
    failure and burns its rounds anyway.

    Returns human-readable "name [N bits]" strings.
    """
    bad: list[str] = []
    for m in re.finditer(r"\breg\s*(?:\[\s*(\d+)\s*:\s*0\s*\])?\s*([A-Za-z_]\w*)",
                         design):
        width = int(m.group(1)) + 1 if m.group(1) else 1
        name = m.group(2)
        if not _COUNTERISH.search(name):
            continue
        if width <= 2:
            bad.append(f"{name} [{width} bits]")
    return bad


def _task_arity_mismatches(src: str) -> list[tuple[str, int, int, int]]:
    """(task, declared_args, passed_args, line) for every bad call.

    Verilog tasks are not checked against their call sites for arity, so a
    missing argument is only caught at elaboration -- `error: Missing
    argument N of call to task` -- which kills the whole candidate. It is a
    purely mechanical slip, so it belongs in a sanitizer rather than costing
    an LLM repair round.
    """
    arity: dict[str, int] = {}
    for m in re.finditer(
        r"\btask\s+(?:automatic\s+)?([A-Za-z_]\w*)\s*(.*?)\bendtask\b", src, re.S
    ):
        name, decl = m.group(1), m.group(2)
        # one `input` declaration per argument, wherever they sit in the body
        arity[name] = len(re.findall(r"\binput\b", decl))
    if not arity:
        return []
    bad: list[tuple[str, int, int, int]] = []
    for m in re.finditer(r"(?m)^.*?\b([A-Za-z_]\w*)\s*\(([^;]*?)\)\s*;", src):
        name, args = m.group(1), m.group(2)
        if name not in arity:
            continue
        # skip the declaration itself: it sits inside a `task ... endtask`
        line = src[: m.start()].count("\n") + 1
        opened = src.rfind(f"task ", 0, m.start())
        closed = src.rfind("endtask", 0, m.start())
        if opened > closed:
            continue
        # a bare `()` call passes no arguments
        passed = 0 if not args.strip() else _count_call_args(args)
        if passed != arity[name]:
            bad.append((name, arity[name], passed, line))
    return bad


def _count_call_args(args: str) -> int:
    """Count top-level arguments in a Verilog argument list."""
    depth = 0
    count = 1
    for ch in args:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def fix_task_arity(src: str) -> tuple[str, list[str]]:
    """Pad or trim task calls to match their declared arity.

    Padding inserts `1'b0` (a defined value, so the call elaborates and the
    testbench still runs); trimming drops the excess. Either way the candidate
    gets to the simulator instead of dying at elaboration, and the remaining
    failures come from real behaviour rather than from a signature typo.
    """
    bad = _task_arity_mismatches(src)
    if not bad:
        return src, []
    lines = src.splitlines(keepends=True)
    warns: list[str] = []
    for name, want, got, line in reversed(bad):
        if not (1 <= line <= len(lines)):
            continue
        ln = lines[line - 1]
        m = re.search(rf"(\b{re.escape(name)}\s*\()([^;]*?)(\)\s*;)", ln)
        if not m:
            continue
        raw = m.group(2)
        args = [] if not raw.strip() else [a.strip() for a in _split_args(raw)]
        if got < want:
            args += ["1'b0"] * (want - got)
            verb = "padded"
        else:
            args = args[:want]
            verb = "trimmed"
        lines[line - 1] = ln[: m.start()] + m.group(1) + ", ".join(args) + m.group(3) + ln[m.end():]
        warns.append(f"{verb} task call {name}() to {want} arg(s)")
    return "".join(lines), warns


def _split_args(raw: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def sanitize_testbench(tb: str, dut: str) -> tuple[str, list[str]]:
    """Deterministic TB fixes (no LLM): repair a botched `timescale, task-call
    arity, then make sure the directive precedes the module and add a watchdog.

    The order matters. The `timescale is settled first because both
    `fix_task_arity` and `add_sim_watchdog` read it to scale their work, and
    the watchdog is appended last because the `timescale relocation branch
    rewrites the file by line index.

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
    # arity first, then the watchdog: both read the `timescale that is now
    # settled above, and the watchdog must be the last thing inserted so the
    # timescale-relocation branch cannot drop or relocate it.
    out, arity_warns = fix_task_arity(out)
    warns += arity_warns
    out, wd_warns = add_sim_watchdog(out, dut)
    warns += wd_warns
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


# Rules that keep a repaired pair convergent. They must appear in EVERY prompt
# that produces or repairs Verilog, not just the first generation: a repair
# round told only "FAIL: timed out waiting for state 3" re-introduces
# `while (dut.state !== ...)` — reintroducing the very defect the generation
# prompt forbids. That was observed in a real run.
STRUCTURAL_RULES = """HARD RULES (breaking these makes the candidate unfixable, not
merely wrong — a repair round can never undo them):
1. BLACK-BOX: the testbench must observe ONLY the DUT's output ports. Never
   write `dut.<internal signal>` (not dut.state, not dut.count, not a DUT
   localparam). A hierarchical reference ties the testbench to ONE internal
   encoding; if the DUT numbered its states differently, the wait never
   completes and you will be back here forever. Identify a phase from which
   OUTPUTS are lit, not from an internal register.
2. COUNTER WIDTH: every counter, timer and limit register must be wide enough
   to hold the largest duration the spec requires (10 cycles needs >= 4 bits;
   8 bits is cheap and safe). `reg [1:0] count` wraps at 3 and the design can
   then never reach the state the testbench waits for.
3. A bare `wait (...)` with no timeout will hang the whole simulation. Always
   bound every wait loop with a cycle counter and report a FAIL on timeout.
4. `output <name>` that is assigned inside an always block must be declared
   `output reg`; `output wire` cannot be a procedural l-value."""


ADJUDICATE_PROMPT = """You decide which file is WRONG in a failing Verilog simulation.

You are given the DUT specification (the GROUND TRUTH), the DUT, the testbench,
and the failing checks. Judge ONLY against the spec. The testbench is not
automatically the culprit just because the assertion is what failed.

Reply with ONLY a JSON object:
{{"culprit": "design" | "testbench" | "both", "reason": "<one sentence>"}}

Decision rules:
- "design"     — the DUT's behaviour contradicts an explicit spec statement
                 (wrong reset state, missing a required feature, wrong port
                 behaviour, a phase/feature the spec requires but the DUT lacks).
- "testbench"  — the DUT matches the spec and the testbench's expectation or
                 its cycle counting/edge sampling is wrong.
- "both"       — the DUT and the TB encode different interpretations of an
                 ambiguous spec line, or you cannot tell which is at fault.

Be concrete about WHICH spec sentence decides it.

SPEC:
{spec}

DUT:
```verilog
{design}
```

TESTBENCH:
```verilog
{tb}
```

FAILING CHECKS (sim log):
```
{log}
```"""


def adjudicate_target(
    design: str, tb: str, log: str, spec: dict[str, Any], dut: str
) -> tuple[str, str]:
    """Ask the model which file to repair, judged against the SPEC.

    `repair_target` used to return "testbench" for every non-compile
    failure, which gave the repair loop a fixed, wrong bias: a DUT that
    violates the spec gets its testbench rewritten to match it, and the
    run then reports a green winner for a spec-violating design. (The
    traffic-light run reset to green where the spec demanded all-red.)

    Returns (target, reason) where target is "design" | "testbench" | "both".
    On any failure it returns "both", so the ambiguous case regenerates the
    pair together rather than silently deferring to the testbench.
    """
    from .llm import chat as llm_chat

    try:
        raw = llm_chat(
            [{"role": "user", "content": ADJUDICATE_PROMPT.format(
                spec=json.dumps(spec, indent=2)[:6000],
                design=design[:8000],
                tb=tb[:8000],
                log=log[:4000],
            )}],
            max_tokens=400,
            temperature=0.0,
        )
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        culprit = str(obj.get("culprit", "")).strip().lower()
        if culprit in ("design", "testbench", "both"):
            return culprit, str(obj.get("reason", ""))[:300]
    except Exception:
        pass
    return "both", "adjudication unavailable; regenerating both files"


def pair_repair_prompt(
    design: str, tb: str, log: str, spec: dict[str, Any], dut: str
) -> str:
    """Regenerate the DUT and its testbench together, from the spec.

    Used when adjudication cannot pick a side. Repairing one file against the
    other converges them onto whatever that one file happens to do, which is
    how a wrong-but-consistent pair used to get published. Re-deriving both
    from the spec in one reply makes the pair consistent by construction.
    """
    return (
        f"Output ONLY a JSON object with keys \"design\" and \"testbench\", "
        f"no prose, no markdown fences around the JSON.\n"
        f"The DUT and its testbench implement DIFFERENT interpretations of the "
        f"spec below, so neither can be fixed in isolation. Re-derive BOTH from "
        f"the spec so they agree with each other AND with the spec.\n"
        f"Rules: design module MUST be named {dut}, synthesizable Verilog-2005, "
        f"ports exactly as specified. A bare `output` must be declared `reg` if "
        f"it is assigned inside an always block (never assign to an `output wire`). "
        f"Testbench module MUST be tb_{dut}, `timescale 1ns/1ps first line, "
        f"$dumpfile(\"wave.vcd\"), $dumpvars(0,tb_{dut}), $finish. Drive stimulus "
        f"at negedge, sample outputs #1 after posedge. Every expected value MUST "
        f"be derived from the spec — in particular the reset state and any "
        f"phase durations, which must be counted from the same clock convention "
        f"the DUT uses. Check EVERY port in the spec.\n"
        f"Output: {{\"design\": \"<verilog>\", \"testbench\": \"<verilog>\"}}\n\n"
        f"{STRUCTURAL_RULES}\n\n"
        f"SPEC:\n{json.dumps(spec, indent=2)[:6000]}\n\n"
        f"CURRENT DUT:\n```verilog\n{design}\n```\n\n"
        f"CURRENT TESTBENCH:\n```verilog\n{tb}\n```\n\n"
        f"SIM LOG:\n```\n{log[:4000]}\n```"
    )


def _extract_pair(text: str, dut: str) -> tuple[str, str]:
    """Extract (design, testbench) from a paired-repair reply."""
    import json as _json

    t = text.strip()
    try:
        obj = _json.loads(t)
        if isinstance(obj, dict) and obj.get("design") and obj.get("testbench"):
            return str(obj["design"]), str(obj["testbench"])
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            obj = _json.loads(m.group(0))
            if isinstance(obj, dict) and obj.get("design") and obj.get("testbench"):
                return str(obj["design"]), str(obj["testbench"])
        except (ValueError, TypeError):
            pass
    raise ValueError("paired repair reply contained no design+testbench JSON")


def _norm(v: str) -> str:
    """Whitespace/comment/directive-insensitive form for no-op repair detection.

    Compares only what the MODEL authored. `sanitize_testbench` injects a
    `timescale and a watchdog block into every file it writes, so comparing
    raw text made an identical repair look like a change and the loop burned
    its remaining rounds re-deriving the same file.
    """
    stripped = re.sub(r"//[^\n]*", "", v)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r"^\s*`\w+[^\n]*$", "", stripped, flags=re.M)
    # drop the injected watchdog (comment-free copy)
    stripped = re.sub(
        r"initial\s+begin\s*#\s*\d+\s*;\s*"
        r"\$display\(\s*\"FAIL: vlab watchdog.*?\)\s*;\s*\$finish\s*;\s*end\b",
        "",
        stripped,
        flags=re.S,
    )
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
                fixed, _ = sanitize_design(fixed, dut)
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
