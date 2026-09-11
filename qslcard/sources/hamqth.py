"""HamQTH free callbook connector (SRS 4.7)."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence

from ..model import QSO
from ..net import TransportError
from .base import CallbookRecord, SourceContext

__all__ = ["HAMQTH_URL", "HamQthSource", "parse_hamqth_search"]

HAMQTH_URL = "https://www.hamqth.com/xml.php"
_PROGRAM = "qslcard"


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def parse_hamqth_search(xml_text: str) -> CallbookRecord:
    """Parse a HamQTH search response."""
    root = ET.fromstring(xml_text)
    session = root.find("session")
    if session is not None:
        error = _text(session, "error")
        if error:
            raise TransportError(f"HamQTH error: {error}")
    search = root.find("search")
    if search is None:
        return CallbookRecord(source="hamqth")
    address = ", ".join(
        part
        for part in (
            _text(search, "adr_street1"),
            _text(search, "adr_street2"),
            _text(search, "adr_city"),
            _text(search, "adr_zip"),
        )
        if part
    )
    return CallbookRecord(
        call=_text(search, "callsign").upper(),
        name=_text(search, "nick") or _text(search, "adr_name"),
        qth=_text(search, "adr_city") or _text(search, "qth"),
        address=address,
        state=_text(search, "us_state"),
        zip=_text(search, "adr_zip"),
        country=_text(search, "country"),
        gridsquare=_text(search, "grid").upper(),
        cq_zone=_text(search, "cq"),
        itu_zone=_text(search, "itu"),
        email=_text(search, "email"),
        qsl_via=_text(search, "qsl_via"),
        source="hamqth",
    )


class HamQthSource:
    """Free fallback used when QRZ has no subscription or is out of quota."""

    name = "hamqth"

    def __init__(self, url: str = HAMQTH_URL) -> None:
        self.url = url
        self._session = ""

    def is_configured(self, ctx: SourceContext) -> bool:
        return bool(ctx.credential("hamqth_username") and ctx.credential("hamqth_password"))

    def _login(self, ctx: SourceContext) -> str:
        if self._session:
            return self._session
        user = ctx.credential("hamqth_username")
        password = ctx.credential("hamqth_password")
        if not (user and password):
            raise TransportError("HamQTH credentials are missing")
        query = urllib.parse.urlencode({"u": user, "p": password, "prg": _PROGRAM})
        response = ctx.require_transport().request("GET", f"{self.url}?{query}")
        root = ET.fromstring(response.body)
        session = root.find("session")
        error = _text(session, "error") if session is not None else ""
        if error:
            raise TransportError(f"HamQTH login failed: {error}")
        session_id = _text(session, "session_id") if session is not None else ""
        if not session_id:
            raise TransportError("HamQTH login returned no session id")
        self._session = session_id
        return session_id

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]:
        records: list[CallbookRecord] = []
        for call in calls:
            key = f"hamqth:call:{call.upper()}"
            if ctx.cache is not None:
                cached = ctx.cache.get(key)
                if cached is not None:
                    records.append(parse_hamqth_search(cached))
                    continue
            session = self._login(ctx)
            query = urllib.parse.urlencode({"id": session, "callsign": call, "prg": _PROGRAM})
            response = ctx.require_transport().request("GET", f"{self.url}?{query}")
            if ctx.cache is not None:
                ctx.cache.put(key, response.body)
            records.append(parse_hamqth_search(response.body))
        return records

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]:
        return iter(())

    def test(self, ctx: SourceContext) -> str:
        session = self._login(ctx)
        return f"HamQTH session established (id length {len(session)})"
