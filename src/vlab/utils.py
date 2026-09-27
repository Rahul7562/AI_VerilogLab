"""Small shell helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def which(tool: str) -> str | None:
    return shutil.which(tool)


def run_capture(
    cmd: list[str], timeout: int = 15, extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run cmd, return (returncode, combined output truncated)."""
    import os

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out[:2000]
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def run_logged(
    cmd: list[str],
    cwd: Path | str | None = None,
    timeout: int = 120,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run cmd capturing full output (for logs). Returns (rc, output)."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out
    except FileNotFoundError as e:
        return 127, f"not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
