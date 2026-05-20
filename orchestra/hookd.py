"""orchestra hook daemon. Listens on a Unix domain socket, dispatches
events through hooks._handle to the project's state.db.

Lifecycle:
- Started by _hook_client._spawn_daemon (double-fork) or by a test harness
- Idle-exits after ORCHESTRA_HOOKD_IDLE_S seconds (default 300) of no events
- SIGTERM → graceful close + cleanup of PID + socket
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from orchestra import hooks, state


def _idle_seconds() -> float:
    try:
        return float(os.environ.get("ORCHESTRA_HOOKD_IDLE_S", "300"))
    except ValueError:
        return 300.0


class _Server:
    def __init__(self, state_db: Path, idle_s: float) -> None:
        self.state_db = state_db
        self.idle_s = idle_s
        self.last_event_t = 0.0
        self.shutdown_event = asyncio.Event()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readuntil(b"\n")
            msg = json.loads(line.decode("utf-8"))
            event = msg["event"]
            worker_id = msg["worker_id"]
            payload = msg.get("payload", {})
            conn = state.connect(self.state_db)
            try:
                hooks._handle(event, payload, conn, worker_id)
            finally:
                conn.close()
            self.last_event_t = asyncio.get_running_loop().time()
        except Exception:  # noqa: BLE001 — never crash the daemon over one bad msg
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def idle_watch(self) -> None:
        loop = asyncio.get_running_loop()
        # Poll cadence: at most every 10s, at least once per idle window.
        tick = min(self.idle_s, 10.0)
        if tick <= 0:
            tick = 0.1
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=tick)
                return  # shutdown signalled
            except asyncio.TimeoutError:
                pass
            if loop.time() - self.last_event_t > self.idle_s:
                self.shutdown_event.set()
                return


async def main_async(state_db: Path) -> None:
    sock_path = state_db.parent / "hook.sock"
    pid_path = state_db.parent / "hookd.pid"
    if sock_path.exists():
        sock_path.unlink()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()) + "\n")

    srv = _Server(state_db, _idle_seconds())
    srv.last_event_t = asyncio.get_running_loop().time()
    server = await asyncio.start_unix_server(srv.handle, path=str(sock_path))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, srv.shutdown_event.set)

    idle_task = asyncio.create_task(srv.idle_watch())
    try:
        async with server:
            await srv.shutdown_event.wait()
    finally:
        idle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await idle_task
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            pid_path.unlink()


def spawn_daemon_for_test(orch_dir: Path) -> subprocess.Popen[bytes]:
    """Test helper: spawn the daemon as a subprocess (no double-fork) so tests
    can wait/terminate it cleanly. Inherits the caller's environment so
    ORCHESTRA_HOOKD_IDLE_S monkeypatches propagate.
    """
    state_db = orch_dir / "state.db"
    return subprocess.Popen(
        [sys.executable, "-m", "orchestra.hookd", str(state_db)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m orchestra.hookd <state_db>", file=sys.stderr)
        return 2
    asyncio.run(main_async(Path(argv[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
