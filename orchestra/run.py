"""orchestra run — one-shot dogfood runner.

Spawns a PM with the given mission and blocks on the project state.db,
streaming events to stdout until the PM emits `worker_done` or a watchdog
fires.

Exit codes:
    0   PM emitted `worker_done`.
    2   pre-flight failure (dirty repo, missing mission, missing .orchestra,
        PM worker row already exists).
  124   wall-clock watchdog fired.
  125   activity watchdog (no new events) fired.
  126   loop exited without a terminal signal (defensive).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from orchestra import spawn, state

POLL_INTERVAL_S = 2.0


def _is_git_repo(cwd: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _is_clean_tree(cwd: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == ""


def _summarize_payload(payload_json: str, max_chars: int = 120) -> str:
    if not payload_json:
        return ""
    try:
        # Compact-dump to drop whitespace from the stored row.
        compact = json.dumps(json.loads(payload_json), separators=(",", ":"))
    except (ValueError, TypeError):
        compact = payload_json
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1] + "…"


def run_mission(
    mission_path: Path,
    *,
    model: str = "opus",
    max_wallclock: float = 5400.0,
    max_activity: float = 600.0,
    allow_dirty: bool = False,
) -> int:
    """Spawn a PM with the given mission and block until completion or watchdog.

    Returns an exit code (see module docstring).
    """
    cwd = Path.cwd()

    # ---- Pre-flight: git repo + clean tree ----
    if not _is_git_repo(cwd):
        print(f"[runner] error: {cwd} is not a git repository", flush=True)
        return 2
    if not allow_dirty and not _is_clean_tree(cwd):
        print(
            "[runner] error: working tree is dirty; commit/stash or pass --allow-dirty",
            flush=True,
        )
        return 2

    # ---- Pre-flight: mission file ----
    mission_path = Path(mission_path)
    if not mission_path.exists():
        print(f"[runner] error: mission file not found: {mission_path}", flush=True)
        return 2
    mission_body = mission_path.read_text()

    # ---- Pre-flight: .orchestra exists ----
    orch_dir = cwd / ".orchestra"
    state_db_path = orch_dir / "state.db"
    if not state_db_path.exists():
        print("[runner] error: run `orchestra init` first", flush=True)
        return 2

    # ---- Pre-flight: no existing pm row ----
    conn = state.connect(state_db_path)
    try:
        existing = state.get_worker(conn, "pm")
    finally:
        conn.close()
    if existing is not None:
        print(
            "[runner] error: a worker row for 'pm' already exists; "
            "clean up state.db (or remove the row) and retry",
            flush=True,
        )
        return 2

    # ---- Compute session name (mirror cli._session_name_for) ----
    from orchestra.cli import _session_name_for
    session_name = _session_name_for(cwd)

    # ---- PATH manipulation: prepend <cwd>/.venv/bin so the spawned PM can
    # find `orchestra` from inside its tmux pane. tmux inherits the calling
    # process env, so we mutate os.environ for the spawn call and restore
    # in a try/finally — no permanent env leak.
    venv_bin = cwd / ".venv" / "bin"
    saved_path = os.environ.get("PATH", "")
    if (venv_bin / "orchestra").exists():
        os.environ["PATH"] = f"{venv_bin}:{saved_path}"

    # ---- Spawn the PM ----
    spawn_conn = state.connect(state_db_path)
    try:
        spawn.spawn_worker(
            spawn_conn,
            worker_id="pm",
            model=model,
            task="",
            project_root=str(cwd),
            state_db=state_db_path,
            ctx_files=[],
            session_name=session_name,
            role="pm",
            brief=mission_body,
            worktree_name=None,
        )
    finally:
        spawn_conn.close()
        os.environ["PATH"] = saved_path

    # ---- Polling loop ----
    wallclock_start = time.monotonic()
    last_event_at = time.monotonic()
    cursor = 0

    poll_conn = state.connect(state_db_path)
    try:
        while True:
            rows = poll_conn.execute(
                "SELECT id, worker_id, kind, payload FROM events "
                "WHERE id > ? ORDER BY id ASC",
                (cursor,),
            ).fetchall()
            if rows:
                last_event_at = time.monotonic()
                for r in rows:
                    eid, wid, kind, payload = r["id"], r["worker_id"], r["kind"], r["payload"]
                    summary = _summarize_payload(payload or "")
                    print(f"[{wid}] {kind} {summary}".rstrip(), flush=True)
                    cursor = max(cursor, int(eid))
                    if wid == "pm" and kind == "worker_done":
                        print("[runner] pm worker_done — exiting", flush=True)
                        return 0

            now = time.monotonic()
            if now - wallclock_start > max_wallclock:
                elapsed = int(now - wallclock_start)
                print(f"[runner] wallclock_timeout after {elapsed}s", flush=True)
                return 124
            if now - last_event_at > max_activity:
                elapsed = int(now - last_event_at)
                print(f"[runner] activity_timeout after {elapsed}s", flush=True)
                return 125

            time.sleep(POLL_INTERVAL_S)
    finally:
        poll_conn.close()

    # Defensive: the loop has no break, but mypy + tests can hit this.
    return 126  # pragma: no cover
