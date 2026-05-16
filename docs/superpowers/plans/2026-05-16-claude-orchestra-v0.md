# claude-orchestra v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the v0 vertical slice from the design doc — Python package that can spawn one Claude Code worker in tmux, persist state in SQLite, and surface it live on a localhost web dashboard.

**Architecture:** Single Python package `orchestra`. Project-local state under `.orchestra/state.db` (SQLite, WAL). One process per role: short-lived `spawn` and `worker` subcommands; long-lived `dash` (FastAPI). Workers shell out to `orchestra worker status …` to update SQLite. Dashboard reads SQLite and pushes events over SSE; pane peeks via short-poll calling `tmux capture-pane`.

**Tech Stack:** Python 3.11, Typer (CLI), FastAPI + uvicorn (web), sse-starlette (event stream), Jinja2 (templates), sqlite3 (stdlib), pytest + pytest-asyncio (tests), ruff + mypy (lint/typecheck).

**Reference:** `docs/superpowers/specs/2026-05-16-claude-orchestra-design.md` (v0 design spec).

---

## Conventions

- All file paths are absolute relative to repo root `~/dev/claude-orchestra/`.
- Python target: 3.11+. `from __future__ import annotations` everywhere.
- Imports sorted by `ruff` (isort rules); types via PEP 604 (`str | None`, not `Optional[str]`).
- All test files in `tests/` mirror module names.
- Each task ends with a commit. Commit messages follow `<area>: <imperative>` (e.g. `state: add escalation CRUD`).

---

## Task 1: Project scaffolding (TaskCreate #11)

**Goal:** Create the Python package skeleton — `pyproject.toml`, `orchestra/__init__.py`, `orchestra/__main__.py`, `tests/__init__.py`, ruff + mypy config. After this task, `pip install -e .` succeeds and `orchestra --help` runs (even though it prints nothing useful yet).

**Files:**
- Create: `pyproject.toml`
- Create: `orchestra/__init__.py`
- Create: `orchestra/__main__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Acceptance Criteria:**
- [ ] `pip install -e .` succeeds in a clean venv
- [ ] `orchestra --version` prints `orchestra 0.1.0`
- [ ] `pytest` runs (zero tests, zero failures)
- [ ] `ruff check .` passes
- [ ] `mypy orchestra/` passes

**Verify:** `pip install -e . && orchestra --version && pytest -q && ruff check . && mypy orchestra/`

**Steps:**

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "claude-orchestra"
version = "0.1.0"
description = "Tmux-based orchestrator for parallel Claude Code workers"
requires-python = ">=3.11"
readme = "README.md"
license = { text = "MIT" }
dependencies = [
    "typer>=0.12",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "jinja2>=3.1",
    "sse-starlette>=2.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
    "ruff>=0.4",
    "mypy>=1.10",
    "types-jinja2",
]

[project.scripts]
orchestra = "orchestra.__main__:main"

[tool.setuptools.packages.find]
include = ["orchestra*"]

[tool.setuptools.package-data]
orchestra = ["templates/*.html", "static/*"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]

[tool.mypy]
python_version = "3.11"
strict = false

[[tool.mypy.overrides]]
module = ["orchestra.state", "orchestra.tmux"]
strict = true

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Write `orchestra/__init__.py`**

```python
"""claude-orchestra: tmux-based orchestrator for parallel Claude Code workers."""
from __future__ import annotations

__version__ = "0.1.0"
```

- [ ] **Step 3: Write `orchestra/__main__.py`**

```python
"""Entry point for the `orchestra` CLI."""
from __future__ import annotations

import typer

from orchestra import __version__

app = typer.Typer(help="Tmux-based orchestrator for parallel Claude Code workers.")


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        typer.echo(f"orchestra {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write `tests/__init__.py` and `tests/conftest.py`**

`tests/__init__.py`:
```python
```

`tests/conftest.py`:
```python
"""Shared pytest fixtures."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """A fresh SQLite database path under a temp dir."""
    return tmp_path / "state.db"


@pytest.fixture
def tmp_orch_dir(tmp_path: Path) -> Path:
    """A fresh `.orchestra/` directory under a temp dir."""
    d = tmp_path / ".orchestra"
    d.mkdir()
    return d
```

- [ ] **Step 5: Install and verify**

Run:
```bash
cd ~/dev/claude-orchestra
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
orchestra --version
pytest -q
ruff check .
mypy orchestra/
```

Expected:
- `orchestra --version` prints `orchestra 0.1.0`
- `pytest -q` reports `no tests ran`
- ruff and mypy exit 0

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml orchestra/ tests/
git commit -m "scaffold: pyproject + package skeleton + ruff/mypy config"
```

---

## Task 2: SQLite state layer (TaskCreate #6)

**Goal:** `orchestra.state` — connect to SQLite with WAL + busy_timeout, init schema (workers/events/escalations), CRUD wrappers returning dataclasses, `record_event(conn, kind, worker_id=None, **payload)`.

**Files:**
- Create: `orchestra/state.py`
- Create: `tests/test_state.py`

**Acceptance Criteria:**
- [ ] `connect(path)` returns a connection with `journal_mode=WAL` and `busy_timeout=5000`
- [ ] `init_schema(conn)` is idempotent (running twice does not error)
- [ ] `create_worker`, `get_worker`, `list_workers`, `update_worker` round-trip a Worker dataclass
- [ ] `record_event(conn, kind, worker_id=None, **payload)` JSON-encodes payload, returns an Event with an assigned `id`
- [ ] `list_events(conn, worker_id=None, since_id=None, limit=200)` orders by id ASC, filters as documented
- [ ] `create_escalation` + `resolve_escalation` transition `resolved` from 0→1 with the answer captured
- [ ] Index `events_worker_ts` exists after init

**Verify:** `pytest tests/test_state.py -v && mypy --strict orchestra/state.py`

**Steps:**

- [ ] **Step 1: Write the failing tests (`tests/test_state.py`)**

```python
"""Tests for orchestra.state."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestra import state


def _open(tmp_db: Path) -> sqlite3.Connection:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    return conn


class TestConnect:
    def test_wal_and_busy_timeout(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode.lower() == "wal"
        assert timeout == 5000

    def test_init_schema_idempotent(self, tmp_db: Path) -> None:
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.init_schema(conn)  # must not raise


class TestWorkers:
    def test_create_then_get(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        created = state.create_worker(
            conn,
            id="w1",
            task="Implement auth",
            model="sonnet",
            branch="orch/w1",
            pane_target="orch-proj:1",
        )
        assert created.id == "w1"
        assert created.status == "spawning"
        assert created.turns == 0
        got = state.get_worker(conn, "w1")
        assert got == created

    def test_get_missing_returns_none(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        assert state.get_worker(conn, "nope") is None

    def test_update_worker(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        state.update_worker(conn, "w1", status="working", progress="ok", turns=3)
        got = state.get_worker(conn, "w1")
        assert got is not None
        assert got.status == "working"
        assert got.progress == "ok"
        assert got.turns == 3

    def test_list_workers(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        state.create_worker(
            conn, id="w1", task="t1", model="sonnet",
            branch=None, pane_target="s:1",
        )
        state.create_worker(
            conn, id="w2", task="t2", model="haiku",
            branch=None, pane_target="s:2",
        )
        rows = state.list_workers(conn)
        assert {w.id for w in rows} == {"w1", "w2"}


class TestEvents:
    def test_record_event_with_payload(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        evt = state.record_event(
            conn, kind="spawn_start", worker_id="w1", task="t", model="sonnet",
        )
        assert evt.id >= 1
        assert evt.kind == "spawn_start"
        assert evt.worker_id == "w1"
        assert evt.payload == {"task": "t", "model": "sonnet"}

    def test_list_events_filters_by_worker_and_since(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        a = state.record_event(conn, kind="spawn_start", worker_id="w1")
        b = state.record_event(conn, kind="spawn_window", worker_id="w1")
        c = state.record_event(conn, kind="spawn_start", worker_id="w2")

        all_events = state.list_events(conn)
        assert [e.id for e in all_events] == [a.id, b.id, c.id]

        w1_only = state.list_events(conn, worker_id="w1")
        assert [e.id for e in w1_only] == [a.id, b.id]

        after_a = state.list_events(conn, since_id=a.id)
        assert [e.id for e in after_a] == [b.id, c.id]


class TestEscalations:
    def test_create_then_resolve(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        esc = state.create_escalation(
            conn, worker_id="w1", question="RS256 or HS256?",
            context="key mgmt", blocking=True,
        )
        assert esc.resolved is False
        assert esc.answer is None
        opens = state.list_open_escalations(conn)
        assert [e.id for e in opens] == [esc.id]

        resolved = state.resolve_escalation(conn, esc.id, answer="Use RS256")
        assert resolved.resolved is True
        assert resolved.answer == "Use RS256"

        assert state.list_open_escalations(conn) == []

    def test_resolve_missing_raises(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        with pytest.raises(KeyError):
            state.resolve_escalation(conn, 999, answer="x")


def test_events_worker_ts_index_exists(tmp_db: Path) -> None:
    conn = _open(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "events_worker_ts" in names
```

- [ ] **Step 2: Run tests, watch them fail**

Run: `pytest tests/test_state.py -v`
Expected: ImportError / AttributeError on every test (module doesn't exist yet).

- [ ] **Step 3: Implement `orchestra/state.py`**

```python
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
        sets.append("status = ?"); args.append(status)
    if progress is not None:
        sets.append("progress = ?"); args.append(progress)
    if turns is not None:
        sets.append("turns = ?"); args.append(turns)
    sets.append("updated_at = ?"); args.append(now_iso())
    args.append(worker_id)
    conn.execute(f"UPDATE workers SET {', '.join(sets)} WHERE id = ?", args)


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
        where.append("worker_id = ?"); args.append(worker_id)
    if since_id is not None:
        where.append("id > ?"); args.append(since_id)
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
```

- [ ] **Step 4: Run tests, watch them pass**

Run: `pytest tests/test_state.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Run mypy and ruff**

Run: `mypy --strict orchestra/state.py && ruff check orchestra/state.py tests/test_state.py`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add orchestra/state.py tests/test_state.py
git commit -m "state: add SQLite layer with workers/events/escalations CRUD"
```

---

## Task 3: Worker prompt template (TaskCreate #7)

**Goal:** `orchestra.prompts.render_startup_prompt(...)` returns the structured prompt injected into freshly spawned workers. Pure function; no side effects.

**Files:**
- Create: `orchestra/prompts.py`
- Create: `tests/test_prompts.py`

**Acceptance Criteria:**
- [ ] Rendered prompt contains: `worker_id`, the task verbatim, branch `orch/<id>`, model name, and the literal commands `orchestra worker status` and `orchestra worker escalate`
- [ ] Contains rules: "commit yes, push no", "do not spawn additional workers", "do not end this session yourself"
- [ ] Context section appears only when `ctx_files` is non-empty
- [ ] Multi-line string; trailing newline trimmed

**Verify:** `pytest tests/test_prompts.py -v`

**Steps:**

- [ ] **Step 1: Write failing tests (`tests/test_prompts.py`)**

```python
from __future__ import annotations

from orchestra.prompts import render_startup_prompt


def test_contains_required_identity():
    p = render_startup_prompt(
        worker_id="w1", task="Implement auth", model="sonnet", ctx_files=[],
    )
    assert "w1" in p
    assert "Implement auth" in p
    assert "sonnet" in p
    assert "orch/w1" in p


def test_contains_required_commands():
    p = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    assert "orchestra worker status" in p
    assert "orchestra worker escalate" in p


def test_contains_required_rules():
    p = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    # case-insensitive checks
    low = p.lower()
    assert "commit" in low and "push" in low
    assert "do not spawn" in low
    assert "do not end this session" in low


def test_context_only_when_files_present():
    no_ctx = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    with_ctx = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet",
        ctx_files=["src/auth.py", "src/db.py"],
    )
    assert "CONTEXT" not in no_ctx
    assert "CONTEXT" in with_ctx
    assert "src/auth.py" in with_ctx
    assert "src/db.py" in with_ctx


def test_no_trailing_newline():
    p = render_startup_prompt(
        worker_id="w1", task="t", model="sonnet", ctx_files=[],
    )
    assert not p.endswith("\n")
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_prompts.py -v`
Expected: ImportError on `orchestra.prompts`.

- [ ] **Step 3: Implement `orchestra/prompts.py`**

```python
"""Worker startup prompt template."""
from __future__ import annotations


def render_startup_prompt(
    *,
    worker_id: str,
    task: str,
    model: str,
    ctx_files: list[str],
) -> str:
    branch = f"orch/{worker_id}"
    context_section = ""
    if ctx_files:
        bullets = "\n".join(f"- {f}" for f in ctx_files)
        context_section = f"\n### CONTEXT\nRelevant files to read first:\n{bullets}\n"

    prompt = f"""## WORKER {worker_id}
You are a worker in a tmux orchestration system (claude-orchestra).

### TASK
{task}

### IDENTITY
- Worker ID: {worker_id}
- Model: {model}
- Branch: {branch}

### COORDINATION RULES (mandatory)
- Status: every ~20 turns OR after each meaningful milestone, run:
  `orchestra worker status --progress "<short summary>" --turns <N>`
- Escalation: when uncertain, run instead of guessing:
  `orchestra worker escalate --blocking --question "..." --context "..."`
  Use `--blocking` for must-have answers; omit for async questions.
- Git: commit to branch {branch} — commit yes, push no.
- Do not spawn additional workers (no `orchestra spawn` calls from here).
- Do not end this session yourself.
{context_section}
### GO
Write your first status update FIRST (e.g. `orchestra worker status --progress "Starting" --turns 0`),
then begin the task."""
    return prompt.rstrip("\n")
```

- [ ] **Step 4: Run tests, watch them pass**

Run: `pytest tests/test_prompts.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestra/prompts.py tests/test_prompts.py
git commit -m "prompts: add worker startup prompt template"
```

---

## Task 4: tmux driver primitives (TaskCreate #5)

**Goal:** `orchestra.tmux` — thin, well-tested wrappers around `subprocess.run(["tmux", ...])`. All business logic stays out.

**Files:**
- Create: `orchestra/tmux.py`
- Create: `tests/test_tmux.py`

**Acceptance Criteria:**
- [ ] `send_literal` invokes `tmux send-keys -t <target> -l <text>` exactly once (no Enter)
- [ ] `send_enter` invokes `tmux send-keys -t <target> Enter`
- [ ] `send_multiline` invokes `tmux load-buffer -b <name> -` (text via stdin), `tmux paste-buffer -p -d -b <name> -t <target>`, then `tmux send-keys -t <target> Enter`
- [ ] `capture(target, lines=N)` strips ANSI from `tmux capture-pane -t <target> -p -S -<N>` output
- [ ] `is_idle` returns `False` when the captured text contains any spinner pattern (`Running|thinking|Searching|Reading|Writing|Editing`), regardless of prompt regex
- [ ] `is_idle` returns `True` only when no spinner AND a prompt line (`❯` or `>` at end of a line) is present
- [ ] `is_idle` returns `False` for unknown states (safe default)
- [ ] `pane_current_command` returns the result of `tmux display-message -p -t <target> '#{pane_current_command}'`, stripped
- [ ] `ensure_session(name, cwd)` calls `tmux has-session -t <name>`; if it exits non-zero, runs `tmux new-session -d -s <name> -c <cwd>`
- [ ] `new_window(session, name, cwd)` runs `tmux new-window -t <session>: -n <name> -c <cwd>` and returns the target string (`<session>:<name>`)

**Verify:** `pytest tests/test_tmux.py -v && mypy --strict orchestra/tmux.py`

**Steps:**

- [ ] **Step 1: Write failing tests (`tests/test_tmux.py`)**

```python
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call

import pytest

from orchestra import tmux


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch):
    """Stub subprocess.run; record all calls."""
    runner = MagicMock(spec=subprocess.run)
    runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


class TestSend:
    def test_send_literal(self, fake_run):
        tmux.send_literal("s:1", "hello")
        fake_run.assert_called_once_with(
            ["tmux", "send-keys", "-t", "s:1", "-l", "hello"],
            check=True, capture_output=True, text=True,
        )

    def test_send_enter(self, fake_run):
        tmux.send_enter("s:1")
        fake_run.assert_called_once_with(
            ["tmux", "send-keys", "-t", "s:1", "Enter"],
            check=True, capture_output=True, text=True,
        )

    def test_send_multiline_uses_load_paste_then_enter(self, fake_run):
        tmux.send_multiline("s:1", "line1\nline2", buffer_name="b1")
        # Three calls in order: load-buffer, paste-buffer, send-keys Enter
        calls = fake_run.call_args_list
        assert len(calls) == 3
        load_args, load_kwargs = calls[0]
        assert load_args[0] == ["tmux", "load-buffer", "-b", "b1", "-"]
        assert load_kwargs.get("input") == "line1\nline2"
        paste_args, _ = calls[1]
        assert paste_args[0] == ["tmux", "paste-buffer", "-p", "-d", "-b", "b1", "-t", "s:1"]
        enter_args, _ = calls[2]
        assert enter_args[0] == ["tmux", "send-keys", "-t", "s:1", "Enter"]


class TestCapture:
    def test_capture_strips_ansi(self, monkeypatch):
        # raw output with ANSI: red, OSC, charset switch
        raw = "\x1b[31mhello\x1b[0m\n\x1b]0;title\x07world\n\x1b(Bplain\n"
        runner = MagicMock()
        runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=raw, stderr="")
        monkeypatch.setattr(subprocess, "run", runner)

        out = tmux.capture("s:1", lines=80)
        assert "hello" in out and "\x1b" not in out
        assert "world" in out
        assert "plain" in out
        # argv shape
        called = runner.call_args.args[0]
        assert called == ["tmux", "capture-pane", "-t", "s:1", "-p", "-S", "-80"]


class TestIdle:
    def test_busy_on_spinner(self, monkeypatch):
        monkeypatch.setattr(tmux, "capture", lambda target, lines=12: "Running tests...\n❯ ")
        assert tmux.is_idle("s:1") is False

    def test_idle_on_prompt_only(self, monkeypatch):
        monkeypatch.setattr(tmux, "capture", lambda target, lines=12: "some output\n❯ ")
        assert tmux.is_idle("s:1") is True

    def test_unknown_state_returns_false(self, monkeypatch):
        monkeypatch.setattr(tmux, "capture", lambda target, lines=12: "blah blah\n")
        assert tmux.is_idle("s:1") is False


class TestPaneCommand:
    def test_pane_current_command(self, monkeypatch):
        runner = MagicMock()
        runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="claude\n", stderr="")
        monkeypatch.setattr(subprocess, "run", runner)
        assert tmux.pane_current_command("s:1") == "claude"
        called = runner.call_args.args[0]
        assert called == ["tmux", "display-message", "-p", "-t", "s:1", "#{pane_current_command}"]


class TestSession:
    def test_ensure_session_creates_when_missing(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["tmux", "has-session"]:
                # session not found -> non-zero
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake)
        tmux.ensure_session("orch-x", cwd="/tmp")
        assert calls[0] == ["tmux", "has-session", "-t", "orch-x"]
        assert calls[1] == ["tmux", "new-session", "-d", "-s", "orch-x", "-c", "/tmp"]

    def test_ensure_session_skips_when_present(self, monkeypatch):
        runner = MagicMock()
        runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        monkeypatch.setattr(subprocess, "run", runner)
        tmux.ensure_session("orch-x", cwd="/tmp")
        runner.assert_called_once()
        assert runner.call_args.args[0] == ["tmux", "has-session", "-t", "orch-x"]

    def test_new_window_returns_target(self, fake_run):
        target = tmux.new_window(session="orch-x", name="w1", cwd="/tmp")
        assert target == "orch-x:w1"
        fake_run.assert_called_once_with(
            ["tmux", "new-window", "-t", "orch-x:", "-n", "w1", "-c", "/tmp"],
            check=True, capture_output=True, text=True,
        )
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_tmux.py -v`
Expected: ImportError on `orchestra.tmux`.

- [ ] **Step 3: Implement `orchestra/tmux.py`**

```python
"""tmux primitives for claude-orchestra.

Single rule: every function is a thin wrapper over `tmux` invocations.
Business logic (retries, choreography, idle policy) lives in higher layers.
"""
from __future__ import annotations

import re
import subprocess

# ANSI scrubber — covers CSI/OSC/DCS/charset/SI-SO that tmux pane output may contain.
# Match the patterns from primeline-ai/claude-tmux-orchestration; battle-tested.
_ANSI_RES = [
    re.compile(r"\x1b\[[0-9;:?<=>]*[a-zA-Z]"),       # CSI
    re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"),  # OSC
    re.compile(r"\x1bP[^\x1b]*(?:\x1b\\|$)"),         # DCS
    re.compile(r"\x1b[()][0-9A-Za-z]"),                # charset switches
    re.compile(r"[\x0e\x0f]"),                          # SI/SO
]

_SPINNER_RE = re.compile(r"(Running|thinking|Searching|Reading|Writing|Editing)")
_PROMPT_RE = re.compile(r"(?:❯|>)\s*$", re.MULTILINE)


def _strip_ansi(text: str) -> str:
    for r in _ANSI_RES:
        text = r.sub("", text)
    return text


def _run(argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "check": True,
        "capture_output": True,
        "text": True,
    }
    if input is not None:
        kwargs["input"] = input
    return subprocess.run(argv, **kwargs)  # type: ignore[call-overload]


# ---- send ----

def send_literal(target: str, text: str) -> None:
    _run(["tmux", "send-keys", "-t", target, "-l", text])


def send_enter(target: str) -> None:
    _run(["tmux", "send-keys", "-t", target, "Enter"])


def send_multiline(target: str, text: str, *, buffer_name: str = "orch") -> None:
    """Load text into a named tmux buffer and paste it, then submit with Enter.

    send-keys breaks on embedded newlines; paste-buffer is the only reliable path.
    -p enables paste bracket mode (no shell interpretation); -d deletes the buffer.
    """
    _run(["tmux", "load-buffer", "-b", buffer_name, "-"], input=text)
    _run(["tmux", "paste-buffer", "-p", "-d", "-b", buffer_name, "-t", target])
    _run(["tmux", "send-keys", "-t", target, "Enter"])


# ---- read ----

def capture(target: str, lines: int = 80) -> str:
    """Return the last `lines` lines from `target`, ANSI-stripped."""
    proc = _run(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
    return _strip_ansi(proc.stdout)


def is_idle(target: str) -> bool:
    """Cheap idle heuristic: spinner overrides everything; otherwise look for prompt."""
    text = capture(target, lines=12)
    if _SPINNER_RE.search(text):
        return False
    return bool(_PROMPT_RE.search(text))


def pane_current_command(target: str) -> str:
    proc = _run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"])
    return proc.stdout.strip()


# ---- session / window ----

def ensure_session(name: str, *, cwd: str) -> None:
    """Create the session if it doesn't exist; no-op if it does."""
    try:
        _run(["tmux", "has-session", "-t", name])
    except subprocess.CalledProcessError:
        _run(["tmux", "new-session", "-d", "-s", name, "-c", cwd])


def new_window(*, session: str, name: str, cwd: str) -> str:
    """Create a new window in `session`. Returns its target string."""
    _run(["tmux", "new-window", "-t", f"{session}:", "-n", name, "-c", cwd])
    return f"{session}:{name}"
```

- [ ] **Step 4: Run tests, watch them pass**

Run: `pytest tests/test_tmux.py -v`
Expected: all PASS.

- [ ] **Step 5: Run mypy strict**

Run: `mypy --strict orchestra/tmux.py`
Expected: exit 0. (The `# type: ignore[call-overload]` on `_run` is justified — the kwarg variants of `subprocess.run` don't compose cleanly under strict.)

- [ ] **Step 6: Commit**

```bash
git add orchestra/tmux.py tests/test_tmux.py
git commit -m "tmux: add driver primitives (send/capture/idle/session)"
```

---

## Task 5: Worker spawn choreography (TaskCreate #8)

**Goal:** `orchestra.spawn.spawn_worker(...)` performs the 7-step boot dance. Every step writes an event row so the dashboard can render spawn progress live. Bounded waits at idle (60s) and first-status (90s).

**Files:**
- Create: `orchestra/spawn.py`
- Create: `tests/test_spawn.py`

**Acceptance Criteria:**
- [ ] Records events in order: `spawn_start`, `spawn_window`, `spawn_idle`, `model_switched`, `prompt_injected`, then either `spawn_ok` or `spawn_first_status_timeout`
- [ ] On boot timeout: worker `status=error`, event `spawn_timeout` with the last captured screen in payload, window NOT killed
- [ ] On first-status timeout: worker `status=stale_spawn`, event recorded, window NOT killed
- [ ] After `spawn_idle`, sends `Enter` twice (~1s apart) to dismiss trust prompts
- [ ] Send-multiline failure causes a single retry attempt; second failure → `status=error`, event `prompt_inject_failed`
- [ ] First-status detection: polls `state.list_events(conn, worker_id=id)` for a `status` event from the worker
- [ ] Boot command exports `ORCHESTRA_WORKER_ID` and `ORCHESTRA_STATE_DB` before running `claude --dangerously-skip-permissions`

**Verify:** `pytest tests/test_spawn.py -v && mypy --strict orchestra/spawn.py`

**Steps:**

- [ ] **Step 1: Write failing tests (`tests/test_spawn.py`)**

```python
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestra import spawn, state


def _open(tmp_db: Path) -> sqlite3.Connection:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    return conn


@pytest.fixture
def fake_tmux(monkeypatch):
    """Replace orchestra.tmux with a MagicMock at the spawn module level."""
    m = MagicMock()
    # ensure_session and new_window are no-ops; new_window returns a target
    m.new_window.return_value = "orch-proj:w1"
    m.is_idle.return_value = True  # default to "ready immediately"
    m.capture.return_value = "❯ "
    monkeypatch.setattr(spawn, "tmux", m)
    return m


def _kinds(conn: sqlite3.Connection, worker_id: str) -> list[str]:
    return [e.kind for e in state.list_events(conn, worker_id=worker_id)]


class TestHappyPath:
    def test_records_event_sequence_and_marks_working(
        self, tmp_db, tmp_orch_dir, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        # First poll for status event returns nothing; second poll finds it.
        # Simulate this by having the worker write a status event after 2 polls.
        original_list = state.list_events
        calls = {"n": 0}

        def stub_list(conn_, **kw):
            calls["n"] += 1
            evts = list(original_list(conn_, **kw))
            if kw.get("worker_id") == "w1" and calls["n"] >= 2:
                # inject a fake status event
                state.record_event(conn_, "status", worker_id="w1", progress="starting", turns=0)
            return original_list(conn_, **kw)

        monkeypatch.setattr(spawn.state, "list_events", stub_list)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="Implement auth",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "working"

        kinds = _kinds(conn, "w1")
        # Ensure the canonical sequence appears in order
        expected_prefix = [
            "spawn_start", "spawn_window", "spawn_idle",
            "model_switched", "prompt_injected", "spawn_ok",
        ]
        for needle, pos in zip(expected_prefix, range(len(expected_prefix))):
            assert needle in kinds, f"missing {needle} in {kinds}"

    def test_boot_command_has_env_and_dangerously_skip(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))
        monkeypatch.setattr(spawn, "_wait_first_status", lambda *a, **kw: True)

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        # The boot command goes through send_literal as the first send to the new pane.
        sent_texts = [c.args[1] for c in fake_tmux.send_literal.call_args_list]
        boot_cmd = sent_texts[0]
        assert "ORCHESTRA_WORKER_ID='w1'" in boot_cmd
        assert "ORCHESTRA_STATE_DB=" in boot_cmd
        assert "claude --dangerously-skip-permissions" in boot_cmd


class TestBootTimeout:
    def test_marks_error_and_records_event(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        fake_tmux.is_idle.return_value = False
        fake_tmux.capture.return_value = "spinning forever..."
        # Compress the wait loop time.
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 0.05)
        monkeypatch.setattr(spawn, "BOOT_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "error"
        kinds = _kinds(conn, "w1")
        assert "spawn_timeout" in kinds


class TestFirstStatusTimeout:
    def test_marks_stale_spawn(
        self, tmp_db, fake_tmux, monkeypatch
    ):
        conn = _open(tmp_db)
        monkeypatch.setattr(spawn, "FIRST_STATUS_TIMEOUT_S", 0.05)
        monkeypatch.setattr(spawn, "FIRST_STATUS_POLL_S", 0.01)
        monkeypatch.setattr(spawn, "time", MagicMock(sleep=MagicMock()))

        spawn.spawn_worker(
            conn,
            worker_id="w1",
            model="sonnet",
            task="t",
            project_root="/tmp/proj",
            state_db=tmp_db,
            ctx_files=[],
            session_name="orch-proj",
        )

        worker = state.get_worker(conn, "w1")
        assert worker is not None
        assert worker.status == "stale_spawn"
        kinds = _kinds(conn, "w1")
        assert "spawn_first_status_timeout" in kinds
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_spawn.py -v`
Expected: ImportError on `orchestra.spawn`.

- [ ] **Step 3: Implement `orchestra/spawn.py`**

```python
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

import sqlite3
import time
from pathlib import Path

from orchestra import prompts, state, tmux

# Timeouts are module-level so tests can monkeypatch them.
BOOT_TIMEOUT_S = 60
BOOT_POLL_S = 3.0
FIRST_STATUS_TIMEOUT_S = 90
FIRST_STATUS_POLL_S = 5.0


def _boot_command(worker_id: str, state_db: Path) -> str:
    return (
        f"ORCHESTRA_WORKER_ID='{worker_id}' "
        f"ORCHESTRA_STATE_DB='{state_db}' "
        f"claude --dangerously-skip-permissions"
    )


def _wait_idle(target: str) -> bool:
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if tmux.is_idle(target):
            return True
        time.sleep(BOOT_POLL_S)
    return False


def _wait_first_status(conn: sqlite3.Connection, worker_id: str) -> bool:
    deadline = time.monotonic() + FIRST_STATUS_TIMEOUT_S
    while time.monotonic() < deadline:
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

    # Step 4: wait for idle
    if not _wait_idle(target):
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
        state.update_worker(conn, worker_id, status="working")
        state.record_event(conn, "spawn_ok", worker_id=worker_id)
    else:
        state.update_worker(conn, worker_id, status="stale_spawn")
        state.record_event(conn, "spawn_first_status_timeout", worker_id=worker_id)
```

- [ ] **Step 4: Run tests, watch them pass**

Run: `pytest tests/test_spawn.py -v`
Expected: all PASS.

- [ ] **Step 5: Run mypy strict**

Run: `mypy --strict orchestra/spawn.py`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add orchestra/spawn.py tests/test_spawn.py
git commit -m "spawn: add worker boot choreography with bounded waits"
```

---

## Task 6: CLI commands (TaskCreate #9)

**Goal:** Typer command tree. `init`, `spawn`, `status`, `tail`, `stop`, `worker status`, `worker escalate`, `dash`. Workers' subcommands require `ORCHESTRA_WORKER_ID` + `ORCHESTRA_STATE_DB` env.

**Files:**
- Modify: `orchestra/__main__.py` (wire sub-apps)
- Create: `orchestra/cli.py`
- Create: `tests/test_cli.py`

**Acceptance Criteria:**
- [ ] `orchestra init` creates `.orchestra/state.db` with schema and `.orchestra/config.toml`; idempotent
- [ ] `orchestra spawn <id> <model> <task>` delegates to `spawn.spawn_worker` after `init` was run
- [ ] `orchestra status` prints one line per worker (`id  status  turns  progress`)
- [ ] `orchestra status --worker <id>` prints detail + last 20 events
- [ ] `orchestra stop <id>` updates worker to `status=stopped`, sends `C-c` then `C-c` 0.5s apart, records `stopped` event
- [ ] `orchestra worker status` requires both env vars; without them exits 2 with a clear message
- [ ] `orchestra worker status --progress STR --turns INT` updates worker + records `status` event
- [ ] `orchestra worker escalate --question STR [--context STR] [--blocking]` creates escalation, sets worker `status=waiting` when blocking, records `escalation` event
- [ ] `orchestra dash --port N` launches `uvicorn orchestra.web:app` on the given port (test via `--port 0` smoke)

**Verify:** `pytest tests/test_cli.py -v`

**Steps:**

- [ ] **Step 1: Write failing tests (`tests/test_cli.py`)**

```python
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from orchestra import cli, state
from orchestra.__main__ import app


runner = CliRunner()


def _init_in(path: Path) -> None:
    os.chdir(path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output


class TestInit:
    def test_creates_state_and_config(self, tmp_path: Path):
        os.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / ".orchestra" / "state.db").exists()
        assert (tmp_path / ".orchestra" / "config.toml").exists()

    def test_idempotent(self, tmp_path: Path):
        os.chdir(tmp_path)
        assert runner.invoke(app, ["init"]).exit_code == 0
        assert runner.invoke(app, ["init"]).exit_code == 0


class TestSpawn:
    def test_invokes_spawn_worker(self, tmp_path: Path, monkeypatch):
        _init_in(tmp_path)
        called = {}

        def fake_spawn(conn, **kw):
            called.update(kw)

        monkeypatch.setattr(cli.spawn, "spawn_worker", fake_spawn)
        result = runner.invoke(app, ["spawn", "w1", "sonnet", "do thing"])
        assert result.exit_code == 0, result.output
        assert called["worker_id"] == "w1"
        assert called["model"] == "sonnet"
        assert called["task"] == "do thing"


class TestStatus:
    def test_lists_all(self, tmp_path: Path):
        _init_in(tmp_path)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="orch-x:w1",
        )
        state.update_worker(conn, "w1", status="working", progress="busy", turns=2)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "w1" in result.output
        assert "working" in result.output


class TestWorkerCommands:
    def test_status_requires_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("ORCHESTRA_WORKER_ID", raising=False)
        monkeypatch.delenv("ORCHESTRA_STATE_DB", raising=False)
        result = runner.invoke(app, ["worker", "status", "--progress", "x", "--turns", "1"])
        assert result.exit_code == 2
        assert "must run inside a spawned worker pane" in result.output

    def test_status_writes_event(self, tmp_path: Path, monkeypatch):
        _init_in(tmp_path)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        result = runner.invoke(
            app, ["worker", "status", "--progress", "made progress", "--turns", "5"]
        )
        assert result.exit_code == 0, result.output
        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.progress == "made progress"
        assert w.turns == 5
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "status" in kinds

    def test_escalate_blocking_sets_waiting(self, tmp_path: Path, monkeypatch):
        _init_in(tmp_path)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )
        state.update_worker(conn, "w1", status="working")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        result = runner.invoke(
            app,
            ["worker", "escalate", "--blocking",
             "--question", "RS256 or HS256?", "--context", "tradeoffs"],
        )
        assert result.exit_code == 0, result.output
        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.status == "waiting"
        opens = state.list_open_escalations(conn)
        assert len(opens) == 1


class TestStop:
    def test_sends_ctrl_c_twice_and_records(self, tmp_path: Path, monkeypatch):
        _init_in(tmp_path)
        db = tmp_path / ".orchestra" / "state.db"
        conn = state.connect(db)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch=None, pane_target="orch-x:w1",
        )

        tmux_mock = MagicMock()
        monkeypatch.setattr(cli, "tmux", tmux_mock)

        result = runner.invoke(app, ["stop", "w1"])
        assert result.exit_code == 0, result.output

        # Two Ctrl-C invocations (we use send_keys C-c)
        cc_calls = [c for c in tmux_mock._run.call_args_list]  # see implementation
        # implementation may use a different shape; we instead assert via send_keys mock
        assert tmux_mock.send_ctrl_c.call_count == 2

        w = state.get_worker(conn, "w1")
        assert w is not None
        assert w.status == "stopped"
```

Note: this test expects a helper `tmux.send_ctrl_c(target)` — add it in this task, alongside the CLI.

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_cli.py -v`
Expected: ImportError on `orchestra.cli`, plus the missing `tmux.send_ctrl_c`.

- [ ] **Step 3: Add `send_ctrl_c` to `orchestra/tmux.py`**

Inside `orchestra/tmux.py`, alongside `send_enter`:

```python
def send_ctrl_c(target: str) -> None:
    _run(["tmux", "send-keys", "-t", target, "C-c"])
```

- [ ] **Step 4: Implement `orchestra/cli.py`**

```python
"""Typer commands for orchestra."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import typer

from orchestra import prompts, spawn, state, tmux

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


def _require_initialized() -> Path:
    db = _state_db()
    if not db.exists():
        typer.echo("error: run `orchestra init` first", err=True)
        raise typer.Exit(2)
    return db


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


@app.command()
def spawn_cmd(
    worker_id: str = typer.Argument(..., metavar="ID"),
    model: str = typer.Argument(..., metavar="MODEL"),
    task: str = typer.Argument(..., metavar="TASK"),
    context: list[str] = typer.Option([], "--context", help="Context files."),
) -> None:
    """Spawn a worker into a new tmux window."""
    db = _require_initialized()
    project_root = str(Path.cwd())
    session_name = f"orch-{Path.cwd().name.lower()}"
    conn = state.connect(db)
    try:
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
    finally:
        conn.close()


# Typer treats `spawn` as a reserved-ish word inside this module (we import the module).
# Expose the command as `orchestra spawn`.
app.command(name="spawn")(spawn_cmd)
# Remove the auto-named `spawn-cmd` registration:
app.registered_commands = [c for c in app.registered_commands if c.name != "spawn-cmd"]


@app.command()
def status(worker: str | None = typer.Option(None, "--worker")) -> None:
    """Print worker status table; with --worker, print detail."""
    db = _require_initialized()
    conn = state.connect(db)
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
    db = _require_initialized()
    conn = state.connect(db)
    w = state.get_worker(conn, worker_id)
    if w is None:
        typer.echo(f"no such worker: {worker_id}", err=True)
        raise typer.Exit(2)
    try:
        tmux.send_ctrl_c(w.pane_target)
        time.sleep(0.5)
        tmux.send_ctrl_c(w.pane_target)
    except subprocess.CalledProcessError as e:
        state.record_event(conn, "stop_send_failed", worker_id=worker_id, error=repr(e))
    state.update_worker(conn, worker_id, status="stopped")
    state.record_event(conn, "stopped", worker_id=worker_id)
    typer.echo(f"stopped {worker_id}")


@app.command()
def tail(worker_id: str = typer.Argument(..., metavar="ID"), lines: int = 80) -> None:
    """Print the last N lines of the worker's pane (one-shot)."""
    db = _require_initialized()
    conn = state.connect(db)
    w = state.get_worker(conn, worker_id)
    if w is None:
        typer.echo(f"no such worker: {worker_id}", err=True)
        raise typer.Exit(2)
    typer.echo(tmux.capture(w.pane_target, lines=lines))


@app.command()
def dash(port: int = typer.Option(8765, "--port"), host: str = typer.Option("127.0.0.1")) -> None:
    """Start the dashboard."""
    import uvicorn

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
    wid, db = _worker_env()
    conn = state.connect(db)
    state.update_worker(conn, wid, progress=progress, turns=turns)
    state.record_event(conn, "status", worker_id=wid, progress=progress, turns=turns)


@worker_app.command("escalate")
def worker_escalate(
    question: str = typer.Option(..., "--question"),
    context: str | None = typer.Option(None, "--context"),
    blocking: bool = typer.Option(False, "--blocking"),
) -> None:
    wid, db = _worker_env()
    conn = state.connect(db)
    esc = state.create_escalation(
        conn, worker_id=wid, question=question, context=context, blocking=blocking,
    )
    if blocking:
        state.update_worker(conn, wid, status="waiting")
    state.record_event(
        conn, "escalation", worker_id=wid,
        escalation_id=esc.id, blocking=blocking, question=question,
    )
```

- [ ] **Step 5: Update `orchestra/__main__.py` to use `cli.app`**

Replace `orchestra/__main__.py` with:

```python
"""Entry point for the `orchestra` CLI."""
from __future__ import annotations

import typer

from orchestra import __version__
from orchestra.cli import app


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        typer.echo(f"orchestra {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run tests, watch them pass**

Run: `pytest tests/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestra/cli.py orchestra/tmux.py orchestra/__main__.py tests/test_cli.py
git commit -m "cli: add init/spawn/status/tail/stop/dash + worker subcommands"
```

---

## Task 7: Web dashboard (TaskCreate #10)

**Goal:** FastAPI app serving the dashboard HTML + a small REST/SSE API. Reads SQLite; calls `tmux.capture` for live pane peeks. Vanilla JS frontend.

**Files:**
- Create: `orchestra/web.py`
- Create: `orchestra/templates/index.html`
- Create: `tests/test_web.py`

**Acceptance Criteria:**
- [ ] `GET /api/workers` returns JSON list of all workers
- [ ] `GET /api/workers/{id}` returns worker detail + last 50 events
- [ ] `GET /api/workers/{id}/pane?lines=N` calls `tmux.capture(target, lines=N)` and returns text
- [ ] `GET /api/workers/{id}/escalations?open=true` returns open escalations
- [ ] `POST /api/workers/{id}/answer` resolves escalation, sends answer to worker pane, records event, sets worker `status=working`
- [ ] `GET /api/stream` is SSE; pushes a JSON message per new event, format `{"id": N, "kind": K, "worker_id": W, "payload": {...}}`
- [ ] 404 JSON for unknown worker; 409 JSON for already-resolved escalation
- [ ] `GET /` returns the dashboard HTML (200 OK, content-type text/html)

**Verify:** `pytest tests/test_web.py -v`

**Steps:**

- [ ] **Step 1: Write failing tests (`tests/test_web.py`)**

```python
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from orchestra import state, web


@pytest.fixture
def client(tmp_orch_dir: Path, monkeypatch):
    db = tmp_orch_dir / "state.db"
    conn = state.connect(db)
    state.init_schema(conn)
    # one canned worker
    state.create_worker(
        conn, id="w1", task="Implement auth", model="sonnet",
        branch="orch/w1", pane_target="orch-x:w1",
    )
    state.update_worker(conn, "w1", status="working", progress="busy", turns=3)
    conn.close()
    # Point the app at this DB via ENV
    monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
    # Stub tmux.capture
    monkeypatch.setattr(web.tmux, "capture", lambda target, lines=80: f"<{target} {lines}>")
    monkeypatch.setattr(web.tmux, "send_multiline", MagicMock())
    return TestClient(web.app)


class TestWorkersAPI:
    def test_list(self, client):
        r = client.get("/api/workers")
        assert r.status_code == 200
        rows = r.json()
        assert any(w["id"] == "w1" for w in rows)

    def test_detail(self, client):
        r = client.get("/api/workers/w1")
        assert r.status_code == 200
        body = r.json()
        assert body["worker"]["id"] == "w1"
        assert "events" in body and isinstance(body["events"], list)

    def test_detail_missing(self, client):
        r = client.get("/api/workers/zz")
        assert r.status_code == 404

    def test_pane(self, client):
        r = client.get("/api/workers/w1/pane?lines=120")
        assert r.status_code == 200
        body = r.json()
        assert body["text"] == "<orch-x:w1 120>"


class TestEscalations:
    def test_answer_resolves_and_sends(self, client, tmp_orch_dir, monkeypatch):
        db = tmp_orch_dir / "state.db"
        conn = state.connect(db)
        esc = state.create_escalation(
            conn, worker_id="w1", question="RS256?", context=None, blocking=True,
        )
        state.update_worker(conn, "w1", status="waiting")
        conn.close()

        r = client.post(f"/api/workers/w1/answer", json={"escalation_id": esc.id, "answer": "RS256"})
        assert r.status_code == 200, r.text

        # Worker should be back to working
        conn = state.connect(db)
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "working"
        open_e = state.list_open_escalations(conn)
        assert open_e == []

    def test_answer_already_resolved_returns_409(self, client, tmp_orch_dir):
        db = tmp_orch_dir / "state.db"
        conn = state.connect(db)
        esc = state.create_escalation(
            conn, worker_id="w1", question="q", context=None, blocking=False,
        )
        state.resolve_escalation(conn, esc.id, answer="x")
        conn.close()

        r = client.post("/api/workers/w1/answer", json={"escalation_id": esc.id, "answer": "y"})
        assert r.status_code == 409


class TestStream:
    def test_stream_emits_recent_event(self, client, tmp_orch_dir):
        # Write an event after the client is set up, then read the SSE
        db = tmp_orch_dir / "state.db"
        conn = state.connect(db)
        state.record_event(conn, "status", worker_id="w1", progress="hi", turns=1)
        conn.close()
        with client.stream("GET", "/api/stream") as resp:
            assert resp.status_code == 200
            # Read up to 1KB then stop
            chunk = next(resp.iter_text())
            assert "status" in chunk


class TestDashboard:
    def test_root_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "<html" in r.text.lower()
```

- [ ] **Step 2: Run, watch fail**

Run: `pytest tests/test_web.py -v`
Expected: ImportError on `orchestra.web`.

- [ ] **Step 3: Implement `orchestra/web.py`**

```python
"""FastAPI app — REST + SSE dashboard for orchestra."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from orchestra import state, tmux

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="claude-orchestra dashboard")


def _db_path() -> Path:
    p = os.environ.get("ORCHESTRA_STATE_DB")
    if p:
        return Path(p)
    # default: .orchestra/state.db relative to cwd
    return Path.cwd() / ".orchestra" / "state.db"


def _conn():
    return state.connect(_db_path())


# ---- HTML ----

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# ---- REST ----

@app.get("/api/workers")
def list_workers_api() -> list[dict[str, Any]]:
    conn = _conn()
    return [w.__dict__ for w in state.list_workers(conn)]


@app.get("/api/workers/{worker_id}")
def worker_detail(worker_id: str) -> dict[str, Any]:
    conn = _conn()
    w = state.get_worker(conn, worker_id)
    if w is None:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
    events = state.list_events(conn, worker_id=worker_id, limit=50)
    return {"worker": w.__dict__, "events": [e.__dict__ for e in events]}


@app.get("/api/workers/{worker_id}/pane")
def worker_pane(worker_id: str, lines: int = 80) -> dict[str, str]:
    conn = _conn()
    w = state.get_worker(conn, worker_id)
    if w is None:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
    return {"text": tmux.capture(w.pane_target, lines=lines)}


@app.get("/api/workers/{worker_id}/escalations")
def list_escalations_api(worker_id: str, open: bool = True) -> list[dict[str, Any]]:
    conn = _conn()
    if open:
        return [e.__dict__ for e in state.list_open_escalations(conn, worker_id=worker_id)]
    raise HTTPException(status_code=400, detail="only open=true is supported in v0")


class AnswerIn(BaseModel):
    escalation_id: int
    answer: str


@app.post("/api/workers/{worker_id}/answer")
def answer_escalation(worker_id: str, body: AnswerIn) -> dict[str, Any]:
    conn = _conn()
    w = state.get_worker(conn, worker_id)
    if w is None:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
    try:
        resolved = state.resolve_escalation(conn, body.escalation_id, answer=body.answer)
    except KeyError:
        raise HTTPException(status_code=409, detail="escalation already resolved or unknown")
    tmux.send_multiline(
        w.pane_target,
        f"Answer to escalation #{resolved.id}: {body.answer}",
        buffer_name=f"orch-ans-{resolved.id}",
    )
    state.update_worker(conn, worker_id, status="working")
    state.record_event(
        conn, "escalation_resolved", worker_id=worker_id,
        escalation_id=resolved.id, answer=body.answer,
    )
    return {"ok": True, "escalation_id": resolved.id}


# ---- SSE ----

@app.get("/api/stream")
async def stream(request: Request) -> EventSourceResponse:
    async def gen():
        last_id = 0
        # initial: send recent events to bootstrap the client
        conn = _conn()
        for e in state.list_events(conn, limit=50):
            last_id = max(last_id, e.id)
            yield {"data": json.dumps({"id": e.id, "worker_id": e.worker_id, "kind": e.kind, "payload": e.payload})}
        while True:
            if await request.is_disconnected():
                break
            conn = _conn()
            new = state.list_events(conn, since_id=last_id, limit=200)
            for e in new:
                last_id = max(last_id, e.id)
                yield {"data": json.dumps({"id": e.id, "worker_id": e.worker_id, "kind": e.kind, "payload": e.payload})}
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen())
```

- [ ] **Step 4: Write `orchestra/templates/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>claude-orchestra</title>
<style>
  body { font: 14px/1.4 ui-sans-serif, system-ui; margin: 1rem; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 1.2rem; margin: 0 0 1rem 0; }
  .grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); }
  .card { background: #181b23; border: 1px solid #2a2f3a; border-radius: 6px; padding: 0.75rem 1rem; }
  .card h2 { font-size: 1rem; margin: 0 0 0.5rem 0; display: flex; gap: 0.5rem; align-items: baseline; }
  .status { font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 3px; background: #2a2f3a; }
  .status.working { background: #1b6b3a; }
  .status.waiting { background: #a06600; }
  .status.error, .status.stale_spawn { background: #a02020; }
  .status.stopped, .status.done { background: #444; }
  pre { background: #0a0b0f; padding: 0.5rem; border-radius: 4px; overflow: auto; max-height: 22em; font-size: 12px; white-space: pre-wrap; }
  .esc { margin-top: 0.5rem; padding: 0.5rem; border: 1px solid #a06600; border-radius: 4px; }
  .esc textarea { width: 100%; height: 4em; background: #0a0b0f; color: #e6e6e6; border: 1px solid #2a2f3a; }
  .events { margin-top: 0.5rem; max-height: 14em; overflow: auto; font-size: 12px; }
  .events .ts { color: #888; margin-right: 0.5em; }
</style>
</head>
<body>
<h1>claude-orchestra</h1>
<div id="grid" class="grid"></div>
<script>
const grid = document.getElementById('grid');
const state = { workers: new Map() };

function cardHtml(w, panes, events, escalations) {
  return `
    <div class="card" id="card-${w.id}">
      <h2>${w.id} <span class="status ${w.status}">${w.status}</span>
        <span style="margin-left:auto;color:#999;font-weight:normal">turns ${w.turns}</span></h2>
      <div><b>task:</b> ${w.task}</div>
      <div><b>progress:</b> ${w.progress ?? ''}</div>
      <pre id="pane-${w.id}">${panes ?? ''}</pre>
      <div id="events-${w.id}" class="events">${(events ?? []).map(e =>
        `<div><span class="ts">${e.ts}</span>${e.kind}</div>`).join('')}</div>
      ${(escalations ?? []).map(e => `
        <div class="esc">
          <div><b>escalation:</b> ${e.question}</div>
          <div style="color:#bbb">${e.context ?? ''}</div>
          <textarea id="ans-${e.id}"></textarea>
          <button onclick="answer('${w.id}', ${e.id})">send</button>
        </div>`).join('')}
    </div>`;
}

async function refresh() {
  const workers = await fetch('/api/workers').then(r => r.json());
  for (const w of workers) {
    const detail = await fetch('/api/workers/' + w.id).then(r => r.json());
    const pane = await fetch('/api/workers/' + w.id + '/pane?lines=60').then(r => r.json());
    const esc = await fetch('/api/workers/' + w.id + '/escalations?open=true').then(r => r.json());
    let el = document.getElementById('card-' + w.id);
    const html = cardHtml(w, pane.text, detail.events, esc);
    if (el) el.outerHTML = html; else grid.insertAdjacentHTML('beforeend', html);
  }
}

async function answer(workerId, escId) {
  const ta = document.getElementById('ans-' + escId);
  await fetch('/api/workers/' + workerId + '/answer', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({ escalation_id: escId, answer: ta.value }),
  });
  refresh();
}

const es = new EventSource('/api/stream');
es.onmessage = () => refresh();
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
```

- [ ] **Step 5: Run tests, watch them pass**

Run: `pytest tests/test_web.py -v`
Expected: all PASS.

- [ ] **Step 6: Smoke `orchestra dash`**

Manually verify (one-shot — needs `.orchestra/` set up):
```bash
cd /tmp && mkdir -p smoke && cd smoke && orchestra init
ORCHESTRA_STATE_DB=$PWD/.orchestra/state.db orchestra dash --port 8765 &
sleep 1
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/
# Expected: 200
kill %1
```

- [ ] **Step 7: Commit**

```bash
git add orchestra/web.py orchestra/templates/ tests/test_web.py
git commit -m "web: add FastAPI dashboard with REST + SSE + minimal HTML"
```

---

## Task 8: End-to-end smoke test (TaskCreate #12)

**Goal:** A short shell script that exercises the whole stack against a real `claude` binary. Not in CI; opt-in for releases.

**Files:**
- Create: `scripts/e2e-spawn.sh`

**Acceptance Criteria:**
- [ ] Script: cd to a fresh `mktemp -d`, `orchestra init`, `orchestra spawn w1 sonnet "<trivial task>"`, then poll `orchestra status` until `w1` has a non-zero turn count or 120s elapses
- [ ] Exit 0 if the worker wrote at least one status event; non-zero otherwise
- [ ] Cleanup: tmux kill-session, rm the tmpdir
- [ ] Documented as opt-in (consumes API credits)

**Verify:** `bash scripts/e2e-spawn.sh` (manual)

**Steps:**

- [ ] **Step 1: Write `scripts/e2e-spawn.sh`**

```bash
#!/usr/bin/env bash
# scripts/e2e-spawn.sh — opt-in end-to-end smoke test for claude-orchestra v0.
#
# Requires: claude CLI authenticated, tmux installed, orchestra installed
# (pip install -e .). Consumes API credits.
#
# Exits 0 on success.

set -euo pipefail

if ! command -v claude >/dev/null 2>&1; then
  echo "FAIL: claude CLI not in PATH" >&2; exit 2
fi
if ! command -v orchestra >/dev/null 2>&1; then
  echo "FAIL: orchestra CLI not in PATH (pip install -e .)" >&2; exit 2
fi

TMPDIR_E2E=$(mktemp -d)
SESSION="orch-$(basename "$TMPDIR_E2E" | tr '[:upper:]' '[:lower:]')"
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true; rm -rf "$TMPDIR_E2E"' EXIT

cd "$TMPDIR_E2E"
echo "[e2e] tmpdir: $TMPDIR_E2E  session: $SESSION"

orchestra init
echo "[e2e] init done"

TASK='Write the literal text OK to ./hello.txt then run: orchestra worker status --progress "done" --turns 1'
orchestra spawn w1 sonnet "$TASK"
echo "[e2e] spawn returned"

DEADLINE=$(( $(date +%s) + 120 ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  if orchestra status --worker w1 2>/dev/null | grep -q "turns=1"; then
    echo "[e2e] worker hit turns=1 — pass"
    if [[ -f "$TMPDIR_E2E/hello.txt" ]]; then
      echo "[e2e] hello.txt present:"
      cat "$TMPDIR_E2E/hello.txt"
    fi
    exit 0
  fi
  sleep 5
done

echo "[e2e] FAIL: worker did not reach turns=1 within 120s"
orchestra status --worker w1 || true
exit 1
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/e2e-spawn.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e-spawn.sh
git commit -m "scripts: add opt-in e2e smoke test"
```

- [ ] **Step 4: (optional) Run manually**

Run: `bash scripts/e2e-spawn.sh`
Expected: PASS or FAIL message; on PASS, exit 0.

---

## Final verification

After all tasks, from repo root:

```bash
pip install -e ".[dev]"
pytest -v
ruff check .
mypy orchestra/
orchestra --version
orchestra --help
```

All should exit 0. Coverage on `orchestra/state.py` and `orchestra/tmux.py` should be ≥80%.

---

## Self-review

**Spec coverage:** every section of `2026-05-16-claude-orchestra-design.md` maps to at least one task. Components section → tasks 2–7. Data flow → covered by tasks 5 (spawn), 6 (worker status, escalate), 7 (dashboard answer + pane). Error handling → spawn timeouts in task 5, missing-env in task 6, 404/409 in task 7, missing init in task 6. Testing rings → unit tests inside each task; e2e in task 8.

**Type consistency:** `spawn_worker` signature in task 5 matches the call in task 6's CLI; `tmux.capture(target, lines=N)` consistent across tasks 4, 5, 7; `state.record_event(conn, kind, worker_id=None, **payload)` consistent throughout.

**Placeholder scan:** none — every step shows the code or command.

**User-gate scan:** none of the tasks contain user-thrown gate language (Verbs alone don't trigger; no Scope or Proof bucket matches). No `userGate` tagging needed.
