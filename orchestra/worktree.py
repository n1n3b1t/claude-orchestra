"""git worktree add/remove helpers for orchestra engineers."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _resolve_mission_slug(project_root: Path, mission_slug: str | None) -> str | None:
    """Return mission_slug if supplied; otherwise look up the running mission from state.db."""
    if mission_slug is not None:
        return mission_slug
    from orchestra import state
    db = project_root / ".orchestra" / "state.db"
    if not db.exists():
        return None
    conn = state.connect(db)
    try:
        m = state.get_running_mission(conn)
    finally:
        conn.close()
    return m.slug if m else None


def add(
    project_root: Path,
    *,
    name: str,
    worker_id: str,
    mission_slug: str | None = None,
) -> Path:
    """Ensure a worktree on branch orch/<slug>/<worker_id> at worktrees/<slug>/<name>.

    Falls back to legacy flat layout (worktrees/<name> on orch/<worker_id>) when
    no mission is running and no slug is passed. Idempotent: returns the path if
    it already exists.

    Also installs Claude Code hooks into the worktree's own
    .claude/settings.local.json so that Claude Code launched inside the
    worktree sub-directory can find them (worktrees have a .git FILE not a
    .git/ dir, so Claude Code's project-root detection stops at the worktree
    boundary and finds no hooks unless we install them here).
    """
    from orchestra import settings_merge  # local import to keep add() cheap

    slug = _resolve_mission_slug(project_root, mission_slug)
    if slug is None:
        # Legacy flat layout
        wt_path = project_root / "worktrees" / name
        branch = f"orch/{worker_id}"
    else:
        wt_path = project_root / "worktrees" / slug / name
        branch = f"orch/{slug}/{worker_id}"

    if wt_path.exists():
        return wt_path

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # If the branch already exists (e.g. from a prior partial run), reuse it.
    existing = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--list", branch],
        capture_output=True, text=True, check=True,
    ).stdout
    args = ["worktree", "add"]
    if existing.strip():
        args += [str(wt_path), branch]
    else:
        args += ["-b", branch, str(wt_path), "HEAD"]
    _git(project_root, *args)

    # Install hooks so Claude Code finds them when running inside the worktree.
    settings_merge.ensure_hooks(wt_path / ".claude" / "settings.local.json")
    return wt_path


def remove(
    project_root: Path,
    *,
    name: str,
    worker_id: str,
    mission_slug: str | None = None,
) -> None:
    """Remove the worktree and delete its branch. Tolerates already-missing."""
    slug = _resolve_mission_slug(project_root, mission_slug)
    if slug is None:
        wt_path = project_root / "worktrees" / name
        branch = f"orch/{worker_id}"
    else:
        wt_path = project_root / "worktrees" / slug / name
        branch = f"orch/{slug}/{worker_id}"

    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(wt_path)],
            check=False,
        )
    subprocess.run(
        ["git", "-C", str(project_root), "branch", "-D", branch],
        check=False, capture_output=True,
    )
