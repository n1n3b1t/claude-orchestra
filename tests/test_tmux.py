from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from orchestra import tmux


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch):
    """Stub subprocess.run; record all calls."""
    runner = MagicMock(spec=subprocess.run)
    runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


class TestSend:
    def test_send_literal(self, fake_run):
        tmux.send_literal("s:1", "hello")
        fake_run.assert_called_once_with(
            ["tmux", "send-keys", "-t", "s:1", "-l", "hello"],
            capture_output=True, text=True,
        )

    def test_send_enter(self, fake_run):
        tmux.send_enter("s:1")
        fake_run.assert_called_once_with(
            ["tmux", "send-keys", "-t", "s:1", "Enter"],
            capture_output=True, text=True,
        )

    def test_send_ctrl_c(self, fake_run):
        tmux.send_ctrl_c("s:1")
        fake_run.assert_called_once_with(
            ["tmux", "send-keys", "-t", "s:1", "C-c"],
            capture_output=True, text=True,
        )

    def test_send_multiline_uses_load_paste_then_enter(self, fake_run):
        tmux.send_multiline("s:1", "line1\nline2", buffer_name="b1")
        # Three calls in order: load-buffer, paste-buffer, send-keys Enter
        calls = fake_run.call_args_list
        assert len(calls) == 3
        load_args, load_kwargs = calls[0]
        assert load_args[0] == ["tmux", "load-buffer", "-b", "b1", "-"]
        assert load_kwargs.get("input") == "line1\nline2"
        paste_args, _ = calls[1]
        assert paste_args[0] == ["tmux", "paste-buffer", "-p", "-d", "-b", "b1", "-t", "s:1"]
        enter_args, _ = calls[2]
        assert enter_args[0] == ["tmux", "send-keys", "-t", "s:1", "Enter"]

    def test_send_multiline_default_buffer_name_derived_from_target(self, fake_run):
        tmux.send_multiline("orch-proj:w1", "hi")
        # The first call (load-buffer) should have the derived buffer name
        load_args, _ = fake_run.call_args_list[0]
        assert load_args[0][3] == "orch_orch_proj_w1"


class TestCapture:
    def test_capture_strips_ansi(self, monkeypatch):
        # raw output with ANSI: red, OSC, charset switch
        raw = "\x1b[31mhello\x1b[0m\n\x1b]0;title\x07world\n\x1b(Bplain\n"
        runner = MagicMock()
        runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=raw, stderr=""
        )
        monkeypatch.setattr(subprocess, "run", runner)

        out = tmux.capture("s:1", lines=80)
        assert "hello" in out and "\x1b" not in out
        assert "world" in out
        assert "plain" in out
        # argv shape
        called = runner.call_args.args[0]
        assert called == ["tmux", "capture-pane", "-t", "s:1", "-p", "-S", "-80"]


class TestIdle:
    def test_busy_on_spinner(self, monkeypatch):
        monkeypatch.setattr(tmux, "capture", lambda target, lines=12: "Running tests...\n❯ ")
        assert tmux.is_idle("s:1") is False

    def test_idle_on_prompt_only(self, monkeypatch):
        monkeypatch.setattr(tmux, "capture", lambda target, lines=12: "some output\n❯ ")
        assert tmux.is_idle("s:1") is True

    def test_unknown_state_returns_false(self, monkeypatch):
        monkeypatch.setattr(tmux, "capture", lambda target, lines=12: "blah blah\n")
        assert tmux.is_idle("s:1") is False


class TestPaneCommand:
    def test_pane_current_command(self, monkeypatch):
        runner = MagicMock()
        runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="claude\n", stderr=""
        )
        monkeypatch.setattr(subprocess, "run", runner)
        assert tmux.pane_current_command("s:1") == "claude"
        called = runner.call_args.args[0]
        assert called == ["tmux", "display-message", "-p", "-t", "s:1", "#{pane_current_command}"]


class TestSession:
    def test_ensure_session_creates_when_missing(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["tmux", "has-session"]:
                # session not found -> non-zero
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake)
        tmux.ensure_session("orch-x", cwd="/tmp")
        assert calls[0] == ["tmux", "has-session", "-t", "orch-x"]
        assert calls[1] == ["tmux", "new-session", "-d", "-s", "orch-x", "-c", "/tmp"]

    def test_ensure_session_skips_when_present(self, monkeypatch):
        runner = MagicMock()
        runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        monkeypatch.setattr(subprocess, "run", runner)
        tmux.ensure_session("orch-x", cwd="/tmp")
        runner.assert_called_once()
        assert runner.call_args.args[0] == ["tmux", "has-session", "-t", "orch-x"]

    def test_new_window_returns_target(self, fake_run):
        target = tmux.new_window(session="orch-x", name="w1", cwd="/tmp")
        assert target == "orch-x:w1"
        fake_run.assert_called_once_with(
            ["tmux", "new-window", "-t", "orch-x:", "-n", "w1", "-c", "/tmp"],
            capture_output=True, text=True,
        )


class TestKillWindow:
    def test_kill_window_calls_correct_argv(self, fake_run):
        tmux.kill_window("s:1")
        fake_run.assert_called_once_with(
            ["tmux", "kill-window", "-t", "s:1"],
            capture_output=True, text=True,
        )

    def test_kill_window_tolerates_missing(self, monkeypatch):
        import subprocess
        def boom(argv, **kw):
            raise subprocess.CalledProcessError(1, argv)
        monkeypatch.setattr(subprocess, "run", boom)
        # Must not raise
        tmux.kill_window("s:nonexistent")
