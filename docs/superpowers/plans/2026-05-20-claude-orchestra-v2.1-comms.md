# claude-orchestra v2.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Three independent communication primitives — `spawn-batch`, hook daemon, `merge --batch` — that cut PM serial-coordination time without changing semantics. v2.0 kanban e2e remains the cross-regression acceptance gate.

**Architecture:** Each primitive is additive and isolated (independent files, no shared abstractions). The hook daemon is the biggest piece (~200 LOC + tests); the other two are <100 LOC each.

**Tech Stack:** Python 3.10+ stdlib only — `concurrent.futures.ThreadPoolExecutor` for spawn-batch, `asyncio.start_unix_server` for the daemon, no new dependencies.

**Spec:** [`docs/superpowers/specs/2026-05-20-claude-orchestra-v2.1-comms-design.md`](../specs/2026-05-20-claude-orchestra-v2.1-comms-design.md)

## File Map

**New files:**
- `orchestra/spawn_batch.py` — JSONL parser + threadpool launcher (~60 LOC)
- `orchestra/hookd.py` — daemon: asyncio UDS server + idle shutdown (~150 LOC)
- `orchestra/_hook_client.py` — thin client: connect + send + close (~50 LOC)
- `tests/test_spawn_batch.py` — unit tests for #24
- `tests/test_hookd.py` — round-trip + fallback + respawn tests for #25
- `tests/test_merge_batch.py` — batched merge tests for #26 (or extend `tests/test_cli.py`)

**Modified files:**
- `orchestra/cli.py` — register `spawn-batch` and `worker shutdown-hookd`; extend `merge` with `--batch`
- `orchestra/hooks.py` — fast-path via `_hook_client`, fallback to existing `_handle`
- `orchestra/roles/pm.md` — one-line guidance on the new commands
- `CHANGELOG.md` — v2.1 section

---

## Task 1: `orchestra spawn-batch` — parallel worker spawn

**Goal:** New CLI: `orchestra spawn-batch <spec.jsonl>` parses N worker specs and spawns them concurrently via `ThreadPoolExecutor(max_workers=N)`. Exits 0 if all spawned, 2 if any failed (with per-worker status to stderr).

**Files:**
- Create: `orchestra/spawn_batch.py`
- Modify: `orchestra/cli.py`
- Modify: `orchestra/roles/pm.md`
- Create: `tests/test_spawn_batch.py`

**Acceptance Criteria:**
- [ ] `orchestra spawn-batch /path/to/spec.jsonl` reads JSONL where each line is `{id, model, role?, brief?, worktree?}`
- [ ] Each spec spawns via `spawn.spawn_worker` with its OWN short-lived sqlite3 connection
- [ ] Concurrency is real: 2 spawns whose `spawn_worker` calls are stubbed to `time.sleep(2)` complete in <3.5s, not >4s
- [ ] If any spawn fails, the others still complete; exit code 2; per-worker status printed to stderr
- [ ] Empty input file → exit 2 with "no specs in <path>" message

**Verify:** `.venv/bin/pytest tests/test_spawn_batch.py -v` → all pass

**Steps:**

1. Write failing tests in `tests/test_spawn_batch.py`:

```python
"""Tests for orchestra.spawn_batch — parallel worker spawn."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestra import spawn_batch


class TestParse:
    def test_parses_jsonl(self, tmp_path: Path) -> None:
        p = tmp_path / "specs.jsonl"
        p.write_text(
            '{"id":"a","model":"sonnet"}\n'
            '{"id":"b","model":"sonnet","worktree":"b"}\n'
        )
        specs = spawn_batch.parse_jsonl(p)
        assert len(specs) == 2
        assert specs[0]["id"] == "a"
        assert specs[1]["worktree"] == "b"

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        with pytest.raises(ValueError, match="no specs"):
            spawn_batch.parse_jsonl(p)

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "specs.jsonl"
        p.write_text('{"id":"a","model":"sonnet"}\n\n\n')
        specs = spawn_batch.parse_jsonl(p)
        assert len(specs) == 1


class TestConcurrency:
    def test_runs_in_parallel(self, tmp_path: Path) -> None:
        """Two specs whose spawn takes 2s each should complete in <3.5s
        when run via spawn_batch (truly concurrent), not >4s (serial)."""
        calls = []

        def fake_spawn_worker(conn, **kw):
            calls.append(kw["worker_id"])
            time.sleep(2.0)

        specs = [
            {"id": "a", "model": "sonnet"},
            {"id": "b", "model": "sonnet"},
        ]
        with patch("orchestra.spawn_batch.spawn.spawn_worker", side_effect=fake_spawn_worker):
            start = time.monotonic()
            results = spawn_batch.run(
                specs=specs,
                project_root=str(tmp_path),
                state_db=tmp_path / "state.db",
                session_name="orch-test",
            )
        elapsed = time.monotonic() - start
        assert elapsed < 3.5, f"serial pattern detected: {elapsed:.2f}s"
        assert sorted(calls) == ["a", "b"]
        assert all(r["status"] == "ok" for r in results)


class TestFailureModes:
    def test_one_failure_others_complete(self, tmp_path: Path) -> None:
        def fake_spawn(conn, **kw):
            if kw["worker_id"] == "bad":
                raise RuntimeError("boom")

        specs = [
            {"id": "good", "model": "sonnet"},
            {"id": "bad", "model": "sonnet"},
            {"id": "alsogood", "model": "sonnet"},
        ]
        with patch("orchestra.spawn_batch.spawn.spawn_worker", side_effect=fake_spawn):
            results = spawn_batch.run(
                specs=specs,
                project_root=str(tmp_path),
                state_db=tmp_path / "state.db",
                session_name="orch-test",
            )
        by_id = {r["id"]: r for r in results}
        assert by_id["good"]["status"] == "ok"
        assert by_id["bad"]["status"] == "error"
        assert "boom" in by_id["bad"]["error"]
        assert by_id["alsogood"]["status"] == "ok"
```

2. Run tests; confirm they fail with ModuleNotFoundError.

3. Implement `orchestra/spawn_batch.py`:

```python
"""orchestra spawn-batch — parallel worker spawn.

Reads a JSONL file where each line is a worker spec dict (id, model, role?,
brief?, worktree?) and dispatches them through ``spawn.spawn_worker`` in a
ThreadPoolExecutor. Each worker gets its own short-lived sqlite3 connection
(post-v1.2 #6 the spawn flow no longer pins a connection across its blocking
waits), so true concurrency is safe.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from orchestra import spawn, state


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a worker-spec JSONL file. Raises ValueError on empty input."""
    specs: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        specs.append(json.loads(line))
    if not specs:
        raise ValueError(f"no specs in {path}")
    return specs


def _spawn_one(
    spec: dict[str, Any],
    *,
    project_root: str,
    state_db: Path,
    session_name: str,
) -> dict[str, Any]:
    """Spawn a single worker. Owns its own conn for the duration."""
    conn = state.connect(state_db)
    try:
        spawn.spawn_worker(
            conn,
            worker_id=spec["id"],
            model=spec["model"],
            task=spec.get("task", ""),
            project_root=project_root,
            state_db=state_db,
            ctx_files=spec.get("ctx_files", []),
            session_name=session_name,
            role=spec.get("role"),
            brief=spec.get("brief"),
            worktree_name=spec.get("worktree"),
        )
        return {"id": spec["id"], "status": "ok"}
    except Exception as e:  # noqa: BLE001 — one failure shouldn't kill the batch
        return {"id": spec["id"], "status": "error", "error": repr(e)}
    finally:
        conn.close()


def run(
    *,
    specs: list[dict[str, Any]],
    project_root: str,
    state_db: Path,
    session_name: str,
) -> list[dict[str, Any]]:
    """Spawn all specs concurrently, return per-worker status dicts."""
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as ex:
        futures = [
            ex.submit(
                _spawn_one, s,
                project_root=project_root,
                state_db=state_db,
                session_name=session_name,
            )
            for s in specs
        ]
        for f in as_completed(futures):
            results.append(f.result())
    return results
```

4. Run tests; confirm 6/6 PASS.

5. Wire CLI in `orchestra/cli.py`:

Add a new command (style-match `send_command` / `merge` etc.):

```python
@app.command("spawn-batch")
def spawn_batch_command(
    spec_file: Path = typer.Argument(..., metavar="SPEC_JSONL"),
) -> None:
    """Spawn multiple workers concurrently from a JSONL spec file."""
    from orchestra import spawn_batch as sb
    project_root = Path.cwd()
    state_db = project_root / ".orchestra" / "state.db"
    if not state_db.exists():
        typer.echo("error: run `orchestra init` first", err=True)
        raise typer.Exit(2)
    try:
        specs = sb.parse_jsonl(spec_file)
    except (ValueError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from None
    session_name = _session_name_for(project_root)
    results = sb.run(
        specs=specs,
        project_root=str(project_root),
        state_db=state_db,
        session_name=session_name,
    )
    bad = [r for r in results if r["status"] != "ok"]
    for r in results:
        typer.echo(f"  {r['id']}: {r['status']}" + (f" — {r.get('error','')}" if r['status']!="ok" else ""))
    if bad:
        raise typer.Exit(2)
```

6. Add CLI integration test (light) to `tests/test_spawn_batch.py` — runs `runner.invoke(app, ["spawn-batch", str(spec_path)])` with stubbed spawn, confirms exit 0 and per-worker line in output.

7. Update `orchestra/roles/pm.md` — add this line in the TOOLS YOU CAN USE block:

```
- orchestra spawn-batch <spec.jsonl>  # parallel spawn for any wave of >=2 engineers (preferred over sequential `orchestra spawn`)
```

8. Lint + type-check the touched files; full suite green.

9. Commit with message:

```
feat(spawn): add orchestra spawn-batch for parallel worker spawn

PM can now spawn N engineers in one call via a JSONL spec file. Each
spawn runs in its own thread with its own short-lived sqlite3 connection
(post-v1.2 #6 the spawn flow no longer holds a long-lived conn during
its blocking waits, so true concurrency is safe). Cuts ~18s off every
3-engineer wave in the kanban-shape pipeline. Closes #24.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 2: Hook daemon — Unix domain socket

**Goal:** Replace per-event Python subprocess (~65ms) with a lazily-spawned per-project daemon listening on `<project>/.orchestra/hook.sock`. Drops per-event latency to ~8ms while keeping all current hook semantics. Falls back to the existing in-process path when the daemon is unreachable.

**Files:**
- Create: `orchestra/hookd.py` — daemon
- Create: `orchestra/_hook_client.py` — thin client
- Modify: `orchestra/hooks.py` — fast-path via client, fallback unchanged
- Modify: `orchestra/cli.py` — `worker shutdown-hookd` cleanup command
- Create: `tests/test_hookd.py`

**Acceptance Criteria:**
- [ ] Daemon listens on `<state_db_dir>/hook.sock`, dispatches incoming JSON-lines through `hooks._handle`
- [ ] PID file at `<state_db_dir>/hookd.pid` cleaned up on graceful shutdown and on idle exit
- [ ] Client connects, sends one JSON line `{"event": "...", "worker_id": "...", "payload": {...}, "ts": "..."}`, closes
- [ ] First hook event with no socket lazy-spawns the daemon (double-fork, retry connect once)
- [ ] If daemon unreachable after retry: client falls back to v2.0 in-process path; current `tests/test_hooks.py` cases pass on this path
- [ ] Idle shutdown: 300s of no events → daemon exits, removes PID + socket. Configurable via `ORCHESTRA_HOOKD_IDLE_S` env (default 300).
- [ ] `orchestra worker shutdown-hookd` SIGTERMs the PID file's process and unlinks socket+PID
- [ ] Lazy-spawn race: a `<state_db_dir>/hookd.lock` flock guards the double-fork so concurrent clients don't spawn duplicates

**Verify:**
- `.venv/bin/pytest tests/test_hookd.py tests/test_hooks.py -v` → all pass (new tests + existing fallback-path tests)
- `.venv/bin/pytest -q` → no regressions

**Steps:**

1. Write `tests/test_hookd.py` first with TDD coverage of the round-trip, lazy-spawn race (via lock), and fallback-on-missing-socket cases. Sample of the key tests (full test file fleshed out in implementation):

```python
"""Tests for orchestra.hookd — Unix-socket hook daemon."""
from __future__ import annotations

import json
import os
import signal
import socket
import time
from pathlib import Path

import pytest

from orchestra import hookd, state


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    d = tmp_path / "proj" / ".orchestra"
    d.mkdir(parents=True)
    db = d / "state.db"
    state.connect(db).close()  # initialise schema
    return d


class TestRoundTrip:
    def test_client_send_event_lands_in_state_db(self, project_dir: Path) -> None:
        # Start daemon synchronously (test mode — no double-fork)
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            # Seed a worker row so the SessionStart handler has something to update
            conn = state.connect(project_dir / "state.db")
            state.create_worker(
                conn, id="w1", task="t", model="sonnet",
                branch="orch/w1", pane_target="s:1",
            )
            conn.close()
            # Wait briefly for socket
            sock_path = project_dir / "hook.sock"
            for _ in range(50):
                if sock_path.exists():
                    break
                time.sleep(0.05)
            # Client side
            from orchestra import _hook_client
            ok = _hook_client.send(
                sock_path,
                event="SessionStart",
                worker_id="w1",
                payload={"session_id": "s1"},
            )
            assert ok
            # Wait for event to land
            for _ in range(50):
                conn = state.connect(project_dir / "state.db")
                w = state.get_worker(conn, "w1")
                conn.close()
                if w and w.status == "working":
                    break
                time.sleep(0.05)
            else:
                pytest.fail("event never landed in state.db")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestFallback:
    def test_client_send_returns_false_when_no_socket(self, project_dir: Path) -> None:
        from orchestra import _hook_client
        ok = _hook_client.send(
            project_dir / "nonexistent.sock",
            event="SessionStart",
            worker_id="w1",
            payload={},
            connect_timeout=0.2,
        )
        assert not ok


class TestIdleShutdown:
    def test_daemon_exits_after_idle_window(self, project_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRA_HOOKD_IDLE_S", "1")
        proc = hookd.spawn_daemon_for_test(project_dir)
        # Daemon should exit within ~2s without traffic
        proc.wait(timeout=4)
        assert proc.returncode == 0


class TestLazySpawnRace:
    def test_two_concurrent_clients_only_spawn_one_daemon(
        self, project_dir: Path, monkeypatch
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from orchestra import _hook_client
        # ensure no daemon yet
        sock = project_dir / "hook.sock"
        pid = project_dir / "hookd.pid"
        assert not sock.exists()
        assert not pid.exists()

        def fire():
            return _hook_client.ensure_daemon_and_send(
                sock_path=sock,
                pid_path=pid,
                lock_path=project_dir / "hookd.lock",
                state_db=project_dir / "state.db",
                event="Notification",
                worker_id="w1",
                payload={"message": "hi"},
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            r1, r2 = ex.submit(fire).result(), ex.submit(fire).result()
        # Both should succeed
        assert r1 and r2
        # Only one PID file
        assert pid.exists()
        # Cleanup
        os.kill(int(pid.read_text().strip()), signal.SIGTERM)
        time.sleep(0.3)
```

(Full file flesh-out: ~12 tests covering round-trip per event type, fallback, idle shutdown, lazy-spawn race, graceful SIGTERM, malformed input.)

2. Run; confirm failures (no `hookd`, no `_hook_client`).

3. Implement `orchestra/_hook_client.py`:

```python
"""Tiny client for the orchestra hook daemon. ~5-10 ms per send.

Used by the `orchestra worker hook EVENT` fast path. Public API:
- send(sock_path, event, worker_id, payload, *, connect_timeout=0.5) -> bool
- ensure_daemon_and_send(...) -> bool  (lazy-spawn + retry once)
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
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
    except (OSError, socket.timeout):
        return False


def _spawn_daemon(state_db: Path) -> None:
    """Double-fork so the daemon is reparented to init."""
    # First fork
    pid = os.fork()
    if pid > 0:
        return  # parent returns immediately
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)  # intermediate exits
    # Grandchild: become the daemon
    # Close all fds (best-effort)
    for fd in range(3):
        try:
            os.close(fd)
        except OSError:
            pass
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    # Exec the daemon module
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
    # Spawn under lock so two concurrent clients don't both spawn daemons
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Re-check inside lock
            if not sock_path.exists():
                _spawn_daemon(state_db)
                # Wait briefly for socket to appear
                for _ in range(50):
                    if sock_path.exists():
                        break
                    time.sleep(0.05)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    return send(sock_path, event=event, worker_id=worker_id, payload=payload)
```

4. Implement `orchestra/hookd.py`:

```python
"""orchestra hook daemon. Listens on a Unix domain socket, dispatches
events through hooks._handle to the project's state.db.

Lifecycle:
- Started by _hook_client._spawn_daemon (double-fork) or by a test harness
- Idle-exits after ORCHESTRA_HOOKD_IDLE_S seconds (default 300) of no events
- SIGTERM → graceful close + cleanup of PID + socket
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestra import hooks, state

IDLE_S = float(os.environ.get("ORCHESTRA_HOOKD_IDLE_S", "300"))


class _Server:
    def __init__(self, state_db: Path) -> None:
        self.state_db = state_db
        self.last_event_t = 0.0
        self.shutdown_event = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
            writer.close()

    async def idle_watch(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.shutdown_event.is_set():
            await asyncio.sleep(min(IDLE_S, 10.0))
            if loop.time() - self.last_event_t > IDLE_S:
                self.shutdown_event.set()


async def main_async(state_db: Path) -> None:
    sock_path = state_db.parent / "hook.sock"
    pid_path = state_db.parent / "hookd.pid"
    if sock_path.exists():
        sock_path.unlink()
    pid_path.write_text(str(os.getpid()) + "\n")

    srv = _Server(state_db)
    srv.last_event_t = asyncio.get_running_loop().time()
    server = await asyncio.start_unix_server(srv.handle, path=str(sock_path))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, srv.shutdown_event.set)

    idle_task = asyncio.create_task(srv.idle_watch())
    try:
        async with server:
            await srv.shutdown_event.wait()
    finally:
        idle_task.cancel()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass


def spawn_daemon_for_test(orch_dir: Path) -> subprocess.Popen:
    """Test helper: spawn the daemon as a subprocess (no double-fork) so tests
    can wait/terminate it cleanly."""
    state_db = orch_dir / "state.db"
    return subprocess.Popen(
        [sys.executable, "-m", "orchestra.hookd", str(state_db)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
```

5. Wire fast path in `orchestra/hooks.py`. Replace the body of `dispatch` (keep the function signature identical so call sites don't change):

```python
def dispatch(event: str, *, stdin_text: str) -> int:
    """Typed dispatch — always returns 0."""
    db = _state_db()
    wid = _worker_id()
    if db is None or wid is None:
        return 0
    try:
        payload = json.loads(stdin_text) if stdin_text else {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
    except json.JSONDecodeError:
        payload = {"_parse_error": True, "_raw": stdin_text}

    # v2.1 fast path: try the daemon first.
    if os.environ.get("ORCHESTRA_FORCE_HOOK_FALLBACK") != "1":
        try:
            from orchestra import _hook_client
            orch_dir = db.parent
            if _hook_client.ensure_daemon_and_send(
                sock_path=orch_dir / "hook.sock",
                pid_path=orch_dir / "hookd.pid",
                lock_path=orch_dir / "hookd.lock",
                state_db=db,
                event=event,
                worker_id=wid,
                payload=payload,
            ):
                # Still append the diagnostic line for now (used by debug log).
                _append_log(
                    "hook-debug.log",
                    json.dumps({"ts": _now(), "event": event, "worker_id": wid, "payload": payload, "_via": "daemon"}),
                )
                return 0
        except Exception:  # noqa: BLE001 — fallback is below
            pass

    # Fallback: existing in-process path (unchanged from v2.0).
    conn = None
    try:
        conn = state.connect(db)
        _handle(event, payload, conn, wid)
        _append_log(
            "hook-debug.log",
            json.dumps({"ts": _now(), "event": event, "worker_id": wid, "payload": payload, "_via": "inproc"}),
        )
    except Exception:
        tb = traceback.format_exc()
        _append_log("hook-errors.log",
                    json.dumps({"ts": _now(), "event": event, "worker_id": wid, "traceback": tb}))
        try:
            if conn is None:
                conn = state.connect(db)
            state.record_event(conn, "hook_error", worker_id=wid, event=event, traceback=tb[-2000:])
        except Exception:
            pass
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
    return 0
```

`import os` already in the file. Existing tests still pass because they set `ORCHESTRA_FORCE_HOOK_FALLBACK=1` (add via monkeypatch in conftest if needed — see step 6).

6. Existing `tests/test_hooks.py` must keep passing. Add a session-scoped fixture in `tests/conftest.py` (or per-test monkeypatch) that sets `ORCHESTRA_FORCE_HOOK_FALLBACK=1` so those tests exercise the in-process path exclusively. The new `tests/test_hookd.py` is what exercises the daemon path.

7. Add `worker shutdown-hookd` to `orchestra/cli.py`:

```python
@worker_app.command("shutdown-hookd")
def worker_shutdown_hookd() -> None:
    """Stop the project's hook daemon (if running) and clean up PID + socket."""
    project_root = Path.cwd()
    orch = project_root / ".orchestra"
    pid_path = orch / "hookd.pid"
    sock_path = orch / "hook.sock"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
        except (ProcessLookupError, ValueError, OSError):
            pass
    for p in (pid_path, sock_path):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
    typer.echo("hookd stopped")
```

8. Run all hook + new daemon tests + the full repo suite:
- `.venv/bin/pytest tests/test_hookd.py tests/test_hooks.py -v`
- `.venv/bin/pytest -q`

9. Lint + type-check.

10. Commit:

```
feat(hooks): daemon mode — Unix-socket hook server with lazy spawn

Adds orchestra/hookd.py (asyncio UDS server) and orchestra/_hook_client.py
(tiny client). The dispatch() entry point now tries the daemon first
(~5-10ms per event) and falls back to the v2.0 in-process path if the
daemon is unreachable, preserving full backward compatibility.

Daemon is lazily spawned by the first hook event that finds no socket,
guarded by an flock so concurrent clients don't race. Idle-shutdown after
ORCHESTRA_HOOKD_IDLE_S seconds (default 300) of no events. Per-project
socket at <project>/.orchestra/hook.sock; PID file at hookd.pid.

`orchestra worker shutdown-hookd` provides explicit teardown.

Set ORCHESTRA_FORCE_HOOK_FALLBACK=1 to disable the daemon path (used by
the existing hooks tests + the e2e fallback regression).

Closes #25.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 3: `orchestra merge --batch <id1> <id2> ...`

**Goal:** One CLI invocation merges a list of orch branches sequentially in-process, records the events, and reports the per-merge result. Aborts on first conflict. Saves the PM the Claude-API round-trip cost of three back-to-back single-arg merges.

**Files:**
- Modify: `orchestra/cli.py` — extend the `merge` command
- Modify: `orchestra/roles/pm.md` — one-line guidance
- Create: `tests/test_merge_batch.py` (or extend `tests/test_cli.py` if simpler)

**Acceptance Criteria:**
- [ ] `orchestra merge --batch backend web cli` merges all three in order
- [ ] Each merge records `merge_attempted` + (`merge_ok` | `merge_conflict`)
- [ ] On first conflict, subsequent merges are SKIPPED (no merge_attempted events for them); exit code 2
- [ ] Output is JSON to stdout: `[{"id":"backend","status":"ok"},{"id":"web","status":"conflict","summary":"..."},...]`
- [ ] Single-arg form `orchestra merge backend` still works (no behaviour change)

**Verify:** `.venv/bin/pytest tests/test_merge_batch.py -v` + full suite green.

**Steps:**

1. Find the existing `merge` command in `cli.py` (search for `@app.command("merge")`).

2. Add `--batch` flag — typer's variadic args + flag work fine. Sketch:

```python
@app.command("merge")
def merge_command(
    worker_id: str | None = typer.Argument(None, metavar="ID"),
    batch: list[str] | None = typer.Option(None, "--batch", "-b", help="Merge multiple branches in order"),
) -> None:
    """Merge a worker branch into main. Use --batch <id1> <id2> ... for a wave of merges."""
    ids = batch if batch else ([worker_id] if worker_id else [])
    if not ids:
        typer.echo("error: provide an ID or --batch <id1> <id2> ...", err=True)
        raise typer.Exit(2)
    results = []
    aborted = False
    with _open_db() as conn:
        for wid in ids:
            if aborted:
                results.append({"id": wid, "status": "skipped"})
                continue
            state.record_event(conn, "merge_attempted", worker_id=wid)
            rc = subprocess.run(
                ["git", "merge", "--no-edit", f"orch/{wid}"],
                capture_output=True, text=True,
            )
            if rc.returncode == 0:
                state.record_event(conn, "merge_ok", worker_id=wid)
                results.append({"id": wid, "status": "ok"})
            else:
                state.record_event(conn, "merge_conflict", worker_id=wid,
                                   summary=(rc.stdout + rc.stderr)[:500])
                results.append({"id": wid, "status": "conflict",
                                "summary": (rc.stdout + rc.stderr)[:500]})
                # Abort any future merges in the batch.
                subprocess.run(["git", "merge", "--abort"], capture_output=True)
                aborted = True
    typer.echo(json.dumps(results, indent=2))
    if any(r["status"] != "ok" for r in results):
        raise typer.Exit(2)
```

Confirm typer accepts the `--batch` repeated flag pattern. If not, switch to `--from-file <ids.txt>`.

3. Write tests for both happy path (3 clean merges) and conflict mid-batch (3rd merge aborted). Use `git init` + branch setup in a `tmp_path`, similar to existing merge tests.

4. Update `orchestra/roles/pm.md` — add this to the TOOLS YOU CAN USE block:

```
- orchestra merge --batch <id1> <id2> ...   # one call for a wave of expected-clean merges
```

5. Run tests; commit:

```
feat(cli): orchestra merge --batch for back-to-back merges

PM can now merge N branches in one call instead of N separate
invocations. Each merge still runs sequentially in-process (same working
tree, no parallel git), but the PM no longer pays an inter-merge Claude
turn. Aborts on first conflict, returns per-merge JSON status. Closes #26.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 4: v2.0 kanban e2e cross-regression run  (USER GATE)

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it. Close only after the kanban e2e re-runs against the v2.1 changes and the verifier produces `OK`.

**Goal:** Confirm v2.0 kanban e2e still passes after the three primitives land. Measure the wall-clock delta against v2.0's baseline of 9:05.

**Files:** None modified. Updates only the CHANGELOG with measured timing.

**Acceptance Criteria:**
- [ ] `./scripts/e2e-build-kanban.sh` exits 0 and verifier prints `OK`
- [ ] Hook daemon path is exercised — sample a few `hook-debug.log` lines and confirm `"_via": "daemon"` appears
- [ ] If the PM happened to use `orchestra spawn-batch` (advisory in role template), confirm parallel spawn was visible in the events log (3 `spawn_start` events within ~1s of each other)
- [ ] If the PM happened to use `orchestra merge --batch`, confirm in the cli logs
- [ ] Wall-clock comparison written to CHANGELOG's v2.1 section (e.g. "kanban e2e v2.0 baseline 9:05 → v2.1 X:XX")

**Verify:** Re-run the e2e and capture the resulting state.db timings.

**Steps:**

1. Confirm clean repo state.

2. Run: `./scripts/e2e-build-kanban.sh > /tmp/kanban-v2.1.log 2>&1` (foreground or background — either works).

3. After completion, examine `/tmp/kanban-e2e/.orchestra/state.db`:
   - Wall-clock: `MIN(ts)` to `MAX(ts)` on events table
   - Daemon usage: `tail /tmp/kanban-e2e/.orchestra/hook-debug.log | grep '"_via"' | head -3`
   - Concurrency: `SELECT ts FROM events WHERE kind='spawn_start' ORDER BY id`

4. If verifier passed, write the measured timing into the v2.1 CHANGELOG entry, commit.

5. If verifier failed: diagnose via state.db + hook-debug.log. Likely failure modes:
   - Daemon path broken → set `ORCHESTRA_FORCE_HOOK_FALLBACK=1` to re-run; if that passes, daemon is the bug.
   - spawn-batch race → look for missing `spawn_window` events
   - merge --batch ordering → check `merge_attempted` event sequence

```json:metadata
{"files": ["CHANGELOG.md"], "verifyCommand": "./scripts/e2e-build-kanban.sh", "acceptanceCriteria": ["script exit 0", "verifier prints OK", "daemon path exercised (\"_via\":\"daemon\" lines in hook-debug.log)", "wall-clock delta recorded in CHANGELOG"], "userGate": true, "tags": ["user-gate"], "requiresUserSpecification": false, "gateScope": "task", "failurePolicy": "halt", "requireEvidenceTokens": [["e2e-passed", "verifier-OK"], ["daemon-exercised", "_via:daemon"]]}
```

---

## Self-Review Notes

Spec cross-check:
- §1 spawn-batch: Task 1 covers parser, threadpool launcher, CLI command, PM template guidance, tests.
- §2 hook daemon: Task 2 covers daemon, client, fast-path wire-in, fallback preservation, shutdown command, race lock, idle shutdown, tests.
- §3 merge --batch: Task 3 covers CLI extension, event recording, abort-on-conflict, JSON output, tests.
- §Test strategy: Task 4 captures the v2.0 kanban e2e cross-regression as the user-gate task.

No placeholders. Each task has full code blocks or exact command + expected output. Types and function signatures consistent across tasks (`spawn_batch.run`, `_hook_client.send`, `_hook_client.ensure_daemon_and_send`, `hookd.spawn_daemon_for_test`).
