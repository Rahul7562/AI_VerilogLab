"""Small shell helpers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_capture(
    cmd: list[str], timeout: int = 15, extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run cmd, return (returncode, combined output truncated)."""
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
    except PermissionError:
        return 126, "permission denied"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 126, f"os error: {e}"


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
    except PermissionError as e:
        return 126, f"permission denied: {e}"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 126, f"os error: {e}"
