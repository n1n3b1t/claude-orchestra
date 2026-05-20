"""Typer commands for orchestra."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from orchestra import spawn, state, tmux

app = typer.Typer(help="Orchestra commands.")
worker_app = typer.Typer(help="Commands invoked by workers from inside their panes.")
app.add_typer(worker_app, name="worker")

ORCH_DIR_NAME = ".orchestra"
DEFAULT_CONFIG = """# claude-orchestra config
[intervals]
poll_seconds = 2

[dashboard]
host = "127.0.0.1"
port = 8765
"""


def _orch_dir(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ORCH_DIR_NAME


def _state_db(cwd: Path | None = None) -> Path:
    return _orch_dir(cwd) / "state.db"


def _session_name_for(cwd: Path) -> str:
    """tmux session name derived from cwd basename.

    Sanitizes to ``[a-z0-9-]`` so the dot in ``mktemp -d``-style basenames
    (e.g. ``tmp.UUR8ZsRLFe``) does not collide with tmux's ``session.window``
    target syntax. Falls back to ``orch`` if nothing useful remains.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", cwd.name.lower()).strip("-")
    return f"orch-{base}" if base else "orch"


def _require_initialized() -> Path:
    db = _state_db()
    if not db.exists():
        typer.echo("error: run `orchestra init` first", err=True)
        raise typer.Exit(2)
    return db


@contextmanager
def _open_db() -> Generator[sqlite3.Connection, None, None]:
    db = _require_initialized()
    conn = state.connect(db)
    try:
        yield conn
    finally:
        conn.close()


@app.command()
def init() -> None:
    """Initialize .orchestra/ in the current directory and install hooks."""
    cwd = Path.cwd()
    d = _orch_dir()
    d.mkdir(exist_ok=True)
    db = d / "state.db"
    conn = state.connect(db)
    state.init_schema(conn)
    conn.close()
    cfg = d / "config.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG)
    # Install Claude Code hooks (idempotent).
    from orchestra import settings_merge  # local import to keep init cheap
    settings_merge.ensure_hooks(cwd / ".claude" / "settings.local.json")
    typer.echo(f"initialized {d}")


@app.command("spawn")
def spawn_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
    model: str = typer.Argument(..., metavar="MODEL"),
    task: str = typer.Argument("", metavar="TASK"),
    context: list[str] = typer.Option(  # noqa: B008
        [], "--context", help="Context files."
    ),
    role: str = typer.Option("engineer", "--role"),
    brief: Path | None = typer.Option(None, "--brief"),  # noqa: B008
    worktree_name: str | None = typer.Option(None, "--worktree"),  # noqa: B008
) -> None:
    """Spawn a worker into a new tmux window."""
    project_root = str(Path.cwd())
    session_name = _session_name_for(Path.cwd())
    brief_text = brief.read_text() if brief else None
    with _open_db() as conn:
        db = _state_db()
        spawn.spawn_worker(
            conn,
            worker_id=worker_id,
            model=model,
            task=task,
            project_root=project_root,
            state_db=db,
            ctx_files=list(context),
            session_name=session_name,
            role=role,
            brief=brief_text,
            worktree_name=worktree_name,
        )
        typer.echo(f"spawn {worker_id} → {session_name}:{worker_id}")


@app.command()
def status(worker: str | None = typer.Option(None, "--worker")) -> None:
    """Print worker status table; with --worker, print detail."""
    with _open_db() as conn:
        if worker:
            w = state.get_worker(conn, worker)
            if w is None:
                typer.echo(f"no such worker: {worker}", err=True)
                raise typer.Exit(2)
            typer.echo(
                f"{w.id}  {w.status}  turns={w.turns}  branch={w.branch}\n"
                f"  task: {w.task}\n  progress: {w.progress}"
            )
            typer.echo("\nrecent events:")
            for e in state.list_events(conn, worker_id=worker)[-20:]:
                typer.echo(f"  {e.ts}  {e.kind}  {e.payload}")
        else:
            rows = state.list_workers(conn)
            if not rows:
                typer.echo("(no workers)")
                return
            for w in rows:
                typer.echo(
                    f"{w.id:>8}  {w.status:<12}  turns={w.turns:<4}  {w.progress or ''}"
                )


@app.command()
def stop(worker_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Send Ctrl-C twice to the worker pane and mark stopped."""
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None:
            typer.echo(f"no such worker: {worker_id}", err=True)
            raise typer.Exit(2)
        signaled = True
        try:
            tmux.send_ctrl_c(w.pane_target)
            time.sleep(0.5)
            tmux.send_ctrl_c(w.pane_target)
        except subprocess.CalledProcessError as e:
            signaled = False
            state.record_event(conn, "stop_send_failed", worker_id=worker_id, error=repr(e))
        if signaled:
            state.update_worker(conn, worker_id, status="stopped")
            state.record_event(conn, "stopped", worker_id=worker_id)
            typer.echo(f"stopped {worker_id}")
        else:
            state.update_worker(conn, worker_id, status="stop_send_failed")
            typer.echo(
                f"warning: failed to send Ctrl-C to {w.pane_target} — worker may still be running",
                err=True,
            )
            raise typer.Exit(1)


@app.command()
def tail(
    worker_id: str = typer.Argument(..., metavar="ID"),
    lines: int = typer.Option(80, "--lines", "-n"),
) -> None:
    """Print the last N lines of the worker's pane (one-shot)."""
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None:
            typer.echo(f"no such worker: {worker_id}", err=True)
            raise typer.Exit(2)
        typer.echo(tmux.capture(w.pane_target, lines=lines))


@app.command()
def dash(
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
) -> None:
    """Start the dashboard."""
    import uvicorn  # noqa: PLC0415 — lazy import (web module may not be installed)

    uvicorn.run("orchestra.web:app", host=host, port=port, log_level="info")


@app.command("run")
def run_command(
    mission: Path = typer.Argument(..., metavar="MISSION_MD"),  # noqa: B008
    model: str = typer.Option("opus", "--model"),
    max_wallclock: float = typer.Option(5400.0, "--max-wallclock"),
    max_activity: float = typer.Option(600.0, "--max-activity"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
) -> None:
    """Spawn a PM with MISSION_MD and block until done or watchdog fires."""
    from orchestra import run as run_mod  # local import keeps CLI startup cheap

    rc = run_mod.run_mission(
        mission,
        model=model,
        max_wallclock=max_wallclock,
        max_activity=max_activity,
        allow_dirty=allow_dirty,
    )
    raise typer.Exit(rc)


# ---- worker subcommands ----

def _worker_env() -> tuple[str, Path]:
    wid = os.environ.get("ORCHESTRA_WORKER_ID")
    db = os.environ.get("ORCHESTRA_STATE_DB")
    if not wid or not db:
        typer.echo(
            "error: must run inside a spawned worker pane "
            "(ORCHESTRA_WORKER_ID + ORCHESTRA_STATE_DB required)",
            err=True,
        )
        raise typer.Exit(2)
    return wid, Path(db)


@worker_app.command("status")
def worker_status(
    progress: str = typer.Option(..., "--progress"),
    turns: int = typer.Option(..., "--turns"),
) -> None:
    """Update worker progress and turn count."""
    wid, db = _worker_env()
    conn = state.connect(db)
    try:
        state.update_worker(conn, wid, progress=progress, turns=turns)
        state.record_event(conn, "status", worker_id=wid, progress=progress, turns=turns)
    finally:
        conn.close()


@worker_app.command("escalate")
def worker_escalate(
    question: str = typer.Option(..., "--question"),
    context: str | None = typer.Option(None, "--context"),
    blocking: bool = typer.Option(False, "--blocking"),
) -> None:
    """Escalate a question to the user."""
    wid, db = _worker_env()
    conn = state.connect(db)
    try:
        esc = state.create_escalation(
            conn, worker_id=wid, question=question, context=context, blocking=blocking,
        )
        if blocking:
            state.update_worker(conn, wid, status="waiting")
        state.record_event(
            conn, "escalation", worker_id=wid,
            escalation_id=esc.id, blocking=blocking, question=question,
        )
    finally:
        conn.close()


@worker_app.command("done")
def worker_done(
    summary: str = typer.Option("done", "--summary"),
) -> None:
    """Mark this worker as done (cooperative) and record a worker_done event."""
    wid, db = _worker_env()
    conn = state.connect(db)
    try:
        state.update_worker(conn, wid, status="done", progress=summary)
        state.record_event(conn, "worker_done", worker_id=wid, summary=summary)
    finally:
        conn.close()


@worker_app.command("hook")
def worker_hook(event: str = typer.Argument(..., metavar="EVENT")) -> None:
    """Hook entrypoint invoked by Claude Code; reads payload JSON on stdin."""
    from orchestra import hooks  # local import to keep CLI import cheap

    rc = hooks.main([event])
    raise typer.Exit(rc)


@app.command("send")
def send_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
    message: str = typer.Argument(..., metavar="MSG"),
) -> None:
    """Type MSG into the worker's pane (PM → engineer nudges)."""
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None:
            typer.echo(f"no such worker: {worker_id}", err=True)
            raise typer.Exit(2)
        tmux.send_multiline(w.pane_target, message)
        state.record_event(conn, "message_sent", worker_id=worker_id, message=message)


@app.command("answer")
def answer_command(
    escalation_id: int = typer.Argument(..., metavar="ESC_ID"),
    answer: str = typer.Argument(..., metavar="ANSWER"),
) -> None:
    """Resolve an escalation and send the answer to the asker's pane."""
    with _open_db() as conn:
        try:
            esc = state.resolve_escalation(conn, escalation_id, answer=answer)
        except KeyError:
            typer.echo(f"no open escalation #{escalation_id}", err=True)
            raise typer.Exit(2) from None
        w = state.get_worker(conn, esc.worker_id)
        if w is not None:
            tmux.send_multiline(w.pane_target, f"[answer to #{esc.id}] {answer}")
        state.record_event(
            conn, "escalation_resolved", worker_id=esc.worker_id,
            escalation_id=esc.id, answer=answer,
        )


def _cursor_file(caller: str) -> Path:
    return _orch_dir() / f"poll-cursor.{caller}"


@app.command("poll")
def poll_command(
    timeout: float = typer.Option(30.0, "--timeout"),
    include_tools: bool = typer.Option(False, "--include-tools"),
    caller: str = typer.Option("pm", "--caller",
                                help="Caller id for cursor persistence."),
) -> None:
    """Block up to TIMEOUT seconds for new engineer events; print state snapshot."""
    from orchestra import poll as poll_mod  # local import keeps CLI cheap

    db = _require_initialized()
    cur_path = _cursor_file(caller)
    since_id = 0
    if cur_path.exists():
        try:
            since_id = int(cur_path.read_text().strip() or "0")
        except ValueError:
            since_id = 0
    new_cursor, snapshot = poll_mod.poll(
        db, since_id=since_id, timeout=timeout, include_tools=include_tools,
    )
    cur_path.write_text(str(new_cursor))
    typer.echo(snapshot)


@app.command("merge")
def merge_command(
    ids: Annotated[list[str] | None, typer.Argument(metavar="ID...")] = None,
    batch: Annotated[
        bool,
        typer.Option(
            "--batch", "-b",
            help="Merge multiple worker branches sequentially; "
                 "aborts on first conflict.",
        ),
    ] = False,
) -> None:
    """Merge orch/<worker_id> into the current branch.

    Single-arg form: ``orchestra merge backend`` — unchanged behaviour, echoes a
    human-readable line, exits 1 on conflict.

    Batch form: ``orchestra merge --batch backend web cli`` — merges each in
    order in-process. On the first conflict, aborts the in-progress merge and
    SKIPS remaining ids (no events recorded for skipped ids). Emits JSON to
    stdout; exits 2 on any non-clean result.
    """
    if not ids:
        typer.echo("error: provide an ID or --batch <id1> <id2> ...", err=True)
        raise typer.Exit(2)
    if not batch and len(ids) > 1:
        typer.echo(
            "error: multiple IDs require --batch (got "
            f"{len(ids)}: {' '.join(ids)})",
            err=True,
        )
        raise typer.Exit(2)

    project_root = Path.cwd()
    # Single-arg legacy path: preserve exact pre-existing output + exit code.
    if not batch:
        wid = ids[0]
        branch = f"orch/{wid}"
        with _open_db() as conn:
            state.record_event(conn, "merge_attempted", worker_id=wid, branch=branch)
            proc = subprocess.run(
                ["git", "-C", str(project_root), "merge", "--no-edit", branch],
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                state.record_event(conn, "merge_ok", worker_id=wid,
                                   stdout=proc.stdout[-2000:])
                typer.echo(f"merged {branch}")
            else:
                state.record_event(
                    conn, "merge_conflict", worker_id=wid,
                    stdout=proc.stdout[-2000:], stderr=proc.stderr[-2000:],
                )
                typer.echo(f"merge conflict on {branch}", err=True)
                raise typer.Exit(1)
        return

    results: list[dict[str, str]] = []
    aborted = False
    with _open_db() as conn:
        for wid in ids:
            if aborted:
                results.append({"id": wid, "status": "skipped"})
                continue
            branch = f"orch/{wid}"
            state.record_event(conn, "merge_attempted", worker_id=wid, branch=branch)
            proc = subprocess.run(
                ["git", "-C", str(project_root), "merge", "--no-edit", branch],
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                state.record_event(conn, "merge_ok", worker_id=wid,
                                   stdout=proc.stdout[-2000:])
                results.append({"id": wid, "status": "ok"})
            else:
                summary = (proc.stdout + proc.stderr)[:500]
                state.record_event(
                    conn, "merge_conflict", worker_id=wid,
                    stdout=proc.stdout[-2000:], stderr=proc.stderr[-2000:],
                )
                results.append({"id": wid, "status": "conflict", "summary": summary})
                subprocess.run(
                    ["git", "-C", str(project_root), "merge", "--abort"],
                    capture_output=True,
                )
                aborted = True
    typer.echo(json.dumps(results, indent=2))
    if any(r["status"] != "ok" for r in results):
        raise typer.Exit(2)


@app.command("reap")
def reap_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
) -> None:
    """Remove the worker's worktree and delete its branch."""
    from orchestra import worktree as wt_mod
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None or w.worktree is None:
            typer.echo(f"worker {worker_id} has no worktree", err=True)
            raise typer.Exit(2)
        wt_mod.remove(Path.cwd(), name=w.worktree, worker_id=worker_id)
        state.record_event(conn, "worktree_reaped", worker_id=worker_id,
                           name=w.worktree)
