"""eQSL.cc connector (SRS 4.5).

eQSL has no documented general-purpose programming interface, so the supported
path is importing the ADIF report the user downloads from the InBox page.  The
remote fetch entry point fails with instructions rather than guessing at an
undocumented form post (open question OQ-02).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from ..adif import iter_qsos
from ..model import QSO
from ..net import TransportError
from .base import CallbookRecord, SourceContext

__all__ = ["EQSL_INBOX_URL", "EqslSource"]

EQSL_INBOX_URL = "https://www.eqsl.cc/qslcard/DownloadInBox.cfm"


class EqslSource:
    name = "eqsl"

    def is_configured(self, ctx: SourceContext) -> bool:
        return bool(ctx.option("report_path"))

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]:
        report = str(ctx.option("report_path", ""))
        if not report:
            return
        if not Path(report).is_file():
            raise TransportError(f"eQSL report not found: {report}")
        for qso in iter_qsos(report):
            qso.source = self.name
            if not qso.eqsl_qsl_rcvd and qso.lotw_qsl_rcvd:
                qso.eqsl_qsl_rcvd = "Y"
            yield qso

    def download(self, ctx: SourceContext, destination: str) -> str:
        raise TransportError(
            "Automatic eQSL download is not supported: eQSL publishes no general API. "
            f"Open {EQSL_INBOX_URL}, export the InBox as ADIF, then set eqsl.report_path."
        )

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]:
        return []

    def test(self, ctx: SourceContext) -> str:
        report = str(ctx.option("report_path", ""))
        if report and Path(report).is_file():
            return f"eQSL report available: {report}"
        return "No eQSL report configured; export the InBox as ADIF and set eqsl.report_path"
