"""Email notification for form submissions.

Two rules shape this module.

**The submission is already stored before this runs.** Email is a convenience for
whoever reads the forms, not the record of them. A mail server that is slow,
misconfigured or down must not turn away a message someone took the time to
write, so every failure here is logged and swallowed.

**It must not block the response.** SMTP handshakes take seconds; a visitor
should not watch a spinner for them. Sending happens on a daemon thread, so the
form returns as soon as the row is committed.

Configuration (all optional — with none of it set, notifications simply do not
send and the API says so once at startup):

    TAI_SMTP_HOST      smtp.qq.com
    TAI_SMTP_PORT      465            (465 = implicit TLS, 587 = STARTTLS)
    TAI_SMTP_USER      1637321445@qq.com
    TAI_SMTP_PASSWORD  an app authorisation code, NOT the account password
    TAI_NOTIFY_EMAIL   where to send
"""

from __future__ import annotations

import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

HOST = (os.getenv("TAI_SMTP_HOST") or "").strip()
PORT = int(os.getenv("TAI_SMTP_PORT") or 465)
USER = (os.getenv("TAI_SMTP_USER") or "").strip()
PASSWORD = (os.getenv("TAI_SMTP_PASSWORD") or "").strip()
TO = (os.getenv("TAI_NOTIFY_EMAIL") or USER).strip()

SUBJECT_PREFIX = {
    "contact": "[TAI] New contact message",
    "careers": "[TAI] New job application",
}


def _ssl_context() -> ssl.SSLContext:
    """A context that actually verifies the certificate.

    python.org builds on macOS ship no CA bundle, so ``create_default_context()``
    fails with "unable to get local issuer certificate" on a perfectly good
    connection. certifi carries its own bundle and is already a transitive
    dependency of the SDK's httpx; falling back to the system store keeps this
    working where certifi is absent.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def configured() -> bool:
    return bool(HOST and USER and PASSWORD and TO)


def describe() -> str:
    if configured():
        return "email notifications -> %s via %s:%s" % (TO, HOST, PORT)
    return ("email notifications are OFF (set TAI_SMTP_HOST, TAI_SMTP_USER, "
            "TAI_SMTP_PASSWORD and TAI_NOTIFY_EMAIL to enable them)")


def _body(kind: str, name: str, email: str, topic: str, message: str,
          extra: dict | None, file_name: str | None, submission_id: int | None) -> str:
    lines = [
        "A form on the developer platform was submitted.",
        "",
        "Kind:     %s" % kind,
        "Name:     %s" % name,
        "Email:    %s" % email,
        "Topic:    %s" % (topic or "-"),
        "Received: %s" % __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]
    if submission_id is not None:
        lines.append("Record:   #%s (server.admin_cli submissions)" % submission_id)
    if extra:
        for key, value in extra.items():
            if value:
                lines.append("%-9s %s" % (key.title() + ":", value))
    if file_name:
        lines.append("Resume:   %s" % file_name)
    lines += ["", "-" * 60, "", message, "", "-" * 60, "",
              "Reply directly to this email to answer %s." % email]
    return "\n".join(lines)


def _send(kind: str, name: str, email: str, topic: str, message: str,
          extra: dict | None, file_name: str | None, submission_id: int | None) -> None:
    try:
        msg = EmailMessage()
        msg["Subject"] = "%s — %s" % (SUBJECT_PREFIX.get(kind, "[TAI] New submission"), topic or name)
        msg["From"] = USER
        msg["To"] = TO
        # So hitting reply in the mail client answers the person who wrote in,
        # not ourselves.
        msg["Reply-To"] = email
        msg.set_content(_body(kind, name, email, topic, message, extra, file_name, submission_id))

        context = _ssl_context()
        if PORT == 465:
            with smtplib.SMTP_SSL(HOST, PORT, timeout=20, context=context) as server:
                server.login(USER, PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(HOST, PORT, timeout=20) as server:
                server.starttls(context=context)
                server.login(USER, PASSWORD)
                server.send_message(msg)
        print("[notify] sent %s notification to %s" % (kind, TO), flush=True)
    except Exception as exc:  # noqa: BLE001 - a mail failure is never the caller's problem
        print("[notify] FAILED to send %s notification: %s: %s"
              % (kind, type(exc).__name__, exc), flush=True)


def submission(kind: str, name: str, email: str, topic: str, message: str,
               extra: dict | None = None, file_name: str | None = None,
               submission_id: int | None = None) -> None:
    """Queue a notification. Returns immediately, whatever happens to the mail."""
    if not configured():
        return
    thread = threading.Thread(
        target=_send,
        args=(kind, name, email, topic, message, extra, file_name, submission_id),
        daemon=True,   # never hold the process open for an email
    )
    thread.start()
