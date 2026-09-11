"""Card selection rules (SRS section 6.2).

A rule is a small, serialisable predicate that compiles into both a SQL filter
(fast path, executed by SQLite) and an in-memory check (used for derived
conditions such as new-DXCC detection that need surrounding context).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .model import QSO
from .store import QsoFilter, Store

__all__ = ["CardRule", "RuleContext", "select_qsos"]

_TRUTHY = {"1", "TRUE", "YES", "Y", "T"}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return (str(value),)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class RuleContext:
    """Data a rule may need beyond the QSO row itself."""

    confirmed_dxcc: frozenset[str] = field(default_factory=frozenset)
    address_calls: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class CardRule:
    name: str = "needs_card"
    bands: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    call_prefix: str = ""
    date_from: str = ""
    date_to: str = ""
    require_unconfirmed: bool = True
    require_unsent: bool = True
    only_new_dxcc: bool = False
    route_exists: bool = False
    limit: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, name: str = "needs_card") -> CardRule:
        data = data or {}
        return cls(
            name=str(data.get("name", name)),
            bands=_as_tuple(data.get("bands") or data.get("band")),
            modes=_as_tuple(data.get("modes") or data.get("mode")),
            call_prefix=str(data.get("call_prefix", "")),
            date_from=str(data.get("date_from", "")),
            date_to=str(data.get("date_to", "")),
            require_unconfirmed=_as_bool(data.get("require_unconfirmed"), True),
            require_unsent=_as_bool(data.get("require_unsent"), True),
            only_new_dxcc=_as_bool(data.get("only_new_dxcc"), False),
            route_exists=_as_bool(data.get("route_exists"), False),
            limit=int(data.get("limit", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bands": list(self.bands),
            "modes": list(self.modes),
            "call_prefix": self.call_prefix,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "require_unconfirmed": self.require_unconfirmed,
            "require_unsent": self.require_unsent,
            "only_new_dxcc": self.only_new_dxcc,
            "route_exists": self.route_exists,
            "limit": self.limit,
        }

    def to_filter(self) -> QsoFilter:
        """Compile the SQL-expressible part of the rule."""
        flt = QsoFilter(
            band=self.bands,
            mode=self.modes,
            call_prefix=self.call_prefix,
            date_from=self.date_from,
            date_to=self.date_to,
        )
        if self.require_unconfirmed:
            flt.confirmed = False
        if self.require_unsent:
            flt.qsl_sent = False
        return flt

    def matches(self, qso: QSO, context: RuleContext | None = None) -> bool:
        if self.bands and qso.band.lower() not in {b.lower() for b in self.bands}:
            return False
        if self.modes and qso.mode.upper() not in {m.upper() for m in self.modes}:
            return False
        if self.call_prefix and not qso.call.upper().startswith(self.call_prefix.upper()):
            return False
        if self.date_from and qso.qso_date < self.date_from:
            return False
        if self.date_to and qso.qso_date > self.date_to:
            return False
        if self.require_unconfirmed and qso.is_confirmed():
            return False
        if self.require_unsent and qso.qsl_sent.strip().upper() in _TRUTHY:
            return False
        ctx = context or RuleContext()
        if self.only_new_dxcc and qso.country and qso.country in ctx.confirmed_dxcc:
            return False
        return not (self.route_exists and qso.call not in ctx.address_calls)


def select_qsos(
    store: Store,
    rule: CardRule,
    context: RuleContext | None = None,
    *,
    scan_limit: int = 0,
) -> Iterator[QSO]:
    """Stream QSOs matching a rule.

    The SQL filter trims the candidate set before any Python predicate runs, so
    the common case (unsent and unconfirmed) is handled entirely by the
    database.  Derived predicates stream on top of that.
    """
    flt = rule.to_filter()
    derived = rule.only_new_dxcc or rule.route_exists
    candidates = store.iter_qsos(flt) if derived else iter(store.query(flt))
    count = 0
    for qso in candidates:
        if derived and not rule.matches(qso, context):
            continue
        yield qso
        count += 1
        if rule.limit and count >= rule.limit:
            return
        if scan_limit and count >= scan_limit:
            return
