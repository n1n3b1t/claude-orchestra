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

    def test_installs_hooks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        settings = tmp_path / ".claude" / "settings.local.json"
        assert settings.exists()
        got = __import__("json").loads(settings.read_text())
        assert "SessionStart" in got["hooks"]


class TestSessionNameFor:
    """The session-name sanitiser keeps tmux's `session.window` target syntax safe."""

    def test_drops_dots(self):
        # mktemp -d-style basename has a literal dot — tmux splits on it
        assert cli._session_name_for(Path("/tmp/tmp.UUR8ZsRLFe")) == "orch-tmp-uur8zsrlfe"

    def test_collapses_punctuation(self):
        assert cli._session_name_for(Path("/work/My Project (v2)!")) == "orch-my-project-v2"

    def test_fallback_for_empty(self):
        # Root '/' has empty basename, sanitiser must fall back to 'orch'
        assert cli._session_name_for(Path("/")) == "orch"


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
        assert called["project_root"] == str(tmp_path)
        assert called["session_name"] == cli._session_name_for(tmp_path)
        assert called["state_db"] == tmp_path / ".orchestra" / "state.db"
        assert called["ctx_files"] == []

    def test_spawn_passes_context_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        called: dict = {}

        def fake_spawn(conn, **kw):
            called.update(kw)

        monkeypatch.setattr(cli.spawn, "spawn_worker", fake_spawn)
        result = runner.invoke(
            app, ["spawn", "w1", "sonnet", "do", "--context", "a.py", "--context", "b.py"]
        )
        assert result.exit_code == 0, result.output
        assert called["ctx_files"] == ["a.py", "b.py"]


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

    def test_default_shows_tokens(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Default cost mode is tokens: output contains token summary, not $."""
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="orch-x:w1",
        )
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        # Zero tokens → compact tokens format
        assert "0/0 cache=0" in result.output

    def test_cost_mode_dollars_shows_dollar(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """--cost-mode dollars preserves $X.XX output."""
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="orch-x:w1",
        )
        result = runner.invoke(app, ["status", "--cost-mode", "dollars"])
        assert result.exit_code == 0
        assert "$" in result.output

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

    def test_escalate_non_blocking_keeps_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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
            ["worker", "escalate", "--question", "minor q"],
        )
        assert result.exit_code == 0, result.output
        w = state.get_worker(conn, "w1")
        assert w is not None
        # status MUST still be "working" — not "waiting"
        assert w.status == "working"
        opens = state.list_open_escalations(conn)
        assert len(opens) == 1
        assert opens[0].blocking is False


class TestWorkerDone:
    def test_writes_status_done_and_worker_done_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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
            ["worker", "done", "--summary", "verified, code=Aah5jM"],
        )
        assert result.exit_code == 0, result.output
        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.status == "done"
        assert w.progress == "verified, code=Aah5jM"
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "worker_done" in kinds


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

    def test_stop_when_send_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        conn.close()

        import subprocess
        tmux_mock = MagicMock()
        tmux_mock.send_ctrl_c.side_effect = subprocess.CalledProcessError(1, ["tmux"])
        monkeypatch.setattr(cli, "tmux", tmux_mock)

        result = runner.invoke(app, ["stop", "w1"])
        assert result.exit_code == 1
        assert "may still be running" in result.output.lower() or "failed" in result.output.lower()
        conn = state.connect(db)
        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.status == "stop_send_failed"


class TestRequiresInit:
    def test_status_exits_2_without_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 2
        assert "run `orchestra init` first" in result.output

    def test_stop_exits_2_without_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stop", "w1"])
        assert result.exit_code == 2
        assert "run `orchestra init` first" in result.output

    def test_spawn_fails_without_init(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["spawn", "w1", "sonnet", "do"])
        assert result.exit_code == 2
        assert "run `orchestra init` first" in result.output

    def test_tail_fails_without_init(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["tail", "w1"])
        assert result.exit_code == 2
        assert "run `orchestra init` first" in result.output


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

    def test_tail_with_lines_option(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _init_in(tmp_path, monkeypatch)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        conn.close()
        tmux_mock = MagicMock()
        tmux_mock.capture.return_value = "screen"
        monkeypatch.setattr(cli, "tmux", tmux_mock)
        result = runner.invoke(app, ["tail", "w1", "-n", "200"])
        assert result.exit_code == 0
        tmux_mock.capture.assert_called_once_with("orch-x:w1", lines=200)


class TestSpawnFlags:
    def test_role_brief_worktree_flags_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        # Make tmp_path a git repo so worktree creation works.
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"],
                       check=True)
        (tmp_path / "README.md").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"],
                       check=True)
        (tmp_path / "brief.md").write_text("do backend stuff")
        called: dict = {}
        def fake_spawn(conn, **kw):
            called.update(kw)
        monkeypatch.setattr(cli.spawn, "spawn_worker", fake_spawn)
        result = runner.invoke(app, [
            "spawn", "backend", "sonnet", "implement",
            "--role", "engineer", "--brief", "brief.md", "--worktree", "backend",
        ])
        assert result.exit_code == 0, result.output
        assert called["role"] == "engineer"
        assert called["brief"] == "do backend stuff"
        assert called["worktree_name"] == "backend"


class TestSend:
    def test_send_records_event_and_calls_tmux(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        # Seed a worker row.
        with cli._open_db() as conn:  # type: ignore[attr-defined]
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer",
            )
        sent: list = []
        def fake_send(target, msg, **kw):
            sent.append((target, msg))
        monkeypatch.setattr(cli.tmux, "send_multiline", fake_send)
        result = runner.invoke(app, ["send", "backend", "merge conflict in app.py"])
        assert result.exit_code == 0, result.output
        assert sent == [("s:backend", "merge conflict in app.py")]
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "message_sent" in kinds


class TestAnswer:
    def test_answer_resolves_and_sends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer",
            )
            esc = state.create_escalation(
                conn, worker_id="backend",
                question="contract?", context=None, blocking=True,
            )
        sent: list = []
        monkeypatch.setattr(cli.tmux, "send_multiline",
                            lambda t, m, **k: sent.append((t, m)))
        result = runner.invoke(app, ["answer", str(esc.id), "use {code:str}"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            open_now = state.list_open_escalations(conn)
        assert open_now == []
        assert sent and sent[0][0] == "s:backend"
        assert "use {code:str}" in sent[0][1]

    def test_answer_invalid_id_exits_2_with_clean_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Issue #3: unhandled KeyError on a bad/typoed escalation id used to
        # surface as a Python traceback with exit 1, with no actionable message.
        _init_in(tmp_path, monkeypatch)
        monkeypatch.setattr(cli.tmux, "send_multiline",
                            lambda t, m, **k: None)
        result = runner.invoke(app, ["answer", "9999", "irrelevant"])
        assert result.exit_code == 2
        assert "no open escalation #9999" in (result.stderr or result.output)


class TestPoll:
    def test_poll_prints_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer", worktree="backend",
            )
        result = runner.invoke(app, ["poll", "--timeout", "0.1", "--caller", "pm"])
        assert result.exit_code == 0, result.output
        assert "backend" in result.output


class TestMergeReap:
    def test_merge_records_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        # Stub the git call so the test stays hermetic.
        import subprocess as _sub

        from orchestra import worktree as wt_mod
        calls: list = []
        def fake_run(argv, **kw):  # noqa: ANN001
            calls.append(argv)
            return _sub.CompletedProcess(argv, 0, stdout="", stderr="")
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: None,
        )
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer", worktree="backend",
            )
        result = runner.invoke(app, ["merge", "backend"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "merge_attempted" in kinds
        assert "merge_ok" in kinds

    def test_reap_calls_worktree_remove_and_records_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        from orchestra import worktree as wt_mod
        calls: list = []
        def fake_remove(project_root, *, name, worker_id, mission_slug=None):  # noqa: ANN001
            calls.append({"project_root": project_root,
                          "name": name, "worker_id": worker_id})
        monkeypatch.setattr(wt_mod, "remove", fake_remove)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer", worktree="backend",
            )
        result = runner.invoke(app, ["reap", "backend"])
        assert result.exit_code == 0, result.output
        assert calls == [
            {"project_root": Path.cwd(), "name": "backend", "worker_id": "backend"},
        ]
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "worktree_reaped" in kinds

    def test_reap_no_worktree_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A worker without a worktree (e.g. PM, or v0 worker) cannot be reaped."""
        _init_in(tmp_path, monkeypatch)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="pm", task="coordinate", model="opus",
                branch=None, pane_target="s:pm",
                role="pm", worktree=None,
            )
        result = runner.invoke(app, ["reap", "pm"])
        assert result.exit_code == 2
        assert "no worktree" in (result.stderr or result.output)

    def test_reap_unknown_worker_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        result = runner.invoke(app, ["reap", "ghost"])
        assert result.exit_code == 2
        assert "no worktree" in (result.stderr or result.output)

    def test_reap_tolerates_already_gone_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`worktree.remove` is a clean no-op when the dir is already gone — reap
        still records `worktree_reaped` and does not mutate worker status."""
        _init_in(tmp_path, monkeypatch)
        from orchestra import worktree as wt_mod
        # Simulate the real worktree.remove's no-op-when-missing behaviour.
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: None,
        )
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer", worktree="backend",
            )
            state.update_worker(conn, "backend", status="done")
        result = runner.invoke(app, ["reap", "backend"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
            w = state.get_worker(conn, "backend")
        assert "worktree_reaped" in kinds
        # reap does not mutate worker status — cooperative `worker done` owns it.
        assert w is not None
        assert w.status == "done"


class TestReapDefault:
    """v2.2: `orchestra merge` auto-reaps the worktree+branch on success.
    `--keep` opts out and preserves the legacy v2.1 behaviour."""

    def _seed(self, wid: str) -> None:
        with cli._open_db() as conn:
            state.create_worker(
                conn, id=wid, task="t", model="sonnet",
                branch=f"orch/{wid}", pane_target=f"s:{wid}",
                role="engineer", worktree=wid,
            )

    def test_merge_reaps_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        import subprocess as _sub

        from orchestra import worktree as wt_mod
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda argv, **k: _sub.CompletedProcess(argv, 0, stdout="", stderr=""),
        )
        remove_calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: remove_calls.append(
                {"project_root": project_root, "name": name, "worker_id": worker_id}
            ),
        )
        self._seed("backend")
        result = runner.invoke(app, ["merge", "backend"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "merge_ok" in kinds
        assert "worktree_reaped" in kinds
        assert remove_calls == [
            {"project_root": Path.cwd(), "name": "backend", "worker_id": "backend"},
        ]

    def test_merge_keeps_on_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        import subprocess as _sub

        from orchestra import worktree as wt_mod
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda argv, **k: _sub.CompletedProcess(
                argv, 1, stdout="CONFLICT (content)", stderr="",
            ),
        )
        remove_calls: list[str] = []
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: remove_calls.append(name),
        )
        self._seed("backend")
        result = runner.invoke(app, ["merge", "backend"])
        assert result.exit_code == 1
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "merge_conflict" in kinds
        assert "worktree_reaped" not in kinds
        assert remove_calls == []

    def test_merge_batch_reaps_successes_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        import subprocess as _sub

        from orchestra import worktree as wt_mod

        def fake_run(argv, **kw):  # noqa: ANN001
            # Second merge (orch/web) conflicts.
            if "merge" in argv and "orch/web" in argv:
                return _sub.CompletedProcess(
                    argv, 1, stdout="CONFLICT", stderr="",
                )
            return _sub.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        remove_calls: list[str] = []
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: remove_calls.append(name),
        )
        for wid in ("backend", "web", "cli"):
            self._seed(wid)
        result = runner.invoke(
            app, ["merge", "--batch", "backend", "web", "cli"],
        )
        # Any non-ok → exit 2 (existing batch contract).
        assert result.exit_code == 2, result.output

        # Only the 1st success was reaped; the conflict (web) is kept for
        # inspection; the skipped one (cli) is untouched.
        assert remove_calls == ["backend"]

        with cli._open_db() as conn:
            backend_kinds = [
                e.kind for e in state.list_events(conn, worker_id="backend")
            ]
            web_kinds = [
                e.kind for e in state.list_events(conn, worker_id="web")
            ]
            cli_kinds = [
                e.kind for e in state.list_events(conn, worker_id="cli")
            ]
        assert "merge_ok" in backend_kinds
        assert "worktree_reaped" in backend_kinds
        assert "merge_conflict" in web_kinds
        assert "worktree_reaped" not in web_kinds
        # The skipped worker has no events of its own.
        assert "merge_attempted" not in cli_kinds
        assert "worktree_reaped" not in cli_kinds

    def test_keep_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        import subprocess as _sub

        from orchestra import worktree as wt_mod
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda argv, **k: _sub.CompletedProcess(argv, 0, stdout="", stderr=""),
        )
        remove_calls: list[str] = []
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: remove_calls.append(name),
        )
        self._seed("backend")
        result = runner.invoke(app, ["merge", "backend", "--keep"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "merge_ok" in kinds
        assert "worktree_reaped" not in kinds
        assert remove_calls == []

    def test_keep_flag_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`--keep` also opts out of reaping in batch mode."""
        _init_in(tmp_path, monkeypatch)
        import subprocess as _sub

        from orchestra import worktree as wt_mod
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda argv, **k: _sub.CompletedProcess(argv, 0, stdout="", stderr=""),
        )
        remove_calls: list[str] = []
        monkeypatch.setattr(
            wt_mod, "remove",
            lambda project_root, *, name, worker_id, mission_slug=None: remove_calls.append(name),
        )
        for wid in ("backend", "web"):
            self._seed(wid)
        result = runner.invoke(
            app, ["merge", "--batch", "--keep", "backend", "web"],
        )
        assert result.exit_code == 0, result.output
        assert remove_calls == []
        with cli._open_db() as conn:
            backend_kinds = [
                e.kind for e in state.list_events(conn, worker_id="backend")
            ]
        assert "merge_ok" in backend_kinds
        assert "worktree_reaped" not in backend_kinds


class TestMissionCommands:
    """Integration tests for orchestra mission new/list/show/run."""

    def _init_project(self, tmp_path: Path) -> None:
        from orchestra import state
        (tmp_path / ".orchestra").mkdir()
        conn = state.connect(tmp_path / ".orchestra" / "state.db")
        state.init_schema(conn)
        conn.close()

    def test_mission_new_creates_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "new", "alpha"])
        assert res.exit_code == 0, res.output
        assert (tmp_path / "missions" / "alpha" / "mission.md").exists()
        assert (tmp_path / "missions" / "alpha" / "verifier.sh").exists()

    def test_mission_new_rejects_bad_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "new", "BAD-SLUG"])
        assert res.exit_code == 2

    def test_mission_list_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "list"])
        assert res.exit_code == 0
        assert "(no missions yet)" in res.output

    def test_mission_list_with_running_row_bold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from orchestra import state
        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        conn = state.connect(tmp_path / ".orchestra" / "state.db")
        state.create_mission(conn, slug="m1", mission_path="missions/m1/mission.md")
        conn.close()
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "list"])
        assert res.exit_code == 0
        assert "**m1**" in res.output

    def test_mission_show_unknown_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "show", "nope"])
        assert res.exit_code == 2

    def test_mission_run_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "run", "ghost"])
        assert res.exit_code == 2
        assert "does not exist" in res.output

    def test_mission_lint_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression — the existing `mission lint` command must still work
        after the subgroup refactor."""
        from typer.testing import CliRunner

        from orchestra.cli import app
        monkeypatch.chdir(tmp_path)
        m = tmp_path / "mission.md"
        m.write_text("# Mission: hello\n\n## Acceptance\n- thing\n\n"
                     "## Team\n- engineer\n\nworker_done\n")
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "lint", str(m)])
        assert res.exit_code == 0, res.output
