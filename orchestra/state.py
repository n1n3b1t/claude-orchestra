"""SQLite-backed state for claude-orchestra.

Tables:
- workers: one row per spawned worker, mutated as the worker progresses.
  v1 columns: role ('engineer'|'pm'), worktree (None unless --worktree was set).
- events: append-only audit trail; payload is JSON. `events.kind` is free-form
  text — no schema migration is needed when a new kind is added.
- escalations: blocking/non-blocking questions from workers to user / PM.

Event kinds (events.kind values), grouped by lifecycle:

  Spawn lifecycle (orchestra/spawn.py):
    spawn_start                  — worker row created, spawn flow started
    worktree_created             — engineer worktree set up under worktrees/<name>
    worktree_failed              — git worktree add failed; worker → status=error
    spawn_window                 — tmux window for the worker pane created
    spawn_trust_accepted         — Claude trust prompt dismissed in parallel
    spawn_idle                   — session_ready event arrived in time
    spawn_stale_idle             — _wait_idle_via_event timed out (soft, continues)
    model_switched               — /<model> slash command sent to pane
    prompt_inject_retry          — send_multiline raised; retrying
    prompt_inject_failed         — startup prompt injection gave up
    prompt_injected              — startup prompt successfully sent
    spawn_ok                     — first turn_complete arrived (proof of life)
    spawn_first_status_timeout   — first-status wait timed out (soft)

  Hook-driven worker lifecycle (orchestra/hooks.py):
    session_ready                — SessionStart hook fired; worker → working
    turn_complete                — Stop hook fired; worker.turns += 1
                                   (payload tokens: input_tokens, output_tokens,
                                   cache_read_tokens, cache_creation_tokens —
                                   currently best-effort; real counts live in
                                   the transcript JSONL, see issue #8)
    tool_started                 — PreToolUse hook fired
    tool_finished                — PostToolUse hook fired
    session_ended                — SessionEnd hook fired; worker → done unless
                                   already in a terminal state
    notification                 — Notification hook fired (permission stalls)
    done_to_working_blocked      — SessionStart re-entry intercepted because
                                   the worker had already cooperatively
                                   completed (preserved-done; issue #2 / #14)
    hook_error                   — caught exception inside a hook handler

  Coordination (orchestra/cli.py, orchestra/web.py):
    status                       — worker self-reported progress + turns
    message_sent                 — `orchestra send` typed into the worker pane
    escalation                   — `orchestra worker escalate` created a row
    escalation_resolved          — `orchestra answer` or dashboard resolved it
    worker_done                  — cooperative termination via
                                   `orchestra worker done` (worker → done)
    stopped                      — `orchestra stop` sent Ctrl-C twice
    stop_send_failed             — Ctrl-C send raised; worker may still run

  Merge / worktree (orchestra/cli.py):
    merge_attempted              — `orchestra merge` invoked git merge
    merge_ok                     — git merge returned 0
    merge_conflict               — git merge returned non-zero
    worktree_reaped              — `orchestra reap` removed worktree + branch

Worker status values: spawning, working, waiting (blocking escalation),
done, error, stopped, stop_send_failed, stale_spawn.

Connection settings: WAL journal mode + 5s busy timeout.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---- Dataclasses ----

@dataclass(frozen=True)
class Worker:
    id: str
    task: str
    model: str
    branch: str | None
    pane_target: str
    status: str
    progress: str | None
    turns: int
    started_at: str
    updated_at: str
    role: str = "engineer"
    worktree: str | None = None


@dataclass(frozen=True)
class Event:
    id: int
    worker_id: str | None
    ts: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Escalation:
    id: int
    worker_id: str
    ts: str
    question: str
    context: str | None
    blocking: bool
    resolved: bool
    answer: str | None


# ---- Helpers ----

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # PRAGMA journal_mode=WAL is exclusive and busy_timeout does not apply, so
    # concurrent first-time connections can collide. Skip the switch when the
    # file is already WAL, and retry briefly otherwise.
    mode_row = conn.execute("PRAGMA journal_mode").fetchone()
    if (mode_row[0] if mode_row else "").lower() != "wal":
        for attempt in range(5):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---- Schema ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id          TEXT PRIMARY KEY,
    task        TEXT NOT NULL,
    model       TEXT NOT NULL,
    branch      TEXT,
    pane_target TEXT NOT NULL,
    status      TEXT NOT NULL,
    progress    TEXT,
    turns       INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'engineer',
    worktree    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_worker_ts ON events(worker_id, ts);
CREATE TABLE IF NOT EXISTS escalations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    ts        TEXT NOT NULL,
    question  TEXT NOT NULL,
    context   TEXT,
    blocking  INTEGER NOT NULL,
    resolved  INTEGER NOT NULL DEFAULT 0,
    answer    TEXT
);
CREATE TABLE IF NOT EXISTS resource_locks (
    name        TEXT PRIMARY KEY,
    worker_id   TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
"""


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Forward-compat: if a v0 DB pre-dates role/worktree, ALTER TABLE in.
    cols = _existing_columns(conn, "workers")
    if "role" not in cols:
        conn.execute(
            "ALTER TABLE workers ADD COLUMN role TEXT NOT NULL DEFAULT 'engineer'"
        )
    if "worktree" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN worktree TEXT")


# ---- Workers ----

def _row_to_worker(row: sqlite3.Row) -> Worker:
    return Worker(
        id=row["id"],
        task=row["task"],
        model=row["model"],
        branch=row["branch"],
        pane_target=row["pane_target"],
        status=row["status"],
        progress=row["progress"],
        turns=row["turns"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        role=row["role"],
        worktree=row["worktree"],
    )


def create_worker(
    conn: sqlite3.Connection,
    *,
    id: str,
    task: str,
    model: str,
    branch: str | None,
    pane_target: str,
    status: str = "spawning",
    role: str = "engineer",
    worktree: str | None = None,
) -> Worker:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO workers (id, task, model, branch, pane_target,
                             status, progress, turns, started_at, updated_at,
                             role, worktree)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?)
        """,
        (id, task, model, branch, pane_target, status, ts, ts, role, worktree),
    )
    got = get_worker(conn, id)
    assert got is not None  # just inserted
    return got


def get_worker(conn: sqlite3.Connection, worker_id: str) -> Worker | None:
    row = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    return _row_to_worker(row) if row else None


def list_workers(conn: sqlite3.Connection) -> list[Worker]:
    rows = conn.execute("SELECT * FROM workers ORDER BY started_at ASC").fetchall()
    return [_row_to_worker(r) for r in rows]


def update_worker(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    status: str | None = None,
    progress: str | None = None,
    turns: int | None = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if progress is not None:
        sets.append("progress = ?")
        args.append(progress)
    if turns is not None:
        sets.append("turns = ?")
        args.append(turns)
    if not sets:
        raise ValueError("update_worker requires at least one field to update")
    sets.append("updated_at = ?")
    args.append(now_iso())
    args.append(worker_id)
    cur = conn.execute(f"UPDATE workers SET {', '.join(sets)} WHERE id = ?", args)
    if cur.rowcount == 0:
        raise KeyError(f"worker {worker_id} not found")


# ---- Events ----

def record_event(
    conn: sqlite3.Connection,
    kind: str,
    worker_id: str | None = None,
    **payload: Any,
) -> Event:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO events (worker_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
        (worker_id, ts, kind, json.dumps(payload, default=str)),
    )
    evt_id = cur.lastrowid
    assert evt_id is not None
    return Event(id=evt_id, worker_id=worker_id, ts=ts, kind=kind, payload=dict(payload))


def list_events(
    conn: sqlite3.Connection,
    *,
    worker_id: str | None = None,
    since_id: int | None = None,
    limit: int = 200,
) -> list[Event]:
    where: list[str] = []
    args: list[Any] = []
    if worker_id is not None:
        where.append("worker_id = ?")
        args.append(worker_id)
    if since_id is not None:
        where.append("id > ?")
        args.append(since_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM events{clause} ORDER BY id ASC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [
        Event(
            id=r["id"],
            worker_id=r["worker_id"],
            ts=r["ts"],
            kind=r["kind"],
            payload=json.loads(r["payload"]) if r["payload"] else {},
        )
        for r in rows
    ]


# ---- Escalations ----

def _row_to_escalation(row: sqlite3.Row) -> Escalation:
    return Escalation(
        id=row["id"],
        worker_id=row["worker_id"],
        ts=row["ts"],
        question=row["question"],
        context=row["context"],
        blocking=bool(row["blocking"]),
        resolved=bool(row["resolved"]),
        answer=row["answer"],
    )


def create_escalation(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    question: str,
    context: str | None,
    blocking: bool,
) -> Escalation:
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO escalations (worker_id, ts, question, context, blocking, resolved)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (worker_id, ts, question, context, 1 if blocking else 0),
    )
    esc_id = cur.lastrowid
    assert esc_id is not None
    row = conn.execute("SELECT * FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    return _row_to_escalation(row)


def resolve_escalation(
    conn: sqlite3.Connection,
    escalation_id: int,
    *,
    answer: str,
) -> Escalation:
    cur = conn.execute(
        "UPDATE escalations SET resolved = 1, answer = ? WHERE id = ? AND resolved = 0",
        (answer, escalation_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"escalation {escalation_id} not found or already resolved")
    row = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
    return _row_to_escalation(row)


def list_open_escalations(
    conn: sqlite3.Connection,
    worker_id: str | None = None,
) -> list[Escalation]:
    if worker_id:
        rows = conn.execute(
            "SELECT * FROM escalations WHERE resolved = 0 AND worker_id = ? ORDER BY id ASC",
            (worker_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM escalations WHERE resolved = 0 ORDER BY id ASC"
        ).fetchall()
    return [_row_to_escalation(r) for r in rows]


# ---- Resource locks ----

def acquire_resource(
    conn: sqlite3.Connection,
    name: str,
    worker_id: str,
    *,
    blocking: bool = True,
    timeout_s: float = 300.0,
    poll_s: float = 0.5,
) -> bool:
    """INSERT-OR-FAIL on resource_locks(name).

    Returns True on acquire. When blocking=True and the row exists, polls every
    poll_s seconds until either the lock is gone (then re-tries INSERT) or
    timeout_s elapses. Returns False if non-blocking-and-held or timeout.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            conn.execute(
                "INSERT INTO resource_locks (name, worker_id, acquired_at)"
                " VALUES (?, ?, ?)",
                (name, worker_id, now_iso()),
            )
            return True
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                # Table absent means schema not initialized; treat as unheld.
                return False
            raise
        except sqlite3.IntegrityError:
            if not blocking:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)


def release_resource(
    conn: sqlite3.Connection, name: str, worker_id: str
) -> bool:
    """DELETE WHERE name=? AND worker_id=?. Returns True iff a row was deleted."""
    try:
        cur = conn.execute(
            "DELETE FROM resource_locks WHERE name = ? AND worker_id = ?",
            (name, worker_id),
        )
        return cur.rowcount > 0
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise


def release_worker_resources(
    conn: sqlite3.Connection, worker_id: str
) -> int:
    """Release ALL locks held by worker_id. Returns count released."""
    try:
        cur = conn.execute(
            "DELETE FROM resource_locks WHERE worker_id = ?",
            (worker_id,),
        )
        return cur.rowcount
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
