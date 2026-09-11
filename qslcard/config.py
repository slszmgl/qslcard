"""Configuration loading and secret indirection (SRS section 10.3).

JSON is always supported with the standard library.  YAML is optional and only
required when a .yaml/.yml file is actually used, so the default install stays
dependency light.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

__all__ = [
    "PRINT_FIELD_NOTES",
    "PRINT_PRESETS",
    "QSL_VIA_OPTIONS",
    "format_qsl_via",
    "parse_qsl_via",
    "qsl_via_display",
    "AppConfig",
    "PrintConfig",
    "StationConfig",
    "apply_print_preset",
    "default_config_path",
    "describe_preset_changes",
    "describe_print_settings",
    "load_config",
    "resolve_secret",
    "save_config",
]


def _home() -> Path:
    override = os.environ.get("QSLCARD_HOME")
    if override:
        return Path(override)
    return Path.home() / ".qslcard"


def default_config_path() -> Path:
    return _home() / "config.json"


@dataclass(slots=True)
class StationConfig:
    callsign: str = ""
    operator: str = ""
    name: str = ""
    grid: str = ""
    qth: str = ""
    address: str = ""
    email: str = ""
    qsl_via: str = "BURO"


@dataclass(slots=True)
class PrintConfig:
    """Print defaults from SRS D-02 and D-03: home first, 90x140 mm, 3 mm bleed."""

    paper: str = "A4"
    preset: str = "home"
    duplex: bool = True
    bleed_mm: float = 3.0
    # Tight defaults so the 90x140 + 3 mm bleed card still tiles 2x2 on A4;
    # SRS section 8 asks the solver to maximise the count and warn instead.
    gap_mm: float = 1.0
    margin_mm: float = 2.0
    dpi: int = 300
    color: str = "RGB"
    #: pdf for a general file, pdfx for a PDF/X-1a style file with page boxes.
    pdf_type: str = "pdf"
    card_width_mm: float = 90.0
    card_height_mm: float = 140.0
    calibration_lines: bool = True
    calibration_mm: float = 3.0
    crop_marks: bool = True
    registration_marks: bool = False
    color_bars: bool = False
    mark_length_mm: float = 4.0
    #: Images placed below this effective resolution are reported.
    min_image_dpi: int = 300

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class AppConfig:
    station: StationConfig = field(default_factory=StationConfig)
    print: PrintConfig = field(default_factory=PrintConfig)
    sources: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    template: str = "classic"
    database: str = ""
    output_dir: str = ""
    template_dir: str = ""
    vault_path: str = ""
    offline: bool = False
    language: str = "zh"

    def resolved_paths(self) -> AppConfig:
        base = _home()
        if not self.database:
            self.database = str(base / "qslcard.db")
        if not self.output_dir:
            self.output_dir = str(base / "out")
        if not self.template_dir:
            self.template_dir = str(Path(__file__).resolve().parent.parent / "templates")
        if not self.vault_path:
            self.vault_path = str(base / "vault.json")
        return self


# --------------------------------------------------------------------------
# Commercial print presets (SRS CAL-007/008/009)
# --------------------------------------------------------------------------

#: Named print presets.  Keys mirror PrintConfig field names.
PRINT_PRESETS: dict[str, dict[str, object]] = {
    "home": {
        "preset": "home",
        "pdf_type": "pdf",
        "color": "RGB",
        "dpi": 300,
        "bleed_mm": 3.0,
        "gap_mm": 1.0,
        "margin_mm": 2.0,
        "crop_marks": True,
        "registration_marks": False,
        "color_bars": False,
        "calibration_lines": True,
    },
    "commercial": {
        "preset": "commercial",
        "pdf_type": "pdfx",
        "color": "CMYK",
        "dpi": 600,
        "bleed_mm": 3.0,
        "gap_mm": 0.0,
        "margin_mm": 3.0,
        "crop_marks": True,
        "registration_marks": True,
        "color_bars": True,
        "calibration_lines": False,
    },
}

#: Plain-language explanation per setting, for the visual panel (CAL-007).
PRINT_FIELD_NOTES: dict[str, str] = {
    "pdf_type": "通用 PDF 适合家用打印机；PDF/X 风格文件会写入成品框与出血框，印刷厂更易识别。",
    "color": "RGB 适合喷墨/激光打印机；CMYK 是印刷机的工作色彩，屏幕上看起来会偏暗。",
    "dpi": "分辨率越高细节越多，文件也越大；300 DPI 家用足够，600 DPI 适合精细图片。",
    "bleed_mm": "出血是裁切线外的余量，防止裁偏露出白边；默认四边各 3 mm。",
    "crop_marks": "裁切线帮助印刷厂对准裁切位置，成品会被裁掉，不会印在卡片上。",
    "registration_marks": "套准十字用于多色印刷对齐，家用打印不需要。",
    "color_bars": "色彩条供印刷机校色，家用打印不需要。",
    "calibration_lines": "成品线内侧 3 mm 校准线，只用于自己对准文字位置，交给印刷厂时应关闭。",
    "gap_mm": "卡片之间的间距；0 表示相邻卡片共用一条裁切线。",
    "margin_mm": "纸张四周留白，受打印机不可打印区域限制。",
    "min_image_dpi": "低于此有效分辨率的图片会被列出来，避免印出来发虚。",
}


def apply_print_preset(config: PrintConfig, name: str) -> PrintConfig:
    """Return a copy of config with a named preset applied."""
    key = name.strip().lower()
    if key not in PRINT_PRESETS:
        raise KeyError(f"unknown preset: {name!r} (known: {', '.join(sorted(PRINT_PRESETS))})")
    return replace(config, **dict(PRINT_PRESETS[key]))


def describe_print_settings(config: PrintConfig) -> list[tuple[str, str, str]]:
    """Return (label, value, explanation) rows describing the current settings."""
    return [
        (
            "输出类型",
            "PDF/X 风格" if config.pdf_type == "pdfx" else "通用 PDF",
            PRINT_FIELD_NOTES["pdf_type"],
        ),
        ("色彩模式", config.color.upper(), PRINT_FIELD_NOTES["color"]),
        ("分辨率", f"{config.dpi} DPI", PRINT_FIELD_NOTES["dpi"]),
        ("出血", f"{config.bleed_mm:g} mm", PRINT_FIELD_NOTES["bleed_mm"]),
        ("纸张", config.paper, PRINT_FIELD_NOTES["margin_mm"]),
        ("卡片间距", f"{config.gap_mm:g} mm", PRINT_FIELD_NOTES["gap_mm"]),
        ("裁切线", "开" if config.crop_marks else "关", PRINT_FIELD_NOTES["crop_marks"]),
        (
            "套准标记",
            "开" if config.registration_marks else "关",
            PRINT_FIELD_NOTES["registration_marks"],
        ),
        ("色彩条", "开" if config.color_bars else "关", PRINT_FIELD_NOTES["color_bars"]),
        (
            "校准线",
            "开" if config.calibration_lines else "关",
            PRINT_FIELD_NOTES["calibration_lines"],
        ),
    ]


def describe_preset_changes(config: PrintConfig, name: str) -> list[str]:
    """Explain, in plain language, what switching to a preset changes (CAL-008)."""
    key = name.strip().lower()
    if key not in PRINT_PRESETS:
        raise KeyError(f"unknown preset: {name!r}")
    target = dict(PRINT_PRESETS[key])
    notes: list[str] = []
    if target.get("color") != config.color:
        if target.get("color") == "CMYK":
            notes.append("颜色将转换为 CMYK，家用打印机上可能显得偏暗、偏灰。")
        else:
            notes.append("颜色将改回 RGB，适合家用打印机。")
    if target.get("pdf_type") != config.pdf_type:
        if target.get("pdf_type") == "pdfx":
            notes.append("输出会写入成品框与出血框，文件略大，便于印刷厂识别。")
        else:
            notes.append("输出改回通用 PDF，适合直接打印。")
    if target.get("dpi") != config.dpi:
        notes.append(f"分辨率调整为 {target.get('dpi')} DPI，文件大小随之变化。")
    for flag, label in (("registration_marks", "套准标记"), ("color_bars", "色彩条")):
        if target.get(flag) and not getattr(config, flag):
            notes.append(f"将开启{label}，它们印在裁切余量里，成品上看不到。")
        elif getattr(config, flag) and target.get(flag) is False:
            notes.append(f"将关闭{label}。")
    if target.get("calibration_lines") is False and config.calibration_lines:
        notes.append("将关闭校准线，适合交给印刷厂的成品文件。")
    if target.get("margin_mm") != config.margin_mm or target.get("gap_mm") != config.gap_mm:
        notes.append("边距与间距会按印刷要求调整，每张纸的卡片数可能变化。")
    if not notes:
        notes.append("与当前设置一致，无需调整。")
    return notes


#: QSL routes the operator may accept.  More than one is normal: plenty of
#: stations take both bureau and direct cards.
QSL_VIA_OPTIONS: tuple[tuple[str, str], ...] = (
    ("BUREAU", "卡片局 (Bureau)"),
    ("DIRECT", "直接邮寄 (Direct)"),
    ("OQRS", "OQRS 在线索卡"),
    ("LOTW", "LoTW 电子确认"),
    ("EQSL", "eQSL 电子确认"),
)

#: Spellings seen in the wild, folded onto the canonical keys above.
_QSL_VIA_ALIASES: dict[str, str] = {
    "BURO": "BUREAU",
    "BUREAU": "BUREAU",
    "B": "BUREAU",
    "DIRECT": "DIRECT",
    "D": "DIRECT",
    "OQRS": "OQRS",
    "O": "OQRS",
    "LOTW": "LOTW",
    "EQSL": "EQSL",
    "E": "EQSL",
}


def parse_qsl_via(value: str) -> list[str]:
    """Split a stored QSL route string into canonical option keys."""
    text = (value or "").strip()
    if not text:
        return []
    parts = [part.strip().upper() for part in re.split(r"[,/;|]+", text) if part.strip()]
    return [key for key in (_QSL_VIA_ALIASES.get(part, part) for part in parts) if key]


def format_qsl_via(values: Any) -> str:
    """Join selected route keys in a stable, ADIF-friendly order."""
    chosen = {str(item).strip().upper() for item in values if str(item).strip()}
    normalised = {_QSL_VIA_ALIASES.get(item, item) for item in chosen}
    ordered = [key for key, _label in QSL_VIA_OPTIONS if key in normalised]
    extras = sorted(normalised - {key for key, _label in QSL_VIA_OPTIONS})
    return ",".join(ordered + extras)


def qsl_via_display(value: str) -> str:
    """Readable form of a stored route, for labels and card printing."""
    labels = {key: label for key, label in QSL_VIA_OPTIONS}
    keys = parse_qsl_via(value)
    if not keys:
        return ""
    return " / ".join(labels.get(key, key) for key in keys)


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration; a missing file yields defaults."""
    target = Path(path) if path else default_config_path()
    if not target.exists():
        return AppConfig().resolved_paths()
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PyYAML is required to read YAML config; install it or use JSON instead"
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a mapping")
    cfg = AppConfig()
    if isinstance(data.get("station"), dict):
        cfg.station = _from_dict(StationConfig, data["station"])
    if isinstance(data.get("print"), dict):
        cfg.print = _from_dict(PrintConfig, data["print"])
    for name in ("sources", "rules"):
        value = data.get(name)
        if isinstance(value, dict):
            setattr(cfg, name, value)
    for name in ("template", "database", "output_dir", "template_dir", "vault_path", "language"):
        value = data.get(name)
        if isinstance(value, str) and value:
            setattr(cfg, name, value)
    if isinstance(data.get("offline"), bool):
        cfg.offline = data["offline"]
    return cfg.resolved_paths()


def save_config(config: AppConfig, path: str | os.PathLike[str] | None = None) -> Path:
    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "station": asdict(config.station),
        "print": asdict(config.print),
        "sources": config.sources,
        "rules": config.rules,
        "template": config.template,
        "database": config.database,
        "output_dir": config.output_dir,
        "template_dir": config.template_dir,
        "vault_path": config.vault_path,
        "offline": config.offline,
        "language": config.language,
    }
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def resolve_secret(value: str, vault: Any | None = None) -> str:
    """Resolve env:NAME and vault:NAME references; literal values pass through.

    This is what keeps secrets out of the configuration file (SRS PRIV-010).
    """
    if not value:
        return ""
    if value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if value.startswith("vault:"):
        if vault is None:
            raise RuntimeError(f"vault lookup required for {value!r} but no vault was supplied")
        return vault.get(value[6:], "") or ""
    return value
