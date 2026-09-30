"""TAI Developer Platform -- authentication API.

Run from the project root (the directory that contains the .html files)::

    python -m uvicorn server.main:app --reload --port 8000

The service both serves the static site and exposes the JSON API, so cookies are
first-party and there is no CORS configuration to get wrong in production.

Design decisions worth knowing:

* **Sessions, not JWTs.** The cookie holds a random opaque token; the database
  stores only its SHA-256. Logout and "revoke all sessions" are therefore real.
* **HttpOnly + SameSite=Lax.** JavaScript cannot read the cookie, so an XSS bug
  cannot exfiltrate a session.
* **Origin check on writes.** SameSite=Lax already blocks cross-site POSTs in
  modern browsers; the explicit check is cheap defence in depth.
* **Stable error codes.** The response body carries a machine-readable ``code``
  so the bilingual front-end can render the message in the visitor's language.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import db, pricing, security
from server.schemas import (
    ApiKeyOut,
    CreateKeyRequest,
    LoginRequest,
    RegisterRequest,
    UserOut,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SITE_ROOT = Path(__file__).resolve().parent.parent

COOKIE_NAME = "tai_session"
SESSION_DAYS = int(os.getenv("TAI_SESSION_DAYS", "7"))
COOKIE_SECURE = os.getenv("TAI_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
COOKIE_SAMESITE = os.getenv("TAI_COOKIE_SAMESITE", "lax").lower()

# Browsers refuse SameSite=None without Secure, and cross-site cookies are
# blocked outright, so fail loudly rather than shipping a silently broken login.
if COOKIE_SAMESITE == "none" and not COOKIE_SECURE:
    raise RuntimeError(
        "TAI_COOKIE_SAMESITE=none requires TAI_COOKIE_SECURE=true (HTTPS). "
        "Prefer serving the site and the API from the same registrable domain "
        "and leaving SameSite=Lax."
    )
if COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    raise RuntimeError("TAI_COOKIE_SAMESITE must be one of: lax, strict, none")

# Comma-separated list of front-end origins allowed to call this API with
# credentials. Required when the site is on GitHub Pages and the API elsewhere.
ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("TAI_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

# When the API runs behind a tunnel it should serve the API *only*; the pages
# come from GitHub Pages. Turning the static mount off shrinks the attack
# surface to just /api/*.
SERVE_SITE = os.getenv("TAI_SERVE_SITE", "true").lower() in {"1", "true", "yes"}
# Interactive docs expose the whole API surface; handy locally, optional in prod.
ENABLE_DOCS = os.getenv("TAI_ENABLE_DOCS", "true").lower() in {"1", "true", "yes"}

# Paths that must never be served by the static mount. The site root contains the
# backend itself, so without this the SQLite file and secrets would be public.
BLOCKED_PREFIXES = (
    "/server",
    "/.git",
    "/.env",
    "/data",
    "/__pycache__",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="TAI Developer Platform API",
    version="1.0.0",
    docs_url="/api/docs" if ENABLE_DOCS else None,
    openapi_url="/api/openapi.json" if ENABLE_DOCS else None,
    lifespan=lifespan,
)

if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )


@app.middleware("http")
async def block_private_paths(request: Request, call_next):
    path = request.url.path
    if path.startswith(BLOCKED_PREFIXES) or "/." in path:
        return JSONResponse(
            status_code=404, content={"code": "not_found", "message": "Not found"}
        )
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def fail(status_code: int, code: str, message: str) -> HTTPException:
    """An error the front-end can translate by looking at ``detail.code``."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def assert_same_origin(request: Request) -> None:
    """Reject cross-site writes from origins we do not trust.

    Same-origin requests match Host. Requests from the static site on GitHub
    Pages are cross-origin by nature, so any origin listed in
    TAI_ALLOWED_ORIGINS is accepted too. An absent Origin header is allowed
    (non-browser clients such as curl).
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    if origin.rstrip("/") in ALLOWED_ORIGINS:
        return
    host = request.headers.get("host", "")
    if origin.split("://", 1)[-1] != host:
        raise fail(status.HTTP_403_FORBIDDEN, "bad_origin", "Cross-origin request rejected")


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )


def issue_session(conn: sqlite3.Connection, user_id: int, request: Request) -> str:
    token = security.new_session_token()
    conn.execute(
        """
        INSERT INTO sessions (user_id, token_hash, created_at, expires_at, user_agent, ip)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            security.token_fingerprint(token),
            db.iso(db.utcnow()),
            db.iso(db.utcnow() + timedelta(days=SESSION_DAYS)),
            (request.headers.get("user-agent") or "")[:255],
            client_ip(request),
        ),
    )
    conn.commit()
    return token


def row_to_user(row: sqlite3.Row) -> UserOut:
    return UserOut(
        id=row["id"], email=row["email"], name=row["name"], created_at=row["created_at"]
    )


def current_user(
    conn: sqlite3.Connection = Depends(db.get_db),
    tai_session: str | None = Cookie(default=None),
) -> sqlite3.Row:
    """Resolve the session cookie to a user row, or raise 401."""
    if not tai_session:
        raise fail(status.HTTP_401_UNAUTHORIZED, "not_authenticated", "No active session")

    row = conn.execute(
        """
        SELECT u.* FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
        """,
        (security.token_fingerprint(tai_session), db.iso(db.utcnow())),
    ).fetchone()

    if row is None:
        raise fail(status.HTTP_401_UNAUTHORIZED, "not_authenticated", "Session expired")
    return row


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

@app.post("/api/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    assert_same_origin(request)

    exists = conn.execute(
        "SELECT 1 FROM users WHERE lower(email) = lower(?)", (payload.email,)
    ).fetchone()
    if exists:
        raise fail(status.HTTP_409_CONFLICT, "email_taken", "That email is already registered")

    cursor = conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (payload.email, payload.name, security.hash_password(payload.password), db.iso(db.utcnow())),
    )
    conn.commit()

    user_id = int(cursor.lastrowid)
    set_session_cookie(response, issue_session(conn, user_id, request))

    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return row_to_user(row)


@app.post("/api/auth/login", response_model=UserOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    assert_same_origin(request)

    ip = client_ip(request)
    if db.recent_failures(conn, payload.email, ip) >= db.MAX_FAILED_ATTEMPTS:
        raise fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many failed attempts. Try again later.",
        )

    row = conn.execute(
        "SELECT * FROM users WHERE lower(email) = lower(?)", (payload.email,)
    ).fetchone()

    # Always run a hash comparison so response time does not reveal whether the
    # account exists.
    stored = row["password_hash"] if row else security.hash_password("dummy-password")
    ok = security.verify_password(payload.password, stored)

    if row is None or not ok or not row["is_active"]:
        db.record_attempt(conn, payload.email, ip, ok=False)
        raise fail(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Incorrect email or password")

    if security.needs_rehash(stored):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (security.hash_password(payload.password), row["id"]),
        )
        conn.commit()

    db.clear_failures(conn, payload.email, ip)
    db.record_attempt(conn, payload.email, ip, ok=True)
    set_session_cookie(response, issue_session(conn, int(row["id"]), request))
    return row_to_user(row)


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(db.get_db),
    tai_session: str | None = Cookie(default=None),
):
    assert_same_origin(request)
    if tai_session:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (security.token_fingerprint(tai_session),),
        )
        conn.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/auth/me", response_model=UserOut)
def me(user: sqlite3.Row = Depends(current_user)):
    return row_to_user(user)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #

@app.get("/api/keys", response_model=list[ApiKeyOut])
def list_keys(
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    rows = conn.execute(
        """
        SELECT id, name, key_prefix, created_at, last_used_at
        FROM api_keys
        WHERE user_id = ? AND revoked_at IS NULL
        ORDER BY id DESC
        """,
        (user["id"],),
    ).fetchall()
    return [
        ApiKeyOut(
            id=r["id"],
            name=r["name"],
            prefix=r["key_prefix"],
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
        )
        for r in rows
    ]


@app.post("/api/keys", status_code=status.HTTP_201_CREATED)
def create_key(
    payload: CreateKeyRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    assert_same_origin(request)
    plaintext, prefix, fingerprint = security.new_api_key()
    cursor = conn.execute(
        """
        INSERT INTO api_keys (user_id, name, key_prefix, key_hash, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user["id"], payload.name, prefix, fingerprint, db.iso(db.utcnow())),
    )
    conn.commit()
    # The only moment the full key is ever returned.
    return {
        "id": int(cursor.lastrowid),
        "name": payload.name,
        "prefix": prefix,
        "created_at": db.iso(db.utcnow()),
        "key": plaintext,
    }


@app.delete("/api/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    assert_same_origin(request)
    cursor = conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (db.iso(db.utcnow()), key_id, user["id"]),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such API key")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Usage metering
# --------------------------------------------------------------------------- #

@app.get("/api/usage")
def get_usage(
    conn: sqlite3.Connection = Depends(db.get_db),
    user: sqlite3.Row = Depends(current_user),
):
    """Real figures for the dashboard, computed from ``usage_events``.

    A fresh account correctly reports zeros — nothing here is invented. Once the
    inference endpoints call :func:`db.record_usage`, these numbers fill in.
    """
    summary = db.usage_summary(conn, int(user["id"]))
    summary["models"] = [
        {
            "id": model,
            "name": entry["name"],
            "live": entry["live"],
            "input": entry["input"],
            "output": entry["output"],
        }
        for model, entry in pricing.MODELS.items()
    ]
    return summary


# --------------------------------------------------------------------------- #
# API-key authentication (for the future inference endpoints)
# --------------------------------------------------------------------------- #

def require_api_key(
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> sqlite3.Row:
    """Authenticate a model call with ``Authorization: Bearer sk-tai-...``.

    Not used by any route yet — the inference endpoints come next. It lives here
    so the keys minted on the dashboard are real credentials from day one.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise fail(
            status.HTTP_401_UNAUTHORIZED,
            "missing_api_key",
            "Provide your API key as a Bearer token",
        )
    row = db.find_api_key(conn, security.api_key_fingerprint(header[7:].strip()))
    if row is None:
        raise fail(status.HTTP_401_UNAUTHORIZED, "invalid_api_key", "Unknown or revoked API key")
    if not row["user_active"]:
        raise fail(status.HTTP_403_FORBIDDEN, "account_disabled", "This account is disabled")
    return row


@app.get("/api/v1/models")
def list_models(key: sqlite3.Row = Depends(require_api_key)) -> dict:
    """Validate an API key and return the catalogue. Cheap, useful smoke test."""
    return {
        "account": key["user_id"],
        "key_prefix": key["key_prefix"],
        "models": [
            {
                "id": model,
                "name": entry["name"],
                "live": entry["live"],
                "input_cny_per_1m": entry["input"],
                "output_cny_per_1m": entry["output"],
            }
            for model, entry in pricing.MODELS.items()
            if entry["live"]
        ],
    }


# --------------------------------------------------------------------------- #
# Error shape
# --------------------------------------------------------------------------- #

@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        payload = detail
    else:
        payload = {"code": "error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=payload,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Collapse Pydantic's nested error list into the same {code, message} shape.

    The front-end then has exactly one error format to translate and display.
    """
    first = exc.errors()[0] if exc.errors() else {}
    location = first.get("loc", ())
    field = location[-1] if location else ""
    reason = first.get("msg", "Invalid request")

    if field == "email":
        code, message = "invalid_email", "Please enter a valid email address"
    elif field == "password":
        code, message = "weak_password", "Password must be at least 8 characters"
    elif field == "name":
        code, message = "invalid_name", "Please enter your name"
    else:
        code, message = "invalid_request", reason

    return JSONResponse(status_code=422, content={"code": code, "message": message})


# --------------------------------------------------------------------------- #
# Static site (must be mounted last so /api/* wins)
# --------------------------------------------------------------------------- #

if SERVE_SITE:
    app.mount("/", StaticFiles(directory=str(SITE_ROOT), html=True), name="site")
else:
    @app.get("/")
    def root() -> dict:
        """The API runs standalone when deployed behind a tunnel."""
        return {
            "service": "TAI Developer Platform API",
            "status": "ok",
            "docs": "/api/docs" if ENABLE_DOCS else None,
        }
