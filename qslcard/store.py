"""SQLite persistence with de-duplication (SRS sections 5.4 and 9).

Performance design
------------------
* WAL journal plus synchronous=NORMAL: one fsync per transaction instead of per
  commit, which is the single biggest win for bulk imports.
* Large page cache and memory temp store keep hot indices in RAM.
* Imports run inside one transaction per batch (default 5000 records) and use
  executemany, so a 100k-record log is a few dozen transactions, not 100k.
* De-duplication prefetches the existing rows for a whole batch with one JOIN
  against a temp table instead of one SELECT per record.
* Rows are materialised into QSO objects with positional construction, which is
  markedly faster than 40+ setattr calls per row.
* QSOs all live in a WITHOUT ROWID table keyed by the business key, so the
  primary key *is* the de-duplication index.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .model import (
    COLUMNS,
    META_COLUMNS,
    QSO,
    business_key,
    merge_qso,
    normalize_date,
)

__all__ = ["ImportStats", "QsoFilter", "Store", "SCHEMA_VERSION"]

SCHEMA_VERSION = 2
_N = len(COLUMNS)

#: Lifecycle of one dispatched card in the card log.
CARD_STATUSES: tuple[str, ...] = ("queued", "printed", "sent", "returned", "bounced", "ignored")
_ALL_COLUMNS: tuple[str, ...] = (
    "bkey",
    *COLUMNS,
    *META_COLUMNS,
    "extra",
    "created_at",
    "updated_at",
)
_UPDATE_COLUMNS: tuple[str, ...] = (*COLUMNS, *META_COLUMNS, "extra", "updated_at")
_SELECT_COLUMNS = ", ".join(("bkey", *COLUMNS, *META_COLUMNS, "extra"))
_SELECT_BASE = f"SELECT {_SELECT_COLUMNS} FROM qso"
_INSERT_SQL = (
    f"INSERT INTO qso ({', '.join(_ALL_COLUMNS)}) VALUES ({', '.join('?' * len(_ALL_COLUMNS))})"
)
_INSERT_OR_IGNORE_SQL = (
    f"INSERT OR IGNORE INTO qso ({', '.join(_ALL_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_ALL_COLUMNS))})"
)
_UPDATE_SQL = (
    f"UPDATE qso SET {', '.join(name + ' = ?' for name in _UPDATE_COLUMNS)} WHERE bkey = ?"
)

_AFFIRMATIVE_SQL = "('Y','YES','1','TRUE','T')"
_CONFIRMED_SQL = (
    f"(lotw_qsl_rcvd IN {_AFFIRMATIVE_SQL} OR eqsl_qsl_rcvd IN {_AFFIRMATIVE_SQL}"
    f" OR qsl_rcvd IN {_AFFIRMATIVE_SQL})"
)

_STRATEGIES = ("skip", "merge", "overwrite")


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """Split a sequence for SQLite IN clauses (well under the variable limit)."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


@dataclass(slots=True)
class ImportStats:
    """Result of one import run."""

    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0

    def __add__(self, other: ImportStats) -> ImportStats:
        return ImportStats(
            total=self.total + other.total,
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            skipped=self.skipped + other.skipped,
            errors=self.errors + other.errors,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
        }


@dataclass(slots=True)
class QsoFilter:
    """Query filter; every field is optional."""

    call: str = ""
    call_prefix: str = ""
    date_from: str = ""
    date_to: str = ""
    band: tuple[str, ...] = ()
    mode: tuple[str, ...] = ()
    source: str = ""
    country: str = ""
    band_in: tuple[str, ...] = ()
    confirmed: bool | None = None
    lotw_confirmed: bool | None = None
    qsl_sent: bool | None = None
    needs_card: bool | None = None
    limit: int = 0
    offset: int = 0
    order_by: str = "qso_date, time_on"

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.call:
            clauses.append("call = ?")
            params.append(self.call.strip().upper())
        if self.call_prefix:
            clauses.append("call LIKE ?")
            params.append(self.call_prefix.strip().upper() + "%")
        if self.date_from:
            clauses.append("qso_date >= ?")
            params.append(self.date_from)
        if self.date_to:
            clauses.append("qso_date <= ?")
            params.append(self.date_to)
        bands = self.band or self.band_in
        if bands:
            clauses.append(f"band IN ({', '.join('?' * len(bands))})")
            params.extend(b.lower() for b in bands)
        if self.mode:
            clauses.append(f"mode IN ({', '.join('?' * len(self.mode))})")
            params.extend(m.upper() for m in self.mode)
        if self.source:
            clauses.append("source = ?")
            params.append(self.source)
        if self.country:
            clauses.append("country = ?")
            params.append(self.country)
        if self.confirmed is not None:
            clauses.append(_CONFIRMED_SQL if self.confirmed else f"NOT {_CONFIRMED_SQL}")
        if self.lotw_confirmed is not None:
            expr = f"lotw_qsl_rcvd IN {_AFFIRMATIVE_SQL}"
            clauses.append(expr if self.lotw_confirmed else f"NOT {expr}")
        if self.qsl_sent is not None:
            expr = f"qsl_sent IN {_AFFIRMATIVE_SQL}"
            clauses.append(expr if self.qsl_sent else f"NOT {expr}")
        if self.needs_card:
            not_sent = f"qsl_sent NOT IN {_AFFIRMATIVE_SQL}"
            clauses.append(f"{not_sent} AND NOT {_CONFIRMED_SQL}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params


def _row_to_qso(row: Sequence[Any]) -> QSO:
    """Materialise a row fast: positional construction, JSON only when present."""
    qso = QSO(*row[1 : 1 + _N])
    qso.source = row[1 + _N] or ""
    qso.source_batch = row[1 + _N + 1] or ""
    raw_extra = row[1 + _N + 2]
    if raw_extra:
        try:
            parsed = json.loads(raw_extra)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            qso.extra = {str(k): str(v) for k, v in parsed.items()}
    return qso


def _insert_params(qso: QSO, key: str, now: str) -> tuple[Any, ...]:
    row = [key]
    row.extend(getattr(qso, name) for name in COLUMNS)
    row.append(qso.source)
    row.append(qso.source_batch)
    row.append(qso.extra_json())
    row.append(now)
    row.append(now)
    return tuple(row)


def _update_params(qso: QSO, key: str, now: str) -> tuple[Any, ...]:
    row = [getattr(qso, name) for name in COLUMNS]
    row.append(qso.source)
    row.append(qso.source_batch)
    row.append(qso.extra_json())
    row.append(now)
    row.append(key)
    return tuple(row)


class Store:
    """Thin, allocation-conscious SQLite wrapper."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        cache_kb: int = 32768,
        mmap_mb: int = 128,
        synchronous: str = "NORMAL",
    ) -> None:
        self.path = os.fspath(path)
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        cur = self._conn
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA synchronous={synchronous}")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute(f"PRAGMA cache_size=-{int(cache_kb)}")
        cur.execute(f"PRAGMA mmap_size={int(mmap_mb) * 1024 * 1024}")
        cur.execute("PRAGMA busy_timeout=10000")
        self.migrate()

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- schema ----------------------------------------------------------
    def migrate(self) -> None:
        columns_sql = ",\n    ".join(
            f"{name} TEXT NOT NULL DEFAULT ''" for name in (*COLUMNS, *META_COLUMNS)
        )
        self._conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS qso (
                bkey TEXT NOT NULL PRIMARY KEY,
                {columns_sql},
                extra TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS batch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS batch_item (
                batch_id INTEGER NOT NULL,
                bkey TEXT NOT NULL,
                PRIMARY KEY (batch_id, bkey)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS card_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bkey TEXT NOT NULL,
                call TEXT NOT NULL DEFAULT '',
                card_no TEXT NOT NULL DEFAULT '',
                batch_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'sent',
                sent_date TEXT NOT NULL DEFAULT '',
                sent_via TEXT NOT NULL DEFAULT '',
                returned_date TEXT NOT NULL DEFAULT '',
                returned_via TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_card_log_bkey ON card_log(bkey);
            CREATE INDEX IF NOT EXISTS ix_card_log_status ON card_log(status);
            CREATE INDEX IF NOT EXISTS ix_card_log_call ON card_log(call);
            CREATE INDEX IF NOT EXISTS ix_card_log_sent ON card_log(sent_date);
            """
        )
        # Column additions must run before the QSO indexes: a database written
        # by an older version has no band/mode/country columns for them to index.
        self._add_missing_columns()
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS ix_qso_call ON qso(call);
            CREATE INDEX IF NOT EXISTS ix_qso_date ON qso(qso_date);
            CREATE INDEX IF NOT EXISTS ix_qso_band ON qso(band);
            CREATE INDEX IF NOT EXISTS ix_qso_mode ON qso(mode);
            CREATE INDEX IF NOT EXISTS ix_qso_country ON qso(country);
            """
        )
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _add_missing_columns(self) -> list[str]:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS never alters an existing table, so upgrades
        such as QSLSDATE/QSLRDATE would otherwise be silently missing on any
        database written by an earlier version.
        """
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(qso)")}
        if not existing:
            return []
        added: list[str] = []
        for name in (*COLUMNS, *META_COLUMNS):
            if name not in existing:
                self._conn.execute(f"ALTER TABLE qso ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
                added.append(name)
        return added

    def schema_version(self) -> int:
        try:
            return int(self.get_meta("schema_version", "1"))
        except ValueError:
            return 1

    # -- meta ------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, value, value),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def add_audit(self, action: str, target: str = "", detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO audit(at, action, target, detail) VALUES (?, ?, ?, ?)",
            (_utcnow(), action, target, detail),
        )

    def record_batch(self, kind: str, source: str, stats: ImportStats, started_at: str = "") -> int:
        """Record one job in the batch history; returns the new batch id."""
        cursor = self._conn.execute(
            "INSERT INTO batch(kind, source, started_at, finished_at, total, inserted, updated,"
            " skipped, errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                source,
                started_at or _utcnow(),
                _utcnow(),
                stats.total,
                stats.inserted,
                stats.updated,
                stats.skipped,
                stats.errors,
            ),
        )
        return int(cursor.lastrowid or 0)

    def record_items(self, batch_id: int, bkeys: Iterable[str]) -> int:
        """Associate the business keys a batch touched, so it can be tracked."""
        keys = [(batch_id, key) for key in bkeys]
        if not keys:
            return 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO batch_item(batch_id, bkey) VALUES (?, ?)", keys
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(keys)

    def batch_bkeys(self, batch_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT bkey FROM batch_item WHERE batch_id = ? ORDER BY bkey", (batch_id,)
        ).fetchall()
        return [row[0] for row in rows]

    def recent_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, kind, source, finished_at, total, inserted, updated, skipped, errors"
            " FROM batch ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = (
            "id",
            "kind",
            "source",
            "finished_at",
            "total",
            "inserted",
            "updated",
            "skipped",
            "errors",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # -- import ----------------------------------------------------------
    def import_qsos(
        self,
        qsos: Iterable[QSO],
        *,
        strategy: str = "merge",
        source: str = "",
        batch_size: int = 5000,
    ) -> ImportStats:
        """Import an iterable of QSOs with de-duplication.

        strategy: skip keeps the stored row, merge fills blanks and never loses
        a confirmation, overwrite replaces the stored row.
        """
        if strategy not in _STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy!r} (expected one of {_STRATEGIES})")
        stats = ImportStats()
        self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS _incoming(bkey TEXT PRIMARY KEY)")
        batch: list[QSO] = []
        for qso in qsos:
            if source and not qso.source:
                qso.source = source
            batch.append(qso)
            if len(batch) >= batch_size:
                self._flush(batch, strategy, stats)
                batch = []
        if batch:
            self._flush(batch, strategy, stats)
        return stats

    def _collapse(self, batch: list[QSO]) -> list[QSO]:
        """Merge records that share a business key inside one batch."""
        merged: dict[str, QSO] = {}
        for qso in batch:
            key = business_key(qso)
            current = merged.get(key)
            if current is None:
                merged[key] = qso
            else:
                merge_qso(current, qso)
        return list(merged.values())

    def _flush(self, batch: list[QSO], strategy: str, stats: ImportStats) -> None:
        records = self._collapse(batch)
        stats.total += len(batch)
        now = _utcnow()
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if strategy == "skip":
                before = conn.total_changes
                conn.executemany(
                    _INSERT_OR_IGNORE_SQL,
                    [_insert_params(q, business_key(q), now) for q in records],
                )
                inserted = conn.total_changes - before
                stats.inserted += inserted
                stats.skipped += len(records) - inserted
            else:
                conn.executemany(
                    "INSERT OR IGNORE INTO _incoming(bkey) VALUES (?)",
                    [(business_key(q),) for q in records],
                )
                existing: dict[str, QSO] = {}
                for row in conn.execute(f"{_SELECT_BASE} JOIN _incoming USING(bkey)"):
                    existing[row[0]] = _row_to_qso(row)
                new_rows: list[tuple[Any, ...]] = []
                upd_rows: list[tuple[Any, ...]] = []
                for qso in records:
                    key = business_key(qso)
                    old = existing.get(key)
                    if old is None:
                        new_rows.append(_insert_params(qso, key, now))
                        stats.inserted += 1
                    elif strategy == "overwrite":
                        upd_rows.append(_update_params(qso, key, now))
                        stats.updated += 1
                    else:
                        before_values = tuple(getattr(old, name) for name in COLUMNS)
                        merge_qso(old, qso)
                        after_values = tuple(getattr(old, name) for name in COLUMNS)
                        if before_values != after_values or old.extra:
                            upd_rows.append(_update_params(old, key, now))
                            stats.updated += 1
                        else:
                            stats.skipped += 1
                if new_rows:
                    conn.executemany(_INSERT_SQL, new_rows)
                if upd_rows:
                    conn.executemany(_UPDATE_SQL, upd_rows)
                conn.execute("DELETE FROM _incoming")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- queries ---------------------------------------------------------
    def count(self, flt: QsoFilter | None = None) -> int:
        where, params = flt.where() if flt else ("", [])
        row = self._conn.execute(f"SELECT COUNT(*) FROM qso{where}", params).fetchone()
        return int(row[0]) if row else 0

    def query(self, flt: QsoFilter | None = None) -> list[QSO]:
        where, params = flt.where() if flt else ("", [])
        sql = f"{_SELECT_BASE}{where} ORDER BY {flt.order_by if flt else 'qso_date, time_on'}"
        if flt and flt.limit:
            sql += " LIMIT ? OFFSET ?"
            params = [*params, flt.limit, flt.offset]
        return [_row_to_qso(row) for row in self._conn.execute(sql, params)]

    def iter_qsos(self, flt: QsoFilter | None = None, *, fetch: int = 1000) -> Iterator[QSO]:
        """Stream rows in batches so a huge result set never materialises at once."""
        where, params = flt.where() if flt else ("", [])
        order = flt.order_by if flt else "qso_date, time_on"
        cursor = self._conn.execute(f"{_SELECT_BASE}{where} ORDER BY {order}", params)
        while True:
            rows = cursor.fetchmany(fetch)
            if not rows:
                break
            for row in rows:
                yield _row_to_qso(row)

    def get(self, key: str) -> QSO | None:
        row = self._conn.execute(f"{_SELECT_BASE} WHERE bkey = ?", (key,)).fetchone()
        return _row_to_qso(row) if row else None

    def get_many(self, keys: Iterable[str]) -> list[QSO]:
        """Fetch several QSOs by business key in one round of IN queries."""
        wanted = [key for key in dict.fromkeys(keys) if key]
        found: list[QSO] = []
        for chunk in _chunks(wanted, 400):
            placeholders = ", ".join("?" * len(chunk))
            for row in self._conn.execute(f"{_SELECT_BASE} WHERE bkey IN ({placeholders})", chunk):
                found.append(_row_to_qso(row))
        return found

    def get_by_call(self, call: str, date: str = "", time_on: str = "") -> QSO | None:
        key = "|".join(("", call.strip().upper(), date.strip(), time_on.strip()[:4], "", ""))
        row = self._conn.execute(f"{_SELECT_BASE} WHERE bkey = ?", (key,)).fetchone()
        return _row_to_qso(row) if row else None

    def distinct(self, column: str, flt: QsoFilter | None = None) -> list[str]:
        if column not in COLUMNS:
            raise ValueError(f"unknown column: {column!r}")
        where, params = flt.where() if flt else ("", [])
        rows = self._conn.execute(
            f"SELECT DISTINCT {column} FROM qso{where} ORDER BY {column}", params
        ).fetchall()
        return [row[0] for row in rows if row[0]]

    def update_status(self, keys: Iterable[str], **fields: str) -> int:
        """Update whitelisted status fields for the given business keys."""
        allowed = {
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
            "notes",
            "comment",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"fields not updatable: {sorted(unknown)}")
        if not fields:
            return 0
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = [*fields.values(), _utcnow()]
        keys = list(keys)
        updated = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            sql = f"UPDATE qso SET {assignments}, updated_at = ? WHERE bkey = ?"
            cursor = self._conn.executemany(sql, [(*params, key) for key in keys])
            updated = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else len(keys)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return updated

    # -- card log (dispatch and return tracking) --------------------------
    def record_dispatch(
        self,
        bkeys: Iterable[str],
        *,
        sent_date: str,
        via: str = "",
        batch_id: int = 0,
        note: str = "",
        card_no: str = "",
        status: str = "sent",
    ) -> int:
        """Record that cards were posted, and mirror the state onto the QSOs.

        An entry still marked queued or printed is promoted in place; a card
        that was already sent creates a new row, so re-sends remain visible as
        history rather than overwriting the first send.
        """
        if status not in CARD_STATUSES:
            raise ValueError(f"status must be one of {CARD_STATUSES}, got {status!r}")
        keys = list(dict.fromkeys(key for key in bkeys if key))
        if not keys:
            return 0
        date = normalize_date(sent_date)
        now = _utcnow()
        calls = self._calls_for(keys)
        open_rows = self._latest_log_rows(keys)
        updates: list[tuple[Any, ...]] = []
        inserts: list[tuple[Any, ...]] = []
        for key in keys:
            latest = open_rows.get(key)
            if latest is not None and latest[1] in ("queued", "printed"):
                updates.append((status, date, via, note or latest[2], now, latest[0]))
            else:
                inserts.append(
                    (
                        key,
                        calls.get(key, ""),
                        card_no,
                        batch_id,
                        status,
                        date,
                        via,
                        "",
                        "",
                        note,
                        now,
                        now,
                    )
                )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if updates:
                self._conn.executemany(
                    "UPDATE card_log SET status = ?, sent_date = ?, sent_via = ?,"
                    " note = COALESCE(NULLIF(?, ''), note), updated_at = ? WHERE id = ?",
                    updates,
                )
            if inserts:
                self._conn.executemany(
                    "INSERT INTO card_log (bkey, call, card_no, batch_id, status, sent_date,"
                    " sent_via, returned_date, returned_via, note, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    inserts,
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        fields: dict[str, str] = {"qsl_sent": "Y"}
        if date:
            fields["qslsdate"] = date
        if via:
            fields["qsl_sent_via"] = via
        self.update_status(keys, **fields)
        return len(keys)

    def mark_returned(
        self,
        bkeys: Iterable[str],
        *,
        returned_date: str,
        via: str = "",
        note: str = "",
    ) -> int:
        """Record reply cards that arrived, updating the QSO status too."""
        keys = list(dict.fromkeys(key for key in bkeys if key))
        if not keys:
            return 0
        date = normalize_date(returned_date)
        now = _utcnow()
        calls = self._calls_for(keys)
        latest = self._latest_log_rows(keys)
        updates: list[tuple[Any, ...]] = []
        inserts: list[tuple[Any, ...]] = []
        for key in keys:
            row = latest.get(key)
            if row is None:
                inserts.append(
                    (key, calls.get(key, ""), "", 0, "returned", "", "", date, via, note, now, now)
                )
            else:
                updates.append((date, via, note, now, row[0]))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if updates:
                self._conn.executemany(
                    "UPDATE card_log SET status = 'returned', returned_date = ?,"
                    " returned_via = ?, note = COALESCE(NULLIF(?, ''), note), updated_at = ?"
                    " WHERE id = ?",
                    updates,
                )
            if inserts:
                self._conn.executemany(
                    "INSERT INTO card_log (bkey, call, card_no, batch_id, status, sent_date,"
                    " sent_via, returned_date, returned_via, note, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    inserts,
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        fields: dict[str, str] = {"qsl_rcvd": "Y"}
        if date:
            fields["qslrdate"] = date
        if via:
            fields["qsl_rcvd_via"] = via
        self.update_status(keys, **fields)
        return len(keys)

    def _calls_for(self, keys: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for chunk in _chunks(keys, 400):
            placeholders = ", ".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT bkey, call FROM qso WHERE bkey IN ({placeholders})", chunk
            )
            found.update({row[0]: row[1] for row in rows})
        return found

    def _latest_log_rows(self, keys: Sequence[str]) -> dict[str, tuple[int, str, str]]:
        """Latest card_log row per key as (id, status, note)."""
        found: dict[str, tuple[int, str, str]] = {}
        for chunk in _chunks(keys, 400):
            placeholders = ", ".join("?" * len(chunk))
            rows = self._conn.execute(
                "SELECT c.bkey, c.id, c.status, c.note FROM card_log c"
                " JOIN (SELECT bkey, MAX(id) AS mid FROM card_log WHERE bkey IN"
                f" ({placeholders}) GROUP BY bkey) m ON c.bkey = m.bkey AND c.id = m.mid",
                chunk,
            )
            found.update({row[0]: (int(row[1]), row[2], row[3]) for row in rows})
        return found

    def card_log(
        self,
        *,
        status: str = "",
        call: str = "",
        batch_id: int = 0,
        sent_from: str = "",
        sent_to: str = "",
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Dispatch history joined with the QSO, newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            if status not in CARD_STATUSES:
                raise ValueError(f"status must be one of {CARD_STATUSES}, got {status!r}")
            clauses.append("c.status = ?")
            params.append(status)
        if call:
            clauses.append("c.call LIKE ?")
            params.append(call.strip().upper() + "%")
        if batch_id:
            clauses.append("c.batch_id = ?")
            params.append(batch_id)
        if sent_from:
            clauses.append("c.sent_date >= ?")
            params.append(normalize_date(sent_from))
        if sent_to:
            clauses.append("c.sent_date <= ?")
            params.append(normalize_date(sent_to))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT c.id, c.bkey, c.call, q.qso_date, q.band, q.mode, q.country,"
            " c.sent_date, c.sent_via, c.status, c.returned_date, c.returned_via,"
            " c.batch_id, c.card_no, c.note FROM card_log c LEFT JOIN qso q ON q.bkey = c.bkey"
            f"{where} ORDER BY c.id DESC"
        )
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        keys = (
            "id",
            "bkey",
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
        return [dict(zip(keys, row, strict=True)) for row in self._conn.execute(sql, params)]

    def update_card_log(self, entry_id: int, **fields: str) -> bool:
        allowed = {
            "cart_no",
            "note",
            "card_no",
            "status",
            "sent_date",
            "sent_via",
            "returned_date",
            "returned_via",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"fields not updatable: {sorted(unknown)}")
        if not fields:
            return False
        for name in ("sent_date", "returned_date"):
            if name in fields:
                fields[name] = normalize_date(fields[name])
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cursor = self._conn.execute(
            f"UPDATE card_log SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), _utcnow(), entry_id),
        )
        return bool(cursor.rowcount)

    def dispatch_stats(self) -> dict[str, Any]:
        counts = {
            row[0]: int(row[1])
            for row in self._conn.execute("SELECT status, COUNT(*) FROM card_log GROUP BY status")
        }
        sent = counts.get("sent", 0) + counts.get("returned", 0)
        returned = counts.get("returned", 0)
        pending = counts.get("sent", 0) + counts.get("queued", 0) + counts.get("printed", 0)
        return {
            "total": sum(counts.values()),
            "sent": sent,
            "returned": returned,
            "pending": pending,
            "bounced": counts.get("bounced", 0),
            "by_status": counts,
            "return_rate": round(returned / sent, 4) if sent else 0.0,
        }

    def card_log_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Alias kept for callers that name the table rather than the concept."""
        return self.card_log(**kwargs)

    # -- maintenance -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        total = self.count()
        confirmed = self.count(QsoFilter(confirmed=True))
        needs_card = self.count(QsoFilter(needs_card=True))
        calls = self._conn.execute("SELECT COUNT(DISTINCT call) FROM qso").fetchone()[0]
        first = self._conn.execute("SELECT MIN(qso_date) FROM qso").fetchone()[0] or ""
        last = self._conn.execute("SELECT MAX(qso_date) FROM qso").fetchone()[0] or ""
        return {
            "total": total,
            "confirmed": confirmed,
            "needs_card": needs_card,
            "calls": int(calls),
            "first_date": first,
            "last_date": last,
            "path": self.path,
            "schema_version": self.get_meta("schema_version", "?"),
        }

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")

    def optimize(self) -> None:
        """Refresh planner statistics and compact the WAL."""
        self._conn.execute("PRAGMA optimize")
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
