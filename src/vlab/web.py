"""Local web dashboard: prompt in -> live progress/logs -> results.

Serves the static UI and a small JSON/SSE API over the existing pipeline.
Run with `vlab serve`. No auth (localhost only by default).
"""
from __future__ import annotations

import json
import os
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

# run_id -> {"queue": Queue, "done": bool, "created": ts, "last_event": ts,
#              "status": "running"|"done"|"error", "detail": str, "steps": {step: ts}}
_RUNS: dict[str, dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()
# clarify tickets: ticket -> {"req": AutoRequest, "questions": list}
_TICKETS: dict[str, dict[str, Any]] = {}
# serialises the on-disk run registry read-modify-write
_REGISTRY_LOCK = threading.Lock()


def ui_dir() -> Path:
    return Path(__file__).resolve().parent / "webui"


class AutoRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    dut: str | None = None
    width: int | None = Field(default=None, ge=1, le=1024)
    candidates: int | None = Field(default=None, ge=1, le=8)
    iterations: int = Field(default=1, ge=1, le=5)
    fix_rounds: int = Field(default=2, ge=0, le=5)
    use_llm: bool = True
    freq_mhz: float = Field(default=100.0, gt=0)


class ClarifyAnswer(BaseModel):
    ticket: str
    answers: list[str] = Field(default_factory=list, max_length=8)


def _workspace(project: str | None) -> Path:
    from .config import PROJECT_FILE

    pdir = config.resolve_project_dir(project)
    if not (pdir / PROJECT_FILE).exists():
        from .cli import init_cmd  # noqa: PLC0415 - reuse scaffolding

        init_cmd(path=str(pdir), dut="dut")
    return pdir


def _register_run(run_id: str, stop: threading.Event | None = None) -> None:
    now = time.time()
    with _RUNS_LOCK:
        # bound memory: drop the oldest finished runs, keep active ones
        if len(_RUNS) > 200:
            done = sorted(
                (k for k, v in _RUNS.items() if v.get("done")),
                key=lambda k: _RUNS[k].get("created", 0.0),
            )
            for k in done:
                if len(_RUNS) <= 200:
                    break
                _RUNS.pop(k, None)
        _RUNS[run_id] = {
            "queue": queue.Queue(maxsize=2000), "done": False,
            "created": now, "last_event": now, "status": "running",
            "detail": "", "steps": {}, "finished": False, "run_dir": None,
            "stop": stop or threading.Event(),
        }


def _emit(run_id: str, event: dict[str, Any]) -> None:
    event = {"at": time.time(), **event}
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
        if run is None or run["done"]:
            return
        run["last_event"] = event["at"]
        if event["type"] == "step":
            run["steps"][event["step"]] = event["at"]
        elif event["type"] == "error":
            run["status"] = "error"
            run["detail"] = str(event.get("message", ""))[:300]
        elif event["type"] == "done":
            run["status"] = "done"
        try:
            run["queue"].put_nowait(event)
        except queue.Full:
            # Drop the OLDEST event and retry, so a slow reader loses history
            # rather than the newest state. Without the retry, run["steps"]
            # is already mutated above and the polling endpoint reports
            # progress the SSE stream never delivered.
            try:
                run["queue"].get_nowait()
                run["queue"].put_nowait(event)
            except (queue.Empty, queue.Full):
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
    notes: dict[str, str] = {}
    mem = run_dir / "memory.jsonl"
    if mem.exists():
        for line in mem.read_text().splitlines()[-50:]:
            try:
                e = json.loads(line)
                if e.get("note"):
                    notes[e.get("id", "")] = str(e["note"])[:300]
            except (json.JSONDecodeError, ValueError):
                continue
    for cdir in cand_dirs:
        c: dict[str, Any] = {"id": cdir.name}
        gen_p = cdir / "gen.json"
        if gen_p.exists():
            try:
                c["warnings"] = json.loads(gen_p.read_text()).get("warnings", [])
            except (json.JSONDecodeError, ValueError):
                pass
        if cdir.name in notes:
            c["note"] = notes[cdir.name]
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
                off = offsets.get(key, 0)
                try:
                    size = p.stat().st_size
                    if size < off:  # truncated/rotated
                        off = 0
                    if size <= off:
                        continue
                    # read only the new tail; re-reading whole files every
                    # 500 ms is O(n) per log per tick
                    with p.open("rb") as fh:
                        fh.seek(off)
                        chunk = fh.read().decode("utf-8", "replace")
                    offsets[key] = off + len(chunk.encode("utf-8", "replace"))
                except OSError:
                    continue
                if chunk:
                    _emit(run_id, {
                        "type": "log", "source": key,
                        "lines": chunk.splitlines()[-200:],
                    })
        if (run_dir / "auto.json").exists():
            idle_rounds += 1
            if idle_rounds >= 3:
                # the worker sets _RUNS[run]["finished"] when run_auto
                # returns; waiting for that (bounded) means the final
                # snapshot is complete instead of racing the writer
                with _RUNS_LOCK:
                    worker_done = _RUNS.get(run_id, {}).get("finished", False)
                if worker_done or idle_rounds >= 20:
                    _emit(run_id, {"type": "done", "state": snap})
                    with _RUNS_LOCK:
                        if run_id in _RUNS:
                            _RUNS[run_id]["done"] = True
                    return
        else:
            # the worker died before writing auto.json (error path): there is
            # nothing left to watch, so do not spin on this run forever
            with _RUNS_LOCK:
                worker_gone = bool(_RUNS.get(run_id, {}).get("finished")) and \
                    bool(_RUNS.get(run_id, {}).get("done"))
            if worker_gone:
                return
        time.sleep(0.5)


def _run_auto_bg(
    run_id: str,
    pdir: Path,
    req: AutoRequest,
    stop: threading.Event | None = None,
) -> None:
    from .auto import run_auto

    run_dir: Path | None = None
    failed: str | None = None

    def _publish(d: Path) -> None:
        # the monitor can attach as soon as the session dir exists, instead
        # of scanning runs/ and possibly grabbing a concurrent run's dir
        with _RUNS_LOCK:
            if run_id in _RUNS:
                _RUNS[run_id]["run_dir"] = str(d)
        _registry_write(pdir, run_id, d.name)

    try:
        s = run_auto(
            pdir, req.prompt, dut=req.dut, width=req.width,
            candidates=req.candidates, iterations=req.iterations,
            max_fix_rounds=req.fix_rounds, use_llm=req.use_llm,
            freq_mhz=req.freq_mhz, on_session=_publish,
        )
        run_dir = pdir / "runs" / s["session"]
        _registry_write(pdir, run_id, s["session"])
    except Exception as e:  # noqa: BLE001 - surface to UI
        failed = f"{type(e).__name__}: {e}"[:500]
        # resolve the monitor before reporting, so a monitor waiting for a
        # session dir that will never appear stops waiting
        with _RUNS_LOCK:
            if run_id in _RUNS:
                _RUNS[run_id]["finished"] = True
        _emit(run_id, {"type": "error", "message": failed})
    finally:
        with _RUNS_LOCK:
            if run_id in _RUNS:
                _RUNS[run_id]["finished"] = True
        if run_dir is not None and failed is None:
            # let the monitor flush the last log/step events first
            time.sleep(1.5)
            _emit(run_id, {"type": "done", "state": _snapshot(run_dir)})
        # signal the monitor to stop polling: it is a daemon thread, but on a
        # long-lived server it would otherwise spin for its full timeout
        if stop is not None:
            stop.set()
        with _RUNS_LOCK:
            if run_id in _RUNS:
                _RUNS[run_id]["done"] = True


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

        from .auto import assess_prompt

        # clarification gate: LLM asks for missing requirements first
        if req.use_llm:
            questions = assess_prompt(req.prompt.strip(), True)
            if questions:
                ticket = uuid.uuid4().hex[:12]
                with _RUNS_LOCK:
                    _TICKETS[ticket] = {"req": req, "questions": questions}
                    while len(_TICKETS) > 100:  # bound unanswered tickets
                        _TICKETS.pop(next(iter(_TICKETS)))
                return {"needs_input": True, "ticket": ticket, "questions": questions}
        return _launch(req)

    @app.post("/api/runs/answer")
    def answer_run(ans: ClarifyAnswer):
        with _RUNS_LOCK:
            ticket = _TICKETS.pop(ans.ticket, None)
        if ticket is None:
            raise HTTPException(404, "unknown or expired ticket")
        req: AutoRequest = ticket["req"]
        qa = [f"{q} -> {a.strip()}" for q, a in
              zip(ticket["questions"], ans.answers) if a.strip()]
        prompt = req.prompt.strip()
        if qa:
            prompt += "\nClarifications: " + "; ".join(qa)
        # model_copy skips validation, so the concatenation can exceed the
        # declared limit; clamp so downstream prompts stay bounded
        req = req.model_copy(update={"prompt": prompt[:2000]})
        return _launch(req)

    def _launch(req: AutoRequest) -> dict[str, str]:
        import uuid

        run_id = uuid.uuid4().hex[:12]
        stop = threading.Event()
        _register_run(run_id, stop)
        # monitor starts immediately; worker fills artifacts as it goes
        threading.Thread(
            target=_monitor_wait_sid, args=(run_id, pdir, stop), daemon=True
        ).start()
        threading.Thread(
            target=_run_auto_bg, args=(run_id, pdir, req, stop), daemon=True
        ).start()
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str):
        run_dir = _find_run_dir(pdir, run_id)
        if run_dir is None:
            # run may not have produced a session dir yet
            with _RUNS_LOCK:
                run = _RUNS.get(run_id)
            if run is None:
                raise HTTPException(404, "unknown run")
            return {
                "run_id": run_id, "pending": True,
                "meta": _run_meta(run),
            }
        snap = _snapshot(run_dir)
        snap["run_id"] = run_id
        with _RUNS_LOCK:
            run = _RUNS.get(run_id)
        snap["meta"] = _run_meta(run)
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
                    ev = q.get(timeout=15)
                except queue.Empty:
                    with _RUNS_LOCK:
                        r = _RUNS.get(run_id, {})
                        done = r.get("done", True)
                    # heartbeat: keeps client clock + stall detector alive
                    yield f"data: {json.dumps({'type': 'ping', 'at': time.time()})}\n\n"
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

    @app.get("/api/runs/{run_id}/files")
    def run_files(run_id: str):
        run_dir = _find_run_dir(pdir, run_id)
        if run_dir is None:
            raise HTTPException(404, "unknown run")
        out: list[str] = []
        roots = [("", run_dir)] + [
            (c.name, c) for c in _sibling_cands(run_dir)
        ]
        for prefix, root in roots:
            for p in sorted(root.rglob("*")):
                if p.is_file() and len(out) < 300:
                    rel = str(p.relative_to(root))
                    out.append(f"{prefix}/{rel}" if prefix else rel)
        return {"files": out}

    @app.get("/api/runs/{run_id}/files/{fname:path}")
    def run_file(run_id: str, fname: str):
        run_dir = _find_run_dir(pdir, run_id)
        if run_dir is None:
            raise HTTPException(404, "unknown run")
        if ".." in fname.replace("\\", "/").split("/"):
            raise HTTPException(404, "no such file")
        parts = fname.split("/", 1)
        if len(parts) == 2 and parts[0] in {c.name for c in _sibling_cands(run_dir)}:
            base = run_dir.parent / parts[0]
            target = (base / parts[1]).resolve()
        else:
            target = (run_dir / fname).resolve()
        # Path-prefix check, not str.startswith: "/p/runs_evil" would pass
        # a string prefix test against "/p/runs".
        runs_root = (pdir / "runs").resolve()
        if runs_root != target and runs_root not in target.parents:
            raise HTTPException(404, "no such file")
        if not target.is_file():
            raise HTTPException(404, "no such file")
        media = {
            ".v": "text/plain", ".sv": "text/plain", ".json": "application/json",
            ".log": "text/plain", ".md": "text/markdown", ".vcd": "text/plain",
            ".tcl": "text/plain", ".sdc": "text/plain", ".ys": "text/plain",
            ".rpt": "text/plain",
        }.get(target.suffix, "application/octet-stream")
        return FileResponse(target, media_type=media)

    return app


def _run_meta(run: dict[str, Any] | None) -> dict[str, Any]:
    if run is None:
        return {"status": "unknown", "detail": "", "current_step": None, "steps": {}}
    steps = run.get("steps", {})
    current = next((s for s in STEPS if s not in steps), "done")
    return {
        "status": run.get("status", "running"),
        "detail": run.get("detail", ""),
        "created": run.get("created"),
        "last_event": run.get("last_event"),
        "now": time.time(),
        "current_step": current,
        "steps": steps,
    }


def _registry_path(pdir: Path) -> Path:
    return pdir / "runs" / ".registry.json"


def _registry_read(pdir: Path) -> dict[str, str]:
    try:
        return json.loads(_registry_path(pdir).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _registry_write(pdir: Path, run_id: str, sid: str) -> None:
    """Persist run_id -> session_id.

    Three threads call this per run (worker publish, worker finish, monitor),
    so the read-modify-write must be serialised and the replace atomic:
    otherwise concurrent writers lose entries outright, and a reader can see
    a half-written file (write_text truncates before writing).
    """
    with _REGISTRY_LOCK:
        try:
            reg = _registry_read(pdir)
            reg[run_id] = sid
            # also allow lookup by session id directly
            reg.setdefault(sid, sid)
            dst = _registry_path(pdir)
            tmp = dst.with_name(f"{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(reg))
            os.replace(tmp, dst)  # atomic on POSIX and Windows
        except OSError:
            pass


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
    sid = _registry_read(pdir).get(run_id)
    if sid:
        cand = pdir / "runs" / sid
        if (cand / "spec.json").exists():
            return cand
    return None


def _monitor_wait_sid(run_id: str, pdir: Path, stop: threading.Event) -> None:
    """Wait for the worker's session dir to appear, then monitor it.

    The worker registers the run_dir itself as soon as run_auto returns, so
    prefer that; fall back to scanning for a new session dir only while the
    worker is still running. Scanning alone is wrong when two runs are in
    flight — it can attach this run's UI to the other run's directory.
    """
    run_dir: Path | None = None
    for _ in range(600):  # up to ~5 min for spec expand on slow models
        if stop.is_set():
            return
        with _RUNS_LOCK:
            mapped = _RUNS.get(run_id, {}).get("run_dir")
            worker_done = _RUNS.get(run_id, {}).get("finished", False)
        if mapped:
            p = Path(mapped)
            if (p / "spec.json").exists():
                run_dir = p
                break
        if worker_done:
            break  # worker finished without a run dir (error path)
        time.sleep(0.5)
    if run_dir is None:
        sid = _registry_read(pdir).get(run_id)
        if sid:
            p = pdir / "runs" / sid
            if (p / "spec.json").exists():
                run_dir = p
    if run_dir is None:
        with _RUNS_LOCK:
            already_done = _RUNS.get(run_id, {}).get("finished", False)
        if not already_done:
            # Report it, but do NOT set done: the worker is still running and
            # owns that flag. Marking it done here makes every later _emit a
            # no-op, so run_auto would finish invisibly.
            _emit(run_id, {
                "type": "error",
                "message": "no session dir yet; run still in progress "
                           "(progress will appear once DEFINE completes)",
            })
        return
    with _RUNS_LOCK:
        if run_id in _RUNS:
            _RUNS[run_id]["run_dir"] = str(run_dir)
    _registry_write(pdir, run_id, run_dir.name)
    _monitor(run_id, run_dir, stop)
