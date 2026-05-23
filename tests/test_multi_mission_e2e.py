"""End-to-end: two consecutive missions in a fresh project, plus sequential gate."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from orchestra import missions, state
from orchestra import run as run_mod


@pytest.fixture
def fresh_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git-initialized project dir with `.orchestra/state.db` initialized
    and a `.gitignore` excluding `.orchestra/`. cwd is set to the project."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "README").write_text("x")
    (tmp_path / ".gitignore").write_text(".orchestra/\nworktrees/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True,
    )
    (tmp_path / ".orchestra").mkdir(exist_ok=True)
    conn = state.connect(tmp_path / ".orchestra" / "state.db")
    state.init_schema(conn)
    conn.close()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stub_spawn_and_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make run_mission skip the real PM spawn + tmux side-effects."""
    def _no_op_spawn(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return None
    monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
    # Speed up the poll loop.
    monkeypatch.setattr(run_mod, "POLL_INTERVAL_S", 0.05)
    # Stub anything tmux-y if the run loop touches it. The current run_mission
    # doesn't call ensure_session/kill_session directly (spawn_worker does),
    # but we stub them defensively in case the codepath evolves.
    try:
        from orchestra import tmux
        monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(tmux, "kill_session", lambda *a, **k: None)
    except (ImportError, AttributeError):
        pass


def _emit_worker_done_after_delay(db: Path, delay: float = 0.2) -> None:
    """Background thread that records a worker_done event for the pm worker."""
    def _emit() -> None:
        time.sleep(delay)
        conn = state.connect(db)
        try:
            state.record_event(conn, "worker_done", worker_id="pm")
        finally:
            conn.close()
    threading.Thread(target=_emit, daemon=True).start()


class TestTwoConsecutiveMissions:
    def test_both_missions_complete_cleanly(
        self, fresh_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = fresh_project
        db = project / ".orchestra" / "state.db"
        # Scaffold + commit both missions so the clean-tree pre-flight passes.
        missions.scaffold_mission_dir(project, slug="alpha")
        missions.scaffold_mission_dir(project, slug="beta")
        subprocess.run(
            ["git", "-C", str(project), "add", "missions"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(project), "commit", "-q", "-m", "missions"], check=True,
        )
        _stub_spawn_and_tmux(monkeypatch)

        # Run mission alpha.
        _emit_worker_done_after_delay(db)
        code_a = run_mod.run_mission(
            Path("missions/alpha/mission.md"),
            model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert code_a == 0, "first mission must complete cleanly"

        # Confirm alpha is closed and there is no running mission.
        conn = state.connect(db)
        running = state.get_running_mission(conn)
        conn.close()
        assert running is None, "first mission must be in terminal state before next run"

        # Run mission beta (the gate must allow this).
        _emit_worker_done_after_delay(db)
        code_b = run_mod.run_mission(
            Path("missions/beta/mission.md"),
            model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert code_b == 0, "second mission must complete cleanly"

        # Final assertions.
        conn = state.connect(db)
        rows = state.list_missions(conn)
        running = state.get_running_mission(conn)
        conn.close()
        assert {r.slug for r in rows} == {"alpha", "beta"}
        assert all(r.status == "done" for r in rows)
        assert all(r.exit_code == 0 for r in rows)
        assert running is None


class TestSequentialGate:
    def test_blocks_when_another_mission_running(
        self, fresh_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = fresh_project
        db = project / ".orchestra" / "state.db"
        missions.scaffold_mission_dir(project, slug="alpha")
        subprocess.run(
            ["git", "-C", str(project), "add", "missions"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(project), "commit", "-q", "-m", "scaffold"], check=True,
        )

        # Seed a stuck running mission.
        conn = state.connect(db)
        state.create_mission(conn, slug="stuck", mission_path="(test)")
        conn.close()

        _stub_spawn_and_tmux(monkeypatch)

        code = run_mod.run_mission(
            Path("missions/alpha/mission.md"),
            model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert code == 2, "sequential gate must block while another mission is running"

        # Confirm the stuck mission is unchanged AND no new alpha row was created.
        conn = state.connect(db)
        rows = state.list_missions(conn)
        conn.close()
        assert len(rows) == 1
        assert rows[0].slug == "stuck"
        assert rows[0].status == "running"
