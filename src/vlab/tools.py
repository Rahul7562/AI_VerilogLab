"""EDA tool detection for M0. M3-M5 will actually invoke these tools."""
from __future__ import annotations

from dataclasses import dataclass

from .utils import run_capture

@dataclass
class ToolStatus:
    name: str
    binary: str
    found: bool
    version_hint: str = ""
    purpose: str = ""


TOOL_SPECS: list[tuple[str, str, list[str], str]] = [
    # (display, binary, version_args, purpose)
    ("Icarus Verilog", "iverilog", ["-V"], "simulation, VCD waveform"),
    ("VVP runtime", "vvp", ["-V"], "run iverilog output"),
    ("Verilator", "verilator", ["--version"], "alt simulator"),
    ("Yosys", "yosys", ["-V"], "synthesis + area (stat)"),
    ("ABC (yosys-abc)", "yosys-abc", ["-h"], "tech mapping via yosys"),
    ("OpenSTA", "sta", ["-v"], "timing + power from VCD"),
    ("GTKWave", "gtkwave", ["--version"], "optional waveform viewer"),
]


def check_tools() -> list[ToolStatus]:
    from .config import resolve_tool as _resolve

    results: list[ToolStatus] = []
    for display, binary, ver_args, purpose in TOOL_SPECS:
        path = _resolve(binary)
        if not path:
            results.append(ToolStatus(display, binary, False, "", purpose))
            continue
        rc, out = run_capture([binary, *ver_args])
        hint = ""
        if out.strip():
            hint = out.strip().splitlines()[0][:120]
        results.append(ToolStatus(display, binary, True, hint, purpose))
    return results
