"""Tests for the SQLite store and de-duplication behaviour."""

from __future__ import annotations

import pytest

from qslcard.adif import qso_from_fields
from qslcard.model import business_key
from qslcard.store import ImportStats, QsoFilter, Store


def make_store() -> Store:
    return Store(":memory:")


def rec(**fields: str):
    return qso_from_fields(fields)


def test_insert_and_count() -> None:
    with make_store() as store:
        stats = store.import_qsos(
            [rec(CALL="BG1XY", QSO_DATE="20260911", TIME_ON="1230", BAND="20m", MODE="SSB")],
            strategy="skip",
        )
        assert stats.inserted == 1
        assert store.count() == 1


def test_skip_strategy_keeps_first_record() -> None:
    with make_store() as store:
        store.import_qsos(
            [
                rec(
                    CALL="A1AA",
                    QSO_DATE="20260101",
                    TIME_ON="1200",
                    BAND="20m",
                    MODE="SSB",
                    NAME="first",
                )
            ],
            strategy="skip",
        )
        stats = store.import_qsos(
            [
                rec(
                    CALL="A1AA",
                    QSO_DATE="20260101",
                    TIME_ON="1200",
                    BAND="20m",
                    MODE="SSB",
                    NAME="second",
                )
            ],
            strategy="skip",
        )
        assert stats.inserted == 0
        assert stats.skipped == 1
        assert store.query()[0].name == "first"


def test_merge_fills_blanks_and_keeps_confirmation() -> None:
    with make_store() as store:
        store.import_qsos(
            [
                rec(
                    CALL="JA1ABC",
                    QSO_DATE="20260101",
                    TIME_ON="0100",
                    BAND="15m",
                    MODE="FT8",
                    LOTW_QSL_RCVD="Y",
                )
            ],
            strategy="merge",
        )
        stats = store.import_qsos(
            [
                rec(
                    CALL="JA1ABC",
                    QSO_DATE="20260101",
                    TIME_ON="0100",
                    BAND="15m",
                    MODE="FT8",
                    LOTW_QSL_RCVD="",
                    NAME="Taro",
                    QTH="Tokyo",
                )
            ],
            strategy="merge",
        )
        assert stats.updated == 1
        qso = store.query()[0]
        assert qso.lotw_qsl_rcvd == "Y"
        assert qso.name == "Taro"
        assert qso.qth == "Tokyo"


def test_merge_is_skipped_when_nothing_changes() -> None:
    with make_store() as store:
        payload = rec(
            CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB", NAME="Ann"
        )
        store.import_qsos([payload], strategy="merge")
        stats = store.import_qsos([payload], strategy="merge")
        assert stats.updated == 0
        assert stats.skipped == 1


def test_overwrite_replaces_values() -> None:
    with make_store() as store:
        store.import_qsos(
            [
                rec(
                    CALL="A1AA",
                    QSO_DATE="20260101",
                    TIME_ON="1200",
                    BAND="20m",
                    MODE="SSB",
                    NAME="old",
                )
            ],
            strategy="merge",
        )
        store.import_qsos(
            [
                rec(
                    CALL="A1AA",
                    QSO_DATE="20260101",
                    TIME_ON="1200",
                    BAND="20m",
                    MODE="SSB",
                    NAME="new",
                )
            ],
            strategy="overwrite",
        )
        assert store.query()[0].name == "new"


def test_hhmm_and_hhmmss_collapse_to_one_row() -> None:
    with make_store() as store:
        store.import_qsos(
            [rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB")],
            strategy="merge",
        )
        store.import_qsos(
            [rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="120045", BAND="20M", MODE="ssb")],
            strategy="merge",
        )
        assert store.count() == 1


def test_duplicate_keys_inside_one_batch_are_merged() -> None:
    with make_store() as store:
        stats = store.import_qsos(
            [
                rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB"),
                rec(
                    CALL="A1AA",
                    QSO_DATE="20260101",
                    TIME_ON="1200",
                    BAND="20m",
                    MODE="SSB",
                    NAME="merged",
                ),
            ],
            strategy="merge",
            batch_size=10,
        )
        assert store.count() == 1
        assert stats.total == 2
        assert store.query()[0].name == "merged"


def test_extra_fields_round_trip() -> None:
    with make_store() as store:
        store.import_qsos(
            [
                rec(
                    CALL="A1AA",
                    QSO_DATE="20260101",
                    TIME_ON="1200",
                    BAND="20m",
                    MODE="SSB",
                    MY_ANTENNA="Dipole",
                )
            ],
            strategy="skip",
        )
        assert store.query()[0].extra == {"MY_ANTENNA": "Dipole"}


def test_filters() -> None:
    with make_store() as store:
        store.import_qsos(
            [
                rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB"),
                rec(
                    CALL="B2BB",
                    QSO_DATE="20260202",
                    TIME_ON="1300",
                    BAND="40m",
                    MODE="CW",
                    LOTW_QSL_RCVD="Y",
                ),
                rec(
                    CALL="C3CC",
                    QSO_DATE="20260303",
                    TIME_ON="1400",
                    BAND="20m",
                    MODE="FT8",
                    QSL_SENT="Y",
                ),
            ],
            strategy="skip",
        )
        assert store.count() == 3
        assert store.count(QsoFilter(band=("20m",))) == 2
        assert store.count(QsoFilter(confirmed=True)) == 1
        assert store.count(QsoFilter(call_prefix="B")) == 1
        assert store.count(QsoFilter(date_from="20260201")) == 2
        assert store.count(QsoFilter(needs_card=True)) == 1
        assert [q.call for q in store.query(QsoFilter(mode=("CW", "FT8")))] == ["B2BB", "C3CC"]


def test_iter_qsos_streams() -> None:
    with make_store() as store:
        store.import_qsos(
            [
                rec(CALL=f"K{i}AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB")
                for i in range(50)
            ],
            strategy="skip",
        )
        assert len(list(store.iter_qsos(fetch=7))) == 50


def test_update_status_and_audit() -> None:
    with make_store() as store:
        qso = rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB")
        store.import_qsos([qso], strategy="skip")
        assert store.update_status([business_key(qso)], qsl_sent="Y", qsl_sent_via="B") == 1
        stored = store.query()[0]
        assert stored.qsl_sent == "Y"
        assert stored.qsl_sent_via == "B"
        store.add_audit("mark-sent", business_key(qso))
        assert store.count(QsoFilter(qsl_sent=True)) == 1


def test_rejects_unknown_strategy_and_field() -> None:
    with make_store() as store:
        try:
            store.import_qsos([], strategy="nope")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
        try:
            store.update_status([], call="X")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_stats_and_batches() -> None:
    with make_store() as store:
        store.import_qsos(
            [rec(CALL="A1AA", QSO_DATE="20260101", TIME_ON="1200", BAND="20m", MODE="SSB")],
            strategy="skip",
        )
        store.record_batch("import", "adif", ImportStats(total=1, inserted=1))
        info = store.stats()
        assert info["total"] == 1
        assert info["calls"] == 1
        assert info["first_date"] == "20260101"
        assert store.recent_batches()[0]["inserted"] == 1
        store.optimize()


def test_large_batch_import_is_correct() -> None:
    with make_store() as store:
        records = [
            rec(
                CALL=f"K{i % 500}AA",
                QSO_DATE="20260101",
                TIME_ON=f"{i % 60:02d}00",
                BAND="20m",
                MODE="SSB",
            )
            for i in range(5000)
        ]
        stats = store.import_qsos(records, strategy="skip", batch_size=512)
        assert stats.total == 5000
        assert store.count() == stats.inserted


# --------------------------------------------------------------------------
# Card log and schema migration
# --------------------------------------------------------------------------


def test_card_log_dispatch_return_and_stats() -> None:
    with make_store() as store:
        qsos = [
            rec(CALL="JA1AA", QSO_DATE="20260911", TIME_ON="1200", BAND="20m", MODE="SSB"),
            rec(CALL="VK2XY", QSO_DATE="20260911", TIME_ON="1230", BAND="40m", MODE="CW"),
        ]
        store.import_qsos(qsos, strategy="skip")
        keys = [business_key(q) for q in qsos]

        assert store.record_dispatch(keys, sent_date="2026-09-12", via="BURO", batch_id=9) == 2
        rows = store.card_log(status="sent")
        assert len(rows) == 2
        assert {row["batch_id"] for row in rows} == {9}
        assert {row["sent_date"] for row in rows} == {"20260912"}
        assert store.card_log(call="JA1")[0]["call"] == "JA1AA"
        assert store.card_log(batch_id=9) and store.card_log(batch_id=8) == []
        assert store.card_log(sent_from="2026-09-01", sent_to="2026-09-30")

        assert store.mark_returned(keys[:1], returned_date="20261005", via="BURO") == 1
        stats = store.dispatch_stats()
        assert stats["total"] == 2
        assert stats["sent"] == 2
        assert stats["returned"] == 1
        assert stats["pending"] == 1
        assert stats["return_rate"] == 0.5

        entry_id = rows[0]["id"]
        assert store.update_card_log(entry_id, card_no="A-001", note="checked") is True
        with pytest.raises(ValueError):
            store.update_card_log(entry_id, bogus="x")
        with pytest.raises(ValueError):
            store.record_dispatch(keys, sent_date="20260912", status="teleported")


def test_card_log_promotes_queued_entries_instead_of_duplicating() -> None:
    with make_store() as store:
        qso = rec(CALL="JA1AA", QSO_DATE="20260911", TIME_ON="1200", BAND="20m", MODE="SSB")
        store.import_qsos([qso], strategy="skip")
        key = business_key(qso)
        store.record_dispatch([key], sent_date="", status="printed")
        assert len(store.card_log()) == 1
        store.record_dispatch([key], sent_date="20260912", via="BURO", status="sent")
        rows = store.card_log()
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"
        assert rows[0]["sent_date"] == "20260912"


def test_get_many_returns_matching_qsos() -> None:
    with make_store() as store:
        qsos = [
            rec(
                CALL=f"K{i}AA",
                QSO_DATE="20260911",
                TIME_ON=f"{1200 + i:04d}",
                BAND="20m",
                MODE="SSB",
            )
            for i in range(5)
        ]
        store.import_qsos(qsos, strategy="skip")
        keys = [business_key(q) for q in qsos[:3]]
        found = store.get_many([*keys, "missing|key"])
        assert {q.call for q in found} == {"K0AA", "K1AA", "K2AA"}
        assert store.get_many([]) == []


def test_migration_adds_columns_to_an_older_database(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE qso (
            bkey TEXT NOT NULL PRIMARY KEY,
            call TEXT NOT NULL DEFAULT '',
            qso_date TEXT NOT NULL DEFAULT '',
            qsl_sent TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        -- The business key format the current code computes for this row.
        INSERT INTO qso (bkey, call, qso_date, qsl_sent)
        VALUES ('|JA1AA|20260911|||', 'JA1AA', '20260911', 'Y');
        """
    )
    legacy.commit()
    legacy.close()

    with Store(path) as store:
        assert store.schema_version() == 2
        assert store.count() == 1
        qso = store.query()[0]
        assert qso.call == "JA1AA"
        assert qso.qsl_sent == "Y"  # existing data survives the upgrade
        # Columns added by the upgrade are immediately usable.
        assert store.record_dispatch([business_key(qso)], sent_date="20260912", via="BURO") == 1
        refreshed = store.query()[0]
        assert refreshed.qslsdate == "20260912"
        assert store.card_log()[0]["call"] == "JA1AA"
