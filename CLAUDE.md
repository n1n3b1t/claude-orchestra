# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Python orchestrator that drives multiple Claude Code instances from outside their processes via tmux. v1.0 ships a PM (role=pm) + engineers (role=engineer) topology where the PM coordinates engineers through hooks, git worktrees, and SQLite-backed shared state — proven by the URL-shortener e2e acceptance test (`scripts/e2e-build-urlshortener.sh`).

The thesis: a sufficiently capable Opus PM, given a few CLI primitives, can coordinate Sonnet engineers to autonomously build small software without human-in-the-loop.

## Setup + common commands

> **Always use `.venv/bin/pytest` and `.venv/bin/mypy`** — your shell's `pytest` may resolve to system Python which lacks the dev deps.

```bash
pip install -e ".[dev]"

# Tests. test_web.py currently can't be collected (sse-starlette is declared
# but the install order can miss it; see v1.2 issue #9). Use --ignore for now.
.venv/bin/pytest -v --ignore=tests/test_web.py

# Single test
.venv/bin/pytest tests/test_spawn.py::TestEventDrivenWaits::test_wait_idle_returns_true_when_session_ready_event_arrives -v

# Lint + type check. Two modules are strict-mypy (state.py, tmux.py); others
# are loose-mypy. The ruff selectors are E,F,I,B,UP,SIM at 100-col line length.
.venv/bin/ruff check orchestra/ tests/
.venv/bin/mypy orchestra/state.py orchestra/tmux.py    # strict modules
.venv/bin/mypy orchestra/                              # rest

# Run orchestra against a project (this repo or any other):
orchestra init                                # creates .orchestra/ + merges .claude/settings.local.json
orchestra spawn <id> <model> "<task>"         # single-worker v0 path
orchestra spawn pm opus "" --role pm --brief missions/urlshortener/mission.md
orchestra status                              # snapshot of all workers
orchestra tail <id>                           # follow the pane
orchestra dash                                # FastAPI dashboard on :8765

# E2e acceptance tests (consume API credits — opt-in):
./scripts/e2e-spawn.sh                        # v0 single-worker smoke
./scripts/e2e-build-urlshortener.sh           # v1 PM + 2 engineers full run (~8 min)
```

## Architecture — the big picture

The orchestrator is a thin coordinator on top of three substrates:

1. **tmux** — every Claude Code worker runs in its own tmux window. The orchestrator drives panes via `send-keys`/`paste-buffer` (multiline) and reads them via ANSI-stripped `capture-pane`. See `orchestra/tmux.py`.
2. **SQLite state.db** under `<project>/.orchestra/` — append-only `events` table + mutable `workers` and `escalations` tables. WAL mode, `busy_timeout=5000ms`. `events.kind` is free-form text — no schema migration when new kinds are added. See `orchestra/state.py`.
3. **Claude Code hooks** — every hook event (`SessionStart`, `Stop`, `Pre/PostToolUse`, `SessionEnd`, `Notification`) fires `orchestra worker hook <event>`, which reads the JSON payload from stdin and writes a typed event row. The hook command MUST exit 0 even on internal error — a non-zero hook breaks the worker's turn. See `orchestra/hooks.py`.

### Pre-run hook

`<project>/.orchestra/pre-run.sh` is an optional shell script that runs before any PM is spawned. It executes between the `.orchestra`-exists check and the PATH manipulation step in `run_mission`. If the script exits non-zero, `orchestra run` aborts with exit code 2 — the same as other pre-flight failures — so broken environment setup is caught before any API credits are spent. The canonical use case is `adb connect <ip>` to pre-warm a flaky network ADB session for on-device testing missions. When the file is absent the hook is silently skipped; when present but not executable, `orchestra run` emits a warning and proceeds.

### Data flow

```
Claude Code worker pane
  │
  ├── tmux send-keys ──────────────┐
  │   (orchestra → worker)         │
  │                                ▼
  └── stdin to `orchestra worker hook EVENT` (Claude Code → orchestra)
       │
       ▼
     state.db
       │
       ▼
     orchestra poll  (PM reads bounded snapshot)
       │
       ▼
     PM acts (send / answer / merge / reap)
```

The PM never reads worker panes directly. It reads `state.db` via `orchestra poll` (returns a markdown state-snapshot table) and writes back via `orchestra send/answer/merge/reap`. The pane itself is for the worker to use; the orchestrator only writes to it (via `send_multiline`) when the PM explicitly asks.

### PM execution model — single mega-turn

The PM stays in **one** Claude Code response for the whole orchestration: `orchestra poll → act → poll → act → …` until the verifier passes or it gives up. `Stop` fires once at the very end. There's no per-cycle heartbeat — Claude Code's `Stop` hook fires on response end, not between tool calls, so single-mega-turn is the natural model.

`orchestra poll` returns a **bounded** snapshot (per-engineer status, new-event count, pending escalations, last status message) so the PM's context stays compact across hundreds of polls.

### Worktree-per-engineer pattern

Each engineer is spawned with `--worktree <name>`, which creates a git worktree at `worktrees/<name>` on branch `orch/<worker_id>`. Engineers commit there; only the PM checks out main and merges via `orchestra merge <id>`. After merge, the PM runs `orchestra reap <id>` to delete the worktree + branch.

**Important quirk:** a git worktree has a `.git` *file* (gitlink), not a `.git/` directory. Claude Code's project-root detection stops at that boundary, so the engineer's session **does not** inherit the parent project's `.claude/settings.local.json`. To make hooks fire inside worktrees, `orchestra/worktree.py:add` also runs `settings_merge.ensure_hooks(<worktree>/.claude/settings.local.json)`. If you ever change how hooks are detected, this is the critical cross-module invariant.

**Worktree namespace:** engineers' worktrees live at `worktrees/<mission_slug>/<worker_id>` on branch `orch/<mission_slug>/<worker_id>`. The mission slug is resolved by `orchestra/worktree.py:_resolve_mission_slug` — it reads the currently-running `missions` row when not passed explicitly. If you change how the slug is resolved, also update `cli._branch_for` (the merge/reap path).

### Missions

`state.db.missions` is the canonical record of every orchestrated run. Each row has a unique slug, a path to its mission file, a status (`running | done | failed | aborted | archived`), and timestamps. Worker rows carry a `mission_id` foreign key.

Only one mission may have `status='running'` at a time — `orchestra run` enforces this via a pre-flight check that consults `state.get_running_mission`. Direct `orchestra spawn` invocations outside a mission inherit the running one if any; otherwise they leave `mission_id = NULL` (legacy workers from v2.3 are backfilled under a single `legacy-<ts>` archived mission on first init).

Worktrees and branches are namespaced under the mission slug: `worktrees/<slug>/<id>` on `orch/<slug>/<id>`. Two missions can have an engineer named `backend` without collision. Legacy pre-v2.4 worktrees may still exist at the old flat paths (`worktrees/<id>`); they are not auto-migrated by the runtime — clean them up with `git worktree remove` if needed.

### Two prompt-template modules

- `orchestra/prompts.py` — v0 single-role template (`render_startup_prompt`). Still used when `--role` is absent on spawn. Don't merge it with role_prompts.
- `orchestra/role_prompts.py` — v1 PM + Engineer templates. PM template enforces mega-turn discipline + phase-status writes + `/compact` advisory; engineer template enforces worktree-only writes + escalate-on-uncertainty.

`spawn._render_startup_prompt` branches on `role` to pick which template to render. v0 callers (no `--role` flag) hit the v0 path; v1 callers hit the role-aware path.

### Role templates and per-role permissions (v2.0)

**Role templates** (`orchestra/roles/*.md`) are loaded by `role_prompts.py`
with project overrides at `<project>/.orchestra/roles/<name>.md` taking
precedence. Each may carry YAML front matter with `permissions:` that
gets merged into the worker's settings.local.json before spawn — that's
how the v2.0 reviewer pattern is built without any orchestra-side
"read-only" flag.

### Event-driven spawn waits (not pane-polling)

v1 replaced v0's `tmux capture-pane` polling with DB-event waits:
- `_wait_idle_via_event(conn, worker_id, target=)` blocks until a `session_ready` event arrives (or timeout). When `target` is supplied, it also runs trust-prompt dismissal in parallel via `tmux.capture` — the trust prompt fires *before* `SessionStart`, so hooks alone can't catch it.
- `_wait_first_status_via_event(conn, worker_id)` blocks until first `turn_complete` event (NOT the cooperative `status` kind — that has a separate purpose).

Both timeouts are "soft": on timeout, status becomes `stale_spawn` and the spawn flow continues. A worker whose hooks are slow can still recover.

### Coordination CLI surface

These are the commands the PM (and any user) uses to coordinate:

- `orchestra mission new <slug>` — scaffolds `missions/<slug>/{mission.md, verifier.sh}`
- `orchestra mission list` — lists all missions in `state.db` with status + timestamps
- `orchestra mission show <slug>` — detailed view of one mission (workers, events)
- `orchestra mission run <slug>` — shortcut for `orchestra run missions/<slug>/mission.md`
- `orchestra send <id> "<msg>"` — types into a worker's pane (PM → engineer nudge)
- `orchestra answer <escalation_id> "<answer>"` — resolves an escalation + delivers the answer to the asker's pane
- `orchestra poll [--timeout 30] [--caller pm]` — blocking; returns the bounded state snapshot. Cursor is per-caller at `.orchestra/poll-cursor.<caller>`
- `orchestra merge <id>` — `git merge orch/<id>` from cwd; records `merge_attempted` + `merge_ok` or `merge_conflict`
- `orchestra reap <id>` — `git worktree remove --force` + `git branch -D`
- `orchestra worker done --summary "..."` — cooperative termination signal; sets `status=done` + records `worker_done` event. The e2e watchdog watches for this on the PM.

### E2e driver

`scripts/e2e-build-urlshortener.sh` is the v1 acceptance contract. It runs three background watchdogs:

| Watchdog | Default | Exit code | Mechanism |
|---|---|---|---|
| wall-clock | 5400 s (90 min) | 124 | timer |
| activity | 600 s (10 min) | 125 | no new event rows in state.db |
| cost | $10 | 126 | sum `turn_complete` token counts × per-model pricing |

The cost watchdog is currently non-functional — Claude Code's `Stop` hook payload doesn't carry usage data; the real fix needs to read `transcript_path`. See v1.2 issue #8.

The driver also kills any leftover tmux session matching `orch-<projectname>` at the start *and* on EXIT, so a failed run doesn't poison the next attempt.

## Conventions used throughout

- `from __future__ import annotations` at the top of every module
- Frozen dataclasses for state types (`Worker`, `Event`, `Escalation`)
- Heavy imports (`from orchestra import state`, `from orchestra import poll`) are deferred inside CLI command bodies, not at module top, to keep `orchestra --help` and the worker-hook subprocess startup fast
- Hook handlers MUST exit 0 — broad `except Exception: # noqa: BLE001` with a comment explaining the contract is the standard pattern
- Tests are class-grouped (`class TestX:`), use `monkeypatch` for env/subprocess, and use the `tmp_db` / `tmp_orch_dir` fixtures from `tests/conftest.py`
- `tmux.send_multiline` (not `send_literal`) for any content with embedded newlines — bare `send-keys` breaks on `\n`
- All pane captures go through `_strip_ansi` before regex matching

## Where the design lives

- Specs: `docs/superpowers/specs/2026-05-1{6,7}-claude-orchestra*.md` — the v0 and v1 designs. The v1 spec is the source of truth for orchestration semantics.
- Plans: `docs/superpowers/plans/` — task-by-task implementation plans (each plan ships a `.tasks.json` sibling for resumable execution).
- Release notes: `CHANGELOG.md` — v1.0 ships the headline metrics + known follow-ups.
- Open follow-ups: GitHub milestone v1.2 — https://github.com/n1n3b1t/claude-orchestra/milestone/1
