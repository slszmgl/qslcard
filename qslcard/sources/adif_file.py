"""ADIF file connector (SRS 4.2 and 4.9)."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from pathlib import Path

from ..adif import iter_qsos
from ..model import QSO
from .base import CallbookRecord, SourceContext

__all__ = ["AdifFileSource", "collect_adif_paths"]


def collect_adif_paths(paths: Sequence[str], *, recursive: bool = True) -> list[str]:
    """Expand files and directories into a sorted list of ADIF paths."""
    found: list[str] = []
    for raw in paths:
        path = Path(os.path.expanduser(raw))
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            for candidate in sorted(path.glob(pattern)):
                if candidate.suffix.lower() in {".adi", ".adif", ".adx"}:
                    found.append(str(candidate))
        elif path.is_file():
            found.append(str(path))
    return found


class AdifFileSource:
    """Yields QSOs from local ADIF files (the offline, always-available source)."""

    name = "adif"

    def is_configured(self, ctx: SourceContext) -> bool:
        return bool(ctx.option("paths"))

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]:
        paths = collect_adif_paths([str(p) for p in ctx.option("paths", [])])
        for path in paths:
            for qso in iter_qsos(path):
                if not qso.source:
                    qso.source = self.name
                yield qso

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]:
        return []

    def test(self, ctx: SourceContext) -> str:
        paths = collect_adif_paths([str(p) for p in ctx.option("paths", [])])
        return f"{len(paths)} ADIF file(s) found"
