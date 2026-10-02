"""Service monitoring and email alerts.

Run this periodically - cron, launchd, or by hand. It checks each service, and
when something is down it emails the operator. When everything is healthy it
stays silent.

Three things it does that a bare `curl` loop would not:

**It does not spam.** A service that is down for six hours must produce one
message, not one per minute. Failures are recorded in a state file and only a
change of state sends mail.

**It distinguishes down from unreachable.** The chat backend and the model
server fail independently, and "the whole machine is off" reads differently from
"one process died" when you are trying to work out what to fix.

**It checks that backups are still happening.** A backup schedule that quietly
stopped is invisible, and it is precisely the failure you discover on the day you
need the backup.

Usage::

    python3 -m server.monitor            # check, alert on change
    python3 -m server.monitor --quiet    # check, exit code only
    python3 -m server.monitor --test     # send a test alert
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import smtplib
import ssl
import sys
import urllib.error
import urllib.request

from server import backup, db, notify

#: name -> (url, what it means when this is down)
SERVICES = (
    ("platform", "http://127.0.0.1:8000/api/health", "accounts, keys, billing and the API"),
    ("inference", "http://127.0.0.1:8001/health", "the torch models"),
    ("tfmf", "http://127.0.0.1:8002/health", "TFMF, served by llama.cpp"),
    ("sigma", "http://127.0.0.1:5001/health", "the chat platform"),
)

STATE_PATH = db.DATA_DIR / "monitor-state.json"
TIMEOUT = 8

#: Warn when the newest backup is older than this. Daily backups plus slack.
BACKUP_MAX_AGE_HOURS = int(os.getenv("TAI_BACKUP_MAX_AGE_HOURS") or 30)


def _get(url: str, timeout: int = TIMEOUT) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "true"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", "replace")
            return (200 <= resp.status < 400), "HTTP %s" % resp.status
    except urllib.error.HTTPError as exc:
        # An HTTP error still proves something is listening and answering.
        return exc.code < 500, "HTTP %s" % exc.code
    except Exception as exc:                        # noqa: BLE001
        return False, type(exc).__name__


def check_services() -> dict[str, dict]:
    results = {}
    for name, url, purpose in SERVICES:
        ok, detail = _get(url)
        results[name] = {"ok": ok, "detail": detail, "purpose": purpose}
    return results


def check_backups() -> dict:
    dest = backup.default_dir()
    files = sorted(dest.glob("app-*.db")) if dest.exists() else []
    if not files:
        return {"ok": False, "detail": "no backup has ever been taken", "dir": str(dest)}

    newest = max(files, key=lambda p: p.stat().st_mtime)
    age_hours = (datetime.datetime.now().timestamp() - newest.stat().st_mtime) / 3600
    ok = age_hours <= BACKUP_MAX_AGE_HOURS
    return {
        "ok": ok,
        "detail": "newest %s, %.1f h old" % (newest.name, age_hours),
        "dir": str(dest),
        "age_hours": round(age_hours, 1),
        "offsite": backup.is_offsite(dest),
        "count": len(files),
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def send(subject: str, body: str) -> bool:
    """Send one alert through the same settings the forms use."""
    if not notify.configured():
        print("[monitor] cannot send: email is not configured", file=sys.stderr)
        return False
    try:
        msg = __import__("email.message", fromlist=["EmailMessage"]).EmailMessage()
        msg["Subject"] = subject
        msg["From"] = notify.USER
        msg["To"] = notify.TO
        msg.set_content(body)

        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        if notify.PORT == 465:
            with smtplib.SMTP_SSL(notify.HOST, notify.PORT, timeout=20, context=ctx) as server:
                server.login(notify.USER, notify.PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(notify.HOST, notify.PORT, timeout=20) as server:
                server.starttls(context=ctx)
                server.login(notify.USER, notify.PASSWORD)
                server.send_message(msg)
        print("[monitor] sent: %s" % subject)
        return True
    except Exception as exc:                        # noqa: BLE001
        print("[monitor] send failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return False


def run(quiet: bool = False) -> int:
    services = check_services()
    backups = check_backups()

    broken = {k: v for k, v in services.items() if not v["ok"]}
    healthy = not broken and backups["ok"]

    state = load_state()
    previous = set(state.get("broken") or [])
    current = set(broken)

    if not quiet:
        for name, info in services.items():
            mark = "up  " if info["ok"] else "DOWN"
            print("  %-10s %s  %s" % (name, mark, info["detail"]))
        mark = "ok  " if backups["ok"] else "STALE"
        print("  %-10s %s  %s" % ("backups", mark, backups["detail"]))
        if not backups.get("offsite", True):
            print("  %-10s WARN  same disk as the database" % "")

    # Only a change in the set of failures is worth an email.
    if current != previous:
        if current:
            lines = ["These services are not responding:", ""]
            for name in sorted(current):
                lines.append("  %-10s %s   (%s)" % (name, services[name]["detail"],
                                                    services[name]["purpose"]))
            still = previous & current
            if still:
                lines += ["", "Still down from before: " + ", ".join(sorted(still))]
            lines += ["", "Checked %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
            send("[TAI] %d service(s) down" % len(current), "\n".join(lines))
        elif previous:
            send("[TAI] recovered", "Back up: %s\n\nChecked %s"
                 % (", ".join(sorted(previous)),
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    if not backups["ok"] and "backups" not in previous:
        send("[TAI] backups are stale", "%s\n\nDirectory: %s"
             % (backups["detail"], backups.get("dir")))
        current.add("backups")

    state["broken"] = sorted(current)
    state["checked_at"] = db.iso(db.utcnow())
    state["services"] = {k: v["ok"] for k, v in services.items()}
    save_state(state)

    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the services and alert on change.")
    ap.add_argument("--quiet", action="store_true", help="no output, exit code only")
    ap.add_argument("--test", action="store_true", help="send a test alert and exit")
    args = ap.parse_args(argv)

    if args.test:
        ok = send("[TAI] test alert",
                  "If you are reading this, monitoring can reach you.\n\n%s" % notify.describe())
        return 0 if ok else 1
    return run(quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
