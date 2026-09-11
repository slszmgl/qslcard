"""Tests for imposition geometry and card grouping."""

from __future__ import annotations

import pytest

from qslcard.adif import qso_from_fields
from qslcard.cards import plan_cards, summarize_cards
from qslcard.geometry import (
    CardSpec,
    LayoutError,
    SheetSpec,
    best_layout,
    compute_layout,
)


def rec(**fields: str):
    return qso_from_fields(fields)


def test_default_card_document_size_is_bleed_expanded() -> None:
    card = CardSpec()
    assert card.doc_width_mm == 96.0
    assert card.doc_height_mm == 146.0


def test_a4_default_yields_four_up() -> None:
    layout = best_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=1.0, margin_mm=2.0)
    assert layout.per_sheet == 4
    assert layout.columns == 2
    assert layout.rows == 2
    assert not layout.rotated_card
    assert layout.page_count(4) == 1
    assert layout.page_count(5) == 2
    assert layout.page_count(0) == 0


def test_letter_geometry_reflects_bleed() -> None:
    # 90x140 with 3 mm bleed is 96x146: two rows need 292 mm, more than Letter.
    tight = compute_layout(SheetSpec.from_name("LETTER"), CardSpec(), gap_mm=0.0, margin_mm=0.0)
    assert (tight.columns, tight.rows) == (2, 1)
    assert tight.per_sheet == 2
    assert any("zero margin" in w for w in tight.warnings)
    assert any("zero gap" in w for w in tight.warnings)

    # The classic 3.5x5.5 in card without bleed stacks twice exactly (279.4 mm).
    classic = CardSpec(width_mm=88.9, height_mm=139.7, bleed_mm=0.0)
    exact = compute_layout(SheetSpec.from_name("LETTER"), classic, gap_mm=0.0, margin_mm=0.0)
    assert (exact.columns, exact.rows) == (2, 2)
    assert exact.per_sheet == 4

    # A 2 mm gap cannot fit two 146 mm bleed boxes in 279.4 mm.
    assert (
        compute_layout(SheetSpec.from_name("LETTER"), CardSpec(), gap_mm=2.0, margin_mm=2.0).rows
        == 1
    )


def test_a3_rotation_beats_upright() -> None:
    upright = compute_layout(
        SheetSpec.from_name("A3"), CardSpec(), gap_mm=1.0, margin_mm=2.0, allow_rotation=False
    )
    assert (upright.columns, upright.rows) == (3, 2)
    assert upright.per_sheet == 6

    # Rotating the card fits 2 columns x 4 rows, which the solver prefers.
    auto = compute_layout(SheetSpec.from_name("A3"), CardSpec(), gap_mm=1.0, margin_mm=2.0)
    assert auto.rotated_card
    assert (auto.columns, auto.rows) == (2, 4)
    assert auto.per_sheet == 8


def test_rectangles_and_calibration_lines() -> None:
    layout = compute_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=1.0, margin_mm=2.0)
    first = layout.placements[0]
    top_y = 297.0 - 2.0 - 146.0
    assert layout.bleed_rect(first) == (2.0, top_y, 96.0, 146.0)

    trim = layout.trim_rect(first)
    assert trim == (5.0, top_y + 3.0, 90.0, 140.0)

    # The calibration line sits calibration_mm inside the trim line, i.e. the
    # bleed is excluded from the inset.
    cal = layout.calibration_rect(first)
    assert cal == (2.0 + 3.0 + 3.0, top_y + 3.0 + 3.0, 84.0, 134.0)


def test_placements_fill_the_grid_top_left_first() -> None:
    layout = compute_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=1.0, margin_mm=2.0)
    assert [(p.column, p.row) for p in layout.placements] == [(0, 0), (1, 0), (0, 1), (1, 1)]
    # Row 0 is the top row, so its y is larger than row 1's.
    assert layout.placements[0].y_mm > layout.placements[2].y_mm


def test_mirror_placement_for_duplex() -> None:
    layout = compute_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=1.0, margin_mm=2.0)
    front = layout.placements[0]
    back = layout.mirror_placement(front, axis="x")
    assert back.x_mm == pytest.approx(210.0 - 2.0 - 96.0)
    assert back.y_mm == front.y_mm
    flipped = layout.mirror_placement(front, axis="y")
    assert flipped.y_mm == pytest.approx(297.0 - front.y_mm - 146.0)
    with pytest.raises(ValueError):
        layout.mirror_placement(front, axis="z")


def test_rotation_can_win_on_narrow_sheets() -> None:
    sheet = SheetSpec.custom(150.0, 300.0)
    card = CardSpec(width_mm=90.0, height_mm=140.0, bleed_mm=0.0)
    upright = compute_layout(sheet, card, gap_mm=0.0, margin_mm=0.0, allow_rotation=False)
    assert (upright.columns, upright.rows) == (1, 2)
    assert upright.per_sheet == 2

    rotated = compute_layout(sheet, card, gap_mm=0.0, margin_mm=0.0, allow_rotation=True)
    assert rotated.rotated_card
    assert (rotated.columns, rotated.rows) == (1, 3)
    assert rotated.per_sheet == 3


def test_best_layout_warns_when_it_reduces_margin() -> None:
    layout = best_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=2.0, margin_mm=5.0)
    assert layout.per_sheet == 4
    assert any("margin reduced" in w for w in layout.warnings)
    assert any("gap reduced" in w for w in layout.warnings)


def test_layout_errors() -> None:
    with pytest.raises(LayoutError):
        SheetSpec.from_name("B0")
    with pytest.raises(LayoutError):
        SheetSpec.custom(0.0, 10.0)
    with pytest.raises(LayoutError):
        compute_layout(SheetSpec.custom(50.0, 50.0), CardSpec(), gap_mm=0.0, margin_mm=0.0)
    with pytest.raises(LayoutError):
        compute_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=-1.0, margin_mm=0.0)
    with pytest.raises(LayoutError):
        best_layout(SheetSpec.custom(10.0, 10.0), CardSpec())


def test_describe_is_readable() -> None:
    layout = compute_layout(SheetSpec.from_name("A4"), CardSpec(), gap_mm=1.0, margin_mm=2.0)
    text = layout.describe()
    assert "A4" in text
    assert "90x140" in text
    assert "2x2 = 4" in text


def test_plan_cards_grouping_and_splitting() -> None:
    qsos = [
        rec(
            CALL="JA1ABC",
            QSO_DATE="20260101",
            TIME_ON="0100",
            BAND="20m",
            MODE="SSB",
            COUNTRY="Japan",
        ),
        rec(
            CALL="JA1ABC",
            QSO_DATE="20260102",
            TIME_ON="0200",
            BAND="40m",
            MODE="CW",
            COUNTRY="Japan",
        ),
        rec(
            CALL="JA1ABC",
            QSO_DATE="20260103",
            TIME_ON="0300",
            BAND="15m",
            MODE="FT8",
            COUNTRY="Japan",
        ),
        rec(
            CALL="VK2XY",
            QSO_DATE="20260104",
            TIME_ON="0400",
            BAND="20m",
            MODE="SSB",
            COUNTRY="Australia",
        ),
    ]
    per_qso = plan_cards(qsos, group_by="none")
    assert len(per_qso) == 4
    assert all(len(card) == 1 for card in per_qso)

    # Default max_per_card is 4, so three QSOs with JA1ABC share one card.
    by_call = plan_cards(qsos, group_by="call")
    assert len(by_call) == 2
    assert {card.call for card in by_call} == {"JA1ABC", "VK2XY"}
    assert len(next(c for c in by_call if c.call == "JA1ABC")) == 3

    split = plan_cards(qsos, group_by="call", max_per_card=2)
    assert len(split) == 3
    assert sum(len(card) for card in split) == 4

    by_country = plan_cards(qsos, group_by="country")
    assert len(by_country) == 2
    assert summarize_cards(by_call) == {"cards": 2, "qsos": 4, "calls": 2, "multi_qso_cards": 1}


def test_plan_cards_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        plan_cards([], group_by="band")
    with pytest.raises(ValueError):
        plan_cards([], max_per_card=0)


def test_paper_labels_carry_dimensions_in_millimetres() -> None:
    from qslcard.geometry import paper_choices, paper_label

    assert paper_label("a4") == "A4 (210 x 297 mm)"
    assert paper_label("LETTER") == "LETTER (215.9 x 279.4 mm)"
    labels = paper_choices()
    assert labels == sorted(labels)
    assert all("mm" in label for label in labels)
    assert any(label.startswith("A4 (") for label in labels)
    # Unknown names pass through unchanged rather than losing information.
    assert paper_label("B0") == "B0"
    assert paper_choices()[0].startswith("A3")


def test_parse_paper_accepts_labels_keys_and_junk() -> None:
    from qslcard.geometry import parse_paper

    assert parse_paper("A4 (210 x 297 mm)") == "A4"
    assert parse_paper("a4") == "A4"
    assert parse_paper("  LETTER (215.9 x 279.4 mm) ") == "LETTER"
    assert parse_paper("") == ""
    assert parse_paper("B0") == "B0"
