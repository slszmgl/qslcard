"""Internal QSO data model (SRS section 5.1).

Canonical rule: the attribute name of a scalar field is the lowercase ADIF tag
name, so mapping between ADIF and the database is a plain tag.lower().
Values are kept as strings for lossless round-tripping; unknown tags are kept
in QSO.extra so no source data is ever discarded.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Scalar fields persisted as database columns (order defines column order).
COLUMNS: tuple[str, ...] = (
    "station_callsign",
    "operator",
    "call",
    "qso_date",
    "time_on",
    "time_off",
    "band",
    "freq",
    "mode",
    "submode",
    "rst_sent",
    "rst_rcvd",
    "tx_pwr",
    "gridsquare",
    "my_gridsquare",
    "dxcc",
    "country",
    "cqz",
    "ituz",
    "cont",
    "state",
    "cnty",
    "iota",
    "sig",
    "sig_info",
    "name",
    "qth",
    "address",
    "zip",
    "email",
    "qsl_via",
    "qsl_sent",
    "qsl_sent_via",
    "qslsdate",
    "qsl_rcvd",
    "qsl_rcvd_via",
    "qslrdate",
    "lotw_qsl_sent",
    "lotw_qsl_rcvd",
    "lotw_qslsdate",
    "lotw_qslrdate",
    "eqsl_qsl_sent",
    "eqsl_qsl_rcvd",
    "comment",
    "notes",
)

#: Provenance columns, filled by the system rather than by an ADIF source.
META_COLUMNS: tuple[str, ...] = ("source", "source_batch")

#: Values that mean yes in ADIF status enumerations.
AFFIRMATIVE: frozenset[str] = frozenset({"Y", "YES", "1", "TRUE", "T"})

#: Fields whose merge must never downgrade a positive confirmation to a blank.
STATUS_FIELDS: tuple[str, ...] = (
    "qsl_sent",
    "qsl_sent_via",
    "qslsdate",
    "qsl_rcvd",
    "qsl_rcvd_via",
    "qslrdate",
    "lotw_qsl_sent",
    "lotw_qsl_rcvd",
    "lotw_qslsdate",
    "lotw_qslrdate",
    "eqsl_qsl_sent",
    "eqsl_qsl_rcvd",
)


@dataclass(slots=True)
class QSO:
    """One amateur radio contact."""

    station_callsign: str = ""
    operator: str = ""
    call: str = ""
    qso_date: str = ""
    time_on: str = ""
    time_off: str = ""
    band: str = ""
    freq: str = ""
    mode: str = ""
    submode: str = ""
    rst_sent: str = ""
    rst_rcvd: str = ""
    tx_pwr: str = ""
    gridsquare: str = ""
    my_gridsquare: str = ""
    dxcc: str = ""
    country: str = ""
    cqz: str = ""
    ituz: str = ""
    cont: str = ""
    state: str = ""
    cnty: str = ""
    iota: str = ""
    sig: str = ""
    sig_info: str = ""
    name: str = ""
    qth: str = ""
    address: str = ""
    zip: str = ""
    email: str = ""
    qsl_via: str = ""
    qsl_sent: str = ""
    qsl_sent_via: str = ""
    #: ADIF QSLSDATE - the day our paper card was posted.
    qslsdate: str = ""
    qsl_rcvd: str = ""
    qsl_rcvd_via: str = ""
    #: ADIF QSLRDATE - the day the reply card arrived.
    qslrdate: str = ""
    lotw_qsl_sent: str = ""
    lotw_qsl_rcvd: str = ""
    lotw_qslsdate: str = ""
    lotw_qslrdate: str = ""
    eqsl_qsl_sent: str = ""
    eqsl_qsl_rcvd: str = ""
    comment: str = ""
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    source: str = ""
    source_batch: str = ""

    # -- derived helpers -------------------------------------------------
    def is_confirmed(self) -> bool:
        """True when any electronic or paper channel already confirmed the QSO."""
        return (
            is_affirmative(self.lotw_qsl_rcvd)
            or is_affirmative(self.eqsl_qsl_rcvd)
            or is_affirmative(self.qsl_rcvd)
        )

    def get(self, name: str, default: str = "") -> str:
        return getattr(self, name, default)

    def to_dict(self, include_extra: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {name: getattr(self, name) for name in COLUMNS}
        data.update({name: getattr(self, name) for name in META_COLUMNS})
        if include_extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QSO:
        qso = cls()
        for name in COLUMNS:
            value = data.get(name)
            if value is not None:
                setattr(qso, name, str(value))
        for name in META_COLUMNS:
            value = data.get(name)
            if value is not None:
                setattr(qso, name, str(value))
        raw_extra = data.get("extra")
        if isinstance(raw_extra, str) and raw_extra:
            try:
                parsed = json.loads(raw_extra)
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict):
                qso.extra = {str(k): str(v) for k, v in parsed.items()}
        elif isinstance(raw_extra, Mapping):
            qso.extra = {str(k): str(v) for k, v in raw_extra.items()}
        return qso

    def extra_json(self) -> str:
        if not self.extra:
            return ""
        return json.dumps(self.extra, ensure_ascii=False, separators=(",", ":"))


def is_affirmative(value: str | None) -> bool:
    return bool(value) and value.strip().upper() in AFFIRMATIVE


def normalize_date(value: str) -> str:
    """Accept 2026-09-11, 2026/09/11 or 20260911 and return ADIF YYYYMMDD."""
    text = (value or "").strip()
    if not text:
        return ""
    compact = text.replace("-", "").replace("/", "").replace(".", "")
    if len(compact) == 8 and compact.isdigit():
        return compact
    return text


def business_key(qso: QSO) -> str:
    """Stable de-duplication key (SRS DED-001).

    TIME_ON is truncated to HHMM because LoTW reports HHMMSS while most loggers
    report HHMM; contacts inside the same minute on the same band and mode are
    the same contact for QSL purposes.
    """
    return "|".join(
        (
            qso.station_callsign.strip().upper(),
            qso.call.strip().upper(),
            qso.qso_date.strip(),
            qso.time_on.strip()[:4],
            qso.band.strip().lower(),
            qso.mode.strip().upper(),
        )
    )


def normalize_band(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.endswith(("m", "cm", "mm")) or lowered in {
        "lf",
        "mf",
        "hf",
        "vhf",
        "uhf",
        "shf",
    }:
        return lowered
    return text


def normalize_time(value: str) -> str:
    text = value.strip().replace(":", "")
    if not text:
        return ""
    if text.isdigit():
        if len(text) == 1:
            return "000" + text
        if len(text) == 2:
            return text + "00"
        if len(text) == 3:
            return "0" + text
        if len(text) in (4, 6):
            return text
    return value.strip()


def normalize_qso(qso: QSO) -> QSO:
    """Normalize in place: cheap string operations only, once per record."""
    qso.call = qso.call.strip().upper()
    qso.station_callsign = qso.station_callsign.strip().upper()
    qso.operator = qso.operator.strip().upper()
    qso.mode = qso.mode.strip().upper()
    qso.submode = qso.submode.strip().upper()
    qso.band = normalize_band(qso.band)
    qso.time_on = normalize_time(qso.time_on)
    qso.time_off = normalize_time(qso.time_off)
    qso.qso_date = qso.qso_date.strip()
    qso.dxcc = qso.dxcc.strip()
    qso.country = qso.country.strip()
    qso.gridsquare = qso.gridsquare.strip().upper()
    qso.my_gridsquare = qso.my_gridsquare.strip().upper()
    for name in STATUS_FIELDS:
        setattr(qso, name, getattr(qso, name).strip().upper())
    return qso


def iter_attrs(qso: QSO) -> Iterator[tuple[str, str]]:
    for name in COLUMNS:
        yield name, getattr(qso, name)


def merge_qso(base: QSO, incoming: QSO) -> QSO:
    """Field-level merge used by the de-duplicator (SRS DED-002).

    Rules: prefer a non-empty incoming value, but never lose a confirmation and
    never let a blank overwrite data; extra keys are unioned with the incoming
    record winning on conflicts.
    """
    for name in COLUMNS:
        new_value = getattr(incoming, name)
        if not new_value:
            continue
        if name in STATUS_FIELDS:
            old_value = getattr(base, name)
            if is_affirmative(old_value) and not is_affirmative(new_value):
                continue
        setattr(base, name, new_value)
    if incoming.extra:
        base.extra.update(incoming.extra)
    if incoming.source and not base.source:
        base.source = incoming.source
    return base
