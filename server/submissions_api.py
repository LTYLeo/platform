"""Contact and job-application submissions.

These are the only endpoints an unauthenticated visitor can write to, so they are
deliberately narrow: a fixed set of fields, a size cap, a rate limit, and no way
to read anything back. A form that silently discards what someone typed is worse
than no form at all — the sender believes it arrived — so these really store.

Reading them is staff-only, through ``/api/admin/submissions`` or
``python3 -m server.admin_cli submissions``.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status

from server import db, notify
from server.deps import client_ip, fail, new_id, require_admin

router = APIRouter(prefix="/api/submissions", tags=["submissions"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])

KINDS = ("contact", "careers")

MAX_MESSAGE = 5000
MAX_NAME = 120
MAX_TOPIC = 120
MAX_FILE_BYTES = 10 * 1024 * 1024          # 10 MB is generous for a resume
RESUME_SUFFIXES = (".pdf", ".doc", ".docx", ".txt", ".md", ".rtf")

# Per address, per kind, per window. Low on purpose: this is a contact form, not
# an API, and a person sending five messages an hour is already unusual.
RATE_LIMIT = 5
RATE_WINDOW_HOURS = 1

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _resumes_dir() -> pathlib.Path:
    path = pathlib.Path(db.DATA_DIR) / "resumes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _recent_count(conn: sqlite3.Connection, ip: str, kind: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM submissions
        WHERE ip = ? AND kind = ?
          AND created_at > ?
        """,
        (ip, kind, db.iso(db.utcnow() - __import__("datetime").timedelta(hours=RATE_WINDOW_HOURS))),
    ).fetchone()
    return int(row["n"])


def _check_rate(conn: sqlite3.Connection, ip: str, kind: str) -> None:
    if _recent_count(conn, ip, kind) >= RATE_LIMIT:
        raise fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many submissions from this address. Try again later, or email us directly.",
        )


def _clean(value: str | None, limit: int, field: str, *, required: bool = True) -> str:
    text = (value or "").strip()
    if required and not text:
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request",
                   f"`{field}` is required", param=field)
    if len(text) > limit:
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request",
                   f"`{field}` is too long (limit {limit})", param=field)
    return text


def _store(conn: sqlite3.Connection, *, kind: str, name: str, email: str, topic: str,
           message: str, extra: dict | None, ip: str,
           file_path: str | None = None, file_name: str | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO submissions
            (kind, name, email, topic, message, extra_json, file_path, file_name, ip, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (kind, name, email, topic, message,
         json.dumps(extra, ensure_ascii=False) if extra else None,
         file_path, file_name, ip, db.iso(db.utcnow())),
    )
    conn.commit()
    return int(cur.lastrowid)


@router.post("/contact", status_code=status.HTTP_201_CREATED)
def submit_contact(payload: dict, request: Request,
                   conn: sqlite3.Connection = Depends(db.get_db)):
    ip = client_ip(request)
    _check_rate(conn, ip, "contact")

    name = _clean(payload.get("name"), MAX_NAME, "name")
    email = _clean(payload.get("email"), MAX_NAME, "email")
    if not EMAIL_RE.match(email):
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_email",
                   "Please enter a valid email address", param="email")
    subject = _clean(payload.get("subject"), MAX_TOPIC, "subject")
    message = _clean(payload.get("message"), MAX_MESSAGE, "message")

    submission_id = _store(conn, kind="contact", name=name, email=email,
                           topic=subject, message=message, extra=None, ip=ip)
    notify.submission("contact", name, email, subject, message, submission_id=submission_id)
    return {"status": "success", "id": submission_id,
            "message": "Thank you — your message has reached us."}


@router.post("/careers", status_code=status.HTTP_201_CREATED)
async def submit_careers(
    request: Request,
    # One name field. "First" and "last" do not describe Chinese names: written
    # in Latin order a Chinese name reads backwards, and asking for two halves
    # invites the wrong one into each box.
    name: str | None = Form(default=None),
    first_name: str | None = Form(default=None),
    last_name: str | None = Form(default=None),
    email: str = Form(...),
    position: str = Form(...),
    message: str = Form(...),
    portfolio: str = Form(default=""),
    resume: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(db.get_db),
):
    ip = client_ip(request)
    _check_rate(conn, ip, "careers")

    full = _clean(name, MAX_NAME, "name", required=False)
    if not full:
        # A client still sending the two halves keeps working.
        first = _clean(first_name, MAX_NAME, "firstName", required=False)
        last = _clean(last_name, MAX_NAME, "lastName", required=False)
        full = ("%s %s" % (first, last)).strip()
        if not full:
            raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request",
                       "`name` is required", param="name")
    mail = _clean(email, MAX_NAME, "email")
    if not EMAIL_RE.match(mail):
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_email",
                   "Please enter a valid email address", param="email")
    role = _clean(position, MAX_TOPIC, "position")
    note = _clean(message, MAX_MESSAGE, "message")
    link = _clean(portfolio, 400, "portfolio", required=False)

    suffix = pathlib.Path(resume.filename or "").suffix.lower()
    if suffix not in RESUME_SUFFIXES:
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_file",
                   "Resume must be one of: " + ", ".join(RESUME_SUFFIXES), param="resume")

    blob = await resume.read()
    if not blob:
        raise fail(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_file",
                   "The resume file is empty", param="resume")
    if len(blob) > MAX_FILE_BYTES:
        raise fail(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large",
                   f"Resume must be under {MAX_FILE_BYTES // (1024 * 1024)} MB", param="resume")

    # Never trust the uploaded name as a path: keep only a safe suffix and build
    # the filename ourselves, so `../../etc/passwd` cannot escape the directory.
    stored_name = "%s%s" % (new_id("sub"), suffix)
    target = _resumes_dir() / stored_name
    target.write_bytes(blob)

    submission_id = _store(
        conn, kind="careers", name=full, email=mail, topic=role,
        message=note, extra={"portfolio": link} if link else None, ip=ip,
        file_path=str(pathlib.Path("resumes") / stored_name),
        file_name=resume.filename or stored_name,
    )
    notify.submission("careers", full, mail, role, note,
                      extra={"portfolio": link} if link else None,
                      file_name=resume.filename, submission_id=submission_id)
    return {"status": "success", "id": submission_id,
            "message": "Thank you — your application has reached us."}


@admin_router.get("/submissions")
def list_submissions(
    kind: str | None = None,
    unhandled_only: bool = False,
    limit: int = 100,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin=Depends(require_admin),
):
    sql = "SELECT * FROM submissions"
    where, params = [], []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if unhandled_only:
        where.append("handled_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))

    rows = conn.execute(sql, params).fetchall()
    return {
        "object": "list",
        "data": [
            {
                "id": r["id"], "kind": r["kind"], "name": r["name"], "email": r["email"],
                "topic": r["topic"], "message": r["message"],
                "extra": json.loads(r["extra_json"]) if r["extra_json"] else None,
                "file": r["file_path"], "file_name": r["file_name"],
                "created_at": r["created_at"], "handled_at": r["handled_at"],
            }
            for r in rows
        ],
    }


@admin_router.post("/submissions/{submission_id}/handled")
def mark_handled(
    submission_id: int,
    conn: sqlite3.Connection = Depends(db.get_db),
    admin=Depends(require_admin),
):
    if conn.execute("SELECT 1 FROM submissions WHERE id = ?", (submission_id,)).fetchone() is None:
        raise fail(status.HTTP_404_NOT_FOUND, "not_found", "No such submission")
    conn.execute("UPDATE submissions SET handled_at = ? WHERE id = ?",
                 (db.iso(db.utcnow()), submission_id))
    conn.commit()
    return {"status": "ok", "id": submission_id}
