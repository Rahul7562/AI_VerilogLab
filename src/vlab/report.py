"""M5 report: consolidate PPA.md + summary."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_report(outdir: Path) -> Path:
    spec = json.loads((outdir / "spec.json").read_text()) if (outdir / "spec.json").exists() else {}
    sim = json.loads((outdir / "sim.json").read_text()) if (outdir / "sim.json").exists() else {}
    synth = json.loads((outdir / "synth.json").read_text()) if (outdir / "synth.json").exists() else {}
    ppa = json.loads((outdir / "ppa.json").read_text()) if (outdir / "ppa.json").exists() else {}
    gen = json.loads((outdir / "gen.json").read_text()) if (outdir / "gen.json").exists() else {}

    area = ppa.get("area", synth.get("area", {}))
    timing = ppa.get("timing", {})
    power = ppa.get("power", {})

    def fmt(v: Any) -> str:
        return "n/a" if v is None else str(v)

    md = f"""# VerilogLab PPA Report — {spec.get('dut_name', outdir.name)}

## Spec
- DUT: `{spec.get('dut_name', '?')}`
- Description: {spec.get('description', '')[:500]}
- PDK requested: `{spec.get('pdk_choice', '?')}` -> selected `{spec.get('pdk_selected', '?')}`
- Reason: {spec.get('pdk_reason', '')}
- Target: {spec.get('target_freq_mhz', ppa.get('freq_mhz', '?'))} MHz

## Simulation ({sim.get('tool', '?')})
- Status: **{sim.get('status', '?')}** (pass={sim.get('pass_count', '?')}, fail={sim.get('fail_count', '?')})
- VCD: `{sim.get('vcd', ppa.get('vcd', 'none'))}` ({sim.get('vcd_bytes', 0)} bytes)
- Log: `{sim.get('log', '')}`

## Synthesis (yosys{' +abc' if (area.get('mapped')) else ''})
- Cells: {fmt(area.get('cell_count', area.get('area_cells')))}  Wires: {fmt(area.get('wire_count'))}
- Liberty: `{fmt(area.get('liberty'))}`
- Netlist: `{synth.get('netlist', '')}`

## Timing ({timing.get('method', '?')})
- WNS (ns): {fmt(timing.get('wns_ns'))}  TNS (ns): {fmt(timing.get('tns_ns'))}  Crit (ns): {fmt(timing.get('crit_ns'))}

## Power ({power.get('method', '?')})
- Power (W): {fmt(power.get('power_w'))}
- Detail: {power.get('raw', power.get('note', ''))}

## Files
- design: `{gen.get('design', 'design.v')}`
- testbench: `{gen.get('testbench', '')}`
- warnings: {gen.get('warnings', [])}
"""
    out = outdir / "PPA.md"
    out.write_text(md)
    return out
