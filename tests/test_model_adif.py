"""Tests for the QSO model and the ADIF reader/writer."""

from __future__ import annotations

import io

from qslcard.adif import (
    dumps,
    iter_qsos,
    iter_records,
    load_adif,
    qso_from_fields,
    write_adif,
)
from qslcard.model import QSO, business_key, is_affirmative, merge_qso, normalize_qso

SIMPLE = "<CALL:5>BG1XY<QSO_DATE:8>20260911<TIME_ON:4>1230<BAND:3>20m<MODE:3>SSB<EOR>"


def test_parse_single_record() -> None:
    records = list(iter_records(io.StringIO(SIMPLE)))
    assert records == [
        {"CALL": "BG1XY", "QSO_DATE": "20260911", "TIME_ON": "1230", "BAND": "20m", "MODE": "SSB"}
    ]


def test_parse_tolerates_header_whitespace_case_and_bom() -> None:
    text = (
        "\ufeff<ADIF_VER:5>3.1.7\n<PROGRAMID:4>test\n<EOH>\n"
        "  <call:5>bg1xy<qso_date:8>20260911<time_on:4>1230\n"
        "  <band:3>20M<mode:3>ssb<rst_sent:2>59<rst_rcvd:2>57<EOR>\n<EOR>\n"
    )
    records = list(iter_records(io.StringIO(text)))
    assert len(records) == 1
    qso = qso_from_fields(records[0])
    assert qso.call == "BG1XY"
    assert qso.band == "20m"
    assert qso.mode == "SSB"
    assert qso.rst_sent == "59"


def test_value_may_contain_angle_bracket_and_spaces() -> None:
    text = "<COMMENT:11>a < b c 1 2<EOR>"
    records = list(iter_records(io.StringIO(text)))
    assert records[0]["COMMENT"] == "a < b c 1 2"


def test_type_suffix_is_ignored() -> None:
    records = list(iter_records(io.StringIO("<CALL:5:S>BG1XY<FREQ:6:N>14.074<EOR>")))
    assert records[0] == {"CALL": "BG1XY", "FREQ": "14.074"}


def test_unknown_tags_are_preserved_in_extra() -> None:
    qso = qso_from_fields({"CALL": "BG1XY", "MY_ANTENNA": "Dipole", "QSO_DATE": "20260911"})
    assert qso.extra == {"MY_ANTENNA": "Dipole"}


def test_round_trip_through_adif() -> None:
    original = qso_from_fields(
        {
            "CALL": "JA1ABC",
            "QSO_DATE": "20260101",
            "TIME_ON": "010203",
            "BAND": "15m",
            "MODE": "FT8",
            "RST_SENT": "-10",
            "RST_RCVD": "-12",
            "NAME": "Taro",
            "QSL_VIA": "Bureau",
            "CUSTOM_FIELD": "kept",
        }
    )
    text = dumps([original])
    reloaded = load_adif(io.StringIO(text))
    assert len(reloaded) == 1
    again = reloaded[0]
    assert again.call == "JA1ABC"
    assert again.time_on == "010203"
    assert again.name == "Taro"
    assert again.extra.get("CUSTOM_FIELD") == "kept"


def test_write_returns_record_count_and_has_header() -> None:
    buffer = io.StringIO()
    count = write_adif([QSO(call="A1AA", qso_date="20260101")], buffer)
    text = buffer.getvalue()
    assert count == 1
    assert text.startswith("<ADIF_VER:5>3.1.7")
    assert "<EOH>" in text
    assert text.rstrip().endswith("<EOR>")


def test_business_key_ignores_seconds_and_band_case() -> None:
    a = qso_from_fields(
        {"CALL": "bg1xy", "QSO_DATE": "20260911", "TIME_ON": "1230", "BAND": "20M", "MODE": "ssb"}
    )
    b = qso_from_fields(
        {"CALL": "BG1XY", "QSO_DATE": "20260911", "TIME_ON": "123045", "BAND": "20m", "MODE": "SSB"}
    )
    assert business_key(a) == business_key(b)


def test_normalize_time_forms() -> None:
    assert normalize_qso(QSO(time_on="9")).time_on == "0009"
    assert normalize_qso(QSO(time_on="12")).time_on == "1200"
    assert normalize_qso(QSO(time_on="123")).time_on == "0123"
    assert normalize_qso(QSO(time_on="12:30")).time_on == "1230"


def test_merge_never_loses_confirmation() -> None:
    base = qso_from_fields(
        {"CALL": "BG1XY", "QSO_DATE": "20260911", "TIME_ON": "1230", "LOTW_QSL_RCVD": "Y"}
    )
    incoming = qso_from_fields(
        {
            "CALL": "BG1XY",
            "QSO_DATE": "20260911",
            "TIME_ON": "1230",
            "LOTW_QSL_RCVD": "",
            "NAME": "Zhang",
        }
    )
    merged = merge_qso(base, incoming)
    assert merged.lotw_qsl_rcvd == "Y"
    assert merged.name == "Zhang"


def test_is_confirmed_variants() -> None:
    assert QSO(lotw_qsl_rcvd="Y").is_confirmed()
    assert QSO(eqsl_qsl_rcvd="y").is_confirmed()
    assert QSO(qsl_rcvd="Y").is_confirmed()
    assert not QSO(qsl_rcvd="N").is_confirmed()
    assert is_affirmative("yes")


def test_iter_qsos_from_path(tmp_path) -> None:
    path = tmp_path / "sample.adi"
    path.write_text(SIMPLE + "\n" + SIMPLE.replace("1230", "1300"), encoding="utf-8")
    qsos = list(iter_qsos(path))
    assert [q.time_on for q in qsos] == ["1230", "1300"]


def test_large_stream_is_parsed_incrementally() -> None:
    record = "<CALL:5>BG1XY<QSO_DATE:8>20260911<TIME_ON:4>1230<BAND:3>20m<MODE:3>SSB<EOR>\n"
    text = "<EOH>\n" + record * 5000
    qsos = list(iter_qsos(io.StringIO(text)))
    assert len(qsos) == 5000


def test_adx_xml_form() -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<ADX><HEADER><ADIF_VER>3.1.7</ADIF_VER></HEADER>"
        "<RECORDS><RECORD><CALL>BG1XY</CALL><QSO_DATE>20260911</QSO_DATE>"
        "<TIME_ON>1230</TIME_ON><BAND>20m</BAND><MODE>SSB</MODE></RECORD></RECORDS></ADX>"
    )
    qsos = list(iter_qsos(io.StringIO(xml)))
    assert len(qsos) == 1
    assert qsos[0].call == "BG1XY"


def test_bad_length_prefix_is_repaired_and_reported() -> None:
    # Real logs frequently carry a wrong length prefix; the delimiters must win
    # and the repair must be reported rather than silently merging records.
    text = "<CALL:5>BG1XY<QSL_VIA:5>BURO<EOR><CALL:5>JA1AA<QSO_DATE:8>20260101<EOR>"
    errors: list[dict] = []
    records = list(iter_records(io.StringIO(text), errors=errors))
    assert len(records) == 2
    assert records[0]["QSL_VIA"] == "BURO"
    assert records[1]["CALL"] == "JA1AA"
    assert records[1]["QSO_DATE"] == "20260101"
    assert len(errors) == 1
    assert errors[0]["tag"] == "QSL_VIA"
    assert errors[0]["declared"] == 5


def test_overlong_value_does_not_swallow_the_next_record() -> None:
    text = "<PROGRAMID:9>qslcard\n<CALL:5>JA1AA<QSO_DATE:8>20260101<EOR>"
    errors: list[dict] = []
    qsos = list(iter_qsos(io.StringIO(text), errors))
    assert len(qsos) == 1
    assert qsos[0].call == "JA1AA"
    assert errors


def test_whitespace_between_fields_is_not_an_error() -> None:
    errors: list[dict] = []
    records = list(
        iter_records(io.StringIO("<CALL:5>BG1XY \n <QSO_DATE:8>20260101<EOR>"), errors=errors)
    )
    assert records[0]["CALL"] == "BG1XY"
    assert records[0]["QSO_DATE"] == "20260101"
    assert errors == []
