"""CLI error paths must fail cleanly (message + nonzero exit, no traceback)."""
from typer.testing import CliRunner

from vlab.cli import app

runner = CliRunner()


def _proj(tmp_path):
    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    r = runner.invoke(app, ["new", "--project", str(tmp_path),
                             "--non-interactive", "--dut", "d",
                             "--desc", "d", "--no-llm"])
    assert r.exit_code == 0, r.stdout


def test_simulate_without_gen_fails_clean(tmp_path):
    _proj(tmp_path)
    r = runner.invoke(app, ["simulate", "--project", str(tmp_path)])
    assert r.exit_code == 1
    assert "Traceback" not in r.stdout


def test_ppa_without_synth_fails_clean(tmp_path):
    _proj(tmp_path)
    r = runner.invoke(app, ["ppa", "--project", str(tmp_path)])
    assert r.exit_code == 1
    assert "Traceback" not in r.stdout


def test_gen_twice_needs_force(tmp_path):
    _proj(tmp_path)
    assert runner.invoke(app, ["gen", "--project", str(tmp_path),
                               "--no-llm"]).exit_code == 0
    r = runner.invoke(app, ["gen", "--project", str(tmp_path), "--no-llm"])
    assert r.exit_code != 0
    assert "Traceback" not in r.stdout
    assert "--force" in r.stdout


def test_new_bad_pdk_fails_clean(tmp_path):
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    r = runner.invoke(app, ["new", "--project", str(tmp_path),
                             "--non-interactive", "--dut", "d",
                             "--desc", "d", "--pdk", "bogus", "--no-llm"])
    assert r.exit_code != 0
    assert "Traceback" not in r.stdout


def test_no_sessions_fails_clean_for_every_pipeline_cmd(tmp_path):
    """A fresh project must not dump a traceback from find_spec."""
    assert runner.invoke(app, ["init", "--path", str(tmp_path)]).exit_code == 0
    for cmd in ("simulate", "synth", "ppa", "report", "fix", "run", "gen"):
        r = runner.invoke(app, [cmd, "--project", str(tmp_path)])
        assert r.exit_code == 1, cmd
        assert "Traceback" not in r.stdout, cmd
        assert "no sessions found" in r.stdout, cmd


def test_bad_session_id_fails_clean(tmp_path):
    _proj(tmp_path)
    for cmd in ("simulate", "synth", "ppa", "report"):
        r = runner.invoke(app, [cmd, "--project", str(tmp_path),
                                "--session", "does_not_exist"])
        assert r.exit_code == 1, cmd
        assert "Traceback" not in r.stdout, cmd


def test_simulate_reports_tool_error_cleanly(tmp_path, monkeypatch):
    """A missing EDA binary must be a message, not a stack trace."""
    _proj(tmp_path)
    assert runner.invoke(app, ["gen", "--project", str(tmp_path),
                               "--no-llm"]).exit_code == 0
    import vlab.sim as sim_mod

    def boom(*a, **k):
        raise RuntimeError("iverilog/vvp not found. Install iverilog.")

    monkeypatch.setattr(sim_mod, "run_iverilog", boom)
    r = runner.invoke(app, ["simulate", "--project", str(tmp_path)])
    assert r.exit_code == 1
    assert "Traceback" not in r.stdout
    assert "iverilog" in r.stdout
