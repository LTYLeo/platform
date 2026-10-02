"""Staff tooling.

The admin API is guarded by ``users.is_admin``, so there has to be a way to set
that flag that is not "the first person to register becomes an admin" — that
would hand the platform to whoever signs up first on a public deployment.

Usage::

    python3 -m server.admin_cli promote you@example.com
    python3 -m server.admin_cli list-admins
    python3 -m server.admin_cli grant-credit someone@example.com 50 "launch bonus"
    python3 -m server.admin_cli set-plan someone@example.com pro 1
    python3 -m server.admin_cli gen-codes api_credit 10 --amount 20
    python3 -m server.admin_cli gen-codes sigma_pro 5 --months 1
    python3 -m server.admin_cli orders --status pending
    python3 -m server.admin_cli mark-paid ord_xxxx

Run it on the machine that holds the database (the Pi), not over the network.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from server import billing, db


def _find_user(conn, email: str):
    row = conn.execute(
        "SELECT * FROM users WHERE lower(email) = lower(?)", (email,)
    ).fetchone()
    if row is None:
        print("No such user: %s" % email, file=sys.stderr)
        raise SystemExit(1)
    return row


def cmd_promote(args) -> None:
    conn = db.connect()
    row = _find_user(conn, args.email)
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))
    billing.audit(conn, None, "cli.promote", {"email": args.email, "user_id": row["id"]})
    conn.commit()
    print("%s (id=%s) is now an administrator." % (row["email"], row["id"]))
    conn.close()


def cmd_demote(args) -> None:
    conn = db.connect()
    row = _find_user(conn, args.email)
    conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (row["id"],))
    billing.audit(conn, None, "cli.demote", {"email": args.email, "user_id": row["id"]})
    conn.commit()
    print("%s (id=%s) is no longer an administrator." % (row["email"], row["id"]))
    conn.close()


def cmd_list_admins(_args) -> None:
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, email, name, created_at FROM users WHERE is_admin = 1 ORDER BY id"
    ).fetchall()
    if not rows:
        print("No administrators yet. Use: promote <email>")
    for r in rows:
        print("  id=%-4s %-32s %s" % (r["id"], r["email"], r["name"]))
    conn.close()


def cmd_grant_credit(args) -> None:
    conn = db.connect()
    row = _find_user(conn, args.email)
    balance = db.grant_credit(conn, int(row["id"]), float(args.amount), args.reason)
    billing.audit(conn, None, "cli.grant_credit",
                  {"user_id": row["id"], "amount_cny": args.amount, "reason": args.reason})
    conn.commit()
    print("Granted ¥%.2f to %s. New balance: ¥%.2f" % (args.amount, row["email"], balance))
    conn.close()


def cmd_set_plan(args) -> None:
    conn = db.connect()
    row = _find_user(conn, args.email)
    result = billing.set_plan(conn, int(row["id"]), args.plan, args.months or 1)
    billing.audit(conn, None, "cli.set_plan", {"user_id": row["id"], **result})
    conn.commit()
    print("%s -> %s%s" % (row["email"], result["plan"],
                          " until %s" % result.get("expires_at") if result.get("expires_at") else ""))
    conn.close()


def cmd_gen_codes(args) -> None:
    conn = db.connect()
    if args.kind == "api_credit":
        grants = {"kind": "api_credit", "amount_cny": args.amount}
    else:
        grants = {"kind": "sigma_pro", "months": args.months or 1}

    batch_id, codes = billing.generate_codes(
        conn, grants, args.count, note=args.note, expires_at=args.expires
    )
    print("batch %s  (%s x %s)" % (batch_id, args.count, grants))
    for code in codes:
        print("  %s" % code)
    conn.close()


def cmd_orders(args) -> None:
    conn = db.connect()
    rows = billing.list_orders(conn, status=args.status, limit=args.limit)
    if not rows:
        print("No orders%s." % (" with status %s" % args.status if args.status else ""))
    for r in rows:
        print("  %-22s %-10s %-11s ¥%-8.2f %-22s %s" % (
            r["id"], r["status"], r["purpose"], r["amount_cny"], r["user_email"], r["created_at"]))
    conn.close()


def cmd_mark_paid(args) -> None:
    conn = db.connect()
    ok, result = billing.mark_order_paid(conn, args.order_id, provider_trade_no=args.trade_no)
    print(("OK: " if ok else "FAILED: ") + str(result))
    conn.close()
    if not ok:
        raise SystemExit(1)


def cmd_users(args) -> None:
    conn = db.connect()
    sql = "SELECT id, email, name, is_admin, created_at FROM users"
    params: list = []
    if args.q:
        sql += " WHERE email LIKE ? OR name LIKE ?"
        params += ["%%%s%%" % args.q, "%%%s%%" % args.q]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    for r in conn.execute(sql, params):
        ent = billing.entitlements(conn, int(r["id"]))
        print("  id=%-4s %-30s %-8s ¥%-8.2f free=%-6s admin=%s" % (
            r["id"], r["email"], ent["plan"], ent["balance_cny"],
            ent["free_tokens"]["remaining"], bool(r["is_admin"])))
    conn.close()


def cmd_submissions(args) -> None:
    """Read what visitors sent through the contact and careers forms."""
    conn = db.connect()
    sql = "SELECT * FROM submissions"
    where, params = [], []
    if args.kind:
        where.append("kind = ?")
        params.append(args.kind)
    if args.unhandled:
        where.append("handled_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No submissions%s." % (" of kind " + args.kind if args.kind else ""))
    for r in rows:
        flag = " " if r["handled_at"] else "*"
        print("%s #%-4s %-8s %-18s %-28s %s" % (
            flag, r["id"], r["kind"], r["name"], r["email"], r["created_at"]))
        print("        topic:   %s" % (r["topic"] or "-"))
        print("        message: %s" % r["message"].replace("\n", "\n                 "))
        if r["extra_json"]:
            print("        extra:   %s" % r["extra_json"])
        if r["file_path"]:
            full = pathlib.Path(db.DATA_DIR) / r["file_path"]
            print("        resume:  %s (%s)" % (full, r["file_name"]))
    unhandled = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE handled_at IS NULL").fetchone()["n"]
    print()
    print("%d shown, %d unhandled in total  (* = unhandled)" % (len(rows), unhandled))
    conn.close()


def cmd_handled(args) -> None:
    conn = db.connect()
    conn.execute("UPDATE submissions SET handled_at = ? WHERE id = ?",
                 (db.iso(db.utcnow()), args.submission_id))
    conn.commit()
    print("Marked #%s as handled." % args.submission_id)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="server.admin_cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("promote", help="grant administrator rights")
    p.add_argument("email"); p.set_defaults(func=cmd_promote)

    p = sub.add_parser("demote", help="revoke administrator rights")
    p.add_argument("email"); p.set_defaults(func=cmd_demote)

    p = sub.add_parser("list-admins", help="show all administrators")
    p.set_defaults(func=cmd_list_admins)

    p = sub.add_parser("users", help="list accounts with plan and balance")
    p.add_argument("--q", default=None); p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("grant-credit", help="add API credit to an account")
    p.add_argument("email"); p.add_argument("amount", type=float)
    p.add_argument("reason", nargs="?", default="manual grant")
    p.set_defaults(func=cmd_grant_credit)

    p = sub.add_parser("set-plan", help="set the Sigma plan")
    p.add_argument("email"); p.add_argument("plan", choices=["free", "pro", "admin"])
    p.add_argument("months", nargs="?", type=int, default=1)
    p.set_defaults(func=cmd_set_plan)

    p = sub.add_parser("gen-codes", help="mint redeem codes")
    p.add_argument("kind", choices=["api_credit", "sigma_pro"])
    p.add_argument("count", type=int)
    p.add_argument("--amount", type=float, default=10.0, help="for api_credit")
    p.add_argument("--months", type=int, default=1, help="for sigma_pro")
    p.add_argument("--note", default=None)
    p.add_argument("--expires", default=None, help="ISO date, e.g. 2026-12-31T00:00:00+00:00")
    p.set_defaults(func=cmd_gen_codes)

    p = sub.add_parser("orders", help="list orders")
    p.add_argument("--status", default=None); p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_orders)

    p = sub.add_parser("submissions", help="read contact and job-application submissions")
    p.add_argument("--kind", choices=["contact", "careers"], default=None)
    p.add_argument("--unhandled", action="store_true", help="only ones not yet marked handled")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_submissions)

    p = sub.add_parser("handled", help="mark a submission as dealt with")
    p.add_argument("submission_id", type=int)
    p.set_defaults(func=cmd_handled)

    p = sub.add_parser("mark-paid", help="confirm an order arrived and deliver it")
    p.add_argument("order_id"); p.add_argument("--trade-no", default=None)
    p.set_defaults(func=cmd_mark_paid)

    args = parser.parse_args()
    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
