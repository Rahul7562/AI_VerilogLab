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


def test_stubs_exit_2():
    for cmd in ["new", "gen", "simulate", "synth", "ppa", "run", "report"]:
        r = runner.invoke(app, [cmd])
        assert r.exit_code == 2, cmd


def test_init_creates_project(tmp_path):
    r = runner.invoke(app, ["init", "--path", str(tmp_path), "--dut", "alu"])
    assert r.exit_code == 0, r.stdout
    assert (tmp_path / "vlab_project.yaml").exists()
    assert (tmp_path / "sessions").is_dir()
    assert (tmp_path / "runs").is_dir()
