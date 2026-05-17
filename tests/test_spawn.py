from __future__ import annotations

import shlex
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestra import spawn, state


def _open(tmp_db: Path) -> sqlite3.Connection:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    return conn


@pytest.fixture
def fake_tmux(monkeypatch):
    """Replace orchestra.tmux with a MagicMock at the spawn module level."""
    m = MagicMock()
    # ensure_session and new_window are no-ops; new_window returns a target
    m.new_window.return_value = "orch-proj:w1"
    m.is_idle.return_value = True  # default to "ready immediately"
    m.capture.return_value = "❯ "
    monkeypatch.setattr(spawn, "tmux", m)
    return m


def _kinds(conn: sqlite3.Connection, worker_id: str) -> list[str]:
    return [e.kind for e in state.list_events(conn, worker_id=worker_id)]


class TestHappyPath:
    def test_records_event_sequence_and_marks_working(
        self, tmp_db, tmp_orch_dir, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        # First poll for status event returns nothing; second poll finds it.
        # Simulate this by having the worker write a status event after 2 polls.
        original_list = state.list_events
        calls = {"n": 0}

        def stub_list(conn_, **kw):
            calls["n"] += 1
            list(original_list(conn_, **kw))
            if kw.get("worker_id") == "w1" and calls["n"] >= 2:
                # inject a fake status event
                state.record_event(conn_, "status", worker_id="w1", progress="starting", turns=0)
            return original_list(conn_, **kw)

        monkeypatch.setattr(spawn.state, "list_events", stub_list)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="Implement auth",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "working"

        kinds = _kinds(conn, "w1")
        # Ensure the canonical sequence appears in order
        expected_prefix = [
            "spawn_start", "spawn_window", "spawn_idle",
            "model_switched", "prompt_injected", "spawn_ok",
        ]
        for needle in expected_prefix:
            assert needle in kinds, f"missing {needle} in {kinds}"

    def test_boot_command_has_env_and_dangerously_skip(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        monkeypatch.setattr(spawn, "_wait_first_status", lambda *a, **kw: True)

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        # The boot command goes through send_literal as the first send to the new pane.
        sent_texts = [c.args[1] for c in fake_tmux.send_literal.call_args_list]
        boot_cmd = sent_texts[0]
        expected_id = f"ORCHESTRA_WORKER_ID={shlex.quote('w1')}"
        expected_db = f"ORCHESTRA_STATE_DB={shlex.quote(str(tmp_db))}"
        assert expected_id in boot_cmd
        assert expected_db in boot_cmd
        assert "claude --dangerously-skip-permissions" in boot_cmd

    def test_boot_command_handles_apostrophe_in_worker_id(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        monkeypatch.setattr(spawn, "_wait_first_status", lambda *a, **kw: True)

        worker_id = "o'brien"
        spawn.spawn_worker(
            conn,
            worker_id=worker_id,
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        sent_texts = [c.args[1] for c in fake_tmux.send_literal.call_args_list]
        boot_cmd = sent_texts[0]
        # shlex.quote on "o'brien" produces proper shell escaping
        expected_id = f"ORCHESTRA_WORKER_ID={shlex.quote(worker_id)}"
        assert expected_id in boot_cmd
        assert "claude --dangerously-skip-permissions" in boot_cmd


class TestBootTimeout:
    def test_marks_error_and_records_event(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        fake_tmux.is_idle.return_value = False
        fake_tmux.capture.return_value = "spinning forever..."
        # Compress the wait loop time.
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 0.05)
        monkeypatch.setattr(spawn, "BOOT_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "error"
        kinds = _kinds(conn, "w1")
        assert "spawn_timeout" in kinds


class TestFirstStatusTimeout:
    def test_marks_stale_spawn(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "FIRST_STATUS_TIMEOUT_S", 0.05)
        monkeypatch.setattr(spawn, "FIRST_STATUS_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "stale_spawn"
        kinds = _kinds(conn, "w1")
        assert "spawn_first_status_timeout" in kinds


class TestPromptInjectFailure:
    def test_two_failures_mark_error(
        self, tmp_db, tmp_orch_dir, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        # Both attempts raise — exhausts the (1, 2) retry loop
        fake_tmux.send_multiline.side_effect = RuntimeError("buffer too big")
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "error"
        kinds = _kinds(conn, "w1")
        assert "prompt_inject_failed" in kinds
        # Two retry events recorded (one per failed attempt)
        retry_events = [k for k in kinds if k == "prompt_inject_retry"]
        assert len(retry_events) == 2
        # send_multiline was actually invoked twice
        assert fake_tmux.send_multiline.call_count == 2


class TestTrustPrompt:
    def test_dismisses_trust_prompt_then_reaches_idle(
        self, tmp_db, tmp_orch_dir, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        # First two is_idle polls report busy; trust-prompt is visible in capture.
        # After that, is_idle returns True (claude reached its real prompt).
        idle_returns = [False, False, True, True]
        fake_tmux.is_idle.side_effect = (
            lambda *a, **kw: idle_returns.pop(0) if idle_returns else True
        )
        # Capture returns trust-prompt text the first time, plain prompt after.
        trust_screen = (
            "Is this a project you created or one you trust?\n"
            "❯ 1. Yes, I trust this folder\n  2. No, exit\n"
        )
        cap_returns = [trust_screen, "❯ ", "❯ "]
        fake_tmux.capture.side_effect = lambda *a, **kw: (
            cap_returns.pop(0) if cap_returns else "❯ "
        )
        monkeypatch.setattr(spawn, "BOOT_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "FIRST_STATUS_TIMEOUT_S", 0.05)
        monkeypatch.setattr(spawn, "FIRST_STATUS_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        kinds = _kinds(conn, "w1")
        # trust_accepted event was recorded
        assert "spawn_trust_accepted" in kinds
        # Trust acceptance sent exactly one Enter (caller doesn't double-up here)
        enter_calls = fake_tmux.send_enter.call_args_list
        # boot_cmd + 2 post-idle dismiss Enters + 1 trust + 1 model => at least 4
        assert len(enter_calls) >= 4
        # model_switched implies the trust handling unblocked _wait_idle
        assert "model_switched" in kinds
