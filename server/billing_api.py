"""Billing, admin and internal endpoints.

Three routers, deliberately separated by who may call them:

* ``/api/billing``  — the signed-in user: their own orders, codes, entitlements.
* ``/api/admin``    — staff only (``users.is_admin``); grants money and plans.
* ``/api/internal`` — the Sigma backend, authenticated by a shared secret. This is
                      how Sigma asks "what is this account entitled to?" instead
                      of keeping a second, drifting copy of the plan.

Plus ``/api/payments/callback/{provider}`` which payment channels call. It is
public by necessity, so every provider must verify its own signature and the
handler is idempotent.
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server import billing, db
from server.deps import current_user, fail, require_admin

billing_router = APIRouter(prefix="/api/billing", tags=["billing"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
internal_router = APIRouter(prefix="/api/internal", tags=["internal"])
payments_router = APIRouter(prefix="/api/payments", tags=["payments"])

PROVIDERS = billing.build_providers()
INTERNAL_TOKEN = os.getenv("TAI_INTERNAL_TOKEN")


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #

def order_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "object": "order",
        "user_id": row["user_id"],
        "purpose": row["purpose"],
        "amount_cny": row["amount_cny"],
        "months": row["months"],
        "provider": row["provider"],
        "status": row["status"],
        "created_at": row["created_at"],
        "paid_at": row["paid_at"],
        "provider_trade_no": row["provider_trade_no"],
    }


def code_out(row: sqlite3.Row) -> dict:
    import json

    return {
        "code": row["code"],
        "object": "redeem_code",
        "grants": json.loads(row["grants_json"]),
        "note": row["note"],
        "batch_id": row["batch_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "redeemed_at": row["redeemed_at"],
        "redeemed_by": row["redeemed_by"],
    }


def user_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class CreateOrderBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    purpose: str = Field(default="api_credit")
    amount_cny: float = Field(gt=0, le=100000)
    months: int = Field(default=0, ge=0, le=120)
    provider: str = Field(default="manual")

    @field_validator("purpose")
    @classmethod
    def check_purpose(cls, v: str) -> str:
        if v not in billing.PURPOSES:
            raise ValueError("purpose must be one of %s" % (billing.PURPOSES,))
        return v

    @field_validator("provider")
    @classmethod
    def check_provider(cls, v: str) -> str:
        if v not in billing.PROVIDERS:
            raise ValueError("provider must be one of %s" % (billing.PROVIDERS,))
        return v


class RedeemBody(BaseModel):
    code: str = Field(min_length=4, max_length=64)


class GenerateCodesBody(BaseModel):
    kind: str = Field(default="api_credit")
    amount_cny: float | None = Field(default=None, gt=0, le=100000)
    months: int | None = Field(default=None, ge=1, le=120)
    count: int = Field(default=1, ge=1, le=500)
    note: str | None = Field(default=None, max_length=200)
    expires_at: str | None = None


class GrantCreditBody(BaseModel):
    amount_cny: float = Field(gt=0, le=100000)
    reason: str = Field(default="manual grant", max_length=200)


class SetPlanBody(BaseModel):
    plan: str = Field(default="pro")
    months: int = Field(default=1, ge=0, le=120)


# --------------------------------------------------------------------------- #
# User-facing billing
# --------------------------------------------------------------------------- #

@billing_router.get("/entitlements")
def my_entitlements(
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    return billing.entitlements(conn, int(user["id"]))


@billing_router.get("/orders")
def my_orders(
    limit: int = 20,
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    rows = billing.list_orders(conn, user_id=int(user["id"]), limit=limit)
    return {"object": "list", "data": [order_out(r) for r in rows]}


@billing_router.post("/orders", status_code=status.HTTP_201_CREATED)
def create_my_order(
    body: CreateOrderBody,
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    months = body.months or (1 if body.purpose == "sigma_pro" else 0)
    row = billing.create_order(
        conn, int(user["id"]), body.purpose, body.amount_cny,
        months=months, provider=body.provider,
    )
    return order_out(row)


@billing_router.post("/orders/{order_id}/intent")
def order_intent(
    order_id: str,
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    """How should the user pay? Providers that are not configured say so plainly."""
    row = billing.get_order(conn, order_id)
    if row is None or int(row["user_id"]) != int(user["id"]):
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such order")
    if row["status"] == "paid":
        return {"order_id": order_id, "status": "paid", "intent": None}

    provider = PROVIDERS.get(row["provider"])
    if provider is None:
        raise fail(status.HTTP_400_BAD_REQUEST, "unknown_provider", "Unknown payment provider")
    try:
        intent = provider.create_intent(row)
    except (RuntimeError, NotImplementedError) as exc:
        # Not configured yet: a clear 409 beats a link that cannot be paid.
        raise fail(status.HTTP_409_CONFLICT, "provider_not_configured", str(exc)) from exc
    return {"order_id": order_id, "status": row["status"], "intent": dict(intent)}


@billing_router.post("/redeem")
def redeem(
    body: RedeemBody,
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    ok, reason, result = billing.redeem_code(conn, body.code, int(user["id"]))
    if not ok:
        messages = {
            "invalid_code": "That code is not valid.",
            "already_redeemed": "That code has already been used.",
            "code_expired": "That code has expired.",
        }
        raise fail(status.HTTP_400_BAD_REQUEST, reason, messages.get(reason, "Could not redeem"))
    result["entitlements"] = billing.entitlements(conn, int(user["id"]))
    return result


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #

@admin_router.get("/providers")
def provider_status(admin: sqlite3.Row = Depends(require_admin)):
    """Which payment channels are usable right now."""
    return {
        "object": "list",
        "data": [
            {
                "name": name,
                "configured": bool(getattr(p, "configured", True)),
                "implemented": not isinstance(p, billing.WechatNativeProvider) or p.configured,
            }
            for name, p in PROVIDERS.items()
        ],
    }


@admin_router.get("/orders")
def admin_orders(
    status_filter: str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    rows = billing.list_orders(conn, status=status_filter, limit=limit)
    return {"object": "list", "data": [order_out(r) for r in rows]}


@admin_router.post("/orders/{order_id}/mark-paid")
def admin_mark_paid(
    order_id: str,
    provider_trade_no: str | None = None,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    """Confirm a transfer arrived and deliver the order.

    Safe to press twice: fulfilment short-circuits on an already-paid order.
    """
    ok, result = billing.mark_order_paid(
        conn, order_id, provider_trade_no=provider_trade_no, actor_id=int(admin["id"])
    )
    if not ok:
        raise fail(status.HTTP_400_BAD_REQUEST, result.get("error", "failed"), "Could not mark paid")
    return result


@admin_router.post("/codes", status_code=status.HTTP_201_CREATED)
def admin_generate_codes(
    body: GenerateCodesBody,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    if body.kind == "api_credit":
        grants = {"kind": "api_credit", "amount_cny": body.amount_cny or 0}
    elif body.kind == "sigma_pro":
        grants = {"kind": "sigma_pro", "months": body.months or 1}
    else:
        raise fail(status.HTTP_400_BAD_REQUEST, "invalid_kind", "kind must be api_credit or sigma_pro")

    try:
        batch_id, codes = billing.generate_codes(
            conn, grants, body.count, note=body.note,
            expires_at=body.expires_at, created_by=int(admin["id"]),
        )
    except ValueError as exc:
        raise fail(status.HTTP_400_BAD_REQUEST, "invalid_request", str(exc)) from exc
    return {"batch_id": batch_id, "grants": grants, "count": len(codes), "codes": codes}


@admin_router.get("/codes")
def admin_list_codes(
    batch_id: str | None = None,
    unredeemed_only: bool = False,
    limit: int = 100,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    rows = billing.list_codes(conn, batch_id=batch_id, unredeemed_only=unredeemed_only, limit=limit)
    return {"object": "list", "data": [code_out(r) for r in rows]}


@admin_router.get("/users")
def admin_users(
    q: str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    sql = "SELECT id, email, name, is_admin, created_at FROM users"
    params: list = []
    if q:
        sql += " WHERE email LIKE ? OR name LIKE ?"
        params += ["%%%s%%" % q, "%%%s%%" % q]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        item = user_out(r)
        item["entitlements"] = billing.entitlements(conn, int(r["id"]))
        out.append(item)
    return {"object": "list", "data": out}


@admin_router.post("/users/{user_id}/credit")
def admin_grant_credit(
    user_id: int,
    body: GrantCreditBody,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such user")
    balance = db.grant_credit(conn, user_id, body.amount_cny, "admin:%s" % body.reason)
    billing.audit(conn, int(admin["id"]), "admin.grant_credit",
                  {"user_id": user_id, "amount_cny": body.amount_cny, "reason": body.reason})
    conn.commit()
    return {"user_id": user_id, "balance_cny": balance}


@admin_router.post("/users/{user_id}/plan")
def admin_set_plan(
    user_id: int,
    body: SetPlanBody,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such user")
    result = billing.set_plan(conn, user_id, body.plan, body.months)
    billing.audit(conn, int(admin["id"]), "admin.set_plan", {"user_id": user_id, **result})
    conn.commit()
    return result


@admin_router.get("/audit")
def admin_audit(
    limit: int = 100,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin: sqlite3.Row = Depends(require_admin),
):
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
    ).fetchall()
    import json as _json

    return {
        "object": "list",
        "data": [
            {
                "id": r["id"], "actor_id": r["actor_id"], "action": r["action"],
                "detail": _json.loads(r["detail_json"] or "{}"), "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Internal (Sigma -> platform)
# --------------------------------------------------------------------------- #

def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
    """Shared-secret auth for server-to-server calls.

    If no token is configured the internal API is closed rather than open; an
    unauthenticated endpoint that can read entitlements is not a safe default.
    """
    if not INTERNAL_TOKEN:
        raise fail(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "internal_api_disabled",
            "Set TAI_INTERNAL_TOKEN to enable the internal API",
        )
    if not x_internal_token or not secrets_compare(x_internal_token, INTERNAL_TOKEN):
        raise fail(status.HTTP_401_UNAUTHORIZED, "bad_internal_token", "Invalid internal token")


def secrets_compare(a: str, b: str) -> bool:
    import secrets

    return secrets.compare_digest(a, b)


@internal_router.get("/entitlements/{user_id}")
def internal_entitlements(
    user_id: int,
    conn: sqlite3.Connection = Depends(db.get_db),
    _: None = Depends(require_internal),
):
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such user")
    return billing.entitlements(conn, user_id)


@internal_router.get("/health")
def internal_health(_: None = Depends(require_internal)):
    return {"status": "ok", "providers": [n for n, p in PROVIDERS.items()
                                           if getattr(p, "configured", True)]}


# --------------------------------------------------------------------------- #
# Payment callbacks
# --------------------------------------------------------------------------- #

@payments_router.post("/callback/{provider_name}")
async def payment_callback(provider_name: str, request: Request,
                           conn: sqlite3.Connection = Depends(db.get_db)):
    """Inbound notification from a payment channel.

    Always returns 200 for anything we understood, even when the order was
    already fulfilled: channels retry aggressively and a non-2xx reply makes them
    retry harder. Anything we could not verify returns 400 so it shows up in
    their dashboard instead of silently disappearing.
    """
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise fail(status.HTTP_404_NOT_FOUND, "unknown_provider", "Unknown payment provider")

    body = await request.body()
    ok, trade_no, parsed = provider.verify_callback(dict(request.headers), body)
    if not ok:
        raise fail(status.HTTP_400_BAD_REQUEST, "callback_rejected",
                   "Signature verification failed or the payment is not complete")

    order_id = (parsed or {}).get("out_trade_no") or (parsed or {}).get("order_id")
    if not order_id:
        raise fail(status.HTTP_400_BAD_REQUEST, "callback_rejected", "No order reference in callback")

    ok2, result = billing.mark_order_paid(
        conn, order_id, provider_trade_no=trade_no, payload=parsed
    )
    if not ok2:
        raise fail(status.HTTP_400_BAD_REQUEST, result.get("error", "failed"), "Could not apply payment")
    return {"status": "ok", "result": result}
