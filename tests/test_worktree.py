"""Tests for orchestra.worktree (git worktree helpers)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from orchestra import worktree


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
