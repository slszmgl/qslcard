"""Tests for the image asset helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from qslcard.images import (
    cover_crop,
    fit_rect,
    image_effective_dpi,
    image_size_px,
    image_warnings,
    is_supported_image,
)


def make_png(
    path: Path, size: tuple[int, int] = (600, 900), colour: tuple[int, int, int] = (10, 60, 145)
) -> Path:
    from PIL import Image

    Image.new("RGB", size, colour).save(path)
    return path


def test_image_size_and_effective_dpi(tmp_path) -> None:
    path = make_png(tmp_path / "a.png", (600, 900))
    assert image_size_px(path) == (600, 900)
    # 600 px across 50.8 mm (2 in) is 300 DPI.
    assert image_effective_dpi(path, 50.8, 76.2) == pytest.approx(300.0)
    assert image_effective_dpi(path, 100.0, 150.0) == pytest.approx(152.4, abs=0.1)


def test_image_warnings_for_low_resolution(tmp_path) -> None:
    small = make_png(tmp_path / "small.png", (60, 90))
    notes = image_warnings(small, 90.0, 135.0, 300)
    assert notes and "DPI" in notes[0]
    big = make_png(tmp_path / "big.png", (1200, 1800))
    assert image_warnings(big, 90.0, 135.0, 300) == []


def test_image_warnings_for_missing_or_odd_files(tmp_path) -> None:
    missing = tmp_path / "nope.png"
    assert "不存在" in image_warnings(missing, 10, 10)[0]
    text = tmp_path / "notes.txt"
    text.write_text("not an image", encoding="utf-8")
    assert "格式" in image_warnings(text, 10, 10)[0]
    assert not is_supported_image(text)
    assert is_supported_image("a.JPG")


def test_cover_crop_matches_target_ratio_and_is_cached(tmp_path) -> None:
    source = make_png(tmp_path / "wide.png", (800, 400))
    cache = tmp_path / "cache"
    cropped = cover_crop(source, 90 / 140, cache)
    assert Path(cropped).is_file()
    with __import__("PIL.Image", fromlist=["Image"]).open(cropped) as image:
        assert image.width / image.height == pytest.approx(90 / 140, rel=1e-2)
    # Second call returns the cached file rather than re-cropping.
    assert cover_crop(source, 90 / 140, cache) == cropped


def test_fit_rect_modes() -> None:
    assert fit_rect(1.0, 100.0, 50.0, "stretch") == (0.0, 0.0, 100.0, 50.0)
    contain = fit_rect(1.0, 100.0, 50.0, "contain")
    assert contain[2] == pytest.approx(50.0)
    assert contain[3] == pytest.approx(50.0)
    assert contain[0] == pytest.approx(25.0)
    assert fit_rect(0.0, 10.0, 10.0, "contain") == (0.0, 0.0, 10.0, 10.0)
