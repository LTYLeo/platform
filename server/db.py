"""SQLite storage layer.

Deliberately dependency-free: :mod:`sqlite3` is in the standard library and the
whole database is a single file, which keeps deployment to "copy one directory".

All timestamps are stored as UTC ISO-8601 strings so they sort lexicographically.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique
    ON users (lower(email));

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    user_agent TEXT,
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions (token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions (user_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    key_prefix   TEXT    NOT NULL,
    key_hash     TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);

-- One row per login attempt, used to throttle credential stuffing.
CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT    NOT NULL,
    ip           TEXT    NOT NULL,
    attempted_at TEXT    NOT NULL,
    succeeded    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_lookup
    ON login_attempts (email, ip, attempted_at);

-- One row per billed model call. Written by record_usage() once the inference
-- endpoint exists; the dashboard aggregates whatever is here.
CREATE TABLE IF NOT EXISTS usage_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    api_key_id    INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cny      REAL    NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_key       ON usage_events (api_key_id);

-- Money in / money out. Balance is the sum; nothing writes here until top-ups
-- or charges are implemented, so a fresh account correctly reports ¥0.00.
CREATE TABLE IF NOT EXISTS credit_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_cny REAL    NOT NULL,
    reason     TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON credit_ledger (user_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    """Open a connection for one request.

    ``check_same_thread=False`` is required because FastAPI executes synchronous
    dependencies and the endpoint in a threadpool, and they are not guaranteed to
    land on the same worker thread. Each request gets its own connection and uses
    it sequentially, so no connection is ever touched concurrently.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the schema if it does not exist yet. Safe to call on every boot."""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, always closed."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #

MAX_FAILED_ATTEMPTS = 8
LOCKOUT_WINDOW = timedelta(minutes=15)


def recent_failures(conn: sqlite3.Connection, email: str, ip: str) -> int:
    since = iso(utcnow() - LOCKOUT_WINDOW)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM login_attempts
        WHERE lower(email) = lower(?) AND ip = ?
          AND succeeded = 0 AND attempted_at >= ?
        """,
        (email, ip, since),
    ).fetchone()
    return int(row["n"]) if row else 0


def record_attempt(conn: sqlite3.Connection, email: str, ip: str, ok: bool) -> None:
    conn.execute(
        "INSERT INTO login_attempts (email, ip, attempted_at, succeeded) VALUES (?, ?, ?, ?)",
        (email, ip, iso(utcnow()), 1 if ok else 0),
    )
    # Opportunistic cleanup so the table cannot grow without bound.
    conn.execute(
        "DELETE FROM login_attempts WHERE attempted_at < ?",
        (iso(utcnow() - timedelta(days=7)),),
    )
    conn.commit()


def clear_failures(conn: sqlite3.Connection, email: str, ip: str) -> None:
    conn.execute(
        "DELETE FROM login_attempts WHERE lower(email) = lower(?) AND ip = ? AND succeeded = 0",
        (email, ip),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Usage metering
# --------------------------------------------------------------------------- #

def month_start() -> str:
    now = utcnow()
    return iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def record_usage(
    conn: sqlite3.Connection,
    user_id: int,
    api_key_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Persist one billed call. Returns the cost that was recorded.

    This is the single write path for usage; the future inference endpoint calls
    it once per completion.
    """
    from server import pricing  # local import keeps this module dependency-free

    cost = pricing.cost_cny(model, input_tokens, output_tokens)
    conn.execute(
        """
        INSERT INTO usage_events
            (user_id, api_key_id, model, input_tokens, output_tokens, cost_cny, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, api_key_id, model, input_tokens, output_tokens, cost, iso(utcnow())),
    )
    if api_key_id is not None:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (iso(utcnow()), api_key_id)
        )
    conn.commit()
    return cost


def find_api_key(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    """Look up a live API key by the SHA-256 of its plaintext."""
    return conn.execute(
        """
        SELECT k.*, u.is_active AS user_active
        FROM api_keys k
        JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = ? AND k.revoked_at IS NULL
        """,
        (fingerprint,),
    ).fetchone()


def usage_summary(conn: sqlite3.Connection, user_id: int) -> dict:
    """Everything the dashboard needs, computed from real rows."""
    from server import pricing

    since = month_start()

    month = conn.execute(
        """
        SELECT COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0)  AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_cny), 0)      AS cost
        FROM usage_events
        WHERE user_id = ? AND created_at >= ?
        """,
        (user_id, since),
    ).fetchone()

    lifetime = conn.execute(
        """
        SELECT COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens
        FROM usage_events WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    balance_row = conn.execute(
        "SELECT COALESCE(SUM(amount_cny), 0) AS balance FROM credit_ledger WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    by_model = conn.execute(
        """
        SELECT model,
               COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens,
               COALESCE(SUM(cost_cny), 0) AS cost
        FROM usage_events
        WHERE user_id = ? AND created_at >= ?
        GROUP BY model
        ORDER BY tokens DESC
        """,
        (user_id, since),
    ).fetchall()

    used_free = min(int(lifetime["tokens"]), pricing.FREE_TOKENS)

    return {
        "period": since[:7],
        "requests_this_month": int(month["requests"]),
        "input_tokens_this_month": int(month["input_tokens"]),
        "output_tokens_this_month": int(month["output_tokens"]),
        "tokens_this_month": int(month["input_tokens"]) + int(month["output_tokens"]),
        "cost_cny_this_month": round(float(month["cost"]), 6),
        "requests_total": int(lifetime["requests"]),
        "tokens_total": int(lifetime["tokens"]),
        "balance_cny": round(float(balance_row["balance"]), 2),
        "free_tokens": {
            "granted": pricing.FREE_TOKENS,
            "used": used_free,
            "remaining": max(0, pricing.FREE_TOKENS - used_free),
        },
        "by_model": [
            {
                "model": r["model"],
                "requests": int(r["requests"]),
                "tokens": int(r["tokens"]),
                "cost_cny": round(float(r["cost"]), 6),
            }
            for r in by_model
        ],
    }
