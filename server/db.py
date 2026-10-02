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

-- ---------------------------------------------------------------------------
-- Billing
-- ---------------------------------------------------------------------------

-- One row per purchase attempt, whatever the payment channel. This is the only
-- place that records "money was supposed to arrive", so the manual channel,
-- Afdian and WeChat Pay all write here and nothing else needs to care.
CREATE TABLE IF NOT EXISTS payment_orders (
    id                TEXT PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose           TEXT    NOT NULL,           -- 'api_credit' | 'sigma_pro'
    amount_cny        REAL    NOT NULL,
    months            INTEGER NOT NULL DEFAULT 0, -- for 'sigma_pro'
    provider          TEXT    NOT NULL,           -- 'manual' | 'afdian' | 'wechat'
    provider_trade_no TEXT    UNIQUE,             -- UNIQUE => replayed callbacks are a no-op
    status            TEXT    NOT NULL,           -- 'pending' | 'paid' | 'failed' | 'refunded'
    created_at        TEXT    NOT NULL,
    paid_at           TEXT,
    payload_json      TEXT                        -- raw callback, kept for disputes
);
CREATE INDEX IF NOT EXISTS idx_orders_user   ON payment_orders (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON payment_orders (status);

-- Redeemable codes. Their whole point is to decouple "how the money was
-- collected" (WeChat transfer, Afdian, cash, a service provider) from "what the
-- user gets", so changing payment channel never touches product code.
CREATE TABLE IF NOT EXISTS redeem_codes (
    code         TEXT PRIMARY KEY,
    grants_json  TEXT    NOT NULL,                -- {"kind":"api_credit","amount_cny":50}
    note         TEXT,
    batch_id     TEXT,                            -- groups codes minted in one go
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT,
    redeemed_at  TEXT,
    redeemed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_codes_batch    ON redeem_codes (batch_id);
CREATE INDEX IF NOT EXISTS idx_codes_redeemed ON redeem_codes (redeemed_by, redeemed_at);

-- Sigma subscriptions (free / pro). Kept here so there is exactly one account
-- and one billing authority; Sigma asks the platform what a user is entitled to.
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    plan       TEXT NOT NULL DEFAULT 'free',      -- 'free' | 'pro'
    expires_at TEXT,                              -- NULL = never expires (staff)
    updated_at TEXT NOT NULL
);

-- Who changed what. Small, but an admin panel that hands out money needs it.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (created_at);

-- ---------------------------------------------------------------------------
-- API v1 objects (sdk/API_CONTRACT.md §2-§4). Ids are opaque, type-prefixed
-- strings rather than integers because they are handed to customers.
--
-- Deleting a user cascades to all three tables. Deleting a thread cascades to
-- its messages. Deleting an assistant only clears threads.assistant_id (the
-- thread keeps its own `model` and stays usable) -- see the note in the README.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assistants (
    id            TEXT    PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    instructions  TEXT    NOT NULL DEFAULT '',
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistants_user ON assistants (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS threads (
    id            TEXT    PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL,
    assistant_id  TEXT    REFERENCES assistants(id) ON DELETE SET NULL,
    model         TEXT,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_user      ON threads (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_assistant ON threads (assistant_id);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT    PRIMARY KEY,
    thread_id  TEXT    NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    model      TEXT,
    usage_json TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user   ON messages (user_id);
"""


# Columns added after the first release. SQLite has no
# "ALTER TABLE ... ADD COLUMN IF NOT EXISTS", so we inspect and patch on boot.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
]

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
    """Create the schema if it does not exist yet, then patch in new columns.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so columns
    added after the first release are applied here. Safe to call on every boot.
    """
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for table, column, definition in _MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, plan, expires_at, updated_at) "
            "SELECT id, 'free', NULL, ? FROM users",
            (iso(utcnow()),),
        )
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


def account_state(conn: sqlite3.Connection, user_id: int) -> dict:
    """Balance, the free allowance, and how much of it is gone.

    The free grant is tracked in **tokens**, not money, because it is advertised
    as "5,000 free tokens". A call is free while any allowance remains; the first
    call that starts after it is exhausted is charged in full. That boundary is
    deliberately simple rather than prorating across a single call.

    ``balance_cny`` is the sum of the credit ledger: top-ups are positive,
    charges are negative.
    """
    from server import pricing

    row = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens,
               COALESCE(SUM(cost_cny), 0) AS cost
        FROM usage_events WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    balance_row = conn.execute(
        "SELECT COALESCE(SUM(amount_cny), 0) AS balance FROM credit_ledger WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    tokens_used = int(row["tokens"])
    granted = pricing.FREE_TOKENS
    used_free = min(tokens_used, granted)

    return {
        "balance_cny": round(float(balance_row["balance"]), 6),
        "tokens_used": tokens_used,
        "cost_cny_total": round(float(row["cost"]), 6),
        "free_granted": granted,
        "free_used": used_free,
        "free_remaining": max(0, granted - used_free),
    }


def can_generate(conn: sqlite3.Connection, user_id: int) -> tuple[bool, str, dict]:
    """May this account generate right now?

    Allowed while the free allowance lasts, or while there is a positive balance.
    Returns ``(allowed, reason, state)`` so the caller can put real numbers in the
    402 body instead of a bare "insufficient balance".
    """
    state = account_state(conn, user_id)

    # Staff accounts are never gated. Checking the plan rather than a balance is
    # what makes "unlimited" actually unlimited instead of a very large number
    # that someone eventually exhausts and then has to top up again.
    row = conn.execute(
        "SELECT plan FROM subscriptions WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None and row["plan"] == "admin":
        return True, "admin", state

    if state["free_remaining"] > 0:
        return True, "free_allowance", state
    if state["balance_cny"] > 0:
        return True, "balance", state
    return False, "insufficient_balance", state


def grant_credit(
    conn: sqlite3.Connection, user_id: int, amount_cny: float, reason: str
) -> float:
    """Add money to an account and return the new balance."""
    if amount_cny <= 0:
        raise ValueError("amount_cny must be positive")
    conn.execute(
        "INSERT INTO credit_ledger (user_id, amount_cny, reason, created_at) VALUES (?, ?, ?, ?)",
        (user_id, round(float(amount_cny), 6), reason, iso(utcnow())),
    )
    conn.commit()
    return account_state(conn, user_id)["balance_cny"]


def record_usage(
    conn: sqlite3.Connection,
    user_id: int,
    api_key_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Persist one call, charge for it if the free allowance is gone, and return
    the cost that was recorded.

    This is the single write path for usage, so the free-allowance accounting
    cannot drift: every generation goes through here exactly once.
    """
    from server import pricing  # local import keeps this module dependency-free

    # Snapshot *before* inserting, so the call that exhausts the allowance is
    # still free and the next one is the first to be charged.
    free_before = account_state(conn, user_id)["free_remaining"]

    cost = pricing.cost_cny(model, input_tokens, output_tokens)
    now = iso(utcnow())
    conn.execute(
        """
        INSERT INTO usage_events
            (user_id, api_key_id, model, input_tokens, output_tokens, cost_cny, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, api_key_id, model, input_tokens, output_tokens, cost, now),
    )
    if api_key_id is not None:
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, api_key_id))

    if free_before <= 0 and cost > 0:
        conn.execute(
            "INSERT INTO credit_ledger (user_id, amount_cny, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, -round(cost, 6), "usage:%s" % model, now),
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


# --------------------------------------------------------------------------- #
# API v1 objects: assistants, threads, messages
#
# Everything below is additive. Ids are opaque strings; ordering is always
# ``created_at`` (second precision) with ``rowid`` as the tie-breaker, so two
# objects created in the same second still page deterministically.
# --------------------------------------------------------------------------- #

_ASSISTANT_COLUMNS = ("name", "model", "instructions", "metadata_json")
_THREAD_COLUMNS = ("title", "assistant_id", "model", "metadata_json")


def _page(
    conn: sqlite3.Connection,
    *,
    table: str,
    where: str,
    params: list,
    limit: int,
    after_row: sqlite3.Row | None,
    ascending: bool = False,
) -> list[sqlite3.Row]:
    """Up to ``limit`` rows, continuing after ``after_row`` in sort order.

    ``table``/``where`` are module-private literals, never user input.
    """
    operator = ">" if ascending else "<"
    direction = "ASC" if ascending else "DESC"

    sql = f"SELECT *, rowid AS _rowid FROM {table} WHERE {where}"
    args = list(params)
    if after_row is not None:
        sql += (
            f" AND (created_at {operator} ?"
            f" OR (created_at = ? AND rowid {operator} ?))"
        )
        args += [after_row["created_at"], after_row["created_at"], int(after_row["_rowid"])]
    sql += f" ORDER BY created_at {direction}, rowid {direction} LIMIT ?"
    args.append(int(limit))
    return conn.execute(sql, args).fetchall()


def _update(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    object_id: str,
    user_id: int,
    columns: tuple[str, ...],
    fields: dict,
) -> sqlite3.Row | None:
    """Apply a whitelisted subset of columns. Returns the fresh row, or None."""
    allowed = {name: fields[name] for name in columns if name in fields}
    if allowed:
        assignments = ", ".join(f"{name} = ?" for name in allowed)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {id_column} = ? AND user_id = ?",
            [*allowed.values(), object_id, user_id],
        )
    return conn.execute(
        f"SELECT *, rowid AS _rowid FROM {table} WHERE {id_column} = ? AND user_id = ?",
        (object_id, user_id),
    ).fetchone()


# -- assistants ------------------------------------------------------------- #

def create_assistant(
    conn: sqlite3.Connection,
    assistant_id: str,
    user_id: int,
    name: str,
    model: str,
    instructions: str,
    metadata_json: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO assistants
            (id, user_id, name, model, instructions, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (assistant_id, user_id, name, model, instructions, metadata_json, now, now),
    )
    conn.commit()


def get_assistant(
    conn: sqlite3.Connection, assistant_id: str, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT *, rowid AS _rowid FROM assistants WHERE id = ? AND user_id = ?",
        (assistant_id, user_id),
    ).fetchone()


def list_assistants(
    conn: sqlite3.Connection, user_id: int, limit: int, after_row: sqlite3.Row | None = None
) -> list[sqlite3.Row]:
    return _page(
        conn,
        table="assistants",
        where="user_id = ?",
        params=[user_id],
        limit=limit,
        after_row=after_row,
    )


def update_assistant(
    conn: sqlite3.Connection,
    assistant_id: str,
    user_id: int,
    fields: dict,
    now: str,
) -> sqlite3.Row | None:
    fields = {**fields, "updated_at": now}
    row = _update(
        conn,
        table="assistants",
        id_column="id",
        object_id=assistant_id,
        user_id=user_id,
        columns=_ASSISTANT_COLUMNS + ("updated_at",),
        fields=fields,
    )
    conn.commit()
    return row


def delete_assistant(conn: sqlite3.Connection, assistant_id: str, user_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM assistants WHERE id = ? AND user_id = ?", (assistant_id, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


# -- threads ---------------------------------------------------------------- #

def create_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    user_id: int,
    title: str,
    assistant_id: str | None,
    model: str | None,
    metadata_json: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO threads
            (id, user_id, title, assistant_id, model, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (thread_id, user_id, title, assistant_id, model, metadata_json, now, now),
    )
    conn.commit()


def get_thread(conn: sqlite3.Connection, thread_id: str, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT *, rowid AS _rowid FROM threads WHERE id = ? AND user_id = ?",
        (thread_id, user_id),
    ).fetchone()


def list_threads(
    conn: sqlite3.Connection, user_id: int, limit: int, after_row: sqlite3.Row | None = None
) -> list[sqlite3.Row]:
    return _page(
        conn,
        table="threads",
        where="user_id = ?",
        params=[user_id],
        limit=limit,
        after_row=after_row,
    )


def update_thread(
    conn: sqlite3.Connection, thread_id: str, user_id: int, fields: dict, now: str
) -> sqlite3.Row | None:
    fields = {**fields, "updated_at": now}
    row = _update(
        conn,
        table="threads",
        id_column="id",
        object_id=thread_id,
        user_id=user_id,
        columns=_THREAD_COLUMNS + ("updated_at",),
        fields=fields,
    )
    conn.commit()
    return row


def delete_thread(conn: sqlite3.Connection, thread_id: str, user_id: int) -> bool:
    """Deletes the thread and, by ON DELETE CASCADE, every message in it."""
    cursor = conn.execute(
        "DELETE FROM threads WHERE id = ? AND user_id = ?", (thread_id, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def touch_thread(conn: sqlite3.Connection, thread_id: str, now: str) -> None:
    conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id))
    conn.commit()


def message_counts(conn: sqlite3.Connection, thread_ids: list[str]) -> dict[str, int]:
    """``{thread_id: count}`` in one query, so listing threads is not N+1."""
    if not thread_ids:
        return {}
    placeholders = ", ".join("?" for _ in thread_ids)
    rows = conn.execute(
        f"SELECT thread_id, COUNT(*) AS n FROM messages "
        f"WHERE thread_id IN ({placeholders}) GROUP BY thread_id",
        list(thread_ids),
    ).fetchall()
    return {r["thread_id"]: int(r["n"]) for r in rows}


# -- messages --------------------------------------------------------------- #

def create_message(
    conn: sqlite3.Connection,
    message_id: str,
    thread_id: str,
    user_id: int,
    role: str,
    content: str,
    model: str | None,
    usage_json: str | None,
    now: str,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO messages
            (id, thread_id, user_id, role, content, model, usage_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, thread_id, user_id, role, content, model, usage_json, now),
    )
    conn.commit()
    return get_message(conn, message_id, user_id)  # type: ignore[return-value]


def get_message(
    conn: sqlite3.Connection, message_id: str, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT *, rowid AS _rowid FROM messages WHERE id = ? AND user_id = ?",
        (message_id, user_id),
    ).fetchone()


def list_messages(
    conn: sqlite3.Connection,
    thread_id: str,
    user_id: int,
    limit: int,
    after_row: sqlite3.Row | None = None,
    ascending: bool = False,
) -> list[sqlite3.Row]:
    return _page(
        conn,
        table="messages",
        where="thread_id = ? AND user_id = ?",
        params=[thread_id, user_id],
        limit=limit,
        after_row=after_row,
        ascending=ascending,
    )


def all_messages(
    conn: sqlite3.Connection, thread_id: str, user_id: int, limit: int = 100
) -> list[sqlite3.Row]:
    """The most recent ``limit`` messages, returned oldest-first (prompt order)."""
    rows = _page(
        conn,
        table="messages",
        where="thread_id = ? AND user_id = ?",
        params=[thread_id, user_id],
        limit=limit,
        after_row=None,
        ascending=False,
    )
    return list(reversed(rows))
