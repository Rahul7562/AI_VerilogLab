"""M5: PPA — area (yosys), timing + power (openSTA + VCD)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import generic_lib_path, resolve_tool, session_dir, tool_env
from .utils import run_logged


def _parse_sta_checks(log: str) -> dict[str, Any]:
    slacks: list[float] = []
    for m in re.finditer(r"(-?[\d.]+)\s+slack \((?:MET|VIOLATED)\)", log):
        try:
            slacks.append(float(m.group(1)))
        except ValueError:
            pass
    wns = min(slacks) if slacks else None
    tns = round(sum(s for s in slacks if s < 0), 3) if slacks else None
    arrs = [float(x) for x in re.findall(r"^\s*([\d.]+)\s+data arrival time", log, re.M)]
    crit = max(arrs) if arrs else None
    # fallback to old wns/tns keywords
    if wns is None:
        for m in re.finditer(r"wns\s+(-?[\d.]+)", log, re.I):
            wns = float(m.group(1))
    if tns is None:
        for m in re.finditer(r"tns\s+(-?[\d.]+)", log, re.I):
            tns = float(m.group(1))
    return {"wns_ns": wns, "tns_ns": tns, "crit_ns": crit}


def _parse_sta_power(log: str) -> dict[str, Any]:
    # opensta report_power table: columns Internal Switching Leakage Total + pct.
    # Take the 4th number (Total power) on the '^Total' row — never the % column.
    out: dict[str, Any] = {"power_w": None, "raw": None}
    for line in log.splitlines():
        m = re.match(
            r"\s*Total\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)",
            line,
        )
        if m:
            try:
                out["power_w"] = float(m.group(4))
                out["raw"] = line.strip()[:200]
            except ValueError:
                pass
    return out


def _vcd_toggle_estimate(vcd: Path, freq_mhz: float = 100.0) -> dict[str, Any]:
    """Fallback power: count value changes in VCD -> rough dynamic estimate."""
    toggles = 0
    signals = set()
    try:
        with open(vcd, errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 200000:
                    break
                if line.startswith("$var"):
                    signals.add(line)
                elif line and line[0] in "01xXzZ" and len(line) > 1:
                    toggles += 1
    except FileNotFoundError:
        return {"method": "none", "power_w": None}
    # P ~ toggles * C * V^2 * f normalized — placeholder scaling, clearly labeled estimate
    est_uw = toggles * 0.001 * (freq_mhz / 100.0)
    return {
        "method": "vcd-toggle-estimate",
        "toggles": toggles,
        "signals": len(signals),
        "power_w": est_uw / 1e6,
        "note": "estimate only (no STA power). Provide PDK liberty for real numbers.",
    }


def _vcd_activity(vcd: Path) -> tuple[float, int, int]:
    """Parse VCD -> (activity 0..1, toggles, signals)."""
    toggles = 0
    signals = 0
    try:
        with open(vcd, errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 200000:
                    break
                s = line.strip()
                if s.startswith("$var"):
                    signals += 1
                elif s and s[0] in "01xXzZbB" and len(s) > 1:
                    toggles += 1
    except FileNotFoundError:
        return 0.1, 0, 0
    if signals <= 0:
        return 0.1, toggles, 0
    # activity = avg toggles per signal per 1000 time units, clamped 0.01..0.5
    act = max(0.01, min(0.5, toggles / max(1, signals * 200)))
    return act, toggles, signals


def run_sta(
    outdir: Path,
    top: str,
    netlist: Path,
    lib: Path,
    vcd: Path | None,
    clk_port: str = "clk",
    clk_period_ns: float = 10.0,
    timeout: int = 120,
) -> dict[str, Any]:
    sta = resolve_tool("sta")
    if not sta:
        raise RuntimeError("sta (OpenSTA) not found.")
    sdc = outdir / "design.sdc"
    margin = clk_period_ns * 0.2
    sdc.write_text(
        f"create_clock -name clk -period {clk_period_ns} [get_ports {clk_port}]\n"
        f"set_input_delay {margin:.3f} -clock clk [all_inputs]\n"
        f"set_output_delay {margin:.3f} -clock clk [all_outputs]\n"
    )
    activity, toggles, signals = (0.1, 0, 0)
    if vcd and vcd.exists():
        activity, toggles, signals = _vcd_activity(vcd)
    tcl = outdir / "sta.tcl"
    # NOTE: bundled opensta is 2.0.x (no read_vcd/read_saif). Feed VCD-derived
    # switching activity via set_power_activity; newer STA would use read_vcd.
    lines = [
        f"read_liberty {lib}",
        f"read_verilog {netlist}",
        f"link_design {top}",
        f"read_sdc {sdc}",
        f"set_power_activity -global -activity {activity:.4f} -duty 0.5",
        f"set_power_activity -input -activity {activity:.4f} -duty 0.5",
        "report_checks",
        "report_power",
    ]
    tcl.write_text("\n".join(lines) + "\n")
    env = tool_env()
    rc, log = run_logged([sta, "-no_init", "-exit", str(tcl)], cwd=outdir, timeout=timeout, extra_env=env)
    (outdir / "sta.log").write_text(log)
    return {"rc": rc, "log": log, "tcl": str(tcl),
            "activity": activity, "toggles": toggles, "signals": signals}


def run_ppa(
    project_dir: Path,
    session_id: str,
    freq_mhz: float = 100.0,
    clk_port: str = "clk",
    timeout: int = 120,
) -> dict[str, Any]:
    outdir = session_dir(project_dir, session_id)
    area_p = outdir / "area.json"
    area: dict[str, Any] = json.loads(area_p.read_text()) if area_p.exists() else {}
    netlist = outdir / "netlist.v"
    if not netlist.exists():
        raise FileNotFoundError("netlist.v missing. Run `vlab synth` first.")
    vcd = outdir / "wave.vcd"
    lib = generic_lib_path()
    timing: dict[str, Any] = {"method": "none"}
    power: dict[str, Any] = {"method": "none"}

    sta_bin = resolve_tool("sta")
    if sta_bin and lib.exists():
        try:
            period = 1000.0 / freq_mhz
            spec = json.loads((outdir / "spec.json").read_text())
            top = spec.get("dut_name", session_id.split("_")[0])
            clk = clk_port or spec.get("clock", "clk") or "clk"
            r = run_sta(outdir, top, netlist, lib, vcd if vcd.exists() else None, clk, period, timeout)
            timing = _parse_sta_checks(r["log"])
            timing.update({"method": "opensta", "rc": r["rc"], "log": str(outdir / "sta.log")})
            pp = _parse_sta_power(r["log"])
            if pp.get("power_w") is not None:
                power = {"method": "opensta+vcd-activity", **pp,
                         "activity": r.get("activity"), "toggles": r.get("toggles")}
            else:
                power = _vcd_toggle_estimate(vcd, freq_mhz) if vcd.exists() else pp
                power["sta_fallback"] = True
            (outdir / "timing.rpt").write_text(r["log"])
        except Exception as e:  # noqa: BLE001
            timing = {"method": "opensta-failed", "error": str(e)[:300]}
            power = _vcd_toggle_estimate(vcd, freq_mhz) if vcd.exists() else {"method": "none"}
    else:
        if vcd.exists():
            power = _vcd_toggle_estimate(vcd, freq_mhz)
        timing = {
            "method": "estimate",
            "note": "no sta/liberty; timing unavailable. Provide PDK lib for real STA.",
            "wns_ns": None,
        }

    res = {
        "session": session_id,
        "freq_mhz": freq_mhz,
        "area": area,
        "timing": timing,
        "power": power,
    }
    if vcd.exists():
        res["vcd"] = str(vcd)
    (outdir / "ppa.json").write_text(json.dumps(res, indent=2))
    return res
