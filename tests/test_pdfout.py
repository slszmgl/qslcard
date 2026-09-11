"""Tests for the PDF renderer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qslcard.adif import qso_from_fields
from qslcard.cards import plan_cards
from qslcard.config import StationConfig
from qslcard.pdfout import CardRenderer, RenderOptions, find_default_font, render_single_cards
from qslcard.templates import builtin_template

STATION = StationConfig(
    callsign="BG1XYZ",
    name="Zhang San",
    qth="Beijing",
    grid="PM95",
    address="Chaoyang, Beijing, China",
    qsl_via="BURO",
)


def make_cards(count: int) -> list:
    qsos = [
        qso_from_fields(
            {
                "CALL": f"JA1{chr(65 + i % 26)}{chr(65 + i % 7)}",
                "QSO_DATE": "20260911",
                "TIME_ON": f"{1000 + i:04d}",
                "BAND": "20m",
                "MODE": "FT8",
                "RST_SENT": "-10",
                "RST_RCVD": "-12",
                "NAME": "Taro",
                "QTH": "Tokyo",
            }
        )
        for i in range(count)
    ]
    return plan_cards(qsos, group_by="call", max_per_card=4)


def core_only_options(**kwargs: object) -> RenderOptions:
    # font_path="" forces the built-in core font, which keeps the text stream
    # greppable and makes the test independent of host fonts.
    base: dict[str, object] = {
        "template": builtin_template("classic"),
        "station": STATION,
        "font_path": "",
        "compress": False,
    }
    base.update(kwargs)
    return RenderOptions(**base)  # type: ignore[arg-type]


def test_render_sheet_pdf_duplex(tmp_path) -> None:
    renderer = CardRenderer(core_only_options())
    result = renderer.render(make_cards(5), tmp_path / "cards.pdf")
    assert result.cards == 5
    assert result.sheets == 2
    assert result.pages == 4
    assert result.bytes_written > 1000
    assert "A4" in result.layout
    data = Path(result.path).read_bytes()
    assert data.startswith(b"%PDF")
    assert b"BG1XYZ" in data
    assert b"/MediaBox" in data


def test_render_without_duplex_halves_the_pages(tmp_path) -> None:
    renderer = CardRenderer(core_only_options(duplex=False))
    result = renderer.render(make_cards(4), tmp_path / "single.pdf")
    assert result.sheets == 1
    assert result.pages == 1


def test_guides_can_be_disabled(tmp_path) -> None:
    renderer = CardRenderer(core_only_options(crop_marks=False, calibration_lines=False))
    result = renderer.render(make_cards(1), tmp_path / "noguides.pdf")
    assert result.pages == 2
    assert Path(result.path).exists()


def test_single_card_pdf_has_one_page_per_card(tmp_path) -> None:
    result = render_single_cards(
        make_cards(3), core_only_options(duplex=False), tmp_path / "one.pdf"
    )
    assert result.pages == 3
    assert result.cards == 3


def test_multi_qso_card_renders_rows(tmp_path) -> None:
    qsos = [
        qso_from_fields(
            {
                "CALL": "JA1ABC",
                "QSO_DATE": "20260911",
                "TIME_ON": f"{1000 + i:04d}",
                "BAND": band,
                "MODE": "SSB",
                "RST_SENT": "59",
                "RST_RCVD": "59",
            }
        )
        for i, band in enumerate(("20m", "40m", "15m"))
    ]
    cards = plan_cards(qsos, group_by="call", max_per_card=4)
    assert len(cards) == 1
    assert len(cards[0]) == 3
    renderer = CardRenderer(core_only_options())
    result = renderer.render(cards, tmp_path / "multi.pdf")
    assert result.cards == 1
    data = Path(result.path).read_bytes()
    assert b"2026-09-11" in data  # the formatted date row reached the PDF


def test_cjk_text_is_rendered_or_downgraded_cleanly(tmp_path) -> None:
    # Auto-detect a system Unicode font; without one the renderer must degrade
    # to question marks and report a warning instead of raising.
    station = StationConfig(callsign="BG1XYZ", name="\u5f20\u4e09", qth="\u5317\u4eac")
    options = RenderOptions(template=builtin_template("classic"), station=station, compress=False)
    renderer = CardRenderer(options)
    result = renderer.render(make_cards(1), tmp_path / "cjk.pdf")
    assert result.pages == 2
    assert Path(result.path).exists()
    if renderer._font_path is None:
        assert any("Unicode font" in w for w in result.warnings)


def test_core_font_reports_replacement_warning(tmp_path) -> None:
    station = StationConfig(callsign="BG1XYZ", qth="\u5317\u4eac")
    renderer = CardRenderer(core_only_options(station=station))
    result = renderer.render(make_cards(1), tmp_path / "core.pdf")
    assert any("latin-1" in w for w in result.warnings)


def test_custom_font_detection_is_optional() -> None:
    found = find_default_font()
    assert found is None or os.path.isfile(found)


def test_long_values_shrink_instead_of_overflowing(tmp_path) -> None:
    qso = qso_from_fields(
        {
            "CALL": "ZZ9ZZZZZZZZZZZZ",
            "QSO_DATE": "20260911",
            "TIME_ON": "1200",
            "BAND": "20m",
        }
    )
    cards = plan_cards([qso], group_by="call")
    result = CardRenderer(core_only_options()).render(cards, tmp_path / "long.pdf")
    assert result.cards == 1


# --------------------------------------------------------------------------
# Commercial print features (SRS CAL-007/008, PRN-Q-007)
# --------------------------------------------------------------------------


def make_png(path, size=(600, 900), colour=(10, 60, 145)):
    from PIL import Image

    Image.new("RGB", size, colour).save(path)
    return path


def test_cmyk_output_uses_device_cmyk_operators(tmp_path) -> None:
    from qslcard.config import PrintConfig

    options = core_only_options(print_config=PrintConfig(color="CMYK"))
    result = CardRenderer(options).render(make_cards(2), tmp_path / "cmyk.pdf")
    assert result.color_mode == "CMYK"
    data = Path(result.path).read_bytes()
    assert b" k" in data  # device CMYK fill operator
    assert b" K" in data  # device CMYK stroke operator
    assert b" rg" not in data and b" RG" not in data


def test_rgb_output_has_no_cmyk_operators(tmp_path) -> None:
    from qslcard.config import PrintConfig

    options = core_only_options(print_config=PrintConfig(color="RGB"))
    result = CardRenderer(options).render(make_cards(2), tmp_path / "rgb.pdf")
    data = Path(result.path).read_bytes()
    assert b" rg" in data
    assert b" k" not in data


def test_pdfx_writes_trim_and_bleed_boxes(tmp_path) -> None:
    from qslcard.config import PrintConfig

    options = core_only_options(print_config=PrintConfig(pdf_type="pdfx"))
    result = CardRenderer(options).render(make_cards(1), tmp_path / "pdfx.pdf")
    assert result.pdf_type == "pdfx"
    assert result.page_boxes == result.pages == 2
    data = Path(result.path).read_bytes()
    assert b"/TrimBox [" in data
    assert b"/BleedBox [" in data
    assert b"/ArtBox [" in data
    # Boxes stay inside the MediaBox of A4.
    assert b"/TrimBox [14.17 14.17 581.1 827.71]" in data


def test_plain_pdf_has_no_page_boxes(tmp_path) -> None:
    result = CardRenderer(core_only_options()).render(make_cards(1), tmp_path / "plain.pdf")
    assert result.page_boxes == 0
    assert b"/TrimBox" not in Path(result.path).read_bytes()


def test_registration_marks_and_color_bars_change_output(tmp_path) -> None:
    from qslcard.config import PrintConfig

    plain = CardRenderer(core_only_options(print_config=PrintConfig())).render(
        make_cards(1), tmp_path / "noplates.pdf"
    )
    marked = CardRenderer(
        core_only_options(
            print_config=PrintConfig(registration_marks=True, color_bars=True, margin_mm=4.0)
        )
    ).render(make_cards(1), tmp_path / "plates.pdf")
    assert marked.bytes_written > plain.bytes_written


def test_background_image_is_embedded_full_bleed(tmp_path) -> None:
    from qslcard.templates import builtin_template

    image = make_png(tmp_path / "bg.png", (1200, 1800))
    template = builtin_template("classic")
    template.front.background_image = str(image)
    options = RenderOptions(template=template, station=STATION, font_path="", compress=False)
    result = CardRenderer(options).render(make_cards(1), tmp_path / "bg.pdf")
    data = Path(result.path).read_bytes()
    assert b"/Subtype /Image" in data
    assert not [w for w in result.warnings if "DPI" in w]


def test_low_resolution_background_image_is_reported(tmp_path) -> None:
    from qslcard.templates import builtin_template

    image = make_png(tmp_path / "tiny.png", (60, 90))
    template = builtin_template("classic")
    template.front.background_image = str(image)
    options = RenderOptions(template=template, station=STATION, font_path="", compress=False)
    result = CardRenderer(options).render(make_cards(1), tmp_path / "tiny.pdf")
    assert any("DPI" in w for w in result.warnings)


def test_image_element_renders_and_reports_missing_file(tmp_path) -> None:
    from qslcard.templates import Element, builtin_template

    image = make_png(tmp_path / "logo.png", (600, 600))
    template = builtin_template("minimal")
    template.front.elements.append(
        Element.from_dict(
            {"type": "image", "image": str(image), "x_mm": 10, "y_mm": 10, "w_mm": 30, "h_mm": 30}
        )
    )
    template.front.elements.append(
        Element.from_dict(
            {
                "type": "image",
                "image": str(tmp_path / "gone.png"),
                "x_mm": 10,
                "y_mm": 60,
                "w_mm": 20,
                "h_mm": 20,
            }
        )
    )
    options = RenderOptions(template=template, station=STATION, font_path="", compress=False)
    result = CardRenderer(options).render(make_cards(1), tmp_path / "logo.pdf")
    assert b"/Subtype /Image" in Path(result.path).read_bytes()
    assert any("不存在" in w for w in result.warnings)


def test_print_presets_and_descriptions() -> None:
    from qslcard.config import (
        PrintConfig,
        apply_print_preset,
        describe_preset_changes,
        describe_print_settings,
    )

    home = PrintConfig()
    commercial = apply_print_preset(home, "commercial")
    assert commercial.pdf_type == "pdfx"
    assert commercial.color == "CMYK"
    assert commercial.registration_marks and commercial.color_bars
    assert not commercial.calibration_lines  # press output should not carry guides
    assert home.calibration_lines
    rows = describe_print_settings(home)
    assert len(rows) == 10
    assert all(len(row) == 3 and row[2] for row in rows)
    notes = describe_preset_changes(home, "commercial")
    assert any("CMYK" in n for n in notes)
    assert any("成品框" in n for n in notes)
    assert describe_preset_changes(home, "home") == ["与当前设置一致，无需调整。"]
    with pytest.raises(KeyError):
        apply_print_preset(home, "nope")
