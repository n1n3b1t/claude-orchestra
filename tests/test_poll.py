"""Tests for orchestra.poll (state snapshot rendering)."""
from __future__ import annotations

import time
from pathlib import Path

from orchestra import poll, state


def _setup(tmp_db: Path) -> None:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    state.create_worker(
        conn, id="backend", task="api", model="sonnet",
        branch="orch/backend", pane_target="s:backend",
        role="engineer", worktree="backend",
    )
    state.create_worker(
        conn, id="frontend", task="ui", model="sonnet",
        branch="orch/frontend", pane_target="s:frontend",
        role="engineer", worktree="frontend",
    )
    conn.close()


class TestSnapshot:
    def test_renders_per_engineer_rows(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        out = poll.render_snapshot(tmp_db, since_id=0)
        assert "backend" in out and "frontend" in out

    def test_tool_events_filtered_out_of_count(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.record_event(conn, "tool_started", worker_id="backend", tool="Read")
        state.record_event(conn, "tool_finished", worker_id="backend", tool="Read")
        state.record_event(conn, "turn_complete", worker_id="backend", input_tokens=10)
        conn.close()
        out = poll.render_snapshot(tmp_db, since_id=0)
        # The new-event count for backend should be 1 (turn_complete), not 3.
        line = next(ln for ln in out.splitlines() if "backend" in ln)
        assert "1" in line  # naive: at least one number that's 1
        # tool_started/tool_finished should not appear in the human-readable summary.
        assert "tool_started" not in out
        assert "tool_finished" not in out

    def test_worker_done_counted_as_interesting(self, tmp_db: Path) -> None:
        # Issue #4: worker_done was emitted by `orchestra worker done` but
        # excluded from INTERESTING_KINDS, so a PM keying on the new-event
        # count missed engineer completions between polls.
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.record_event(conn, "tool_started", worker_id="backend", tool="Read")
        state.record_event(conn, "worker_done", worker_id="backend", summary="ok")
        conn.close()
        out = poll.render_snapshot(tmp_db, since_id=0)
        # The new-event count for backend should be 1 (worker_done), not 0.
        line = next(ln for ln in out.splitlines() if "backend" in ln)
        assert "1" in line

    def test_pending_escalations_listed(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.create_escalation(
            conn, worker_id="backend",
            question="What is the API contract?",
            context=None, blocking=True,
        )
        conn.close()
        out = poll.render_snapshot(tmp_db, since_id=0)
        assert "API contract" in out


class TestBlocking:
    def test_returns_immediately_when_changes_since_cursor(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.record_event(conn, "turn_complete", worker_id="backend")
        max_id_before = max(e.id for e in state.list_events(conn))
        conn.close()
        start = time.monotonic()
        new_cursor, _ = poll.poll(tmp_db, since_id=0, timeout=5)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert new_cursor >= max_id_before

    def test_blocks_until_event_arrives(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        max_id_before = max((e.id for e in state.list_events(conn)), default=0)
        conn.close()

        import threading
        def write_after_delay() -> None:
            time.sleep(0.3)
            c = state.connect(tmp_db)
            state.record_event(c, "turn_complete", worker_id="backend")
            c.close()
        t = threading.Thread(target=write_after_delay)
        t.start()
        try:
            new_cursor, snapshot = poll.poll(
                tmp_db, since_id=max_id_before, timeout=3,
                poll_interval_s=0.05,
            )
        finally:
            t.join()
        assert new_cursor > max_id_before
        assert "backend" in snapshot

    def test_returns_after_timeout_even_with_no_events(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        start = time.monotonic()
        cursor, _ = poll.poll(tmp_db, since_id=10_000, timeout=0.3, poll_interval_s=0.05)
        elapsed = time.monotonic() - start
        assert 0.3 <= elapsed < 1.5
