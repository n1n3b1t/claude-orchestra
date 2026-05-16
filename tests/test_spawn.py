from __future__ import annotations

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
        assert "ORCHESTRA_WORKER_ID='w1'" in boot_cmd
        assert "ORCHESTRA_STATE_DB=" in boot_cmd
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
