"""Worker spawn choreography.

Seven steps:
  1. state.create_worker  (row, status="spawning") + event spawn_start
  2. ensure_session + new_window                   + event spawn_window
  3. send_literal(boot_cmd) + send_enter
  4. poll is_idle (max BOOT_TIMEOUT_S)             + event spawn_idle / spawn_timeout
     on success: double-Enter to clear trust prompt
  5. send_literal("/<model>") + send_enter         + event model_switched
  6. send_multiline(startup_prompt) with 1 retry    + event prompt_injected / failed
  7. poll for first `status` event from worker     + spawn_ok / spawn_first_status_timeout
"""
from __future__ import annotations

import shlex
import sqlite3
import time
from pathlib import Path
from time import monotonic

from orchestra import prompts, state, tmux

# Timeouts are module-level so tests can monkeypatch them.
BOOT_TIMEOUT_S = 60
BOOT_POLL_S = 3.0
FIRST_STATUS_TIMEOUT_S = 90
FIRST_STATUS_POLL_S = 5.0


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


def _wait_idle(
    target: str,
    conn: sqlite3.Connection | None = None,
    worker_id: str | None = None,
) -> bool:
    """Poll until claude is at its main input prompt.

    Also handles claude's first-boot 'trust this folder?' menu: when seen,
    send Enter to accept the default-highlighted 'Yes, I trust' option,
    record a ``spawn_trust_accepted`` event, then keep polling. Without
    this, ``is_idle``'s prompt regex (``❯`` at end-of-line) never matches
    the trust menu's ``❯ 1. Yes, I trust this folder`` line, so the loop
    spins to the deadline.

    The conn/worker_id args are optional so existing callers (and tests
    that pre-date this signature) keep working with no audit-trail entry.
    """
    trust_handled = False
    deadline = monotonic() + BOOT_TIMEOUT_S
    while monotonic() < deadline:
        if tmux.is_idle(target):
            return True
        if not trust_handled:
            cap = tmux.capture(target, lines=30)
            if any(marker in cap for marker in _TRUST_PROMPT_MARKERS):
                tmux.send_enter(target)
                if conn is not None and worker_id is not None:
                    state.record_event(
                        conn, "spawn_trust_accepted", worker_id=worker_id,
                    )
                trust_handled = True
                time.sleep(2.0)
                continue
        time.sleep(BOOT_POLL_S)
    return False


def _wait_first_status(conn: sqlite3.Connection, worker_id: str) -> bool:
    deadline = monotonic() + FIRST_STATUS_TIMEOUT_S
    while monotonic() < deadline:
        for evt in state.list_events(conn, worker_id=worker_id):
            if evt.kind == "status":
                return True
        time.sleep(FIRST_STATUS_POLL_S)
    return False


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
) -> None:
    branch = f"orch/{worker_id}"
    pane_target = f"{session_name}:{worker_id}"

    # Step 1: worker row
    state.create_worker(
        conn, id=worker_id, task=task, model=model,
        branch=branch, pane_target=pane_target,
    )
    state.record_event(conn, "spawn_start", worker_id=worker_id, task=task, model=model)

    # Step 2: tmux session + window
    tmux.ensure_session(session_name, cwd=project_root)
    target = tmux.new_window(session=session_name, name=worker_id, cwd=project_root)
    state.record_event(conn, "spawn_window", worker_id=worker_id, target=target)

    # Step 3: boot claude
    boot_cmd = _boot_command(worker_id, state_db)
    tmux.send_literal(target, boot_cmd)
    tmux.send_enter(target)

    # Step 4: wait for idle. The poll also handles claude's 'trust this folder?'
    # first-boot menu — see _wait_idle for details.
    if not _wait_idle(target, conn=conn, worker_id=worker_id):
        last_screen = tmux.capture(target, lines=20)
        state.record_event(
            conn, "spawn_timeout", worker_id=worker_id, last_screen=last_screen,
        )
        state.update_worker(conn, worker_id, status="error")
        return

    state.record_event(conn, "spawn_idle", worker_id=worker_id)

    # Double-Enter to dismiss any trust/welcome prompt.
    tmux.send_enter(target)
    time.sleep(1.0)
    tmux.send_enter(target)
    time.sleep(1.0)

    # Step 5: switch model
    tmux.send_literal(target, f"/{model}")
    tmux.send_enter(target)
    time.sleep(3.0)
    state.record_event(conn, "model_switched", worker_id=worker_id, model=model)

    # Step 6: inject startup prompt with 1 retry
    startup = prompts.render_startup_prompt(
        worker_id=worker_id, task=task, model=model, ctx_files=ctx_files,
    )
    inject_ok = False
    for attempt in (1, 2):
        try:
            tmux.send_multiline(target, startup, buffer_name=f"orch-{worker_id}")
            inject_ok = True
            break
        except Exception as e:  # noqa: BLE001
            state.record_event(
                conn, "prompt_inject_retry",
                worker_id=worker_id, attempt=attempt, error=repr(e),
            )
            time.sleep(1.0)
    if not inject_ok:
        state.record_event(conn, "prompt_inject_failed", worker_id=worker_id)
        state.update_worker(conn, worker_id, status="error")
        return
    state.record_event(conn, "prompt_injected", worker_id=worker_id)

    # Step 7: wait for first status event from the worker
    if _wait_first_status(conn, worker_id):
        state.record_event(conn, "spawn_ok", worker_id=worker_id)
        state.update_worker(conn, worker_id, status="working")
    else:
        state.update_worker(conn, worker_id, status="stale_spawn")
        state.record_event(conn, "spawn_first_status_timeout", worker_id=worker_id)
