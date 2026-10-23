#!/usr/bin/env python3
"""SQLite-backed virtual API keys and lightweight usage accounting.

Token quotas remain for the local embedding model so existing keys continue to
work.  New, billable upstream models use integer micro-USD credits; avoiding
floating point keeps concurrent debit operations exact.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 3
MICRO_USD_PER_USD = 1_000_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"mgw_{uuid.uuid4().hex[:12]}_{secrets.token_hex(32)}"


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _add_column_if_missing(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_database(db_path: Path) -> None:
    """Create the database and migrate the original token-only schema in place."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                quota_tokens INTEGER CHECK (quota_tokens IS NULL OR quota_tokens >= 0),
                used_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
                reserved_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
                credit_enabled INTEGER NOT NULL DEFAULT 0 CHECK (credit_enabled IN (0, 1)),
                credit_microusd INTEGER CHECK (credit_microusd IS NULL OR credit_microusd >= 0),
                spent_microusd INTEGER NOT NULL DEFAULT 0 CHECK (spent_microusd >= 0),
                reserved_microusd INTEGER NOT NULL DEFAULT 0 CHECK (reserved_microusd >= 0),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT NOT NULL REFERENCES api_keys(id),
                request_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                finished_at TEXT,
                input_count INTEGER NOT NULL,
                requested_tokens INTEGER NOT NULL,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                charged_tokens INTEGER NOT NULL DEFAULT 0,
                model_id TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_microusd INTEGER NOT NULL DEFAULT 0,
                charged_microusd INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                http_status INTEGER,
                latency_ms INTEGER,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_usage_events_key_time
                ON usage_events(key_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_usage_events_status
                ON usage_events(status);
            """
        )
        # SQLite cannot add a CHECK constraint via ALTER TABLE. Validation is
        # performed by the public functions below for migrated databases.
        _add_column_if_missing(
            connection, "api_keys", "credit_enabled", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(connection, "api_keys", "credit_microusd", "INTEGER")
        _add_column_if_missing(
            connection, "api_keys", "spent_microusd", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(
            connection, "api_keys", "reserved_microusd", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(connection, "usage_events", "model_id", "TEXT")
        _add_column_if_missing(
            connection, "usage_events", "input_tokens", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(
            connection, "usage_events", "output_tokens", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column_if_missing(
            connection,
            "usage_events",
            "estimated_microusd",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            connection,
            "usage_events",
            "charged_microusd",
            "INTEGER NOT NULL DEFAULT 0",
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


@dataclass(frozen=True)
class QuotaSnapshot:
    key_id: str
    name: str
    quota_tokens: int | None
    used_tokens: int
    reserved_tokens: int
    credit_enabled: bool = False
    credit_microusd: int | None = None
    spent_microusd: int = 0
    reserved_microusd: int = 0

    @property
    def remaining_tokens(self) -> int | None:
        if self.quota_tokens is None:
            return None
        return max(0, self.quota_tokens - self.used_tokens - self.reserved_tokens)

    @property
    def remaining_microusd(self) -> int | None:
        if self.credit_microusd is None:
            return None
        return max(0, self.credit_microusd - self.spent_microusd - self.reserved_microusd)


class InvalidAPIKey(Exception):
    pass


class DisabledAPIKey(Exception):
    pass


class QuotaExceeded(Exception):
    def __init__(self, snapshot: QuotaSnapshot, requested_tokens: int):
        self.snapshot = snapshot
        self.requested_tokens = requested_tokens
        super().__init__("token quota exceeded")


class CreditExceeded(Exception):
    def __init__(self, snapshot: QuotaSnapshot, requested_microusd: int):
        self.snapshot = snapshot
        self.requested_microusd = requested_microusd
        super().__init__("credit balance exceeded")


class CreditNotEnabled(Exception):
    def __init__(self, snapshot: QuotaSnapshot):
        self.snapshot = snapshot
        super().__init__("dollar credit is not configured for this key")


def _snapshot(row: sqlite3.Row) -> QuotaSnapshot:
    return QuotaSnapshot(
        key_id=row["id"],
        name=row["name"],
        quota_tokens=row["quota_tokens"],
        used_tokens=row["used_tokens"],
        reserved_tokens=row["reserved_tokens"],
        credit_enabled=bool(row["credit_enabled"]),
        credit_microusd=row["credit_microusd"],
        spent_microusd=row["spent_microusd"],
        reserved_microusd=row["reserved_microusd"],
    )


def _require_enabled(connection: sqlite3.Connection, api_key: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM api_keys WHERE key_hash = ?", (hash_api_key(api_key),)
    ).fetchone()
    if row is None:
        raise InvalidAPIKey()
    if not row["enabled"]:
        raise DisabledAPIKey()
    return row


def create_key(
    db_path: Path,
    name: str,
    quota_tokens: int | None = None,
    api_key: str | None = None,
    credit_microusd: int | None = None,
    credit_enabled: bool | None = None,
) -> tuple[str, QuotaSnapshot]:
    if not name.strip():
        raise ValueError("key name cannot be empty")
    if quota_tokens is not None and quota_tokens < 0:
        raise ValueError("token quota must be non-negative or unlimited")
    if credit_microusd is not None and credit_microusd < 0:
        raise ValueError("credit must be non-negative or unlimited")
    if credit_enabled is None:
        credit_enabled = credit_microusd is not None
    api_key = api_key or generate_api_key()
    now = utc_now()
    key_id = uuid.uuid4().hex
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO api_keys(
                id, name, key_hash, key_prefix, quota_tokens, used_tokens,
                reserved_tokens, credit_enabled, credit_microusd, spent_microusd,
                reserved_microusd, enabled, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 0, 0, ?, ?, 0, 0, 1, ?, ?)
            """,
            (
                key_id,
                name.strip(),
                hash_api_key(api_key),
                api_key[:17],
                quota_tokens,
                int(credit_enabled),
                credit_microusd,
                now,
                now,
            ),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        connection.commit()
    return api_key, _snapshot(row)


def authenticate(db_path: Path, api_key: str) -> QuotaSnapshot:
    if not api_key:
        raise InvalidAPIKey()
    with connect(db_path) as connection:
        row = _require_enabled(connection, api_key)
    return _snapshot(row)


def reserve_tokens(
    db_path: Path,
    api_key: str,
    request_id: str,
    input_count: int,
    token_count: int,
) -> QuotaSnapshot:
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_enabled(connection, api_key)
        except (InvalidAPIKey, DisabledAPIKey):
            connection.rollback()
            raise
        snapshot = _snapshot(row)
        remaining = snapshot.remaining_tokens
        if remaining is not None and token_count > remaining:
            connection.execute(
                """
                INSERT INTO usage_events(
                    key_id, request_id, occurred_at, finished_at, input_count,
                    requested_tokens, reserved_tokens, charged_tokens,
                    status, http_status, error
                ) VALUES(?, ?, ?, ?, ?, ?, 0, 0, 'rejected_quota', 429, ?)
                """,
                (
                    snapshot.key_id,
                    request_id,
                    now,
                    now,
                    input_count,
                    token_count,
                    f"remaining={remaining}",
                ),
            )
            connection.commit()
            raise QuotaExceeded(snapshot, token_count)

        connection.execute(
            "UPDATE api_keys SET reserved_tokens = reserved_tokens + ?, updated_at = ? WHERE id = ?",
            (token_count, now, snapshot.key_id),
        )
        connection.execute(
            """
            INSERT INTO usage_events(
                key_id, request_id, occurred_at, input_count, requested_tokens,
                reserved_tokens, charged_tokens, status
            ) VALUES(?, ?, ?, ?, ?, ?, 0, 'reserved')
            """,
            (snapshot.key_id, request_id, now, input_count, token_count, token_count),
        )
        updated = connection.execute("SELECT * FROM api_keys WHERE id = ?", (snapshot.key_id,)).fetchone()
        connection.commit()
    return _snapshot(updated)


def finalize_reservation(
    db_path: Path,
    request_id: str,
    success: bool,
    http_status: int,
    latency_ms: int,
    error: str | None = None,
) -> QuotaSnapshot:
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        event = connection.execute(
            "SELECT * FROM usage_events WHERE request_id = ?", (request_id,)
        ).fetchone()
        if event is None or event["status"] != "reserved":
            connection.rollback()
            raise RuntimeError(f"no active reservation for {request_id}")
        reserved = event["reserved_tokens"]
        charged = reserved if success else 0
        connection.execute(
            """
            UPDATE api_keys
            SET reserved_tokens = reserved_tokens - ?,
                used_tokens = used_tokens + ?,
                last_used_at = CASE WHEN ? > 0 THEN ? ELSE last_used_at END,
                updated_at = ?
            WHERE id = ? AND reserved_tokens >= ?
            """,
            (reserved, charged, charged, now, now, event["key_id"], reserved),
        )
        connection.execute(
            """
            UPDATE usage_events
            SET finished_at = ?, charged_tokens = ?, status = ?, http_status = ?,
                latency_ms = ?, error = ?
            WHERE request_id = ?
            """,
            (
                now,
                charged,
                "success" if success else "backend_error",
                http_status,
                latency_ms,
                error,
                request_id,
            ),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (event["key_id"],)).fetchone()
        connection.commit()
    return _snapshot(row)


def reserve_credit(
    db_path: Path,
    api_key: str,
    request_id: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    reserve_microusd: int,
) -> QuotaSnapshot:
    """Atomically reserve an upper-bound cost before sending an upstream request."""
    if min(input_tokens, output_tokens, reserve_microusd) < 0:
        raise ValueError("usage values cannot be negative")
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_enabled(connection, api_key)
        except (InvalidAPIKey, DisabledAPIKey):
            connection.rollback()
            raise
        snapshot = _snapshot(row)
        if not snapshot.credit_enabled:
            connection.rollback()
            raise CreditNotEnabled(snapshot)
        remaining = snapshot.remaining_microusd
        if remaining is not None and reserve_microusd > remaining:
            connection.execute(
                """
                INSERT INTO usage_events(
                    key_id, request_id, occurred_at, finished_at, input_count,
                    requested_tokens, reserved_tokens, charged_tokens, model_id,
                    input_tokens, output_tokens, estimated_microusd,
                    charged_microusd, status, http_status, error
                ) VALUES(?, ?, ?, ?, 0, ?, 0, 0, ?, ?, ?, ?, 0,
                         'rejected_credit', 429, ?)
                """,
                (
                    snapshot.key_id,
                    request_id,
                    now,
                    now,
                    input_tokens + output_tokens,
                    model_id,
                    input_tokens,
                    output_tokens,
                    reserve_microusd,
                    f"remaining_microusd={remaining}",
                ),
            )
            connection.commit()
            raise CreditExceeded(snapshot, reserve_microusd)

        connection.execute(
            "UPDATE api_keys SET reserved_microusd = reserved_microusd + ?, updated_at = ? WHERE id = ?",
            (reserve_microusd, now, snapshot.key_id),
        )
        connection.execute(
            """
            INSERT INTO usage_events(
                key_id, request_id, occurred_at, input_count, requested_tokens,
                reserved_tokens, charged_tokens, model_id, input_tokens,
                output_tokens, estimated_microusd, charged_microusd, status
            ) VALUES(?, ?, ?, 0, ?, 0, 0, ?, ?, ?, ?, 0, 'reserved_credit')
            """,
            (
                snapshot.key_id,
                request_id,
                now,
                input_tokens + output_tokens,
                model_id,
                input_tokens,
                output_tokens,
                reserve_microusd,
            ),
        )
        updated = connection.execute("SELECT * FROM api_keys WHERE id = ?", (snapshot.key_id,)).fetchone()
        connection.commit()
    return _snapshot(updated)


def finalize_credit_reservation(
    db_path: Path,
    request_id: str,
    success: bool,
    http_status: int,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    charged_microusd: int = 0,
    error: str | None = None,
) -> QuotaSnapshot:
    """Release reserved credit and record the exact cost reported by upstream."""
    if min(input_tokens, output_tokens, charged_microusd) < 0:
        raise ValueError("usage values cannot be negative")
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        event = connection.execute(
            "SELECT * FROM usage_events WHERE request_id = ?", (request_id,)
        ).fetchone()
        if event is None or event["status"] != "reserved_credit":
            connection.rollback()
            raise RuntimeError(f"no active credit reservation for {request_id}")
        reserved = event["estimated_microusd"]
        charged = charged_microusd if success else 0
        connection.execute(
            """
            UPDATE api_keys
            SET reserved_microusd = reserved_microusd - ?,
                spent_microusd = spent_microusd + ?,
                last_used_at = CASE WHEN ? > 0 THEN ? ELSE last_used_at END,
                updated_at = ?
            WHERE id = ? AND reserved_microusd >= ?
            """,
            (reserved, charged, charged, now, now, event["key_id"], reserved),
        )
        connection.execute(
            """
            UPDATE usage_events
            SET finished_at = ?, input_tokens = ?, output_tokens = ?,
                charged_microusd = ?, status = ?, http_status = ?, latency_ms = ?,
                error = ?
            WHERE request_id = ?
            """,
            (
                now,
                input_tokens,
                output_tokens,
                charged,
                "success" if success else "backend_error",
                http_status,
                latency_ms,
                error,
                request_id,
            ),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (event["key_id"],)).fetchone()
        connection.commit()
    return _snapshot(row)


def recover_abandoned_reservations(db_path: Path) -> int:
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        count = connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE status IN ('reserved', 'reserved_credit')"
        ).fetchone()[0]
        connection.execute("UPDATE api_keys SET reserved_tokens = 0, reserved_microusd = 0")
        connection.execute(
            """
            UPDATE usage_events
            SET status = 'aborted_restart', finished_at = ?, charged_tokens = 0,
                charged_microusd = 0, error = 'gateway restarted before completion'
            WHERE status IN ('reserved', 'reserved_credit')
            """,
            (now,),
        )
        connection.commit()
    return count


def find_key(db_path: Path, name: str) -> sqlite3.Row:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE name = ? OR id = ?", (name, name)
        ).fetchone()
    if row is None:
        raise KeyError(name)
    return row


def list_keys(db_path: Path) -> list[sqlite3.Row]:
    with connect(db_path) as connection:
        return connection.execute("SELECT * FROM api_keys ORDER BY created_at, name").fetchall()


def set_quota(db_path: Path, name: str, quota_tokens: int | None) -> None:
    if quota_tokens is not None and quota_tokens < 0:
        raise ValueError("quota must be non-negative or unlimited")
    now = utc_now()
    with connect(db_path) as connection:
        cursor = connection.execute(
            "UPDATE api_keys SET quota_tokens = ?, updated_at = ? WHERE name = ? OR id = ?",
            (quota_tokens, now, name, name),
        )
        if cursor.rowcount != 1:
            raise KeyError(name)


def add_quota(db_path: Path, name: str, tokens: int) -> None:
    if tokens <= 0:
        raise ValueError("tokens must be positive")
    now = utc_now()
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE name = ? OR id = ?", (name, name)
        ).fetchone()
        if row is None:
            raise KeyError(name)
        if row["quota_tokens"] is None:
            raise ValueError("cannot add tokens to an unlimited key; set a finite quota first")
        connection.execute(
            "UPDATE api_keys SET quota_tokens = quota_tokens + ?, updated_at = ? WHERE id = ?",
            (tokens, now, row["id"]),
        )


def set_credit(db_path: Path, name: str, credit_microusd: int | None) -> None:
    if credit_microusd is not None and credit_microusd < 0:
        raise ValueError("credit must be non-negative or unlimited")
    now = utc_now()
    with connect(db_path) as connection:
        cursor = connection.execute(
            "UPDATE api_keys SET credit_enabled = 1, credit_microusd = ?, updated_at = ? WHERE name = ? OR id = ?",
            (credit_microusd, now, name, name),
        )
        if cursor.rowcount != 1:
            raise KeyError(name)


def add_credit(db_path: Path, name: str, microusd: int) -> None:
    if microusd <= 0:
        raise ValueError("credit must be positive")
    now = utc_now()
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE name = ? OR id = ?", (name, name)
        ).fetchone()
        if row is None:
            raise KeyError(name)
        if not row["credit_enabled"]:
            raise ValueError("credit is not configured; use set-credit before topup")
        if row["credit_microusd"] is None:
            raise ValueError("cannot add credit to an unlimited key; set a finite credit first")
        connection.execute(
            "UPDATE api_keys SET credit_microusd = credit_microusd + ?, updated_at = ? WHERE id = ?",
            (microusd, now, row["id"]),
        )


def set_enabled(db_path: Path, name: str, enabled: bool) -> None:
    now = utc_now()
    with connect(db_path) as connection:
        cursor = connection.execute(
            "UPDATE api_keys SET enabled = ?, updated_at = ? WHERE name = ? OR id = ?",
            (int(enabled), now, name, name),
        )
        if cursor.rowcount != 1:
            raise KeyError(name)


def rotate_key(db_path: Path, name: str) -> str:
    new_key = generate_api_key()
    now = utc_now()
    with connect(db_path) as connection:
        cursor = connection.execute(
            """
            UPDATE api_keys
            SET key_hash = ?, key_prefix = ?, updated_at = ?
            WHERE name = ? OR id = ?
            """,
            (hash_api_key(new_key), new_key[:17], now, name, name),
        )
        if cursor.rowcount != 1:
            raise KeyError(name)
    return new_key


def usage_logs(db_path: Path, name: str, limit: int) -> list[sqlite3.Row]:
    key = find_key(db_path, name)
    with connect(db_path) as connection:
        return connection.execute(
            "SELECT * FROM usage_events WHERE key_id = ? ORDER BY id DESC LIMIT ?",
            (key["id"], limit),
        ).fetchall()
