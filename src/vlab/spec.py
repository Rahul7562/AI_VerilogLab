"""Spec schema + validation + PDK auto-select (M1)."""
from __future__ import annotations

import re
from typing import Any

from .config import DEFAULT_PDK_CHOICE, SUPPORTED_PDKS

SPEC_REQUIRED_KEYS = (
    "dut_name",
    "description",
    "category",
    "data_width",
    "pdk_choice",
    "pdk_selected",
    "ports",
    "features",
    "test_plan",
)

_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_module_name(name: str) -> str:
    n = name.strip()
    if not _MODULE_RE.match(n):
        raise ValueError(
            f"Invalid Verilog module name {name!r}. Use [A-Za-z_][A-Za-z0-9_$]*"
        )
    return n


def validate_pdk_choice(choice: str) -> str:
    c = choice.strip().lower()
    if c in ("auto", "let ai choose", "ai", "recommended", ""):
        return "auto"
    # allow "1/2/3/4" menu indices
    mapping = {"1": "auto", "2": "sky130", "3": "nangate45", "4": "asap7"}
    if c in mapping:
        return mapping[c]
    if c in SUPPORTED_PDKS:
        return c
    raise ValueError(f"PDK must be one of auto|{'|'.join(SUPPORTED_PDKS)}")


def heuristic_pdk(description: str, features: str = "") -> tuple[str, str]:
    """Fallback PDK pick when LLM unavailable."""
    text = f"{description} {features}".lower()
    if any(k in text for k in ("ghz", "high speed", "high-speed", "timing-critical", "asap", "7nm")):
        return "asap7", "heuristic: high-speed keywords -> asap7"
    if any(k in text for k in ("balanced", "45nm", "mid-speed", "mid speed")):
        return "nangate45", "heuristic: balanced keywords -> nangate45"
    if any(k in text for k in ("learn", "teach", "demo", "simple")):
        return "sky130", "heuristic: learning/demo keywords -> sky130"
    # default balanced
    return "nangate45", "heuristic: default balanced -> nangate45"


def llm_pick_pdk(description: str, features: str, pdk_choice: str) -> tuple[str, str]:
    """Use LLM to pick PDK when choice is auto. Falls back to heuristic."""
    if pdk_choice != "auto":
        return pdk_choice, f"user-selected {pdk_choice}"
    try:
        from .llm import chat as llm_chat

        prompt = (
            "Pick the best PDK for this RTL design. Reply with exactly one line: "
            "<sky130|nangate45|asap7> : <short reason>.\n"
            f"Design: {description}\nFeatures: {features}\n"
            "Guidance: high-speed/timing-critical->asap7, "
            "balanced/mid-speed->nangate45, learning/simple/area-tolerant->sky130."
        )
        out = llm_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.0,
        )
        low = out.lower()
        for pdk in SUPPORTED_PDKS:
            if pdk in low:
                reason = out.strip().splitlines()[0][:200]
                return pdk, f"llm: {reason}"
    except Exception:
        pass
    return heuristic_pdk(description, features)


def default_spec(dut_name: str = "my_dut") -> dict[str, Any]:
    return {
        "dut_name": validate_module_name(dut_name),
        "description": "",
        "category": "sequential",
        "data_width": 8,
        "clock": "clk",
        "reset": "rst_n",
        "reset_active": "low",
        "pdk_choice": DEFAULT_PDK_CHOICE,
        "pdk_selected": None,
        "pdk_reason": None,
        "ports": [],
        "features": [],
        "test_plan": [],
        "target_freq_mhz": 100,
    }


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize. Raises ValueError on problems."""
    if not isinstance(spec, dict):
        raise ValueError("spec must be a dict")
    for k in ("dut_name", "description", "ports"):
        if k not in spec:
            raise ValueError(f"spec missing required key: {k}")
    spec["dut_name"] = validate_module_name(str(spec["dut_name"]))
    if not str(spec.get("description", "")).strip():
        raise ValueError("spec.description must be non-empty")
    spec["pdk_choice"] = validate_pdk_choice(str(spec.get("pdk_choice", "auto")))
    if not isinstance(spec.get("ports"), list) or not spec["ports"]:
        raise ValueError("spec.ports must be a non-empty list")
    for p in spec["ports"]:
        if not isinstance(p, dict) or "name" not in p or "direction" not in p:
            raise ValueError(f"bad port entry: {p!r}")
        if p["direction"] not in ("input", "output", "inout"):
            raise ValueError(f"bad port direction: {p!r}")
    try:
        spec["data_width"] = int(spec.get("data_width", 8))
    except (TypeError, ValueError):
        raise ValueError("spec.data_width must be an int")
    return spec
