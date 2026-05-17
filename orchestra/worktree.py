"""git worktree add/remove helpers for orchestra engineers."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def add(project_root: Path, *, name: str, worker_id: str) -> Path:
    """Ensure a worktree exists at <root>/worktrees/<name> on branch orch/<worker_id>.

    Idempotent: if the directory exists, returns it without reinitialising.
    """
    wt_path = project_root / "worktrees" / name
    if wt_path.exists():
        return wt_path
    branch = f"orch/{worker_id}"
    # If the branch already exists (e.g. from a prior partial run), reuse it.
    existing = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--list", branch],
        capture_output=True, text=True, check=True,
    ).stdout
    args = ["worktree", "add"]
    if branch in existing:
        args += [str(wt_path), branch]
    else:
        args += ["-b", branch, str(wt_path), "HEAD"]
    _git(project_root, *args)
    return wt_path


def remove(project_root: Path, *, name: str, worker_id: str) -> None:
    """Remove the worktree and delete its branch. Tolerates already-missing."""
    wt_path = project_root / "worktrees" / name
    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(wt_path)],
            check=False,
        )
    subprocess.run(
        ["git", "-C", str(project_root), "branch", "-D", f"orch/{worker_id}"],
        check=False, capture_output=True,
    )
