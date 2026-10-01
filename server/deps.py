"""Shared helpers and dependencies.

Everything in here is used by **both** routers:

* :mod:`server.main` -- the cookie-authenticated browser API, and
* :mod:`server.api_v1` -- the public ``/api/v1`` Bearer-key API.

It lives in its own module so that ``api_v1`` never has to import ``main``
(which would be a cycle, because ``main`` imports ``api_v1`` to mount it).
"""

from __future__ import annotations

import secrets
import sqlite3

from fastapi import Depends, HTTPException, Request, status

from server import db, pricing, security

#: ``secrets.token_urlsafe(18)`` is the contract's id recipe; keeping the size in
#: one place means every object id is generated the same way.
ID_TOKEN_BYTES = 18

#: Id prefixes, straight from sdk/API_CONTRACT.md §1.2.
ASSISTANT_PREFIX = "asst_"
THREAD_PREFIX = "thrd_"
MESSAGE_PREFIX = "msg_"
CHAT_PREFIX = "chat_"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

def fail(
    status_code: int,
    code: str,
    message: str,
    param: str | None = None,
) -> HTTPException:
    """An error in the documented ``{code, message, param?}`` shape.

    ``param`` is only included when we actually know which field was at fault,
    which is what the contract's optional field is for.
    """
    detail: dict[str, str] = {"code": code, "message": message}
    if param is not None:
        detail["param"] = param
    return HTTPException(status_code=status_code, detail=detail)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #

def new_id(prefix: str) -> str:
    """An opaque, type-prefixed object id (contract §1.2)."""
    return f"{prefix}{secrets.token_urlsafe(ID_TOKEN_BYTES)}"


# --------------------------------------------------------------------------- #
# API-key authentication
# --------------------------------------------------------------------------- #

def require_api_key(
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> sqlite3.Row:
    """Authenticate a model call with ``Authorization: Bearer sk-tai-...``.

    The returned row is the ``api_keys`` row plus ``user_active``; use
    ``row["user_id"]`` to scope every query and ``row["id"]`` to attribute usage.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise fail(
            status.HTTP_401_UNAUTHORIZED,
            "missing_api_key",
            "Provide your API key as a Bearer token",
        )
    token = header[7:].strip()
    if not token:
        raise fail(
            status.HTTP_401_UNAUTHORIZED,
            "missing_api_key",
            "Provide your API key as a Bearer token",
        )
    row = db.find_api_key(conn, security.api_key_fingerprint(token))
    if row is None:
        raise fail(status.HTTP_401_UNAUTHORIZED, "invalid_api_key", "Unknown or revoked API key")
    if not row["user_active"]:
        raise fail(status.HTTP_403_FORBIDDEN, "account_disabled", "This account is disabled")
    return row


# --------------------------------------------------------------------------- #
# Model catalogue
# --------------------------------------------------------------------------- #

def check_model(model_id: str) -> str:
    """Return ``model_id`` if it is a live model, else raise the contract error.

    ``model_not_found`` (404) for an id that does not exist at all,
    ``model_not_available`` (409) for one that exists but is not live yet.
    """
    entry = pricing.MODELS.get(model_id)
    if entry is None:
        raise fail(
            status.HTTP_404_NOT_FOUND,
            "model_not_found",
            f"Unknown model: {model_id}",
            param="model",
        )
    if not entry["live"]:
        raise fail(
            status.HTTP_409_CONFLICT,
            "model_not_available",
            f"The model '{model_id}' exists but is not live yet",
            param="model",
        )
    return model_id
