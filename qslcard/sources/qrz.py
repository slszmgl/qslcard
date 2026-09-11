"""QRZ.com connector: XML callbook lookup and Logbook API (SRS 4.3).

QRZ is a first-class source (SRS SR-QRZ-008): the XML subscription service
supplies counterparty details and the Logbook API supplies QSOs and
confirmations.  Both use the user account stored only in the local vault.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence

from ..adif import iter_qsos, record_to_adif
from ..model import QSO, normalize_date
from ..net import TransportError
from .base import CallbookRecord, SourceContext

__all__ = [
    "QRZ_XML_URL",
    "QRZ_LOGBOOK_URL",
    "QrzSource",
    "parse_qrz_callsign",
    "parse_logbook_response",
    "qsl_status_adif",
]

QRZ_XML_URL = "https://xmldata.qrz.com/xml/current/"
QRZ_LOGBOOK_URL = "https://logbook.qrz.com/api"
_AGENT = "qslcard"


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def _join_address(parts: Sequence[str]) -> str:
    return ", ".join(part for part in (p.strip() for p in parts) if part)


def parse_qrz_callsign(xml_text: str, call: str = "") -> CallbookRecord:
    """Parse a QRZ XML Callsign document into a callbook record."""
    root = ET.fromstring(xml_text)
    session = root.find("Session")
    if session is not None:
        error = _text(session, "Error")
        if error:
            raise TransportError(f"QRZ session error: {error}")
    node = root.find("Callsign")
    if node is None:
        return CallbookRecord(call=call.upper(), source="qrz")
    address = _join_address(
        [
            _text(node, "addr1"),
            _text(node, "addr2"),
            _text(node, "city"),
            _text(node, "state"),
            _text(node, "zip"),
        ]
    )
    return CallbookRecord(
        call=(_text(node, "call") or call).upper(),
        name=_join_address([_text(node, "fname"), _text(node, "name")]),
        qth=_text(node, "city") or _text(node, "addr2"),
        address=address,
        state=_text(node, "state"),
        zip=_text(node, "zip"),
        country=_text(node, "country"),
        gridsquare=_text(node, "grid").upper(),
        cq_zone=_text(node, "cqzone"),
        itu_zone=_text(node, "ituzone"),
        email=_text(node, "email"),
        qsl_via=_text(node, "qslmgr"),
        source="qrz",
        extra={"class": _text(node, "class"), "lat": _text(node, "lat"), "lon": _text(node, "lon")},
    )


def parse_logbook_response(body: str) -> tuple[dict[str, str], str]:
    """Split a Logbook API response into its fields and any embedded ADIF."""
    fields = {
        key: values[0]
        for key, values in urllib.parse.parse_qs(body, keep_blank_values=True).items()
    }
    return fields, fields.get("ADIF", "")


def qsl_status_adif(qso: QSO, *, sent_date: str = "", via: str = "", received: bool = False) -> str:
    """Build the ADIF record that carries QSL status back to a QRZ logbook.

    Only the identification fields plus the QSL fields are sent: the logbook
    matches the existing contact on call/date/time/band/mode and updates the
    QSL Card Sent column the operator sees on QRZ.
    """
    fields: dict[str, str] = {}
    for tag, value in (
        ("CALL", qso.call),
        ("QSO_DATE", qso.qso_date),
        ("TIME_ON", qso.time_on[:4]),
        ("BAND", qso.band),
        ("MODE", qso.mode or qso.submode),
    ):
        if value:
            fields[tag] = value
    date = normalize_date(sent_date) or qso.qslsdate
    if received:
        fields["QSL_RCVD"] = "Y"
        if date:
            fields["QSLRDATE"] = date
        if via:
            fields["QSL_RCVD_VIA"] = via
    else:
        fields["QSL_SENT"] = "Y"
        if date:
            fields["QSLSDATE"] = date
        if via:
            fields["QSL_SENT_VIA"] = via
    return record_to_adif(fields)


class QrzSource:
    name = "qrz"

    def __init__(self, xml_url: str = QRZ_XML_URL, logbook_url: str = QRZ_LOGBOOK_URL) -> None:
        self.xml_url = xml_url
        self.logbook_url = logbook_url
        self._session_key = ""

    # -- credentials -----------------------------------------------------
    def _user(self, ctx: SourceContext) -> tuple[str, str]:
        return ctx.credential("qrz_username"), ctx.credential("qrz_password")

    def is_configured(self, ctx: SourceContext) -> bool:
        user, password = self._user(ctx)
        return bool(user and password)

    # -- XML callbook ----------------------------------------------------
    def _login(self, ctx: SourceContext) -> str:
        if self._session_key:
            return self._session_key
        user, password = self._user(ctx)
        if not (user and password):
            raise TransportError("QRZ credentials are missing from the local vault")
        query = urllib.parse.urlencode({"username": user, "password": password, "agent": _AGENT})
        response = ctx.require_transport().request("GET", f"{self.xml_url}?{query}")
        root = ET.fromstring(response.body)
        session = root.find("Session")
        if session is None:
            raise TransportError("QRZ returned no session element")
        error = _text(session, "Error")
        if error:
            raise TransportError(f"QRZ login failed: {error}")
        key = _text(session, "Key")
        if not key:
            raise TransportError("QRZ login returned no session key")
        self._session_key = key
        return key

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]:
        cache = ctx.cache
        records: list[CallbookRecord] = []
        for call in calls:
            key = f"qrz:call:{call.upper()}"
            if cache is not None:
                cached = cache.get(key)
                if cached is not None:
                    records.append(parse_qrz_callsign(cached, call))
                    continue
            session = self._login(ctx)
            query = urllib.parse.urlencode({"s": session, "callsign": call})
            response = ctx.require_transport().request("GET", f"{self.xml_url}?{query}")
            if cache is not None:
                cache.put(key, response.body)
            records.append(parse_qrz_callsign(response.body, call))
        return records

    # -- logbook ---------------------------------------------------------
    def _logbook_key(self, ctx: SourceContext) -> str:
        key = ctx.credential("qrz_logbook_key")
        if not key:
            raise TransportError("QRZ Logbook API key is missing from the local vault")
        return key

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]:
        """Pull this station's log from the QRZ Logbook.

        since limits the pull to contacts changed on or after that date, which
        keeps a daily sync cheap; without it the whole logbook is fetched.
        """
        option = f"MODSINCE:{normalize_date(since)}" if since else "ALL"
        payload = {"KEY": self._logbook_key(ctx), "ACTION": "FETCH", "OPTION": option}
        response = ctx.require_transport().request("POST", self.logbook_url, data=payload)
        fields, adif_text = parse_logbook_response(response.body)
        if fields.get("RESULT", "").upper() not in ("OK", ""):
            raise TransportError(
                f"QRZ logbook fetch failed: {fields.get('REASON', 'unknown error')}"
            )
        if not adif_text:
            return
        import io

        for qso in iter_qsos(io.StringIO(adif_text)):
            qso.source = self.name
            yield qso

    def push_qsl_status(
        self,
        ctx: SourceContext,
        qsos: Sequence[QSO],
        *,
        sent_date: str = "",
        via: str = "",
        received: bool = False,
        chunk_size: int = 40,
    ) -> int:
        """Write QSL Card Sent (or Received) dates back to the QRZ Logbook.

        Uses ACTION=INSERT, which updates the matching contact when it exists.
        Requests are chunked to stay inside the logbook payload limits, and a
        FAIL result stops the run immediately, because QRZ blocks accounts that
        keep re-sending rejected batches.
        """
        key = self._logbook_key(ctx)
        records = [
            qsl_status_adif(qso, sent_date=sent_date, via=via, received=received)
            for qso in qsos
            if qso.call and qso.qso_date
        ]
        if not records:
            return 0
        size = max(chunk_size, 1)
        pushed = 0
        transport = ctx.require_transport()
        for start in range(0, len(records), size):
            chunk = records[start : start + size]
            response = transport.request(
                "POST",
                self.logbook_url,
                data={"KEY": key, "ACTION": "INSERT", "ADIF": "".join(chunk)},
            )
            fields, _adif = parse_logbook_response(response.body)
            result = fields.get("RESULT", "").upper()
            if result not in ("OK", "REPLACE"):
                raise TransportError(
                    "QRZ logbook rejected the QSL update: "
                    f"{fields.get('REASON', result or 'unknown error')}. "
                    "Check the API key and that the logbook is writable."
                )
            pushed += len(chunk)
        return pushed

    def test(self, ctx: SourceContext) -> str:
        session = self._login(ctx)
        return f"QRZ session established (key length {len(session)})"
