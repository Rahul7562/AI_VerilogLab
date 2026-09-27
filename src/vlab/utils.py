"""Small shell helpers."""
from __future__ import annotations

import shutil
import subprocess


def which(tool: str) -> str | None:
    return shutil.which(tool)


def run_capture(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    """Run cmd, return (returncode, combined output truncated)."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out[:2000]
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
