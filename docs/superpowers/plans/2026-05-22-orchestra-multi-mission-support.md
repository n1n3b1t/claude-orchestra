# Multi-Mission Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `orchestra` treat a mission as a first-class entity so users can run missions sequentially without manual SQL, browse mission history, and reuse engineer names across missions without git collisions.

**Architecture:** New `missions` table in `state.db`, new `mission_id` column on `workers`, new `orchestra mission` CLI command group, `orchestra run` lifecycle wires the mission row, worktrees and engineer-spawn inherit the running mission's slug to namespace branches + paths.

**Tech Stack:** Python 3.10+, SQLite (existing state.db), typer CLI, FastAPI (dashboard), pytest. No new dependencies.

**Source spec:** `docs/superpowers/specs/2026-05-22-orchestra-multi-mission-support-design.md`

---

## File Structure

**State layer**
- Modify `orchestra/state.py` — add `missions` table to `SCHEMA`, add `mission_id` column to `workers` via forward-compat ALTER, add `Mission` dataclass, add CRUD: `create_mission`, `get_mission_by_slug`, `get_running_mission`, `list_missions`, `update_mission`. Migration backfills a `legacy-*` mission row when an old DB has worker rows but no mission rows.

**Mission scaffolding**
- Create `orchestra/missions.py` — slug validation (regex + collision check helper), `scaffold_mission_dir(project_root, slug)` that writes templated files into `missions/<slug>/`.

**CLI surface**
- Modify `orchestra/cli.py` — flesh out the existing `mission` command group stub at `cli.py:298` with `new`, `list`, `show`, `run` subcommands. (`mission lint` already exists and stays.)

**Run lifecycle**
- Modify `orchestra/run.py:run_mission` — sequential gate, slug resolution from path, mission row create/update across the lifecycle.

**Worktree + spawn**
- Modify `orchestra/worktree.py:add` and `:remove` — namespace path + branch by mission slug read from state.db.
- Modify `orchestra/spawn.py:spawn_worker` — set `mission_id` on new worker rows from the running mission (NULL if none).
- Modify `orchestra/cli.py:_reap` and the existing `merge` command — look up mission slug via `worker.mission_id` to compute the right worktree path / branch name.

**Dashboard**
- Modify `orchestra/web.py` — add a missions dropdown, a `/missions` route, and a `?mission=<slug>` filter on existing worker / event views.

**Docs**
- Rewrite step 4 in `GETTING_STARTED.md`.
- Add a "Missions" subsection in `CLAUDE.md` under "Architecture — the big picture".
- Update the "One-shot runner" section in `README.md`.
- Move `examples/urlshortener-mission.md` and `examples/kanban/.orchestra/` into `missions/urlshortener/` and `missions/kanban/` respectively (keeping old paths working via symlink-free runtime acceptance, NOT by maintaining duplicate files).

**Tests**
- New / modify: `tests/test_state.py` (missions table + migration), `tests/test_missions.py` (slug validation + scaffolding), `tests/test_cli.py` (mission subcommands), `tests/test_run.py` (run-flow integration), `tests/test_worktree.py` (namespaced paths), `tests/test_spawn.py` (mission_id inheritance), `tests/test_web.py` (mission filter; this test file is currently in the v1.2 known-broken-collection list — keep that ignore in place but add the new tests behind it).

---

## Task 1: State layer — missions table, mission_id, migration, CRUD

**Goal:** Land the SQLite schema additions, the Mission dataclass, the CRUD helpers, and the one-shot legacy-archive migration. Everything below builds on this.

**Files:**
- Modify: `orchestra/state.py`
- Modify: `tests/test_state.py`

**Acceptance Criteria:**
- [ ] `init_schema(conn)` against a fresh DB creates the `missions` table with the schema in the spec (columns: id, slug, mission_path, status, exit_code, started_at, ended_at) and an index on status.
- [ ] `init_schema(conn)` adds a `mission_id` column to `workers` if it does not exist (ALTER TABLE pattern matches the existing `role` / `worktree` precedent at `state.py:188-197`).
- [ ] `init_schema(conn)` against a v2.3 DB with one or more worker rows and no missions table inserts exactly one `legacy-<YYYYMMDD-HHMM>` mission row with `status='archived'`, `mission_path='(unknown)'`, `started_at` = oldest worker's `started_at`, `ended_at` = newest worker's `updated_at`; backfills every worker's `mission_id` to that mission's id.
- [ ] `init_schema(conn)` against a fresh DB (no workers, no missions) does NOT create a legacy row.
- [ ] `init_schema(conn)` is idempotent: running it twice in a row is a no-op after the first call.
- [ ] `Mission` dataclass is frozen and mirrors the SQLite columns.
- [ ] CRUD functions exist and behave: `create_mission(conn, slug, mission_path, status='running') -> int` (returns new id), `get_mission_by_slug(conn, slug) -> Mission | None`, `get_running_mission(conn) -> Mission | None` (returns the single row with status='running' or None; raises if more than one — invariant violation), `list_missions(conn) -> list[Mission]` (sorted by `started_at` desc), `update_mission(conn, mission_id, *, status=None, exit_code=None, ended_at=None) -> None`.
- [ ] New tests in `tests/test_state.py` cover all CRUD paths and the migration. All tests pass.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_state.py -v
.venv/bin/mypy orchestra/state.py  # strict mypy module
```
Expected: all state tests pass; mypy reports `Success: no issues found`.

**Steps:**

- [ ] **Step 1: Write failing tests for the missions table + CRUD.**

Append to `tests/test_state.py`:

```python
class TestMissionsTable:
    def test_init_schema_creates_missions_table(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(missions)").fetchall()}
        assert {"id", "slug", "mission_path", "status",
                "exit_code", "started_at", "ended_at"} <= cols

    def test_init_schema_adds_mission_id_to_workers(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        assert "mission_id" in cols

    def test_init_schema_idempotent(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.init_schema(conn)  # second call must not raise
        # missions table still present and empty
        rows = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
        assert rows == 0


class TestMissionsCRUD:
    def test_create_and_get_by_slug(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        mid = state.create_mission(conn, slug="m1", mission_path="missions/m1/mission.md")
        row = state.get_mission_by_slug(conn, "m1")
        assert row is not None and row.id == mid and row.status == "running"

    def test_get_running_mission(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        assert state.get_running_mission(conn) is None
        state.create_mission(conn, slug="m1", mission_path="p")
        running = state.get_running_mission(conn)
        assert running is not None and running.slug == "m1"

    def test_get_running_mission_raises_when_multiple(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.create_mission(conn, slug="m1", mission_path="p1")
        state.create_mission(conn, slug="m2", mission_path="p2")
        import pytest
        with pytest.raises(state.StateInvariantError):
            state.get_running_mission(conn)

    def test_update_mission_to_terminal(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        mid = state.create_mission(conn, slug="m1", mission_path="p")
        state.update_mission(conn, mid, status="done", exit_code=0,
                             ended_at=state.now_iso())
        row = state.get_mission_by_slug(conn, "m1")
        assert row.status == "done" and row.exit_code == 0 and row.ended_at is not None

    def test_list_missions_desc_by_started_at(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        state.create_mission(conn, slug="m1", mission_path="p1")
        import time; time.sleep(0.01)
        state.create_mission(conn, slug="m2", mission_path="p2")
        rows = state.list_missions(conn)
        assert [r.slug for r in rows] == ["m2", "m1"]
```

- [ ] **Step 2: Write failing tests for the legacy migration.**

Add to `tests/test_state.py`:

```python
class TestLegacyMigration:
    def test_archives_legacy_workers_into_one_mission(self, tmp_db):
        import sqlite3
        # Build a v2.3-shaped DB by hand: workers table without missions.
        conn = sqlite3.connect(tmp_db)
        conn.executescript("""
        CREATE TABLE workers (
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
        INSERT INTO workers VALUES
          ('pm', '', 'opus', NULL, 's:0.0', 'done', NULL, 5,
           '2026-05-20T09:42:00Z', '2026-05-20T10:10:00Z', 'pm', NULL),
          ('backend', '', 'sonnet', 'orch/backend', 's:0.1', 'done', NULL, 12,
           '2026-05-20T09:45:00Z', '2026-05-20T10:08:00Z', 'engineer', 'backend');
        """)
        conn.commit(); conn.close()

        conn = state.connect(tmp_db)
        state.init_schema(conn)

        rows = state.list_missions(conn)
        assert len(rows) == 1
        legacy = rows[0]
        assert legacy.status == "archived"
        assert legacy.slug.startswith("legacy-")
        assert legacy.mission_path == "(unknown)"
        assert legacy.started_at == "2026-05-20T09:42:00Z"
        assert legacy.ended_at == "2026-05-20T10:10:00Z"

        # All workers backfilled to that mission.
        worker_missions = {
            row[0]: row[1]
            for row in conn.execute("SELECT id, mission_id FROM workers").fetchall()
        }
        assert worker_missions == {"pm": legacy.id, "backend": legacy.id}

    def test_no_legacy_row_on_fresh_db(self, tmp_db):
        conn = state.connect(tmp_db)
        state.init_schema(conn)
        assert state.list_missions(conn) == []
```

- [ ] **Step 3: Run the tests; they fail (`AttributeError`, `OperationalError`, etc.).**

```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_state.py::TestMissionsTable tests/test_state.py::TestMissionsCRUD tests/test_state.py::TestLegacyMigration -v
```
Expected: all new tests FAIL.

- [ ] **Step 4: Edit `orchestra/state.py`. Add the missions DDL to the SCHEMA string.**

Update the `SCHEMA` constant at `state.py:143-181` so it includes the missions table and index. Append after the existing `resource_locks` block:

```python
CREATE TABLE IF NOT EXISTS missions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,
    mission_path TEXT NOT NULL,
    status       TEXT NOT NULL,
    exit_code    INTEGER,
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);
CREATE INDEX IF NOT EXISTS missions_status ON missions(status);
```

- [ ] **Step 5: Add the `Mission` dataclass + `StateInvariantError` near the existing dataclasses at `state.py:78-114`.**

```python
class StateInvariantError(RuntimeError):
    """Raised when a state-table invariant is violated (e.g. two running missions)."""


@dataclass(frozen=True)
class Mission:
    id: int
    slug: str
    mission_path: str
    status: str          # running | done | failed | aborted | archived
    exit_code: int | None
    started_at: str
    ended_at: str | None
```

- [ ] **Step 6: Extend `init_schema` to ALTER workers + run migration.**

Replace the body of `init_schema` at `state.py:188-197` with:

```python
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
    if "mission_id" not in cols:
        conn.execute(
            "ALTER TABLE workers ADD COLUMN mission_id INTEGER REFERENCES missions(id)"
        )
    _migrate_legacy_workers(conn)


def _migrate_legacy_workers(conn: sqlite3.Connection) -> None:
    """One-shot: if workers exist with NULL mission_id and missions is empty,
    archive them as a single legacy-<ts> mission.
    """
    missions_count = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
    if missions_count > 0:
        return
    rows = conn.execute(
        "SELECT MIN(started_at), MAX(updated_at) FROM workers WHERE mission_id IS NULL"
    ).fetchone()
    started_at, ended_at = rows[0], rows[1]
    if started_at is None:
        # workers table empty — fresh DB, nothing to migrate
        return
    # Derive a legacy slug from the oldest started_at.
    slug = "legacy-" + started_at.replace("-", "").replace(":", "").replace("T", "-")[:13]
    cur = conn.execute(
        "INSERT INTO missions (slug, mission_path, status, started_at, ended_at) "
        "VALUES (?, ?, 'archived', ?, ?)",
        (slug, "(unknown)", started_at, ended_at),
    )
    legacy_id = cur.lastrowid
    conn.execute(
        "UPDATE workers SET mission_id = ? WHERE mission_id IS NULL",
        (legacy_id,),
    )
    conn.commit()
```

- [ ] **Step 7: Add the mission CRUD functions to `state.py` (after the worker CRUD block, before `record_event`).**

```python
def _row_to_mission(row: sqlite3.Row) -> Mission:
    return Mission(
        id=row["id"],
        slug=row["slug"],
        mission_path=row["mission_path"],
        status=row["status"],
        exit_code=row["exit_code"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )


def create_mission(
    conn: sqlite3.Connection,
    *,
    slug: str,
    mission_path: str,
    status: str = "running",
) -> int:
    """Insert a new mission row and return its id."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO missions (slug, mission_path, status, started_at) "
        "VALUES (?, ?, ?, ?)",
        (slug, mission_path, status, ts),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def get_mission_by_slug(conn: sqlite3.Connection, slug: str) -> Mission | None:
    row = conn.execute("SELECT * FROM missions WHERE slug = ?", (slug,)).fetchone()
    return _row_to_mission(row) if row is not None else None


def get_running_mission(conn: sqlite3.Connection) -> Mission | None:
    rows = conn.execute(
        "SELECT * FROM missions WHERE status = 'running'"
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        slugs = ", ".join(r["slug"] for r in rows)
        raise StateInvariantError(
            f"more than one mission is 'running' ({slugs}); manual cleanup required"
        )
    return _row_to_mission(rows[0])


def list_missions(conn: sqlite3.Connection) -> list[Mission]:
    rows = conn.execute(
        "SELECT * FROM missions ORDER BY started_at DESC"
    ).fetchall()
    return [_row_to_mission(r) for r in rows]


def update_mission(
    conn: sqlite3.Connection,
    mission_id: int,
    *,
    status: str | None = None,
    exit_code: int | None = None,
    ended_at: str | None = None,
) -> None:
    sets: list[str] = []
    args: list[object] = []
    if status is not None:
        sets.append("status = ?"); args.append(status)
    if exit_code is not None:
        sets.append("exit_code = ?"); args.append(exit_code)
    if ended_at is not None:
        sets.append("ended_at = ?"); args.append(ended_at)
    if not sets:
        return
    args.append(mission_id)
    conn.execute(f"UPDATE missions SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()
```

- [ ] **Step 8: Re-run the tests; all pass.**

```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_state.py -v
.venv/bin/mypy orchestra/state.py
.venv/bin/ruff check orchestra/state.py tests/test_state.py
```
Expected: green pytest, mypy `Success: no issues found`, no ruff warnings.

- [ ] **Step 9: Commit.**

```bash
git add orchestra/state.py tests/test_state.py
git commit -m "feat(state): add missions table, mission_id on workers, legacy archive migration"
```

---

## Task 2: Mission CLI commands — `new`, `list`, `show`, `run`

**Goal:** Add the `orchestra mission new|list|show|run` user-facing surface that uses the state CRUD from Task 1.

**Files:**
- Create: `orchestra/missions.py` (slug validation, directory scaffolding, templates)
- Modify: `orchestra/cli.py` (add subcommands to the existing `mission` Typer subgroup at line 298)
- Create: `tests/test_missions.py` (slug validation + scaffolding unit tests)
- Modify: `tests/test_cli.py` (integration-flavored tests for the new CLI subcommands)

**Acceptance Criteria:**
- [ ] `orchestra mission new <slug>` creates `missions/<slug>/mission.md` and an executable `missions/<slug>/verifier.sh` under the project root. The mission.md template includes `worker_done` so `orchestra mission lint` produces no warning on the scaffolded file.
- [ ] `orchestra mission new <slug>` rejects slugs that don't match `^[a-z0-9][a-z0-9-]*$` (exit code 2, clear message).
- [ ] `orchestra mission new <slug>` rejects a slug already present in either `missions/<slug>/` on disk OR in the `missions` table (exit 2).
- [ ] `orchestra mission list` prints a markdown table sorted by `started_at` desc with columns `slug | status | started_at | duration | exit`. A running mission's row is wrapped in `**bold**`. Empty state prints `(no missions yet)`.
- [ ] `orchestra mission show <slug>` prints three labelled blocks: mission metadata, worker table, last 20 events (chronological, oldest first). Errors with exit 2 if the slug does not exist.
- [ ] `orchestra mission run <slug>` resolves to `missions/<slug>/mission.md` and invokes the same code path as `orchestra run`. The four optional flags (`--model`, `--max-wallclock`, `--max-activity`, `--allow-dirty`) are accepted and forwarded. Errors with exit 2 if `missions/<slug>/mission.md` does not exist.
- [ ] All new tests pass; mypy + ruff clean.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_missions.py tests/test_cli.py -v
.venv/bin/mypy orchestra/missions.py orchestra/cli.py
.venv/bin/ruff check orchestra/missions.py orchestra/cli.py tests/test_missions.py
```
Expected: green pytest, mypy `Success`, no ruff warnings.

**Steps:**

- [ ] **Step 1: Write failing unit tests for slug validation + scaffolding.**

Create `tests/test_missions.py`:

```python
"""Tests for the orchestra/missions.py scaffolding module."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestra import missions


class TestSlugValidation:
    @pytest.mark.parametrize("slug", [
        "urlshortener", "kanban-v2", "abc", "a1", "a-b-c", "0name",
    ])
    def test_valid(self, slug: str) -> None:
        missions.validate_slug(slug)  # no raise

    @pytest.mark.parametrize("slug", [
        "", "-leading", "UPPER", "has_underscore", "has.dot", "has space",
    ])
    def test_invalid(self, slug: str) -> None:
        with pytest.raises(missions.InvalidSlugError):
            missions.validate_slug(slug)


class TestScaffold:
    def test_creates_files(self, tmp_path: Path) -> None:
        missions.scaffold_mission_dir(tmp_path, slug="m1")
        assert (tmp_path / "missions" / "m1" / "mission.md").is_file()
        assert (tmp_path / "missions" / "m1" / "verifier.sh").is_file()
        assert (tmp_path / "missions" / "m1" / "verifier.sh").stat().st_mode & 0o111

    def test_mission_template_mentions_worker_done(self, tmp_path: Path) -> None:
        missions.scaffold_mission_dir(tmp_path, slug="m1")
        body = (tmp_path / "missions" / "m1" / "mission.md").read_text()
        assert "worker_done" in body

    def test_refuses_if_dir_exists(self, tmp_path: Path) -> None:
        (tmp_path / "missions" / "m1").mkdir(parents=True)
        with pytest.raises(missions.SlugCollisionError):
            missions.scaffold_mission_dir(tmp_path, slug="m1")
```

- [ ] **Step 2: Run the new test file; it fails because `orchestra.missions` does not exist.**

```bash
.venv/bin/pytest tests/test_missions.py -v
```
Expected: ImportError or ModuleNotFoundError on `from orchestra import missions`.

- [ ] **Step 3: Create `orchestra/missions.py`.**

```python
"""Mission scaffolding + slug helpers.

Pure-Python helpers (no DB I/O). The CLI calls these from cli.py, then
writes the resulting Mission row via orchestra.state.
"""
from __future__ import annotations

import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

MISSION_TEMPLATE = """\
# Mission: <one-line goal>

Replace this paragraph with a few sentences describing what you want built.

## Acceptance
- <criterion 1>
- <criterion 2>

## Team
- engineer (sonnet) — implements the work.

You only emit `worker_done` when every acceptance check passes.
"""

VERIFIER_TEMPLATE = """\
#!/usr/bin/env bash
set -euo pipefail
# Replace these checks with your real acceptance commands.
# Exit 0 = pass, non-zero = fail.
echo "verifier skeleton — replace with real checks"
"""


class InvalidSlugError(ValueError):
    """Slug failed the regex check."""


class SlugCollisionError(FileExistsError):
    """missions/<slug>/ already exists on disk or in the missions table."""


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"slug {slug!r} must match {SLUG_RE.pattern} "
            "(lowercase alphanumerics + dashes, must not start with a dash)"
        )


def scaffold_mission_dir(project_root: Path, *, slug: str) -> Path:
    """Create missions/<slug>/{mission.md,verifier.sh}. Raise SlugCollisionError
    if the directory already exists.
    """
    validate_slug(slug)
    target = project_root / "missions" / slug
    if target.exists():
        raise SlugCollisionError(f"{target} already exists")
    target.mkdir(parents=True)
    (target / "mission.md").write_text(MISSION_TEMPLATE)
    verifier = target / "verifier.sh"
    verifier.write_text(VERIFIER_TEMPLATE)
    verifier.chmod(0o755)
    return target
```

- [ ] **Step 4: Re-run; the new unit tests pass.**

```bash
.venv/bin/pytest tests/test_missions.py -v
```
Expected: PASS.

- [ ] **Step 5: Inspect the existing `mission` Typer subgroup in `cli.py`.**

Open `orchestra/cli.py:298` and read the `mission_command` wiring. It is a `typer.Typer()` subgroup that already hosts `mission lint`. The new subcommands go on the same subgroup.

- [ ] **Step 6: Add `mission new` to `cli.py`.**

Inside the `mission` Typer subgroup (find the existing `mission_lint` registration; add immediately below):

```python
@mission_app.command("new")
def mission_new(slug: str = typer.Argument(..., metavar="SLUG")) -> None:
    """Scaffold missions/<slug>/{mission.md, verifier.sh}."""
    from orchestra import missions
    cwd = Path.cwd()
    # Collision check against state.db (table) as well as disk.
    db_path = _state_db()
    if db_path.exists():
        with _open_db() as conn:
            if state.get_mission_by_slug(conn, slug) is not None:
                typer.echo(f"error: mission slug {slug!r} already exists in state.db", err=True)
                raise typer.Exit(code=2)
    try:
        missions.validate_slug(slug)
    except missions.InvalidSlugError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    try:
        target = missions.scaffold_mission_dir(cwd, slug=slug)
    except missions.SlugCollisionError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"scaffolded {target.relative_to(cwd)}")
```

- [ ] **Step 7: Add `mission list` to `cli.py`.**

```python
@mission_app.command("list")
def mission_list() -> None:
    """Print a markdown table of every mission, sorted by started_at desc."""
    _require_initialized()
    with _open_db() as conn:
        rows = state.list_missions(conn)
    if not rows:
        typer.echo("(no missions yet)")
        return
    typer.echo("| slug | status | started_at | duration | exit |")
    typer.echo("|------|--------|------------|----------|------|")
    for m in rows:
        dur = _format_duration(m.started_at, m.ended_at)
        exit_str = "-" if m.exit_code is None else str(m.exit_code)
        slug_cell = f"**{m.slug}**" if m.status == "running" else m.slug
        typer.echo(f"| {slug_cell} | {m.status} | {m.started_at} | {dur} | {exit_str} |")


def _format_duration(start: str, end: str | None) -> str:
    if end is None:
        return "-"
    from datetime import datetime
    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    delta = e - s
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    mi, sec = divmod(rem, 60)
    return f"{h:02d}:{mi:02d}:{sec:02d}"
```

- [ ] **Step 8: Add `mission show` to `cli.py`.**

```python
@mission_app.command("show")
def mission_show(slug: str = typer.Argument(..., metavar="SLUG")) -> None:
    """Print a mission row, its workers, and its last 20 events."""
    _require_initialized()
    with _open_db() as conn:
        m = state.get_mission_by_slug(conn, slug)
        if m is None:
            typer.echo(f"error: no mission with slug {slug!r}", err=True)
            raise typer.Exit(code=2)
        # Workers in this mission.
        worker_rows = conn.execute(
            "SELECT id, role, model, status, started_at, turns "
            "FROM workers WHERE mission_id = ? ORDER BY started_at",
            (m.id,),
        ).fetchall()
        # Last 20 events for any of those workers, chronological.
        if worker_rows:
            worker_ids = [w["id"] for w in worker_rows]
            placeholders = ",".join("?" * len(worker_ids))
            ev_rows = conn.execute(
                f"SELECT * FROM events WHERE worker_id IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 20", worker_ids,
            ).fetchall()
            ev_rows = list(reversed(ev_rows))  # back to chronological
        else:
            ev_rows = []
    # Mission metadata
    typer.echo(f"# mission {m.slug}")
    typer.echo(f"- status: {m.status}")
    typer.echo(f"- mission_path: {m.mission_path}")
    typer.echo(f"- started_at: {m.started_at}")
    typer.echo(f"- ended_at: {m.ended_at or '-'}")
    typer.echo(f"- duration: {_format_duration(m.started_at, m.ended_at)}")
    typer.echo(f"- exit_code: {m.exit_code if m.exit_code is not None else '-'}")
    # Workers
    typer.echo("\n## workers")
    if not worker_rows:
        typer.echo("(none)")
    else:
        typer.echo("| id | role | model | status | started_at | turns |")
        typer.echo("|----|------|-------|--------|------------|-------|")
        for w in worker_rows:
            typer.echo(f"| {w['id']} | {w['role']} | {w['model']} | {w['status']} | {w['started_at']} | {w['turns']} |")
    # Events
    typer.echo("\n## last 20 events")
    if not ev_rows:
        typer.echo("(none)")
    else:
        for ev in ev_rows:
            typer.echo(f"- {ev['ts']} {ev['worker_id'] or '-'} {ev['kind']}")
```

- [ ] **Step 9: Add `mission run` to `cli.py`.**

```python
@mission_app.command("run")
def mission_run(
    slug: str = typer.Argument(..., metavar="SLUG"),
    model: str = typer.Option("opus", "--model"),
    max_wallclock: float = typer.Option(5400.0, "--max-wallclock"),
    max_activity: float = typer.Option(600.0, "--max-activity"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
) -> None:
    """Shortcut for `orchestra run missions/<slug>/mission.md`."""
    mission_path = Path("missions") / slug / "mission.md"
    if not mission_path.exists():
        typer.echo(
            f"error: {mission_path} does not exist. "
            f"Run `orchestra mission new {slug}` first.",
            err=True,
        )
        raise typer.Exit(code=2)
    from orchestra import run as run_mod
    code = run_mod.run_mission(
        mission_path,
        model=model,
        max_wallclock=max_wallclock,
        max_activity=max_activity,
        allow_dirty=allow_dirty,
    )
    raise typer.Exit(code=code)
```

- [ ] **Step 10: Write integration-style CLI tests in `tests/test_cli.py`.**

Append a new class (use `typer.testing.CliRunner`):

```python
from typer.testing import CliRunner
from orchestra.cli import app
from orchestra import state


class TestMissionCommands:
    def _init_project(self, tmp_path):
        import os
        os.chdir(tmp_path)
        # Mimic `orchestra init` enough for the commands that need .orchestra/state.db.
        (tmp_path / ".orchestra").mkdir()
        conn = state.connect(tmp_path / ".orchestra" / "state.db")
        state.init_schema(conn)
        conn.close()

    def test_mission_new_creates_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "new", "alpha"])
        assert res.exit_code == 0, res.output
        assert (tmp_path / "missions" / "alpha" / "mission.md").exists()
        assert (tmp_path / "missions" / "alpha" / "verifier.sh").exists()

    def test_mission_new_rejects_bad_slug(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "new", "BAD-SLUG"])
        assert res.exit_code == 2
        assert "must match" in res.output

    def test_mission_list_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "list"])
        assert res.exit_code == 0
        assert "(no missions yet)" in res.output

    def test_mission_list_with_rows(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        conn = state.connect(tmp_path / ".orchestra" / "state.db")
        state.create_mission(conn, slug="m1", mission_path="missions/m1/mission.md")
        conn.close()
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "list"])
        assert res.exit_code == 0
        assert "**m1**" in res.output  # running -> bold

    def test_mission_show_unknown_slug(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "show", "nope"])
        assert res.exit_code == 2

    def test_mission_run_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._init_project(tmp_path)
        runner = CliRunner()
        res = runner.invoke(app, ["mission", "run", "ghost"])
        assert res.exit_code == 2
        assert "does not exist" in res.output
```

- [ ] **Step 11: Run the full test sweep.**

```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_missions.py tests/test_cli.py -v
.venv/bin/mypy orchestra/missions.py orchestra/cli.py
.venv/bin/ruff check orchestra/missions.py orchestra/cli.py tests/test_missions.py tests/test_cli.py
```
Expected: all green.

- [ ] **Step 12: Commit.**

```bash
git add orchestra/missions.py orchestra/cli.py tests/test_missions.py tests/test_cli.py
git commit -m "feat(cli): add mission new/list/show/run subcommands"
```

---

## Task 3: `orchestra run` lifecycle — sequential gate + mission row

**Goal:** Wire the mission lifecycle into `run_mission`: enforce sequential, resolve a slug, create the row at start, update it at end.

**Files:**
- Modify: `orchestra/run.py`
- Modify: `tests/test_run.py`

**Acceptance Criteria:**
- [ ] If another mission has `status='running'`, `run_mission` exits 2 with a message naming that mission's slug. No new mission row is created. No PM is spawned.
- [ ] If the mission path matches `^missions/(?P<slug>[a-z0-9][a-z0-9-]*)/mission\.md$`, that slug is used. Otherwise an auto slug `m-<YYYYMMDD>-<HHMM>` is generated; collisions get `-2`, `-3`, ... appended.
- [ ] On successful pre-flight, a new `missions` row is inserted with `status='running'`. The PM worker row carries the new `mission_id`.
- [ ] The existing PM-row pre-flight check at `run.py:114-126` is replaced with one that errors only if a `pm` worker row exists with the SAME `mission_id` as the row we just created (which can never happen on first run; the migration path puts old PM rows under a different mission_id).
- [ ] On terminal: `worker_done` → status=done, exit_code=0; wall-clock watchdog (124) → status=failed, exit_code=124; activity watchdog (125) → status=failed, exit_code=125; defensive loop (126) → status=failed, exit_code=126. `ended_at` is set in all four cases.
- [ ] All existing `tests/test_run.py` tests still pass; new tests cover the four lifecycle paths + the sequential gate.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_run.py -v
.venv/bin/mypy orchestra/run.py
.venv/bin/ruff check orchestra/run.py tests/test_run.py
```
Expected: green pytest, mypy `Success`, no ruff warnings.

**Steps:**

- [ ] **Step 1: Read the current `run.py:run_mission` (already partially read at design time) and `tests/test_run.py` to understand the existing test fixtures + mocks.**

```bash
cat orchestra/run.py
cat tests/test_run.py
```

- [ ] **Step 2: Write failing tests for the sequential gate + slug resolution + lifecycle updates.**

Append to `tests/test_run.py`:

```python
class TestRunMissionMissions:
    """Wiring between run_mission and the missions table."""

    def _mk_project(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        # config git user so commit pre-flight doesn't surprise us
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
        (tmp_path / ".orchestra").mkdir()
        conn = state.connect(tmp_path / ".orchestra" / "state.db")
        state.init_schema(conn)
        conn.close()
        # An empty initial commit so `git status --porcelain` is clean.
        (tmp_path / "README").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
        return tmp_path

    def test_sequential_gate_blocks_when_running(self, tmp_path, monkeypatch):
        project = self._mk_project(tmp_path)
        monkeypatch.chdir(project)
        # Pre-create a running mission row.
        conn = state.connect(project / ".orchestra" / "state.db")
        state.create_mission(conn, slug="busy", mission_path="missions/busy/mission.md")
        conn.close()
        # Now try to run another.
        (project / "missions" / "x").mkdir(parents=True)
        (project / "missions" / "x" / "mission.md").write_text("# x\n\nworker_done")
        from orchestra import run as run_mod
        code = run_mod.run_mission(Path("missions/x/mission.md"))
        assert code == 2

    def test_slug_inferred_from_path(self, tmp_path, monkeypatch):
        project = self._mk_project(tmp_path)
        monkeypatch.chdir(project)
        (project / "missions" / "alpha").mkdir(parents=True)
        (project / "missions" / "alpha" / "mission.md").write_text("# alpha\n\nworker_done")
        # Patch spawn + the poll loop so we don't actually try to spawn a PM.
        from orchestra import run as run_mod
        _patch_run_mission_to_no_op(monkeypatch, run_mod, terminal="worker_done")
        run_mod.run_mission(Path("missions/alpha/mission.md"))
        conn = state.connect(project / ".orchestra" / "state.db")
        row = state.get_mission_by_slug(conn, "alpha")
        conn.close()
        assert row is not None and row.status == "done" and row.exit_code == 0

    def test_slug_auto_generated_for_non_missions_path(self, tmp_path, monkeypatch):
        project = self._mk_project(tmp_path)
        monkeypatch.chdir(project)
        (project / "legacy-mission.md").write_text("# legacy\n\nworker_done")
        from orchestra import run as run_mod
        _patch_run_mission_to_no_op(monkeypatch, run_mod, terminal="worker_done")
        run_mod.run_mission(Path("legacy-mission.md"))
        conn = state.connect(project / ".orchestra" / "state.db")
        rows = state.list_missions(conn)
        conn.close()
        assert len(rows) == 1 and rows[0].slug.startswith("m-")
```

`_patch_run_mission_to_no_op` is a helper that monkeypatches `spawn.spawn_worker` and the event loop so we don't spend real tmux/credits. Adapt the existing pattern in `tests/test_run.py` (find how the current tests stub spawn; if no such pattern exists yet, add one — set `run_mod.spawn.spawn_worker = lambda *a, **k: None` and short-circuit the poll loop by inserting a `worker_done` event row directly).

- [ ] **Step 3: Run the new tests; they fail.**

```bash
.venv/bin/pytest tests/test_run.py::TestRunMissionMissions -v
```
Expected: FAIL on each test.

- [ ] **Step 4: Edit `orchestra/run.py:run_mission` to add the sequential gate + slug resolution + row creation.**

Insert after the existing `.orchestra` existence check (around `run.py:92-94`) and before the PM-row pre-flight at `run.py:114`:

```python
    # ---- Pre-flight: sequential gate ----
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

    # ---- Slug resolution ----
    mission_slug = _resolve_slug(mission_path, state_db_path)

    # ---- Create mission row ----
    mission_conn = state.connect(state_db_path)
    try:
        try:
            relative = str(mission_path.relative_to(cwd))
        except ValueError:
            relative = str(mission_path)
        mission_id = state.create_mission(
            mission_conn, slug=mission_slug, mission_path=relative,
        )
    finally:
        mission_conn.close()
```

Replace the PM-row check that follows so it scopes by `mission_id`:

```python
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
        # Cannot happen in normal flow (we just created the mission), but guard.
        print("[runner] error: pm row already exists for this mission", flush=True)
        return 2
```

Pass `mission_id` into the spawn call (Task 4 makes the spawn code read it, but we already pass it here so the wire is in place):

```python
        spawn.spawn_worker(
            spawn_conn,
            worker_id="pm",
            model=model,
            task="",
            project_root=str(cwd),
            state_db=state_db_path,
            mission_id=mission_id,   # NEW
            # ... other existing kwargs
        )
```

At the end of `run_mission`, replace the bare `return code` with code that updates the mission row first:

```python
    # ---- Update mission row to terminal status ----
    end_conn = state.connect(state_db_path)
    try:
        if code == 0:
            status = "done"
        elif code in (124, 125, 126):
            status = "failed"
        else:
            status = "failed"
        state.update_mission(
            end_conn, mission_id,
            status=status, exit_code=code, ended_at=state.now_iso(),
        )
    finally:
        end_conn.close()
    return code
```

Add the `_resolve_slug` helper at module scope:

```python
import re as _re

_MISSION_PATH_RE = _re.compile(r"^missions/(?P<slug>[a-z0-9][a-z0-9-]*)/mission\.md$")


def _resolve_slug(mission_path: Path, state_db_path: Path) -> str:
    rel = str(mission_path)
    m = _MISSION_PATH_RE.match(rel)
    if m is not None:
        return m.group("slug")
    # Auto-generate m-<YYYYMMDD>-<HHMM>, suffix on collision.
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
```

- [ ] **Step 5: Update `spawn.spawn_worker` to accept and write `mission_id`. (Detailed change is in Task 4 — for now, add a kwarg with default None.)**

In `orchestra/spawn.py:163` find `def spawn_worker(...)`. Add a `mission_id: int | None = None` keyword parameter. Inside, when calling `state.create_worker(...)`, pass `mission_id` through. (The `create_worker` function will be extended in Task 4 to accept this — for this task, add a TEMPORARY direct SQL update after `create_worker` returns:

```python
if mission_id is not None:
    conn.execute("UPDATE workers SET mission_id = ? WHERE id = ?", (mission_id, worker_id))
    conn.commit()
```

This temporary path will be cleaned up in Task 4.)

- [ ] **Step 6: Re-run tests; all pass.**

```bash
.venv/bin/pytest tests/test_run.py -v
.venv/bin/mypy orchestra/run.py orchestra/spawn.py
.venv/bin/ruff check orchestra/run.py
```
Expected: green.

- [ ] **Step 7: Commit.**

```bash
git add orchestra/run.py orchestra/spawn.py tests/test_run.py
git commit -m "feat(run): sequential gate + mission row lifecycle in orchestra run"
```

---

## Task 4: Worktree + spawn namespacing, merge/reap lookup

**Goal:** Engineer worktrees + branches are scoped under the running mission's slug. `spawn`, `merge`, and `reap` all use the same lookup helper.

**Files:**
- Modify: `orchestra/worktree.py`
- Modify: `orchestra/spawn.py`
- Modify: `orchestra/state.py` (extend `create_worker` to accept `mission_id`; revert the temporary direct-SQL hack from Task 3 Step 5)
- Modify: `orchestra/cli.py` — `_reap` and the `merge` command
- Modify: `tests/test_worktree.py`, `tests/test_spawn.py`

**Acceptance Criteria:**
- [ ] `worktree.add(project_root, name=..., worker_id=..., mission_slug=...)` creates `worktrees/<mission_slug>/<name>/` on branch `orch/<mission_slug>/<worker_id>` instead of the old flat paths.
- [ ] `worktree.add` reads the running mission's slug from `state.db` when `mission_slug` is not passed (back-compat fallback used by callers we have not yet updated).
- [ ] `worktree.remove` resolves the path + branch from `mission_slug` symmetrically.
- [ ] `state.create_worker` accepts a `mission_id: int | None` parameter and writes it to the row.
- [ ] `spawn.spawn_worker` reads `state.get_running_mission(conn)` (if no `mission_id` kwarg passed in) and threads the slug into `worktree.add` and the id into `create_worker`.
- [ ] `cli._reap(worker_id)` and the `merge` command both look up `worker.mission_id` (via `state.get_worker`) and resolve the worktree path / branch from that mission's slug. The existing PM-facing CLI invocation (`orchestra merge backend`, `orchestra reap backend`) is unchanged from the user's perspective.
- [ ] All tests pass; mypy + ruff clean.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_worktree.py tests/test_spawn.py tests/test_run.py tests/test_cli.py -v
.venv/bin/mypy orchestra/state.py orchestra/spawn.py orchestra/worktree.py orchestra/cli.py
.venv/bin/ruff check orchestra/state.py orchestra/spawn.py orchestra/worktree.py orchestra/cli.py
```
Expected: all green.

**Steps:**

- [ ] **Step 1: Write failing tests for namespaced worktrees in `tests/test_worktree.py`.**

```python
class TestNamespacedWorktree:
    def test_add_uses_mission_slug(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                        "-m", "init", "-q",
                        "-c", "user.email=t@t", "-c", "user.name=t"], check=True)
        from orchestra import worktree
        path = worktree.add(tmp_path, name="backend", worker_id="backend",
                            mission_slug="alpha")
        assert path == tmp_path / "worktrees" / "alpha" / "backend"
        assert path.exists()
        # Branch should be orch/alpha/backend
        branches = subprocess.check_output(
            ["git", "-C", str(tmp_path), "branch"], text=True,
        )
        assert "orch/alpha/backend" in branches

    def test_remove_uses_mission_slug(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty",
                        "-m", "init", "-q",
                        "-c", "user.email=t@t", "-c", "user.name=t"], check=True)
        from orchestra import worktree
        worktree.add(tmp_path, name="backend", worker_id="backend", mission_slug="alpha")
        worktree.remove(tmp_path, name="backend", worker_id="backend", mission_slug="alpha")
        assert not (tmp_path / "worktrees" / "alpha" / "backend").exists()
        branches = subprocess.check_output(
            ["git", "-C", str(tmp_path), "branch"], text=True,
        )
        assert "orch/alpha/backend" not in branches
```

- [ ] **Step 2: Run; tests fail (function signature doesn't accept `mission_slug`).**

- [ ] **Step 3: Modify `orchestra/worktree.py` to namespace by mission slug.**

```python
"""git worktree add/remove helpers for orchestra engineers."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _resolve_slug(project_root: Path, mission_slug: str | None) -> str:
    if mission_slug is not None:
        return mission_slug
    from orchestra import state
    db = project_root / ".orchestra" / "state.db"
    conn = state.connect(db)
    try:
        m = state.get_running_mission(conn)
    finally:
        conn.close()
    if m is None:
        raise RuntimeError(
            "worktree.add/remove called with no running mission; "
            "spawn an engineer only from inside an orchestra run"
        )
    return m.slug


def add(
    project_root: Path,
    *,
    name: str,
    worker_id: str,
    mission_slug: str | None = None,
) -> Path:
    """Ensure a worktree exists at <root>/worktrees/<slug>/<name> on
    branch orch/<slug>/<worker_id>. Idempotent."""
    from orchestra import settings_merge

    slug = _resolve_slug(project_root, mission_slug)
    wt_path = project_root / "worktrees" / slug / name
    if wt_path.exists():
        return wt_path
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"orch/{slug}/{worker_id}"
    existing = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--list", branch],
        capture_output=True, text=True, check=True,
    ).stdout
    args = ["worktree", "add"]
    if existing.strip():
        args += [str(wt_path), branch]
    else:
        args += ["-b", branch, str(wt_path), "HEAD"]
    _git(project_root, *args)
    settings_merge.ensure_hooks(wt_path / ".claude" / "settings.local.json")
    return wt_path


def remove(
    project_root: Path,
    *,
    name: str,
    worker_id: str,
    mission_slug: str | None = None,
) -> None:
    slug = _resolve_slug(project_root, mission_slug)
    wt_path = project_root / "worktrees" / slug / name
    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(wt_path)],
            check=False,
        )
    subprocess.run(
        ["git", "-C", str(project_root), "branch", "-D", f"orch/{slug}/{worker_id}"],
        check=False, capture_output=True,
    )
```

- [ ] **Step 4: Extend `state.create_worker` to accept `mission_id: int | None = None`.**

In `state.py:219`, add the kwarg, pass it through into the INSERT. Update the SQL string in the function body to include `mission_id`:

```python
def create_worker(
    conn: sqlite3.Connection,
    *,
    id: str,
    task: str,
    model: str,
    branch: str | None,
    pane_target: str,
    role: str = "engineer",
    worktree: str | None = None,
    mission_id: int | None = None,
) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO workers (id, task, model, branch, pane_target, status, "
        "started_at, updated_at, role, worktree, mission_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, task, model, branch, pane_target, "spawning", ts, ts, role,
         worktree, mission_id),
    )
    conn.commit()
```

(The exact existing signature may differ slightly — adapt to whatever is in the file. The key change: thread `mission_id` through.)

- [ ] **Step 5: Update `spawn.spawn_worker` to use the new `create_worker` signature.**

In `orchestra/spawn.py:163`:

- Accept `mission_id: int | None = None` as a kwarg.
- If `mission_id` is None, read the running mission via `state.get_running_mission(conn)`; use its id if present (`m.id`), else None.
- Pass `mission_id` to `state.create_worker(...)`.
- When calling `worktree.add(...)`, pass `mission_slug=<the_running_mission_slug>`.
- Remove the temporary direct-SQL UPDATE hack added in Task 3 Step 5.

- [ ] **Step 6: Update `cli._reap` and the `merge` command to look up `mission_id` from the worker row.**

In `orchestra/cli.py:480` (`_reap`):

```python
def _reap(worker_id: str) -> bool:
    """Remove worktree + branch for a worker. Returns True on success."""
    cwd = Path.cwd()
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None:
            typer.echo(f"reap: no worker {worker_id!r}", err=True)
            return False
        # Resolve mission slug from the worker's mission_id.
        if w.mission_id is None:
            mission_slug = None  # legacy fallback — old-style flat worktree
        else:
            m = conn.execute("SELECT slug FROM missions WHERE id = ?",
                             (w.mission_id,)).fetchone()
            mission_slug = m["slug"] if m else None
    from orchestra import worktree
    worktree.remove(cwd, name=w.worktree or worker_id, worker_id=worker_id,
                    mission_slug=mission_slug)
    return True
```

For `merge` (at `cli.py:496`), similarly look up the worker's `mission_id` to compute the branch name. The branch is `orch/<slug>/<worker_id>` for missions; if `mission_id is None`, fall back to `orch/<worker_id>`.

Note: `state.Worker` needs a `mission_id` field. Add it to the dataclass at `state.py:78`:

```python
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
    role: str
    worktree: str | None
    mission_id: int | None  # NEW
```

And update `_row_to_worker` at `state.py:202` to include `mission_id=row["mission_id"]`. (The `mission_id` column is added by `init_schema`.)

- [ ] **Step 7: Re-run the relevant test files.**

```bash
.venv/bin/pytest tests/test_worktree.py tests/test_spawn.py tests/test_run.py tests/test_cli.py -v
```
Expected: green.

- [ ] **Step 8: Commit.**

```bash
git add orchestra/worktree.py orchestra/spawn.py orchestra/state.py orchestra/cli.py \
       tests/test_worktree.py tests/test_spawn.py
git commit -m "feat(worktree,spawn): namespace branches + paths by mission slug"
```

---

## Task 5: Dashboard mission switcher + `/missions` page

**Goal:** Surface the mission concept in the FastAPI dashboard.

**Files:**
- Modify: `orchestra/web.py`
- Modify: `tests/test_web.py` (currently ignored in CI; keep the `--ignore` but add new tests)

**Acceptance Criteria:**
- [ ] A `/missions` route returns an HTML page listing every mission (table mirroring `orchestra mission list`).
- [ ] Every existing route accepts `?mission=<slug>` and filters worker / event queries by that mission's id when provided. When omitted, the default is the running mission's id if any, else the most-recent mission's id; if there are no missions, behaviour is unchanged from today (show all workers, no filter).
- [ ] A dropdown / link surface at the top of every page exposes the list of mission slugs.
- [ ] `pytest tests/test_web.py` (when collected) passes with the new tests.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_web.py -v
.venv/bin/mypy orchestra/web.py
.venv/bin/ruff check orchestra/web.py tests/test_web.py
```
Expected: green pytest (the `--ignore=tests/test_web.py` in the project-wide invocation can stay; this task's verify command runs the file explicitly), mypy `Success`, no ruff warnings.

**Steps:**

- [ ] **Step 1: Read `orchestra/web.py` to understand its current shape (FastAPI app + Jinja templates? string concat? something else).**

```bash
sed -n '1,80p' orchestra/web.py
```

- [ ] **Step 2: Add a `/missions` route. Mirror the existing route-registration pattern.**

(The shape of the change depends on web.py's current template strategy. If it builds HTML by string concat, the new page does too. If it uses Jinja templates, add a `missions.html`. Match the existing style — do not invent a new templating system.)

- [ ] **Step 3: Add the `?mission=<slug>` query param handling to existing worker / event routes.**

Compute the active mission once per request (helper `_active_mission_id(request, conn) -> int | None`) and use it to filter queries.

- [ ] **Step 4: Add the top-of-page mission switcher.**

(Either a `<select>` that drives a query param, or a list of `<a href="?mission=X">` links — whichever is closer to the existing UI style.)

- [ ] **Step 5: Write tests in `tests/test_web.py` that boot the FastAPI test client, seed a few missions + workers, and assert filtering works.**

(Adapt to the existing test pattern in `test_web.py` — that file is currently in the broken-collection list per CLAUDE.md, but the breakage is `sse-starlette`-related, not a missing test infra. Restore collection here if the missing dep is now available; if not, document in the commit that this task's tests require the dep to be installed.)

- [ ] **Step 6: Run the tests.**

```bash
.venv/bin/pytest tests/test_web.py -v
```
Expected: green. If `sse-starlette` is still missing, `pip install sse-starlette` and document the bump in CHANGELOG.

- [ ] **Step 7: Commit.**

```bash
git add orchestra/web.py tests/test_web.py
git commit -m "feat(dashboard): mission switcher + /missions page"
```

---

## Task 6: Documentation updates

**Goal:** Reflect the new mission workflow in all user-facing docs.

**Files:**
- Modify: `GETTING_STARTED.md` — rewrite step 4.
- Modify: `CLAUDE.md` — new "Missions" subsection + update the data-flow ASCII block + add worktree namespace as a critical invariant.
- Modify: `README.md` — update the "One-shot runner" section.
- Modify: `examples/urlshortener-mission.md` → `missions/urlshortener/mission.md` (move + delete old).
- Modify: `examples/kanban/` → `missions/kanban/` (the `examples/kanban/.orchestra/` content becomes `missions/kanban/.orchestra/` symmetrically; preserve the role examples).
- Modify: `CHANGELOG.md` — add a v2.4 entry summarising the change.

**Acceptance Criteria:**
- [ ] `GETTING_STARTED.md` step 4 instructs the user to run `orchestra mission new <slug>` then edit the scaffolded files, with `orchestra mission run <slug>` for step 6.
- [ ] `CLAUDE.md` has a new H3 "Missions" under "Architecture — the big picture" that names the `missions` table + status lifecycle + the legacy migration + the `mission_id` column on workers.
- [ ] `CLAUDE.md` adds a bullet under "Important quirk" (the settings-merge invariant) noting the new worktree namespacing convention: `worktrees/<slug>/<id>` on `orch/<slug>/<id>`.
- [ ] `README.md`'s "One-shot runner" section now references `orchestra mission new` + `orchestra mission run`. The legacy `orchestra run .orchestra/mission.md` line stays with a note that it still works but is soft-deprecated.
- [ ] Examples migrated: `missions/urlshortener/mission.md` exists with the same content as the old `examples/urlshortener-mission.md`; `missions/kanban/` mirrors `examples/kanban/`. The `examples/` paths are removed.
- [ ] `CHANGELOG.md` has a new entry dated 2026-05-22 under v2.4 (or whatever the next version is) describing multi-mission support.
- [ ] No links in the codebase point at the deleted example paths.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
test -f missions/urlshortener/mission.md
test -d missions/kanban
! test -f examples/urlshortener-mission.md
! test -d examples/kanban
grep -q 'orchestra mission new' GETTING_STARTED.md
grep -q '## Missions' CLAUDE.md || grep -q '### Missions' CLAUDE.md
grep -q 'orchestra mission new' README.md
grep -q 'v2.4\|multi-mission' CHANGELOG.md
# No surviving references to the deleted example paths
! grep -RIn 'examples/urlshortener-mission.md\|examples/kanban' --exclude-dir=.git --exclude-dir=worktrees .
echo OK
```
Expected: `OK`.

**Steps:**

- [ ] **Step 1: Rewrite `GETTING_STARTED.md` step 4.**

Replace the existing section 4 (which uses a heredoc to write `.orchestra/mission.md`) with:

```markdown
## 4. Write a mission file

**What:** Scaffold a new mission under `missions/<slug>/`. Replace `<slug>`
with a short, lowercase name (e.g. `urlshortener`, `kanban-v2`).

**Run:**
```bash
orchestra mission new my-first-mission
$EDITOR missions/my-first-mission/mission.md
```

Fill in the placeholder goal, acceptance criteria, and team sections. Keep
the `worker_done` reference — that is what tells the PM how to terminate.

**Verify:**
```bash
orchestra mission lint missions/my-first-mission/mission.md
```

Expected: exit 0 and no `warning:` lines.
```

Update section 5 (optional verifier) to point at `missions/<slug>/verifier.sh` (which `mission new` already scaffolded). Update section 6 to use `orchestra mission run my-first-mission`.

- [ ] **Step 2: Add the Missions subsection to `CLAUDE.md` under "Architecture — the big picture".**

Insert (right after the "Worktree-per-engineer pattern" subsection):

```markdown
### Missions

`state.db.missions` is the canonical record of every orchestrated run. Each
row has a unique slug, a path to its mission file, a status
(`running | done | failed | aborted | archived`), and timing. Worker rows
carry a `mission_id` foreign key.

Only one mission may have `status='running'` at a time —
`orchestra run` enforces this via a pre-flight check. Direct
`orchestra spawn` invocations outside a mission leave `mission_id = NULL`.

The schema was introduced in v2.4 via a forward-compat ALTER and a one-shot
migration that archives any pre-existing worker rows under a single
`legacy-<ts>` mission with `status='archived'`.
```

Update the data-flow ASCII block in the same section to mention missions.

Under "Important quirk" (the settings-merge invariant), add:

```markdown
**Worktree namespace:** engineers' worktrees live at
`worktrees/<mission_slug>/<worker_id>` on branch
`orch/<mission_slug>/<worker_id>`. The mission slug is read from the
currently-running `missions` row by `orchestra/worktree.py:add`. Two
missions can have an engineer named `backend` without collision. Legacy
pre-v2.4 worktrees may still exist at the old flat paths
(`worktrees/<id>`); they are not auto-migrated.
```

- [ ] **Step 3: Update `README.md`'s "One-shot runner" section.**

Replace the existing block with:

```markdown
## One-shot runner

```
orchestra mission new my-mission         # scaffolds missions/my-mission/
$EDITOR missions/my-mission/mission.md
orchestra mission run my-mission         # blocks until done or watchdog fires
```

The legacy form `orchestra run .orchestra/mission.md` still works but is
soft-deprecated; new projects should use the `mission` subcommands.
```

- [ ] **Step 4: Move the example mission directories.**

```bash
cd /home/n1n3b1t/dev/claude-orchestra
mkdir -p missions/urlshortener
git mv examples/urlshortener-mission.md missions/urlshortener/mission.md
mkdir -p missions/kanban
# Move every file under examples/kanban/ into missions/kanban/.
git mv examples/kanban/* missions/kanban/
rmdir examples/kanban examples
```

(If anything under `examples/` other than `urlshortener-mission.md` and `kanban/` exists, leave it alone — only move what is genuinely a mission example.)

Update `scripts/e2e-build-urlshortener.sh` if it references the old path.

- [ ] **Step 5: Add a CHANGELOG entry.**

Prepend to `CHANGELOG.md`:

```markdown
## v2.4 — multi-mission support

- New `missions` table in `state.db`. Every `orchestra run` creates a
  mission row; workers carry a `mission_id` foreign key.
- New CLI: `orchestra mission new <slug>`, `orchestra mission list`,
  `orchestra mission show <slug>`, `orchestra mission run <slug>`.
- Sequential gate: `orchestra run` refuses to start when another mission
  has `status='running'`.
- Worktrees and branches are namespaced by mission slug:
  `worktrees/<slug>/<id>` on `orch/<slug>/<id>`. Existing pre-v2.4 paths
  keep working but are no longer used by new runs.
- Dashboard: top-of-page mission switcher + `/missions` page.
- Migration: on first start against an old DB, all pre-existing worker
  rows are archived under a single `legacy-<ts>` mission with
  `status='archived'`. No manual SQL surgery required.
- Examples moved from `examples/` to `missions/`. Old paths removed.
```

- [ ] **Step 6: Run the doc verify command.**

```bash
test -f missions/urlshortener/mission.md \
  && test -d missions/kanban \
  && ! test -f examples/urlshortener-mission.md \
  && ! test -d examples/kanban \
  && grep -q 'orchestra mission new' GETTING_STARTED.md \
  && (grep -q '## Missions' CLAUDE.md || grep -q '### Missions' CLAUDE.md) \
  && grep -q 'orchestra mission new' README.md \
  && (grep -q 'v2.4' CHANGELOG.md || grep -q 'multi-mission' CHANGELOG.md) \
  && ! grep -RIn 'examples/urlshortener-mission.md\|examples/kanban' \
        --exclude-dir=.git --exclude-dir=worktrees . \
  && echo OK
```
Expected: `OK`.

- [ ] **Step 7: Commit.**

```bash
git add GETTING_STARTED.md CLAUDE.md README.md CHANGELOG.md missions/ \
        scripts/e2e-build-urlshortener.sh 2>/dev/null
git add -u  # capture the deletes under examples/
git commit -m "docs: rewrite mission workflow for multi-mission support (v2.4)"
```

---

## Task 7: End-to-end test — two consecutive missions

**Goal:** A test that exercises the full stack against two consecutive missions, proving the sequential gate, slug resolution, mission row lifecycle, and worktree namespacing all hold together. Stays under unit-test cost (no real PM spawn, no API credits).

**Files:**
- Create: `tests/test_multi_mission_e2e.py`

**Acceptance Criteria:**
- [ ] The test creates a fresh tmp project, initialises state.db via `state.init_schema`, scaffolds two missions (`alpha`, `beta`) via `missions.scaffold_mission_dir`, and simulates two consecutive `orchestra run` invocations by directly invoking `run.run_mission` with spawn + the poll loop monkeypatched to no-op.
- [ ] After both runs, `state.list_missions(conn)` returns exactly two rows, both with `status='done'`, `exit_code=0`, and distinct slugs `alpha`, `beta`.
- [ ] During the second run's pre-flight, `state.get_running_mission` returns None (the first mission has been moved to `done`).
- [ ] If the test seeds an artificial second running mission before invoking the second run, the second run exits with code 2 (sequential gate trips).
- [ ] No git worktrees or branches leak out of the tmp project after the test.

**Verify:**
```bash
cd /home/n1n3b1t/dev/claude-orchestra
.venv/bin/pytest tests/test_multi_mission_e2e.py -v
```
Expected: all green.

**Steps:**

- [ ] **Step 1: Author the test.**

Create `tests/test_multi_mission_e2e.py`:

```python
"""End-to-end: two consecutive missions in a fresh project."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestra import missions, state


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".orchestra").mkdir()
    db = tmp_path / ".orchestra" / "state.db"
    conn = state.connect(db)
    state.init_schema(conn)
    conn.close()
    (tmp_path / "README").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    return tmp_path


def _stub_run_internals(monkeypatch, terminal_kind="worker_done"):
    """Make run.run_mission skip the real PM spawn and the poll loop."""
    from orchestra import run, spawn
    def _fake_spawn_worker(conn, **kwargs):
        # Insert a minimal worker row directly so downstream code sees it.
        ts = state.now_iso()
        conn.execute(
            "INSERT INTO workers (id, task, model, branch, pane_target, status, "
            "started_at, updated_at, role, worktree, mission_id) "
            "VALUES (?, '', ?, NULL, 's:0.0', 'spawning', ?, ?, 'pm', NULL, ?)",
            (kwargs["worker_id"], kwargs["model"], ts, ts, kwargs.get("mission_id")),
        )
        # Immediately record the terminal event so the poll loop exits clean.
        state.record_event(conn, worker_id=kwargs["worker_id"], kind=terminal_kind, payload={})
        conn.commit()
    monkeypatch.setattr(spawn, "spawn_worker", _fake_spawn_worker)
    # Short-circuit any tmux operations the real code path would do.
    from orchestra import tmux
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "kill_session", lambda *a, **k: None)


def test_two_consecutive_missions(fresh_project, monkeypatch):
    project = fresh_project
    # Scaffold both missions.
    missions.scaffold_mission_dir(project, slug="alpha")
    missions.scaffold_mission_dir(project, slug="beta")
    _stub_run_internals(monkeypatch)

    from orchestra import run as run_mod
    code_a = run_mod.run_mission(Path("missions/alpha/mission.md"))
    assert code_a == 0, "first mission must complete cleanly"
    code_b = run_mod.run_mission(Path("missions/beta/mission.md"))
    assert code_b == 0, "second mission must complete cleanly (gate must allow)"

    conn = state.connect(project / ".orchestra" / "state.db")
    rows = state.list_missions(conn)
    assert {r.slug for r in rows} == {"alpha", "beta"}
    assert all(r.status == "done" and r.exit_code == 0 for r in rows)
    assert state.get_running_mission(conn) is None
    conn.close()


def test_sequential_gate_trips_when_another_running(fresh_project, monkeypatch):
    project = fresh_project
    missions.scaffold_mission_dir(project, slug="alpha")
    missions.scaffold_mission_dir(project, slug="beta")
    # Seed a stuck running mission.
    conn = state.connect(project / ".orchestra" / "state.db")
    state.create_mission(conn, slug="stuck", mission_path="(test)")
    conn.close()
    _stub_run_internals(monkeypatch)

    from orchestra import run as run_mod
    code = run_mod.run_mission(Path("missions/alpha/mission.md"))
    assert code == 2, "sequential gate must block while another mission is running"
```

- [ ] **Step 2: Run the test.**

```bash
.venv/bin/pytest tests/test_multi_mission_e2e.py -v
```
Expected: PASS for both tests.

- [ ] **Step 3: Run the full project test suite to verify no regressions elsewhere.**

```bash
.venv/bin/pytest -v --ignore=tests/test_web.py
.venv/bin/mypy orchestra/state.py orchestra/tmux.py
.venv/bin/mypy orchestra/
.venv/bin/ruff check orchestra/ tests/
```
Expected: all green.

- [ ] **Step 4: Commit.**

```bash
git add tests/test_multi_mission_e2e.py
git commit -m "test(missions): e2e two consecutive missions + sequential gate"
```
