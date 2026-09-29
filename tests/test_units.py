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


def test_pdk_data_files_resolve():
    """generic.lib must be found in a source checkout AND when installed.

    The wheel ships pdks/ inside the package; a source checkout has it at the
    repo root. Both have to resolve or synth/STA silently degrade.
    """
    from vlab.config import data_dir, generic_lib_path, pdks_registry_path

    lib = generic_lib_path()
    assert lib.exists(), f"liberty not found via {data_dir()}"
    assert pdks_registry_path().exists()
    assert lib.parent == data_dir()


def test_sdc_builds_explicit_port_lists(tmp_path):
    """Data-input/output sets must be built explicitly, not via -no_clocks.

    On OpenSTA 2.0.x `[all_inputs -no_clocks]` and `remove_from_collection`
    both evaluate to an EMPTY set, which drops every input delay and makes
    all input->register timing invisible.
    """
    from vlab.ppa import _sdc_for

    p = _sdc_for(tmp_path, "clk", 10.0)
    sdc = p.read_text()
    assert "create_clock -name clk -period 10 [get_ports {clk}]" in sdc
    # the broken collection-filter idioms must not be *used* (they may be
    # named in the explanatory comment)
    code = "\n".join(
        ln for ln in sdc.splitlines() if not ln.strip().startswith("#")
    )
    assert "-no_clocks" not in code
    assert "remove_from_collection" not in code
    # explicit port walk, and the clock is excluded by name
    assert "get_property $vlab_p direction" in sdc
    assert 'eq {clk}' in sdc
    assert "set_input_delay 2.0000 -clock clk $vlab_data_inputs" in sdc
    assert "set_output_delay 2.0000 -clock clk $vlab_data_outputs" in sdc


def test_parse_sta_checks_reports_path_count():
    log = "\n".join(
        f"Startpoint: in{i} (input port clocked by clk)\n"
        f"           {1.0 + i:.1f}   data arrival time\n"
        f"          {9.0 - i:.1f}   slack (MET)"
        for i in range(3)
    )
    t = _parse_sta_checks(log)
    assert t["paths"] == 3
    assert t["crit_ns"] == 3.0      # max arrival, not the first
    assert t["wns_ns"] == 7.0      # min slack


def test_parse_sta_checks_survives_malformed_numbers():
    """A junk column must not crash the whole PPA report."""
    junk = (
        "Startpoint: a\n"
        "           1.2.3   data arrival time\n"
        "           4.50   data arrival time\n"
        "           1.0   slack (MET)\n"
    )
    t = _parse_sta_checks(junk)
    assert t["crit_ns"] == 4.5
    assert t["wns_ns"] == 1.0
    assert _parse_sta_checks("wns abc\n")["wns_ns"] is None


def test_fmax_uses_achievable_period_not_reciprocal_crit():
    """1/crit overstates fmax by ignoring setup + IO budget."""
    from vlab.optimize import candidate_metrics

    timing = {"crit_ns": 3.1528, "wns_ns": 6.795, "period_ns": 10.0}
    m = candidate_metrics("pass", {"cell_count": 59}, timing, {"power_w": 1e-5})
    # achievable period = 10 - 6.795 = 3.205 ns -> ~312 MHz
    assert m["fmax_mhz"] == 312.01
    assert m["fmax_mhz"] < 1000.0 / timing["crit_ns"] + 1e-6
    # falls back to 1/crit when the target period is unknown
    m2 = candidate_metrics("pass", {"cell_count": 1}, {"crit_ns": 2.0}, {})
    assert m2["fmax_mhz"] == 500.0
    # no timing at all -> no fmax, not a crash
    m3 = candidate_metrics("pass", {"cell_count": 1}, {}, {})
    assert m3["fmax_mhz"] is None


def test_validate_spec_rejects_uncompilable_ports():
    """Port names/widths must be valid Verilog, not just present.

    A name like "data[7:0]" renders as `input wire [7:0] data[7:0]`, and a
    missing width silently becomes 1 bit, so a width-8 design would be
    scored on 1-bit metrics.
    """
    base = {"dut_name": "d", "description": "x", "pdk_choice": "auto"}

    def spec(ports):
        return dict(base, ports=ports)

    with pytest.raises(ValueError, match="invalid Verilog port name"):
        validate_spec(spec([{"name": "data[7:0]", "direction": "input",
                             "width": 8}]))
    with pytest.raises(ValueError, match="duplicate port"):
        validate_spec(spec([{"name": "a", "direction": "input", "width": 1},
                            {"name": "a", "direction": "output", "width": 1}]))
    with pytest.raises(ValueError, match="width must be >= 1"):
        validate_spec(spec([{"name": "a", "direction": "input", "width": 0}]))
    with pytest.raises(ValueError, match="width must be an int"):
        validate_spec(spec([{"name": "a", "direction": "input",
                             "width": "eight"}]))
    with pytest.raises(ValueError, match="invalid Verilog port name"):
        validate_spec(spec([{"name": "1bad", "direction": "input", "width": 1}]))
    # a missing width normalises to 1 rather than being left undefined
    out = validate_spec(spec([{"name": "a", "direction": "input"}]))
    assert out["ports"][0]["width"] == 1


def test_llm_pick_pdk_uses_first_mentioned(monkeypatch):
    """Registry-order scanning picked sky130 from "nangate45, not sky130"."""
    import vlab.llm as llm_mod
    from vlab.spec import llm_pick_pdk

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for reply, want in [
        ("nangate45 : better than sky130 here", "nangate45"),
        ("asap7 : use it, not sky130", "asap7"),
        ("sky130 : fine", "sky130"),
    ]:
        monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: reply)
        got, _ = llm_pick_pdk("d", "", "auto")
        assert got == want, reply


def test_module_extraction_ignores_submodules():
    """`find("module cnt")` also matches `module cnt_sub`."""
    from vlab.fix import _extract_design, _extract_tb

    text = (
        "```verilog\n"
        "module cnt_sub(input a, output y); assign y = a; endmodule\n"
        "module cnt(input a, output y); assign y = a; endmodule\n"
        "```"
    )
    d = _extract_design(text, "cnt")
    assert d.startswith("module cnt(")
    assert "cnt_sub" not in d

    tb_text = (
        "```verilog\n"
        "module tb_cnt_sub; $finish; endmodule\n"
        "module tb_cnt; $finish; endmodule\n"
        "```"
    )
    t = _extract_tb(tb_text, "cnt")
    assert "module tb_cnt;" in t and "tb_cnt_sub" not in t


def test_require_design_intact_uses_header_not_substring():
    """`a` must not be satisfied by `a_reg`."""
    from vlab.fix import _require_design_intact

    bad = "module d(input clk); reg a_reg; endmodule"
    with pytest.raises(ValueError, match="dropped ports"):
        _require_design_intact(bad, "d", [{"name": "a"}])
    good = "module d(input clk, input a); endmodule"
    _require_design_intact(good, "d", [{"name": "a"}])
    # a renamed module is rejected even if the old name is a prefix
    with pytest.raises(ValueError, match="renamed"):
        _require_design_intact("module d_sub(input a); endmodule", "d",
                               [{"name": "a"}])


def test_parse_result_ignores_fail_prose():
    from vlab.sim import _parse_result

    log = (
        "Note: PASS/FAIL summary below\n"
        "$ vvp rc=0\n"
        'PASS: x\n$finish called at 10\n'
    )
    r = _parse_result(log, 0)
    assert r["fail_count"] == 0
    assert r["pass_count"] == 1
    assert r["status"] == "pass"


def test_parse_result_requires_at_least_one_assertion():
    from vlab.sim import _parse_result

    r = _parse_result("$ vvp rc=0\n", 0)
    assert r["status"] == "fail"
    assert r["pass_count"] == 0
    assert "no assertions" in r.get("note", "")


def test_find_spec_keeps_sessions_whose_name_contains_double_underscore_i(tmp_path):
    """A DUT named `my__int` must not be filtered out as a candidate dir."""
    from vlab.config import find_spec, is_candidate_dir

    assert is_candidate_dir("alu8_20250101_000000__i1c2")
    assert not is_candidate_dir("my__int_20250101_000000")
    (tmp_path / "sessions" / "my__int_20250101_000000").mkdir(parents=True)
    (tmp_path / "sessions" / "my__int_20250101_000000" / "spec.json").write_text("{}")
    sid, _ = find_spec(tmp_path, None)
    assert sid == "my__int_20250101_000000"


def test_builtin_template_drives_inout():
    from vlab.rtl_gen import builtin_template

    spec = {
        "dut_name": "pad", "data_width": 4,
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "io", "direction": "inout", "width": 4},
            {"name": "a", "direction": "input", "width": 4},
            {"name": "y", "direction": "output", "width": 4},
        ],
    }
    d, tb = builtin_template(spec)
    # inout must not become the adder destination
    assert "y <= a + a" in d or "y <= a + " in d
    assert "io" in d
    # and it must be driven, not left floating at z
    assert "assign io = io_tb" in tb


def test_registry_write_is_atomic_and_serialised(tmp_path):
    """Concurrent writers lost entries with an unlocked read-modify-write."""
    import threading

    from vlab import web as web_mod

    (tmp_path / "runs").mkdir(parents=True)
    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            for j in range(20):
                web_mod._registry_write(tmp_path, f"run{i}_{j}", f"sess{i}_{j}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    reg = web_mod._registry_read(tmp_path)
    assert len(reg) == 6 * 20 * 2  # each write adds run_id and sid entries
    # no temp files left behind
    assert not list((tmp_path / "runs").glob("*.tmp"))


def test_sta_liberty_uses_recorded_liberty(tmp_path):
    """STA must read the same liberty synthesis mapped against."""
    from vlab.ppa import _sta_liberty

    lib = tmp_path / "custom.lib"
    lib.write_text("/* custom */\n")
    area = {"liberty": str(lib)}
    assert _sta_liberty(tmp_path, area) == lib
    # falls back to the packaged generic lib when the record is gone
    got = _sta_liberty(tmp_path, {})
    assert got.name == "generic.lib" and got.exists()


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


def test_sanitize_testbench():
    from vlab.fix import sanitize_testbench

    bad = ("module tb_foo;\n\n    `timescale 1ns/1ps\n\n"
           "    reg clk;\n    $finish;\nendmodule\n")
    fixed, warns = sanitize_testbench(bad, "foo")
    assert fixed.startswith("`timescale 1ns/1ps\nmodule tb_foo;")
    assert "`timescale" not in fixed.split("module tb_foo;")[1]
    assert warns  # reports the move
    good = "`timescale 1ns/1ps\nmodule tb_foo;\n$finish;\nendmodule\n"
    fixed2, warns2 = sanitize_testbench(good, "foo")
    assert fixed2 == good and warns2 == []  # idempotent
    with pytest.raises(ValueError, match="\\$finish"):
        sanitize_testbench("module tb_foo;\nendmodule\n", "foo")
    with pytest.raises(ValueError, match="wrong module"):
        sanitize_testbench("module tb_x;\n$finish;\nendmodule\n", "foo")


def test_require_sane():
    from vlab.rtl_gen import _require_sane

    good_d = "module foo(input a); endmodule"
    good_t = "module tb_foo; $dumpfile(\"w\"); $finish; endmodule"
    _require_sane(good_d, good_t, "foo")  # must not raise
    with pytest.raises(ValueError, match="wrong module"):
        _require_sane("module bar; endmodule", good_t, "foo")
    with pytest.raises(ValueError, match="wrong module"):
        _require_sane(good_d, "module tb_x; $finish; endmodule", "foo")
    with pytest.raises(ValueError, match="\\$finish"):
        _require_sane(good_d, "module tb_foo; endmodule", "foo")


def test_gen_retry_then_success(tmp_path, monkeypatch):
    import json as _json

    import vlab.llm as llm_mod
    from vlab.rtl_gen import run_gen

    spec = {
        "dut_name": "foo", "description": "d", "category": "sequential",
        "data_width": 1, "pdk_choice": "auto", "ports": [
            {"name": "a", "direction": "input", "width": 1},
            {"name": "y", "direction": "output", "width": 1}],
    }
    proj = tmp_path / "p"
    (proj / "sessions" / "s1").mkdir(parents=True)
    (proj / "runs" / "s1").mkdir(parents=True)
    (proj / "runs" / "s1" / "spec.json").write_text(_json.dumps(spec))
    good = _json.dumps({
        "design": "module foo(input a, output y); assign y = a; endmodule",
        "testbench": (
            "module tb_foo; reg a; wire y; foo u(a, y);"
            " initial begin a = 1; #1; if (y) $display(\"PASS: y\");"
            " $finish; end endmodule")})
    calls = {"n": 0}

    def fake_chat(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"design": "module foo(truncated'
        return good

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    m = run_gen(proj, "s1", proj / "runs" / "s1" / "spec.json", use_llm=True)
    assert calls["n"] == 2
    assert (proj / "runs" / "s1" / "gen_raw_retry.txt").exists()
    assert "module foo" in open(proj / "runs" / "s1" / "design.v").read()
    # the first rejection is recorded for diagnosis
    assert any("first attempt rejected" in w for w in m["warnings"])


def test_gen_double_failure_raises(tmp_path, monkeypatch):
    import json as _json

    import vlab.llm as llm_mod
    from vlab.rtl_gen import run_gen

    spec = {
        "dut_name": "foo", "description": "d", "category": "sequential",
        "data_width": 1, "pdk_choice": "auto", "ports": [
            {"name": "a", "direction": "input", "width": 1},
            {"name": "y", "direction": "output", "width": 1}],
    }
    proj = tmp_path / "p"
    (proj / "sessions" / "s1").mkdir(parents=True)
    (proj / "runs" / "s1").mkdir(parents=True)
    (proj / "runs" / "s1" / "spec.json").write_text(_json.dumps(spec))
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: "thinking prose, no code")
    with pytest.raises(ValueError):
        run_gen(proj, "s1", proj / "runs" / "s1" / "spec.json", use_llm=True)


def test_require_ports():
    from vlab.rtl_gen import _require_ports, module_ports

    spec = {"dut_name": "d", "ports": [{"name": "clk"}, {"name": "weird_sig"}]}
    _require_ports("module d(input clk, input weird_sig); endmodule", spec)
    with pytest.raises(ValueError, match="weird_sig"):
        _require_ports("module d(input clk, input other); endmodule", spec)

    # ANSI and non-ANSI headers both parse
    assert module_ports("module d(input clk, output [7:0] y); endmodule", "d") == \
        {"clk", "y"}
    non_ansi = "module d(clk, y);\n input clk;\n output [7:0] y;\nendmodule"
    assert module_ports(non_ansi, "d") == {"clk", "y"}
    assert module_ports("module other(x); endmodule", "d") is None

    # A port name that only appears in the body is NOT a declared port:
    # the old whole-file substring search passed these.
    body_only = "module d(input clk); reg weird_sig; assign weird_sig = clk; endmodule"
    with pytest.raises(ValueError, match="weird_sig"):
        _require_ports(body_only, {"dut_name": "d",
                                   "ports": [{"name": "weird_sig"}]})
    assert module_ports(body_only, "d") == {"clk"}


def test_require_tb_ports():
    from vlab.rtl_gen import _require_tb_ports

    spec = {"dut_name": "d", "ports": [
        {"name": "a"}, {"name": "y"}, {"name": "en"}]}
    good = "module tb_d; reg a, en; wire y; d u(a, y, en); $finish; endmodule"
    _require_tb_ports(good, spec)
    # a TB that never mentions `en` cannot detect a DUT ignoring it
    with pytest.raises(ValueError, match="en"):
        _require_tb_ports("module tb_d; reg a; wire y; d u(a, y, unused);"
                          " a = 1; $finish; endmodule", spec)


def test_gen_retries_on_port_mismatch(tmp_path, monkeypatch):
    import json as _json

    import vlab.llm as llm_mod
    from vlab.rtl_gen import run_gen

    spec = {
        "dut_name": "foo", "description": "d", "category": "sequential",
        "data_width": 1, "pdk_choice": "auto", "ports": [
            {"name": "a", "direction": "input", "width": 1},
            {"name": "y", "direction": "output", "width": 1}],
    }
    proj = tmp_path / "p"
    (proj / "sessions" / "s1").mkdir(parents=True)
    (proj / "runs" / "s1").mkdir(parents=True)
    (proj / "runs" / "s1" / "spec.json").write_text(_json.dumps(spec))
    wrong = _json.dumps({
        "design": "module foo(input q, output z); assign z = q; endmodule",
        "testbench": "module tb_foo; reg q; wire z; foo u(q, z);"
                     " initial begin $finish; end endmodule"})
    right = _json.dumps({
        "design": "module foo(input a, output y); assign y = a; endmodule",
        "testbench": "module tb_foo; reg a; wire y; foo u(a, y);"
                     " initial begin a = 1; #1; if (y) $display(\"PASS: y\");"
                     " $finish; end endmodule"})
    calls = {"n": 0}

    def fake_chat(*a, **k):
        calls["n"] += 1
        return wrong if calls["n"] == 1 else right

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    m = run_gen(proj, "s1", proj / "runs" / "s1" / "spec.json", use_llm=True)
    assert calls["n"] == 2
    assert "input a" in open(proj / "runs" / "s1" / "design.v").read()
