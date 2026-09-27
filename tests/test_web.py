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
