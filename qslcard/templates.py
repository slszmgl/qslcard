"""Card template model and placeholder engine (SRS sections 6.4 and 7).

A template is plain JSON so it can be version controlled and diffed (SRS
TPL-X-001).  Element coordinates are millimetres relative to the bottom-left of
the finished card face; negative values are allowed so backgrounds and header
bands can run into the bleed area.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, field
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

from .geometry import CardSpec
from .model import COLUMNS, QSO

__all__ = [
    "BUILTIN_TEMPLATE_NAMES",
    "CardFace",
    "CardTemplate",
    "Element",
    "available_templates",
    "build_context",
    "builtin_template",
    "format_field",
    "load_template",
    "pretty_date",
    "pretty_time",
    "render_text",
]

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_OPEN_ESCAPE = "\x00OPEN\x00"
_CLOSE_ESCAPE = "\x00CLOSE\x00"

#: Element kinds understood by the renderer.
ELEMENT_TYPES = ("text", "rect", "line", "image", "qso_rows")

_DEFAULT_ROW_FIELDS = ("qso_date", "time_on", "band", "mode", "rst_sent", "rst_rcvd")

_ELEMENT_FIELDS = {
    "type",
    "x_mm",
    "y_mm",
    "w_mm",
    "h_mm",
    "text",
    "size_pt",
    "color",
    "fill",
    "border",
    "border_width",
    "align",
    "valign",
    "bold",
    "font",
    "line_height",
    "image",
    "fields",
    "labels",
    "row_height_mm",
    "header",
    "x1_mm",
    "y1_mm",
    "x2_mm",
    "y2_mm",
}


@dataclass(slots=True)
class Element:
    """One drawable item on a card face."""

    type: str = "text"
    x_mm: float = 0.0
    y_mm: float = 0.0
    w_mm: float = 0.0
    h_mm: float = 0.0
    text: str = ""
    size_pt: float = 10.0
    color: str = "#000000"
    fill: str = ""
    border: str = ""
    border_width: float = 0.2
    align: str = "left"
    valign: str = "top"
    bold: bool = False
    font: str = "regular"
    line_height: float = 1.2
    image: str = ""
    fields: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    row_height_mm: float = 7.0
    header: bool = True
    x1_mm: float = 0.0
    y1_mm: float = 0.0
    x2_mm: float = 0.0
    y2_mm: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Element:
        payload: dict[str, Any] = {}
        for key, value in data.items():
            if key not in _ELEMENT_FIELDS:
                continue
            if key == "fields":
                payload[key] = tuple(str(v) for v in value)
            elif key == "labels":
                payload[key] = {str(k): str(v) for k, v in dict(value).items()}
            else:
                payload[key] = value
        element = cls(**payload)
        if element.type not in ELEMENT_TYPES:
            raise ValueError(f"unknown element type: {element.type!r}")
        if element.align not in ("left", "center", "right"):
            raise ValueError(f"align must be left/center/right, got {element.align!r}")
        if element.type == "qso_rows" and not element.fields:
            element.fields = _DEFAULT_ROW_FIELDS
        return element

    def to_dict(self) -> dict[str, Any]:
        """Serialise only non-default values, so templates stay short."""
        data: dict[str, Any] = {"type": self.type}
        for spec in dc_fields(self):
            if spec.name == "type":
                continue
            value = getattr(self, spec.name)
            if spec.default is not MISSING and value == spec.default:
                continue
            if spec.default_factory is not MISSING and value == spec.default_factory():
                continue
            data[spec.name] = list(value) if isinstance(value, tuple) else value
        return data


@dataclass(slots=True)
class CardFace:
    background: str = "#FFFFFF"
    #: Optional full-bleed background image, drawn across the whole bleed box.
    background_image: str = ""
    background_fit: str = "cover"
    elements: list[Element] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> CardFace:
        data = data or {}
        fit = str(data.get("background_fit", "cover")).lower()
        if fit not in ("cover", "contain", "stretch"):
            fit = "cover"
        return cls(
            background=str(data.get("background", "#FFFFFF")),
            background_image=str(data.get("background_image", "")),
            background_fit=fit,
            elements=[Element.from_dict(item) for item in data.get("elements", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "background": self.background,
            "elements": [element.to_dict() for element in self.elements],
        }
        if self.background_image:
            data["background_image"] = self.background_image
            data["background_fit"] = self.background_fit
        return data


@dataclass(slots=True)
class CardTemplate:
    name: str = "classic"
    description: str = ""
    card: CardSpec = field(default_factory=CardSpec)
    fonts: dict[str, str] = field(default_factory=dict)
    front: CardFace = field(default_factory=CardFace)
    back: CardFace = field(default_factory=CardFace)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CardTemplate:
        card_data = dict(data.get("card", {}))
        card = CardSpec(
            width_mm=float(card_data.get("width_mm", 90.0)),
            height_mm=float(card_data.get("height_mm", 140.0)),
            bleed_mm=float(card_data.get("bleed_mm", 3.0)),
            calibration_mm=float(card_data.get("calibration_mm", 3.0)),
        )
        return cls(
            name=str(data.get("name", "classic")),
            description=str(data.get("description", "")),
            card=card,
            fonts={str(k): str(v) for k, v in dict(data.get("fonts", {})).items()},
            front=CardFace.from_dict(data.get("front")),
            back=CardFace.from_dict(data.get("back")),
        )

    @classmethod
    def load(cls, path: str | Path) -> CardTemplate:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "card": {
                "width_mm": self.card.width_mm,
                "height_mm": self.card.height_mm,
                "bleed_mm": self.card.bleed_mm,
                "calibration_mm": self.card.calibration_mm,
            },
            "fonts": self.fonts,
            "front": self.front.to_dict(),
            "back": self.back.to_dict(),
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    def required_fields(self, face: str = "back") -> set[str]:
        """Placeholder names used by a face, so missing data can be reported."""
        chosen = self.back if face == "back" else self.front
        names: set[str] = set()
        for element in chosen.elements:
            names.update(_PLACEHOLDER.findall(element.text))
        return names


# --------------------------------------------------------------------------
# Placeholders
# --------------------------------------------------------------------------


def pretty_date(value: str) -> str:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return text


def pretty_time(value: str) -> str:
    text = value.strip()
    if len(text) == 4 and text.isdigit():
        return f"{text[0:2]}:{text[2:4]}"
    if len(text) == 6 and text.isdigit():
        return f"{text[0:2]}:{text[2:4]}:{text[4:6]}"
    return text


def format_field(name: str, value: str) -> str:
    """Human formatting for a QSO field used in a table row."""
    if name in ("qso_date", "lotw_qslsdate", "lotw_qslrdate"):
        return pretty_date(value)
    if name in ("time_on", "time_off"):
        return pretty_time(value)
    return value


def build_context(
    qso: QSO,
    station: Any = None,
    *,
    index: int = 0,
    total: int = 0,
) -> dict[str, str]:
    """Build the placeholder namespace for one QSO."""
    context: dict[str, str] = {}
    for name in COLUMNS:
        context[name] = getattr(qso, name, "")
    context["date"] = pretty_date(qso.qso_date)
    context["time"] = pretty_time(qso.time_on)
    context["time_off"] = pretty_time(qso.time_off)
    context["grid"] = qso.gridsquare
    context["state"] = qso.state
    if station is not None:
        context["my_call"] = getattr(station, "callsign", "") or context.get("station_callsign", "")
        context["my_grid"] = getattr(station, "grid", "") or qso.my_gridsquare
        context["my_qth"] = getattr(station, "qth", "")
        context["my_name"] = getattr(station, "name", "")
        context["my_email"] = getattr(station, "email", "")
        context["my_address"] = getattr(station, "address", "")
        context["my_operator"] = getattr(station, "operator", "")
        context["qsl_via"] = qso.qsl_via or getattr(station, "qsl_via", "")
    else:
        context["my_call"] = qso.station_callsign
        context["my_grid"] = qso.my_gridsquare
    context["index"] = str(index)
    context["total"] = str(total)
    context["call"] = qso.call
    context.setdefault("name", qso.name)
    return context


def render_text(
    template: str,
    context: Mapping[str, str],
    warnings: list[str] | None = None,
) -> str:
    """Substitute {placeholders}; unknown names render empty and are reported."""
    if not template or "{" not in template:
        return template
    prepared = template.replace("{{", _OPEN_ESCAPE).replace("}}", _CLOSE_ESCAPE)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return context[key]
        if warnings is not None:
            warnings.append(f"unknown placeholder: {{{key}}}")
        return ""

    rendered = _PLACEHOLDER.sub(replace, prepared)
    return rendered.replace(_OPEN_ESCAPE, "{").replace(_CLOSE_ESCAPE, "}")


# --------------------------------------------------------------------------
# Built-in templates (SRS FR-TPL-002)
# --------------------------------------------------------------------------

_NAVY = "#0B3D91"
_INK = "#12161C"
_MUTED = "#6B7280"
_PAPER = "#FFFFFF"


def _classic_dict() -> dict[str, Any]:
    return {
        "name": "classic",
        "description": "Bold header band with a large callsign and a QSO table on the back",
        "card": {"width_mm": 90, "height_mm": 140, "bleed_mm": 3, "calibration_mm": 3},
        "front": {
            "background": _PAPER,
            "elements": [
                {"type": "rect", "x_mm": -3, "y_mm": 112, "w_mm": 96, "h_mm": 31, "fill": _NAVY},
                {
                    "type": "text",
                    "text": "{my_call}",
                    "x_mm": 4,
                    "y_mm": 124,
                    "w_mm": 82,
                    "size_pt": 30,
                    "align": "center",
                    "color": _PAPER,
                    "bold": True,
                },
                {
                    "type": "text",
                    "text": "{my_qth}",
                    "x_mm": 4,
                    "y_mm": 114,
                    "w_mm": 82,
                    "size_pt": 11,
                    "align": "center",
                    "color": "#D8E2F5",
                },
                {
                    "type": "text",
                    "text": "QSL",
                    "x_mm": 4,
                    "y_mm": 62,
                    "w_mm": 82,
                    "size_pt": 46,
                    "align": "center",
                    "color": "#C9D3E4",
                    "bold": True,
                },
                {
                    "type": "text",
                    "text": "Confirming our QSO with {call}",
                    "x_mm": 6,
                    "y_mm": 50,
                    "w_mm": 78,
                    "size_pt": 11,
                    "align": "center",
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "CQ {cqz}  ITU {ituz}  GRID {my_grid}",
                    "x_mm": 6,
                    "y_mm": 6,
                    "w_mm": 78,
                    "size_pt": 9,
                    "align": "center",
                    "color": _MUTED,
                },
            ],
        },
        "back": {
            "background": _PAPER,
            "elements": [
                {
                    "type": "text",
                    "text": "CONFIRMING OUR QSO",
                    "x_mm": 6,
                    "y_mm": 128,
                    "w_mm": 78,
                    "size_pt": 12,
                    "align": "center",
                    "color": _NAVY,
                    "bold": True,
                },
                {
                    "type": "line",
                    "x1_mm": 6,
                    "y1_mm": 126,
                    "x2_mm": 84,
                    "y2_mm": 126,
                    "border_width": 0.4,
                    "color": _NAVY,
                },
                {
                    "type": "qso_rows",
                    "x_mm": 6,
                    "y_mm": 96,
                    "w_mm": 78,
                    "row_height_mm": 8,
                    "size_pt": 9,
                    "header": True,
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "TNX FOR THE QSO  /  73",
                    "x_mm": 6,
                    "y_mm": 58,
                    "w_mm": 78,
                    "size_pt": 11,
                    "align": "center",
                    "color": _INK,
                    "bold": True,
                },
                {
                    "type": "text",
                    "text": "{my_name}  {my_call}",
                    "x_mm": 6,
                    "y_mm": 40,
                    "w_mm": 78,
                    "size_pt": 10,
                    "align": "center",
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "{my_address}",
                    "x_mm": 6,
                    "y_mm": 26,
                    "w_mm": 78,
                    "size_pt": 9,
                    "align": "center",
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "PSE QSL VIA {qsl_via}",
                    "x_mm": 6,
                    "y_mm": 10,
                    "w_mm": 78,
                    "size_pt": 9,
                    "align": "center",
                    "color": _MUTED,
                },
            ],
        },
    }


def _minimal_dict() -> dict[str, Any]:
    return {
        "name": "minimal",
        "description": "Quiet, type-led layout with rules instead of colour blocks",
        "card": {"width_mm": 90, "height_mm": 140, "bleed_mm": 3, "calibration_mm": 3},
        "front": {
            "background": _PAPER,
            "elements": [
                {
                    "type": "text",
                    "text": "{my_call}",
                    "x_mm": 3,
                    "y_mm": 104,
                    "w_mm": 84,
                    "size_pt": 34,
                    "align": "center",
                    "color": _INK,
                    "bold": True,
                },
                {
                    "type": "line",
                    "x1_mm": 20,
                    "y1_mm": 100,
                    "x2_mm": 70,
                    "y2_mm": 100,
                    "border_width": 0.5,
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "{my_qth}   GRID {my_grid}",
                    "x_mm": 6,
                    "y_mm": 90,
                    "w_mm": 78,
                    "size_pt": 10,
                    "align": "center",
                    "color": _MUTED,
                },
                {
                    "type": "text",
                    "text": "QSL",
                    "x_mm": 6,
                    "y_mm": 56,
                    "w_mm": 78,
                    "size_pt": 24,
                    "align": "center",
                    "color": _MUTED,
                },
            ],
        },
        "back": {
            "background": _PAPER,
            "elements": [
                {
                    "type": "text",
                    "text": "{call}",
                    "x_mm": 6,
                    "y_mm": 126,
                    "w_mm": 78,
                    "size_pt": 16,
                    "align": "left",
                    "color": _INK,
                    "bold": True,
                },
                {
                    "type": "line",
                    "x1_mm": 6,
                    "y1_mm": 124,
                    "x2_mm": 84,
                    "y2_mm": 124,
                    "border_width": 0.3,
                    "color": _MUTED,
                },
                {
                    "type": "qso_rows",
                    "x_mm": 6,
                    "y_mm": 104,
                    "w_mm": 78,
                    "row_height_mm": 7.5,
                    "size_pt": 8.5,
                    "header": True,
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "TNX / 73",
                    "x_mm": 6,
                    "y_mm": 62,
                    "w_mm": 78,
                    "size_pt": 10,
                    "align": "left",
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "{my_call}  {my_name}",
                    "x_mm": 6,
                    "y_mm": 14,
                    "w_mm": 78,
                    "size_pt": 8,
                    "align": "left",
                    "color": _MUTED,
                },
            ],
        },
    }


def _contest_dict() -> dict[str, Any]:
    return {
        "name": "contest",
        "description": "Dense single-side layout for high volume bureau batches",
        "card": {"width_mm": 90, "height_mm": 140, "bleed_mm": 3, "calibration_mm": 3},
        "front": {
            "background": _PAPER,
            "elements": [
                {
                    "type": "text",
                    "text": "{my_call}",
                    "x_mm": 3,
                    "y_mm": 118,
                    "w_mm": 84,
                    "size_pt": 26,
                    "align": "center",
                    "color": _INK,
                    "bold": True,
                },
                {
                    "type": "line",
                    "x1_mm": 3,
                    "y1_mm": 116,
                    "x2_mm": 87,
                    "y2_mm": 116,
                    "border_width": 0.4,
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "CONFIRMING QSO WITH {call}",
                    "x_mm": 3,
                    "y_mm": 108,
                    "w_mm": 84,
                    "size_pt": 11,
                    "align": "center",
                    "color": _INK,
                    "bold": True,
                },
                {
                    "type": "qso_rows",
                    "x_mm": 3,
                    "y_mm": 40,
                    "w_mm": 84,
                    "row_height_mm": 7,
                    "size_pt": 8,
                    "header": True,
                    "color": _INK,
                },
                {
                    "type": "text",
                    "text": "TNX 73 DE {my_call}   PSE QSL VIA {qsl_via}",
                    "x_mm": 3,
                    "y_mm": 8,
                    "w_mm": 84,
                    "size_pt": 8,
                    "align": "center",
                    "color": _MUTED,
                },
            ],
        },
        "back": {
            "background": _PAPER,
            "elements": [
                {
                    "type": "text",
                    "text": "QSL {my_call}",
                    "x_mm": 6,
                    "y_mm": 126,
                    "w_mm": 78,
                    "size_pt": 14,
                    "align": "center",
                    "color": _INK,
                    "bold": True,
                },
                {
                    "type": "qso_rows",
                    "x_mm": 6,
                    "y_mm": 80,
                    "w_mm": 78,
                    "row_height_mm": 7,
                    "size_pt": 8,
                    "header": True,
                    "color": _INK,
                },
            ],
        },
    }


_BUILTINS: dict[str, dict[str, Any]] = {
    "classic": _classic_dict(),
    "minimal": _minimal_dict(),
    "contest": _contest_dict(),
}

BUILTIN_TEMPLATE_NAMES: tuple[str, ...] = tuple(_BUILTINS)


def available_templates() -> list[str]:
    return sorted(_BUILTINS)


def builtin_template(
    name: str = "classic", *, overrides: Mapping[str, Any] | None = None
) -> CardTemplate:
    key = name.strip().lower()
    if key not in _BUILTINS:
        raise KeyError(
            f"unknown built-in template: {name!r} (known: {', '.join(available_templates())})"
        )
    data = json.loads(json.dumps(_BUILTINS[key]))
    if overrides:
        data.update(overrides)
    return CardTemplate.from_dict(data)


def load_template(reference: str | Path, template_dir: str | Path | None = None) -> CardTemplate:
    """Load by built-in name or by JSON path (absolute or relative to a dir)."""
    text = str(reference)
    if text.lower() in _BUILTINS:
        return builtin_template(text)
    candidate = Path(text)
    if not candidate.exists() and template_dir is not None:
        for suffix in ("", ".json"):
            probe = Path(template_dir) / f"{text}{suffix}"
            if probe.exists():
                candidate = probe
                break
    if not candidate.exists():
        raise FileNotFoundError(f"template not found: {text}")
    return CardTemplate.load(candidate)


def line_sequence(values: Iterable[str]) -> Sequence[str]:
    return tuple(values)
