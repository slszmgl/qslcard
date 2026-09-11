"""Grouping QSOs into physical cards (SRS TPL-M-001/003)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .model import QSO

__all__ = ["Card", "plan_cards", "summarize_cards"]

GROUP_BY = ("call", "country", "none")


@dataclass(slots=True)
class Card:
    """One physical card and the QSO(s) printed on it."""

    key: str
    call: str
    qsos: list[QSO] = field(default_factory=list)

    @property
    def country(self) -> str:
        for qso in self.qsos:
            if qso.country:
                return qso.country
        return ""

    @property
    def primary(self) -> QSO:
        return self.qsos[0]

    def __len__(self) -> int:
        return len(self.qsos)


def _chunks(items: list[QSO], size: int) -> Iterable[list[QSO]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def plan_cards(
    qsos: Iterable[QSO],
    *,
    group_by: str = "call",
    max_per_card: int = 4,
) -> list[Card]:
    """Turn a QSO stream into cards.

    group_by: call merges all QSOs with one station onto shared cards, country
    groups by DXCC entity, none gives every QSO its own card.  max_per_card
    splits a group once it is full, so one card is never over-filled.
    """
    if group_by not in GROUP_BY:
        raise ValueError(f"group_by must be one of {GROUP_BY}, got {group_by!r}")
    if max_per_card < 1:
        raise ValueError("max_per_card must be at least 1")

    groups: dict[str, list[QSO]] = {}
    for qso in qsos:
        if group_by == "call":
            key = qso.call or "?"
        elif group_by == "country":
            key = qso.country or qso.call or "?"
        else:
            key = f"{qso.call}|{qso.qso_date}|{qso.time_on}"
        groups.setdefault(key, []).append(qso)

    out: list[Card] = []
    for key, members in groups.items():
        for index, chunk in enumerate(_chunks(members, max_per_card)):
            suffix = f"#{index + 1}" if len(members) > max_per_card else ""
            out.append(Card(key=f"{key}{suffix}", call=chunk[0].call, qsos=chunk))
    return out


def summarize_cards(cards: list[Card]) -> dict[str, int]:
    return {
        "cards": len(cards),
        "qsos": sum(len(card) for card in cards),
        "calls": len({card.call for card in cards}),
        "multi_qso_cards": sum(1 for card in cards if len(card) > 1),
    }
