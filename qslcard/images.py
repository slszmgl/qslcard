"""Image asset helpers shared by the renderer and the designer (SRS TPL-I-001).

All Pillow use is confined here and imported lazily, so listing templates or
importing the package never pays for image decoding.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = [
    "MIN_PRINT_DPI",
    "cover_crop",
    "fit_rect",
    "image_effective_dpi",
    "image_size_px",
    "image_warnings",
]

#: Below this effective resolution a printed bitmap starts to look soft.
MIN_PRINT_DPI = 300

_SUPPORTED = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")


def is_supported_image(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in _SUPPORTED


def image_size_px(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """Pixel dimensions, or None when the file cannot be read."""
    try:
        from PIL import Image

        with Image.open(path) as handle:
            return int(handle.width), int(handle.height)
    except Exception:  # noqa: BLE001 - unreadable assets must not break a render
        return None


def image_effective_dpi(
    path: str | os.PathLike[str],
    width_mm: float,
    height_mm: float,
) -> float | None:
    """Effective print resolution of an image placed at the given size."""
    size = image_size_px(path)
    if size is None or width_mm <= 0 or height_mm <= 0:
        return None
    pixels_x, pixels_y = size
    inches_x = width_mm / 25.4
    inches_y = height_mm / 25.4
    return min(pixels_x / inches_x, pixels_y / inches_y)


def image_warnings(
    path: str | os.PathLike[str],
    width_mm: float,
    height_mm: float,
    min_dpi: int = MIN_PRINT_DPI,
) -> list[str]:
    """Human-readable warnings for one placed image."""
    name = Path(path).name
    if not Path(path).is_file():
        return [f"图片不存在：{path}"]
    if not is_supported_image(path):
        return [f"图片格式可能不受支持：{name}"]
    dpi = image_effective_dpi(path, width_mm, height_mm)
    if dpi is None:
        return [f"无法读取图片尺寸：{name}"]
    if dpi < min_dpi:
        return [
            f"图片 {name} 的有效分辨率约 {dpi:.0f} DPI，低于 {min_dpi} DPI，"
            "印出来可能发虚；请换用更大的原图或缩小放置尺寸。"
        ]
    return []


def fit_rect(
    source_ratio: float,
    box_w: float,
    box_h: float,
    fit: str = "cover",
) -> tuple[float, float, float, float]:
    """Largest rectangle of the source ratio inside a box (contain or stretch)."""
    if source_ratio <= 0 or box_w <= 0 or box_h <= 0:
        return (0.0, 0.0, box_w, box_h)
    if fit == "stretch":
        return (0.0, 0.0, box_w, box_h)
    box_ratio = box_w / box_h
    if fit == "contain":
        if source_ratio > box_ratio:
            width = box_w
            height = box_w / source_ratio
        else:
            height = box_h
            width = box_h * source_ratio
        return ((box_w - width) / 2, (box_h - height) / 2, width, height)
    return (0.0, 0.0, box_w, box_h)


def cover_crop(
    path: str | os.PathLike[str],
    target_ratio: float,
    cache_dir: str | os.PathLike[str],
    *,
    suffix: str = ".png",
) -> str:
    """Return a centre-cropped copy of the image matching the target ratio.

    Cropping once per document (the result is cached on disk by source identity
    and ratio) keeps a thousand-card run from decoding the background image a
    thousand times.
    """
    from PIL import Image

    source = Path(path)
    stamp = source.stat().st_mtime_ns if source.exists() else 0
    key = hashlib.sha256(f"{source}|{stamp}|{target_ratio:.6f}".encode()).hexdigest()[:16]
    target = Path(cache_dir) / f"cover-{key}{suffix}"
    if target.exists():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as handle:
        image = handle.convert("RGBA")
        width, height = image.size
        source_ratio = width / height if height else 1.0
        if abs(source_ratio - target_ratio) < 1e-6:
            cropped = image
        elif source_ratio > target_ratio:
            new_width = max(int(round(height * target_ratio)), 1)
            left = (width - new_width) // 2
            cropped = image.crop((left, 0, left + new_width, height))
        else:
            new_height = max(int(round(width / target_ratio)), 1)
            top = (height - new_height) // 2
            cropped = image.crop((0, top, width, top + new_height))
        cropped.save(target, "PNG")
    return str(target)
