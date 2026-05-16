"""Typer commands for orchestra."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

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
    """Initialize .orchestra/ in the current directory."""
    d = _orch_dir()
    d.mkdir(exist_ok=True)
    db = d / "state.db"
    conn = state.connect(db)
    state.init_schema(conn)
    conn.close()
    cfg = d / "config.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG)
    typer.echo(f"initialized {d}")


@app.command("spawn")
def spawn_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
    model: str = typer.Argument(..., metavar="MODEL"),
    task: str = typer.Argument(..., metavar="TASK"),
    context: list[str] = typer.Option(  # noqa: B008
        [], "--context", help="Context files."
    ),
) -> None:
    """Spawn a worker into a new tmux window."""
    project_root = str(Path.cwd())
    session_name = _session_name_for(Path.cwd())
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
