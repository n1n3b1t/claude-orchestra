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
import re as _re
import subprocess
import time
from pathlib import Path

from orchestra import spawn, state

POLL_INTERVAL_S = 2.0

_MISSION_PATH_RE = _re.compile(r"^missions/(?P<slug>[a-z0-9][a-z0-9-]*)/mission\.md$")


def _resolve_slug(mission_path: Path, state_db_path: Path) -> str:
    rel = str(mission_path)
    m = _MISSION_PATH_RE.match(rel)
    if m is not None:
        return m.group("slug")
    import datetime as dt
    base = "m-" + dt.datetime.utcnow().strftime("%Y%m%d-%H%M")
    conn = state.connect(state_db_path)
    try:
        candidate = base
        n = 2
        while state.get_mission_by_slug(conn, candidate) is not None:
            candidate = f"{base}-{n}"
            n += 1
        return candidate
    finally:
        conn.close()


def _close_mission(
    state_db_path: Path, mission_id: int, *, status: str, exit_code: int
) -> None:
    conn = state.connect(state_db_path)
    try:
        state.update_mission(
            conn, mission_id,
            status=status, exit_code=exit_code, ended_at=state.now_iso(),
        )
    finally:
        conn.close()


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

    # ---- Pre-flight: sequential gate (no other mission already running) ----
    seq_conn = state.connect(state_db_path)
    try:
        existing = state.get_running_mission(seq_conn)
    finally:
        seq_conn.close()
    if existing is not None:
        print(
            f"[runner] error: mission {existing.slug!r} is still running; "
            "finish or abort it first",
            flush=True,
        )
        return 2

    # ---- Pre-flight: optional pre-run.sh hook ----
    pre_run = orch_dir / "pre-run.sh"
    if pre_run.exists() and os.access(pre_run, os.X_OK):
        print(f"[runner] executing {pre_run}", flush=True)
        proc = subprocess.run([str(pre_run)], check=False)
        if proc.returncode != 0:
            print(
                f"[runner] error: pre-run.sh exited {proc.returncode}; aborting",
                flush=True,
            )
            return 2
    elif pre_run.exists():
        # File present but not executable — warn and proceed (no-op).
        print(
            f"[runner] warning: {pre_run} exists but is not executable; skipping",
            flush=True,
        )

    # ---- Slug resolution ----
    mission_slug = _resolve_slug(mission_path, state_db_path)

    # ---- Create mission row (status='running') ----
    try:
        relative = str(mission_path.relative_to(cwd))
    except ValueError:
        relative = str(mission_path)
    mc = state.connect(state_db_path)
    try:
        mission_id = state.create_mission(
            mc, slug=mission_slug, mission_path=relative,
        )
    finally:
        mc.close()

    # ---- Pre-flight: no existing pm row FOR THIS MISSION ----
    conn = state.connect(state_db_path)
    try:
        existing_pm = conn.execute(
            "SELECT id FROM workers WHERE id='pm' AND mission_id = ?",
            (mission_id,),
        ).fetchone()
    finally:
        conn.close()
    if existing_pm is not None:
        # Cannot happen in normal flow; defensive guard.
        print("[runner] error: pm row already exists for this mission", flush=True)
        # Mark mission failed before returning
        _close_mission(state_db_path, mission_id, status="failed", exit_code=2)
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
            mission_id=mission_id,
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
                        _close_mission(state_db_path, mission_id, status="done", exit_code=0)
                        return 0

            now = time.monotonic()
            if now - wallclock_start > max_wallclock:
                elapsed = int(now - wallclock_start)
                print(f"[runner] wallclock_timeout after {elapsed}s", flush=True)
                _close_mission(state_db_path, mission_id, status="failed", exit_code=124)
                return 124
            if now - last_event_at > max_activity:
                elapsed = int(now - last_event_at)
                print(f"[runner] activity_timeout after {elapsed}s", flush=True)
                _close_mission(state_db_path, mission_id, status="failed", exit_code=125)
                return 125

            time.sleep(POLL_INTERVAL_S)
    finally:
        poll_conn.close()

    # Defensive: the loop has no break, but mypy + tests can hit this.
    return 126  # pragma: no cover
