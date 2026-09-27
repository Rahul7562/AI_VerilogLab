"""Local web dashboard: prompt in -> live progress/logs -> results.

Serves the static UI and a small JSON/SSE API over the existing pipeline.
Run with `vlab serve`. No auth (localhost only by default).
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config
from .tools import check_tools

STEPS = ["define", "generate", "verify", "synthesize", "ppa", "select", "done"]

# run_id -> {"queue": Queue, "done": bool}
_RUNS: dict[str, dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()


def ui_dir() -> Path:
    return Path(__file__).resolve().parent / "webui"


class AutoRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    dut: str | None = None
    width: int = Field(default=8, ge=1, le=1024)
    candidates: int = Field(default=1, ge=1, le=8)
    iterations: int = Field(default=1, ge=1, le=5)
    fix_rounds: int = Field(default=2, ge=0, le=5)
    use_llm: bool = True
    freq_mhz: float = Field(default=100.0, gt=0)


def _workspace(project: str | None) -> Path:
    from .config import PROJECT_FILE

    pdir = config.resolve_project_dir(project)
    if not (pdir / PROJECT_FILE).exists():
        from .cli import init_cmd  # noqa: PLC0415 - reuse scaffolding

        init_cmd(path=str(pdir), dut="dut")
    return pdir


def _emit(run_id: str, event: dict[str, Any]) -> None:
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run and not run["done"]:
        try:
            run["queue"].put_nowait(event)
        except queue.Full:
            pass


def _sibling_cands(run_dir: Path) -> list[Path]:
    """Candidate session dirs live beside the session root: <sid>__i<c>c<d>."""
    prefix = run_dir.name + "__i"
    try:
        return sorted(
            [d for d in run_dir.parent.iterdir()
             if d.is_dir() and d.name.startswith(prefix)
             and (d / "spec.json").exists()]
        )
    except OSError:
        return []


def _snapshot(run_dir: Path) -> dict[str, Any]:
    """Cheap live state derived from artifacts on disk."""
    state: dict[str, Any] = {"steps": {}, "candidates": [], "final": None}
    if (run_dir / "spec.json").exists():
        state["steps"]["define"] = "done"
    cand_dirs = _sibling_cands(run_dir)
    for cdir in cand_dirs:
        c: dict[str, Any] = {"id": cdir.name}
        if (cdir / "design.v").exists():
            state["steps"]["generate"] = "done"
            c["rtl"] = True
        sim_p = cdir / "sim.json"
        if sim_p.exists():
            try:
                sim = json.loads(sim_p.read_text())
                c.update({k: sim.get(k) for k in ("status", "pass_count", "fail_count")})
                state["steps"]["verify"] = "done"
            except (json.JSONDecodeError, ValueError):
                pass
        if (cdir / "synth.json").exists():
            c["synth"] = True
            state["steps"]["synthesize"] = "done"
        if (cdir / "ppa.json").exists():
            try:
                ppa = json.loads((cdir / "ppa.json").read_text())
                c["cells"] = (ppa.get("area") or {}).get("cell_count")
                c["crit_ns"] = (ppa.get("timing") or {}).get("crit_ns")
                c["power_w"] = (ppa.get("power") or {}).get("power_w")
                state["steps"]["ppa"] = "done"
            except (json.JSONDecodeError, ValueError):
                pass
        state["candidates"].append(c)
    if (run_dir / "auto.json").exists():
        state["steps"]["select"] = "done"
        state["steps"]["done"] = "done"
        try:
            auto = json.loads((run_dir / "auto.json").read_text())
            state["winner"] = auto.get("winner")
        except (json.JSONDecodeError, ValueError):
            pass
    if (run_dir / "final" / "PPA.md").exists():
        state["final"] = True
    return state


def _monitor(run_id: str, run_dir: Path, stop: threading.Event) -> None:
    """Poll artifacts; emit step + log-tail events until auto.json lands."""
    seen_steps: set[str] = set()
    offsets: dict[str, int] = {}
    for log in ("sim.log", "synth.log", "sta.log"):
        key = f"root/{log}"
        offsets.setdefault(key, 0)
    idle_rounds = 0
    while not stop.is_set():
        snap = _snapshot(run_dir)
        for step, val in snap["steps"].items():
            if val == "done" and step not in seen_steps:
                seen_steps.add(step)
                _emit(run_id, {"type": "step", "step": step})
        # tail logs in session root + candidate dirs
        for cdir in [run_dir, *_sibling_cands(run_dir)]:
            for log in ("sim.log", "synth.log", "sta.log"):
                p = cdir / log
                if not p.exists():
                    continue
                key = f"{cdir.name}/{log}"
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                off = offsets.get(key, 0)
                if len(data) < off:  # truncated/rotated
                    off = 0
                if len(data) > off:
                    chunk = data[off:].decode("utf-8", "replace")
                    offsets[key] = len(data)
                    _emit(run_id, {
                        "type": "log", "source": key,
                        "lines": chunk.splitlines()[-200:],
                    })
        if (run_dir / "auto.json").exists():
            idle_rounds += 1
            if idle_rounds >= 3:
                _emit(run_id, {"type": "done", "state": snap})
                with _RUNS_LOCK:
                    if run_id in _RUNS:
                        _RUNS[run_id]["done"] = True
                return
        time.sleep(0.5)


def _run_auto_bg(run_id: str, pdir: Path, req: AutoRequest) -> None:
    from .auto import run_auto

    run_dir: Path | None = None
    try:
        s = run_auto(
            pdir, req.prompt, dut=req.dut, width=req.width,
            candidates=req.candidates, iterations=req.iterations,
            max_fix_rounds=req.fix_rounds, use_llm=req.use_llm,
            freq_mhz=req.freq_mhz,
        )
        run_dir = pdir / "runs" / s["session"]
    except Exception as e:  # noqa: BLE001 - surface to UI
        _emit(run_id, {"type": "error", "message": f"{type(e).__name__}: {e}"[:500]})
    finally:
        # monitor flushes final state; ensure stream terminates
        time.sleep(2.5)
        with _RUNS_LOCK:
            if run_id in _RUNS:
                _RUNS[run_id]["done"] = True
        if run_dir is not None:
            _emit(run_id, {"type": "done", "state": _snapshot(run_dir)})


def create_app(project: str | None = None) -> FastAPI:
    app = FastAPI(title="AI VerilogLab")
    pdir = _workspace(project)
    app.state.project_dir = pdir
    app.mount("/static", StaticFiles(directory=ui_dir() / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ui_dir() / "static" / "index.html")

    @app.get("/api/status")
    def status():
        import os

        tools = [
            {"name": t.name, "binary": t.binary, "found": t.found}
            for t in check_tools()
        ]
        return {
            "version": __version__,
            "tools": tools,
            "llm_configured": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
            "project": str(pdir),
        }

    @app.post("/api/runs")
    def start_run(req: AutoRequest):
        import uuid

        run_id = uuid.uuid4().hex[:12]
        with _RUNS_LOCK:
            _RUNS[run_id] = {"queue": queue.Queue(maxsize=2000), "done": False}
        stop = threading.Event()
        # monitor starts immediately; worker fills artifacts as it goes
        threading.Thread(
            target=_monitor_wait_sid, args=(run_id, pdir, stop), daemon=True
        ).start()
        threading.Thread(
            target=_run_auto_bg, args=(run_id, pdir, req), daemon=True
        ).start()
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str):
        run_dir = _find_run_dir(pdir, run_id)
        if run_dir is None:
            raise HTTPException(404, "unknown run")
        snap = _snapshot(run_dir)
        snap["run_id"] = run_id
        return snap

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str):
        with _RUNS_LOCK:
            run = _RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "unknown run (server restarted?)")

        def gen():
            q: queue.Queue = run["queue"]
            yield ": connected\n\n"
            while True:
                try:
                    ev = q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"
                    with _RUNS_LOCK:
                        done = _RUNS.get(run_id, {}).get("done", True)
                    if done and q.empty():
                        break
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") in ("done", "error"):
                    with _RUNS_LOCK:
                        done = _RUNS.get(run_id, {}).get("done", False)
                    if done:
                        # drain remaining quickly then close
                        time.sleep(0.2)
                        while True:
                            try:
                                ev2 = q.get_nowait()
                            except queue.Empty:
                                break
                            yield f"data: {json.dumps(ev2)}\n\n"
                        break

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/files/{fname:path}")
    def run_file(run_id: str, fname: str):
        run_dir = _find_run_dir(pdir, run_id)
        if run_dir is None:
            raise HTTPException(404, "unknown run")
        target = (run_dir / fname).resolve()
        if not str(target).startswith(str(run_dir.resolve())) or not target.is_file():
            raise HTTPException(404, "no such file")
        media = {
            ".v": "text/plain", ".sv": "text/plain", ".json": "application/json",
            ".log": "text/plain", ".md": "text/markdown", ".vcd": "text/plain",
            ".tcl": "text/plain", ".sdc": "text/plain", ".ys": "text/plain",
            ".rpt": "text/plain",
        }.get(target.suffix, "application/octet-stream")
        return FileResponse(target, media_type=media)

    return app


def _find_run_dir(pdir: Path, run_id: str) -> Path | None:
    with _RUNS_LOCK:
        mapped = _RUNS.get(run_id, {}).get("run_dir")
    if mapped:
        cand = Path(mapped)
        if (cand / "spec.json").exists():
            return cand
    for base in (pdir / "runs",):
        cand = base / run_id
        if (cand / "spec.json").exists():
            return cand
    return None


def _monitor_wait_sid(run_id: str, pdir: Path, stop: threading.Event) -> None:
    """Wait for the worker's session dir to appear, then monitor it."""
    seen: set[str] = {d.name for d in (pdir / "runs").iterdir() if d.is_dir()}
    run_dir: Path | None = None
    for _ in range(600):  # up to ~5 min for spec expand on slow models
        if stop.is_set():
            return
        current = {d.name for d in (pdir / "runs").iterdir() if d.is_dir()}
        new = [n for n in current - seen
               if "__i" not in n and (pdir / "runs" / n / "spec.json").exists()]
        if new:
            run_dir = pdir / "runs" / sorted(new)[-1]
            break
        time.sleep(0.5)
    if run_dir is None:
        _emit(run_id, {"type": "error", "message": "session dir never appeared"})
        with _RUNS_LOCK:
            if run_id in _RUNS:
                _RUNS[run_id]["done"] = True
        return
    with _RUNS_LOCK:
        if run_id in _RUNS:
            _RUNS[run_id]["run_dir"] = str(run_dir)
    _monitor(run_id, run_dir, stop)
