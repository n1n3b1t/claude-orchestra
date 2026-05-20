"""Tiny client for the orchestra hook daemon. ~5-10 ms per send.

Used by the `orchestra worker hook EVENT` fast path. Public API:
- send(sock_path, event, worker_id, payload, *, connect_timeout=0.5) -> bool
- ensure_daemon_and_send(...) -> bool  (lazy-spawn + retry once)
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def send(
    sock_path: Path,
    *,
    event: str,
    worker_id: str,
    payload: dict[str, Any],
    connect_timeout: float = 0.5,
) -> bool:
    """Send one event to the daemon. Returns True on success, False otherwise."""
    if not sock_path.exists():
        return False
    line = json.dumps({
        "event": event,
        "worker_id": worker_id,
        "payload": payload,
        "ts": _now_iso(),
    }, separators=(",", ":")) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(connect_timeout)
            s.connect(str(sock_path))
            s.sendall(line.encode("utf-8"))
        return True
    except (OSError, TimeoutError):
        return False


def _spawn_daemon(state_db: Path) -> None:
    """Double-fork so the daemon is reparented to init."""
    pid = os.fork()
    if pid > 0:
        # Reap intermediate child so it doesn't linger as a zombie.
        with contextlib.suppress(OSError):
            os.waitpid(pid, 0)
        return
    # Child
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    # Grandchild: become the daemon
    for fd in range(3):
        with contextlib.suppress(OSError):
            os.close(fd)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.execvp(
        sys.executable,
        [sys.executable, "-m", "orchestra.hookd", str(state_db)],
    )


def ensure_daemon_and_send(
    *,
    sock_path: Path,
    pid_path: Path,
    lock_path: Path,
    state_db: Path,
    event: str,
    worker_id: str,
    payload: dict[str, Any],
) -> bool:
    """Try to send; if no daemon, lazy-spawn one under a file lock, then retry."""
    if send(sock_path, event=event, worker_id=worker_id, payload=payload):
        return True
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Re-check inside the lock — another client may have spawned it.
            if not sock_path.exists():
                _spawn_daemon(state_db)
                for _ in range(50):
                    if sock_path.exists():
                        break
                    time.sleep(0.05)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    return send(sock_path, event=event, worker_id=worker_id, payload=payload)
