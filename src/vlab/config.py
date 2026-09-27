"""Project config, env handling, paths, PDK registry, tool resolution."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # loads .env if present, env vars take precedence

DEFAULT_MODEL = os.getenv(
    "OPENROUTER_MODEL", "stealth/space-bunny-alpha"
)
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

PROJECT_FILE = "vlab_project.yaml"

# PDKs we bundle metadata for. AI auto-selects unless user overrides.
SUPPORTED_PDKS = ("sky130", "nangate45", "asap7")
DEFAULT_PDK_CHOICE = "auto"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pdks_registry_path() -> Path:
    return repo_root() / "pdks" / "registry.yaml"


def generic_lib_path() -> Path:
    return repo_root() / "pdks" / "generic.lib"


# --- project / session paths ---

def resolve_project_dir(project: str | None) -> Path:
    """Find project dir: explicit --project, else cwd, else walk up to vlab_project.yaml."""
    if project:
        return Path(project).resolve()
    cwd = Path.cwd()
    if (cwd / PROJECT_FILE).exists():
        return cwd
    for parent in cwd.parents:
        if (parent / PROJECT_FILE).exists():
            return parent
    return cwd


def sessions_dir(project_dir: Path) -> Path:
    d = project_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def runs_dir(project_dir: Path) -> Path:
    d = project_dir / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_dir(project_dir: Path, session_id: str) -> Path:
    d = runs_dir(project_dir) / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_spec(project_dir: Path, session_id: str | None) -> tuple[str, Path]:
    """Locate spec.json for session. Returns (session_id, spec_path)."""
    sdir = sessions_dir(project_dir)
    if session_id:
        p = sdir / session_id / "spec.json"
        if not p.exists():
            # also allow runs/<id>/spec.json
            p2 = project_dir / "runs" / session_id / "spec.json"
            if p2.exists():
                return session_id, p2
            raise FileNotFoundError(f"spec not found for session {session_id!r}")
        return session_id, p
    # latest session by mtime (skip auto candidate dirs: <sid>__i<c>c<d>)
    candidates = sorted(
        [d for d in sdir.iterdir()
         if d.is_dir() and (d / "spec.json").exists() and "__i" not in d.name],
        key=lambda d: (d / "spec.json").stat().st_mtime,
    )
    if not candidates:
        # fallback to runs/
        rdir = runs_dir(project_dir)
        candidates = sorted(
            [d for d in rdir.iterdir()
             if d.is_dir() and (d / "spec.json").exists() and "__i" not in d.name],
            key=lambda d: (d / "spec.json").stat().st_mtime,
        )
    if not candidates:
        raise FileNotFoundError("no sessions found. Run `vlab new` first.")
    latest = candidates[-1]
    return latest.name, latest / "spec.json"


# --- tool resolution (PATH + VLAB_* overrides + bundled user-space EDA) ---
# Persistent toolchain lives in ~/.local/edatools (usr/bin, usr/lib, ...).
# VLAB_EDA_ROOT overrides; /tmp/edatools/extract kept as legacy fallback.

_TOOL_ENV = {
    "iverilog": "VLAB_IVERILOG_BIN",
    "vvp": "VLAB_VVP_BIN",
    "verilator": "VLAB_VERILATOR_BIN",
    "yosys": "VLAB_YOSYS_BIN",
    "yosys-abc": "VLAB_ABC_BIN",
    "abc": "VLAB_ABC_BIN",
    "sta": "VLAB_STA_BIN",
}


def eda_roots() -> list[Path]:
    roots: list[Path] = []
    override = os.getenv("VLAB_EDA_ROOT", "").strip()
    if override:
        roots.append(Path(override))
    roots += [Path.home() / ".local" / "edatools", Path("/tmp/edatools/extract")]
    return [r for r in roots if (r / "usr" / "bin").is_dir()]


def _extra_bin_dirs() -> list[Path]:
    return [r / "usr" / "bin" for r in eda_roots()]


def resolve_tool(binary: str) -> str | None:
    """Resolve EDA binary via env override, PATH, then known user-space dirs."""
    env_key = _TOOL_ENV.get(binary, f"VLAB_{binary.upper()}_BIN")
    override = os.getenv(env_key, "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which(binary)
    if found:
        return found
    for d in _extra_bin_dirs():
        cand = d / binary
        if cand.exists():
            return str(cand)
    return None


def tool_env() -> dict[str, str]:
    """Env additions so bundled user-space EDA tools work (tcl, ld path)."""
    env: dict[str, str] = {}
    for root in eda_roots():
        ld = root / "usr" / "lib" / "x86_64-linux-gnu"
        tcl = root / "usr" / "share" / "tcltk" / "tcl8.6" / "init.tcl"
        share = root / "usr" / "share" / "yosys"
        if ld.is_dir():
            prev = env.get("LD_LIBRARY_PATH", os.environ.get("LD_LIBRARY_PATH", ""))
            env["LD_LIBRARY_PATH"] = f"{ld}:{prev}" if prev else str(ld)
        if tcl.exists() and "TCL_LIBRARY" not in os.environ and "TCL_LIBRARY" not in env:
            env["TCL_LIBRARY"] = str(tcl.parent)
        if share.is_dir() and "YOSYS_SHARE" not in os.environ:
            env["YOSYS_SHARE"] = str(share)
        if "LD_LIBRARY_PATH" in env and "TCL_LIBRARY" in env:
            break
    return env
