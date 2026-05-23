"""Tests for orchestra.worktree (git worktree helpers)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestra import state, worktree
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


class TestNamespacedWorktree:
    def test_add_uses_mission_slug(self, tmp_path: Path) -> None:
        """add() with mission_slug creates worktrees/<slug>/<name> on orch/<slug>/<id>."""
        _init_repo(tmp_path)
        path = worktree.add(tmp_path, name="backend", worker_id="backend",
                            mission_slug="alpha")
        assert path == tmp_path / "worktrees" / "alpha" / "backend"
        assert path.exists()
        branches = subprocess.run(
            ["git", "-C", str(tmp_path), "branch"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/alpha/backend" in branches

    def test_remove_uses_mission_slug(self, tmp_path: Path) -> None:
        """remove() with mission_slug tears down the namespaced worktree + branch."""
        _init_repo(tmp_path)
        worktree.add(tmp_path, name="backend", worker_id="backend",
                     mission_slug="alpha")
        worktree.remove(tmp_path, name="backend", worker_id="backend",
                        mission_slug="alpha")
        assert not (tmp_path / "worktrees" / "alpha" / "backend").exists()
        branches = subprocess.run(
            ["git", "-C", str(tmp_path), "branch"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/alpha/backend" not in branches

    def test_add_legacy_flat_layout_when_no_mission(self, tmp_path: Path) -> None:
        """When no mission is running and no slug passed, use legacy flat layout."""
        _init_repo(tmp_path)
        # No .orchestra/state.db → _resolve_mission_slug returns None → flat layout.
        path = worktree.add(tmp_path, name="backend", worker_id="backend")
        assert path == tmp_path / "worktrees" / "backend"
        assert path.exists()
        branches = subprocess.run(
            ["git", "-C", str(tmp_path), "branch"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/backend" in branches

    def test_add_reads_running_mission_from_state_db(self, tmp_path: Path) -> None:
        """When no slug is passed, add() reads the running mission from state.db."""
        _init_repo(tmp_path)
        # Create a .orchestra/state.db with a running mission.
        orch_dir = tmp_path / ".orchestra"
        orch_dir.mkdir()
        db_path = orch_dir / "state.db"
        conn = state.connect(db_path)
        state.init_schema(conn)
        state.create_mission(conn, slug="beta", mission_path="/tmp/m.md")
        conn.close()

        path = worktree.add(tmp_path, name="frontend", worker_id="frontend")
        assert path == tmp_path / "worktrees" / "beta" / "frontend"
        assert path.exists()
        branches = subprocess.run(
            ["git", "-C", str(tmp_path), "branch"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/beta/frontend" in branches
