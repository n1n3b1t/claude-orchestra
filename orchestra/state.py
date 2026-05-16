"""SQLite-backed state for claude-orchestra.

Tables:
- workers: one row per spawned worker, mutated as the worker progresses
- events: append-only audit trail; payload is JSON
- escalations: blocking/non-blocking questions from workers to user

Connection settings: WAL journal mode + 5s busy timeout.
"""
from __future__ import annotations

import json
import sqlite3
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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
    updated_at  TEXT NOT NULL
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
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


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
) -> Worker:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO workers (id, task, model, branch, pane_target,
                             status, progress, turns, started_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
        """,
        (id, task, model, branch, pane_target, status, ts, ts),
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
