"""Tests for orchestra/run.py and the `orchestra run` CLI wiring."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestra import run as run_mod
from orchestra import state
from orchestra.__main__ import app

runner = CliRunner()


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def _init_orchestra(path: Path) -> Path:
    """Mirror what `orchestra init` does for a fresh project dir."""
    d = path / ".orchestra"
    d.mkdir(exist_ok=True)
    db = d / "state.db"
    conn = state.connect(db)
    state.init_schema(conn)
    conn.close()
    return db


def _gitignore_orchestra(path: Path) -> None:
    """Mark .orchestra/ ignored so the runner's clean-tree check passes."""
    gi = path / ".gitignore"
    gi.write_text(".orchestra/\n")
    subprocess.run(["git", "-C", str(path), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "ignore orchestra"],
        check=True,
    )


def _commit_mission(path: Path, mission: Path) -> None:
    subprocess.run(["git", "-C", str(path), "add", mission.name], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "add mission"],
        check=True,
    )


def _no_op_spawn(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """A spawn stub: don't touch tmux, don't create a worker row.

    The runner only requires that no pm worker row exists *before* spawn;
    it doesn't re-check after. The polling loop just watches events.
    """
    return None


class TestRunMissionHappyPath:
    def test_returns_0_when_pm_emits_worker_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\nDo a thing.\n")
        _commit_mission(tmp_path, mission)
        _gitignore_orchestra(tmp_path)
        db = _init_orchestra(tmp_path)

        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        monkeypatch.setattr(run_mod, "POLL_INTERVAL_S", 0.05)
        monkeypatch.chdir(tmp_path)

        def _emit_done_after_delay() -> None:
            time.sleep(0.3)
            conn = state.connect(db)
            try:
                state.record_event(
                    conn, "worker_done", worker_id="pm", summary="all done",
                )
            finally:
                conn.close()

        threading.Thread(target=_emit_done_after_delay, daemon=True).start()
        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert rc == 0

    def test_via_cli_prints_event_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ):
        _init_git_repo(tmp_path)
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\nDo a thing.\n")
        _commit_mission(tmp_path, mission)
        _gitignore_orchestra(tmp_path)
        db = _init_orchestra(tmp_path)

        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        monkeypatch.setattr(run_mod, "POLL_INTERVAL_S", 0.05)
        monkeypatch.chdir(tmp_path)

        def _emit_done_after_delay() -> None:
            time.sleep(0.3)
            conn = state.connect(db)
            try:
                state.record_event(
                    conn, "session_ready", worker_id="pm",
                )
                state.record_event(
                    conn, "worker_done", worker_id="pm", summary="all done",
                )
            finally:
                conn.close()

        threading.Thread(target=_emit_done_after_delay, daemon=True).start()
        result = runner.invoke(app, ["run", str(mission)])
        assert result.exit_code == 0, result.output
        # CliRunner captures the typer Exit; printed lines from run.py
        # are emitted via print() so should appear in capfd.
        out = result.output + (capfd.readouterr().out or "")
        assert "worker_done" in out


class TestWatchdogs:
    def test_wallclock_watchdog_exits_124(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        _commit_mission(tmp_path, mission)
        _gitignore_orchestra(tmp_path)
        _init_orchestra(tmp_path)

        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        monkeypatch.setattr(run_mod, "POLL_INTERVAL_S", 0.05)
        monkeypatch.chdir(tmp_path)

        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=0.5, max_activity=600.0,
        )
        assert rc == 124

    def test_activity_watchdog_exits_125(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        _commit_mission(tmp_path, mission)
        _gitignore_orchestra(tmp_path)
        db = _init_orchestra(tmp_path)

        # Inject an event up front so the activity timer starts and then
        # nothing else arrives.
        conn = state.connect(db)
        try:
            state.record_event(conn, "session_ready", worker_id="pm")
        finally:
            conn.close()

        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        monkeypatch.setattr(run_mod, "POLL_INTERVAL_S", 0.05)
        monkeypatch.chdir(tmp_path)

        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=600.0, max_activity=0.5,
        )
        assert rc == 125


class TestPreflight:
    def test_dirty_repo_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        _init_orchestra(tmp_path)
        # Dirty the tree.
        (tmp_path / "dirty.txt").write_text("uncommitted")

        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)

        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert rc == 2

    def test_dirty_repo_with_allow_dirty_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        _commit_mission(tmp_path, mission)
        _gitignore_orchestra(tmp_path)
        db = _init_orchestra(tmp_path)
        (tmp_path / "dirty.txt").write_text("uncommitted")  # intentional

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        monkeypatch.setattr(run_mod, "POLL_INTERVAL_S", 0.05)

        def _emit_done_after_delay() -> None:
            time.sleep(0.3)
            conn = state.connect(db)
            try:
                state.record_event(conn, "worker_done", worker_id="pm",
                                   summary="ok")
            finally:
                conn.close()

        threading.Thread(target=_emit_done_after_delay, daemon=True).start()
        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=30.0, max_activity=10.0,
            allow_dirty=True,
        )
        assert rc == 0

    def test_missing_mission_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        _init_orchestra(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        rc = run_mod.run_mission(
            tmp_path / "nope.md", model="opus",
            max_wallclock=30.0, max_activity=10.0,
        )
        assert rc == 2

    def test_missing_orchestra_dir_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        # Intentionally do NOT call _init_orchestra(tmp_path).
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert rc == 2

    def test_existing_pm_row_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_git_repo(tmp_path)
        db = _init_orchestra(tmp_path)
        # Seed a pre-existing 'pm' worker row.
        conn = state.connect(db)
        try:
            state.create_worker(
                conn, id="pm", task="", model="opus",
                branch=None, pane_target="s:pm", role="pm",
            )
        finally:
            conn.close()
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert rc == 2

    def test_not_a_git_repo_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # tmp_path is not a git repo; even with .orchestra/ present we bail.
        _init_orchestra(tmp_path)
        mission = tmp_path / "mission.md"
        mission.write_text("# Mission\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(run_mod.spawn, "spawn_worker", _no_op_spawn)
        rc = run_mod.run_mission(
            mission, model="opus", max_wallclock=30.0, max_activity=10.0,
        )
        assert rc == 2


class TestPayloadSummary:
    def test_truncates_long_payload(self):
        long = "x" * 500
        import json as _json
        s = run_mod._summarize_payload(_json.dumps({"k": long}))
        assert len(s) <= 120
        assert s.endswith("…")

    def test_empty_payload(self):
        assert run_mod._summarize_payload("") == ""

    def test_handles_invalid_json(self):
        # Should not raise; falls through to the raw string.
        out = run_mod._summarize_payload("not json {")
        assert "not json" in out


class TestCliHelp:
    def test_run_help_prints_and_exits_0(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0, result.output
        assert "MISSION_MD" in result.output
        assert "--max-wallclock" in result.output
