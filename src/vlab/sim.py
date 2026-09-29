"""M3: simulate via iverilog/vvp or verilator -> wave.vcd + pass/fail."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import resolve_tool, session_dir, tool_env
from .utils import run_logged


def _find_rtl(outdir: Path, dut: str) -> tuple[Path, Path]:
    design = outdir / "design.v"
    tb = outdir / f"tb_{dut}.v"
    vfiles = sorted(outdir.glob("*.v"))
    if not design.exists():
        # Only a file that actually declares `module <dut>` can be the DUT;
        # alphabetical order otherwise picks a stray .v, and golden_tb_* is
        # a frozen copy that must never be synthesised or simulated as RTL.
        cands = [
            p for p in vfiles
            if not p.name.startswith("golden_tb_")
            and p.name != f"tb_{dut}.v"
            and re.search(rf"\bmodule\s+{re.escape(dut)}\b",
                          p.read_text(errors="ignore")[:4000])
        ]
        if cands:
            design = cands[0]
    if not tb.exists():
        cands = [p for p in vfiles if p != design
                 and p.name.startswith("tb_")]
        if cands:
            tb = cands[0]
    if not design.exists() or not tb.exists():
        raise FileNotFoundError(f"RTL not found in {outdir}. Run `vlab gen` first.")
    return design, tb


def _clear_stale(outdir: Path) -> None:
    """Remove artifacts from a previous run.

    A leftover wave.vcd from an earlier passing simulation would otherwise be
    reported for a run that just failed, feeding wrong switching activity into
    the power estimate.
    """
    for name in ("wave.vcd", "sim.vvp"):
        p = outdir / name
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _parse_result(log: str, rc: int = 0) -> dict[str, Any]:
    passes = len(re.findall(r"PASS[:\s]", log))
    # Anchor to a display-like line so prose such as "Note: PASS/FAIL summary
    # below" or an echoed command line is not scored as a failing check.
    fails = len(re.findall(r"(?m)^\s*(?:[^:\n]*:\s*)*FAIL\b", log))
    fails += len(re.findall(r"ERROR[:\s]|ASSERT", log))
    # $fatal / assertion failures
    if re.search(r"FATAL|\\$fatal", log):
        fails += 1
    finished = re.search(r"\$finish|finish called", log, re.I) is not None
    # A run is only a pass if it terminated, had no failing checks, AND
    # actually asserted something. Without the last condition a testbench
    # that compiles, runs and $finishes with zero checks would be reported
    # as a pass with 0 passes — a silent false positive that lets an
    # unverified DUT win selection.
    ran = "PASS" in log or "DONE" in log or finished
    ok = rc == 0 and fails == 0 and ran and (passes > 0 or fails > 0)
    status = "pass" if ok else "fail"
    out: dict[str, Any] = {
        "pass_count": passes, "fail_count": fails, "status": status,
    }
    if rc == 0 and passes == 0 and fails == 0:
        out["note"] = "testbench made no assertions (0 PASS, 0 FAIL)"
    return out


def _iverilog_base_args(iv: str) -> list[str]:
    """Return [-B <dir>] when using extracted user-space iverilog."""
    import os

    explicit = os.getenv("VLAB_IVERILOG_BASE", "").strip()
    if explicit:
        return ["-B", explicit]
    # known bundled locations containing ivl/ivl
    candidates = [
        Path.home() / ".local" / "edatools" / "usr" / "lib" / "x86_64-linux-gnu" / "ivl",
        Path("/tmp/edatools/extract/usr/lib/x86_64-linux-gnu/ivl"),
        Path("/usr/lib/x86_64-linux-gnu/ivl"),
    ]
    iv_path = Path(iv)
    # if binary lives under an extract root, derive base from it
    for ancestor in iv_path.parents:
        cand = ancestor.parent / "lib" / "x86_64-linux-gnu" / "ivl"
        if (cand / "ivl").exists():
            return ["-B", str(cand)]
    for c in candidates:
        if (c / "ivl").exists():
            return ["-B", str(c)]
    return []


def run_iverilog(outdir: Path, design: Path, tb: Path, timeout: int = 120) -> dict[str, Any]:
    iv = resolve_tool("iverilog")
    vvp = resolve_tool("vvp")
    if not iv or not vvp:
        raise RuntimeError("iverilog/vvp not found. Install iverilog or set VLAB_IVERILOG_BIN.")
    vvp_out = outdir / "sim.vvp"
    _clear_stale(outdir)
    env = tool_env()
    base_args = _iverilog_base_args(iv)
    rc1, log1 = run_logged([iv, *base_args, "-g2012", "-o", str(vvp_out), str(design), str(tb)], cwd=outdir, timeout=timeout, extra_env=env)
    log = f"$ iverilog compile rc={rc1}\n{log1}\n"
    rc: int = rc1
    if rc1 == 0:
        rc2, log2 = run_logged([vvp, str(vvp_out)], cwd=outdir, timeout=timeout, extra_env=env)
        log += f"$ vvp rc={rc2}\n{log2}\n"
        rc = rc2
    (outdir / "sim.log").write_text(log)
    vcd = outdir / "wave.vcd"
    # vvp may write wave.vcd into cwd (outdir) since $dumpfile relative
    parsed = _parse_result(log, rc)
    return {
        "tool": "iverilog",
        "rc": rc,
        "log": str(outdir / "sim.log"),
        "vcd": str(vcd) if vcd.exists() else None,
        "vcd_bytes": vcd.stat().st_size if vcd.exists() else 0,
        **parsed,
    }


def run_verilator(outdir: Path, design: Path, tb: Path, timeout: int = 180) -> dict[str, Any]:
    ver = resolve_tool("verilator")
    if not ver:
        raise RuntimeError("verilator not found. Install verilator or set VLAB_VERILATOR_BIN.")
    # verilator --binary shells out to make + a C++ compiler; without them it
    # fails deep inside with an opaque rc=127 instead of saying what's missing
    if resolve_tool("make") is None:
        raise RuntimeError(
            "verilator --binary requires `make` (and a C++ compiler) to build the "
            "testbench executable. Install build-essential, or use --tool iverilog."
        )
    env = tool_env()
    _clear_stale(outdir)
    # --binary builds a model executable; needs --top-module tb.
    # --trace is required for $dumpfile/$dumpvars to emit a VCD — without it
    # the whole PPA power path (which needs wave.vcd) silently degrades.
    tb_top = tb.stem
    exe = outdir / f"V{tb_top}"
    cmd = [
        ver, "--binary", "-j", "0", "--trace", "--trace-structs",
        # Verilator exits nonzero on any warning by default; lint noise
        # (missing timescale, unused bits) must not fail the simulation.
        "-Wno-fatal", "-Wno-TIMESCALEMOD", "-Wno-WIDTH", "-Wno-UNUSED",
        "-Wno-DECLFILENAME", "-Wno-EOFNEWLINE",
        "--top-module", tb_top, "-o", str(exe), str(design), str(tb),
    ]
    rc1, log1 = run_logged(cmd, cwd=outdir, timeout=timeout, extra_env=env)
    log = f"$ {' '.join(cmd)} rc={rc1}\n{log1}\n"
    rc = rc1
    if rc1 == 0 and exe.exists():
        rc2, log2 = run_logged([str(exe)], cwd=outdir, timeout=timeout, extra_env=env)
        log += f"$ {exe.name} rc={rc2}\n{log2}\n"
        rc = rc2
    (outdir / "sim.log").write_text(log)
    vcd = outdir / "wave.vcd"
    parsed = _parse_result(log, rc)
    if vcd.exists():
        parsed["vcd"] = str(vcd)
        parsed["vcd_bytes"] = vcd.stat().st_size
    else:
        parsed["vcd"] = None
        log += (
            "NOTE: no wave.vcd produced. Verilator only dumps a trace when the "
            "testbench calls $dumpfile/$dumpvars.\n"
        )
        (outdir / "sim.log").write_text(log)
    return {"tool": "verilator", "rc": rc, "log": str(outdir / "sim.log"), **parsed}


def run_simulate(
    project_dir: Path,
    session_id: str,
    tool: str = "auto",
    timeout: int = 120,
) -> dict[str, Any]:
    # iverilog/vvp/verilator run with cwd=outdir, so the design and testbench
    # paths handed to them must be absolute
    project_dir = Path(project_dir).resolve()
    outdir = session_dir(project_dir, session_id)
    spec_p = outdir / "spec.json"
    if not spec_p.exists():
        # sessions/ mirror
        spec_p = project_dir / "sessions" / session_id / "spec.json"
    dut = json.loads(spec_p.read_text())["dut_name"]
    design, tb = _find_rtl(outdir, dut)
    tool = tool.lower()
    if tool == "auto":
        tool = "iverilog" if resolve_tool("iverilog") else "verilator"
    if tool == "iverilog":
        res = run_iverilog(outdir, design, tb, timeout)
    elif tool == "verilator":
        res = run_verilator(outdir, design, tb, timeout)
    else:
        raise ValueError("tool must be auto|iverilog|verilator")
    res.update({"session": session_id, "dut": dut, "design": str(design), "testbench": str(tb)})
    (outdir / "sim.json").write_text(json.dumps(res, indent=2))
    return res
