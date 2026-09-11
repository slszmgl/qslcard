"""Tests for the packaged desktop entry point."""

from __future__ import annotations

import json
from pathlib import Path

from qslcard import desktop


def test_selftest_runs_the_full_pipeline(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QSLCARD_HOME", str(tmp_path / "home"))
    assert desktop.run_selftest(str(tmp_path / "work")) == 0
    report = json.loads((tmp_path / "work" / "selftest-report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["import"]["inserted"] == 4
    assert report["selected"] == 3
    assert report["cards"]["cards"] == 3
    assert report["pdf"]["pages"] >= 2
    assert report["pdf"]["bytes"] > 1000
    assert Path(report["pdf"]["path"]).is_file()
    assert Path(report["pdf"]["path"]).read_bytes().startswith(b"%PDF")
    assert "A4" in report["layout"]
    # The commercial path and the lazily imported GUI modules are covered too.
    assert report["commercial"]["color"] == "CMYK"
    assert report["commercial"]["pdf_type"] == "pdfx"
    assert report["commercial"]["trim_box"] is True
    assert report["commercial"]["bleed_box"] is True
    assert report["commercial"]["device_cmyk"] is True
    assert report["commercial"]["page_boxes"] == report["commercial"]["pages"]
    assert report["gui_modules"]["editor_ok"] is True
    if report["gui_modules"].get("available"):
        assert report["gui_modules"]["card_log"] == "CardLogPanel"
        assert report["gui_modules"]["credentials"] == "CredentialsDialog"
        assert report["gui_modules"]["main_window"] == "QslCardWindow"
    assert "mm" in report["paper_label"]
    assert report["qsl_via_round_trip"] == "BUREAU,DIRECT"
    # Dispatch recording, the return tracker and the QRZ write-back payload.
    assert report["tracking"]["recorded"] == report["tracking"]["card_log_rows"] == 3
    assert report["tracking"]["stats"]["sent"] == 3
    assert report["tracking"]["stats"]["returned"] == 1
    assert "<QSLSDATE:8>20260920" in report["tracking"]["qrz_write_back"]


def test_main_keeps_gui_launch_off_the_cli_path(monkeypatch) -> None:
    # No arguments and no subcommand means the GUI; the console binary never is.
    assert desktop._wants_cli([]) is False
    assert desktop._wants_cli(["--selftest"]) is False
    assert desktop._wants_cli(["-c", "cfg.json", "cards"]) is True
    assert desktop._wants_cli(["--help"]) is True


def test_selftest_wins_over_the_console_binary_name(monkeypatch) -> None:
    # qslcard-cli.exe must still be able to run the packaged self-test.
    monkeypatch.setattr(desktop.sys, "argv", ["qslcard-cli.exe", "--selftest", "out"])
    assert desktop._wants_cli(["--selftest", "out"]) is False


def test_main_routes_subcommands_to_the_cli(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("QSLCARD_HOME", str(tmp_path / "home"))
    config = tmp_path / "config.json"
    assert desktop.main(["-c", str(config), "config", "init"]) == 0
    assert "已写入默认配置" in capsys.readouterr().out


def test_main_selftest_writes_a_report_and_sets_exit_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QSLCARD_HOME", str(tmp_path / "home"))
    assert desktop.main(["--selftest", str(tmp_path / "self")]) == 0
    report = tmp_path / "self" / "selftest-report.json"
    assert report.is_file()
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is True


def test_prepare_environment_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QSLCARD_HOME", str(tmp_path / "home"))
    first = desktop.prepare_environment()
    second = desktop.prepare_environment()
    assert first == second
    assert (first / "templates" / "classic.json").is_file()
