"""Editing primitives for the card designer (SRS 7, TPL-I-001).

Deliberately free of Tk: every operation the designer performs on an element is
a pure function here, so the behaviour is unit tested without a display and the
GUI stays a thin layer on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geometry import CardSpec
from .images import image_effective_dpi, image_size_px, image_warnings
from .templates import CardFace, Element

__all__ = [
    "HANDLES",
    "MIN_ELEMENT_MM",
    "PLACEHOLDER_PRESETS",
    "add_element",
    "bounds",
    "clamp",
    "element_rect",
    "full_bleed_rect",
    "hit_test",
    "image_report",
    "move_element",
    "resize_element",
    "set_rect",
    "snap",
]

#: Resize handles, ordered clockwise from the top-left corner.
HANDLES: tuple[str, ...] = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

#: Nothing may be resized smaller than this, so it stays reachable.
MIN_ELEMENT_MM = 4.0

#: Handy text snippets offered by the designer.
PLACEHOLDER_PRESETS: tuple[tuple[str, str], ...] = (
    ("对方呼号", "{call}"),
    ("日期", "{date}"),
    ("UTC 时间", "{time}"),
    ("波段/模式", "{band} {mode}"),
    ("RST", "{rst_sent}/{rst_rcvd}"),
    ("我的呼号", "{my_call}"),
    ("对方姓名", "{name}"),
    ("对方 QTH", "{qth}"),
    ("网格", "{grid}"),
    ("QSL 经手", "{qsl_via}"),
)


def clamp(value: float, low: float, high: float) -> float:
    if high < low:
        return low
    return max(low, min(high, value))


def snap(value: float, step: float = 0.5) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 3)


def bounds(card: CardSpec, *, allow_bleed: bool = True) -> tuple[float, float, float, float]:
    """Editable area: the bleed box, or the trim box when bleed is disallowed."""
    if allow_bleed:
        bleed = card.bleed_mm
        return (-bleed, -bleed, card.width_mm + 2 * bleed, card.height_mm + 2 * bleed)
    return (0.0, 0.0, card.width_mm, card.height_mm)


def full_bleed_rect(card: CardSpec) -> tuple[float, float, float, float]:
    """The rectangle an element needs to cover the card plus its bleed."""
    bleed = card.bleed_mm
    return (-bleed, -bleed, card.width_mm + 2 * bleed, card.height_mm + 2 * bleed)


def element_rect(el: Element, card: CardSpec) -> tuple[float, float, float, float]:
    """Bounding box of an element in face millimetres (y grows upward)."""
    if el.type == "line":
        x = min(el.x1_mm, el.x2_mm)
        y = min(el.y1_mm, el.y2_mm)
        return (x, y, max(abs(el.x2_mm - el.x1_mm), 0.5), max(abs(el.y2_mm - el.y1_mm), 0.5))
    height = el.h_mm or el.size_pt * (25.4 / 72.0) * el.line_height
    return (el.x_mm, el.y_mm, el.w_mm, height)


def set_rect(el: Element, x: float, y: float, w: float, h: float) -> None:
    if el.type == "line":
        el.x1_mm, el.y1_mm, el.x2_mm, el.y2_mm = x, y, x + w, y + h
        return
    el.x_mm, el.y_mm, el.w_mm, el.h_mm = x, y, w, h


def move_element(
    el: Element,
    dx: float,
    dy: float,
    card: CardSpec,
    *,
    allow_bleed: bool = True,
    step: float = 0.0,
) -> tuple[float, float]:
    """Translate an element by a delta, keeping it inside the editable area.

    Lines are translated endpoint by endpoint: going through their bounding box
    would turn a horizontal line into a barely-sloped one.
    """
    if el.type == "line":
        bx, by, bw, bh = bounds(card, allow_bleed=allow_bleed)
        span_x = abs(el.x2_mm - el.x1_mm)
        span_y = abs(el.y2_mm - el.y1_mm)
        left = min(el.x1_mm, el.x2_mm)
        bottom = min(el.y1_mm, el.y2_mm)
        new_left, new_bottom = left + dx, bottom + dy
        if step:
            new_left, new_bottom = snap(new_left, step), snap(new_bottom, step)
        new_left = clamp(new_left, bx, bx + bw - span_x)
        new_bottom = clamp(new_bottom, by, by + bh - span_y)
        shift_x = round(new_left - left, 3)
        shift_y = round(new_bottom - bottom, 3)
        el.x1_mm = round(el.x1_mm + shift_x, 3)
        el.x2_mm = round(el.x2_mm + shift_x, 3)
        el.y1_mm = round(el.y1_mm + shift_y, 3)
        el.y2_mm = round(el.y2_mm + shift_y, 3)
        return new_left, new_bottom

    x, y, width, height = element_rect(el, card)
    new_x, new_y = x + dx, y + dy
    if step:
        new_x, new_y = snap(new_x, step), snap(new_y, step)
    bx, by, bw, bh = bounds(card, allow_bleed=allow_bleed)
    new_x = clamp(new_x, bx, bx + bw - width)
    new_y = clamp(new_y, by, by + bh - height)
    set_rect(el, new_x, new_y, width, height)
    return new_x, new_y


def resize_element(
    el: Element,
    handle: str,
    dx: float,
    dy: float,
    card: CardSpec,
    *,
    allow_bleed: bool = True,
    step: float = 0.0,
    min_size: float = MIN_ELEMENT_MM,
) -> tuple[float, float, float, float]:
    """Drag one edge or corner, honouring the minimum size and the bounds."""
    if handle not in HANDLES:
        raise ValueError(f"unknown handle: {handle!r} (expected one of {HANDLES})")
    if el.type == "line":
        # A line has no box to resize, so the handle moves the nearer endpoint:
        # a west/south handle drags the first endpoint, anything else the second.
        bx, by, bw, bh = bounds(card, allow_bleed=allow_bleed)
        first = ("w" in handle) or ("s" in handle)
        start_x, start_y = (el.x1_mm, el.y1_mm) if first else (el.x2_mm, el.y2_mm)
        new_x, new_y = start_x + dx, start_y + dy
        if step:
            new_x, new_y = snap(new_x, step), snap(new_y, step)
        new_x = clamp(new_x, bx, bx + bw)
        new_y = clamp(new_y, by, by + bh)
        if first:
            el.x1_mm, el.y1_mm = round(new_x, 3), round(new_y, 3)
        else:
            el.x2_mm, el.y2_mm = round(new_x, 3), round(new_y, 3)
        return element_rect(el, card)

    x0, y0, width, height = element_rect(el, card)
    x1, y1 = x0 + width, y0 + height
    if "w" in handle:
        x0 += dx
    if "e" in handle:
        x1 += dx
    if "s" in handle:
        y0 += dy
    if "n" in handle:
        y1 += dy
    if x1 - x0 < min_size:
        if "w" in handle:
            x0 = x1 - min_size
        else:
            x1 = x0 + min_size
    if y1 - y0 < min_size:
        if "s" in handle:
            y0 = y1 - min_size
        else:
            y1 = y0 + min_size
    if step:
        x0, y0, x1, y1 = snap(x0, step), snap(y0, step), snap(x1, step), snap(y1, step)
    bx, by, bw, bh = bounds(card, allow_bleed=allow_bleed)
    x0 = clamp(x0, bx, bx + bw)
    x1 = clamp(x1, bx, bx + bw)
    y0 = clamp(y0, by, by + bh)
    y1 = clamp(y1, by, by + bh)
    set_rect(el, x0, y0, max(x1 - x0, min_size), max(y1 - y0, min_size))
    return element_rect(el, card)


def hit_test(
    face: CardFace, card: CardSpec, x: float, y: float, *, tolerance: float = 1.0
) -> Element | None:
    """Topmost element containing the point, as the canvas draws them."""
    for element in reversed(face.elements):
        ex, ey, ew, eh = element_rect(element, card)
        if (
            ex - tolerance <= x <= ex + ew + tolerance
            and ey - tolerance <= y <= ey + eh + tolerance
        ):
            return element
    return None


def _centred(card: CardSpec, width: float, height: float) -> tuple[float, float]:
    return ((card.width_mm - width) / 2, (card.height_mm - height) / 2)


def add_element(
    face: CardFace,
    kind: str,
    card: CardSpec,
    *,
    image: str = "",
    text: str = "{call}",
) -> Element:
    """Create a sensible default element of the requested kind and add it."""
    if kind == "text":
        width = max(card.width_mm - 12.0, 20.0)
        x, y = _centred(card, width, 8.0)
        element = Element(
            type="text",
            x_mm=round(x, 2),
            y_mm=round(y, 2),
            w_mm=round(width, 2),
            text=text,
            size_pt=16.0,
            align="center",
        )
    elif kind == "image":
        width, height = 40.0, 30.0
        if image:
            size = image_size_px(image)
            if size and size[1]:
                height = round(width * size[1] / size[0], 2)
        x, y = _centred(card, width, height)
        element = Element(
            type="image",
            x_mm=round(x, 2),
            y_mm=round(y, 2),
            w_mm=width,
            h_mm=height,
            image=image,
        )
    elif kind == "rect":
        width, height = 30.0, 20.0
        x, y = _centred(card, width, height)
        element = Element(
            type="rect", x_mm=round(x, 2), y_mm=round(y, 2), w_mm=width, h_mm=height, fill="#0B3D91"
        )
    elif kind == "line":
        element = Element(
            type="line",
            x1_mm=10.0,
            y1_mm=round(card.height_mm / 2, 2),
            x2_mm=round(card.width_mm - 10.0, 2),
            y2_mm=round(card.height_mm / 2, 2),
            border_width=0.5,
        )
    elif kind == "qso_rows":
        width = max(card.width_mm - 12.0, 20.0)
        x, y = _centred(card, width, 48.0)
        element = Element(
            type="qso_rows",
            x_mm=round(x, 2),
            y_mm=round(y, 2),
            w_mm=round(width, 2),
            row_height_mm=8.0,
            size_pt=9.0,
        )
    else:
        raise ValueError(f"unknown element kind: {kind!r}")
    face.elements.append(element)
    return element


@dataclass(slots=True)
class ImageReport:
    path: str
    width_mm: float
    height_mm: float
    pixels: tuple[int, int] | None
    dpi: float | None
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.warnings


def image_report(
    path: str | Path, width_mm: float, height_mm: float, min_dpi: int = 300
) -> ImageReport:
    """Everything the properties panel shows about an image asset."""
    text = str(path)
    return ImageReport(
        path=text,
        width_mm=width_mm,
        height_mm=height_mm,
        pixels=image_size_px(text),
        dpi=image_effective_dpi(text, width_mm, height_mm),
        warnings=image_warnings(text, width_mm, height_mm, min_dpi),
    )


def face_summary(face: CardFace) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for element in face.elements:
        counts[element.type] = counts.get(element.type, 0) + 1
    return {
        "elements": len(face.elements),
        "by_type": counts,
        "background_image": face.background_image,
    }
