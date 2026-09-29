"""M2: generate design.v + testbench from spec.json via OpenRouter (Nemotron)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GEN_PROMPT = """You are an expert Verilog RTL engineer. Given the DUT spec JSON below,
output exactly two Verilog files.

Respond with ONLY the JSON object as the very first characters — no preamble,
no analysis, no thinking, no markdown fences around the JSON.
Rules:
- design.v: module name MUST be {dut}. Synthesizable Verilog-2005, no SystemVerilog-only constructs.
- Ports must match spec (names, directions, widths). Keep clk/rst if present.
- Start BOTH files with `timescale 1ns/1ps. Verilator errors out if one file
  in the compilation unit has a timescale and another does not.
- testbench: module name tb_{dut}. Must include `timescale 1ns/1ps,
  $dumpfile("wave.vcd"), $dumpvars(0, tb_{dut}), clock gen if sequential,
  reset sequence, at least 5 directed checks with $display("PASS:...") / $display("FAIL:..."),
  and $finish. Drive all inputs, never leave X.
- The testbench must drive and check EVERY port in the spec. Declare a DUT
  output as `wire` (or `reg` only if the TB assigns it) and never assign to
  a DUT output signal.
- Sequential testbench discipline (this is the #1 source of failing checks):
  drive stimulus at negedge clk, sample outputs #1 after posedge clk, keep
  the clock in its own `initial` block. Count cycles from the first edge on
  which the DUT actually samples your value: if a DUT updates y at posedge
  N, then y is readable from just after posedge N, not N+1. Derive expected
  values from the DUT's declared widths (8-bit wraps at 255->0).
- No markdown fences inside code. Output valid JSON only:
{{"design": "<verilog>", "testbench": "<verilog>", "notes": "<short>"}}
Spec:
{spec}
"""


def _extract_code_blocks(text: str) -> list[str]:
    return re.findall(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", text, re.S)


def parse_llm_output(text: str, dut: str) -> tuple[str, str, str]:
    """Parse LLM output into (design, testbench, notes). Robust to JSON or fences."""
    t = text.strip()
    # try direct JSON
    try:
        obj = json.loads(t)
        if isinstance(obj, dict) and "design" in obj and "testbench" in obj:
            return obj["design"], obj["testbench"], str(obj.get("notes", ""))
    except (json.JSONDecodeError, ValueError):
        pass
    # try JSON inside fences
    for block in _extract_code_blocks(t):
        b = block.strip()
        if b.startswith("{"):
            try:
                obj = json.loads(b)
                if isinstance(obj, dict) and "design" in obj:
                    return obj["design"], obj.get("testbench", ""), str(obj.get("notes", ""))
            except (json.JSONDecodeError, ValueError):
                continue
    blocks = _extract_code_blocks(t)
    if len(blocks) >= 2:
        return blocks[0], blocks[1], ""
    tb_re = rf"\bmodule\s+tb_{re.escape(dut)}\b"
    if len(blocks) == 1 and re.search(rf"\bmodule\s+{re.escape(dut)}\b", blocks[0]) \
            and re.search(tb_re, blocks[0]):
        # single block containing both — split on testbench module
        m = re.search(tb_re, blocks[0])
        idx = m.start()
        return blocks[0][:idx].strip(), blocks[0][idx:].strip(), ""
    # raw Verilog without any fences
    if re.search(rf"\bmodule\s+{re.escape(dut)}\b", t) and re.search(tb_re, t):
        m = re.search(tb_re, t)
        idx = m.start()
        design, tb = t[:idx].strip(), t[idx:].strip()
        # strip any leading prose before the design module
        d = re.search(rf"\bmodule\s+{re.escape(dut)}\b", design)
        if d and d.start() > 0:
            design = design[d.start():].strip()
        if "endmodule" in design and "endmodule" in tb:
            return design, tb, ""
    raise ValueError("Could not parse LLM output into design + testbench")


def _port_decl(p: dict[str, Any]) -> str:
    w = int(p.get("width", 1))
    rng = "" if w <= 1 else f" [{w-1}:0]"
    if p["direction"] == "input":
        return f"  input wire{rng} {p['name']}"
    if p["direction"] == "output":
        return f"  output reg{rng} {p['name']}"
    return f"  inout wire{rng} {p['name']}"


def builtin_template(spec: dict[str, Any]) -> tuple[str, str]:
    """Offline fallback: simple adder-style DUT matching spec ports."""
    dut = spec["dut_name"]
    ports = spec["ports"]
    w = int(spec.get("data_width", 8))

    def _find(*names: str) -> dict[str, Any] | None:
        low = {p["name"].lower(): p for p in ports}
        for n in names:
            if n in low:
                return low[n]
        return None

    clk_p = _find("clk", "clock")
    rst_p = _find("rst_n", "rstn", "rst", "reset")
    # inout is neither a data input nor a clean sum destination: the offline
    # template would leave it undriven (z all run) and never check it, so it
    # must not be selected as the adder output.
    outs = [p for p in ports if p["direction"] == "output"]
    ins = [
        p for p in ports
        if p["direction"] == "input"
        and p is not clk_p and p is not rst_p
    ]
    if not outs or len(ins) < 1:
        raise ValueError("builtin template needs >=1 data input and >=1 output")
    y = outs[0]
    a = ins[0]
    b = ins[1] if len(ins) > 1 else ins[0]
    yw, aw, bw = int(y.get("width", w)), int(a.get("width", w)), int(b.get("width", w))

    decls = ",\n".join(_port_decl(p) for p in ports)
    has_clk = clk_p is not None
    rst_expr = f"!{rst_p['name']}" if rst_p and "n" in rst_p["name"].lower() else (rst_p["name"] if rst_p else "1'b0")
    if has_clk:
        if rst_p:
            logic = (
                f"  always @(posedge {clk_p['name']}) begin\n"
                f"    if ({rst_expr}) {y['name']} <= {{{yw}{{1'b0}}}};\n"
                f"    else {y['name']} <= {a['name']} + {b['name']};\n"
                f"  end"
            )
        else:
            logic = (
                f"  always @(posedge {clk_p['name']}) begin\n"
                f"    {y['name']} <= {a['name']} + {b['name']};\n"
                f"  end"
            )
    else:
        logic = f"  always @(*) {y['name']} = {a['name']} + {b['name']};"

    design = (
        f"// Auto template for {dut} (offline fallback, no LLM)\n"
        f"module {dut} (\n{decls}\n);\n{logic}\nendmodule\n"
    )
    # testbench decls + instantiation derived from spec ports
    tb_decls, hooks, tb_drives = [], [], []
    for p in ports:
        pw = int(p.get("width", 1))
        rng = "" if pw <= 1 else f" [{pw-1}:0]"
        if p["direction"] == "input":
            tb_decls.append(f"  reg{rng} {p['name']} = 0;")
        elif p["direction"] == "output":
            tb_decls.append(f"  wire{rng} {p['name']};")
        else:
            # an undriven inout is z for the whole run; drive it from a reg
            # through a continuous assign so it is a known value
            tb_decls.append(f"  reg{rng} {p['name']}_tb = 0;")
            tb_decls.append(f"  wire{rng} {p['name']};")
            tb_drives.append(f"  assign {p['name']} = {p['name']}_tb;")
        hooks.append(f"    .{p['name']}({p['name']})")
    tb_body = "\n".join(tb_decls + tb_drives) + "\n"
    tb_inst = f"  {dut} dut (\n" + ",\n".join(hooks) + "\n  );\n"
    clkgen = f"  always #5 {clk_p['name']} = ~{clk_p['name']};\n" if has_clk else ""
    rstseq = (
        f"    {rst_p['name']} = {'0' if 'n' in rst_p['name'].lower() else '1'}; #20; "
        f"{rst_p['name']} = {'1' if 'n' in rst_p['name'].lower() else '0'};\n"
        if rst_p else ""
    )
    v1, v2 = 3 % (1 << aw), 4 % (1 << bw)
    v3, v4 = 10 % (1 << aw), 20 % (1 << bw)
    if b["name"] == a["name"]:
        # single data port driven twice: last assignment wins, DUT adds it to itself
        e1 = (v2 + v2) % (1 << yw)
        e2 = (v4 + v4) % (1 << yw)
    else:
        e1 = (v1 + v2) % (1 << yw)
        e2 = (v3 + v4) % (1 << yw)
    tb = (
        f"`timescale 1ns/1ps\n"
        f"module tb_{dut};\n{tb_body}{tb_inst}{clkgen}"
        f"  initial begin\n"
        f'    $dumpfile("wave.vcd"); $dumpvars(0, tb_{dut});\n'
        f"{rstseq}"
        f"    {a['name']} = {v1}; {b['name']} = {v2}; #20;\n"
        f'    if ({y["name"]} === {e1}) $display("PASS: {v1}+{v2}={e1}"); '
        f'else $display("FAIL: {y["name"]}=%0d", {y["name"]});\n'
        f"    {a['name']} = {v3}; {b['name']} = {v4}; #20;\n"
        f'    if ({y["name"]} === {e2}) $display("PASS: {v3}+{v4}={e2}"); '
        f'else $display("FAIL: {y["name"]}=%0d", {y["name"]});\n'
        f'    $display("DONE"); $finish;\n'
        f"  end\nendmodule\n"
    )
    return design, tb


def module_ports(verilog: str, dut: str) -> set[str] | None:
    """Port names declared in `module <dut>`'s header, or None if unparsable.

    Handles both ANSI style (`module m(input wire a, output y);`) and
    non-ANSI (`module m(a, y); input a; output y;`).
    """
    m = re.search(rf"\bmodule\s+{re.escape(dut)}\b(.*?);", verilog, re.S)
    if not m:
        return None
    header = m.group(1)
    names: set[str] = set()
    for part in header.split(","):
        toks = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", part)
        if toks:
            names.add(toks[-1])
    # non-ANSI: also pick up `input a;` / `output [7:0] y;` declarations
    body = verilog[m.end():]
    end = body.find("endmodule")
    if end > 0:
        body = body[:end]
    for d in re.finditer(
        r"\b(?:input|output|inout)\b(?:\s+(?:wire|reg|logic|signed))?"
        r"(?:\s*\[[^\]]*\])?\s+([A-Za-z_][A-Za-z0-9_$]*)", body
    ):
        names.add(d.group(1))
    return names


def _require_ports(design: str, spec: dict[str, Any]) -> None:
    """The generated DUT must declare every spec port (models invent their own).

    Checks the module header, not the whole file: a port named `a` also
    appears in most bodies, so a naive substring search passes designs that
    never actually declare the port — which then fail to elaborate.
    """
    dut = spec.get("dut_name", "")
    ports = [p["name"] for p in spec.get("ports", [])]
    if not ports:
        return
    declared = module_ports(design, dut) if dut else None
    if declared is None:
        # header unparsable — fall back to a whole-file word search rather
        # than rejecting a possibly-fine design
        missing = [
            n for n in ports
            if not re.search(rf"\b{re.escape(n)}\b", design)
        ]
    else:
        missing = [n for n in ports if n not in declared]
    if missing:
        raise ValueError(
            "design missing spec ports: "
            + ", ".join(missing)
            + ". Use EXACTLY the specified port names."
        )


def _require_tb_ports(tb: str, spec: dict[str, Any]) -> None:
    """The testbench must drive/observe every spec port.

    A TB that ignores a port cannot detect a DUT that ignores it either, so
    a DUT missing behaviour on that port would still be selected as passing.
    """
    ports = [p["name"] for p in spec.get("ports", [])]
    missing = [
        p for p in ports
        if not re.search(rf"\b{re.escape(p)}\b", tb)
    ]
    if missing:
        raise ValueError(
            "testbench does not reference ports: "
            + ", ".join(missing)
            + ". Drive and check every spec port."
        )


def _require_sane(design: str, tb: str, dut: str) -> None:
    """Hard reject unusable output (wrong module, TB that can't terminate)."""
    problems = []
    if not re.search(rf"\bmodule\s+{re.escape(dut)}\b", design):
        problems.append(f"design names wrong module (want {dut})")
    if not re.search(rf"\bmodule\s+tb_{re.escape(dut)}\b", tb):
        problems.append(f"testbench names wrong module (want tb_{dut})")
    if "$finish" not in tb:
        problems.append("testbench missing $finish (would hang the simulator)")
    if problems:
        raise ValueError("unusable LLM output: " + "; ".join(problems))


def _validate(design: str, tb: str) -> list[str]:
    """Soft warnings only (hard failures are rejected by _require_sane)."""
    warns: list[str] = []
    if "endmodule" not in design or "endmodule" not in tb:
        warns.append("design/testbench missing endmodule")
    if "$dumpfile" not in tb or "$dumpvars" not in tb:
        warns.append("testbench missing $dumpfile/$dumpvars (wave.vcd won't generate)")
    return warns


def run_gen(
    project_dir: Path,
    session_id: str,
    spec_path: Path,
    use_llm: bool = True,
    force: bool = False,
    max_tokens: int = 4096,
    guidance: str = "",
) -> dict[str, Any]:
    """Generate design.v + tb. Returns manifest dict."""
    from .config import session_dir

    spec = json.loads(spec_path.read_text())
    dut = spec["dut_name"]
    outdir = session_dir(project_dir, session_id)
    design_p = outdir / "design.v"
    tb_p = outdir / f"tb_{dut}.v"
    if design_p.exists() and tb_p.exists() and not force:
        raise FileExistsError(f"{design_p} exists. Use --force to overwrite.")

    notes = ""
    llm_used = False
    warns_pre: list[str] = []
    if use_llm:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            raise RuntimeError("OPENROUTER_API_KEY not set. Use --no-llm for template.")
        prompt = GEN_PROMPT.format(dut=dut, spec=json.dumps(spec, indent=2))
        if guidance:
            prompt += f"\nPrior exploration guidance (follow it):\n{guidance}\n"
        raw = llm_chat(
            [
                {
                    "role": "system",
                    "content": "You output only JSON with design + testbench Verilog.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        (outdir / "gen_raw.txt").write_text(raw)
        first_error = ""
        try:
            design, tb, notes = parse_llm_output(raw, dut)
            _require_sane(design, tb, dut)
            _require_ports(design, spec)
            _require_tb_ports(tb, spec)
        except ValueError as e:
            first_error = str(e)[:200]
            # retry with the FULL prompt (truncated/placeholder first replies
            # are common on loaded free-tier models; a bare nudge invites
            # generic examples, so resend the task with a strict trailer)
            raw2 = llm_chat(
                [
                    {
                        "role": "system",
                        "content": "You output only JSON with design + testbench Verilog.",
                    },
                    {"role": "user", "content": prompt + (
                        "\nIMPORTANT: your previous reply was rejected: "
                        + first_error
                        + "\nOutput ONLY one complete JSON object "
                        "{\"design\": \"...\", \"testbench\": \"...\", "
                        "\"notes\": \"...\"}, no prose, no fences.")},
                ],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            (outdir / "gen_raw_retry.txt").write_text(raw2)
            design, tb, notes = parse_llm_output(raw2, dut)
            _require_sane(design, tb, dut)
            _require_ports(design, spec)
            _require_tb_ports(tb, spec)
        if first_error:
            # surface the first rejection so a run that only barely worked
            # is diagnosable from gen.json alone
            warns_pre = [f"first attempt rejected: {first_error}"]
        else:
            warns_pre = []
        llm_used = True
    else:
        design, tb = builtin_template(spec)
        notes = "builtin offline template (no LLM)"

    from .fix import sanitize_design, sanitize_testbench

    # Both files are compiled by iverilog and yosys, so a botched directive
    # fails the whole candidate. Fix deterministically rather than spending
    # a repair round on a one-character mistake.
    design, d_warns = sanitize_design(design)
    tb, san_warns = sanitize_testbench(tb, dut)
    warns = warns_pre + d_warns + _validate(design, tb) + san_warns
    design_p.write_text(design)
    tb_p.write_text(tb)
    manifest = {
        "session": session_id,
        "dut": dut,
        "design": str(design_p),
        "testbench": str(tb_p),
        "llm_used": llm_used,
        "warnings": warns,
        "notes": notes,
    }
    (outdir / "gen.json").write_text(json.dumps(manifest, indent=2))
    return manifest
