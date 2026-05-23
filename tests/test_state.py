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

    def test_update_worker_missing_raises(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        with pytest.raises(KeyError):
            state.update_worker(conn, "ghost", status="working")

    def test_update_worker_no_fields_raises(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="s:1",
        )
        with pytest.raises(ValueError):
            state.update_worker(conn, "w1")


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


class TestSchemaUpgrade:
    def test_v0_db_gets_new_columns_on_init(self, tmp_db: Path) -> None:
        # Simulate a v0 DB: original schema only.
        v0_sql = """
        CREATE TABLE workers (
            id TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL,
            branch TEXT, pane_target TEXT NOT NULL, status TEXT NOT NULL,
            progress TEXT, turns INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT,
            ts TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT NOT NULL,
            ts TEXT NOT NULL, question TEXT NOT NULL, context TEXT,
            blocking INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
            answer TEXT
        );
        """
        conn = state.connect(tmp_db)
        conn.executescript(v0_sql)
        # Now run init_schema — should add role/worktree columns.
        state.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        assert "role" in cols
        assert "worktree" in cols

    def test_v0_upgraded_db_supports_role_and_worktree_crud(self, tmp_db: Path) -> None:
        # Build a v0 DB and upgrade it via init_schema.
        v0_sql = """
        CREATE TABLE workers (
            id TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL,
            branch TEXT, pane_target TEXT NOT NULL, status TEXT NOT NULL,
            progress TEXT, turns INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT,
            ts TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT NOT NULL,
            ts TEXT NOT NULL, question TEXT NOT NULL, context TEXT,
            blocking INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
            answer TEXT
        );
        """
        conn = state.connect(tmp_db)
        conn.executescript(v0_sql)
        state.init_schema(conn)

        # Insert a worker using the new fields and read it back.
        w = state.create_worker(
            conn,
            id="upgraded-worker",
            task="post-upgrade task",
            model="sonnet",
            branch="orch/upgraded",
            pane_target="s:up",
            role="pm",
            worktree="myworktree",
        )
        assert w.role == "pm"
        assert w.worktree == "myworktree"

        got = state.get_worker(conn, "upgraded-worker")
        assert got is not None
        assert got.role == "pm"
        assert got.worktree == "myworktree"


class TestNoInitTolerance:
    def test_release_worker_resources_without_schema_is_noop(self) -> None:
        conn = sqlite3.connect(":memory:")
        result = state.release_worker_resources(conn, worker_id="any")
        assert result == 0

    def test_acquire_resource_without_schema_returns_false(self) -> None:
        conn = sqlite3.connect(":memory:")
        result = state.acquire_resource(conn, "res", "w1", blocking=False)
        assert result is False


class TestResourceLocks:
    def test_acquire_succeeds_when_no_lock(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        result = state.acquire_resource(conn, "device1", "w1", blocking=False)
        assert result is True
        row = conn.execute(
            "SELECT worker_id FROM resource_locks WHERE name = ?", ("device1",)
        ).fetchone()
        assert row is not None
        assert row[0] == "w1"

    def test_acquire_non_blocking_on_held_returns_false(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        assert state.acquire_resource(conn, "device1", "w1", blocking=False) is True
        assert state.acquire_resource(conn, "device1", "w2", blocking=False) is False

    def test_release_only_removes_matching_holder(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.acquire_resource(conn, "device1", "w1", blocking=False)
        assert state.release_resource(conn, "device1", "w2") is False
        assert state.release_resource(conn, "device1", "w1") is True
        row = conn.execute(
            "SELECT 1 FROM resource_locks WHERE name = ?", ("device1",)
        ).fetchone()
        assert row is None

    def test_release_worker_resources_removes_all(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.acquire_resource(conn, "dev-a", "w1", blocking=False)
        state.acquire_resource(conn, "dev-b", "w1", blocking=False)
        count = state.release_worker_resources(conn, "w1")
        assert count == 2
        rows = conn.execute("SELECT * FROM resource_locks").fetchall()
        assert rows == []

    def test_resource_locks_table_exists_after_init_schema(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        conn.execute("SELECT 1 FROM resource_locks LIMIT 1")  # must not raise


class TestRoleAndWorktree:
    def test_create_with_role_and_worktree(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        w = state.create_worker(
            conn, id="backend", task="api", model="sonnet",
            branch="orch/backend", pane_target="s:backend",
            role="engineer", worktree="backend",
        )
        assert w.role == "engineer"
        assert w.worktree == "backend"

    def test_default_role_is_engineer(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        w = state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        assert w.role == "engineer"
        assert w.worktree is None

    def test_pm_role(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        w = state.create_worker(
            conn, id="pm", task="lead", model="opus",
            branch=None, pane_target="s:pm", role="pm",
        )
        assert w.role == "pm"


class TestMissionsTable:
    def test_init_schema_creates_missions_table(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(missions)").fetchall()}
        assert {"id", "slug", "mission_path", "status",
                "exit_code", "started_at", "ended_at"} <= cols

    def test_init_schema_adds_mission_id_to_workers(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        assert "mission_id" in cols

    def test_init_schema_idempotent(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.init_schema(conn)  # second call must not raise
        rows = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
        assert rows == 0


class TestMissionsCRUD:
    def test_create_and_get_by_slug(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        mid = state.create_mission(conn, slug="m1", mission_path="missions/m1/mission.md")
        row = state.get_mission_by_slug(conn, "m1")
        assert row is not None and row.id == mid and row.status == "running"

    def test_get_running_mission(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        assert state.get_running_mission(conn) is None
        state.create_mission(conn, slug="m1", mission_path="p")
        running = state.get_running_mission(conn)
        assert running is not None and running.slug == "m1"

    def test_get_running_mission_raises_when_multiple(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.create_mission(conn, slug="m1", mission_path="p1")
        state.create_mission(conn, slug="m2", mission_path="p2")
        with pytest.raises(state.StateInvariantError):
            state.get_running_mission(conn)

    def test_update_mission_to_terminal(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        mid = state.create_mission(conn, slug="m1", mission_path="p")
        state.update_mission(conn, mid, status="done", exit_code=0,
                             ended_at=state.now_iso())
        row = state.get_mission_by_slug(conn, "m1")
        assert row is not None
        assert row.status == "done" and row.exit_code == 0 and row.ended_at is not None

    def test_list_missions_desc_by_started_at(self, tmp_db: Path) -> None:
        import time
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.create_mission(conn, slug="m1", mission_path="p1")
        time.sleep(0.01)
        state.create_mission(conn, slug="m2", mission_path="p2")
        rows = state.list_missions(conn)
        assert [r.slug for r in rows] == ["m2", "m1"]


class TestLegacyMigration:
    def test_archives_legacy_workers_into_one_mission(self, tmp_db: Path) -> None:
        import sqlite3 as _sqlite3
        # Build a v2.3-shaped DB by hand: workers table without missions.
        conn = _sqlite3.connect(str(tmp_db))
        conn.executescript("""
        CREATE TABLE workers (
            id          TEXT PRIMARY KEY,
            task        TEXT NOT NULL,
            model       TEXT NOT NULL,
            branch      TEXT,
            pane_target TEXT NOT NULL,
            status      TEXT NOT NULL,
            progress    TEXT,
            turns       INTEGER NOT NULL DEFAULT 0,
            started_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'engineer',
            worktree    TEXT
        );
        INSERT INTO workers VALUES
          ('pm', '', 'opus', NULL, 's:0.0', 'done', NULL, 5,
           '2026-05-20T09:42:00Z', '2026-05-20T10:10:00Z', 'pm', NULL),
          ('backend', '', 'sonnet', 'orch/backend', 's:0.1', 'done', NULL, 12,
           '2026-05-20T09:45:00Z', '2026-05-20T10:08:00Z', 'engineer', 'backend');
        """)
        conn.commit()
        conn.close()

        conn = state.connect(tmp_db)
        state.init_schema(conn)

        rows = state.list_missions(conn)
        assert len(rows) == 1
        legacy = rows[0]
        assert legacy.status == "archived"
        assert legacy.slug.startswith("legacy-")
        assert legacy.mission_path == "(unknown)"
        assert legacy.started_at == "2026-05-20T09:42:00Z"
        assert legacy.ended_at == "2026-05-20T10:10:00Z"

        worker_missions = {
            row[0]: row[1]
            for row in conn.execute("SELECT id, mission_id FROM workers").fetchall()
        }
        assert worker_missions == {"pm": legacy.id, "backend": legacy.id}

    def test_no_legacy_row_on_fresh_db(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        assert state.list_missions(conn) == []
