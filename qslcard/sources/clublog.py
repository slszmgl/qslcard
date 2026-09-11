"""Club Log connector: realtime upload and OQRS (SRS 4.6)."""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence
from pathlib import Path

from ..adif import qso_to_fields, record_to_adif
from ..model import QSO
from ..net import TransportError
from .base import CallbookRecord, SourceContext

__all__ = ["CLUBLOG_REALTIME_URL", "ClubLogSource", "read_oqrs_csv"]

CLUBLOG_REALTIME_URL = "https://clublog.org/realtime.php"
_FORBIDDEN = 403


def read_oqrs_csv(path: str) -> list[dict[str, str]]:
    """Read a Club Log OQRS request export.

    Column names vary between exports, so lookups are case-insensitive and the
    caller decides what to do with missing fields.
    """
    rows: list[dict[str, str]] = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return rows


class ClubLogSource:
    name = "clublog"

    def __init__(self, url: str = CLUBLOG_REALTIME_URL) -> None:
        self.url = url

    def is_configured(self, ctx: SourceContext) -> bool:
        return bool(
            ctx.credential("clublog_email")
            and ctx.credential("clublog_password")
            and ctx.credential("clublog_api_key")
        )

    def upload(self, ctx: SourceContext, qsos: Sequence[QSO], *, callsign: str = "") -> int:
        """Upload QSOs one at a time through the realtime API.

        Callers must batch large logs with the dedicated bulk API: the Club Log
        documentation explicitly forbids sequential realtime uploads (SR-CLUB-002).
        """
        email = ctx.credential("clublog_email")
        password = ctx.credential("clublog_password")
        api_key = ctx.credential("clublog_api_key")
        station_call = callsign or (ctx.station.callsign if ctx.station else "")
        if not (email and password and api_key):
            raise TransportError("Club Log credentials are incomplete")
        sent = 0
        for qso in qsos:
            payload = {
                "email": email,
                "password": password,
                "callsign": station_call,
                "adif": record_to_adif(qso_to_fields(qso)),
                "api": api_key,
            }
            try:
                response = ctx.require_transport().request("POST", self.url, data=payload)
            except TransportError as exc:
                if exc.status == _FORBIDDEN:
                    raise TransportError(
                        "Club Log refused the upload (403). Stop sending immediately and fix the "
                        "stored email, application password or API key; repeated failures block "
                        "the account and the IP address.",
                        status=_FORBIDDEN,
                    ) from exc
                raise
            if response.status == _FORBIDDEN:
                raise TransportError(
                    "Club Log refused the upload (403). Stop sending immediately and check the "
                    "stored credentials.",
                    status=_FORBIDDEN,
                )
            sent += 1
        return sent

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]:
        """OQRS requests become cards for the operators who asked for one."""
        report = str(ctx.option("oqrs_path", ""))
        if not report or not Path(report).is_file():
            return
        for row in read_oqrs_csv(report):
            call = row.get("callsign") or row.get("call") or ""
            if not call:
                continue
            qso = QSO(call=call.upper(), source=self.name)
            qso.qsl_rcvd = "R"
            qso.qsl_via = row.get("route", "") or row.get("qsl_via", "")
            qso.name = row.get("name", "")
            qso.address = ", ".join(
                part
                for part in (row.get("address", ""), row.get("city", ""), row.get("country", ""))
                if part
            )
            qso.extra["OQRS"] = "Y"
            if row.get("date"):
                qso.qso_date = row["date"].replace("-", "")
            yield qso

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]:
        return []

    def test(self, ctx: SourceContext) -> str:
        if not self.is_configured(ctx):
            return (
                "Club Log credentials incomplete (email, application password and API key required)"
            )
        return "Club Log credentials present; uploads stay disabled until enabled explicitly"
