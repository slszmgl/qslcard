"""Tests for the designer's editing primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from qslcard.editor import (
    HANDLES,
    MIN_ELEMENT_MM,
    add_element,
    bounds,
    element_rect,
    full_bleed_rect,
    hit_test,
    image_report,
    move_element,
    resize_element,
    snap,
)
from qslcard.geometry import CardSpec
from qslcard.templates import CardFace, Element

CARD = CardSpec()  # 90 x 140, 3 mm bleed


def make_png(path: Path, size=(600, 600)) -> Path:
    from PIL import Image

    Image.new("RGB", size, (10, 60, 145)).save(path)
    return path


def test_snap_and_bounds() -> None:
    assert snap(12.3, 0.5) == 12.5
    assert snap(12.26, 0.5) == 12.5
    assert snap(1.234, 0.0) == 1.234
    assert bounds(CARD) == (-3.0, -3.0, 96.0, 146.0)
    assert bounds(CARD, allow_bleed=False) == (0.0, 0.0, 90.0, 140.0)


def test_full_bleed_rect_matches_the_bleed_box() -> None:
    assert full_bleed_rect(CARD) == (-3.0, -3.0, 96.0, 146.0)


def test_add_element_creates_useful_defaults() -> None:
    face = CardFace()
    text = add_element(face, "text", CARD, text="{call}")
    assert text.type == "text" and text.text == "{call}"
    assert 0 <= text.x_mm <= CARD.width_mm
    image = add_element(face, "image", CARD)
    assert image.type == "image" and image.w_mm > 0 and image.h_mm > 0
    assert add_element(face, "rect", CARD).type == "rect"
    assert add_element(face, "line", CARD).type == "line"
    assert add_element(face, "qso_rows", CARD).type == "qso_rows"
    assert len(face.elements) == 5
    with pytest.raises(ValueError):
        add_element(face, "hologram", CARD)


def test_add_image_keeps_source_aspect_ratio(tmp_path) -> None:
    source = make_png(tmp_path / "wide.png", (800, 400))
    image = add_element(CardFace(), "image", CARD, image=str(source))
    assert image.h_mm == pytest.approx(image.w_mm * 400 / 800, rel=0.01)


def test_move_element_clamps_to_the_bleed_box() -> None:
    element = Element(type="rect", x_mm=10, y_mm=10, w_mm=20, h_mm=20)
    move_element(element, 1000, 1000, CARD, step=0.5)
    x, y, w, h = element_rect(element, CARD)
    assert x + w == pytest.approx(93.0)  # bleed box right edge
    assert y + h == pytest.approx(143.0)
    move_element(element, -1000, -1000, CARD, step=0.5)
    x, y, _, _ = element_rect(element, CARD)
    assert x == pytest.approx(-3.0)
    assert y == pytest.approx(-3.0)


def test_move_element_without_bleed_stays_on_the_card() -> None:
    element = Element(type="rect", x_mm=10, y_mm=10, w_mm=20, h_mm=20)
    move_element(element, -1000, -1000, CARD, allow_bleed=False)
    assert element_rect(element, CARD)[:2] == (0.0, 0.0)


def test_move_line_updates_both_endpoints() -> None:
    line = Element(type="line", x1_mm=0, y1_mm=0, x2_mm=40, y2_mm=0)
    move_element(line, 5, 7, CARD, allow_bleed=False)
    assert (line.x1_mm, line.y1_mm, line.x2_mm, line.y2_mm) == (5.0, 7.0, 45.0, 7.0)


@pytest.mark.parametrize("handle", HANDLES)
def test_resize_handles_move_the_expected_edge(handle: str) -> None:
    element = Element(type="rect", x_mm=20, y_mm=20, w_mm=40, h_mm=40)
    before = element_rect(element, CARD)
    resize_element(element, handle, 5.0, 5.0, CARD, step=0.0)
    after = element_rect(element, CARD)
    if "w" in handle:
        assert after[0] == pytest.approx(before[0] + 5.0)
    if "e" in handle:
        assert after[0] + after[2] == pytest.approx(before[0] + before[2] + 5.0)
    if "s" in handle:
        assert after[1] == pytest.approx(before[1] + 5.0)
    if "n" in handle:
        assert after[1] + after[3] == pytest.approx(before[1] + before[3] + 5.0)


def test_resize_enforces_minimum_size() -> None:
    element = Element(type="rect", x_mm=20, y_mm=20, w_mm=30, h_mm=30)
    # Drag the south-east handle past both opposite edges: each axis collapses to
    # the minimum instead of inverting.
    resize_element(element, "se", -1000.0, 1000.0, CARD)
    x, y, w, h = element_rect(element, CARD)
    assert w == pytest.approx(MIN_ELEMENT_MM)
    assert h == pytest.approx(MIN_ELEMENT_MM)
    assert x == pytest.approx(20.0)  # west edge is the anchor
    assert y + h == pytest.approx(50.0)  # north edge is the anchor


def test_resize_clamps_to_the_bleed_bounds() -> None:
    element = Element(type="rect", x_mm=20, y_mm=20, w_mm=30, h_mm=30)
    resize_element(element, "se", 1000.0, -1000.0, CARD)
    x, y, w, h = element_rect(element, CARD)
    assert x + w == pytest.approx(93.0)  # east edge pinned to the bleed box
    assert y == pytest.approx(-3.0)  # south edge pinned to the bleed box
    assert w >= MIN_ELEMENT_MM
    assert h >= MIN_ELEMENT_MM
    with pytest.raises(ValueError):
        resize_element(element, "middle", 1.0, 1.0, CARD)


def test_resize_line_moves_the_nearer_endpoint() -> None:
    line = Element(type="line", x1_mm=10, y1_mm=20, x2_mm=50, y2_mm=20)
    resize_element(line, "e", 10.0, 0.0, CARD, step=0.0)
    assert (line.x2_mm, line.y2_mm) == (60.0, 20.0)
    assert (line.x1_mm, line.y1_mm) == (10.0, 20.0)
    resize_element(line, "w", -5.0, 0.0, CARD, step=0.0)
    assert (line.x1_mm, line.y1_mm) == (5.0, 20.0)


def test_hit_test_returns_the_topmost_element() -> None:
    face = CardFace()
    lower = Element(type="rect", x_mm=10, y_mm=10, w_mm=40, h_mm=40, fill="#111111")
    upper = Element(type="rect", x_mm=20, y_mm=20, w_mm=40, h_mm=40, fill="#222222")
    face.elements.extend([lower, upper])
    assert hit_test(face, CARD, 25, 25) is upper
    assert hit_test(face, CARD, 12, 12) is lower
    assert hit_test(face, CARD, 200, 200) is None
    assert hit_test(face, CARD, 20.5, 20.5) is upper


def test_image_report_reports_dpi_and_warnings(tmp_path) -> None:
    good = make_png(tmp_path / "good.png", (1200, 1800))
    report = image_report(good, 90.0, 135.0, 300)
    assert report.pixels == (1200, 1800)
    assert report.dpi == pytest.approx(338.7, abs=0.2)
    assert report.ok

    poor = make_png(tmp_path / "poor.png", (100, 150))
    poor_report = image_report(poor, 90.0, 135.0, 300)
    assert not poor_report.ok
    assert "DPI" in poor_report.warnings[0]

    missing = image_report(tmp_path / "none.png", 10.0, 10.0)
    assert not missing.ok
