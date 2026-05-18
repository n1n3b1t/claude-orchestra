"""Tests for orchestra.hooks (spike layer: log-only + typed dispatch)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from orchestra import hooks, state


class TestSpikeLogging:
    def test_logs_raw_stdin_to_debug_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        payload = {"event": "SessionStart", "session_id": "abc"}
        rc = hooks.run_spike("SessionStart", stdin_text=json.dumps(payload))
        assert rc == 0
        log = db.parent / "hook-debug.log"
        assert log.exists()
        line = json.loads(log.read_text().strip())
        assert line["event"] == "SessionStart"
        assert line["payload"] == payload
        assert "ts" in line
        assert line["worker_id"] == "w1"

    def test_returns_zero_even_on_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        rc = hooks.run_spike("Stop", stdin_text="not-json{")
        assert rc == 0
        log = db.parent / "hook-debug.log"
        assert log.exists()
        line = json.loads(log.read_text().strip())
        assert line["parse_error"] is True
        assert line["raw"] == "not-json{"


def _seed_worker(db: Path) -> sqlite3.Connection:
    conn = state.connect(db)
    state.init_schema(conn)
    state.create_worker(
        conn, id="w1", task="t", model="sonnet",
        branch="orch/w1", pane_target="s:1",
    )
    return conn


class TestTypedDispatch:
    def test_session_start_sets_working_and_records_session_ready(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        rc = hooks.dispatch("SessionStart", stdin_text='{"session_id":"abc"}')
        assert rc == 0
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "working"
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "session_ready" in kinds

    def test_stop_increments_turns_and_records_turn_complete(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}}
        rc = hooks.dispatch("Stop", stdin_text=json.dumps(payload))
        assert rc == 0
        w = state.get_worker(conn, "w1")
        assert w is not None and w.turns == 1
        evts = [e for e in state.list_events(conn, worker_id="w1")
                if e.kind == "turn_complete"]
        assert len(evts) == 1
        # token fields pulled from payload
        assert evts[0].payload.get("input_tokens") == 100
        assert evts[0].payload.get("output_tokens") == 50

    def test_session_end_sets_done_when_no_prior_error(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        state.update_worker(conn, "w1", status="working")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        rc = hooks.dispatch("SessionEnd", stdin_text="{}")
        assert rc == 0
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "done"

    def test_session_end_keeps_error_status(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        state.update_worker(conn, "w1", status="error")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        hooks.dispatch("SessionEnd", stdin_text="{}")
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "error"

    def test_session_end_keeps_cooperative_done_status(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Issue #2: SessionEnd guard tuple was missing 'done', so a worker
        # that had already called `orchestra worker done` could be re-flipped.
        conn = _seed_worker(tmp_db)
        state.update_worker(conn, "w1", status="done")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        hooks.dispatch("SessionEnd", stdin_text="{}")
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "done"

    def test_session_start_does_not_overwrite_done(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Issue #2: SessionStart -> worker_done -> SessionStart was clobbering
        # the cooperative 'done' back to 'working'.
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        # First SessionStart — normal entry.
        hooks.dispatch("SessionStart", stdin_text='{"session_id":"s1"}')
        # Worker calls `orchestra worker done` (mirrors cli.worker_done).
        state.update_worker(conn, "w1", status="done", progress="finished")
        state.record_event(conn, "worker_done", worker_id="w1", summary="finished")
        # Second SessionStart (re-attach / restart) — must NOT clobber.
        hooks.dispatch("SessionStart", stdin_text='{"session_id":"s2"}')
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "done"
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "done_to_working_blocked" in kinds

    def test_pre_post_tool_use_record_events_only(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        hooks.dispatch("PreToolUse",
                       stdin_text='{"tool_name":"Bash","tool_input":{"command":"ls"}}')
        hooks.dispatch("PostToolUse",
                       stdin_text='{"tool_name":"Bash","tool_output":"a\\nb"}')
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "tool_started" in kinds and "tool_finished" in kinds
        w = state.get_worker(conn, "w1")
        assert w is not None and w.turns == 0

    def test_internal_error_records_hook_error_and_returns_zero(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No worker exists for the env-set id; status update will raise.
        state.connect(tmp_db).close()
        # ^ Creates the DB file but skips init_schema, so any state.update_worker call
        #   in dispatch hits "no such table: workers" and triggers the hook_error path.
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "nope")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        rc = hooks.dispatch("SessionStart", stdin_text="{}")
        assert rc == 0  # NEVER non-zero
        err_log = tmp_db.parent / "hook-errors.log"
        assert err_log.exists()
