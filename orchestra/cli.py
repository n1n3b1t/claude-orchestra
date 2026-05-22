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
    exclusive_resource: str | None = typer.Option(  # noqa: B008
        None, "--exclusive-resource",
        help="Acquire a named exclusive lock before spawn; blocks if held.",
    ),
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
            exclusive_resource=exclusive_resource,
        )
        typer.echo(f"spawn {worker_id} → {session_name}:{worker_id}")


@app.command("spawn-batch")
def spawn_batch_command(
    spec_file: Path = typer.Argument(..., metavar="SPEC_JSONL"),  # noqa: B008
) -> None:
    """Spawn multiple workers concurrently from a JSONL spec file."""
    from orchestra import spawn_batch as sb
    project_root = Path.cwd()
    state_db = project_root / ORCH_DIR_NAME / "state.db"
    if not state_db.exists():
        typer.echo("error: run `orchestra init` first", err=True)
        raise typer.Exit(2)
    try:
        specs = sb.parse_jsonl(spec_file)
    except (ValueError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from None
    session_name = _session_name_for(project_root)
    results = sb.run(
        specs=specs,
        project_root=str(project_root),
        state_db=state_db,
        session_name=session_name,
    )
    bad = [r for r in results if r["status"] != "ok"]
    for r in results:
        suffix = f" — {r.get('error', '')}" if r["status"] != "ok" else ""
        typer.echo(f"  {r['id']}: {r['status']}{suffix}")
    if bad:
        raise typer.Exit(2)


@app.command()
def status(
    worker: str | None = typer.Option(None, "--worker"),
    cost_mode: str = typer.Option("tokens", "--cost-mode", help="tokens (default) or dollars."),
) -> None:
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
            # local import to avoid circular at import time
            from orchestra import cost as cost_mod
            from orchestra import poll as poll_mod
            for w in rows:
                if cost_mode == "dollars":
                    usd = poll_mod._cost_usd_for(conn, w.id, w.model)
                    cost_str = f"${usd:>6.2f}"
                else:
                    inp, out, cache = poll_mod._token_summary_for(conn, w.id)
                    cost_str = cost_mod.format_tokens(inp, out, cache)
                msg = w.progress or ""
                typer.echo(
                    f"{w.id:>8}  {w.status:<12}  turns={w.turns:<4}  {cost_str}  {msg}"
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
            state.release_worker_resources(conn, worker_id)
            typer.echo(f"stopped {worker_id}")
        else:
            state.update_worker(conn, worker_id, status="stop_send_failed")
            typer.echo(
                f"warning: failed to send Ctrl-C to {w.pane_target} — worker may still be running",
                err=True,
            )
            raise typer.Exit(1)


@app.command("release-resource")
def release_resource_command(
    name: str = typer.Argument(..., metavar="NAME"),
    worker_id: str | None = typer.Option(
        None, "--worker", help="Only release if held by this worker."
    ),
) -> None:
    """Manually release a resource lock (emergency / cleanup)."""
    with _open_db() as conn:
        if worker_id:
            ok = state.release_resource(conn, name, worker_id)
        else:
            cur = conn.execute(
                "DELETE FROM resource_locks WHERE name = ?", (name,)
            )
            ok = cur.rowcount > 0
        if not ok:
            typer.echo(f"no lock held on {name}", err=True)
            raise typer.Exit(2)
        typer.echo(f"released {name}")


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


@app.command("mission")
def mission_command(
    action: str = typer.Argument(..., metavar="ACTION", help="lint"),
    path: Path = typer.Argument(..., metavar="MISSION_MD"),  # noqa: B008
    strict: bool = typer.Option(
        False, "--strict", help="Promote brief-not-found from warning to error."
    ),
) -> None:
    """Mission utilities. Today: `orchestra mission lint <path>`."""
    if action != "lint":
        typer.echo(f"unknown action: {action}", err=True)
        raise typer.Exit(2)
    from orchestra import mission_lint
    findings = mission_lint.lint(path, strict=strict)
    typer.echo(mission_lint.render(findings))
    if mission_lint.has_errors(findings):
        raise typer.Exit(2)


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
        state.release_worker_resources(conn, wid)
    finally:
        conn.close()


@worker_app.command("hook")
def worker_hook(event: str = typer.Argument(..., metavar="EVENT")) -> None:
    """Hook entrypoint invoked by Claude Code; reads payload JSON on stdin."""
    from orchestra import hooks  # local import to keep CLI import cheap

    rc = hooks.main([event])
    raise typer.Exit(rc)


@worker_app.command("shutdown-hookd")
def worker_shutdown_hookd() -> None:
    """Stop the project's hook daemon (if running) and clean up PID + socket."""
    import contextlib
    import signal

    orch = _orch_dir()
    pid_path = orch / "hookd.pid"
    sock_path = orch / "hook.sock"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError, OSError):
            pass
    for p in (pid_path, sock_path):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
    typer.echo("hookd stopped")


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


def _reap(worker_id: str) -> bool:
    """Remove the worker's worktree, delete its branch, record the event.

    Returns True if the worker had a worktree to reap, False otherwise.
    """
    from orchestra import worktree as wt_mod
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None or w.worktree is None:
            return False
        wt_mod.remove(Path.cwd(), name=w.worktree, worker_id=worker_id)
        state.record_event(conn, "worktree_reaped", worker_id=worker_id,
                           name=w.worktree)
        return True


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
    keep: Annotated[
        bool,
        typer.Option(
            "--keep",
            help="Keep worktree + branch after merge (legacy v2.1 behavior). "
                 "Default since v2.2 is to auto-reap on success.",
        ),
    ] = False,
) -> None:
    """Merge orch/<worker_id> into the current branch and reap on success.

    Single-arg form: ``orchestra merge backend`` — on success the worktree +
    branch are reaped; on conflict, the worktree is kept for inspection and the
    command exits 1.

    Batch form: ``orchestra merge --batch backend web cli`` — merges each in
    order in-process. After each successful merge, that worker is reaped.
    On a conflict at position N, positions 0..N-1 (already merged) are reaped,
    N is kept for inspection, and N+1..M are skipped. Emits JSON; exits 2 on
    any non-clean result.

    ``--keep`` preserves the legacy v2.1 behavior of leaving the worktree +
    branch intact after a successful merge.
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
        if not keep:
            _reap(wid)
        return

    results: list[dict[str, str]] = []
    aborted = False
    reaped_ok: list[str] = []
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
                reaped_ok.append(wid)
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
    if not keep:
        for wid in reaped_ok:
            _reap(wid)
    typer.echo(json.dumps(results, indent=2))
    if any(r["status"] != "ok" for r in results):
        raise typer.Exit(2)


@app.command("reap")
def reap_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
) -> None:
    """Remove the worker's worktree and delete its branch."""
    if not _reap(worker_id):
        typer.echo(f"worker {worker_id} has no worktree", err=True)
        raise typer.Exit(2)


@app.command("new-role")
def new_role_command(
    name: str = typer.Argument(..., metavar="NAME"),
    engineer: bool = typer.Option(False, "--engineer"),
    reviewer: bool = typer.Option(False, "--reviewer"),
    runner: bool = typer.Option(False, "--runner"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Scaffold a role markdown file in .orchestra/roles/<NAME>.md."""
    from orchestra import role_scaffold  # local import keeps CLI cheap

    if sum([engineer, reviewer, runner]) > 1:
        typer.echo("error: at most one of --engineer, --reviewer, --runner may be set", err=True)
        raise typer.Exit(2)

    dest_dir = Path.cwd() / ORCH_DIR_NAME / "roles"
    try:
        path = role_scaffold.scaffold(
            name,
            dest_dir=dest_dir,
            engineer=engineer,
            reviewer=reviewer,
            runner=runner,
            force=force,
        )
    except FileExistsError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from None
    typer.echo(str(path.resolve()))
