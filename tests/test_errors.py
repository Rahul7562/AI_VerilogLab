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
