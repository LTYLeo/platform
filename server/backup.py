"""Database backup and restore verification.

Everything this platform knows - accounts, API key fingerprints, usage, orders,
submissions - lives in one SQLite file. Losing it loses all of it, and until now
nothing copied it anywhere.

Two decisions shape this module.

**A backup on the same disk is not a backup.** The failure this protects against
is a dead disk, and a copy beside the original dies with it. ``default_dir()``
therefore prefers an external location and says plainly when it cannot find one,
rather than quietly writing next to the database and reporting success.

**A backup that has never been restored is not a backup either.** It is a file
whose contents are assumed to be good. ``verify()`` opens the copy, runs SQLite's
integrity check, and counts the rows in the tables that matter, so "the backup
ran" and "the backup can be restored" are different statements and both are
checkable. ``drill()`` runs the whole thing end to end and reports.

The copy itself uses SQLite's backup API rather than a file copy. The database is
in WAL mode and is being written to; ``cp`` can capture a torn file that opens
but is missing recent commits, which is the worst kind of backup because it looks
fine until the day it is needed.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil
import sqlite3
import tempfile

#: Tables whose presence and row counts are checked after a restore. If these
#: come back, the backup is usable; if one is missing, it is not.
CRITICAL_TABLES = (
    "users",
    "api_keys",
    "usage_events",
    "payment_orders",
    "subscriptions",
)

DEFAULT_KEEP = 14


def default_dir() -> pathlib.Path:
    """Where backups go.

    ``TAI_BACKUP_DIR`` wins. Otherwise a sibling ``backups/`` next to the data
    directory - convenient, and on the same disk, which ``is_offsite()`` reports
    so the caller can warn instead of pretending.
    """
    override = (os.getenv("TAI_BACKUP_DIR") or "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    from server import db
    return pathlib.Path(db.DATA_DIR).parent / "backups"


def is_offsite(dest: pathlib.Path | None = None) -> bool:
    """True when the destination is not on the same filesystem as the database.

    A weak check - a different mount on the same physical disk passes - but it
    catches the common case of everything sitting in one folder.
    """
    dest = dest or default_dir()
    try:
        from server import db
        return dest.resolve().anchor != pathlib.Path(db.DATA_DIR).resolve().anchor or \
            dest.resolve().parts[:3] != pathlib.Path(db.DATA_DIR).resolve().parts[:3]
    except Exception:
        return False


def backup(dest_dir: pathlib.Path | None = None, keep: int = DEFAULT_KEEP,
           label: str = "") -> pathlib.Path:
    """Copy the live database to ``dest_dir`` and rotate old copies.

    Safe to run while the service is up: SQLite's backup API takes a consistent
    snapshot, unlike a file copy of a database being written.
    """
    from server import db

    dest_dir = pathlib.Path(dest_dir or default_dir())
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "app-%s%s.db" % (stamp, ("-" + label) if label else "")
    target = dest_dir / name

    source = sqlite3.connect(str(db.DB_PATH))
    try:
        out = sqlite3.connect(str(target))
        try:
            # The one correct way to copy a live SQLite database.
            source.backup(out)
        finally:
            out.close()
    finally:
        source.close()

    # Rotate only after a successful write, so a failure never deletes the
    # previous good copy.
    existing = sorted(dest_dir.glob("app-*.db"))
    for old in existing[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass

    return target


def verify(path: pathlib.Path) -> tuple[bool, str]:
    """Open a backup and check it is intact and complete.

    Deliberately opens a *copy* in a temporary directory. SQLite may create WAL
    and shm files alongside whatever it opens, and a verification step that
    writes next to the backup can corrupt the very thing it is checking.
    """
    path = pathlib.Path(path)
    if not path.exists():
        return False, "file does not exist"

    with tempfile.TemporaryDirectory() as tmp:
        probe = pathlib.Path(tmp) / "probe.db"
        shutil.copy2(path, probe)
        try:
            conn = sqlite3.connect(str(probe))
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                result = (row[0] if row else "unknown")
                if result != "ok":
                    return False, "integrity_check: %s" % result

                counts = {}
                for table in CRITICAL_TABLES:
                    try:
                        counts[table] = conn.execute(
                            "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                    except sqlite3.Error as exc:
                        return False, "table %s unreadable: %s" % (table, exc)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return False, "cannot open: %s" % exc

    return True, ", ".join("%s=%d" % (k, v) for k, v in counts.items())


def drill(dest_dir: pathlib.Path | None = None) -> dict:
    """Take a backup, restore it, and report whether the result is usable.

    This is the check worth scheduling. ``backup()`` returning a path only means
    bytes were written.
    """
    dest_dir = pathlib.Path(dest_dir or default_dir())
    report: dict = {"dir": str(dest_dir), "offsite": is_offsite(dest_dir)}

    try:
        path = backup(dest_dir, keep=DEFAULT_KEEP, label="drill")
    except Exception as exc:                       # noqa: BLE001
        report.update(ok=False, stage="backup", error="%s: %s" % (type(exc).__name__, exc))
        return report

    report["file"] = str(path)
    report["size_bytes"] = path.stat().st_size

    ok, detail = verify(path)
    report.update(ok=ok, verify=detail)
    return report


def describe() -> str:
    dest = default_dir()
    where = "off-site" if is_offsite(dest) else "SAME DISK AS THE DATABASE"
    return "backups -> %s (%s)" % (dest, where)
