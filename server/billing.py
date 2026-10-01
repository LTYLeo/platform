"""Billing: payment orders, redeemable codes, and subscriptions.

Design
------
There are **two consumption models** and **one payment layer**.

* Sigma (the chat platform) is subscription-based: free or pro, billed monthly.
* The Developer Platform API is prepaid usage-based: top up, then spend.

Both start with the same thing — a ``payment_orders`` row — and end with the same
thing — a fulfilment that either adds credit or extends a subscription.

Why redeem codes exist
----------------------
Collecting money in China as an individual is the awkward part: personal QR codes
may not be used for business collection, a merchant account normally needs a
business licence, and the channels that *do* work for individuals (Afdian, a
tunnel-vendor "micro merchant") come and go. So the code path is split:

    money arrives however it arrives  ->  a code is issued  ->  the user redeems it

Switching or adding a payment channel therefore only changes *how a code is
issued*, never the product code. ``ManualProvider`` needs no third party at all:
an admin confirms a transfer and the code/credit is granted by hand.

Providers
---------
``PaymentProvider`` is deliberately tiny. Adding WeChat Pay Native later means
writing one subclass and registering it; nothing above this module changes.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Protocol

from server import db

# --------------------------------------------------------------------------- #
# Ids and codes
# --------------------------------------------------------------------------- #

ORDER_PREFIX = "ord_"
BATCH_PREFIX = "bat_"

# No 0/O/1/I/L: these get read aloud and typed by hand.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 3
CODE_GROUP_LEN = 4
CODE_PREFIX = "TAI"

PURPOSES = ("api_credit", "sigma_pro")
PROVIDERS = ("manual", "afdian", "wechat")


def new_order_id() -> str:
    return ORDER_PREFIX + secrets.token_urlsafe(12)


def new_batch_id() -> str:
    return BATCH_PREFIX + secrets.token_urlsafe(9)


def new_code() -> str:
    groups = [
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LEN))
        for _ in range(CODE_GROUPS)
    ]
    return CODE_PREFIX + "-" + "-".join(groups)


def normalize_code(raw: str) -> str:
    """Uppercase, drop separators, and return the canonical form (or "").

    Users paste codes from chat with spaces and type them lowercase, so being
    forgiving here avoids a support conversation.

    The prefix is removed *before* filtering by the alphabet, not after: the
    alphabet deliberately excludes I/L/O/0/1 to avoid misreads, and "TAI"
    contains an I. Filtering first would silently eat it and reject every code.
    """
    text = (raw or "").strip().upper()
    if text.startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):]

    cleaned = "".join(ch for ch in text if ch in CODE_ALPHABET)
    if len(cleaned) != CODE_GROUPS * CODE_GROUP_LEN:
        return ""
    return CODE_PREFIX + "-" + "-".join(
        cleaned[i:i + CODE_GROUP_LEN] for i in range(0, len(cleaned), CODE_GROUP_LEN)
    )


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

def audit(conn: sqlite3.Connection, actor_id: int | None, action: str, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor_id, action, detail_json, created_at) VALUES (?, ?, ?, ?)",
        (actor_id, action, json.dumps(detail or {}, ensure_ascii=False), db.iso(db.utcnow())),
    )


# --------------------------------------------------------------------------- #
# Payment providers
# --------------------------------------------------------------------------- #

class PaymentIntent(dict):
    """What the front-end needs to render a payment step.

    ``kind`` is ``'external_link'`` (send the user somewhere), ``'qrcode'``
    (render ``payload`` as a QR) or ``'manual'`` (show transfer instructions).
    """


class PaymentProvider(Protocol):
    name: str

    def create_intent(self, order: sqlite3.Row) -> PaymentIntent:
        """Return how the user should pay for ``order``."""

    def verify_callback(self, headers: dict, body: bytes) -> tuple[bool, str | None, dict]:
        """Validate an inbound payment notification.

        Returns ``(ok, provider_trade_no, parsed)``. A provider that has no
        callback at all returns ``(False, None, {})`` and relies on an admin.
        """


class ManualProvider:
    """No third party. An admin confirms the transfer by hand.

    This is the only provider that works before any account is set up, and it
    stays useful afterwards: bank transfers, cash, and "my friend paid for me"
    all end up here.
    """

    name = "manual"

    def create_intent(self, order: sqlite3.Row) -> PaymentIntent:
        return PaymentIntent(
            kind="manual",
            note=(
                "Pay ¥%.2f by WeChat, then give the reference %s to an admin so the "
                "order can be confirmed." % (order["amount_cny"], order["id"])
            ),
        )

    def verify_callback(self, headers: dict, body: bytes) -> tuple[bool, str | None, dict]:
        return False, None, {}


class AfdianProvider:
    """Afdian (爱发电). No business licence needed; money is settled by them.

    Wiring it up requires the creator's ``user_id`` and API ``token`` from
    https://afdian.com/dashboard/dev — set ``TAI_AFDIAN_TOKEN`` and
    ``TAI_AFDIAN_USER_ID``. Until those exist this provider refuses to issue an
    intent rather than producing a link that cannot work.

    The callback signature is an MD5 over the token and the raw parameters; keep
    the verification in one place so it is easy to audit.
    """

    name = "afdian"

    def __init__(self, token: str | None = None, user_id: str | None = None, page: str | None = None):
        self.token = token
        self.user_id = user_id
        self.page = page or "https://afdian.com/a/tai_research"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.user_id)

    def create_intent(self, order: sqlite3.Row) -> PaymentIntent:
        if not self.configured:
            raise RuntimeError(
                "Afdian is not configured: set TAI_AFDIAN_TOKEN and TAI_AFDIAN_USER_ID"
            )
        # Afdian has no per-order checkout: the user picks an amount on the page,
        # so the order id has to travel in the remark and be matched by hand or by
        # the callback. Documented rather than hidden.
        return PaymentIntent(
            kind="external_link",
            url=self.page,
            note="Put %s in the message so the payment can be matched." % order["id"],
        )

    def verify_callback(self, headers: dict, body: bytes) -> tuple[bool, str | None, dict]:
        if not self.token:
            return False, None, {}
        import hashlib

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, None, {}

        order = (payload.get("data") or {}).get("order") or {}
        sign = payload.get("sign") or ""
        # Afdian signs md5(token + "params" + params + "ts" + ts + "user_id" + user_id)
        expected = hashlib.md5(
            ("%sparams%sts%suser_id%s" % (self.token, payload.get("params", ""),
                                          payload.get("ts", ""), self.user_id)).encode("utf-8")
        ).hexdigest()
        if not secrets.compare_digest(sign, expected):
            return False, None, {}
        if int(order.get("status", 0)) != 2:      # 2 = paid
            return False, None, order
        return True, order.get("out_trade_no"), order


class WechatNativeProvider:
    """WeChat Pay Native (scan-to-pay on our own page).

    Requires a merchant id and a signing certificate. Those come either from a
    business licence or from a payment service provider that onboards individuals
    as micro-merchants, in which case the money still settles to a personal bank
    card. Set ``TAI_WECHAT_MCHID`` and friends to enable it.

    Deliberately not half-implemented: ordering a payment without valid signing
    credentials would produce QR codes that cannot be paid.
    """

    name = "wechat"

    def __init__(self, mchid: str | None = None, api_v3_key: str | None = None):
        self.mchid = mchid
        self.api_v3_key = api_v3_key

    @property
    def configured(self) -> bool:
        return bool(self.mchid and self.api_v3_key)

    def create_intent(self, order: sqlite3.Row) -> PaymentIntent:
        if not self.configured:
            raise RuntimeError(
                "WeChat Pay is not configured: set TAI_WECHAT_MCHID and TAI_WECHAT_API_V3_KEY"
            )
        raise NotImplementedError(
            "Native ordering is not implemented yet: it needs the merchant "
            "certificate to sign /v3/pay/transactions/native requests."
        )

    def verify_callback(self, headers: dict, body: bytes) -> tuple[bool, str | None, dict]:
        return False, None, {}


def build_providers() -> dict[str, PaymentProvider]:
    import os

    return {
        "manual": ManualProvider(),
        "afdian": AfdianProvider(
            token=os.getenv("TAI_AFDIAN_TOKEN"),
            user_id=os.getenv("TAI_AFDIAN_USER_ID"),
            page=os.getenv("TAI_AFDIAN_PAGE"),
        ),
        "wechat": WechatNativeProvider(
            mchid=os.getenv("TAI_WECHAT_MCHID"),
            api_v3_key=os.getenv("TAI_WECHAT_API_V3_KEY"),
        ),
    }


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #

def create_order(
    conn: sqlite3.Connection,
    user_id: int,
    purpose: str,
    amount_cny: float,
    *,
    months: int = 0,
    provider: str = "manual",
) -> sqlite3.Row:
    if purpose not in PURPOSES:
        raise ValueError("purpose must be one of %s" % (PURPOSES,))
    if provider not in PROVIDERS:
        raise ValueError("provider must be one of %s" % (PROVIDERS,))
    if amount_cny <= 0:
        raise ValueError("amount_cny must be positive")

    order_id = new_order_id()
    now = db.iso(db.utcnow())
    conn.execute(
        """
        INSERT INTO payment_orders
            (id, user_id, purpose, amount_cny, months, provider, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (order_id, user_id, purpose, round(float(amount_cny), 2), int(months), provider, now),
    )
    conn.commit()
    return get_order(conn, order_id)


def get_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()


def list_orders(
    conn: sqlite3.Connection,
    *,
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    sql = "SELECT o.*, u.email AS user_email FROM payment_orders o JOIN users u ON u.id = o.user_id"
    where, params = [], []
    if user_id is not None:
        where.append("o.user_id = ?")
        params.append(user_id)
    if status:
        where.append("o.status = ?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    return conn.execute(sql, params).fetchall()


def fulfil_order(conn: sqlite3.Connection, order: sqlite3.Row) -> dict:
    """Deliver what the order bought. Idempotent: a paid order is never re-delivered."""
    if order["status"] == "paid":
        return {"already_fulfilled": True, "order_id": order["id"]}

    result: dict = {"order_id": order["id"], "purpose": order["purpose"]}

    if order["purpose"] == "api_credit":
        balance = db.grant_credit(
            conn, int(order["user_id"]), float(order["amount_cny"]), "topup:%s" % order["id"]
        )
        result["credited_cny"] = float(order["amount_cny"])
        result["balance_cny"] = balance
    elif order["purpose"] == "sigma_pro":
        result.update(grant_pro(conn, int(order["user_id"]), int(order["months"]) or 1))

    now = db.iso(db.utcnow())
    conn.execute(
        "UPDATE payment_orders SET status = 'paid', paid_at = ? WHERE id = ? AND status != 'paid'",
        (now, order["id"]),
    )
    audit(conn, None, "order.fulfilled", result)
    conn.commit()
    return result


def mark_order_paid(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    provider_trade_no: str | None = None,
    payload: dict | None = None,
    actor_id: int | None = None,
) -> tuple[bool, dict]:
    """Confirm an order and deliver it.

    Idempotency has two layers: ``provider_trade_no`` is UNIQUE so a replayed
    callback cannot create a second order, and ``fulfil_order`` short-circuits on
    an already-paid order. Replaying a callback is therefore harmless, which
    matters because every payment channel retries.
    """
    order = get_order(conn, order_id)
    if order is None:
        return False, {"error": "no_such_order"}

    if provider_trade_no:
        clash = conn.execute(
            "SELECT id FROM payment_orders WHERE provider_trade_no = ? AND id != ?",
            (provider_trade_no, order_id),
        ).fetchone()
        if clash:
            return False, {"error": "trade_no_already_used", "order_id": clash["id"]}
        conn.execute(
            "UPDATE payment_orders SET provider_trade_no = ? WHERE id = ?",
            (provider_trade_no, order_id),
        )
    if payload is not None:
        conn.execute(
            "UPDATE payment_orders SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False)[:20000], order_id),
        )
    conn.commit()

    order = get_order(conn, order_id)
    result = fulfil_order(conn, order)
    audit(conn, actor_id, "order.mark_paid", {"order_id": order_id, "trade_no": provider_trade_no})
    conn.commit()
    return True, result


# --------------------------------------------------------------------------- #
# Redeem codes
# --------------------------------------------------------------------------- #

def validate_grants(grants: dict) -> dict:
    kind = (grants or {}).get("kind")
    if kind == "api_credit":
        amount = float(grants.get("amount_cny") or 0)
        if amount <= 0:
            raise ValueError("api_credit needs a positive amount_cny")
        return {"kind": "api_credit", "amount_cny": round(amount, 2)}
    if kind == "sigma_pro":
        months = int(grants.get("months") or 1)
        if months <= 0:
            raise ValueError("sigma_pro needs months >= 1")
        return {"kind": "sigma_pro", "months": months}
    raise ValueError("grants.kind must be 'api_credit' or 'sigma_pro'")


def generate_codes(
    conn: sqlite3.Connection,
    grants: dict,
    count: int,
    *,
    note: str | None = None,
    expires_at: str | None = None,
    created_by: int | None = None,
) -> tuple[str, list[str]]:
    grants = validate_grants(grants)
    count = int(count)
    if not 1 <= count <= 500:
        raise ValueError("count must be between 1 and 500")

    batch_id = new_batch_id()
    now = db.iso(db.utcnow())
    codes: list[str] = []
    # Retry on the astronomically unlikely collision rather than crashing.
    while len(codes) < count:
        code = new_code()
        try:
            conn.execute(
                """
                INSERT INTO redeem_codes
                    (code, grants_json, note, batch_id, created_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (code, json.dumps(grants, ensure_ascii=False), note, batch_id,
                 created_by, now, expires_at),
            )
            codes.append(code)
        except sqlite3.IntegrityError:
            continue
    audit(conn, created_by, "codes.generated",
          {"batch_id": batch_id, "count": count, "grants": grants})
    conn.commit()
    return batch_id, codes


def redeem_code(conn: sqlite3.Connection, raw_code: str, user_id: int) -> tuple[bool, str, dict]:
    """Redeem a code for ``user_id``. Returns ``(ok, code_reason, result)``."""
    code = normalize_code(raw_code)
    if not code:
        return False, "invalid_code", {}

    row = conn.execute("SELECT * FROM redeem_codes WHERE code = ?", (code,)).fetchone()
    if row is None:
        return False, "invalid_code", {}
    if row["redeemed_at"]:
        return False, "already_redeemed", {"redeemed_at": row["redeemed_at"]}
    if row["expires_at"] and row["expires_at"] < db.iso(db.utcnow()):
        return False, "code_expired", {"expires_at": row["expires_at"]}

    grants = json.loads(row["grants_json"])
    result: dict = {"code": code, "grants": grants}

    if grants["kind"] == "api_credit":
        result["balance_cny"] = db.grant_credit(
            conn, user_id, float(grants["amount_cny"]), "redeem:%s" % code
        )
    elif grants["kind"] == "sigma_pro":
        result.update(grant_pro(conn, user_id, int(grants["months"])))

    # Conditional update: if two requests race, exactly one wins.
    cur = conn.execute(
        "UPDATE redeem_codes SET redeemed_at = ?, redeemed_by = ? "
        "WHERE code = ? AND redeemed_at IS NULL",
        (db.iso(db.utcnow()), user_id, code),
    )
    if cur.rowcount == 0:
        conn.rollback()
        return False, "already_redeemed", {}
    audit(conn, user_id, "code.redeemed", {"code": code, "grants": grants})
    conn.commit()
    return True, "ok", result


def list_codes(
    conn: sqlite3.Connection, *, batch_id: str | None = None, unredeemed_only: bool = False,
    limit: int = 100,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM redeem_codes"
    where, params = [], []
    if batch_id:
        where.append("batch_id = ?")
        params.append(batch_id)
    if unredeemed_only:
        where.append("redeemed_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #

def get_subscription(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {"plan": "free", "expires_at": None, "active": True, "days_left": None}

    expires_at = row["expires_at"]
    if row["plan"] == "free":
        return {"plan": "free", "expires_at": expires_at, "active": True, "days_left": None}

    if expires_at is None:                    # staff / permanent
        return {"plan": row["plan"], "expires_at": None, "active": True, "days_left": None}

    now = db.utcnow()
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return {"plan": "free", "expires_at": expires_at, "active": False, "days_left": 0}

    days_left = (expiry - now).days
    active = expiry > now
    return {
        "plan": row["plan"] if active else "free",
        "expires_at": expires_at,
        "active": active,
        "days_left": max(0, days_left),
    }


def grant_pro(conn: sqlite3.Connection, user_id: int, months: int) -> dict:
    """Extend (or start) a pro subscription.

    Extending stacks on top of an unexpired subscription rather than resetting
    it, so buying two months twice gives four months.
    """
    current = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
    ).fetchone()
    now = db.utcnow()

    base = now
    if current and current["plan"] == "pro" and current["expires_at"]:
        try:
            existing = datetime.fromisoformat(current["expires_at"])
            if existing > now:
                base = existing
        except ValueError:
            pass

    expires = base + timedelta(days=30 * int(months))
    conn.execute(
        """
        INSERT INTO subscriptions (user_id, plan, expires_at, updated_at)
        VALUES (?, 'pro', ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET plan = 'pro', expires_at = excluded.expires_at,
                                           updated_at = excluded.updated_at
        """,
        (user_id, db.iso(expires), db.iso(now)),
    )
    conn.commit()
    return {
        "plan": "pro",
        "months": int(months),
        "expires_at": db.iso(expires),
        "days_left": (expires - now).days,
    }


def set_plan(conn: sqlite3.Connection, user_id: int, plan: str, months: int = 0) -> dict:
    """Set a plan.

    ``admin`` is the internal top tier: unlimited and never expiring, which is
    why it stores a NULL expiry rather than a very long one.
    """
    if plan == "pro":
        return grant_pro(conn, user_id, months or 1)
    if plan == "admin":
        conn.execute(
            """
            INSERT INTO subscriptions (user_id, plan, expires_at, updated_at)
            VALUES (?, 'admin', NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET plan = 'admin', expires_at = NULL,
                                               updated_at = excluded.updated_at
            """,
            (user_id, db.iso(db.utcnow())),
        )
        conn.commit()
        return {"plan": "admin", "expires_at": None, "days_left": None}
    conn.execute(
        """
        INSERT INTO subscriptions (user_id, plan, expires_at, updated_at)
        VALUES (?, 'free', NULL, ?)
        ON CONFLICT(user_id) DO UPDATE SET plan = 'free', expires_at = NULL,
                                           updated_at = excluded.updated_at
        """,
        (user_id, db.iso(db.utcnow())),
    )
    conn.commit()
    return {"plan": "free", "expires_at": None}


def entitlements(conn: sqlite3.Connection, user_id: int) -> dict:
    """Everything a client needs to know what this account may do.

    Sigma calls this instead of keeping its own copy of the plan, so there is one
    billing authority rather than two that drift apart.
    """
    sub = get_subscription(conn, user_id)
    state = db.account_state(conn, user_id)
    allowed, reason, _ = db.can_generate(conn, user_id)
    return {
        "object": "entitlements",
        "plan": sub["plan"],
        "subscription": sub,
        "balance_cny": state["balance_cny"],
        "free_tokens": {
            "granted": state["free_granted"],
            "used": state["free_used"],
            "remaining": state["free_remaining"],
        },
        "can_generate": allowed,
        "reason": reason,
    }
