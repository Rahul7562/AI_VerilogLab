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
