from typer.testing import CliRunner
from vlab.cli import app

runner = CliRunner()


def test_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "VerilogLab" in r.stdout


def test_check_tools_runs():
    r = runner.invoke(app, ["check-tools"])
    assert r.exit_code == 0
    assert "EDA tool status" in r.stdout


def test_init_creates_project(tmp_path):
    r = runner.invoke(app, ["init", "--path", str(tmp_path), "--dut", "alu"])
    assert r.exit_code == 0, r.stdout
    assert (tmp_path / "vlab_project.yaml").exists()
    assert (tmp_path / "sessions").is_dir()
    assert (tmp_path / "runs").is_dir()


def test_new_gen_flow_offline(tmp_path):
    r = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    r = runner.invoke(
        app,
        ["new", "--project", str(tmp_path), "--non-interactive",
         "--dut", "alu8", "--desc", "8-bit adder demo", "--pdk", "auto",
         "--no-llm"],
    )
    assert r.exit_code == 0, r.stdout
    assert "Session:" in r.stdout
    r = runner.invoke(app, ["gen", "--project", str(tmp_path), "--no-llm"])
    assert r.exit_code == 0, r.stdout


def test_every_command_help_works():
    """A broken command signature is invisible until someone runs it."""
    for cmd in ("init", "new", "gen", "simulate", "synth", "ppa", "report",
                "fix", "run", "auto", "rel", "serve", "check-tools",
                "check-llm", "models"):
        r = runner.invoke(app, [cmd, "--help"])
        assert r.exit_code == 0, f"{cmd}: {r.stdout}"


def test_rel_reports_a_rate_not_just_a_run(tmp_path, monkeypatch):
    """`rel` exists to turn reliability into a number, so it must print one."""
    import vlab.auto as auto_mod

    calls = {"n": 0}

    def fake_run_auto(pdir, prompt, **kw):
        calls["n"] += 1
        # first two attempts win, third does not
        win = calls["n"] <= 2
        return {
            "session": f"s{calls['n']}",
            "dut": "d",
            "iterations": 1,
            "rounds_run": 1 if win else 3,
            "winner": "s__i1c1" if win else None,
            "final": None,
            "candidates": [{
                "id": "s__i1c1", "status": "pass" if win else "fail",
                "passes": 6, "repairs": 0,
                "note": "" if win else "exhausted 3 repair round(s); 4 failing",
            }],
        }

    monkeypatch.setattr(auto_mod, "run_auto", fake_run_auto)
    r = runner.invoke(app, ["rel", "an 8-bit counter", "--runs", "3",
                            "--project", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    assert "win rate" in r.stdout
    assert "2/3" in r.stdout
    assert "67%" in r.stdout
    # a failure must be explained, not just counted
    assert "exhausted" in r.stdout
    assert calls["n"] == 3


def test_rel_survives_one_attempt_raising(tmp_path, monkeypatch):
    """A single exploding attempt must not abort the measurement."""
    import vlab.auto as auto_mod

    calls = {"n": 0}

    def fake_run_auto(pdir, prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider exploded")
        return {
            "session": "s", "dut": "d", "iterations": 1, "rounds_run": 1,
            "winner": "s__i1c1", "final": None,
            "candidates": [{"id": "s__i1c1", "status": "pass", "passes": 3,
                            "repairs": 0, "note": ""}],
        }

    monkeypatch.setattr(auto_mod, "run_auto", fake_run_auto)
    r = runner.invoke(app, ["rel", "a counter", "--runs", "2",
                            "--project", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    assert "1/2" in r.stdout
    assert "provider exploded" in r.stdout


def test_models_lists_rotation_order():
    """Regression: --probe referenced get_api_key without importing it."""
    r = runner.invoke(app, ["models"])
    assert r.exit_code == 0, r.stdout
    assert "primary" in r.stdout
    assert "fallback" in r.stdout


def test_models_probe_requires_no_import_error():
    r = runner.invoke(app, ["models", "--probe"])
    # either probes successfully, or fails cleanly with a message
    assert r.exception is None or isinstance(r.exception, SystemExit), r.stdout
    assert "NameError" not in r.stdout
    assert "Traceback" not in r.stdout
