"""Dispatch and confirmation bookkeeping (SRS section 6.7).

One place that knows how a card moves from printed to posted to confirmed, so
the CLI and the GUI behave identically and the QRZ write-back can be swapped
out in tests.
"""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import QSO, business_key, normalize_date
from .net import TransportError
from .privacy import EgressDenied
from .sources import SourceContext, get_source
from .store import ImportStats, Store

__all__ = [
    "CARD_LOG_COLUMNS",
    "DispatchOutcome",
    "PullOutcome",
    "card_log_csv",
    "pull_logbook",
    "qrz_pusher",
    "record_dispatch",
    "record_returns",
]

#: Columns exported by the card log CSV, in a spreadsheet-friendly order.
CARD_LOG_COLUMNS: tuple[str, ...] = (
    "call",
    "qso_date",
    "band",
    "mode",
    "country",
    "sent_date",
    "sent_via",
    "status",
    "returned_date",
    "returned_via",
    "batch_id",
    "card_no",
    "note",
)

#: Errors that mean "the remote side refused", not "the program is broken".
_PUSH_ERRORS = (TransportError, EgressDenied, RuntimeError, ValueError, OSError)


@dataclass(slots=True)
class DispatchOutcome:
    """What one dispatch or return-recording run actually did."""

    recorded: int = 0
    pushed_qrz: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"已记录 {self.recorded} 张卡片"]
        if self.pushed_qrz:
            parts.append(f"已回写 QRZ Logbook {self.pushed_qrz} 条")
        if self.warnings:
            parts.append(f"{len(self.warnings)} 条提示")
        return "，".join(parts)


@dataclass(slots=True)
class PullOutcome:
    source: str
    stats: ImportStats
    messages: list[str] = field(default_factory=list)


def qrz_pusher(
    ctx: SourceContext,
    *,
    sent_date: str = "",
    via: str = "",
    received: bool = False,
) -> Callable[[Sequence[QSO]], int]:
    """Build the callable that writes QSL status back to the QRZ Logbook."""
    source = get_source("qrz")

    def push(qsos: Sequence[QSO]) -> int:
        return int(
            source.push_qsl_status(ctx, qsos, sent_date=sent_date, via=via, received=received)
        )

    return push


def record_dispatch(
    store: Store,
    qsos: Sequence[QSO],
    *,
    sent_date: str,
    via: str = "",
    batch_id: int = 0,
    note: str = "",
    status: str = "sent",
    pusher: Callable[[Sequence[QSO]], int] | None = None,
) -> DispatchOutcome:
    """Record posted cards, mirror the QSO status, and optionally push to QRZ."""
    outcome = DispatchOutcome()
    if not qsos:
        return outcome
    keys = [business_key(qso) for qso in qsos]
    outcome.recorded = store.record_dispatch(
        keys, sent_date=sent_date, via=via, batch_id=batch_id, note=note, status=status
    )
    store.add_audit(
        "dispatch", str(batch_id), f"{outcome.recorded} cards via {via or 'unspecified'}"
    )
    if pusher is not None:
        try:
            outcome.pushed_qrz = pusher(qsos)
        except _PUSH_ERRORS as exc:
            outcome.warnings.append(f"回写 QRZ Logbook 失败：{exc}")
    return outcome


def record_returns(
    store: Store,
    qsos: Sequence[QSO],
    *,
    returned_date: str,
    via: str = "",
    note: str = "",
    pusher: Callable[[Sequence[QSO]], int] | None = None,
) -> DispatchOutcome:
    """Record reply cards that arrived, and optionally push that to QRZ."""
    outcome = DispatchOutcome()
    if not qsos:
        return outcome
    keys = [business_key(qso) for qso in qsos]
    outcome.recorded = store.mark_returned(keys, returned_date=returned_date, via=via, note=note)
    store.add_audit("return", normalize_date(returned_date), f"{outcome.recorded} cards")
    if pusher is not None:
        try:
            outcome.pushed_qrz = pusher(qsos)
        except _PUSH_ERRORS as exc:
            outcome.warnings.append(f"回写 QRZ Logbook 失败：{exc}")
    return outcome


def _require_configured(name: str, ctx: SourceContext, source: Any) -> None:
    if name == "qrz":
        # Only the Logbook key is needed to pull or push logbook data.
        if not ctx.credential("qrz_logbook_key"):
            raise RuntimeError(
                "QRZ Logbook 未配置：请先在凭证库中保存 qrz_logbook_key（Logbook API Key）"
            )
        return
    if name == "lotw":
        if not source.is_configured(ctx):
            raise RuntimeError("LoTW 未配置：请在设置中指定 TQSL 路径并导入证书")
        return
    if not source.is_configured(ctx):
        raise RuntimeError(f"{name} 未配置")


def pull_logbook(
    store: Store,
    name: str,
    ctx: SourceContext,
    *,
    since: str = "",
    work_dir: str | Path = "",
) -> PullOutcome:
    """Pull QSOs/confirmations from a logbook service and merge them.

    QRZ Logbook is fetched over the API; LoTW has no such API, so it is pulled
    through the local TQSL command line into a temporary ADIF report.
    """
    key = name.strip().lower()
    source = get_source(key)
    _require_configured(key, ctx, source)
    messages: list[str] = []
    if key == "lotw":
        work = str(work_dir) if work_dir else tempfile.mkdtemp(prefix="qslcard-lotw-")
        qsos = list(source.sync(ctx, work))
        messages.append(f"LoTW：经 TQSL 下载并解析 {len(qsos)} 条确认记录")
    else:
        qsos = list(source.fetch(ctx, since=since) if since else source.fetch(ctx))
        label = "仅取变更" if since else "全量拉取"
        messages.append(f"{key}：{label} {len(qsos)} 条记录")
    stats = store.import_qsos(qsos, strategy="merge", source=key)
    store.record_batch("sync", key, stats)
    messages.append(f"合并结果：新增 {stats.inserted}，更新 {stats.updated}，跳过 {stats.skipped}")
    return PullOutcome(source=key, stats=stats, messages=messages)


def card_log_csv(
    store: Store,
    out_path: str | Path,
    **filters: Any,
) -> int:
    """Export the dispatch table as CSV (UTF-8 with BOM for Excel)."""
    rows = store.card_log(**filters)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CARD_LOG_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in CARD_LOG_COLUMNS})
    return len(rows)
