"""Sheet imposition geometry (SRS sections 7.1 and 8).

Coordinate convention: every measurement is millimetres, and a placement's
origin is the bottom-left corner of the card's bleed box, matching the PDF
coordinate system (origin bottom-left).  Row 0 is the top row of the sheet.

The finished (trim) card is inset from the bleed box by the bleed amount, and a
further calibration line is drawn inside the trim line so a user can align text
visually (SRS CAL-002).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "MM_PER_INCH",
    "PAPERS",
    "CardSpec",
    "LayoutError",
    "Placement",
    "SheetLayout",
    "SheetSpec",
    "best_layout",
    "compute_layout",
    "paper_choices",
    "paper_label",
    "parse_paper",
]

MM_PER_INCH = 25.4

#: Paper sizes in millimetres, portrait orientation.
PAPERS: dict[str, tuple[float, float]] = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "LETTER": (215.9, 279.4),
    "LEGAL": (215.9, 355.6),
    "TABLOID": (279.4, 431.8),
}

_EPSILON = 1e-6


def paper_label(name: str) -> str:
    """Human label for a paper key, with its size in millimetres.

    A bare "A4" tells a user nothing about the physical sheet, so every paper
    picker in the GUI shows the dimensions (SRS CAL-007: no prepress jargon
    without an explanation).
    """
    key = (name or "").strip().upper()
    size = PAPERS.get(key)
    if size is None:
        return name
    width, height = size
    return f"{key} ({width:g} x {height:g} mm)"


def paper_choices() -> list[str]:
    """Labels for every known paper size, sorted by name."""
    return [paper_label(name) for name in sorted(PAPERS)]


def parse_paper(label: str) -> str:
    """Accept "A4", "a4" or "A4 (210 x 297 mm)" and return the paper key."""
    text = (label or "").strip()
    if not text:
        return ""
    head = text.split("(", 1)[0].strip().upper()
    if head in PAPERS:
        return head
    upper = text.upper()
    return upper if upper in PAPERS else head


class LayoutError(ValueError):
    """Raised when no card fits on the requested sheet."""


@dataclass(slots=True)
class CardSpec:
    """One finished card plus its bleed and calibration insets (SRS D-03)."""

    width_mm: float = 90.0
    height_mm: float = 140.0
    bleed_mm: float = 3.0
    calibration_mm: float = 3.0

    @property
    def doc_width_mm(self) -> float:
        """Bleed box width, i.e. what is actually laid out on the sheet."""
        return self.width_mm + 2 * self.bleed_mm

    @property
    def doc_height_mm(self) -> float:
        return self.height_mm + 2 * self.bleed_mm

    def rotated(self) -> CardSpec:
        return CardSpec(
            width_mm=self.height_mm,
            height_mm=self.width_mm,
            bleed_mm=self.bleed_mm,
            calibration_mm=self.calibration_mm,
        )


@dataclass(slots=True)
class SheetSpec:
    width_mm: float
    height_mm: float
    name: str = "CUSTOM"

    @classmethod
    def from_name(cls, name: str) -> SheetSpec:
        key = name.strip().upper()
        if key not in PAPERS:
            raise LayoutError(f"unknown paper: {name!r} (known: {', '.join(sorted(PAPERS))})")
        width, height = PAPERS[key]
        return cls(width_mm=width, height_mm=height, name=key)

    @classmethod
    def custom(cls, width_mm: float, height_mm: float) -> SheetSpec:
        if width_mm <= 0 or height_mm <= 0:
            raise LayoutError("custom sheet dimensions must be positive")
        return cls(width_mm=width_mm, height_mm=height_mm, name="CUSTOM")

    def rotated(self) -> SheetSpec:
        return SheetSpec(width_mm=self.height_mm, height_mm=self.width_mm, name=self.name)


@dataclass(slots=True, frozen=True)
class Placement:
    """Where one card's bleed box sits on the sheet (bottom-left origin)."""

    x_mm: float
    y_mm: float
    column: int
    row: int


@dataclass(slots=True)
class SheetLayout:
    sheet: SheetSpec
    card: CardSpec
    columns: int
    rows: int
    gap_mm: float
    margin_mm: float
    rotated_card: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def per_sheet(self) -> int:
        return self.columns * self.rows

    def page_count(self, cards: int) -> int:
        if cards <= 0:
            return 0
        return -(-cards // self.per_sheet)

    @property
    def placements(self) -> tuple[Placement, ...]:
        doc_w = self.card.doc_width_mm
        doc_h = self.card.doc_height_mm
        out: list[Placement] = []
        for row in range(self.rows):
            for column in range(self.columns):
                x = self.margin_mm + column * (doc_w + self.gap_mm)
                y_top = self.margin_mm + row * (doc_h + self.gap_mm)
                y = self.sheet.height_mm - y_top - doc_h
                out.append(Placement(x, y, column, row))
        return tuple(out)

    # -- derived rectangles (all in sheet coordinates) --------------------
    def bleed_rect(self, placement: Placement) -> tuple[float, float, float, float]:
        """x, y, width, height of the full bleed box."""
        return (placement.x_mm, placement.y_mm, self.card.doc_width_mm, self.card.doc_height_mm)

    def trim_rect(self, placement: Placement) -> tuple[float, float, float, float]:
        """The finished card area, i.e. the bleed box inset by the bleed."""
        bleed = self.card.bleed_mm
        return (
            placement.x_mm + bleed,
            placement.y_mm + bleed,
            self.card.width_mm,
            self.card.height_mm,
        )

    def calibration_rect(self, placement: Placement) -> tuple[float, float, float, float]:
        """Guides inset from the trim line, used to align text (SRS CAL-002)."""
        inset = self.card.calibration_mm
        return (
            placement.x_mm + self.card.bleed_mm + inset,
            placement.y_mm + self.card.bleed_mm + inset,
            self.card.width_mm - 2 * inset,
            self.card.height_mm - 2 * inset,
        )

    def mirror_placement(self, placement: Placement, *, axis: str = "x") -> Placement:
        """Position of the same card on the reverse side of a duplex sheet.

        axis x models a long-edge (book) flip, axis y a short-edge flip.
        """
        if axis == "x":
            x = self.sheet.width_mm - placement.x_mm - self.card.doc_width_mm
            return Placement(x, placement.y_mm, placement.column, placement.row)
        if axis == "y":
            y = self.sheet.height_mm - placement.y_mm - self.card.doc_height_mm
            return Placement(placement.x_mm, y, placement.column, placement.row)
        raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")

    def describe(self) -> str:
        return (
            f"{self.sheet.name} {self.sheet.width_mm:g}x{self.sheet.height_mm:g} mm, "
            f"card {self.card.width_mm:g}x{self.card.height_mm:g} mm "
            f"(bleed {self.card.bleed_mm:g} mm), "
            f"{self.columns}x{self.rows} = {self.per_sheet} per sheet, "
            f"gap {self.gap_mm:g} mm, margin {self.margin_mm:g} mm"
            + (", rotated" if self.rotated_card else "")
        )


def _fit(usable: float, size: float, gap: float) -> int:
    if usable <= 0 or size <= 0:
        return 0
    count = int((usable + gap) // (size + gap))
    while count > 0 and count * size + (count - 1) * gap > usable + _EPSILON:
        count -= 1
    return max(count, 0)


def compute_layout(
    sheet: SheetSpec,
    card: CardSpec,
    *,
    gap_mm: float = 1.0,
    margin_mm: float = 2.0,
    allow_rotation: bool = True,
) -> SheetLayout:
    """Exact layout for one card orientation (auto-rotation picks the best)."""
    if margin_mm < 0 or gap_mm < 0:
        raise LayoutError("margin and gap must not be negative")

    def attempt(spec: CardSpec, rotated: bool) -> SheetLayout | None:
        usable_w = sheet.width_mm - 2 * margin_mm
        usable_h = sheet.height_mm - 2 * margin_mm
        columns = _fit(usable_w, spec.doc_width_mm, gap_mm)
        rows = _fit(usable_h, spec.doc_height_mm, gap_mm)
        if columns < 1 or rows < 1:
            return None
        warnings: list[str] = []
        if margin_mm <= 0:
            warnings.append("zero margin: the bleed runs to the paper edge and may be clipped")
        if gap_mm <= 0:
            warnings.append("zero gap: adjacent cards share a cut line")
        return SheetLayout(sheet, spec, columns, rows, gap_mm, margin_mm, rotated, tuple(warnings))

    options: list[SheetLayout] = []
    portrait = attempt(card, False)
    if portrait:
        options.append(portrait)
    if allow_rotation:
        landscape = attempt(card.rotated(), True)
        if landscape:
            options.append(landscape)
    if not options:
        raise LayoutError(
            f"no {card.width_mm:g}x{card.height_mm:g} mm card fits on "
            f"{sheet.name} with margin {margin_mm:g} mm and gap {gap_mm:g} mm"
        )
    best = max(options, key=lambda lay: (lay.per_sheet, not lay.rotated_card))
    return best


def _ladder(start: float, *, step: float = 0.5) -> list[float]:
    """Candidate values from start down to 0 in fixed steps.

    A fine ladder matters: the best tiling often needs a margin the caller did
    not ask for (2.5 mm on A4, for instance).  Coarse candidates miss it and
    collapse to a zero margin, which is the riskiest possible choice.
    """
    top = max(start, 0.0)
    values = [top]
    value = top
    while value > 0:
        value = round(max(value - step, 0.0), 2)
        values.append(value)
    values.extend([0.0, 2.0, 3.0, 5.0])
    return sorted(set(values), reverse=True)


def best_layout(
    sheet: SheetSpec,
    card: CardSpec,
    *,
    gap_mm: float = 1.0,
    margin_mm: float = 2.0,
    allow_rotation: bool = True,
) -> SheetLayout:
    """Search margin/gap candidates for the highest card count.

    Maximise cards first, then prefer a larger margin and gap, because a bigger
    margin stays clear of the printer's unprintable area and a bigger gap makes
    guillotine cutting easier.  Any reduction is reported in warnings.
    """
    margins = _ladder(margin_mm)
    gaps = _ladder(gap_mm)
    winner: SheetLayout | None = None
    for candidate_margin in margins:
        for candidate_gap in gaps:
            try:
                layout = compute_layout(
                    sheet,
                    card,
                    gap_mm=candidate_gap,
                    margin_mm=candidate_margin,
                    allow_rotation=allow_rotation,
                )
            except LayoutError:
                continue
            if winner is None or (
                layout.per_sheet,
                layout.margin_mm,
                layout.gap_mm,
            ) > (
                winner.per_sheet,
                winner.margin_mm,
                winner.gap_mm,
            ):
                winner = layout
    if winner is None:
        raise LayoutError(f"no card fits on {sheet.name}")
    notes = list(winner.warnings)
    if winner.margin_mm < margin_mm:
        notes.append(
            f"margin reduced from {margin_mm:g} mm to {winner.margin_mm:g} mm to fit more cards"
        )
    if winner.gap_mm < gap_mm:
        notes.append(f"gap reduced from {gap_mm:g} mm to {winner.gap_mm:g} mm to fit more cards")
    if notes:
        winner = SheetLayout(
            winner.sheet,
            winner.card,
            winner.columns,
            winner.rows,
            winner.gap_mm,
            winner.margin_mm,
            winner.rotated_card,
            tuple(notes),
        )
    return winner
