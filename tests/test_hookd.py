"""Tests for orchestra.hookd — Unix-socket hook daemon."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orchestra import _hook_client, hookd, state


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test .orchestra/ dir with initialised schema + an 'w1' worker row.

    Also flips off the session-scoped FORCE_HOOK_FALLBACK env so this file
    actually exercises the daemon path.
    """
    monkeypatch.delenv("ORCHESTRA_FORCE_HOOK_FALLBACK", raising=False)
    d = tmp_path / "proj" / ".orchestra"
    d.mkdir(parents=True)
    conn = state.connect(d / "state.db")
    state.init_schema(conn)
    state.create_worker(
        conn, id="w1", task="t", model="sonnet",
        branch="orch/w1", pane_target="s:1",
    )
    conn.close()
    return d


def _wait_for_socket(sock: Path, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock.exists():
            return True
        time.sleep(0.05)
    return False


def _stop(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


class TestRoundTrip:
    def test_session_start_event_updates_worker_status(self, project_dir: Path) -> None:
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            sock = project_dir / "hook.sock"
            assert _wait_for_socket(sock), "daemon socket never appeared"
            ok = _hook_client.send(
                sock, event="SessionStart", worker_id="w1",
                payload={"session_id": "s1"},
            )
            assert ok is True
            for _ in range(60):
                conn = state.connect(project_dir / "state.db")
                w = state.get_worker(conn, "w1")
                conn.close()
                if w is not None and w.status == "working":
                    break
                time.sleep(0.05)
            else:
                pytest.fail("session_start event never landed in state.db")
            assert (project_dir / "hookd.pid").exists()
        finally:
            _stop(proc)

    def test_stop_event_increments_turns(self, project_dir: Path) -> None:
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            sock = project_dir / "hook.sock"
            assert _wait_for_socket(sock)
            assert _hook_client.send(
                sock, event="Stop", worker_id="w1", payload={},
            )
            for _ in range(60):
                conn = state.connect(project_dir / "state.db")
                w = state.get_worker(conn, "w1")
                conn.close()
                if w is not None and w.turns == 1:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("stop event never incremented turns")
        finally:
            _stop(proc)


class TestFallback:
    def test_send_returns_false_when_socket_missing(self, project_dir: Path) -> None:
        ok = _hook_client.send(
            project_dir / "nonexistent.sock",
            event="SessionStart", worker_id="w1", payload={},
            connect_timeout=0.2,
        )
        assert ok is False

    def test_send_returns_false_when_socket_present_but_dead(
        self, project_dir: Path
    ) -> None:
        # Create a socket file but don't bind anything to it — connect should fail.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock_path = project_dir / "hook.sock"
        s.bind(str(sock_path))
        s.close()  # leaves the file, but no listener
        try:
            ok = _hook_client.send(
                sock_path, event="SessionStart", worker_id="w1", payload={},
                connect_timeout=0.2,
            )
            assert ok is False
        finally:
            sock_path.unlink(missing_ok=True)


class TestIdleShutdown:
    def test_daemon_exits_after_idle_window(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORCHESTRA_HOOKD_IDLE_S", "1")
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            rc = proc.wait(timeout=6)
            assert rc == 0
            # Cleanup happens on graceful exit.
            assert not (project_dir / "hook.sock").exists()
            assert not (project_dir / "hookd.pid").exists()
        finally:
            if proc.poll() is None:
                _stop(proc)


class TestSigtermCleanup:
    def test_sigterm_unlinks_socket_and_pid(self, project_dir: Path) -> None:
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            assert _wait_for_socket(project_dir / "hook.sock")
            assert (project_dir / "hookd.pid").exists()
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=5)
            assert rc == 0
            assert not (project_dir / "hook.sock").exists()
            assert not (project_dir / "hookd.pid").exists()
        finally:
            if proc.poll() is None:
                _stop(proc)


class TestMalformedInput:
    def test_garbage_line_does_not_crash_daemon(self, project_dir: Path) -> None:
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            sock = project_dir / "hook.sock"
            assert _wait_for_socket(sock)
            # Send raw garbage (no JSON, no newline guarantee).
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(str(sock))
                s.sendall(b"this is not json\n")
            time.sleep(0.2)
            # Daemon still alive — a follow-up real event should still work.
            assert _hook_client.send(
                sock, event="Notification", worker_id="w1",
                payload={"message": "still here"},
            )
            assert proc.poll() is None
        finally:
            _stop(proc)

    def test_missing_required_fields_does_not_crash(self, project_dir: Path) -> None:
        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            sock = project_dir / "hook.sock"
            assert _wait_for_socket(sock)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(str(sock))
                s.sendall(json.dumps({"event": "Notification"}).encode() + b"\n")
            time.sleep(0.2)
            assert proc.poll() is None
        finally:
            _stop(proc)


class TestLazySpawnRace:
    def test_concurrent_clients_only_spawn_one_daemon(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Ensure the daemon will idle-exit quickly so the test can clean up
        # even if the os.kill path fails.
        monkeypatch.setenv("ORCHESTRA_HOOKD_IDLE_S", "3")
        sock = project_dir / "hook.sock"
        pid_path = project_dir / "hookd.pid"
        lock_path = project_dir / "hookd.lock"
        assert not sock.exists()
        assert not pid_path.exists()

        def fire() -> bool:
            return _hook_client.ensure_daemon_and_send(
                sock_path=sock,
                pid_path=pid_path,
                lock_path=lock_path,
                state_db=project_dir / "state.db",
                event="Notification",
                worker_id="w1",
                payload={"message": "hi"},
            )

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = [f.result() for f in [ex.submit(fire) for _ in range(4)]]
        assert all(results), f"some sends failed: {results}"
        assert pid_path.exists()
        pid_text = pid_path.read_text().strip()
        assert pid_text.isdigit()
        pid = int(pid_text)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        # Let the daemon clean up.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if not pid_path.exists() and not sock.exists():
                break
            time.sleep(0.05)


class TestDispatchFastPath:
    def test_dispatch_uses_daemon_when_available(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestra import hooks
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(project_dir / "state.db"))
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.delenv("ORCHESTRA_FORCE_HOOK_FALLBACK", raising=False)

        proc = hookd.spawn_daemon_for_test(project_dir)
        try:
            assert _wait_for_socket(project_dir / "hook.sock")
            rc = hooks.dispatch(
                "SessionStart", stdin_text=json.dumps({"session_id": "s2"})
            )
            assert rc == 0
            # Daemon path should land the event and tag the debug log.
            for _ in range(60):
                conn = state.connect(project_dir / "state.db")
                w = state.get_worker(conn, "w1")
                conn.close()
                if w is not None and w.status == "working":
                    break
                time.sleep(0.05)
            else:
                pytest.fail("dispatch did not reach state.db via daemon")
            log_lines = (project_dir / "hook-debug.log").read_text().splitlines()
            entries = [json.loads(line) for line in log_lines if line.strip()]
            assert any(e.get("_via") == "daemon" for e in entries)
        finally:
            _stop(proc)

    def test_dispatch_falls_back_when_daemon_disabled(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestra import hooks
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(project_dir / "state.db"))
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_FORCE_HOOK_FALLBACK", "1")

        rc = hooks.dispatch(
            "SessionStart", stdin_text=json.dumps({"session_id": "s3"})
        )
        assert rc == 0
        # In-process path should still update worker state.
        conn = state.connect(project_dir / "state.db")
        w = state.get_worker(conn, "w1")
        conn.close()
        assert w is not None
        assert w.status == "working"
        # No socket should have been opened.
        assert not (project_dir / "hook.sock").exists()
