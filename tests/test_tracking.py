"""Tests for dispatch and return tracking."""

from __future__ import annotations

import csv

import pytest

from qslcard.adif import qso_from_fields
from qslcard.store import Store
from qslcard.tracking import card_log_csv, pull_logbook, qrz_pusher, record_dispatch, record_returns


def rec(**fields: str):
    return qso_from_fields(fields)


@pytest.fixture
def store():
    with Store(":memory:") as handle:
        yield handle


def seed(store: Store) -> list:
    qsos = [
        rec(
            CALL="JA1AA",
            QSO_DATE="20260911",
            TIME_ON="1200",
            BAND="20m",
            MODE="SSB",
            COUNTRY="Japan",
        ),
        rec(
            CALL="VK2XY",
            QSO_DATE="20260911",
            TIME_ON="1230",
            BAND="40m",
            MODE="CW",
            COUNTRY="Australia",
        ),
    ]
    store.import_qsos(qsos, strategy="skip")
    return qsos


def test_record_dispatch_updates_card_log_and_qso(store) -> None:
    qsos = seed(store)
    outcome = record_dispatch(store, qsos, sent_date="2026-09-12", via="BURO", batch_id=4)
    assert outcome.recorded == 2
    assert outcome.pushed_qrz == 0
    rows = store.card_log()
    assert len(rows) == 2
    assert {row["sent_date"] for row in rows} == {"20260912"}
    assert {row["status"] for row in rows} == {"sent"}
    stored = {q.call: q for q in store.query()}
    assert stored["JA1AA"].qsl_sent == "Y"
    assert stored["JA1AA"].qslsdate == "20260912"
    assert stored["JA1AA"].qsl_sent_via == "BURO"


def test_record_dispatch_with_pusher_reports_success_and_failure(store) -> None:
    qsos = seed(store)
    outcome = record_dispatch(
        store, qsos, sent_date="20260912", via="BURO", pusher=lambda batch: len(batch)
    )
    assert outcome.pushed_qrz == 2
    assert outcome.warnings == []

    def explode(_batch):
        raise RuntimeError("QRZ Logbook 未配置")

    failed = record_dispatch(store, qsos, sent_date="20260913", pusher=explode)
    assert failed.recorded == 2
    assert failed.pushed_qrz == 0
    assert failed.warnings and "QRZ" in failed.warnings[0]


def test_record_returns_marks_status_and_date(store) -> None:
    qsos = seed(store)
    record_dispatch(store, qsos, sent_date="20260912", via="BURO")
    outcome = record_returns(store, qsos[:1], returned_date="2026-10-05", via="BURO")
    assert outcome.recorded == 1
    rows = {row["call"]: row for row in store.card_log()}
    assert rows["JA1AA"]["status"] == "returned"
    assert rows["JA1AA"]["returned_date"] == "20261005"
    assert rows["VK2XY"]["status"] == "sent"
    stored = {q.call: q for q in store.query()}
    assert stored["JA1AA"].qsl_rcvd == "Y"
    assert stored["JA1AA"].qslrdate == "20261005"
    stats = store.dispatch_stats()
    assert stats["sent"] == 2 and stats["returned"] == 1 and stats["pending"] == 1
    assert stats["return_rate"] == pytest.approx(0.5)


def test_return_without_a_prior_dispatch_still_records(store) -> None:
    qsos = seed(store)
    outcome = record_returns(store, qsos, returned_date="20261005")
    assert outcome.recorded == 2
    assert {row["status"] for row in store.card_log()} == {"returned"}


def test_dispatch_twice_keeps_history(store) -> None:
    qsos = seed(store)[:1]
    record_dispatch(store, qsos, sent_date="20260912", via="BURO")
    record_dispatch(store, qsos, sent_date="20261101", via="DIRECT", note="re-send")
    rows = store.card_log()
    assert len(rows) == 2
    assert {row["sent_date"] for row in rows} == {"20260912", "20261101"}


def test_push_requires_the_qrz_logbook_key(store) -> None:
    from qslcard.sources.base import SourceContext

    ctx = SourceContext(credentials={}, options={})
    push = qrz_pusher(ctx, sent_date="20260912")
    with pytest.raises(Exception):  # noqa: B017 - both TransportError paths are fine
        push(seed(store))


def test_card_log_csv_export(store, tmp_path) -> None:
    qsos = seed(store)
    record_dispatch(store, qsos, sent_date="20260912", via="BURO", batch_id=2)
    target = tmp_path / "log.csv"
    assert card_log_csv(store, target) == 2
    rows = list(csv.DictReader(target.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0]["call"] in {"JA1AA", "VK2XY"}
    assert rows[0]["sent_date"] == "20260912"
    assert "returned_date" in rows[0]
    # Filters are honoured.
    assert card_log_csv(store, tmp_path / "one.csv", call="JA1") == 1


def test_pull_logbook_merges_and_records_a_batch(store) -> None:
    class FakeSource:
        name = "qrz"

        def is_configured(self, _ctx) -> bool:
            return True

        def fetch(self, _ctx, *, since: str = ""):
            yield rec(
                CALL="JA1AA",
                QSO_DATE="20260911",
                TIME_ON="1200",
                BAND="20m",
                MODE="SSB",
                LOTW_QSL_RCVD="Y",
            )

    import qslcard.tracking as tracking

    original = tracking.get_source
    tracking.get_source = lambda name: FakeSource()  # type: ignore[assignment]
    try:
        from qslcard.sources.base import SourceContext

        ctx = SourceContext(credentials={"qrz_logbook_key": "k"}, options={})
        outcome = pull_logbook(store, "qrz", ctx, since="2026-09-01")
    finally:
        tracking.get_source = original  # type: ignore[assignment]

    assert outcome.stats.inserted == 1
    assert any("仅取变更" in message for message in outcome.messages)
    assert store.count() == 1
    assert store.recent_batches(1)[0]["source"] == "qrz"


def test_pull_logbook_requires_credentials(store) -> None:
    from qslcard.sources.base import SourceContext

    ctx = SourceContext(credentials={}, options={})
    with pytest.raises(RuntimeError, match="QRZ Logbook 未配置"):
        pull_logbook(store, "qrz", ctx)
