# Design: multi-mission support

**Date:** 2026-05-22
**Status:** design / pre-implementation
**Companion:** GETTING_STARTED.md gets rewritten in step 4 once this ships.

## Context

Today the orchestrator treats a single PM run as the unit of work. `orchestra
run <mission.md>` spawns a PM whose worker row lives in `state.db.workers`
under the fixed id `pm`. When that PM finishes, the row stays — there is no
"mission" entity. The pre-flight check in `orchestra/run.py:114-126`
("a worker row for 'pm' already exists") then blocks any second run on the
same project until a user manually deletes rows via `sqlite3`.

The result: orchestra works for one mission. Running a second mission on the
same project requires raw SQL surgery. There is no way to ask "what missions
has this project run?" without grepping git history.

This spec adds first-class missions as a project-level concept, keeps strict
sequentiality (only one mission running at a time), namespaces worktrees under
the mission slug to avoid cross-mission collisions, and exposes mission
history via CLI + dashboard.

## Goals

1. Two consecutive `orchestra mission run <slug>` invocations on the same
   project must succeed without manual cleanup, provided the prior mission
   reached a terminal state.
2. A user can list every mission this project has run and inspect details for
   any one of them, including legacy missions from before this change shipped.
3. Engineer worker names can be reused across missions without git-worktree or
   git-branch collisions — even if the previous mission ended in failure and
   left zombie worktrees.
4. The schema upgrade auto-migrates existing `state.db` files (the maintainer's
   urlshortener mission is preserved as a legacy archive entry) without
   requiring `rm .orchestra/state.db`.
5. The dashboard shows the list of missions and lets the user pick which one to
   view; the default view is the active or most-recently-finished mission.

## Non-goals

- Concurrent missions. The constraint "only one mission with `status='running'`
  at a time" is hard-enforced.
- A "resume failed mission" feature. Re-running the same slug starts a fresh
  attempt; the prior failed row stays in history.
- A general-purpose query language over events. `orchestra mission show` ships
  a hard-coded view (last N events + worker summary).
- Removing the `.orchestra/mission.md` convention. Soft-deprecated only —
  `orchestra run` still accepts it.
- Per-mission state.db files. One shared DB, mission_id column.

## Schema additions

### New table: `missions`

```sql
CREATE TABLE IF NOT EXISTS missions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,
    mission_path TEXT NOT NULL,           -- relative to project root
    status       TEXT NOT NULL,           -- running | done | failed | aborted | archived
    exit_code    INTEGER,                  -- null while running
    started_at   TEXT NOT NULL,
    ended_at     TEXT                      -- null while running
);
CREATE INDEX IF NOT EXISTS missions_status ON missions(status);
```

### New column on `workers`

```sql
ALTER TABLE workers ADD COLUMN mission_id INTEGER REFERENCES missions(id);
```

Added by `init_schema` via the same forward-compat ALTER pattern already used
at `state.py:188-197` for the `role` and `worktree` columns.

### Status lifecycle

| Status     | Set by                                             |
|------------|----------------------------------------------------|
| `running`  | `orchestra run` immediately after pre-flight       |
| `done`     | `run_mission` on `worker_done` from the PM         |
| `failed`   | `run_mission` on watchdog fire or non-zero PM exit |
| `aborted`  | Future `orchestra mission abort` (out of scope v1) |
| `archived` | Migration only — never set at runtime              |

### One-shot migration

When `init_schema` runs and observes (a) `workers` is non-empty AND (b) all
`workers.mission_id` are NULL AND (c) `missions` is empty — it auto-archives:

1. Insert one mission row with `slug = "legacy-<YYYYMMDD-HHMM>"` (derived from
   the oldest worker's `started_at`), `mission_path = "(unknown)"`,
   `status = "archived"`, `started_at` = oldest worker's `started_at`,
   `ended_at` = newest worker's `updated_at`.
2. Backfill every existing worker row's `mission_id` to that mission.

Event rows are not backfilled — they have `worker_id`, which already joins
through `workers` to `missions`.

## CLI surface

### `orchestra mission new <slug>`

Validates slug against `^[a-z0-9][a-z0-9-]*$` and that no existing mission row
already uses it. Scaffolds:

- `missions/<slug>/mission.md` — same template the getting-started guide
  ships, with placeholder goal/acceptance/team sections and a `worker_done`
  reference.
- `missions/<slug>/verifier.sh` — executable skeleton that exits 0.

Does **not** insert a `missions` row. The row is created lazily by the next
`orchestra run` (or `orchestra mission run`) that references the slug. This
keeps `mission new` purely a scaffolding command.

### `orchestra mission list`

Prints a markdown table sorted by `started_at` desc:

```
| slug                    | status   | started_at           | duration | exit |
|-------------------------|----------|----------------------|----------|------|
| urlshortener-v2         | running  | 2026-05-22T14:03:00Z | 00:08:14 | -    |
| kanban-prototype        | done     | 2026-05-21T19:11:00Z | 01:42:33 | 0    |
| legacy-20260520-0942    | archived | 2026-05-20T09:42:00Z | -        | -    |
```

The running mission (if any) is highlighted in the rendered table — wrap its
row in `**bold**` markers so it stands out when rendered.

### `orchestra mission show <slug>`

Prints three blocks for the given mission:

1. Mission row metadata (slug, status, started/ended, duration, exit code,
   mission_path).
2. A markdown table of every worker that ran in this mission: `id | role |
   model | status | started_at | turns`.
3. The last 20 event rows for those workers, in chronological order.

Errors with exit 2 if the slug does not exist.

### `orchestra mission run <slug>`

Shortcut. Equivalent to `orchestra run missions/<slug>/mission.md` with the
same `--model`, `--max-wallclock`, `--max-activity`, `--allow-dirty` flags
plumbed through.

## `orchestra run` flow changes

New pre-flight order in `orchestra/run.py:run_mission`:

1. Existing checks unchanged: git repo, clean tree (unless `--allow-dirty`),
   mission file exists, `.orchestra/state.db` exists, optional `pre-run.sh`.
2. **Sequential gate.** Query `missions` for any row with `status='running'`.
   If found, error with `"mission '<slug>' is still running; finish or abort
   it first"` and exit 2.
3. **Slug resolution.** Inspect the mission file path:
   - If it matches `missions/<slug>/mission.md`, use `<slug>`.
   - Else generate `m-<YYYYMMDD>-<HHMM>` from the current UTC time; if that
     slug already exists in `missions`, append `-2`, `-3`, ... until unique.
4. **Mission row creation.** Insert with `status='running'`, `mission_path` =
   the path passed to `run` (made relative to project root if absolute).
5. **PM-row pre-flight (scoped).** The existing check at
   `run.py:114-126` becomes `state.get_worker(conn, "pm", mission_id=<new>)`.
   Old PM rows from prior missions are fine — they have their own
   `mission_id`.
6. PM spawn as before. The PM's worker row is written with `mission_id` set.
7. On terminal event: update the mission row with `status`, `exit_code`,
   `ended_at`. The four exit-code → status mappings:
   - PM emitted `worker_done` → `status='done'`, `exit_code=0`.
   - Wall-clock watchdog (124) → `status='failed'`, `exit_code=124`.
   - Activity watchdog (125) → `status='failed'`, `exit_code=125`.
   - Defensive loop-exit (126) → `status='failed'`, `exit_code=126`.

If the process is killed externally (SIGTERM, parent shell closes), the
mission row stays `running`. A follow-up issue will track adding a "clean
up stuck running missions" command; not in v1.

## Worktree namespacing

`orchestra/worktree.py:add` currently creates `worktrees/<worker_id>/` on
branch `orch/<worker_id>`. Both paths change to include the mission slug:

- Worktree: `worktrees/<mission_slug>/<worker_id>/`
- Branch: `orch/<mission_slug>/<worker_id>`

The slug is read from state.db: there is at most one mission with
`status='running'`, so `worktree.add` looks it up and embeds it. The change
keeps two-mission engineer-name reuse safe: mission A's `backend` lives at
`worktrees/A/backend/` on `orch/A/backend`; mission B's `backend` at
`worktrees/B/backend/` on `orch/B/backend`.

`orchestra merge <id>` and `orchestra reap <id>` look up the worker's
`mission_id` to compute the branch/worktree path. The PM's invocation
syntax is unchanged.

**Legacy worktrees from pre-migration missions** (paths like
`worktrees/backend`, branches like `orch/backend`) are not moved by the
migration step — they may not exist on disk anymore, and physically renaming
git worktrees in someone else's repo would be hazardous. If they do exist
and the user wants to clean up, they can run `git worktree remove
worktrees/<id>` manually. The legacy mission row stays in history regardless.

## Engineer mission inheritance

When the PM types `orchestra spawn <id> <model> "<task>" --worktree <name>`
into its pane, the `orchestra spawn` CLI receives the call. The spawn
command does not know which worker invoked it. Today this is fine because
there is only one mission's worth of state.

New behaviour: `orchestra spawn` (and any other PM-invoked sub-command that
creates worker rows) reads the currently-running mission from state.db
(`SELECT id, slug FROM missions WHERE status='running'`) and writes that
`mission_id` onto the new worker row.

**Direct `orchestra spawn` outside a mission** (the v0 path described in
`CLAUDE.md` — a user types `orchestra spawn id model "task"` directly,
without going through `orchestra run`) is preserved. When no mission has
`status='running'`, the new worker row is written with `mission_id = NULL`.
Such workers do not appear in `orchestra mission list` or
`orchestra mission show` queries; they still appear in `orchestra status`
(which has never been mission-scoped). The sequential gate on
`orchestra run` only inspects the `missions` table — a v0 spawn does NOT
block a subsequent `orchestra run` invocation.

Rationale: only `orchestra run` (and its `mission run` shortcut) has a
monitoring loop that can move a mission to a terminal status. Direct spawn
has no such loop, so creating a `missions` row for it would leave the row
stuck at `running` forever and lock out the sequential gate. Keeping
`mission_id = NULL` for direct spawns sidesteps this without breaking the
v0 path.

This requires no change to the PM's prompt or to how PM types commands.

## Dashboard (`orchestra/web.py`)

The dashboard already loads `state.db` rows and renders them. Two changes:

1. **Mission switcher.** A persistent dropdown in the top nav lists every
   mission (descending by `started_at`). Selecting one sets a
   `?mission=<slug>` query param; default selection is the running mission
   if any, otherwise the most recent.

2. **`/missions` page.** Equivalent to `orchestra mission list` but HTML.
   Each row links to `?mission=<slug>` on the existing worker view.

Existing pages (worker list, event stream) filter by the selected mission
via the new query param.

## Documentation impact

- `GETTING_STARTED.md` — step 4 ("Write a mission file") gets rewritten to:
  `orchestra mission new <slug>` → edit the scaffolded `mission.md` → run
  with `orchestra mission run <slug>`. The "between missions" gap is closed
  by the new flow.
- `CLAUDE.md` — add a "Missions" subsection under "Architecture — the big
  picture". Update the data-flow ASCII block to show the missions table.
  Document the worktree namespace change as a critical cross-module
  invariant (alongside the existing settings-merge invariant).
- `README.md` — update the "One-shot runner" section to reference
  `orchestra mission new` + `orchestra mission run`.
- `examples/urlshortener-mission.md` and `examples/kanban/` — move under
  `missions/urlshortener/` and `missions/kanban/` to match the new
  convention. Old paths (`.orchestra/mission.md`) still work but are no
  longer recommended.

## Out of scope (explicit non-decisions)

- Concurrent missions.
- `orchestra mission abort` / `orchestra mission reset <slug>`.
- "Resume failed mission" semantics.
- Removing the `.orchestra/mission.md` convention.
- Backfilling event rows with `mission_id`.
- Renaming or moving legacy worktrees.
- Per-mission watchdog tuning.

## Open follow-ups (post-v1)

- `orchestra mission abort` — set `status='aborted'`, kill the PM pane, reap
  engineer worktrees.
- "Stuck running mission" detector — on `orchestra status`, flag missions
  whose tmux session is no longer alive but whose row says `running`.
- Mission-scoped cost reporting — sum `turn_complete` token counts per
  mission.
- Export/import missions (so a successful mission's setup can be shared).
- Dashboard event-stream improvements that exploit the new per-mission
  filter.

## Verification of this change

Before merge:

1. `pytest -v --ignore=tests/test_web.py` passes.
2. `mypy orchestra/state.py orchestra/tmux.py` strict mode passes.
3. `mypy orchestra/` (loose) passes.
4. `ruff check orchestra/ tests/` passes.
5. A new end-to-end test (or extension of `scripts/e2e-build-urlshortener.sh`):
   - Boot a fresh project with empty `state.db`.
   - `orchestra mission new test-a` then `orchestra mission run test-a` with
     a trivial mission (mock or short verifier).
   - On done, `orchestra mission new test-b` and `orchestra mission run
     test-b`.
   - Assert both rows exist with status=done, exit_code=0, distinct
     worktree paths during their runs.
6. A targeted migration test:
   - Construct a `state.db` with the v2.3 schema and one PM worker row.
   - Call `init_schema` against it.
   - Assert one `legacy-*` mission row exists with status=`archived` and
     the worker row's `mission_id` matches.
