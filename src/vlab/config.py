"""Project config, env handling, paths, PDK registry, tool resolution."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # loads .env if present, env vars take precedence

DEFAULT_MODEL = os.getenv(
    "OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"
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
    # latest session by mtime
    candidates = sorted(
        [d for d in sdir.iterdir() if d.is_dir() and (d / "spec.json").exists()],
        key=lambda d: (d / "spec.json").stat().st_mtime,
    )
    if not candidates:
        # fallback to runs/
        rdir = runs_dir(project_dir)
        candidates = sorted(
            [d for d in rdir.iterdir() if d.is_dir() and (d / "spec.json").exists()],
            key=lambda d: (d / "spec.json").stat().st_mtime,
        )
    if not candidates:
        raise FileNotFoundError("no sessions found. Run `vlab new` first.")
    latest = candidates[-1]
    return latest.name, latest / "spec.json"


# --- tool resolution (PATH + VLAB_* overrides + user-space edatools) ---

_TOOL_ENV = {
    "iverilog": "VLAB_IVERILOG_BIN",
    "vvp": "VLAB_VVP_BIN",
    "verilator": "VLAB_VERILATOR_BIN",
    "yosys": "VLAB_YOSYS_BIN",
    "yosys-abc": "VLAB_ABC_BIN",
    "abc": "VLAB_ABC_BIN",
    "sta": "VLAB_STA_BIN",
}

_EXTRA_BIN_DIRS = (
    Path.home() / ".local" / "eda" / "bin",
    Path("/tmp/edatools/extract/usr/bin"),
)


def resolve_tool(binary: str) -> str | None:
    """Resolve EDA binary via env override, PATH, then known user-space dirs."""
    env_key = _TOOL_ENV.get(binary, f"VLAB_{binary.upper()}_BIN")
    override = os.getenv(env_key, "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which(binary)
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        cand = d / binary
        if cand.exists():
            return str(cand)
    return None


def tool_env() -> dict[str, str]:
    """Env additions so extracted user-space EDA tools work (tcl, ld path)."""
    env: dict[str, str] = {}
    ld = "/tmp/edatools/extract/usr/lib/x86_64-linux-gnu"
    tcl = "/tmp/edatools/extract/usr/share/tcltk/tcl8.6"
    if Path(ld).exists():
        prev = os.environ.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{ld}:{prev}" if prev else ld
    if Path(tcl, "init.tcl").exists() and "TCL_LIBRARY" not in os.environ:
        env["TCL_LIBRARY"] = tcl
    # yosys share dir for cell libs
    share = "/tmp/edatools/extract/usr/share/yosys"
    if Path(share).exists() and "YOSYS_SHARE" not in os.environ:
        env["YOSYS_SHARE"] = share
    return env
