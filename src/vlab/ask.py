"""M1: autonomous DUT Q&A -> spec.json (with PDK auto-select)."""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

import typer

from .config import sessions_dir
from .spec import (
    default_spec,
    heuristic_pdk,
    llm_pick_pdk,
    validate_module_name,
    validate_pdk_choice,
    validate_spec,
)


def _session_id(dut: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{dut}_{ts}"


def _ask(text: str, default: str = "") -> str:
    prompt = f"{text} [{default}]" if default else text
    try:
        out = typer.prompt(prompt, default=default)
    except (EOFError, KeyboardInterrupt):
        raise typer.Exit(code=1)
    return str(out).strip() or default


def _parse_ports(raw: str, width: int) -> list[dict[str, Any]]:
    """Parse 'a in, y out, clk in' or 'a[7:0] in' into port dicts."""
    ports: list[dict[str, Any]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split()
        name = parts[0].strip().strip(",")
        direction = "input"
        if len(parts) > 1:
            d = parts[1].lower()
            if d in ("in", "input"):
                direction = "input"
            elif d in ("out", "output"):
                direction = "output"
            elif d in ("inout",):
                direction = "inout"
        # width from [msb:lsb] in name
        w = 1
        if "[" in name and "]" in name:
            m = re.search(r"\[(\d+):(\d+)\]", name)
            if m:
                msb, lsb = int(m.group(1)), int(m.group(2))
                w = abs(msb - lsb) + 1
                name = re.sub(r"\s*\[.*\]", "", name)
            else:
                w = width
        else:
            # data ports default to data_width, clk/rst to 1
            if name.lower() in ("clk", "clock", "rst", "reset", "rst_n", "rstn"):
                w = 1
            else:
                w = width
        ports.append({"name": name, "direction": direction, "width": w})
    return ports


def _llm_followups(dut: str, desc: str) -> list[str]:
    """Ask LLM for 3-5 clarifying questions. Fallback to defaults."""
    try:
        from .llm import chat as llm_chat, get_api_key

        if not get_api_key():
            return []
        out = llm_chat(
            [
                {
                    "role": "user",
                    "content": (
                        f"Design is a Verilog DUT named {dut}: {desc}. "
                        "List 3-5 short clarifying questions (one per line) an RTL "
                        "engineer needs: interfaces, clock/reset, edge cases, "
                        "throughput, test scenarios. Questions only, no answers."
                    ),
                }
            ],
            max_tokens=256,
            temperature=0.4,
        )
        qs = [l.strip("- *\t0123456789. ") for l in out.splitlines()]
        return [q for q in qs if len(q) > 8][:5]
    except Exception:
        return []


def run_new(
    project_dir: Path,
    dut: str | None = None,
    non_interactive: bool = False,
    desc: str = "",
    width: int = 8,
    pdk: str = "auto",
    use_llm: bool = True,
    category: str = "sequential",
) -> tuple[str, Path]:
    """Run Q&A (or non-interactive) and write sessions/<id>/spec.json."""
    spec = default_spec(dut or "my_dut")

    if non_interactive:
        dut_name = validate_module_name(dut or "my_dut")
        spec.update(
            {
                "dut_name": dut_name,
                "description": desc or f"{dut_name} generated non-interactively",
                "category": category,
                "data_width": int(width),
                "pdk_choice": validate_pdk_choice(pdk),
            }
        )
        if not spec["ports"]:
            w = int(width)
            spec["ports"] = [
                {"name": "clk", "direction": "input", "width": 1},
                {"name": "rst_n", "direction": "input", "width": 1},
                {"name": "a", "direction": "input", "width": w},
                {"name": "b", "direction": "input", "width": w},
                {"name": "y", "direction": "output", "width": w},
            ]
            spec["features"] = ["combinational/sequential datapath", "reset"]
            spec["test_plan"] = ["directed basics", "random smoke", "reset check"]
    else:
        typer.echo("=== AI VerilogLab: DUT requirements ===")
        dut_name = validate_module_name(
            _ask("DUT/module name", dut or spec["dut_name"])
        )
        description = _ask(
            "What does the DUT do? (1-2 sentences)",
            desc or "",
        )
        while not description:
            description = _ask("What does the DUT do? (required)")
        category = _ask("Category [combinational/sequential/fsm/memory/interface]", category)
        width_raw = _ask("Default data width", str(width))
        try:
            width = max(1, int(width_raw))
        except ValueError:
            width = 8
        clock = _ask("Clock port name (empty = combinational)", "clk")
        reset = _ask("Reset port name + polarity (e.g. rst_n low)", "rst_n low")
        ports_raw = _ask(
            "Ports as 'name dir, ...' e.g. 'a in, b in, y out' (empty = auto)",
            "",
        )
        feats_raw = _ask("Key features / ops (comma separated)", "")
        tests_raw = _ask(
            "Test scenarios (comma separated)", "directed basics, reset check"
        )
        typer.echo(
            "PDK: [1] Let AI choose (recommended) [2] sky130 [3] nangate45 [4] asap7"
        )
        pdk_raw = _ask("PDK choice", pdk or "1")

        ports = _parse_ports(ports_raw, width) if ports_raw else []
        if clock and not any(p["name"] == clock for p in ports):
            ports.insert(0, {"name": clock, "direction": "input", "width": 1})
        rst_name = (reset.split() or ["rst_n"])[0]
        if reset and not any(p["name"] == rst_name for p in ports):
            ports.insert(1, {"name": rst_name, "direction": "input", "width": 1})
        if not ports:
            ports = [
                {"name": "a", "direction": "input", "width": width},
                {"name": "b", "direction": "input", "width": width},
                {"name": "y", "direction": "output", "width": width},
            ]

        # LLM autonomous follow-ups
        extra_answers: list[str] = []
        if use_llm:
            for q in _llm_followups(dut_name, description):
                try:
                    a = typer.prompt(f"AI: {q}", default="")
                except (EOFError, KeyboardInterrupt):
                    a = ""
                if a.strip():
                    extra_answers.append(f"{q} -> {a.strip()}")

        spec.update(
            {
                "dut_name": dut_name,
                "description": description
                + (
                    ("\nClarifications: " + "; ".join(extra_answers))
                    if extra_answers
                    else ""
                ),
                "category": category or "sequential",
                "data_width": width,
                "clock": clock,
                "reset": reset,
                "pdk_choice": validate_pdk_choice(pdk_raw),
                "ports": ports,
                "features": [f.strip() for f in feats_raw.split(",") if f.strip()],
                "test_plan": [t.strip() for t in tests_raw.split(",") if t.strip()],
            }
        )

    # PDK auto-select (LLM when available and allowed)
    feats = ", ".join(spec.get("features", []))
    if use_llm:
        selected, reason = llm_pick_pdk(
            spec["description"], feats, spec["pdk_choice"]
        )
    elif spec["pdk_choice"] == "auto":
        selected, reason = heuristic_pdk(spec["description"], feats)
    else:
        selected, reason = spec["pdk_choice"], f"user-selected {spec['pdk_choice']}"
    spec["pdk_selected"] = selected
    spec["pdk_reason"] = reason

    validate_spec(spec)
    sid = _session_id(spec["dut_name"])
    sdir = sessions_dir(project_dir) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    with open(sdir / "spec.json", "w") as f:
        json.dump(spec, f, indent=2)
    # mirror into runs/<sid> for pipeline convenience
    rdir = project_dir / "runs" / sid
    rdir.mkdir(parents=True, exist_ok=True)
    with open(rdir / "spec.json", "w") as f:
        json.dump(spec, f, indent=2)
    return sid, sdir / "spec.json"
