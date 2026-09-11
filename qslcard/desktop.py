"""Entry point for the packaged desktop application.

Responsibilities specific to the frozen build:

* A windowed executable has no console, so sys.stdout/sys.stderr may be None and
  any print() or traceback write would raise.  They are redirected to a log file
  inside the data directory, which also gives users something to send when
  something goes wrong.
* A first run creates the data directory, the database, the vault and editable
  copies of the built-in templates, so the GUI works with zero configuration.
* --selftest runs the whole pipeline headlessly and writes a JSON report, which
  is how the frozen bundle is verified in CI without a human clicking buttons.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__

__all__ = ["main", "prepare_environment", "run_selftest"]

APP_NAME = "QSL卡片打印系统"


def _data_dir() -> Path:
    override = os.environ.get("QSLCARD_HOME")
    if override:
        return Path(override)
    return Path.home() / ".qslcard"


def prepare_environment() -> Path:
    """Make the process safe for a windowed build and ready to run."""
    home = _data_dir()
    home.mkdir(parents=True, exist_ok=True)
    log_path = home / "qslcard.log"
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    # Seed editable template copies on first run.
    try:
        from .templates import available_templates, builtin_template

        target = home / "templates"
        target.mkdir(parents=True, exist_ok=True)
        for name in available_templates():
            path = target / f"{name}.json"
            if not path.exists():
                builtin_template(name).save(path)
    except Exception:  # noqa: BLE001 - templates are a convenience
        pass
    return home


def run_selftest(workdir: str = "") -> int:
    """Exercise the packaged application end to end without a display."""
    target = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="qslcard-selftest-"))
    target.mkdir(parents=True, exist_ok=True)
    os.environ["QSLCARD_HOME"] = str(target / "home")
    logs = target / "logs"
    logs.mkdir(exist_ok=True)
    report: dict[str, Any] = {"workdir": str(target), "version": __version__, "steps": []}

    sample = (
        "<ADIF_VER:5>3.1.7\n<PROGRAMID:7>qslcard\n<EOH>\n"
        "<CALL:5>JA1AA<QSO_DATE:8>20260911<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB"
        "<RST_SENT:2>59<RST_RCVD:2>59<NAME:4>Taro<QTH:5>Tokyo<COUNTRY:5>Japan<EOR>\n"
        "<CALL:5>VK2XY<QSO_DATE:8>20260911<TIME_ON:4>1230<BAND:3>40m<MODE:2>CW"
        "<RST_SENT:3>599<RST_RCVD:3>579<COUNTRY:9>Australia<EOR>\n"
        "<CALL:6>W1ABCD<QSO_DATE:8>20260912<TIME_ON:4>0100<BAND:3>15m<MODE:3>FT8"
        "<RST_SENT:3>-10<RST_RCVD:3>-15<COUNTRY:3>USA<EOR>\n"
        "<CALL:5>DL1ZZ<QSO_DATE:8>20260913<TIME_ON:4>0300<BAND:3>20m<MODE:3>SSB"
        "<RST_SENT:2>59<RST_RCVD:2>58<COUNTRY:7>Germany<LOTW_QSL_RCVD:1>Y<EOR>\n"
    )
    (logs / "sample.adi").write_text(sample, encoding="utf-8")
    report["steps"].append("write-sample")

    from .config import AppConfig, save_config

    config_path = target / "config.json"
    config = AppConfig().resolved_paths()
    config.station.callsign = "BG1XYZ"
    config.station.name = "Selftest"
    config.station.qth = "Beijing"
    config.station.address = "Beijing, China"
    config.station.grid = "PM95"
    config.sources = {"adif": {"paths": [str(logs)]}}
    config.database = str(target / "qslcard.db")
    config.output_dir = str(target / "out")
    config.offline = True
    save_config(config, config_path)
    report["steps"].append("write-config")

    from .cards import plan_cards, summarize_cards
    from .geometry import SheetSpec, best_layout
    from .pdfout import CardRenderer, RenderOptions
    from .rules import CardRule, select_qsos
    from .store import Store
    from .templates import builtin_template

    store = Store(config.database)
    try:
        from .adif import iter_qsos

        errors: list[dict[str, Any]] = []
        stats = store.import_qsos(
            iter_qsos(logs / "sample.adi", errors), strategy="merge", source="adif"
        )
        report["import"] = stats.as_dict()
        report["steps"].append("import")
        assert stats.inserted == 4, f"expected 4 records, got {stats.inserted}"

        rule = CardRule(require_unconfirmed=True, require_unsent=True)
        selected = list(select_qsos(store, rule))
        report["selected"] = len(selected)
        assert len(selected) == 3, f"expected 3 card candidates, got {len(selected)}"

        cards = plan_cards(selected, group_by="call", max_per_card=4)
        report["cards"] = summarize_cards(cards)

        template = builtin_template("classic")
        layout = best_layout(SheetSpec.from_name("A4"), template.card)
        report["layout"] = layout.describe()
        options = RenderOptions(
            template=template, station=config.station, print_config=config.print, duplex=True
        )
        out = Path(config.output_dir) / "selftest.pdf"
        result = CardRenderer(options).render(cards, out)
        report["pdf"] = {
            "path": str(out),
            "bytes": result.bytes_written,
            "pages": result.pages,
            "sheets": result.sheets,
            "warnings": list(result.warnings),
        }
        report["steps"].append("render")
        assert out.is_file() and out.read_bytes().startswith(b"%PDF"), "PDF was not produced"
        assert result.pages >= 2, "expected front and back pages"

        # Commercial print path: CMYK, marks and PDF/X style page boxes.
        from .config import apply_print_preset

        commercial = apply_print_preset(config.print, "commercial")
        commercial = replace(commercial, duplex=False)
        commercial_options = RenderOptions(
            template=template,
            station=config.station,
            print_config=commercial,
            duplex=False,
            compress=False,  # keep operators greppable for this check
        )
        out2 = Path(config.output_dir) / "selftest-commercial.pdf"
        result2 = CardRenderer(commercial_options).render(cards, out2)
        payload = out2.read_bytes()
        report["commercial"] = {
            "color": result2.color_mode,
            "pdf_type": result2.pdf_type,
            "page_boxes": result2.page_boxes,
            "pages": result2.pages,
            "trim_box": b"/TrimBox [" in payload,
            "bleed_box": b"/BleedBox [" in payload,
            "device_cmyk": b" k" in payload,
        }
        report["steps"].append("commercial-render")
        assert result2.color_mode == "CMYK", "commercial preset must use CMYK"
        assert result2.pdf_type == "pdfx", "commercial preset must write PDF/X boxes"
        assert result2.page_boxes == result2.pages, "every page needs page boxes"
        assert b"/TrimBox [" in payload and b"/BleedBox [" in payload
        assert b" k" in payload, "device CMYK operator missing"

        # Dispatch / return tracking and the QRZ write-back payload.
        from .model import business_key
        from .sources.qrz import qsl_status_adif
        from .tracking import record_dispatch, record_returns

        flat = [qso for card in cards for qso in card.qsos]
        keys = [business_key(qso) for qso in flat]
        dispatch = record_dispatch(store, flat, sent_date="2026-09-20", via="BURO", batch_id=1)
        adif = qsl_status_adif(cards[0].primary, sent_date="2026-09-20", via="BURO")
        record_returns(store, [cards[0].primary], returned_date="2026-10-10", via="BURO")
        report["tracking"] = {
            "recorded": dispatch.recorded,
            "card_log_rows": len(store.card_log()),
            "stats": store.dispatch_stats(),
            "qrz_write_back": adif,
        }
        report["steps"].append("tracking")
        assert dispatch.recorded == len(keys), "every carded QSO must be logged"
        assert len(store.card_log()) == len(keys)
        assert store.dispatch_stats()["returned"] == 1, "one return must be recorded"
        assert "<QSLSDATE:8>20260920" in adif, "QRZ write-back must carry the sent date"
        assert store.query()[0].qslsdate == "20260920", "QSO status must mirror the log"

        # Paper labels carry millimetres and the QSL route round-trips; neither
        # needs a display, so they are checked on every platform.
        from .config import format_qsl_via, parse_qsl_via
        from .editor import add_element, move_element
        from .geometry import paper_label
        from .templates import CardFace

        element = add_element(CardFace(), "rect", template.card)
        move_element(element, 5.0, 5.0, template.card)
        report["paper_label"] = paper_label("A4")
        report["qsl_via_round_trip"] = format_qsl_via(parse_qsl_via("BURO/DIRECT"))
        assert "mm" in report["paper_label"], "paper choices must show millimetres"
        assert report["qsl_via_round_trip"] == "BUREAU,DIRECT"

        # The lazily imported GUI modules must exist inside the frozen bundle.
        # tkinter is optional: a headless Linux build may not ship it, and the
        # core must still work there.
        try:
            import tkinter  # noqa: F401
        except ImportError as exc:
            report["gui_modules"] = {"available": False, "reason": str(exc)}
        else:
            from .cardlog_panel import CardLogPanel
            from .credentials_panel import CredentialsDialog
            from .designer import CardDesigner
            from .gui import QslCardWindow
            from .print_panel import PrintSettingsPanel

            report["gui_modules"] = {
                "available": True,
                "designer": CardDesigner.__name__,
                "print_panel": PrintSettingsPanel.__name__,
                "card_log": CardLogPanel.__name__,
                "credentials": CredentialsDialog.__name__,
                "main_window": QslCardWindow.__name__,
                "editor_ok": element.w_mm > 0,
            }
            assert "main_window" in report["gui_modules"]
        report["steps"].append("gui-modules")
    finally:
        store.close()

    report["ok"] = True
    (target / "selftest-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


#: Subcommands that belong to the console interface rather than the GUI.
CLI_COMMANDS = frozenset(
    {
        "import",
        "sync",
        "callbook",
        "cards",
        "export",
        "track",
        "db",
        "privacy",
        "vault",
        "templates",
        "sources",
        "config",
    }
)


def _wants_cli(args_in: list[str]) -> bool:
    """Decide between the console interface and the GUI.

    The console binary always uses the CLI.  Otherwise a run is treated as CLI
    work when a known subcommand appears anywhere (so -c CONFIG cards ... works)
    or when help is requested; a bare launch opens the GUI.
    """
    # --selftest is a diagnostic of the packaged binary, so it must win over the
    # console-binary default; otherwise qslcard-cli.exe could never self-test.
    if "--selftest" in args_in:
        return False
    if Path(sys.argv[0]).stem.lower().endswith("-cli"):
        return True
    if not args_in:
        return False
    if args_in[0] in ("-h", "--help"):
        return True
    return any(token in CLI_COMMANDS for token in args_in)


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if _wants_cli(args_in):
        from .cli import main as cli_main

        return cli_main(args_in)

    parser = argparse.ArgumentParser(prog=APP_NAME, description="QSL 卡片打印系统（图形界面）")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("-c", "--config", default="", help="配置文件路径")
    parser.add_argument("--selftest", nargs="?", const="", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--report", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(args_in)

    if args.selftest is not None:
        prepare_environment()
        try:
            return run_selftest(args.selftest)
        except Exception as exc:  # noqa: BLE001 - reported as a failure code
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            target = Path(args.selftest) if args.selftest else Path(tempfile.gettempdir())
            target.mkdir(parents=True, exist_ok=True)
            (target / "selftest-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False))
            return 1

    home = prepare_environment()
    from .cli import App

    app = App.open(args.config or None)
    try:
        from .gui import run_gui

        return run_gui(app)
    except Exception as exc:  # noqa: BLE001 - windowed builds must never die silently
        import traceback

        traceback.print_exc()
        tcl_broken = type(exc).__name__ == "TclError" or "init.tcl" in str(exc)
        detail = (
            "本机的 Tk 图形环境不可用（Tcl/Tk 数据文件缺失）。\n"
            "你仍然可以用命令行版本完成同样的工作：\n"
            "  qslcard-cli import <ADIF 目录>\n"
            "  qslcard-cli cards --out out/cards.pdf\n"
            "  qslcard-cli cardlog stats\n"
            if tcl_broken
            else "程序启动时出错。\n"
        )
        try:
            import tkinter.messagebox as mb

            mb.showerror("启动失败", detail + "详情已写入日志：\n" + str(home / "qslcard.log"))
        except Exception:  # noqa: BLE001
            # No usable GUI at all: make sure the message reaches stdout/log.
            print(detail)
            print(f"error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
