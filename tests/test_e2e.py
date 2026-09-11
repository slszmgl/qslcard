"""End-to-end test: ADIF on disk becomes a printable PDF (SRS section 13)."""

from __future__ import annotations

import json
from pathlib import Path

from qslcard.cli import main
from qslcard.store import QsoFilter, Store

SAMPLE_RECORDS = (
    "<CALL:5>JA1AA<QSO_DATE:8>20260911<TIME_ON:4>1200<BAND:3>20m<MODE:3>SSB"
    "<RST_SENT:2>59<RST_RCVD:2>59<NAME:4>Taro<QTH:5>Tokyo<COUNTRY:5>Japan<EOR>",
    "<CALL:5>VK2XY<QSO_DATE:8>20260911<TIME_ON:4>1230<BAND:3>40m<MODE:2>CW"
    "<RST_SENT:3>599<RST_RCVD:3>579<COUNTRY:9>Australia<EOR>",
    "<CALL:6>W1ABCD<QSO_DATE:8>20260912<TIME_ON:4>0100<BAND:3>15m<MODE:3>FT8"
    "<RST_SENT:3>-10<RST_RCVD:3>-15<COUNTRY:3>USA<EOR>",
    "<CALL:5>JA1AA<QSO_DATE:8>20260913<TIME_ON:4>0200<BAND:3>20m<MODE:3>FT8"
    "<RST_SENT:3>-05<RST_RCVD:3>-08<COUNTRY:5>Japan<EOR>",
    "<CALL:5>DL1ZZ<QSO_DATE:8>20260913<TIME_ON:4>0300<BAND:3>20m<MODE:3>SSB"
    "<RST_SENT:2>59<RST_RCVD:2>58<COUNTRY:7>Germany<LOTW_QSL_RCVD:1>Y<EOR>",
)


def _write_sample(path: Path) -> None:
    path.write_text("<EOH>\n" + "\n".join(SAMPLE_RECORDS) + "\n", encoding="utf-8")


def _prepare(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("QSLCARD_HOME", str(home))
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_sample(logs / "sample.adi")
    config_path = tmp_path / "config.json"
    assert main(["-c", str(config_path), "config", "init", "--path", str(config_path)]) == 0
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["station"]["callsign"] = "BG1XYZ"
    data["station"]["name"] = "Zhang San"
    data["station"]["qth"] = "Beijing"
    data["station"]["grid"] = "PM95"
    data["station"]["address"] = "Chaoyang, Beijing, China"
    data["sources"] = {"adif": {"paths": [str(logs)]}}
    data["database"] = str(home / "qslcard.db")
    data["output_dir"] = str(tmp_path / "out")
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "home" / "qslcard.db")


def test_adif_to_printable_pdf(tmp_path, monkeypatch, capsys) -> None:
    config = _prepare(tmp_path, monkeypatch)

    assert main(["-c", str(config), "import", str(tmp_path / "logs")]) == 0
    assert "导入完成" in capsys.readouterr().out

    with Store(_db(tmp_path)) as store:
        assert store.count() == 5
        # DL1ZZ is already LoTW-confirmed, so four records still need a card.
        assert store.count(QsoFilter(needs_card=True)) == 4

    pdf = tmp_path / "out" / "batch.pdf"
    assert (
        main(
            [
                "-c",
                str(config),
                "cards",
                "--out",
                str(pdf),
                "--no-compress",
                "--core-font",
                "--template",
                "classic",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "拼版" in text
    assert "已生成" in text
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF")
    # With the core font and no compression the text is searchable, which proves
    # the station block and the counterparty callsign reached the page.
    assert b"BG1XYZ" in data
    assert b"JA1AA" in data
    assert b"2026-09-11" in data

    with Store(_db(tmp_path)) as store:
        card_batch = next(b for b in store.recent_batches(5) if b["kind"] == "cards")
        keys = store.batch_bkeys(card_batch["id"])
        assert len(keys) == 4
        track = main(
            ["-c", str(config), "track", "--batch", str(card_batch["id"]), "--status", "sent"]
        )
        assert track == 0
        assert store.count(QsoFilter(needs_card=True)) == 0


def test_export_adif_and_csv_round_trip(tmp_path, monkeypatch) -> None:
    config = _prepare(tmp_path, monkeypatch)
    assert main(["-c", str(config), "import", str(tmp_path / "logs")]) == 0

    adif_out = tmp_path / "exported.adi"
    csv_out = tmp_path / "exported.csv"
    assert main(["-c", str(config), "export", "adif", "--out", str(adif_out)]) == 0
    assert main(["-c", str(config), "export", "csv", "--out", str(csv_out)]) == 0
    assert adif_out.read_text(encoding="utf-8").count("<EOR>") == 5
    rows = csv_out.read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(rows) == 6  # header plus five records
    assert rows[0].startswith("call,qso_date")


def test_cards_reports_layout_notes(tmp_path, monkeypatch, capsys) -> None:
    config = _prepare(tmp_path, monkeypatch)
    assert main(["-c", str(config), "import", str(tmp_path / "logs")]) == 0
    capsys.readouterr()
    pdf = tmp_path / "letter.pdf"
    assert main(["-c", str(config), "cards", "--out", str(pdf), "--paper", "LETTER"]) == 0
    assert "LETTER" in capsys.readouterr().out
    assert pdf.is_file()


def test_privacy_selfcheck_passes_on_a_clean_home(tmp_path, monkeypatch, capsys) -> None:
    config = _prepare(tmp_path, monkeypatch)
    assert main(["-c", str(config), "privacy"]) == 0
    text = capsys.readouterr().out
    assert "隐私自检" in text
    assert "遥测：无" in text


def test_sources_and_templates_commands(tmp_path, monkeypatch, capsys) -> None:
    config = _prepare(tmp_path, monkeypatch)
    assert main(["-c", str(config), "sources"]) == 0
    assert main(["-c", str(config), "templates"]) == 0
    text = capsys.readouterr().out
    for name in ("adif", "qrz", "hamqth", "lotw", "clublog", "eqsl", "classic"):
        assert name in text


def test_card_log_cli_records_returns_and_exports(tmp_path, monkeypatch, capsys) -> None:
    config = _prepare(tmp_path, monkeypatch)
    assert main(["-c", str(config), "import", str(tmp_path / "logs")]) == 0
    pdf = tmp_path / "out" / "batch.pdf"
    assert main(["-c", str(config), "cards", "--out", str(pdf)]) == 0
    capsys.readouterr()

    with Store(_db(tmp_path)) as store:
        card_batch = next(b for b in store.recent_batches(5) if b["kind"] == "cards")
        batch_id = card_batch["id"]

    # Recording a dispatch writes both the card log and the QSO status fields.
    assert (
        main(
            [
                "-c",
                str(config),
                "track",
                "--batch",
                str(batch_id),
                "--status",
                "sent",
                "--date",
                "2026-09-15",
                "--via",
                "BURO",
            ]
        )
        == 0
    )
    assert "发卡登记" in capsys.readouterr().out

    with Store(_db(tmp_path)) as store:
        rows = store.card_log()
        assert len(rows) == 4
        assert {row["sent_date"] for row in rows} == {"20260915"}
        assert {row["sent_via"] for row in rows} == {"BURO"}
        assert store.dispatch_stats()["sent"] == 4
        # DL1ZZ was already LoTW-confirmed and never entered the batch.
        updated = {q.call for q in store.query() if q.qslsdate == "20260915" and q.qsl_sent == "Y"}
        assert updated == {"JA1AA", "VK2XY", "W1ABCD"}

    assert main(["-c", str(config), "cardlog", "stats"]) == 0
    stats_text = capsys.readouterr().out
    assert "回卡率" in stats_text

    assert main(["-c", str(config), "cardlog", "list"]) == 0
    assert "JA1AA" in capsys.readouterr().out

    assert (
        main(
            [
                "-c",
                str(config),
                "cardlog",
                "return",
                "--call",
                "JA1AA",
                "--date",
                "2026-10-05",
                "--via",
                "BURO",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exported = tmp_path / "cardlog.csv"
    assert main(["-c", str(config), "cardlog", "export", "--out", str(exported)]) == 0
    assert "已导出 4" in capsys.readouterr().out
    assert imported_row(exported)["sent_date"] == "20260915"

    with Store(_db(tmp_path)) as store:
        info = store.dispatch_stats()
        # JA1AA contributed two QSOs, so both of its card-log rows are returned.
        assert info["returned"] == 2
        assert info["return_rate"] == 0.5
        ja1 = [q for q in store.query() if q.call == "JA1AA"]
        assert ja1 and all(q.qsl_rcvd == "Y" for q in ja1)
        untouched = next(q for q in store.query() if q.call == "VK2XY")
        assert untouched.qsl_rcvd == ""


def imported_row(path) -> dict:
    import csv

    with open(path, encoding="utf-8-sig", newline="") as handle:
        return next(csv.DictReader(handle))
