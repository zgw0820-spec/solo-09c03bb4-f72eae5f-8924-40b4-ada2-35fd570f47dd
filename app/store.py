"""SQLite persistence for diagnosis batches.

The database is the source of truth for results so they survive an API restart.
A batch is written once at start ("running") and once when it finishes. A batch
still "running" after a restart can no longer be trusted (its in-flight probe
tasks died with the old process), so startup marks it as needing a retest and
it keeps that label forever.

Batch ``seq`` values are assigned by the API and are strictly increasing. When
a slow batch finishes after a newer batch was created, its record is stored but
flagged ``superseded`` so it can never become the latest diagnosis result.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL DEFAULT 0,
    state_version  INTEGER NOT NULL,
    status         TEXT NOT NULL,
    outcome        TEXT,
    created_at     TEXT NOT NULL,
    completed_at   TEXT,
    summary        TEXT,
    superseded     INTEGER NOT NULL DEFAULT 0,
    superseded_by  INTEGER,
    result_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_seq ON batches(seq DESC);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    """Small, thread-safe wrapper around one SQLite file."""

    def __init__(self, path: str, max_stored_batches: int = 200) -> None:
        self.path = path
        self.max_stored_batches = max(1, max_stored_batches)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- writes ---------------------------------------------------------
    def create_running_batch(self, batch_id: str, seq: int, state_version: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO batches (batch_id, seq, state_version, status, created_at)"
                " VALUES (?, ?, ?, 'running', ?)",
                (batch_id, seq, state_version, utcnow()),
            )

    def finish_batch(
        self,
        batch_id: str,
        *,
        state_version: int,
        status: str,
        outcome: str,
        summary: str,
        result: dict[str, Any] | None,
        superseded_by: int | None = None,
    ) -> None:
        """Store the terminal state.

        ``superseded_by`` is recorded only when this batch finished after a
        younger batch had already been created; that is the only case where a
        late response genuinely loses a race.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE batches SET status = ?, outcome = ?, summary = ?,"
                " completed_at = ?, superseded = ?, superseded_by = ?,"
                " result_json = ? WHERE batch_id = ?",
                (
                    status,
                    outcome,
                    summary,
                    utcnow(),
                    1 if superseded_by is not None else 0,
                    superseded_by,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    batch_id,
                ),
            )
            conn.execute(
                "DELETE FROM batches WHERE status != 'running' AND batch_id NOT IN ("
                " SELECT batch_id FROM batches WHERE status != 'running'"
                " ORDER BY seq DESC LIMIT ?)",
                (self.max_stored_batches,),
            )

    def mark_all_running_interrupted(self, reason: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE batches SET status = 'interrupted', outcome = 'needs_retest',"
                " summary = ?, completed_at = ? WHERE status = 'running'",
                (reason, utcnow()),
            )
            return cur.rowcount

    # -- meta counters --------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- reads ----------------------------------------------------------
    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return _row_to_dict(row)

    def latest_batch(self, *, completed_only: bool = False) -> dict[str, Any] | None:
        query = "SELECT * FROM batches"
        if completed_only:
            query += " WHERE status = 'completed'"
        query += " ORDER BY seq DESC LIMIT 1"
        with self._lock, self._connect() as conn:
            row = conn.execute(query).fetchone()
        return _row_to_dict(row)

    def max_seq(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM batches").fetchone()
        return int(row["m"])

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    raw = data.pop("result_json", None)
    data["result"] = json.loads(raw) if raw else None
    return data


__all__ = ["Store", "utcnow"]
