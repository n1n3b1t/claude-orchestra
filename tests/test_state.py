"""Tests for orchestra.state."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestra import state


def _open(tmp_db: Path) -> sqlite3.Connection:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    return conn


class TestConnect:
    def test_wal_and_busy_timeout(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode.lower() == "wal"
        assert timeout == 5000

    def test_init_schema_idempotent(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.init_schema(conn)  # must not raise


class TestWorkers:
    def test_create_then_get(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        created = state.create_worker(
            conn,
            id="w1",
            task="Implement auth",
            model="sonnet",
            branch="orch/w1",
            pane_target="orch-proj:1",
        )
        assert created.id == "w1"
        assert created.status == "spawning"
        assert created.turns == 0
        got = state.get_worker(conn, "w1")
        assert got == created

    def test_get_missing_returns_none(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        assert state.get_worker(conn, "nope") is None

    def test_update_worker(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        state.update_worker(conn, "w1", status="working", progress="ok", turns=3)
        got = state.get_worker(conn, "w1")
        assert got is not None
        assert got.status == "working"
        assert got.progress == "ok"
        assert got.turns == 3

    def test_list_workers(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.create_worker(
            conn, id="w1", task="t1", model="sonnet",
            branch=None, pane_target="s:1",
        )
        state.create_worker(
            conn, id="w2", task="t2", model="haiku",
            branch=None, pane_target="s:2",
        )
        rows = state.list_workers(conn)
        assert {w.id for w in rows} == {"w1", "w2"}


class TestEvents:
    def test_record_event_with_payload(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        evt = state.record_event(
            conn, kind="spawn_start", worker_id="w1", task="t", model="sonnet",
        )
        assert evt.id >= 1
        assert evt.kind == "spawn_start"
        assert evt.worker_id == "w1"
        assert evt.payload == {"task": "t", "model": "sonnet"}

    def test_list_events_filters_by_worker_and_since(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        a = state.record_event(conn, kind="spawn_start", worker_id="w1")
        b = state.record_event(conn, kind="spawn_window", worker_id="w1")
        c = state.record_event(conn, kind="spawn_start", worker_id="w2")

        all_events = state.list_events(conn)
        assert [e.id for e in all_events] == [a.id, b.id, c.id]

        w1_only = state.list_events(conn, worker_id="w1")
        assert [e.id for e in w1_only] == [a.id, b.id]

        after_a = state.list_events(conn, since_id=a.id)
        assert [e.id for e in after_a] == [b.id, c.id]


class TestEscalations:
    def test_create_then_resolve(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        esc = state.create_escalation(
            conn, worker_id="w1", question="RS256 or HS256?",
            context="key mgmt", blocking=True,
        )
        assert esc.resolved is False
        assert esc.answer is None
        opens = state.list_open_escalations(conn)
        assert [e.id for e in opens] == [esc.id]

        resolved = state.resolve_escalation(conn, esc.id, answer="Use RS256")
        assert resolved.resolved is True
        assert resolved.answer == "Use RS256"

        assert state.list_open_escalations(conn) == []

    def test_resolve_missing_raises(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        with pytest.raises(KeyError):
            state.resolve_escalation(conn, 999, answer="x")


def test_events_worker_ts_index_exists(tmp_db: Path) -> None:
    conn = _open(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "events_worker_ts" in names
