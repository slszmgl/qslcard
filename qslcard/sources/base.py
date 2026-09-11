"""Connector contracts shared by every data source (SRS 4.1)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import StationConfig
from ..model import QSO
from ..net import ResponseCache, UrllibTransport
from ..privacy import EgressPolicy
from ..store import Store

__all__ = [
    "CallbookRecord",
    "Source",
    "SourceContext",
    "apply_record",
]


@dataclass(slots=True)
class CallbookRecord:
    """Counterparty information retrieved from a callbook service."""

    call: str = ""
    name: str = ""
    qth: str = ""
    address: str = ""
    state: str = ""
    zip: str = ""
    country: str = ""
    gridsquare: str = ""
    cq_zone: str = ""
    itu_zone: str = ""
    email: str = ""
    qsl_via: str = ""
    source: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def has_address(self) -> bool:
        return bool(self.address or self.qth or self.zip)


#: Which QSO fields a callbook result may fill.
_RECORD_FIELDS = (
    "name",
    "qth",
    "address",
    "state",
    "zip",
    "country",
    "gridsquare",
    "email",
    "qsl_via",
)


def apply_record(qso: QSO, record: CallbookRecord, *, overwrite: bool = False) -> bool:
    """Fill a QSO from a callbook record; returns True when anything changed.

    Blank fields are filled by default so manual corrections are never lost
    (SRS SR-CBK-005); pass overwrite=True to force a refresh.
    """
    changed = False
    for name in _RECORD_FIELDS:
        value = getattr(record, name, "")
        if not value:
            continue
        if not overwrite and getattr(qso, name, ""):
            continue
        if getattr(qso, name, "") != value:
            setattr(qso, name, value)
            changed = True
    if record.cq_zone and not qso.cqz:
        qso.cqz = record.cq_zone
        changed = True
    if record.itu_zone and not qso.ituz:
        qso.ituz = record.itu_zone
        changed = True
    if record.source:
        qso.extra["CALLBOOK_SOURCE"] = record.source
    return changed


@dataclass(slots=True)
class SourceContext:
    """Everything a connector may use; injected so connectors stay testable."""

    store: Store | None = None
    transport: Any = None
    cache: ResponseCache | None = None
    policy: EgressPolicy | None = None
    station: StationConfig | None = None
    credentials: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    runner: Any = None

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def credential(self, name: str, default: str = "") -> str:
        return self.credentials.get(name, default)

    def require_transport(self) -> Any:
        if self.transport is None:
            self.transport = UrllibTransport(self.policy or EgressPolicy())
        return self.transport


class Source(Protocol):
    """Contract implemented by all connectors."""

    name: str

    def is_configured(self, ctx: SourceContext) -> bool: ...

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]: ...

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]: ...

    def test(self, ctx: SourceContext) -> str: ...
