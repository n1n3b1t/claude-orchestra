"""Tests for orchestra.worktree (git worktree helpers)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestra import worktree
from orchestra.settings_merge import HOOK_MARKER


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)


class TestAddRemove:
    def test_add_creates_worktree_and_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = worktree.add(tmp_path, name="backend", worker_id="backend")
        assert wt.exists()
        assert (wt / "README.md").exists()
        # Branch should exist:
        out = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", "orch/backend"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/backend" in out

    def test_add_idempotent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt1 = worktree.add(tmp_path, name="backend", worker_id="backend")
        wt2 = worktree.add(tmp_path, name="backend", worker_id="backend")
        assert wt1 == wt2

    def test_remove(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = worktree.add(tmp_path, name="backend", worker_id="backend")
        worktree.remove(tmp_path, name="backend", worker_id="backend")
        assert not wt.exists()
        out = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", "orch/backend"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/backend" not in out

    def test_add_installs_hooks_in_worktree(self, tmp_path: Path) -> None:
        """add() must create .claude/settings.local.json with canonical hooks."""
        _init_repo(tmp_path)
        wt = worktree.add(tmp_path, name="backend", worker_id="backend")
        settings_file = wt / ".claude" / "settings.local.json"
        assert settings_file.exists(), ".claude/settings.local.json must be created"
        data = json.loads(settings_file.read_text())
        hooks = data.get("hooks", {})
        # At minimum SessionStart and Stop must carry our hook marker.
        for event in ("SessionStart", "Stop"):
            assert event in hooks, f"hooks[{event!r}] missing"
            entries = hooks[event]
            commands = [
                h.get("command", "")
                for entry in entries
                for h in entry.get("hooks", [])
            ]
            assert any(HOOK_MARKER in cmd for cmd in commands), (
                f"No orchestra hook command found for {event} in {commands}"
            )
