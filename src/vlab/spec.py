"""Spec schema stub. Full Q&A engine lands in M1."""
from __future__ import annotations

from .config import DEFAULT_PDK_CHOICE, SUPPORTED_PDKS

# M1 will expand this: ports, clk/rst, protocol, test scenarios, PDK choice + reason.
SPEC_REQUIRED_KEYS = ("dut_name", "description", "pdk_choice", "ports")


def validate_pdk_choice(choice: str) -> str:
    c = choice.strip().lower()
    if c in ("auto", "let ai choose", "ai"):
        return "auto"
    if c in SUPPORTED_PDKS:
        return c
    raise ValueError(f"PDK must be one of auto|{'|'.join(SUPPORTED_PDKS)}")


def default_spec(dut_name: str = "my_dut") -> dict:
    return {
        "dut_name": dut_name,
        "description": "",
        "pdk_choice": DEFAULT_PDK_CHOICE,
        "pdk_selected": None,
        "pdk_reason": None,
        "ports": [],
    }
