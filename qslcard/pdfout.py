"""PDF output: card faces, imposition, guides, colour space (SRS 6.5, 7.9, 8).

Performance notes
-----------------
* fpdf2 is imported lazily, so nothing pays for it unless a PDF is produced.
* One document and one page per sheet: no per-card file handles or objects.
* Glyph-width measurements for auto-shrinking text are memoised, because the
  operator callsign and station block are identical on every card.
* A cover-cropped background image is prepared once per document and reused.
* Compression is on by default; turn it off only to make output greppable.

Commercial print support (SRS CAL-007/008, PRN-Q-007): device CMYK via
fpdf.drawing.DeviceCMYK, crop/registration marks and colour bars in the trim
margin, and TrimBox/BleedBox/ArtBox injection for PDF/X style output.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cards import Card
from .config import PrintConfig, StationConfig
from .geometry import Placement, SheetLayout, SheetSpec, best_layout
from .images import cover_crop, image_size_px, image_warnings
from .templates import CardFace, CardTemplate, Element, build_context, format_field, render_text

__all__ = [
    "DEFAULT_FONT_CANDIDATES",
    "CardRenderer",
    "RenderOptions",
    "RenderResult",
    "find_default_font",
    "render_single_cards",
]

_PT_TO_MM = 25.4 / 72.0
_CORE_FAMILY = "helvetica"
_CUSTOM_FAMILY = "qsl"

#: Candidate TrueType fonts, in preference order.  TTC collections are skipped
#: because their sub-font indexing is not portable across PDF backends.
DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    os.path.expandvars(r"%WINDIR%\Fonts\msyh.ttf"),
    os.path.expandvars(r"%WINDIR%\Fonts\simhei.ttf"),
    os.path.expandvars(r"%WINDIR%\Fonts\simkai.ttf"),
    os.path.expandvars(r"%WINDIR%\Fonts\Deng.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)

_BOLD_FONT_CANDIDATES: tuple[str, ...] = (
    os.path.expandvars(r"%WINDIR%\Fonts\msyhbd.ttf"),
    os.path.expandvars(r"%WINDIR%\Fonts\simhei.ttf"),
    os.path.expandvars(r"%WINDIR%\Fonts\arialbd.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

#: Control patches for the press colour bar, as device CMYK.
_COLOR_BAR_PATCHES: tuple[tuple[float, float, float, float], ...] = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
    (1.0, 1.0, 0.0, 0.0),
    (1.0, 0.0, 1.0, 0.0),
    (0.0, 1.0, 1.0, 0.0),
    (1.0, 1.0, 1.0, 0.0),
    (1.0, 1.0, 1.0, 1.0),
    (0.5, 0.5, 0.5, 0.5),
)


def find_default_font(candidates: tuple[str, ...] = DEFAULT_FONT_CANDIDATES) -> str | None:
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (0, 0, 0)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _hex_to_cmyk(value: str) -> tuple[float, float, float, float]:
    """Naive (ICC-free) RGB to CMYK conversion.

    Adequate for solid template colours; photographic content should be
    converted with a real ICC profile by the print shop.
    """
    red, green, blue = (channel / 255.0 for channel in _hex_to_rgb(value))
    black = 1.0 - max(red, green, blue)
    if black >= 1.0:
        return (0.0, 0.0, 0.0, 1.0)
    scale = 1.0 - black
    return (
        round((1.0 - red - black) / scale, 4),
        round((1.0 - green - black) / scale, 4),
        round((1.0 - blue - black) / scale, 4),
        round(black, 4),
    )


def _cmyk_to_rgb(cmyk: tuple[float, float, float, float]) -> tuple[int, int, int]:
    cyan, magenta, yellow, black = cmyk
    return (
        int(round(255 * (1 - cyan) * (1 - black))),
        int(round(255 * (1 - magenta) * (1 - black))),
        int(round(255 * (1 - yellow) * (1 - black))),
    )


@dataclass(slots=True)
class RenderOptions:
    template: CardTemplate
    station: StationConfig = field(default_factory=StationConfig)
    print_config: PrintConfig = field(default_factory=PrintConfig)
    duplex: bool = True
    crop_marks: bool = True
    calibration_lines: bool = True
    compress: bool = True
    #: None auto-detects a system font, "" forces the built-in core font.
    font_path: str | None = None
    axis: str = "x"
    title: str = "QSL cards"
    #: None follows print_config, so the GUI panel is the single source of truth.
    registration_marks: bool | None = None
    color_bars: bool | None = None


@dataclass(slots=True)
class RenderResult:
    path: str
    sheets: int
    pages: int
    cards: int
    bytes_written: int
    layout: str
    warnings: tuple[str, ...] = ()
    pdf_type: str = "pdf"
    page_boxes: int = 0
    color_mode: str = "RGB"


class CardRenderer:
    """Renders cards onto imposed sheets and writes one PDF."""

    def __init__(self, options: RenderOptions) -> None:
        self.options = options
        self.template = options.template
        self.layout: SheetLayout = best_layout(
            SheetSpec.from_name(options.print_config.paper),
            options.template.card,
            gap_mm=options.print_config.gap_mm,
            margin_mm=options.print_config.margin_mm,
        )
        if options.font_path is None:
            self._font_path = find_default_font()
        elif options.font_path == "":
            self._font_path = None
        else:
            self._font_path = options.font_path
        self._bold_path: str | None = None
        if self._font_path:
            for candidate in _BOLD_FONT_CANDIDATES:
                if os.path.isfile(candidate):
                    self._bold_path = candidate
                    break

        printing = options.print_config
        self.color_mode = (printing.color or "RGB").strip().upper()
        self.pdf_type = (printing.pdf_type or "pdf").strip().lower()
        self.registration_marks = (
            printing.registration_marks
            if options.registration_marks is None
            else bool(options.registration_marks)
        )
        self.bar_marks = (
            printing.color_bars if options.color_bars is None else bool(options.color_bars)
        )
        self.mark_length_mm = float(printing.mark_length_mm or 4.0)
        self.min_image_dpi = int(printing.min_image_dpi or printing.dpi or 300)

        self._family = _CORE_FAMILY
        self._has_bold = True
        self._device_cmyk: Any = None
        self._fit_cache: dict[tuple[Any, ...], float] = {}
        self._image_cache: dict[tuple[str, int, int, str], str] = {}
        self._checked_images: set[tuple[str, int, int]] = set()
        self._temp_dir = ""
        self.warnings: list[str] = list(self.layout.warnings)

    # -- document plumbing ------------------------------------------------
    def _scratch_dir(self) -> str:
        if not self._temp_dir:
            self._temp_dir = tempfile.mkdtemp(prefix="qslcard-render-")
        return self._temp_dir

    def _cleanup(self) -> None:
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = ""

    def _new_pdf(self) -> Any:
        from fpdf import FPDF

        pdf = FPDF(unit="mm", format=(self.layout.sheet.width_mm, self.layout.sheet.height_mm))
        pdf.set_auto_page_break(False)
        pdf.set_margins(0, 0, 0)
        pdf.set_compression(self.options.compress)
        pdf.set_title(self.options.title)
        pdf.set_creator("qslcard")
        if self.color_mode == "CMYK":
            from fpdf.drawing import DeviceCMYK

            self._device_cmyk = DeviceCMYK
        if self._font_path:
            try:
                pdf.add_font(_CUSTOM_FAMILY, "", self._font_path)
                self._family = _CUSTOM_FAMILY
                self._has_bold = False
                if self._bold_path:
                    try:
                        pdf.add_font(_CUSTOM_FAMILY, "B", self._bold_path)
                        self._has_bold = True
                    except Exception:  # noqa: BLE001 - font support is best effort
                        pass
            except Exception:  # noqa: BLE001 - fall back to the core font
                self._family = _CORE_FAMILY
                self._has_bold = True
                self.warnings.append(
                    f"could not embed font {self._font_path!r}; falling back to the core font"
                )
        return pdf

    def _apply_font(self, pdf: Any, size_pt: float, bold: bool) -> None:
        style = "B" if bold and self._has_bold else ""
        pdf.set_font(self._family, style, size_pt)

    # -- colour -----------------------------------------------------------
    def _set_fill(self, pdf: Any, colour: str) -> None:
        if self._device_cmyk is not None:
            pdf.set_fill_color(self._device_cmyk(*_hex_to_cmyk(colour)))
        else:
            pdf.set_fill_color(*_hex_to_rgb(colour))

    def _set_draw(self, pdf: Any, colour: str) -> None:
        if self._device_cmyk is not None:
            pdf.set_draw_color(self._device_cmyk(*_hex_to_cmyk(colour)))
        else:
            pdf.set_draw_color(*_hex_to_rgb(colour))

    def _set_text(self, pdf: Any, colour: str) -> None:
        if self._device_cmyk is not None:
            pdf.set_text_color(self._device_cmyk(*_hex_to_cmyk(colour)))
        else:
            pdf.set_text_color(*_hex_to_rgb(colour))

    def _set_cmyk_fill(self, pdf: Any, cmyk: tuple[float, float, float, float]) -> None:
        if self._device_cmyk is not None:
            pdf.set_fill_color(self._device_cmyk(*cmyk))
        else:
            pdf.set_fill_color(*_cmyk_to_rgb(cmyk))

    # -- coordinate helpers ----------------------------------------------
    def _box(self, placement: Placement, element: Element) -> tuple[float, float, float, float]:
        """Convert a face-relative element box into fpdf top-left coordinates."""
        bleed = self.template.card.bleed_mm
        x = placement.x_mm + bleed + element.x_mm
        y_bottom = placement.y_mm + bleed + element.y_mm
        height = element.h_mm or element.size_pt * _PT_TO_MM * element.line_height
        y_top = self.layout.sheet.height_mm - (y_bottom + height)
        return x, y_top, element.w_mm, height

    # -- primitives -------------------------------------------------------
    def _draw_background(
        self,
        pdf: Any,
        placement: Placement,
        colour: str,
        image: str = "",
        fit: str = "cover",
    ) -> None:
        x = placement.x_mm
        y = self.layout.sheet.height_mm - (placement.y_mm + self.template.card.doc_height_mm)
        width = self.template.card.doc_width_mm
        height = self.template.card.doc_height_mm
        self._set_fill(pdf, colour)
        pdf.rect(x, y, width, height, style="F")
        if not image or not os.path.isfile(image):
            if image:
                self.warnings.append(f"背景图片不存在：{image}")
            return
        for note in image_warnings(image, width, height, self.min_image_dpi):
            if note not in self.warnings:
                self.warnings.append(note)
        path = image
        if fit == "cover":
            path = self._cover_image(image, width / height)
            self._place_image(pdf, path, x, y, width, height)
        else:
            size = image_size_px(image)
            ratio = (size[0] / size[1]) if size and size[1] else 1.0
            offset_x, offset_y, draw_w, draw_h = self._scaled(ratio, width, height, fit)
            self._place_image(pdf, path, x + offset_x, y + offset_y, draw_w, draw_h)

    def _cover_image(self, path: str, ratio: float) -> str:
        key = (path, int(ratio * 1000), 0, "cover")
        cached = self._image_cache.get(key)
        if cached:
            return cached
        try:
            cropped = cover_crop(path, ratio, self._scratch_dir())
        except Exception:  # noqa: BLE001 - fall back to the original file
            return path
        self._image_cache[key] = cropped
        return cropped

    @staticmethod
    def _scaled(
        source_ratio: float,
        box_w: float,
        box_h: float,
        fit: str,
    ) -> tuple[float, float, float, float]:
        if fit == "stretch" or source_ratio <= 0:
            return (0.0, 0.0, box_w, box_h)
        box_ratio = box_w / box_h if box_h else 1.0
        if source_ratio > box_ratio:
            width = box_w
            height = box_w / source_ratio
        else:
            height = box_h
            width = box_h * source_ratio
        return ((box_w - width) / 2, (box_h - height) / 2, width, height)

    def _place_image(self, pdf: Any, path: str, x: float, y: float, w: float, h: float) -> None:
        """Place an image given top-left origin and millimetre size."""
        try:
            pdf.image(path, x=x, y=y, w=w, h=h)
        except Exception as exc:  # noqa: BLE001 - a bad asset must not kill the run
            self.warnings.append(f"图片无法嵌入 {os.path.basename(path)}：{exc}")

    def _sanitize(self, text: str) -> str:
        """Drop characters the core font cannot encode, warning once."""
        if self._family != _CORE_FAMILY or not text:
            return text
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            note = (
                "non latin-1 characters were replaced with '?' because no Unicode font "
                "was found; install a TrueType font (for example SimHei or Noto Sans CJK)"
            )
            if note not in self.warnings:
                self.warnings.append(note)
            return text.encode("latin-1", "replace").decode("latin-1")
        return text

    def _fit_size(self, pdf: Any, text: str, max_width: float, size_pt: float, bold: bool) -> float:
        if not text or max_width <= 0:
            return size_pt
        key = (self._family, bold, round(size_pt, 2), round(max_width, 2), text)
        cached = self._fit_cache.get(key)
        if cached is not None:
            return cached
        size = size_pt
        minimum = max(size_pt * 0.45, 4.0)
        while size > minimum:
            self._apply_font(pdf, size, bold)
            if pdf.get_string_width(text) <= max_width:
                break
            size -= 0.5
        if len(self._fit_cache) > 8192:
            self._fit_cache.clear()
        self._fit_cache[key] = size
        return size

    def _draw_text(
        self, pdf: Any, element: Element, text: str, box: tuple[float, float, float, float]
    ) -> None:
        x, y_top, width, height = box
        text = self._sanitize(text)
        if not text:
            return
        size = self._fit_size(pdf, text, width, element.size_pt, element.bold)
        self._apply_font(pdf, size, element.bold)
        self._set_text(pdf, element.color)
        line_height = size * _PT_TO_MM * element.line_height
        if element.valign == "middle":
            y = y_top + (height - line_height) / 2
        elif element.valign == "bottom":
            y = y_top + height - line_height
        else:
            y = y_top
        align = {"left": "L", "center": "C", "right": "R"}[element.align]
        pdf.set_xy(x, y)
        pdf.cell(width, line_height, text, align=align)

    def _draw_rows(
        self,
        pdf: Any,
        card: Card,
        element: Element,
        box: tuple[float, float, float, float],
        placement: Placement,
    ) -> None:
        """Draw the per-QSO table, never letting a row spill off the card.

        Rows grow downward from the element's y; the footer text of a template
        sits below that, so the number of rows must be bounded by the finished
        card, not by the sheet.
        """
        x0, y_top, width, _height = box
        fields = element.fields or ()
        if not fields:
            return
        columns = len(fields)
        column_width = width / columns
        row_height = element.row_height_mm
        size = element.size_pt
        face_bottom = self.layout.sheet.height_mm - (placement.y_mm + self.template.card.bleed_mm)
        self._set_text(pdf, element.color)
        rows: list[list[str]] = []
        if element.header:
            rows.append([element.labels.get(name, name.upper()) for name in fields])
        for qso in card.qsos:
            context = build_context(qso, self.options.station)
            rows.append(
                [render_text(format_field(name, context.get(name, "")), context) for name in fields]
            )
        drawn = 0
        for index, values in enumerate(rows):
            y = y_top + index * row_height
            if y + row_height > face_bottom + 0.05:
                break
            for column, value in enumerate(values):
                cell_x = x0 + column * column_width
                value = self._sanitize(value)
                fitted = self._fit_size(pdf, value, column_width - 1.0, size, element.bold)
                self._apply_font(pdf, fitted, element.bold)
                pdf.set_xy(cell_x, y)
                align = "C" if element.header and index == 0 else "L"
                pdf.cell(column_width, row_height, value, align=align)
            drawn += 1
        if drawn < len(rows):
            note = (
                f"通联表格超出卡片范围：已省略 {len(rows) - drawn} 行，"
                "请减少每卡通联数或调小表格行高"
            )
            if note not in self.warnings:
                self.warnings.append(note)

    def _draw_guides(self, pdf: Any, placement: Placement) -> None:
        sheet_height = self.layout.sheet.height_mm
        bleed = self.template.card.bleed_mm
        if self.options.calibration_lines and self.template.card.calibration_mm > 0:
            cx, cy, cw, ch = self.layout.calibration_rect(placement)
            self._set_draw(pdf, "#AAB4C3")
            pdf.set_line_width(0.15)
            with _dashed(pdf, 1.0, 1.0):
                pdf.rect(cx, sheet_height - (cy + ch), cw, ch)
        if self.options.crop_marks and bleed > 0:
            tx, ty, tw, th = self.layout.trim_rect(placement)
            top = sheet_height - (ty + th)
            mark = max(min(self.mark_length_mm, bleed + self.layout.margin_mm), 1.5)
            self._set_draw(pdf, "#3C3C3C")
            pdf.set_line_width(0.15)
            for corner_x, corner_y, dx, dy in (
                (tx, top, -1, 0),
                (tx, top, 0, -1),
                (tx + tw, top, 1, 0),
                (tx + tw, top, 0, -1),
                (tx, top + th, -1, 0),
                (tx, top + th, 0, 1),
                (tx + tw, top + th, 1, 0),
                (tx + tw, top + th, 0, 1),
            ):
                pdf.line(corner_x, corner_y, corner_x + dx * mark, corner_y + dy * mark)
        if self.registration_marks:
            self._draw_registration(pdf, placement)
        if self.bar_marks:
            self._draw_color_bars(pdf)

    def _draw_registration(self, pdf: Any, placement: Placement) -> None:
        """Crosshair targets on the bleed corners, for multi-colour alignment."""
        bx, by, bw, bh = self.layout.bleed_rect(placement)
        sheet_height = self.layout.sheet.height_mm
        sheet_width = self.layout.sheet.width_mm
        radius = max(min(self.layout.margin_mm, 2.5), 1.2)
        self._set_draw(pdf, "#000000")
        pdf.set_line_width(0.15)
        for corner_x, corner_y in ((bx, by), (bx + bw, by), (bx, by + bh), (bx + bw, by + bh)):
            if not (0 <= corner_x <= sheet_width and 0 <= corner_y <= sheet_height):
                continue
            top = sheet_height - corner_y
            pdf.line(corner_x - radius, top, corner_x + radius, top)
            pdf.line(corner_x, top - radius, corner_x, top + radius)
            with contextlib.suppress(Exception):
                pdf.ellipse(corner_x - radius / 2, top - radius / 2, radius, radius, style="D")

    def _draw_color_bars(self, pdf: Any) -> None:
        """Ink control patches in the bottom trim margin."""
        margin = self.layout.margin_mm
        if margin < 1.5:
            return
        height = max(min(margin - 0.8, 4.0), 1.0)
        sheet_width = self.layout.sheet.width_mm
        available = sheet_width - 2 * self.layout.margin_mm
        patch_width = min(7.0, available / len(_COLOR_BAR_PATCHES))
        x = self.layout.margin_mm
        y = self.layout.sheet.height_mm - margin / 2 - height / 2
        for patch in _COLOR_BAR_PATCHES:
            self._set_cmyk_fill(pdf, patch)
            pdf.rect(x, y, patch_width, height, style="F")
            x += patch_width

    # -- faces ------------------------------------------------------------
    def _draw_face(self, pdf: Any, face: CardFace, card: Card, placement: Placement) -> None:
        primary = card.primary
        qso_total = len(card.qsos)
        context = build_context(primary, self.options.station, index=1, total=qso_total)
        for element in face.elements:
            if element.type == "text":
                text = self._sanitize(render_text(element.text, context, self.warnings))
                self._draw_text(pdf, element, text, self._box(placement, element))
            elif element.type == "rect":
                x, top, width, height = self._box(placement, element)
                if element.fill:
                    self._set_fill(pdf, element.fill)
                    pdf.rect(x, top, width, height, style="F")
                if element.border:
                    self._set_draw(pdf, element.border)
                    pdf.set_line_width(element.border_width)
                    pdf.rect(x, top, width, height, style="D")
            elif element.type == "line":
                bleed = self.template.card.bleed_mm
                x1 = placement.x_mm + bleed + element.x1_mm
                x2 = placement.x_mm + bleed + element.x2_mm
                y1 = placement.y_mm + bleed + element.y1_mm
                y2 = placement.y_mm + bleed + element.y2_mm
                self._set_draw(pdf, element.color)
                pdf.set_line_width(element.border_width)
                pdf.line(x1, sheet_top(self.layout, y1), x2, sheet_top(self.layout, y2))
            elif element.type == "image":
                self._draw_image_element(pdf, element, placement)
            elif element.type == "qso_rows":
                self._draw_rows(pdf, card, element, self._box(placement, element), placement)

    def _draw_image_element(self, pdf: Any, element: Element, placement: Placement) -> None:
        path = element.image
        if not path:
            return
        if not os.path.isfile(path):
            note = f"图片不存在：{path}"
            if note not in self.warnings:
                self.warnings.append(note)
            return
        x, top, width, height = self._box(placement, element)
        if width <= 0 or height <= 0:
            size = image_size_px(path)
            if size is None:
                return
            width = width or size[0] * 25.4 / self.min_image_dpi
            height = height or size[1] * 25.4 / self.min_image_dpi
        key = (path, round(width, 1), round(height, 1))
        if key not in self._checked_images:
            self._checked_images.add(key)
            for note in image_warnings(path, width, height, self.min_image_dpi):
                if note not in self.warnings:
                    self.warnings.append(note)
        self._place_image(pdf, path, x, top, width, height)

    # -- public API -------------------------------------------------------
    def render(self, cards: list[Card], out_path: str | os.PathLike[str]) -> RenderResult:
        pdf = self._new_pdf()
        per_sheet = self.layout.per_sheet
        placements = self.layout.placements
        sheets = 0
        try:
            for start in range(0, len(cards), per_sheet):
                chunk = cards[start : start + per_sheet]
                pdf.add_page()
                for index, card in enumerate(chunk):
                    placement = placements[index]
                    self._draw_background(
                        pdf,
                        placement,
                        self.template.front.background,
                        self.template.front.background_image,
                        self.template.front.background_fit,
                    )
                    self._draw_face(pdf, self.template.front, card, placement)
                    self._draw_guides(pdf, placement)
                sheets += 1
                if self.options.duplex:
                    pdf.add_page()
                    for index, card in enumerate(chunk):
                        placement = self.layout.mirror_placement(
                            placements[index], axis=self.options.axis
                        )
                        self._draw_background(
                            pdf,
                            placement,
                            self.template.back.background,
                            self.template.back.background_image,
                            self.template.back.background_fit,
                        )
                        self._draw_face(pdf, self.template.back, card, placement)
                        self._draw_guides(pdf, placement)
            target = Path(out_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            pdf.output(str(target))
        finally:
            self._cleanup()

        page_boxes = 0
        if self.pdf_type == "pdfx":
            from .pdfx import inject_page_boxes, layout_boxes

            page_boxes = inject_page_boxes(target, layout_boxes(self.layout))

        pages = sheets * (2 if self.options.duplex else 1)
        return RenderResult(
            path=str(target),
            sheets=sheets,
            pages=pages,
            cards=len(cards),
            bytes_written=target.stat().st_size,
            layout=self.layout.describe(),
            warnings=tuple(self.warnings),
            pdf_type=self.pdf_type,
            page_boxes=page_boxes,
            color_mode=self.color_mode,
        )


class _dashed:
    """Context manager that enables a dash pattern when the backend supports it."""

    __slots__ = ("_pdf", "_dash", "_gap")

    def __init__(self, pdf: Any, dash: float, gap: float) -> None:
        self._pdf = pdf
        self._dash = dash
        self._gap = gap

    def __enter__(self) -> None:
        setter = getattr(self._pdf, "set_dash_pattern", None)
        if setter is not None:
            with contextlib.suppress(Exception):
                setter(dash=self._dash, gap=self._gap)

    def __exit__(self, *_exc: object) -> None:
        setter = getattr(self._pdf, "set_dash_pattern", None)
        if setter is not None:
            with contextlib.suppress(Exception):
                setter()


def sheet_top(layout: SheetLayout, y_from_bottom: float) -> float:
    """Convert a bottom-origin y into fpdf's top-origin y."""
    return layout.sheet.height_mm - y_from_bottom


def render_single_cards(
    cards: list[Card],
    options: RenderOptions,
    out_path: str | os.PathLike[str],
) -> RenderResult:
    """One card per page at trim-plus-bleed size (SRS FR-PRN-001)."""
    renderer = CardRenderer(options)
    card_spec = options.template.card
    renderer.layout = SheetLayout(
        sheet=SheetSpec.custom(card_spec.doc_width_mm, card_spec.doc_height_mm),
        card=card_spec,
        columns=1,
        rows=1,
        gap_mm=0.0,
        margin_mm=0.0,
    )
    return renderer.render(cards, out_path)
