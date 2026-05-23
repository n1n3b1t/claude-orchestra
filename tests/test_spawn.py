from __future__ import annotations

import json
import shlex
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestra import spawn, state, tmux


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
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        # Stub _wait_idle_via_event to inject session_ready and return True.
        def fake_wait_idle(conn_, worker_id_, *, target=None):
            state.record_event(conn_, "session_ready", worker_id=worker_id_)
            return True
        monkeypatch.setattr(spawn, "_wait_idle_via_event", fake_wait_idle)

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
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **kw: True)

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
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **kw: True)

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
    def test_soft_timeout_continues_spawn_flow(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        """Idle-wait timeout is now 'soft': sets stale_spawn and continues.

        The spawn flow must record spawn_stale_idle, set status=stale_spawn,
        AND still proceed to model switch + prompt injection (model_switched
        event must be present). Because the final spawn_ok write at the end
        of the flow flips status to working, that recovery is what we assert.
        """
        conn = _open(tmp_db)
        # No session_ready event will arrive → _wait_idle_via_event times out.
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
        # Final status is "working" because spawn_ok is written unconditionally
        # at the end of the flow now that there is no second proof-of-life wait.
        assert worker.status == "working"
        kinds = _kinds(conn, "w1")
        # Soft-timeout event recorded (not spawn_timeout):
        assert "spawn_stale_idle" in kinds
        assert "spawn_timeout" not in kinds
        # Spawn flow continued past the timeout — model switch must have fired:
        assert "model_switched" in kinds
        # Prompt injection was also attempted:
        assert "prompt_injected" in kinds
        # And spawn_ok was recorded at the end:
        assert "spawn_ok" in kinds


class TestPromptInjectFailure:
    def test_two_failures_mark_error(
        self, tmp_db, tmp_orch_dir, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        # Both attempts raise — exhausts the (1, 2) retry loop
        fake_tmux.send_multiline.side_effect = RuntimeError("buffer too big")
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        # _wait_idle_via_event must succeed so we reach prompt injection.
        def fake_wait_idle(conn_, worker_id_, *, target=None):
            state.record_event(conn_, "session_ready", worker_id=worker_id_)
            return True
        monkeypatch.setattr(spawn, "_wait_idle_via_event", fake_wait_idle)

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
        # Compress the wait loop timing so the test is fast.
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 2.0)
        monkeypatch.setattr(spawn, "BOOT_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        # Capture returns trust-prompt text the first time; then plain screen.
        trust_screen = (
            "Is this a project you created or one you trust?\n"
            "❯ 1. Yes, I trust this folder\n  2. No, exit\n"
        )
        cap_returns = [trust_screen, "❯ ", "❯ "]
        fake_tmux.capture.side_effect = lambda *a, **kw: (
            cap_returns.pop(0) if cap_returns else "❯ "
        )

        # After trust dismissal, inject session_ready on the next DB poll
        # by wrapping _wait_idle_via_event to do real trust-prompt logic but
        # inject the event after a couple of iterations.
        real_has_event = spawn._has_event
        call_counts: dict[str, int] = {"n": 0}

        def patched_has_event(conn_, *, worker_id, kind):
            if kind == "session_ready":
                call_counts["n"] += 1
                if call_counts["n"] >= 3:
                    # inject the event so the loop finds it
                    state.record_event(conn_, "session_ready", worker_id=worker_id)
            return real_has_event(conn_, worker_id=worker_id, kind=kind)

        monkeypatch.setattr(spawn, "_has_event", patched_has_event)

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
        # model_switched implies the trust handling unblocked _wait_idle_via_event
        assert "model_switched" in kinds
        # Trust acceptance sent at least one Enter (trust dismiss)
        enter_calls = fake_tmux.send_enter.call_args_list
        assert len(enter_calls) >= 1


class TestEventDrivenWaits:
    def test_wait_idle_returns_true_when_session_ready_event_arrives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        # No event yet — first call times out fast.
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 0.2)
        monkeypatch.setattr(spawn, "BOOT_POLL_S", 0.05)
        assert spawn._wait_idle_via_event(conn, "w1") is False

        # Now insert the event and try again — must succeed.
        state.record_event(conn, "session_ready", worker_id="w1")
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 1.0)
        assert spawn._wait_idle_via_event(conn, "w1") is True


class TestSpawnFirstStatusRemoved:
    """Issue #18: the first-status (turn_complete) proof-of-life wait was
    removed because for engineers doing autonomous multi-step work, Stop only
    fires once the whole task is done — minutes-to-hours after spawn. The
    wait used to time out on a healthy worker and flip status to stale_spawn.
    `session_ready` from SessionStart is now the sole proof-of-life signal.
    """

    def test_helper_is_gone(self) -> None:
        assert not hasattr(spawn, "_wait_first_status_via_event")
        assert not hasattr(spawn, "FIRST_STATUS_TIMEOUT_S")
        assert not hasattr(spawn, "FIRST_STATUS_POLL_S")

    def test_status_working_and_no_first_status_timeout_event(
        self, tmp_db, tmp_orch_dir, fake_tmux, monkeypatch
    ):
        """End-to-end: with session_ready arriving and no turn_complete ever,
        the worker still ends up status=working and there is no
        spawn_first_status_timeout event."""
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        def fake_wait_idle(conn_, worker_id_, *, target=None):
            state.record_event(conn_, "session_ready", worker_id=worker_id_)
            return True
        monkeypatch.setattr(spawn, "_wait_idle_via_event", fake_wait_idle)

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="Long autonomous task",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "working"

        kinds = _kinds(conn, "w1")
        assert "spawn_ok" in kinds
        assert "spawn_first_status_timeout" not in kinds, (
            "the first-status timeout event should never be recorded anymore"
        )


class TestWorktreeFailure:
    def test_worktree_add_failure_records_event_and_marks_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """worktree_mod.add raising leaves a worker row + worktree_failed event."""
        def _raise(*a, **kw):
            raise RuntimeError("git not initialised")

        monkeypatch.setattr(spawn, "worktree_mod", MagicMock(add=_raise))
        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:x"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(spawn.tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="x",
            model="sonnet",
            task="t",
            project_root=str(tmp_path),
            state_db=db,
            ctx_files=[],
            session_name="orch-x",
            worktree_name="x",
        )

        worker = state.get_worker(conn, "x")
        assert worker is not None, "worker row must exist even after worktree failure"
        assert worker.status == "error"

        kinds = _kinds(conn, "x")
        assert "worktree_failed" in kinds, f"expected worktree_failed in {kinds}"

        # The error message must appear in the event payload.
        events = state.list_events(conn, worker_id="x")
        wf_event = next(e for e in events if e.kind == "worktree_failed")
        assert "git not initialised" in str(wf_event.payload)


class TestSpawnRoleSwitching:
    def test_pm_role_uses_pm_renderer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mock everything except the renderer-selection branch.
        from orchestra import role_prompts
        called: dict[str, str | None] = {"which": None}
        monkeypatch.setattr(
            role_prompts, "render_pm_prompt",
            lambda **kw: (called.__setitem__("which", "pm") or "PM PROMPT"),
        )
        monkeypatch.setattr(
            role_prompts, "render_engineer_prompt",
            lambda **kw: (called.__setitem__("which", "eng") or "ENG PROMPT"),
        )
        # Stub out tmux calls.
        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:pm"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        spawn.spawn_worker(
            conn, worker_id="pm", model="opus", task="lead",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x", role="pm",
            brief="MISSION BODY", worktree_name=None,
        )
        assert called["which"] == "pm"

    def test_engineer_role_uses_render_role_not_shim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """role='engineer' now goes through the generic branch (render_role),
        not the render_engineer_prompt shim. The bundled engineer.md is used."""
        from orchestra import role_prompts
        called: dict[str, str | None] = {"which": None}
        monkeypatch.setattr(
            role_prompts, "render_pm_prompt",
            lambda **kw: (called.__setitem__("which", "pm") or "PM PROMPT"),
        )
        original_render_role = role_prompts.render_role
        def patched_render_role(name: str, **kw: object) -> str:
            called["which"] = name
            return original_render_role(name, **kw)  # type: ignore[arg-type]
        monkeypatch.setattr(role_prompts, "render_role", patched_render_role)
        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:eng1"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        spawn.spawn_worker(
            conn, worker_id="eng1", model="sonnet", task="build auth",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x", role="engineer",
            brief="implement auth", worktree_name=None,
        )
        assert called["which"] == "engineer"

    def test_no_role_uses_v0_renderer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestra import prompts
        called: dict[str, bool] = {"v0": False}
        original = prompts.render_startup_prompt
        def patched(**kw):
            called["v0"] = True
            return original(**kw)
        monkeypatch.setattr(prompts, "render_startup_prompt", patched)
        # Also patch spawn.prompts (the module reference in spawn.py)
        monkeypatch.setattr(spawn.prompts, "render_startup_prompt", patched)

        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:w1"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        spawn.spawn_worker(
            conn, worker_id="w1", model="sonnet", task="do stuff",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x",
            # no role, brief, or worktree_name — v0 path
        )
        assert called["v0"] is True

    def test_v0_caller_without_role_kwargs_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn_worker with no role/brief/worktree_name should complete without error."""
        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:w1"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        spawn.spawn_worker(
            conn, worker_id="w1", model="sonnet", task="t",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x",
        )
        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "working"


class TestSpawnConnectionLifetime:
    def test_wait_helpers_receive_fresh_connection_not_callers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn_worker must not hold the caller's conn across the blocking wait.

        Verified by monkeypatching _wait_idle_via_event to capture whatever
        conn it receives, then asserting it is NOT the same object as the
        conn the caller passed in. A fresh short-lived connection is used
        internally so the caller's conn is freed during the up-to-60s wait.
        """
        captured: dict[str, sqlite3.Connection | None] = {"idle": None}

        def fake_wait_idle(conn_, worker_id_, *, target=None):
            captured["idle"] = conn_
            state.record_event(conn_, "session_ready", worker_id=worker_id_)
            return True

        monkeypatch.setattr(spawn, "_wait_idle_via_event", fake_wait_idle)
        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:w1"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        db = tmp_path / "state.db"
        caller_conn = state.connect(db)
        state.init_schema(caller_conn)

        spawn.spawn_worker(
            caller_conn, worker_id="w1", model="sonnet", task="t",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x",
        )

        assert captured["idle"] is not None
        assert captured["idle"] is not caller_conn, (
            "spawn_worker must not pass caller's conn to _wait_idle_via_event"
        )

    def test_caller_conn_still_sees_post_wait_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-wait events written via the internal conn must be visible
        on the caller's conn (WAL + autocommit = shared visibility)."""
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        for fn, retval in [
            ("ensure_session", None), ("new_window", "s:w1"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        db = tmp_path / "state.db"
        caller_conn = state.connect(db)
        state.init_schema(caller_conn)
        spawn.spawn_worker(
            caller_conn, worker_id="w1", model="sonnet", task="t",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x",
        )

        # spawn_ok is written on the internal conn — caller must still see it.
        kinds = [e.kind for e in state.list_events(caller_conn, worker_id="w1")]
        assert "spawn_ok" in kinds
        worker = state.get_worker(caller_conn, "w1")
        assert worker is not None
        assert worker.status == "working"


class TestPermissionsWiring:
    def test_role_with_permissions_writes_settings_when_worktree_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Engineer with a role file carrying permissions gets those perms
        merged into <worktree>/.claude/settings.local.json before tmux opens."""
        from orchestra import spawn, state
        # Set up project root with a custom role file
        project_root = tmp_path / "proj"
        project_root.mkdir()
        roles = project_root / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "  deny:\n"
            "    - Write\n"
            "---\n"
            "## ROLE: Reviewer\nWorker ID: {worker_id}\nWorkspace: {cwd}\n"
            "Branch: {branch}\n{brief_section}"
        )
        # Patch the side-effectful parts of spawn
        worktree_path = project_root / "worktrees" / "rev"
        worktree_path.mkdir(parents=True)
        monkeypatch.setattr(spawn.tmux, "ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "new_window", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_literal", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_enter", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_multiline", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "capture", lambda *a, **k: "")
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn.worktree_mod, "add", lambda *a, **k: worktree_path)

        db = project_root / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="rev",
            model="sonnet",
            task="",
            project_root=str(project_root),
            state_db=db,
            ctx_files=[],
            session_name="orch-test",
            role="reviewer",
            brief=None,
            worktree_name="rev",
        )

        target = project_root / "worktrees" / "rev" / ".claude" / "settings.local.json"
        assert target.is_file()
        data = json.loads(target.read_text())
        assert data["permissions"]["allow"] == ["Read"]
        assert data["permissions"]["deny"] == ["Write"]

    def test_role_without_worktree_writes_to_main_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A no-worktree reviewer-style spawn writes perms into
        <project_root>/.claude/settings.local.json (main checkout)."""
        from orchestra import spawn, state
        project_root = tmp_path / "proj"
        project_root.mkdir()
        roles = project_root / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "---\n"
            "## ROLE: Reviewer\nWorker ID: {worker_id}\nWorkspace: {cwd}\n"
            "Branch: {branch}\n{brief_section}"
        )
        monkeypatch.setattr(spawn.tmux, "ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "new_window", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_literal", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_enter", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_multiline", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "capture", lambda *a, **k: "")
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)

        db = project_root / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="rev",
            model="sonnet",
            task="",
            project_root=str(project_root),
            state_db=db,
            ctx_files=[],
            session_name="orch-test",
            role="reviewer",
            brief=None,
            worktree_name=None,
        )

        target = project_root / ".claude" / "settings.local.json"
        assert target.is_file()
        data = json.loads(target.read_text())
        assert data["permissions"]["allow"] == ["Read"]

    def test_missing_role_marks_worker_error_and_skips_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestra import spawn, state
        project_root = tmp_path / "proj"
        project_root.mkdir()
        new_window_called = {"v": False}
        monkeypatch.setattr(spawn.tmux, "ensure_session", lambda *a, **k: None)

        def _new_window(*a: object, **k: object) -> None:
            new_window_called["v"] = True

        monkeypatch.setattr(spawn.tmux, "new_window", _new_window)

        db = project_root / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="ghost",
            model="sonnet",
            task="",
            project_root=str(project_root),
            state_db=db,
            ctx_files=[],
            session_name="orch-test",
            role="ghost-role-that-does-not-exist",
            brief=None,
            worktree_name=None,
        )

        w = state.get_worker(conn, "ghost")
        assert w is not None and w.status == "error"
        kinds = [e.kind for e in state.list_events(conn, worker_id="ghost")]
        assert "role_load_failed" in kinds
        assert not new_window_called["v"]


class TestMissionIdInheritance:
    """spawn_worker inherits mission_id from the running mission when not supplied."""

    def _simple_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection,
        db: Path, *, worker_id: str, mission_id: int | None = None,
    ) -> None:
        """Helper: stub out all tmux/wait calls and run spawn_worker."""
        for fn, retval in [
            ("ensure_session", None), ("new_window", f"s:{worker_id}"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=retval, **kw: _r)
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        kwargs: dict = dict(
            worker_id=worker_id, model="sonnet", task="t",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x",
        )
        if mission_id is not None:
            kwargs["mission_id"] = mission_id
        spawn.spawn_worker(conn, **kwargs)

    def test_inherits_running_mission_when_mission_id_not_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        mid = state.create_mission(conn, slug="alpha", mission_path="p")

        self._simple_spawn(tmp_path, monkeypatch, conn, db, worker_id="w1")

        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.mission_id == mid
        assert w.branch == "orch/alpha/w1"

    def test_explicit_mission_id_is_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        mid = state.create_mission(conn, slug="beta", mission_path="p")

        self._simple_spawn(tmp_path, monkeypatch, conn, db, worker_id="w1",
                           mission_id=mid)

        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.mission_id == mid

    def test_no_mission_running_keeps_null_and_flat_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no mission exists, mission_id stays NULL and branch is flat."""
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        # No mission created → get_running_mission returns None

        self._simple_spawn(tmp_path, monkeypatch, conn, db, worker_id="w1")

        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.mission_id is None
        assert w.branch == "orch/w1"


class TestExclusiveResource:
    def test_spawn_worker_with_exclusive_resource_records_event(
        self, tmp_db: Path, fake_tmux: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        def fake_wait_idle(
            conn_: sqlite3.Connection, worker_id_: str, *, target: str | None = None
        ) -> bool:
            state.record_event(conn_, "session_ready", worker_id=worker_id_)
            return True

        monkeypatch.setattr(spawn, "_wait_idle_via_event", fake_wait_idle)

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
            exclusive_resource="device1",
        )

        events = state.list_events(conn, worker_id="w1")
        resource_events = [e for e in events if e.kind == "resource_acquired"]
        assert len(resource_events) == 1
        assert resource_events[0].payload.get("resource") == "device1"

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "working"


class TestCustomRoleRendering:
    def test_custom_role_uses_filesystem_template_not_v0_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-pm, non-engineer role like 'reviewer' should render its .md
        template via the filesystem loader, not fall through to v0."""
        from orchestra import spawn
        project_root = tmp_path / "proj"
        project_root.mkdir()
        roles = project_root / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "## ROLE: REVIEWER\n"
            "Worker ID: {worker_id}\n"
            "Workspace: {cwd}\n"
            "Branch: {branch}\n"
            "{brief_section}"
        )

        out = spawn._render_startup_prompt(
            role="reviewer",
            worker_id="r1",
            model="sonnet",
            task="",
            ctx_files=[],
            brief="please review",
            cwd=str(project_root),
            branch="orch/r1",
        )
        assert "## ROLE: REVIEWER" in out
        assert "Worker ID: r1" in out
        assert "Workspace: " in out
        assert "orch/r1" in out
        assert "please review" in out
