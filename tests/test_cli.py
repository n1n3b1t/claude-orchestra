"""Tests for orchestra/cli.py."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from orchestra import cli, state
from orchestra.__main__ import app

runner = CliRunner()


def _init_in(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output


class TestInit:
    def test_creates_state_and_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / ".orchestra" / "state.db").exists()
        assert (tmp_path / ".orchestra" / "config.toml").exists()

    def test_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["init"]).exit_code == 0
        assert runner.invoke(app, ["init"]).exit_code == 0


class TestSpawn:
    def test_invokes_spawn_worker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        called: dict = {}

        def fake_spawn(conn, **kw):
            called.update(kw)

        monkeypatch.setattr(cli.spawn, "spawn_worker", fake_spawn)
        result = runner.invoke(app, ["spawn", "w1", "sonnet", "do thing"])
        assert result.exit_code == 0, result.output
        assert called["worker_id"] == "w1"
        assert called["model"] == "sonnet"
        assert called["task"] == "do thing"


class TestStatus:
    def test_lists_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="orch-x:w1",
        )
        state.update_worker(conn, "w1", status="working", progress="busy", turns=2)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "w1" in result.output
        assert "working" in result.output

    def test_worker_detail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="my task", model="sonnet",
            branch="orch/w1", pane_target="orch-x:w1",
        )
        state.update_worker(conn, "w1", status="working", turns=3)
        result = runner.invoke(app, ["status", "--worker", "w1"])
        assert result.exit_code == 0
        assert "w1" in result.output
        assert "working" in result.output


class TestWorkerCommands:
    def test_status_requires_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ORCHESTRA_WORKER_ID", raising=False)
        monkeypatch.delenv("ORCHESTRA_STATE_DB", raising=False)
        result = runner.invoke(app, ["worker", "status", "--progress", "x", "--turns", "1"])
        assert result.exit_code == 2
        assert "must run inside a spawned worker pane" in result.output

    def test_status_writes_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        result = runner.invoke(
            app, ["worker", "status", "--progress", "made progress", "--turns", "5"]
        )
        assert result.exit_code == 0, result.output
        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.progress == "made progress"
        assert w.turns == 5
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "status" in kinds

    def test_escalate_blocking_sets_waiting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        state.update_worker(conn, "w1", status="working")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        result = runner.invoke(
            app,
            ["worker", "escalate", "--blocking",
             "--question", "RS256 or HS256?", "--context", "tradeoffs"],
        )
        assert result.exit_code == 0, result.output
        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.status == "waiting"
        opens = state.list_open_escalations(conn)
        assert len(opens) == 1


class TestStop:
    def test_sends_ctrl_c_twice_and_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )

        tmux_mock = MagicMock()
        monkeypatch.setattr(cli, "tmux", tmux_mock)

        result = runner.invoke(app, ["stop", "w1"])
        assert result.exit_code == 0, result.output

        assert tmux_mock.send_ctrl_c.call_count == 2

        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.status == "stopped"


class TestRequiresInit:
    def test_status_exits_2_without_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 2
        assert "orchestra init" in result.output

    def test_stop_exits_2_without_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stop", "w1"])
        assert result.exit_code == 2
        assert "orchestra init" in result.output


class TestTail:
    def test_tail_prints_capture(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        tmux_mock = MagicMock()
        tmux_mock.capture.return_value = "pane output here"
        monkeypatch.setattr(cli, "tmux", tmux_mock)
        result = runner.invoke(app, ["tail", "w1"])
        assert result.exit_code == 0, result.output
        assert "pane output here" in result.output
        tmux_mock.capture.assert_called_once_with("orch-x:w1", lines=80)
