"""Unit tests for spec/parsers/ppa/fix helpers (no EDA, no LLM)."""
import pytest

from vlab.ask import _parse_ports
from vlab.fix import _extract_tb
from vlab.ppa import _parse_sta_checks, _parse_sta_power, _vcd_activity, _vcd_toggle_estimate
from vlab.rtl_gen import builtin_template
from vlab.spec import (
    heuristic_pdk,
    validate_module_name,
    validate_pdk_choice,
    validate_spec,
)


def test_validate_module_name():
    assert validate_module_name("alu8") == "alu8"
    with pytest.raises(ValueError):
        validate_module_name("8bad")
    with pytest.raises(ValueError):
        validate_module_name("has space")


def test_validate_pdk_choice():
    assert validate_pdk_choice("auto") == "auto"
    assert validate_pdk_choice("1") == "auto"
    assert validate_pdk_choice(" sky130 ") == "sky130"
    with pytest.raises(ValueError):
        validate_pdk_choice("bogus")


def test_validate_spec_ok_and_bad():
    good = {
        "dut_name": "cnt8",
        "description": "counter",
        "pdk_choice": "auto",
        "ports": [{"name": "clk", "direction": "input", "width": 1}],
        "data_width": 8,
    }
    assert validate_spec(dict(good))["dut_name"] == "cnt8"
    bad = dict(good, ports=[])
    with pytest.raises(ValueError):
        validate_spec(bad)


def test_heuristic_pdk():
    assert heuristic_pdk("high speed GHz datacenter serdes")[0] == "asap7"
    assert heuristic_pdk("simple learning demo")[0] == "sky130"
    assert heuristic_pdk("plain block")[0] == "nangate45"


def test_parse_ports():
    ports = _parse_ports("a in, b[3:0] in, y out, clk in", 8)
    by_name = {p["name"]: p for p in ports}
    assert by_name["a"]["width"] == 8
    assert by_name["b"]["width"] == 4
    assert by_name["y"]["direction"] == "output"
    assert by_name["clk"]["width"] == 1


def test_builtin_template_dynamic_ports():
    spec = {
        "dut_name": "mydut",
        "data_width": 4,
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "rst", "direction": "input", "width": 1},
            {"name": "x", "direction": "input", "width": 4},
            {"name": "z", "direction": "output", "width": 4},
        ],
    }
    d, tb = builtin_template(spec)
    assert "module mydut" in d
    assert ".x(x)" in tb and ".z(z)" in tb
    assert "$dumpfile" in tb and "$finish" in tb


def test_parse_sta_checks_and_power():
    log = (
        "Startpoint: a\n"
        "  3.15   data arrival time\n"
        "  6.79   slack (MET)\n"
        "Group Internal Switching Leakage Total\n"
        "Total 0.00e+00 1.35e-06 6.70e-05 6.83e-05 100.0%\n"
    )
    t = _parse_sta_checks(log)
    assert t["wns_ns"] == 6.79
    assert t["crit_ns"] == 3.15
    p = _parse_sta_power(log)
    assert abs(p["power_w"] - 6.83e-05) < 1e-12


def test_vcd_helpers(tmp_path):
    vcd = tmp_path / "w.vcd"
    vcd.write_text(
        "$var wire 1 ! clk $end\n$var wire 8 a data $end\n"
        "#0\n0!\n#5\n1!\nb00000001 a\n#10\n0!\n"
    )
    act, toggles, signals = _vcd_activity(vcd)
    assert signals == 2 and toggles > 0 and 0.01 <= act <= 0.5
    est = _vcd_toggle_estimate(vcd)
    assert est["power_w"] is not None


def test_extract_tb_fence_and_raw():
    fenced = "```verilog\nmodule tb_foo; $finish; endmodule\n```"
    assert "tb_foo" in _extract_tb(fenced, "foo")
    raw = "some prose\nmodule tb_bar;\n$finish;\nendmodule\ntrailing"
    assert "tb_bar" in _extract_tb(raw, "bar")
    with pytest.raises(ValueError):
        _extract_tb("no verilog here", "baz")
