"""M4: synthesis with yosys (+abc when liberty available) -> netlist + area."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import (
    generic_lib_path,
    pdk_lib_path,
    resolve_tool,
    session_dir,
    tool_env,
)
from .utils import run_logged


def _parse_stat(log: str) -> dict[str, Any]:
    """Parse yosys 'stat' output: cell counts + wires."""
    cells: dict[str, int] = {}
    # lines like: "  $_AND_    12" or "  AND2X1   5" or "Number of cells:  42"
    for m in re.finditer(r"^\s*(\$?\w+)\s+(\d+)\s*$", log, re.M):
        name, num = m.group(1), int(m.group(2))
        if name.startswith("$") or name.isupper() or "_" in name:
            cells[name] = num
    total = 0
    m = re.search(r"Number of cells:\s+(\d+)", log)
    if m:
        total = int(m.group(1))
    else:
        total = sum(cells.values())
    wires = 0
    m = re.search(r"Number of wires:\s+(\d+)", log)
    if m:
        wires = int(m.group(1))
    return {"cell_count": total, "wire_count": wires, "cells": cells}


def _find_liberty(explicit: str | None, pdk: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if p.exists():
            # Must be absolute: yosys runs with cwd=outdir, and ppa later
            # re-reads this path from the area.json record. A relative path
            # resolves against two different directories and fails both.
            return p.resolve()
        raise FileNotFoundError(f"liberty not found: {explicit}")
    # per-PDK lib if the user dropped one into pdks/<pdk>.lib
    per_pdk = pdk_lib_path(pdk)
    if per_pdk:
        return per_pdk
    g = generic_lib_path()
    return g if g.exists() else None


def run_synth(
    project_dir: Path,
    session_id: str,
    top: str | None = None,
    lib: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    yosys = resolve_tool("yosys")
    if not yosys:
        raise RuntimeError("yosys not found. Install yosys or set VLAB_YOSYS_BIN.")
    outdir = session_dir(project_dir, session_id)
    spec = json.loads((outdir / "spec.json").read_text())
    dut = top or spec["dut_name"]
    design = outdir / "design.v"
    if not design.exists():
        raise FileNotFoundError(f"{design} missing. Run `vlab gen` first.")
    liberty = _find_liberty(lib, spec.get("pdk_selected"))
    use_abc = liberty is not None

    netlist = outdir / "netlist.v"
    netjson = outdir / "netlist.json"
    script = outdir / "synth.ys"
    lines = [
        f"read_verilog {design}",
        f"hierarchy -check -top {dut}",
        "proc; opt; fsm; opt; memory; opt;",
    ]
    if use_abc:
        lines += [
            f"techmap; opt; dfflibmap -liberty {liberty}; opt;",
            f"abc -liberty {liberty}",
        ]
    else:
        lines.append("techmap; opt; abc; opt;")
    lines += ["opt; stat", f"write_verilog -noattr {netlist}", f"write_json {netjson}"]
    script.write_text("\n".join(lines) + "\n")

    env = tool_env()
    rc, log = run_logged([yosys, "-s", str(script)], cwd=outdir, timeout=timeout, extra_env=env)
    (outdir / "synth.log").write_text(log)
    area = _parse_stat(log)
    area.update(
        {
            "mapped": use_abc,
            "liberty": str(liberty) if liberty else None,
            # rough normalized area: cells (generic) — real um2 needs PDK
            "area_cells": area["cell_count"],
        }
    )
    (outdir / "area.json").write_text(json.dumps(area, indent=2))
    res = {
        "session": session_id,
        "dut": dut,
        "rc": rc,
        "netlist": str(netlist) if netlist.exists() else None,
        "netlist_json": str(netjson) if netjson.exists() else None,
        "log": str(outdir / "synth.log"),
        "area": area,
    }
    (outdir / "synth.json").write_text(json.dumps(res, indent=2))
    if rc != 0 or not netlist.exists():
        # area.json was already written above from a log that may only
        # contain an error; don't leave a bogus cell_count behind
        area["error"] = f"yosys rc={rc}"
        (outdir / "area.json").write_text(json.dumps(area, indent=2))
        raise RuntimeError(f"yosys failed rc={rc}. See {outdir / 'synth.log'}")
    return res
