# claude-orchestra v0 — design

**Status:** approved
**Author:** Claude (Opus 4.7) with n1n3b1t
**Date:** 2026-05-16
**Use case:** long-running parallel Claude Code workers driven from a single project; observability is the primary user-visible win over existing tools.

## Background

Two reference implementations were evaluated (see `research/synthesis.md`):

- **Jedward23/Tmux-Orchestrator** is prompt-driven; orchestration intelligence lives in a 716-line `CLAUDE.md`. The shell layer is thin and brittle (no idle detection, no send verification, hardcoded paths).
- **primeline-ai/claude-tmux-orchestration** is infrastructure-driven; ~700 lines of bash + jq implement spawn, adaptive heartbeat, send-verify-retry, rate-limit watchdog, file-based coordination. The architecture is right; the language and the lack of observability are wrong for our use.

Load-bearing patterns shared by both:
1. `tmux send-keys -l "..."` + separate `Enter` for reliable injection.
2. `load-buffer` / `paste-buffer -p -d` for multi-line prompts.
3. ANSI-strip before any regex match against `capture-pane`.
4. Idle detection: spinner regex first (overrides idle), then prompt regex.
5. Verify-on-send + retry against `capture-pane`.
6. `--dangerously-skip-permissions` for workers; trust boundary is the project directory.

claude-orchestra adopts all six and adds: SQLite-backed state, a local web dashboard, worker-initiated status writes via a tiny CLI.

## Goals

- A user can `orchestra init` a project, `orchestra spawn w1 sonnet "task"` a worker, and `orchestra dash` to watch it live in a browser.
- The worker pane runs under `--dangerously-skip-permissions` but is identifiable to hooks via `ORCHESTRA_WORKER_ID`.
- Worker status updates (`orchestra worker status …`) and escalations (`orchestra worker escalate …`) land in SQLite within a second.
- The dashboard reflects state within ~2s of any change. Events stream via SSE; live pane peeks via short-poll. Both endpoints exposed so the UI can fall back to polling for events if SSE turns out fiddly.
- The user can answer an escalation from the dashboard; the answer is delivered to the worker pane via `send-keys`.

## Non-goals for v0

- More than one worker at a time (v0.1).
- Heartbeat / adaptive cycle pings (v0.1).
- Rate-limit watchdog (v0.2).
- Worktree-per-worker isolation (v0.2).
- Per-tool permission allowlists (v0.2+).
- Multi-host, multi-user, auth, encryption (out of scope indefinitely; this is a single-developer-machine tool).

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Project directory (where you run `orchestra ...`)         │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  tmux session ("orch-<projectname>")                 │  │
│  │  ┌─────────┐  ┌─────────┐                            │  │
│  │  │ window 0│  │ window 1│                            │  │
│  │  │ shell / │  │ worker  │                            │  │
│  │  │ you     │  │ w1      │                            │  │
│  │  │         │  │ (claude)│                            │  │
│  │  └─────────┘  └────┬────┘                            │  │
│  └────────────────────┼─────────────────────────────────┘  │
│                       │ send-keys / capture-pane           │
│                       ▼                                    │
│  .orchestra/                                               │
│    state.db        ← SQLite (WAL, busy_timeout=5s)         │
│    config.toml     ← intervals, thresholds, paths          │
│    panes/          ← rotated raw capture-pane snapshots    │
│                                                            │
│  Python package `orchestra` (one process per role):        │
│    orchestra spawn   - choreography, then exits            │
│    orchestra dash    - FastAPI on localhost, long-running  │
│    orchestra worker  - tiny CLI workers call to write      │
│                        status into state.db                │
└────────────────────────────────────────────────────────────┘
```

**State is project-local** under `.orchestra/`. Two projects = two state DBs = two swarms, no naming clashes.

**Workers write status by shelling out** to `orchestra worker status --progress "..." --turns 42` rather than writing JSON files. The schema is enforced by the CLI; the Claude instance in the pane only has to remember one command name. Trade-off: each call spawns a Python process. Fine for the ~1-per-minute write rate; not fine if v0.1 ever wants per-turn updates.

**Dashboard is a separate long-running process.** `orchestra dash` starts FastAPI on localhost. Spawn and the worker CLI are short-lived processes that write to the same DB.

## Components

```
orchestra/
├── __init__.py
├── tmux.py        # Driver primitives (~150 lines)
├── state.py       # SQLite layer (~200 lines)
├── spawn.py       # Worker boot choreography (~150 lines)
├── cli.py         # Typer commands (~150 lines)
├── web.py         # FastAPI app (~200 lines)
├── prompts.py     # Worker startup prompt template (~80 lines)
└── templates/index.html
tests/
├── test_tmux.py
├── test_state.py
├── test_prompts.py
├── test_spawn.py
├── test_cli.py
└── test_web.py
pyproject.toml
scripts/e2e-spawn.sh
```

### orchestra.tmux

Primitives only; no business logic.

```python
send_literal(target: str, text: str) -> None
send_multiline(target: str, text: str) -> None   # load-buffer + paste-buffer -p -d
send_enter(target: str) -> None
capture(target: str, lines: int = 200) -> str    # ANSI-stripped
is_idle(target: str) -> bool                     # spinner-first, then prompt regex
pane_current_command(target: str) -> str
ensure_session(name: str, cwd: str) -> None
new_window(session: str, name: str, cwd: str) -> str  # returns "session:window"
```

The ANSI stripper matches primeline's regex set (CSI, OSC, DCS, charset switches, SI/SO). Idle detection: if the last 12 lines contain any of `Running|thinking|Searching|Reading|Writing|Editing`, return `False` regardless of prompt. Otherwise return `True` if `❯` or `>` is at end of a line, else `False` (safe default: assume busy).

### orchestra.state

SQLite with WAL and busy_timeout=5000ms. Tables:

```sql
CREATE TABLE workers (
  id          TEXT PRIMARY KEY,
  task        TEXT NOT NULL,
  model       TEXT NOT NULL,
  branch      TEXT,
  pane_target TEXT NOT NULL,
  status      TEXT NOT NULL,   -- spawning | working | waiting | stale_spawn | stopped | error | done
  progress    TEXT,
  turns       INTEGER NOT NULL DEFAULT 0,
  started_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id TEXT,                 -- nullable for orchestra-level events
  ts        TEXT NOT NULL,
  kind      TEXT NOT NULL,        -- spawn_start|spawn_window|spawn_idle|spawn_timeout|model_switched|prompt_injected|spawn_ok|status|escalation|escalation_resolved|stopped|...
  payload   TEXT                  -- JSON-encoded
);
CREATE TABLE escalations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id  TEXT NOT NULL,
  ts         TEXT NOT NULL,
  question   TEXT NOT NULL,
  context    TEXT,
  blocking   INTEGER NOT NULL,
  resolved   INTEGER NOT NULL DEFAULT 0,
  answer     TEXT
);
```

Wrappers return dataclasses (`Worker`, `Event`, `Escalation`). `record_event(conn, kind, worker_id=None, **payload)` is the only event-write API; payload is JSON-encoded.

### orchestra.prompts

`render_startup_prompt(worker_id, task, model, ctx_files) -> str`. The rendered prompt contains:

- Worker identity and task verbatim.
- Branch name `orch/<worker_id>`.
- Rules: commit yes, push no; do not spawn additional workers; do not end the session yourself; call `orchestra worker status --progress "..." --turns N` periodically (target: every ~20 turns or after a meaningful milestone).
- Escalation directive: when uncertain, run `orchestra worker escalate --blocking --question "..." --context "..."` rather than guessing.
- Context file section (optional): a bullet list of files to read first when `ctx_files` is non-empty.

### orchestra.spawn

End-to-end choreography in 7 steps. Each step records an event so the dashboard can show spawn progress live.

1. `state.create_worker(id, task, model, branch, pane_target, status="spawning")`; event `spawn_start`.
2. `tmux.ensure_session(...)`, `tmux.new_window(...)`; event `spawn_window`.
3. `tmux.send_literal(target, "ORCHESTRA_WORKER_ID=<id> ORCHESTRA_STATE_DB=<path> claude --dangerously-skip-permissions")`, then `send_enter`.
4. Poll `tmux.is_idle` every 3s, max 60s. On success, event `spawn_idle`; double-Enter to dismiss any trust/welcome prompt. On timeout, set `status=error`, event `spawn_timeout` with the last 20 captured lines, return.
5. `tmux.send_literal(target, "/<model>")` + `send_enter`. Sleep ~3s. Event `model_switched`.
6. `tmux.send_multiline(target, prompts.render_startup_prompt(...))` with 3 retries (post-send verify via capture-pane). Event `prompt_injected`.
7. Poll DB every 5s for the worker's first `status` event, max 90s. On success, set `status=working`, event `spawn_ok`. On timeout, set `status=stale_spawn`, event `spawn_first_status_timeout`. Window stays alive in both cases.

### orchestra.cli (Typer)

```
orchestra init                                            # create .orchestra/, state.db, config.toml
orchestra spawn <id> <model> <task> [--context FILE ...]  # delegate to spawn.spawn_worker
orchestra status [--worker ID]                            # plain-text snapshot
orchestra tail <worker>                                   # follow live capture (calls tmux.capture in a loop)
orchestra stop <worker>                                   # status=stopped, send Ctrl-C twice (0.5s gap) to exit claude
orchestra worker status --progress STR --turns INT        # workers call this
orchestra worker escalate [--blocking] --question STR --context STR
orchestra dash [--port 8765]                              # uvicorn orchestra.web:app
```

`worker` subcommands require `ORCHESTRA_WORKER_ID` and `ORCHESTRA_STATE_DB` in env; exit 2 with a clear message otherwise (prevents accidental writes from the user's shell).

### orchestra.web (FastAPI)

```
GET  /                              -> single-page dashboard (Jinja2 template, vanilla JS)
GET  /api/workers                   -> list of workers + last status
GET  /api/workers/{id}              -> detail + recent events (last 50)
GET  /api/workers/{id}/pane?lines=N -> calls tmux.capture, returns text
GET  /api/stream                    -> SSE; pushes new events as they're written
POST /api/workers/{id}/message      -> tmux.send_multiline to worker
POST /api/workers/{id}/answer       -> resolve escalation + send answer to worker pane
```

The dashboard HTML loads the worker list, renders a card per worker (status, progress, turns, last event, pane peek), and subscribes to `/api/stream` for incremental updates. No framework, no bundler.

## Data flow

**Spawn:** `orchestra spawn` → state row created → tmux session/window → claude boots → idle poll → `/model` → multiline prompt injection → first-status wait → status=`working`. Every step writes an event; dashboard renders spawn progress live.

**Worker progress:** Claude in worker pane runs `orchestra worker status --progress "..." --turns 12`. CLI reads ENV, updates worker row + writes `kind=status` event. `/api/stream` pushes to the browser.

**Escalation:** Worker runs `orchestra worker escalate --blocking --question "..." --context "..."`. Escalation row created; worker status=`waiting`; event `escalation`; SSE push; dashboard shows a banner with a textarea. User POSTs to `/api/workers/{id}/answer`; escalation resolved; answer sent to worker pane via `send_multiline`; worker status=`working`; event `escalation_resolved`.

**Live pane peek:** Dashboard polls `/api/workers/{id}/pane?lines=80` every ~2s; web calls `tmux.capture` and returns ANSI-stripped text into a `<pre>` block.

Two channels feed the dashboard: cooperative status writes by the worker, and the always-on pane capture. If the worker forgets to call `worker status`, the pane peek still shows what's happening.

## Error handling

| Where | Failure | Behavior |
|---|---|---|
| Spawn | `tmux` or `claude` binary missing | `init` preflights; `spawn` re-checks; exit 2 with binary name. |
| | Boot timeout (60s) | `status=error`, event with last 20 captured lines; window kept alive. |
| | Send-verify-retry exhausted | Same: `status=error`, event with undelivered message. |
| | First-status timeout (90s) | `status=stale_spawn`; event recorded; not an error. |
| DB | `database is locked` past `busy_timeout` | Surface as 500 (web) / exit 3 (CLI). |
| | `state.db` missing | Every command except `init` fails fast: "Run `orchestra init` first." |
| Worker CLI | Missing `ORCHESTRA_WORKER_ID` env | Exit 2: "must run inside a spawned worker pane." |
| | Worker ID present but tmux window gone | `update_worker` still succeeds; event logged; `orchestra reconcile` (v0.1) cleans up. |
| Web | SSE client drops | Server-side cleanup of dead queue on next write. |
| | Worker not found | 404 JSON. |
| | DB read fails mid-write | 503; dashboard retries on next poll. |
| Escalation | Answer for already-resolved escalation | 409; no double-send to worker. |

Anything not listed lands in `events` with `kind=internal_error` and the traceback as payload.

## Testing

**Ring 1 — Unit (pytest, <1s each)**
- `test_tmux.py`: every driver function with `subprocess.run` patched; assert exact argv shape (`-l` flag, separate `Enter`, `paste-buffer -p -d`).
- `test_state.py`: real sqlite in tmpdir; CRUD round-trip; WAL + busy_timeout asserted.
- `test_prompts.py`: render with several arg combinations; assert required directives are present.

**Ring 2 — Integration (pytest, seconds)**
- `test_spawn.py`: mock `orchestra.tmux` at module level; assert 7-step sequence in order with retries on simulated send failures; verify event rows in real sqlite.
- `test_cli.py`: Typer `CliRunner`; cover `init`, `spawn` (mocked tmux), `worker status` (success + missing env), `worker escalate`.
- `test_web.py`: FastAPI `TestClient`; cover REST contract + SSE smoke + escalation answer paths.

**Ring 3 — End-to-end (manual, opt-in)**
- `scripts/e2e-spawn.sh`: spawn a real claude worker against a trivial task; verify first status event within 90s. Not in CI.

**Tooling**
- `pytest`, `pytest-asyncio`, `ruff`, `mypy --strict` on `state.py` + `tmux.py`.
- Coverage target: 80% on `tmux.py` and `state.py`; 60% overall.

## Roadmap after v0

- **v1.0 (drafted):** replace screen-scraping state detection with Claude Code hook events. See `2026-05-17-claude-orchestra-v1-design.md`.
- **v1.1:** multiple concurrent workers, heartbeat with adaptive intervals (30s/120s/300s), `orchestra reconcile` for orphan cleanup.
- **v1.2:** rate-limit watchdog (steal primeline's retry message verbatim), worktree-per-worker spawn, per-tool permission allowlists via Claude Code hook gating.
- **v1.3:** structured inter-worker messaging (one worker can ask another a question through the orchestrator).

## Open questions deferred to implementation

- Pane snapshot rotation policy for `.orchestra/panes/` — start with "keep last 50 captures per worker, prune older"; expose in `config.toml`.
- Exact event taxonomy may grow during spawn implementation; the schema (`kind TEXT, payload JSON`) is forgiving.
- Whether `orchestra worker status` is one binary call per write, or batches via a tiny long-lived helper. v0 takes the simple path (one call per write); revisit if turn-rate writes ever become a thing.

## References

- `research/Tmux-Orchestrator/` — Jedward23's prompt-driven approach.
- `research/claude-tmux-orchestration/` — primeline's infrastructure-driven approach.
- `research/synthesis.md` — full comparative analysis.
- `bin/claude-tmux-driver.sh` — starter bash driver from earlier in the session; superseded by `orchestra.tmux` but useful as a sanity check.
