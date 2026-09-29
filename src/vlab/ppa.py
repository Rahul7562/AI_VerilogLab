"""M5: PPA — area (yosys), timing + power (openSTA + VCD)."""
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


def _parse_sta_checks(log: str) -> dict[str, Any]:
    slacks: list[float] = []
    for m in re.finditer(r"(-?[\d.]+)\s+slack \((?:MET|VIOLATED)\)", log):
        try:
            slacks.append(float(m.group(1)))
        except ValueError:
            pass
    wns = min(slacks) if slacks else None
    # NOTE: this is the sum over the paths report_checks actually printed
    # (capped by -group_count), not a whole-design TNS.
    tns = round(sum(s for s in slacks if s < 0), 3) if slacks else None
    # the report prints arrival twice per path (positive in the path trace,
    # negated in the slack section); max() keeps the real one
    arrs: list[float] = []
    for x in re.findall(r"^\s*(-?[\d.]+)\s+data arrival time", log, re.M):
        try:
            arrs.append(float(x))
        except ValueError:
            continue  # a malformed column must not sink the whole report
    crit = max(arrs) if arrs else None
    paths = len(re.findall(r"^\s*Startpoint:", log, re.M))
    # fallback to old wns/tns keywords
    if wns is None:
        wns = _first_float(log, r"wns\s+(-?[\d.]+)")
    if tns is None:
        tns = _first_float(log, r"tns\s+(-?[\d.]+)")
    return {"wns_ns": wns, "tns_ns": tns, "crit_ns": crit, "paths": paths}


def _first_float(log: str, pattern: str) -> float | None:
    m = re.search(pattern, log, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


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


def _scan_vcd(vcd: Path, limit: int = 200_000) -> tuple[int, int]:
    """One pass over a VCD -> (toggle count, declared signal count).

    Shared by the activity and power-estimate paths, which previously
    duplicated the scan with slightly different rules. The header
    ($var/$scope/...) lines do not count toward `limit`, so a header-heavy
    VCD does not starve the value-change scan.
    """
    toggles = 0
    signals = 0
    scanned = 0
    try:
        with open(vcd, encoding="utf-8", errors="ignore") as f:
            in_header = True
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if in_header:
                    if s.startswith("$var"):
                        signals += 1
                    elif s.startswith("$enddefinitions") or s.startswith("#"):
                        # $enddefinitions is the normal terminator; a leading
                        # "#<time>" also means the value section has begun,
                        # which keeps truncated/hand-written VCDs working
                        in_header = False
                    continue
                scanned += 1
                if scanned > limit:
                    break
                if s[0] in "01xXzZbB" and len(s) > 1:
                    toggles += 1
    except (OSError, ValueError):
        return 0, 0
    return toggles, signals


def _vcd_toggle_estimate(vcd: Path, freq_mhz: float = 100.0) -> dict[str, Any]:
    """Fallback power: count value changes in VCD -> rough dynamic estimate."""
    toggles, signals = _scan_vcd(vcd)
    if signals == 0 and toggles == 0:
        return {"method": "none", "power_w": None}
    # P ~ toggles * C * V^2 * f normalized — placeholder scaling, clearly labeled estimate
    est_uw = toggles * 0.001 * (freq_mhz / 100.0)
    return {
        "method": "vcd-toggle-estimate",
        "toggles": toggles,
        "signals": signals,
        "power_w": est_uw / 1e6,
        "note": "estimate only (no STA power). Provide PDK liberty for real numbers.",
    }


def _vcd_activity(vcd: Path) -> tuple[float, int, int]:
    """Parse VCD -> (activity 0..1, toggles, signals)."""
    toggles, signals = _scan_vcd(vcd)
    if signals <= 0:
        return 0.1, toggles, 0
    # avg value changes per signal per scanned VCD line, clamped to a
    # plausible 1%-50% switching range for set_power_activity
    act = max(0.01, min(0.5, toggles / max(1, signals * 200)))
    return act, toggles, signals


def _sdc_for(outdir: Path, clk_port: str, clk_period_ns: float) -> Path:
    """Write design.sdc with explicit data-input / data-output port lists.

    The port sets are built with a small Tcl loop rather than the usual
    `[all_inputs -no_clocks]`. On the bundled OpenSTA 2.0.x that collection
    filter evaluates to an EMPTY set, so every input delay is dropped and
    all input->register timing disappears: the report then shows a trivial
    flop->output hop (0.09 ns) as the "critical path" and derives a 11 THz
    fmax from it. `remove_from_collection` fails the same way. Explicitly
    walking the ports behaves identically on old and new OpenSTA.
    """
    sdc = outdir / "design.sdc"
    margin = clk_period_ns * 0.2
    # Tcl braces must be doubled inside f-strings; assembling the variable
    # parts separately keeps the literal braces readable.
    clock_line = (
        f"create_clock -name clk -period {clk_period_ns:g} "
        f"[get_ports {{{clk_port}}}]"
    )
    skip_clk = (
        "  if {[get_full_name $vlab_p] eq {" + clk_port + "}} "
        "{ continue }"
    )
    sdc.write_text(
        "\n".join([
            "# generated by vlab",
            clock_line,
            "# Data inputs: every input port except the clock. Built with an",
            "# explicit loop because OpenSTA 2.0.x returns an EMPTY set for",
            "# the usual [all_inputs -no_clocks] / remove_from_collection",
            "# idioms, which silently drops all input delays.",
            "set vlab_data_inputs [list]",
            "foreach vlab_p [get_ports *] {",
            "  if {[get_property $vlab_p direction] ne \"input\"} { continue }",
            skip_clk,
            "  lappend vlab_data_inputs $vlab_p",
            "}",
            "if {[llength $vlab_data_inputs] > 0} {",
            f"  set_input_delay {margin:.4f} -clock clk $vlab_data_inputs",
            "}",
            "set vlab_data_outputs [list]",
            "foreach vlab_p [get_ports *] {",
            "  if {[get_property $vlab_p direction] eq \"output\"} {",
            "    lappend vlab_data_outputs $vlab_p",
            "  }",
            "}",
            "if {[llength $vlab_data_outputs] > 0} {",
            f"  set_output_delay {margin:.4f} -clock clk $vlab_data_outputs",
            "}",
            "",
        ])
    )
    return sdc


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
    sdc = _sdc_for(outdir, clk_port, clk_period_ns)
    activity, toggles, signals = (0.1, 0, 0)
    if vcd and vcd.exists():
        activity, toggles, signals = _vcd_activity(vcd)
    tcl = outdir / "sta.tcl"
    # NOTE: bundled opensta is 2.0.x (no read_vcd/read_saif). Feed VCD-derived
    # switching activity via set_power_activity; newer STA would use read_vcd.
    # Tcl splits on whitespace, so paths and the module name are
    # brace-quoted: a project path containing a space would otherwise
    # produce a Tcl error rather than a Python one.
    lines = [
        f"read_liberty {{{lib}}}",
        f"read_verilog {{{netlist}}}",
        f"link_design {{{top}}}",
        f"read_sdc {{{sdc}}}",
        f"set_power_activity -global -activity {activity:.4f} -duty 0.5",
        f"set_power_activity -input -activity {activity:.4f} -duty 0.5",
        # A bare `report_checks` prints only the single worst-slack path,
        # which is often a trivial flop->output hop; that made crit_ns read
        # 0.09 ns (=> 11 THz fmax) for a real adder. Ask for many paths per
        # group and take the true maximum data arrival when parsing.
        "report_checks -path_delay max -group_count 50 -digits 4",
        "report_power",
    ]
    tcl.write_text("\n".join(lines) + "\n")
    env = tool_env()
    rc, log = run_logged([sta, "-no_init", "-exit", str(tcl)], cwd=outdir, timeout=timeout, extra_env=env)
    (outdir / "sta.log").write_text(log)
    return {"rc": rc, "log": log, "tcl": str(tcl),
            "activity": activity, "toggles": toggles, "signals": signals}


def _sta_liberty(outdir: Path, area: dict[str, Any]) -> Path:
    """Liberty for STA: the one synthesis actually mapped against.

    Reading a different library than the netlist was built with produces
    cell names OpenSTA cannot link, or (worse) silently wrong timing. The
    mapping liberty is recorded in area.json, so prefer it and fall back to
    the per-PDK lib that synth._find_liberty would have chosen.
    """
    recorded = area.get("liberty")
    if recorded:
        p = Path(recorded)
        if p.exists():
            return p
    # mirror synth's selection: pdks/<pdk>.lib then generic
    per_pdk = pdk_lib_path(_spec_pdk(outdir))
    return per_pdk or generic_lib_path()


def _spec_pdk(outdir: Path) -> str | None:
    p = outdir / "spec.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("pdk_selected")
    except (json.JSONDecodeError, ValueError, OSError):
        return None


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
    # Must use the SAME liberty synthesis mapped against, otherwise STA reads
    # cell timing that does not match the netlist's cells. Prefer the lib
    # recorded in area.json, fall back to the per-PDK/generic choice.
    lib = _sta_liberty(outdir, area)
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
            # a nonzero rc (or no usable path) must not be advertised as a
            # successful openSTA measurement — the report would otherwise
            # print "## Timing (opensta)" followed by n/a
            complete = r["rc"] == 0 and timing.get("wns_ns") is not None
            timing.update({
                "method": "opensta" if complete else "opensta-incomplete",
                "rc": r["rc"],
                "period_ns": round(period, 4),
                "log": str(outdir / "sta.log"),
            })
            if timing.get("crit_ns") is None:
                # a purely combinational design has no startpoint->capture
                # path, so say so instead of leaving a bare null
                timing["note"] = (
                    "no timing paths found (combinational-only netlist, or the "
                    f"clock port '{clk}' is absent from the design)"
                )
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
