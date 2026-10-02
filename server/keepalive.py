"""Watchdog: restart the stack if a service stops answering.

Run by launchd every couple of minutes. Deliberately outside the app - a process
that supervises itself stops supervising the moment it is the thing that died.

Written as a Python module rather than a shell script because of where this
checkout lives. macOS blocks launchd from *executing* a file under ~/Desktop;
reading there is allowed, which is why the backup job works and a .sh beside it
did not. Running through the interpreter, the same way the other two jobs do,
sidesteps that entirely.

Everything here assumes it may be run while the machine is busy and the services
are half up, so nothing raises: a watchdog that dies on its first error is worse
than no watchdog.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

#: name -> (port, path, environment override)
SERVICES = (
    ("platform", 8000, "/api/health", "TAI_PLATFORM_PORT"),
    ("inference", 8001, "/health", "TAI_INFERENCE_PORT"),
    ("tfmf", 8002, "/health", "TAI_TFMF_PORT"),
    ("sigma", 5001, "/health", "TAI_SIGMA_PORT"),
)

TIMEOUT = 5
#: How long to wait after a restart before checking again. The model server
#: loads several gigabytes; reporting failure before it has finished would
#: restart it forever.
SETTLE_SECONDS = 90


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _log_dir() -> pathlib.Path:
    path = _root().parent / "tai-backend" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log(message: str) -> None:
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with (_log_dir() / "keepalive.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def port_for(name: str, default: int, override: str) -> int:
    return int(os.getenv(override) or default)


def alive(port: int, path: str) -> bool:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            headers={"ngrok-skip-browser-warning": "true"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        # An HTTP error still means something is listening and answering.
        return exc.code < 500
    except Exception:
        return False


def check() -> list[str]:
    down = []
    for name, port, path, override in SERVICES:
        if not alive(port_for(name, port, override), path):
            down.append(name)
    return down


def restart() -> int:
    script = _root().parent / "tai-backend" / "start.sh"
    if not script.exists():
        log("start.sh not found at %s" % script)
        return 1
    try:
        # --no-tunnel: the tunnel is managed separately and restarting it would
        # change the public URL that the deployed site has cached.
        proc = subprocess.run([str(script), "--no-tunnel"], cwd=str(script.parent),
                              capture_output=True, text=True, timeout=600)
        tail = (proc.stdout or "")[-400:]
        if tail:
            log("start.sh said: %s" % tail.replace("\n", " | ")[-300:])
        return proc.returncode
    except subprocess.TimeoutExpired:
        log("start.sh timed out")
        return 1
    except Exception as exc:
        log("start.sh failed: %s: %s" % (type(exc).__name__, exc))
        return 1


def run() -> int:
    down = check()
    if not down:
        # Quiet when healthy. The .keepalive-last-ok stamp keeps the log file
        # from growing by one line every two minutes forever.
        stamp = _log_dir() / ".keepalive-last-ok"
        try:
            old = stamp.stat().st_mtime
        except OSError:
            old = 0
        if time.time() - old > 3600:
            log("all services up")
            try:
                stamp.touch()
            except OSError:
                pass
        return 0

    log("down: %s - restarting the stack" % ", ".join(down))
    rc = restart()

    time.sleep(SETTLE_SECONDS)
    still = check()
    if still:
        log("restart finished (rc=%s) but still down: %s" % (rc, ", ".join(still)))
        return 1
    log("recovered")
    return 0


if __name__ == "__main__":
    sys.exit(run())
