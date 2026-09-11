"""Tests for configuration handling and the rule engine."""

from __future__ import annotations

import json

from qslcard.adif import qso_from_fields
from qslcard.config import AppConfig, load_config, resolve_secret, save_config
from qslcard.rules import CardRule, RuleContext, select_qsos
from qslcard.store import Store


def rec(**fields: str):
    return qso_from_fields(fields)


def test_defaults_match_srs_decisions(tmp_path) -> None:
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.print.card_width_mm == 90.0
    assert cfg.print.card_height_mm == 140.0
    assert cfg.print.bleed_mm == 3.0
    assert cfg.print.preset == "home"
    assert cfg.print.calibration_mm == 3.0
    assert cfg.offline is False


def test_json_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.station.callsign = "BG1XYZ"
    cfg.print.paper = "A4"
    cfg.rules["needs_card"] = {"bands": ["20m"], "limit": 25}
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.station.callsign == "BG1XYZ"
    assert loaded.rules["needs_card"]["limit"] == 25


def test_unknown_keys_are_ignored(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"station": {"callsign": "A1AA", "bogus": 1}}), encoding="utf-8")
    assert load_config(path).station.callsign == "A1AA"


def test_resolve_secret_env_and_vault(monkeypatch) -> None:
    class FakeVault:
        def get(self, name: str, default: str | None = None) -> str | None:
            return {"qrz": "from-vault"}.get(name, default)

    monkeypatch.setenv("MY_TOKEN", "from-env")
    assert resolve_secret("env:MY_TOKEN") == "from-env"
    assert resolve_secret("vault:qrz", FakeVault()) == "from-vault"
    assert resolve_secret("literal") == "literal"
    assert resolve_secret("") == ""


def test_rule_from_dict_and_filter() -> None:
    rule = CardRule.from_dict({"bands": ["20m", "40m"], "modes": "CW,FT8", "limit": "10"})
    assert rule.bands == ("20m", "40m")
    assert rule.modes == ("CW", "FT8")
    assert rule.limit == 10
    flt = rule.to_filter()
    assert flt.band == ("20m", "40m")
    assert flt.mode == ("CW", "FT8")
    assert flt.confirmed is False
    assert flt.qsl_sent is False


def test_rule_matches_in_memory() -> None:
    rule = CardRule(bands=("20m",), modes=("SSB",))
    assert rule.matches(
        rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20M", MODE="ssb")
    )
    assert not rule.matches(
        rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="40m", MODE="SSB")
    )
    assert not rule.matches(
        rec(
            CALL="A1AA",
            QSO_DATE="20260101",
            TIME_ON="1200",
            BAND="20m",
            MODE="SSB",
            LOTW_QSL_RCVD="Y",
        )
    )
    assert not rule.matches(
        rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB", QSL_SENT="Y")
    )


def test_rule_new_dxcc_and_route() -> None:
    ctx = RuleContext(confirmed_dxcc=frozenset({"Japan"}), address_calls=frozenset({"JA1ABC"}))
    new_dxcc = CardRule(only_new_dxcc=True)
    assert new_dxcc.matches(rec(CALL="VK2XY", COUNTRY="Australia"), ctx)
    assert not new_dxcc.matches(rec(CALL="JA1ABC", COUNTRY="Japan"), ctx)
    route = CardRule(route_exists=True)
    assert route.matches(rec(CALL="JA1ABC"), ctx)
    assert not route.matches(rec(CALL="VK2XY"), ctx)


def test_select_qsos_streams_and_honours_limit() -> None:
    with Store(":memory:") as store:
        store.import_qsos(
            [
                rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB"),
                rec(
                    CALL="B2BB",
                    QSO_DATE="20260102",
                    TIME_ON="1300",
                    BAND="20m",
                    MODE="SSB",
                    LOTW_QSL_RCVD="Y",
                ),
                rec(
                    CALL="C3CC",
                    QSO_DATE="20260103",
                    TIME_ON="1400",
                    BAND="20m",
                    MODE="SSB",
                    QSL_SENT="Y",
                ),
                rec(CALL="D4DD", QSO_DATE="20260104", TIME_ON="1500", BAND="40m", MODE="SSB"),
            ],
            strategy="skip",
        )
        rule = CardRule(bands=("20m",))
        assert [q.call for q in select_qsos(store, rule)] == ["A1AA"]
        assert len(list(select_qsos(store, CardRule(limit=1)))) == 1
        assert (
            len(list(select_qsos(store, CardRule(require_unsent=False, require_unconfirmed=False))))
            == 4
        )


def test_qsl_via_multi_select_helpers() -> None:
    from qslcard.config import format_qsl_via, parse_qsl_via, qsl_via_display

    # Stored forms seen in the wild all fold onto canonical keys.
    assert parse_qsl_via("BURO/DIRECT") == ["BUREAU", "DIRECT"]
    assert parse_qsl_via("bureau, oqrs") == ["BUREAU", "OQRS"]
    assert parse_qsl_via("B,D") == ["BUREAU", "DIRECT"]
    assert parse_qsl_via("") == []
    assert parse_qsl_via("WEIRD") == ["WEIRD"]

    # Output order is stable regardless of click order.
    assert format_qsl_via(["DIRECT", "BUREAU"]) == "BUREAU,DIRECT"
    assert format_qsl_via(["OQRS"]) == "OQRS"
    assert format_qsl_via([]) == ""
    assert format_qsl_via(["BUREAU", "DIRECT", "CUSTOM"]) == "BUREAU,DIRECT,CUSTOM"

    assert qsl_via_display("BUREAU,DIRECT") == "卡片局 (Bureau) / 直接邮寄 (Direct)"
    assert qsl_via_display("") == ""


def test_station_qsl_via_round_trips_through_adif_and_config(tmp_path) -> None:
    import io

    from qslcard.adif import iter_qsos, qso_from_fields, write_adif

    qso = qso_from_fields({"CALL": "JA1AA", "QSO_DATE": "20260911", "QSL_VIA": "BUREAU,DIRECT"})
    buffer = io.StringIO()
    write_adif([qso], buffer)
    reloaded = list(iter_qsos(io.StringIO(buffer.getvalue())))[0]
    assert reloaded.qsl_via == "BUREAU,DIRECT"
