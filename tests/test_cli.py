"""The berth command line."""

from __future__ import annotations

import csv
import json

from berth.cli import EXIT_INPUT, EXIT_OUTPUT, EXIT_SHORT, main

from conftest import EXAMPLES


def test_plan_example_writes_csv_and_json(tmp_path, capsys):
    out = tmp_path / "plan.csv"
    js = tmp_path / "plan.json"
    code = main(["plan", str(EXAMPLES / "delivery-vans.yaml"), "-o", str(out), "--json", str(js)])
    assert code == 0
    text = capsys.readouterr().out
    assert "All 12 vehicles will be ready" in text
    assert "Berth plan" in text and "Charge on arrival" in text
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["time"] == "16:00"
    assert "VAN-01_kw" in rows[0]
    assert json.loads(js.read_text())["all_ready"] is True


def test_plan_from_csv_files(tmp_path, capsys):
    out = tmp_path / "s.csv"
    code = main([
        "plan", "--vehicles", str(EXAMPLES / "csv" / "vans.csv"), "--tariff", str(EXAMPLES / "csv" / "tariff-tou.csv"),
        "--chargers", "12x19.2", "--site-limit-kw", "150", "--other-load-kw", "12", "--start", "16:00", "-o", str(out),
    ])
    assert code == 0
    assert out.exists()
    assert "All 12 vehicles will be ready" in capsys.readouterr().out


def test_fail_if_short_exit_code(tmp_path):
    code = main(["plan", "--example", "late-shift-infeasible", "--no-csv", "--fail-if-short"])
    assert code == EXIT_SHORT


def test_input_errors_exit_cleanly(tmp_path, capsys):
    assert main(["plan", str(tmp_path / "missing.yaml")]) == EXIT_INPUT
    assert "file not found" in capsys.readouterr().err
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsite: {power_limit_kw: 10, chargers: [{count: 1, kw: 7}]}\n")
    assert main(["plan", str(bad)]) == EXIT_INPUT
    err = capsys.readouterr().err
    assert "tariff: is required but missing" in err
    assert main(["plan", "--vehicles", str(EXAMPLES / "csv" / "vans.csv")]) == EXIT_INPUT
    assert "--tariff" in capsys.readouterr().err


def test_nan_on_the_command_line_is_refused(capsys):
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["plan", "--example", "delivery-vans", "--no-csv", "--time-limit", "nan"])
    assert e.value.code == 2
    assert "not allowed" in capsys.readouterr().err


def test_output_that_cannot_be_written_is_a_clear_error(tmp_path, capsys):
    code = main(["plan", "--example", "delivery-vans", "-o", str(tmp_path / "no-such-folder" / "x.csv")])
    assert code == EXIT_OUTPUT
    err = capsys.readouterr().err
    assert err.startswith("Output error: could not write")
    assert "Traceback" not in err


def test_explain_and_examples(capsys):
    assert main(["explain", "--example", "late-shift-infeasible"]) == 0
    text = capsys.readouterr().out
    assert "Why some vehicles fall short" in text
    assert main(["examples"]) == 0
    assert "school-buses" in capsys.readouterr().out


def test_shortfall_policy_flag(capsys):
    assert main(["plan", "--example", "late-shift-infeasible", "--no-csv", "--shortfall-policy", "fair"]) == 0
    out = capsys.readouterr().out
    assert "Policy fair" in out
    assert main(["plan", "--example", "late-shift-infeasible", "--no-csv"]) == 0
    assert "Policy most_ready" in capsys.readouterr().out
