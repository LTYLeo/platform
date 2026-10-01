"""Billing regression tests.

Focused on the invariants that lose money or lock people out when they break:

* code normalisation (a bug here rejected *every* code, see below),
* the free allowance boundary and the 402 gate,
* idempotency of redemption and of payment confirmation,
* subscription stacking,
* the internal API refusing to be open when unconfigured.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import billing, db, pricing  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture()
def user(conn):
    conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?,?,?,?)",
        ("u@example.com", "U", "x", db.iso(db.utcnow())),
    )
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE email='u@example.com'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, plan, expires_at, updated_at) VALUES (?,?,?,?)",
        (uid, "free", None, db.iso(db.utcnow())),
    )
    conn.commit()
    return int(uid)


# --------------------------------------------------------------------------- #
# Code normalisation
# --------------------------------------------------------------------------- #

class TestNormalizeCode:
    """A regression suite for a bug that rejected 100% of codes.

    ``CODE_ALPHABET`` omits I, L, O, 0 and 1 so codes cannot be misread. The
    ``TAI`` prefix contains an I. The original implementation filtered the
    alphabet *before* stripping the prefix, which deleted that I, so nothing
    ever matched. Every spelling below must resolve.
    """

    CANONICAL = "TAI-JCAM-467B-236R"

    @pytest.mark.parametrize("written", [
        "TAI-JCAM-467B-236R",
        "tai-jcam-467b-236r",
        "tai - jcam - 467b - 236r",
        "TAIJCAM467B236R",
        "  TAI-JCAM-467B-236R  ",
        "TAIJ CAM 467B 236R",
        "JCAM-467B-236R",              # prefix omitted entirely
        "jcam467b236r",
        "TAI-JCAM-467B-236R\n",
    ])
    def test_accepts_any_reasonable_spelling(self, written):
        assert billing.normalize_code(written) == self.CANONICAL

    @pytest.mark.parametrize("bad", [
        "", "garbage", "TAI-JCAM-467B-236", "TAI-JCAM-467B-236RR",
        "TAI-IIII-IIII-IIII", "TAI-0000-0000-0000", None,
    ])
    def test_rejects_everything_else(self, bad):
        assert billing.normalize_code(bad) == ""

    def test_generated_codes_round_trip(self):
        for _ in range(200):
            code = billing.new_code()
            assert billing.normalize_code(code) == code
            # and never leaks an ambiguous character into the payload
            payload = code[len(billing.CODE_PREFIX):].replace("-", "")
            assert not set(payload) & set("ILO01"), code


# --------------------------------------------------------------------------- #
# Free allowance and the 402 gate
# --------------------------------------------------------------------------- #

class TestFreeAllowance:
    def test_fresh_account_is_on_the_free_allowance(self, conn, user):
        allowed, reason, state = db.can_generate(conn, user)
        assert allowed and reason == "free_allowance"
        assert state["free_remaining"] == pricing.FREE_TOKENS
        assert state["balance_cny"] == 0

    def test_calls_are_free_until_the_allowance_runs_out(self, conn, user):
        # exactly consumes the grant
        db.record_usage(conn, user, None, "tfmf", pricing.FREE_TOKENS, 0)
        state = db.account_state(conn, user)
        assert state["free_remaining"] == 0
        assert state["balance_cny"] == 0, "free tokens must not produce a charge"

    def test_the_next_call_is_billed_and_can_go_negative(self, conn, user):
        db.record_usage(conn, user, None, "tfmf", pricing.FREE_TOKENS, 0)
        db.grant_credit(conn, user, 1.0, "topup")
        cost = db.record_usage(conn, user, None, "tfmf", 1_000_000, 1_000_000)
        assert cost == pytest.approx(3.5)          # tfmf: ¥0.5 in + ¥3 out per 1M
        assert db.account_state(conn, user)["balance_cny"] == pytest.approx(1.0 - 3.5)

    def test_gate_closes_only_when_both_sources_are_empty(self, conn, user):
        assert db.can_generate(conn, user)[0] is True
        db.record_usage(conn, user, None, "tfmf", pricing.FREE_TOKENS, 0)
        db.grant_credit(conn, user, 5.0, "topup")
        allowed, reason, _ = db.can_generate(conn, user)
        assert allowed and reason == "balance", "credit alone must be enough"

        # Drain through the ledger directly: grant_credit deliberately refuses
        # negative amounts, which is the behaviour we want for the public API.
        conn.execute(
            "INSERT INTO credit_ledger (user_id, amount_cny, reason, created_at) VALUES (?,?,?,?)",
            (user, -5.0, "test:drain", db.iso(db.utcnow())),
        )
        conn.commit()
        allowed, reason, _ = db.can_generate(conn, user)
        assert not allowed and reason == "insufficient_balance"


# --------------------------------------------------------------------------- #
# Redeem codes
# --------------------------------------------------------------------------- #

class TestRedeemCodes:
    def test_credit_code_credits_once(self, conn, user):
        _, codes = billing.generate_codes(conn, {"kind": "api_credit", "amount_cny": 20}, 1)
        ok, reason, result = billing.redeem_code(conn, codes[0], user)
        assert ok and reason == "ok" and result["balance_cny"] == 20

        ok2, reason2, _ = billing.redeem_code(conn, codes[0], user)
        assert not ok2 and reason2 == "already_redeemed"
        assert db.account_state(conn, user)["balance_cny"] == 20, "double redeem must not double credit"

    def test_pro_code_grants_a_subscription(self, conn, user):
        _, codes = billing.generate_codes(conn, {"kind": "sigma_pro", "months": 1}, 1)
        ok, _, _ = billing.redeem_code(conn, codes[0], user)
        assert ok
        sub = billing.get_subscription(conn, user)
        assert sub["plan"] == "pro" and sub["days_left"] >= 29

    def test_expired_code_is_refused(self, conn, user):
        _, codes = billing.generate_codes(
            conn, {"kind": "api_credit", "amount_cny": 5}, 1,
            expires_at="2000-01-01T00:00:00+00:00",
        )
        ok, reason, _ = billing.redeem_code(conn, codes[0], user)
        assert not ok and reason == "code_expired"

    def test_unknown_code_is_refused(self, conn, user):
        ok, reason, _ = billing.redeem_code(conn, "TAI-ZZZZ-ZZZZ-ZZZZ", user)
        assert not ok and reason == "invalid_code"

    def test_batch_generation_is_unique(self, conn):
        _, codes = billing.generate_codes(conn, {"kind": "api_credit", "amount_cny": 1}, 200)
        assert len(set(codes)) == 200


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #

class TestOrders:
    def test_fulfilment_is_idempotent(self, conn, user):
        order = billing.create_order(conn, user, "api_credit", 50)
        ok, first = billing.mark_order_paid(conn, order["id"])
        assert ok and first["credited_cny"] == 50

        ok2, second = billing.mark_order_paid(conn, order["id"])
        assert ok2 and second.get("already_fulfilled") is True
        assert db.account_state(conn, user)["balance_cny"] == 50, "replaying must not double-credit"

    def test_a_trade_number_cannot_be_reused_across_orders(self, conn, user):
        a = billing.create_order(conn, user, "api_credit", 10)
        b = billing.create_order(conn, user, "api_credit", 10)
        billing.mark_order_paid(conn, a["id"], provider_trade_no="WX-1")

        ok, result = billing.mark_order_paid(conn, b["id"], provider_trade_no="WX-1")
        assert not ok and result["error"] == "trade_no_already_used"
        assert db.account_state(conn, user)["balance_cny"] == 10, "second order must stay unfulfilled"

    def test_rejects_nonsense(self, conn, user):
        with pytest.raises(ValueError):
            billing.create_order(conn, user, "nonsense", 10)
        with pytest.raises(ValueError):
            billing.create_order(conn, user, "api_credit", -1)
        with pytest.raises(ValueError):
            billing.create_order(conn, user, "api_credit", 10, provider="paypal")


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #

class TestSubscriptions:
    def test_pro_extends_rather_than_resets(self, conn, user):
        billing.grant_pro(conn, user, 1)
        first = billing.get_subscription(conn, user)["expires_at"]
        billing.grant_pro(conn, user, 1)
        second = billing.get_subscription(conn, user)["expires_at"]
        assert second > first, "buying twice must stack, not restart the clock"
        assert billing.get_subscription(conn, user)["days_left"] > 55

    def test_expired_pro_falls_back_to_free(self, conn, user):
        conn.execute(
            "INSERT INTO subscriptions (user_id, plan, expires_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET plan='pro', expires_at=excluded.expires_at",
            (user, "pro", "2000-01-01T00:00:00+00:00", db.iso(db.utcnow())),
        )
        conn.commit()
        sub = billing.get_subscription(conn, user)
        assert sub["plan"] == "free" and sub["active"] is False

    def test_entitlements_bundle_everything_a_client_needs(self, conn, user):
        ent = billing.entitlements(conn, user)
        assert ent["plan"] == "free"
        assert ent["can_generate"] is True
        assert set(ent["free_tokens"]) == {"granted", "used", "remaining"}
        assert ent["balance_cny"] == 0
