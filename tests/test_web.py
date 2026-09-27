"""Web dashboard tests (no EDA run, no LLM)."""
from fastapi.testclient import TestClient

from vlab.web import create_app


def _app(tmp_path):
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "spec.json").write_text('{"dut_name":"d"}')
    (tmp_path / "runs" / "r1" / "design.v").write_text("module d; endmodule")
    return TestClient(create_app(str(tmp_path)))


def test_index_and_status(tmp_path):
    c = _app(tmp_path)
    assert c.get("/").status_code == 200
    s = c.get("/api/status").json()
    assert "version" in s and "tools" in s and "llm_configured" in s


def test_reject_short_prompt(tmp_path):
    c = _app(tmp_path)
    assert c.post("/api/runs", json={"prompt": "x"}).status_code == 422


def test_file_serving_and_traversal(tmp_path):
    c = _app(tmp_path)
    r = c.get("/api/runs/r1/files/design.v")
    assert r.status_code == 200 and "module d" in r.text
    assert c.get("/api/runs/r1/files/../spec.json").status_code in (404, 400)
    assert c.get("/api/runs/nope").status_code == 404
    assert c.get("/api/runs/nope/events").status_code == 404


def test_clarify_ticket_flow(tmp_path, monkeypatch):
    import vlab.auto as auto_mod

    c = TestClient(create_app(str(tmp_path)))
    # offline run starts immediately, no questions
    r = c.post("/api/runs", json={"prompt": "8-bit adder", "use_llm": False})
    assert r.json()["run_id"]
    # LLM asks questions -> ticket; answering starts the run
    monkeypatch.setattr(auto_mod, "assess_prompt", lambda *a, **k: ["Sync or async reset?"])
    monkeypatch.setattr(auto_mod, "run_auto",
                        lambda *a, **k: {"session": "s", "winner": None, "final": None,
                                         "candidates": [], "pareto": []})
    r = c.post("/api/runs", json={"prompt": "counter", "use_llm": True})
    body = r.json()
    assert body["needs_input"] is True and body["questions"] == ["Sync or async reset?"]
    r2 = c.post("/api/runs/answer",
                json={"ticket": body["ticket"], "answers": ["sync"]})
    assert r2.json()["run_id"]
    assert c.post("/api/runs/answer",
                  json={"ticket": "deadbeef", "answers": []}).status_code == 404


def test_state_reports_issues_and_meta(tmp_path):
    import json as _json

    c = TestClient(create_app(str(tmp_path)))
    root = tmp_path / "runs" / "r2"
    cand = tmp_path / "runs" / "r2__i1c1"
    root.mkdir(parents=True)
    cand.mkdir(parents=True)
    (root / "spec.json").write_text("{}")
    (cand / "spec.json").write_text("{}")
    (cand / "sim.json").write_text(_json.dumps(
        {"status": "fail", "pass_count": 2, "fail_count": 1}))
    (cand / "gen.json").write_text(_json.dumps({"warnings": ["no VCD"]}))
    (tmp_path / "runs" / "r2" / "memory.jsonl").write_text(
        _json.dumps({"id": "r2__i1c1", "note": "boom"}) + "\n")
    st = c.get("/api/runs/r2").json()
    assert st["meta"]["status"] in ("running", "unknown")
    assert st["meta"]["current_step"] in ("define", None)
    (c0,) = st["candidates"]
    assert c0["status"] == "fail" and c0["note"] == "boom"
    assert c0["warnings"] == ["no VCD"]


def test_registry_survives_restart_and_listing(tmp_path):
    import json as _json

    from vlab import web as web_mod

    proj = tmp_path / "proj"
    (proj / "runs" / "sess1").mkdir(parents=True)
    (proj / "runs" / "sess1" / "spec.json").write_text("{}")
    (proj / "runs" / "sess1" / "a.v").write_text("x")
    web_mod._registry_write(proj, "abc123", "sess1")
    # fresh app instance, empty in-memory _RUNS
    web_mod._RUNS.clear()
    c = TestClient(create_app(str(proj)))
    st = c.get("/api/runs/abc123").json()
    assert st["run_id"] == "abc123"
    # lookup by session id directly also works
    assert c.get("/api/runs/sess1").status_code == 200
    files = c.get("/api/runs/abc123/files").json()["files"]
    assert "spec.json" in files and "a.v" in files
    assert c.get("/api/runs/abc123/files/../x").status_code == 404
    assert c.get("/api/runs/abc123/files/a.v").status_code == 200
