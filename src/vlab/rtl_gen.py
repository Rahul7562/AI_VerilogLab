"""M2: generate design.v + testbench from spec.json via OpenRouter (Nemotron)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GEN_PROMPT = """You are an expert Verilog RTL engineer. Given the DUT spec JSON below,
output exactly two Verilog files.

Rules:
- design.v: module name MUST be {dut}. Synthesizable Verilog-2005, no SystemVerilog-only constructs.
- Ports must match spec (names, directions, widths). Keep clk/rst if present.
- Include `timescale 1ns/1ps is NOT needed in design, only testbench.
- testbench: module name tb_{dut}. Must include `timescale 1ns/1ps,
  $dumpfile("wave.vcd"), $dumpvars(0, tb_{dut}), clock gen if sequential,
  reset sequence, at least 5 directed checks with $display("PASS:...") / $display("FAIL:..."),
  and $finish. Drive all inputs, never leave X.
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
    if len(blocks) == 1 and f"module {dut}" in blocks[0] and f"tb_{dut}" in blocks[0]:
        # single block containing both — split on testbench module
        idx = blocks[0].find(f"module tb_{dut}")
        return blocks[0][:idx].strip(), blocks[0][idx:].strip(), ""
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
        logic = (
            f"  always @(posedge {clk_p['name']}) begin\n"
            + (f"    if ({rst_expr}) {y['name']} <= {{{yw}{{1'b0}}}};\n" if rst_p else "")
            + f"    else {y['name']} <= {a['name']} + {b['name']};\n"
            f"  end"
        )
    else:
        logic = f"  always @(*) {y['name']} = {a['name']} + {b['name']};"

    design = (
        f"// Auto template for {dut} (offline fallback, no LLM)\n"
        f"module {dut} (\n{decls}\n);\n{logic}\nendmodule\n"
    )
    # testbench decls + instantiation derived from spec ports
    tb_decls, hooks = [], []
    for p in ports:
        pw = int(p.get("width", 1))
        rng = "" if pw <= 1 else f" [{pw-1}:0]"
        if p["direction"] == "input":
            tb_decls.append(f"  reg{rng} {p['name']} = 0;")
        elif p["direction"] == "output":
            tb_decls.append(f"  wire{rng} {p['name']};")
        else:
            tb_decls.append(f"  wire{rng} {p['name']};")
        hooks.append(f"    .{p['name']}({p['name']})")
    tb_body = "\n".join(tb_decls) + "\n"
    tb_inst = f"  {dut} dut (\n" + ",\n".join(hooks) + "\n  );\n"
    clkgen = f"  always #5 {clk_p['name']} = ~{clk_p['name']};\n" if has_clk else ""
    rstseq = (
        f"    {rst_p['name']} = {'0' if 'n' in rst_p['name'].lower() else '1'}; #20; "
        f"{rst_p['name']} = {'1' if 'n' in rst_p['name'].lower() else '0'};\n"
        if rst_p else ""
    )
    v1, v2 = 3 % (1 << aw), 4 % (1 << bw)
    e1 = (v1 + v2) % (1 << yw)
    v3, v4 = 10 % (1 << aw), 20 % (1 << bw)
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


def _validate(design: str, tb: str, dut: str) -> list[str]:
    warns: list[str] = []
    if f"module {dut}" not in design:
        warns.append(f"design missing 'module {dut}'")
    if "endmodule" not in design:
        warns.append("design missing endmodule")
    if f"tb_{dut}" not in tb:
        warns.append(f"testbench missing tb_{dut}")
    if "$dumpfile" not in tb or "$dumpvars" not in tb:
        warns.append("testbench missing $dumpfile/$dumpvars (wave.vcd won't generate)")
    if "$finish" not in tb:
        warns.append("testbench missing $finish")
    return warns


def run_gen(
    project_dir: Path,
    session_id: str,
    spec_path: Path,
    use_llm: bool = True,
    force: bool = False,
    max_tokens: int = 4096,
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
    if use_llm:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            raise RuntimeError("OPENROUTER_API_KEY not set. Use --no-llm for template.")
        prompt = GEN_PROMPT.format(dut=dut, spec=json.dumps(spec, indent=2))
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
        design, tb, notes = parse_llm_output(raw, dut)
        llm_used = True
        (outdir / "gen_raw.txt").write_text(raw)
    else:
        design, tb = builtin_template(spec)
        notes = "builtin offline template (no LLM)"

    warns = _validate(design, tb, dut)
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
