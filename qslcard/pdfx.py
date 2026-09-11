"""PDF/X-1a style page boxes (SRS PRN-Q-007, CAL-007).

fpdf2 emits PDF 1.3 with embedded fonts and no transparency, which is the right
starting point, but it exposes no API for page boxes or an output intent.  The
page boxes are therefore injected into the finished file: fpdf2 writes every
page dictionary with /Type /Page as its last entry, so the insertion point is
unambiguous and the classic cross-reference table stays valid because no new
objects are added.

Honest scope: this writes TrimBox, BleedBox and ArtBox.  A certified PDF/X-1a
file additionally needs an output intent (an ICC profile), which is not
implemented; see the README.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .geometry import SheetLayout

__all__ = ["Boxes", "inject_page_boxes", "layout_boxes", "mm_to_pt"]

MM_TO_PT = 72.0 / 25.4
_PAGE_MARKER = b"/Type /Page\n>>"


def mm_to_pt(value: float) -> float:
    return round(value * MM_TO_PT, 2)


@dataclass(slots=True, frozen=True)
class Boxes:
    """Page boxes in PDF points, origin bottom-left."""

    trim: tuple[float, float, float, float]
    bleed: tuple[float, float, float, float]
    art: tuple[float, float, float, float]

    def as_pdf_entries(self) -> bytes:
        def entry(name: str, box: tuple[float, float, float, float]) -> str:
            x, y, w, h = box
            return f"/{name} [{x} {y} {round(x + w, 2)} {round(y + h, 2)}]"

        lines = (
            entry("TrimBox", self.trim),
            entry("BleedBox", self.bleed),
            entry("ArtBox", self.art),
        )
        return ("\n" + "\n".join(lines)).encode("ascii")


def layout_boxes(layout: SheetLayout) -> Boxes:
    """Derive page boxes for an imposed sheet.

    TrimBox is the finished sheet: the laid-out area inset by the bleed, i.e.
    along the finished card edges.  BleedBox is the full laid-out area, and
    ArtBox mirrors the trim.
    """
    margin = layout.margin_mm
    bleed = layout.card.bleed_mm
    width = layout.sheet.width_mm - 2 * margin
    height = layout.sheet.height_mm - 2 * margin
    bleed_box = (mm_to_pt(margin), mm_to_pt(margin), mm_to_pt(width), mm_to_pt(height))
    trim_box = (
        mm_to_pt(margin + bleed),
        mm_to_pt(margin + bleed),
        mm_to_pt(max(width - 2 * bleed, 0.0)),
        mm_to_pt(max(height - 2 * bleed, 0.0)),
    )
    return Boxes(trim=trim_box, bleed=bleed_box, art=trim_box)


def inject_page_boxes(
    path: str | os.PathLike[str],
    boxes: Boxes,
    *,
    marker: bytes = _PAGE_MARKER,
) -> int:
    """Insert the boxes into every page object; returns the number of pages fixed."""
    target = Path(path)
    data = target.read_bytes()
    count = data.count(marker)
    if count == 0:
        return 0
    replacement = b"/Type /Page" + boxes.as_pdf_entries() + b"\n>>"
    target.write_bytes(data.replace(marker, replacement))
    return count
