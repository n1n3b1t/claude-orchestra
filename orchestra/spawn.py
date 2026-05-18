"""Worker spawn choreography (v1).

v1 changes vs v0:
- The polling wait `_wait_idle` is replaced by a hook-event wait: spawn
  watches state.db for the `session_ready` event for the worker_id, max
  BOOT_TIMEOUT_S seconds. `session_ready` is the sole proof-of-life signal —
  there is no second wait on `turn_complete`, because for engineers doing
  autonomous multi-step work, `Stop` only fires when the whole task is done.
- The trust-prompt dismissal still runs in parallel (capture-pane based)
  because it fires BEFORE Claude reaches the point of emitting SessionStart.
- New kwargs: role ('engineer'|'pm'|None), brief (markdown body for
  engineers; mission body for PMs — caller chooses what to inject),
  worktree_name (when set, a git worktree is created under
  <project_root>/worktrees/<name> on branch orch/<worker_id>, and the
  spawn uses that path as cwd).
- When role is set, role_prompts.render_pm_prompt /
  render_engineer_prompt is used instead of the v0 single-role template.

Six steps:
  1. (optional) worktree creation                    + event worktree_created
  2. state.create_worker  (row, status="spawning")   + event spawn_start
  3. ensure_session + new_window                     + event spawn_window
  4. send_literal(boot_cmd) + send_enter
  5. wait for session_ready event (max BOOT_TIMEOUT_S) + event spawn_idle / spawn_timeout
     trust-prompt dismissal runs in parallel (capture-pane)
  6. send_literal("/<model>") + send_enter           + event model_switched
  7. send_multiline(startup_prompt) with 1 retry     + event prompt_injected / failed
     followed by `spawn_ok` and status=working.
"""
from __future__ import annotations

import shlex
import sqlite3
import time
from pathlib import Path
from time import monotonic

from orchestra import prompts, role_prompts, settings_merge, state, tmux
from orchestra import worktree as worktree_mod

# Timeouts are module-level so tests can monkeypatch them.
BOOT_TIMEOUT_S = 60
BOOT_POLL_S = 1.0


def _boot_command(worker_id: str, state_db: Path) -> str:
    return (
        f"ORCHESTRA_WORKER_ID={shlex.quote(worker_id)} "
        f"ORCHESTRA_STATE_DB={shlex.quote(str(state_db))} "
        f"claude --dangerously-skip-permissions"
    )


_TRUST_PROMPT_MARKERS = (
    "trust this folder",
    "Is this a project you created",
    "Yes, I trust",
)


def _has_event(
    conn: sqlite3.Connection, *, worker_id: str, kind: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE worker_id = ? AND kind = ? LIMIT 1",
        (worker_id, kind),
    ).fetchone()
    return row is not None


def _wait_idle_via_event(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    target: str | None = None,
) -> bool:
    """Block until a session_ready event appears, OR the trust prompt needs
    dismissing, OR BOOT_TIMEOUT_S elapses.

    If ``target`` is provided, we capture the pane in parallel to dismiss the
    trust prompt (it fires before SessionStart, so we can't rely on hooks).
    Records a ``spawn_trust_accepted`` event when the trust prompt is dismissed.
    """
    trust_handled = False
    deadline = monotonic() + BOOT_TIMEOUT_S
    while monotonic() < deadline:
        if _has_event(conn, worker_id=worker_id, kind="session_ready"):
            return True
        if target is not None and not trust_handled:
            cap = tmux.capture(target, lines=30)
            if any(marker in cap for marker in _TRUST_PROMPT_MARKERS):
                tmux.send_enter(target)
                state.record_event(conn, "spawn_trust_accepted", worker_id=worker_id)
                trust_handled = True
                time.sleep(2.0)
                time.sleep(BOOT_POLL_S)
                continue
        time.sleep(BOOT_POLL_S)
    return False


def _render_startup_prompt(
    *,
    role: str | None,
    worker_id: str,
    model: str,
    task: str,
    ctx_files: list[str],
    brief: str | None,
    cwd: str,
    branch: str,
    project_root: str | None = None,
) -> str:
    """Select PM / Engineer / v0 prompt renderer based on `role`.

    ``cwd`` is the working directory for the worker (the worktree path for
    engineers, the project root for PMs and custom roles). ``project_root``
    is the canonical project root used for role-template lookup; it defaults
    to ``cwd`` when not supplied (correct for non-worktree workers).
    """
    if role == "pm":
        return role_prompts.render_pm_prompt(
            mission=brief or task,
            worker_id=worker_id,
            project_name=Path(cwd).name,
            # Engineer team is conveyed via the mission file; this argument
            # is only used by callers that programmatically build a team.
            # Pass an empty list to omit the section.
            engineer_specs=[],
            verifier_block="(see mission for verifier)",
        )
    if role is not None:
        # v2.0: any non-pm role renders via the filesystem loader with
        # engineer-shape variables (worker_id, cwd, branch, brief_section).
        # Template lookup uses project_root so user overrides in
        # <project_root>/.orchestra/roles/<name>.md are found even when the
        # worker's cwd is a worktree subdirectory.
        if brief is not None:
            brief_section = "### YOUR BRIEF\n" f"{brief}\n"
        else:
            brief_section = (
                "### YOUR BRIEF\n(none — wait for `orchestra send` instructions)\n"
            )
        return role_prompts.render_role(
            role,
            project_root=Path(project_root or cwd),
            worker_id=worker_id,
            cwd=cwd,
            branch=branch,
            brief_section=brief_section,
        )
    # v0 fallback — no role set
    return prompts.render_startup_prompt(
        worker_id=worker_id, task=task, model=model, ctx_files=ctx_files,
    )


def spawn_worker(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    model: str,
    task: str,
    project_root: str,
    state_db: Path,
    ctx_files: list[str],
    session_name: str,
    role: str | None = None,
    brief: str | None = None,
    worktree_name: str | None = None,
) -> None:
    branch = f"orch/{worker_id}"
    pane_target = f"{session_name}:{worker_id}"

    # Step 1: worker row — created before any external operations so that
    # failures always have an audit trail. Uses the caller's conn.
    state.create_worker(
        conn, id=worker_id, task=task, model=model,
        branch=branch, pane_target=pane_target,
        role=role or "engineer",
        worktree=worktree_name,
    )
    state.record_event(
        conn, "spawn_start", worker_id=worker_id, task=task, model=model,
        role=role, worktree=worktree_name,
    )

    # Pre-step: worktree (engineers only — PMs work in the main checkout).
    cwd = project_root
    if worktree_name is not None:
        try:
            wt = worktree_mod.add(Path(project_root), name=worktree_name, worker_id=worker_id)
            cwd = str(wt)
            state.record_event(
                conn, "worktree_created", worker_id=worker_id,
                name=worktree_name, path=str(wt),
            )
        except Exception as e:  # noqa: BLE001
            state.record_event(
                conn, "worktree_failed", worker_id=worker_id,
                name=worktree_name, error=repr(e),
            )
            state.update_worker(conn, worker_id, status="error")
            return

    # v2.0: load role file & merge per-role permissions before opening the window.
    if role is not None:
        try:
            _, role_perms = role_prompts._load_role(role, project_root=Path(project_root))
        except role_prompts.RoleNotFoundError as e:
            state.record_event(
                conn, "role_load_failed", worker_id=worker_id, error=str(e),
            )
            state.update_worker(conn, worker_id, status="error")
            return
        if role_perms:
            if worktree_name is not None:
                settings_path = (
                    Path(project_root) / "worktrees" / worktree_name
                    / ".claude" / "settings.local.json"
                )
            else:
                settings_path = (
                    Path(project_root) / ".claude" / "settings.local.json"
                )
            settings_merge.ensure_perms(settings_path, role_perms)

    # Step 2: tmux session + window
    tmux.ensure_session(session_name, cwd=cwd)
    target = tmux.new_window(session=session_name, name=worker_id, cwd=cwd)
    state.record_event(conn, "spawn_window", worker_id=worker_id, target=target)

    # Step 3: boot claude
    boot_cmd = _boot_command(worker_id, state_db)
    tmux.send_literal(target, boot_cmd)
    tmux.send_enter(target)

    # Switch to a fresh short-lived connection for the blocking wait window
    # and post-wait writes. The caller's `conn` is intentionally not held
    # across the up-to-BOOT_TIMEOUT_S wait in _wait_idle_via_event — keeps
    # spawn_worker from pinning the caller's connection during long blocking
    # poll loops.
    wait_conn = state.connect(state_db)
    try:
        # Step 4: wait for SessionStart hook (session_ready event). Trust-prompt
        # dismissal runs in parallel because it fires BEFORE SessionStart.
        if not _wait_idle_via_event(wait_conn, worker_id, target=target):
            last_screen = tmux.capture(target, lines=20)
            state.record_event(
                wait_conn, "spawn_stale_idle", worker_id=worker_id,
                last_screen=last_screen,
            )
            state.update_worker(wait_conn, worker_id, status="stale_spawn")
            # Continue anyway — Claude may still come up; PM can send a kickoff
            # message if needed. The final spawn_ok write below will flip
            # status to working if the rest of the flow succeeds.
        else:
            state.record_event(wait_conn, "spawn_idle", worker_id=worker_id)

        # Step 5: switch model
        tmux.send_literal(target, f"/{model}")
        tmux.send_enter(target)
        time.sleep(3.0)
        state.record_event(
            wait_conn, "model_switched", worker_id=worker_id, model=model,
        )

        # Step 6: inject startup prompt with 1 retry
        startup = _render_startup_prompt(
            role=role, worker_id=worker_id, model=model, task=task,
            ctx_files=ctx_files, brief=brief, cwd=cwd, branch=branch,
            project_root=project_root,
        )
        inject_ok = False
        for attempt in (1, 2):
            try:
                tmux.send_multiline(target, startup, buffer_name=f"orch-{worker_id}")
                inject_ok = True
                break
            except Exception as e:  # noqa: BLE001
                state.record_event(
                    wait_conn, "prompt_inject_retry",
                    worker_id=worker_id, attempt=attempt, error=repr(e),
                )
                time.sleep(1.0)
        if not inject_ok:
            state.record_event(
                wait_conn, "prompt_inject_failed", worker_id=worker_id,
            )
            state.update_worker(wait_conn, worker_id, status="error")
            return
        state.record_event(wait_conn, "prompt_injected", worker_id=worker_id)

        # Step 7: session_ready already fired (step 5); no second proof-of-life wait.
        state.record_event(wait_conn, "spawn_ok", worker_id=worker_id)
        state.update_worker(wait_conn, worker_id, status="working")
    finally:
        wait_conn.close()
