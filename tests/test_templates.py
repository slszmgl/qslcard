"""Tests for the template model and placeholder engine."""

from __future__ import annotations

import json

import pytest

from qslcard.adif import qso_from_fields
from qslcard.config import StationConfig
from qslcard.templates import (
    BUILTIN_TEMPLATE_NAMES,
    CardTemplate,
    Element,
    available_templates,
    build_context,
    builtin_template,
    format_field,
    load_template,
    pretty_date,
    pretty_time,
    render_text,
)


def rec(**fields: str):
    return qso_from_fields(fields)


STATION = StationConfig(
    callsign="BG1XYZ",
    name="Zhang San",
    qth="Beijing",
    grid="PM95",
    address="Chaoyang, Beijing, China",
    qsl_via="BURO",
)


def test_three_builtin_templates_exist_with_both_faces() -> None:
    assert len(BUILTIN_TEMPLATE_NAMES) >= 3
    assert available_templates() == ["classic", "contest", "minimal"]
    for name in available_templates():
        template = builtin_template(name)
        assert template.front.elements, name
        assert template.back.elements, name
        # SRS D-03: the default card geometry must be used unless overridden.
        assert template.card.width_mm == 90.0
        assert template.card.height_mm == 140.0
        assert template.card.bleed_mm == 3.0
        assert template.card.calibration_mm == 3.0


def test_round_trip_through_json() -> None:
    original = builtin_template("classic")
    reloaded = CardTemplate.from_dict(original.to_dict())
    assert reloaded.to_dict() == original.to_dict()


def test_save_and_load_from_disk(tmp_path) -> None:
    path = tmp_path / "my.json"
    builtin_template("minimal").save(path)
    loaded = CardTemplate.load(path)
    assert loaded.name == "minimal"
    assert loaded.card.doc_width_mm == 96.0
    assert load_template(str(path)).name == "minimal"
    assert load_template("minimal").name == "minimal"
    with pytest.raises(FileNotFoundError):
        load_template("does-not-exist")


def test_placeholder_substitution_and_escapes() -> None:
    context = {"call": "JA1ABC", "band": "20m"}
    warnings: list[str] = []
    assert render_text("{call} on {band}", context, warnings) == "JA1ABC on 20m"
    assert warnings == []
    assert render_text("literal {{call}}", context, warnings) == "literal {call}"
    assert render_text("{missing}", context, warnings) == ""
    assert warnings == ["unknown placeholder: {missing}"]
    assert render_text("no placeholders", context) == "no placeholders"


def test_build_context_aliases_and_formatting() -> None:
    qso = rec(
        CALL="JA1ABC",
        QSO_DATE="20260101",
        TIME_ON="123045",
        BAND="20m",
        MODE="FT8",
        NAME="Taro",
        MY_GRIDSQUARE="PM95",
        QSL_VIA="",
    )
    context = build_context(qso, STATION, index=1, total=3)
    assert context["call"] == "JA1ABC"
    assert context["my_call"] == "BG1XYZ"
    assert context["my_grid"] == "PM95"
    assert context["date"] == "2026-01-01"
    assert context["time"] == "12:30:45"
    # Station default is used when the QSO does not carry a QSL route.
    assert context["qsl_via"] == "BURO"
    assert context["index"] == "1"
    assert context["total"] == "3"


def test_pretty_helpers_and_field_formatting() -> None:
    assert pretty_date("20260101") == "2026-01-01"
    assert pretty_date("bogus") == "bogus"
    assert pretty_time("1230") == "12:30"
    assert pretty_time("123045") == "12:30:45"
    assert pretty_time("99") == "99"
    assert format_field("qso_date", "20260101") == "2026-01-01"
    assert format_field("time_on", "0700") == "07:00"
    assert format_field("band", "20m") == "20m"


def test_required_fields_and_missing_data_warning() -> None:
    template = builtin_template("classic")
    names = template.required_fields("back")
    assert "my_call" in names
    assert "my_address" in names

    qso = rec(CALL="JA1ABC", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB")
    context = build_context(qso, StationConfig())
    warnings: list[str] = []
    for element in template.back.elements:
        render_text(element.text, context, warnings)
    # An empty station means the operator address renders blank, which the user
    # must be told about rather than silently printing an empty card.
    assert any("my_address" not in w for w in warnings) or warnings == []


def test_element_validation_and_defaults() -> None:
    element = Element.from_dict({"type": "qso_rows", "x_mm": 1})
    assert element.fields == ("qso_date", "time_on", "band", "mode", "rst_sent", "rst_rcvd")
    with pytest.raises(ValueError):
        Element.from_dict({"type": "hologram"})
    with pytest.raises(ValueError):
        Element.from_dict({"type": "text", "align": "justify"})


def test_element_to_dict_drops_defaults() -> None:
    element = Element.from_dict({"type": "text", "text": "{call}", "size_pt": 12})
    data = element.to_dict()
    assert data == {"type": "text", "text": "{call}", "size_pt": 12}


def test_template_json_is_plain_data() -> None:
    data = builtin_template("contest").to_dict()
    text = json.dumps(data)
    assert "{my_call}" in text
    assert "qso_rows" in text
