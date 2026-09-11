"""Connector registry (SRS SR-COM-001).

Adding a source means adding a class here; nothing else in the core changes.
"""

from __future__ import annotations

from .adif_file import AdifFileSource
from .base import CallbookRecord, Source, SourceContext, apply_record
from .clublog import ClubLogSource
from .eqsl import EqslSource
from .hamqth import HamQthSource
from .lotw import LotwSource
from .qrz import QrzSource

__all__ = [
    "SOURCES",
    "AdifFileSource",
    "CallbookRecord",
    "ClubLogSource",
    "EqslSource",
    "HamQthSource",
    "LotwSource",
    "QrzSource",
    "Source",
    "SourceContext",
    "apply_record",
    "available_sources",
    "get_source",
]

SOURCES: dict[str, type] = {
    AdifFileSource.name: AdifFileSource,
    QrzSource.name: QrzSource,
    HamQthSource.name: HamQthSource,
    LotwSource.name: LotwSource,
    ClubLogSource.name: ClubLogSource,
    EqslSource.name: EqslSource,
}


def available_sources() -> list[str]:
    return sorted(SOURCES)


def get_source(name: str) -> object:
    key = name.strip().lower()
    if key not in SOURCES:
        raise KeyError(f"unknown source: {name!r} (known: {', '.join(available_sources())})")
    return SOURCES[key]()
