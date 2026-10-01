"""The internal API Sigma calls.

Sigma keeps its own login but must not keep its own idea of what a plan is worth,
so it asks here. These tests cover the join (email), the refusal to invent an
account, and the shared-secret gate.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "test-internal-token"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod := __import__("server.db", fromlist=["db"]), "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
    import server.billing_api as api

    monkeypatch.setattr(api, "INTERNAL_TOKEN", TOKEN)
    from server.main import app

    db_mod.init_db()
    with TestClient(app) as c:
        yield c


AUTH = {"X-Internal-Token": TOKEN}


def make_user(conn, email="player@tai.dev"):
    conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?,?,?,?)",
        (email, "P", "x", db_mod.iso(db_mod.utcnow())),
    )
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


import server.db as db_mod  # noqa: E402


def test_internal_api_is_closed_when_no_token_is_configured(client, monkeypatch):
    """Unconfigured must mean closed, not open."""
    import server.billing_api as api

    monkeypatch.setattr(api, "INTERNAL_TOKEN", None)
    r = client.get("/api/internal/entitlements/by-email/x@y.z")
    assert r.status_code == 503
    assert r.json()["code"] == "internal_api_disabled"


def test_wrong_or_missing_token_is_rejected(client):
    assert client.get("/api/internal/entitlements/by-email/x@y.z").status_code == 401
    r = client.get("/api/internal/entitlements/by-email/x@y.z",
                   headers={"X-Internal-Token": "wrong"})
    assert r.status_code == 401


def test_lookup_is_by_email_and_case_insensitive(client):
    conn = db_mod.connect()
    make_user(conn, "Player@TAI.dev")
    conn.close()

    r = client.get("/api/internal/entitlements/by-email/player@tai.dev", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["plan"] == "free"
    assert r.json()["free_tokens"]["granted"] > 0


def test_no_platform_account_is_a_clear_404_not_an_invented_account(client):
    """Auto-creating an account would produce one with no password that could
    never be logged into. Saying no is better."""
    r = client.get("/api/internal/entitlements/by-email/nobody@tai.dev", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["code"] == "no_platform_account"

    conn = db_mod.connect()
    assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0, \
        "a failed lookup must not create a user"
    conn.close()


def test_redeem_for_a_sigma_user(client):
    conn = db_mod.connect()
    uid = make_user(conn)
    conn.close()

    con = db_mod.connect()
    _, codes = __import__("server.billing", fromlist=["billing"]).generate_codes(
        con, {"kind": "api_credit", "amount_cny": 30}, 1
    )
    con.close()

    r = client.post("/api/internal/redeem", headers=AUTH,
                    json={"email": "player@tai.dev", "code": codes[0]})
    assert r.status_code == 200
    assert r.json()["balance_cny"] == 30

    # and again: idempotent
    r2 = client.post("/api/internal/redeem", headers=AUTH,
                     json={"email": "player@tai.dev", "code": codes[0]})
    assert r2.status_code == 400
    assert r2.json()["code"] == "already_redeemed"

    con = db_mod.connect()
    assert db_mod.account_state(con, uid)["balance_cny"] == 30
    con.close()


def test_redeem_without_a_platform_account(client):
    r = client.post("/api/internal/redeem", headers=AUTH,
                    json={"email": "ghost@tai.dev", "code": "TAI-ZZZZ-ZZZZ-ZZZZ"})
    assert r.status_code == 404
    assert r.json()["code"] == "no_platform_account"


def test_redeem_accepts_sloppy_input(client):
    conn = db_mod.connect()
    make_user(conn)
    conn.close()
    con = db_mod.connect()
    _, codes = __import__("server.billing", fromlist=["billing"]).generate_codes(
        con, {"kind": "api_credit", "amount_cny": 5}, 1
    )
    con.close()

    messy = codes[0].lower().replace("-", " ")
    r = client.post("/api/internal/redeem", headers=AUTH,
                    json={"email": "player@tai.dev", "code": messy})
    assert r.status_code == 200, r.text
    assert r.json()["balance_cny"] == 5
