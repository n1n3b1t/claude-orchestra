# Changelog

## v2.4 — multi-mission support (2026-05-23)

- New `missions` table in `state.db`. Every `orchestra run` creates a mission row; workers carry a `mission_id` foreign key.
- New CLI surface: `orchestra mission new <slug>`, `orchestra mission list`, `orchestra mission show <slug>`, `orchestra mission run <slug>` (shortcut for `orchestra run missions/<slug>/mission.md`).
- Sequential gate: `orchestra run` refuses to start while another mission has `status='running'`. The error names the offending slug.
- Worktrees and branches namespaced by mission slug: `worktrees/<slug>/<id>` on `orch/<slug>/<id>`. Two missions can reuse engineer names without git collisions.
- Dashboard: top-of-page mission switcher; `/api/missions` endpoint; `/api/workers?mission=<slug>` filter.
- Auto-migration: on first start against a v2.3 DB, all pre-existing worker rows are archived under a single `legacy-<ts>` mission with `status='archived'`. No manual SQL surgery required.
- Examples relocated: `examples/urlshortener-*` → `missions/urlshortener/`; `examples/kanban/` → `missions/kanban/`. The `examples/` directory is removed.
- Legacy `orchestra run <path>` is soft-deprecated: still works, generates a timestamp-derived slug when the path is not under `missions/<slug>/`.

## v2.3 — model fix + DX trio + closeout (2026-05-22)

**Closeout batch (four issues, all orchestrated):**

- **#32 mission lint false-positive** — brief-not-found now produces a warning by default; `--strict` flag promotes back to error. Unblocks missions where briefs are written by the PM at runtime.
- **#33 cost flush race** — `hooks._extract_token_usage` now retries (3 attempts, 500ms each) when the transcript JSONL is empty at hook-fire time. Worktree-engineers now capture cost; the 4-runs-in-a-row correlation pattern is resolved.
- **#40 tokens column** — default `orchestra status` now shows `42k/8k cache=180k` instead of API-list-price `$X.XX`. `--cost-mode dollars` opt-in for the legacy display. Subscription-correctness fix.
- **#41 full-suite flakiness** — `release_worker_resources` and friends tolerate missing `resource_locks` table; PM follow-up also patched a residual WAL-race in `state.connect` (busy_timeout PRAGMA must precede journal_mode=WAL). 5/5 pytest runs at 296 tests now stable.

**Deferred to v2.4:** #43 (PM shortcut behavior — design discussion), #44 (mission YAML DAG — bigger architectural).

**Bonus:** preexisting `poll.py:105` mypy error vanished (#40's poll.py rewrite incidentally fixed it). Strict mypy on `state.py`/`tmux.py` and loose mypy on `orchestra/` both fully clean.

**Tests:** 296 (up from 264 at v2.3 DX-trio close).

## v2.3 — model fix + DX trio (2026-05-21)

**Critical fix:** `orchestra spawn` was sending `/<model>` to the worker pane to switch model — but Claude Code's slash command is actually `/model <name>`. Every orchestra run since v0 silently ran every worker on Opus 1M, regardless of `--model sonnet` / `--model haiku`. Fix in `orchestra/spawn.py:266` (closes #38). User-spotted from `.orchestra/hook-debug.log` model-field inspection.

**DX trio** (closes #34, #35, #36):

- **`orchestra new-role <name> [--engineer|--reviewer|--runner]`** — scaffolds `.orchestra/roles/<name>.md` with a sensible permissions skeleton for the chosen archetype. Saves the ~20-line enumeration most users would write by hand for a runner-style role.
- **Mission `pre-run.sh` hook** — `orchestra run` checks `<project>/.orchestra/pre-run.sh` and executes it before spawning the PM. Exit non-zero aborts the run. Use case: `adb connect <ip>` to pre-warm a flaky network adb session, avoiding a "no device attached" escalation in the first 30 seconds of the PM's mega-turn.
- **`--exclusive-resource <name>` lock** — for missions that touch shared hardware (one Android device, one printer, one db cluster). New `resource_locks` table in state.db, `acquire_resource` / `release_resource` / `release_worker_resources` helpers. Spawn blocks until the named lock is available; auto-released on `worker_done`, `status=error`, or explicit `orchestra release-resource <name>`. Works on both `orchestra spawn --exclusive-resource X` and the `resource` key in spawn-batch JSONL.

**Filed for v2.3 finish-up:**
- #40 — cost column shows API-list-price dollars; subscription users want tokens. Switch default to tokens, `--cost-mode dollars` flag.
- #41 — full-suite test flakiness post-resource-locks (1 random sqlite OperationalError per run, all tests pass in isolation).
- #32, #33 — earlier v2.3 issues (mission-lint false-positive, engineer-cost-zero), not implemented in this batch.

**Tests:** 264 (up from 246 at v2.2 close, modulo the flakiness in #41).

**Proven via dogfood:** the v2.3 batch itself was orchestrated. All 3 engineers actually ran on **Sonnet 4.6** (confirmed via pane status bars after the #38 fix landed earlier in the same session) — the first orchestra run where the cost-tier separation was real, not just claimed.

## v2.2 — DX quick wins (2026-05-20)

Three small DX improvements identified by observing eight orchestrated runs (v1.2 dogfood → ws-scrcpy phase 1):

- **`orchestra merge` reaps by default** (closes #28). Today the PM has to remember `orchestra reap` after every merge; 4 of 8 runs the PM forgot. Now reap-on-success is automatic; `--keep` preserves legacy behavior for inspection. `--batch` reaps each entry on its own success; on conflict in position N, reaps the successful 0…N-1 and leaves N intact.
- **`orchestra status` shows live cost** (closes #29). New `orchestra/cost.py` (per-million-token rates + family regex, lifted from the e2e watchdog so there's one source of truth) and a `$X.XX` column in both `orchestra status` and `orchestra poll`'s snapshot. Surfaces "this run will cost $20" early.
- **`orchestra mission lint <mission.md>`** (closes #30). New static pre-flight check: parses any inline JSONL `spawn-batch` blocks and verifies brief paths exist, role names resolve (override → bundled fallback), worktree names are unique, and the mission body has an `## ACCEPTANCE` / `## VERIFIER` section. Warnings for missing `## TEAM` heading and missing `worker_done` mention. `examples/kanban/mission.md` passes clean.

**Tests:** 246 (up from 204 at v2.1 close). No new runtime deps.

**Backward compatibility:** `orchestra merge` is now reap-by-default — scripts that called `orchestra reap <id>` separately keep working (idempotent if the worktree is already gone); use `--keep` if you need the old behavior. New `cost` column is human-output only; nothing parses these by column index in the codebase.

**Proven via dogfood:** the v2.2 batch itself was orchestrated. All 3 engineers ran in parallel (`spawn-batch`), the hook daemon handled the event stream, the PM merged them via the v2.1 `merge --batch`. Independent run verifier was added post-PM (the `status`-output cost wiring) — the engineer wired cost into `poll.py::render_snapshot` (PM-facing) but missed the human-facing `cli.py::status` path; a small follow-up commit fixed it. Filed as a learning: "engineer reads issue text → engineer interprets one of two surfaces → other surface gets missed" — future missions should call out BOTH surfaces explicitly.

## v2.1 — agent-communication optimizations (2026-05-20)

**Three independent primitives** that cut PM serial-coordination time on multi-engineer runs without changing any semantics. All stdlib-only.

- **`orchestra spawn-batch <spec.jsonl>`** (closes #24). Reads a JSONL of worker specs and dispatches them through `spawn.spawn_worker` in a `ThreadPoolExecutor`. Each worker gets its own short-lived sqlite3 connection (safe because the v1.2 #6 refactor stopped pinning conns across blocking waits). PM template advises using it for any wave of ≥2 engineers.
- **Hook daemon over Unix domain socket** (closes #25). New `orchestra/hookd.py` listens on `<project>/.orchestra/hook.sock`; new `orchestra/_hook_client.py` is the thin (~5-10ms) client. `orchestra/hooks.py` now tries the daemon first and falls back to the v2.0 in-process path on any failure, preserving full backward compatibility. Lazy-spawned on first hook event under `fcntl.flock` so concurrent clients don't race. Idle-shutdown after 5 min (configurable via `ORCHESTRA_HOOKD_IDLE_S`). `orchestra worker shutdown-hookd` for explicit teardown. `ORCHESTRA_FORCE_HOOK_FALLBACK=1` disables the daemon path (used by the existing test suite via an autouse conftest fixture).
- **`orchestra merge --batch <id1> <id2> ...`** (closes #26). Iterates merges sequentially in-process, records events as today, aborts on first conflict, prints per-merge JSON status to stdout. The PM saves one Claude-API turn per intermediate merge. Single-arg form unchanged.

**Proof:** v2.0 kanban e2e re-run on the v2.1 branch. Observed:
- All 572 hook events handled via the daemon path (0 fallback). Per-event latency 65ms → ~8ms.
- backend, web, cli engineers all reached `spawn_start` at the same wall-clock second — parallel spawn confirmed via `orchestra spawn-batch`.
- PM used `orchestra merge --batch` for its three engineer-branch merges.
- App was built end-to-end; the PM's internal verifier produced `OK` before signaling done.

**Known limitation observed during the regression run:** the e2e script's final-acceptance check (a second invocation of `examples/kanban/verifier.sh` after the PM exits) is fragile when the PM cleans up its dev server before signaling done — the script's verifier finds no backend to hit. The PM's *internal* verifier (run with the backend live) is the actual proof; the script-level second pass is redundant safety-net that occasionally trips on LLM variance in PM cleanup behavior. Not a v2.1 bug — flagged for a v2.2 follow-up to either drop the redundant check or have the script start its own backend.

**Backward compatibility:** v2.0 missions using single-`orchestra spawn` and single-`orchestra merge` work unchanged. All new CLI forms are additive. Hook daemon's fallback path IS the v2.0 hooks.dispatch — no v2.0 test changes; the conftest autouse fixture forces the fallback path for the suite so it stays deterministic and offline.

**Test count:** 204 (up from 179 at v2.0 close).

**Out of scope (deferred):** cross-project daemon, networked transport, PM crash resume, real parallel git merge.

## v2.0 — generic role framework (2026-05-18)

**Three new framework primitives** that make orchestra capable of multi-role
multi-stack projects without encoding flow-specific logic.

- **User-defined roles via filesystem.** Role templates moved from Python
  functions to `.md` files. Project override at
  `<project>/.orchestra/roles/<name>.md`, bundled built-ins at
  `orchestra/roles/<name>.md`. `--role` accepts any name; missing files
  surface as `role_load_failed`.
- **Per-role tool permissions.** Each role file may carry YAML front matter
  declaring `permissions.allow` / `permissions.deny`. Orchestra merges the
  block into the worker's `.claude/settings.local.json` before opening the
  tmux pane.
- **Read-only workers via composition.** A reviewer is just a role with
  restrictive permissions, spawned without `--worktree`. No new flag.

**Proof:** `examples/kanban/` plus `scripts/e2e-build-kanban.sh` exercise
the framework end-to-end on an architect + backend + web + CLI + reviewer
project. First run on 2026-05-18 completed in ~7 min wall-clock, $under-budget
cost: architect committed `docs/api.yaml`, three engineers built `backend/app.py`
(FastAPI), `web/index.html`+`app.js`, and `cli/kanban_cli.py` in parallel, the
reviewer (read-only role, no worktree) approved with `permissions.deny` enforced
against Write/Edit/rm/git push at the Claude Code layer, and the verifier
(`bash examples/kanban/verifier.sh`) exited 0 with `OK` after 6 acceptance
checks (health → boards → cards → patch → web HTML → CLI list).

**Backward compatibility:** v1.x missions using `--role pm` and
`--role engineer` work unchanged — the bundled `pm.md` and `engineer.md`
reproduce the v1.x templates byte-for-byte.

**Out of scope (deferred to v2.1+):** recursive PMs, first-class
worker DAG / `--blocked-by`, PM crash resume, cost-budget kill,
cross-worktree inspector mode.

## v1.3 (in progress)

- Fix: `orchestra spawn` no longer waits for the first `turn_complete`
  (Stop) event as a proof-of-life signal. `session_ready` from
  `SessionStart` is the sole proof-of-life. Issue #18.

## v1.0 — 2026-05-18

**The v1 thesis is validated.** A PM agent (Opus) and two engineer agents (Sonnet) autonomously built a working FastAPI URL-shortener web app — backend, frontend, tests — without any human-in-the-loop coordination. The PM mediated the API contract between the engineers, merged their work, ran the verifier, and reported done. Total wall-clock: ~8 minutes.

### What shipped

- **Hook-based state detection.** Replaces v0 screen-scraping. The six Claude Code hooks (`SessionStart`, `Stop`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `Notification`) map to typed event rows in `state.db` via `orchestra worker hook <event>`. Settings are deep-merged into `.claude/settings.local.json` at `orchestra init` time, and **also into each git worktree's `.claude/`** (worktrees have a `.git` file, not a `.git/` dir, so Claude Code's project-root detection stops at the worktree boundary).
- **Multi-worker spawn with worktrees.** `orchestra spawn ID MODEL --role pm|engineer --brief PATH --worktree NAME` — engineers get their own git worktree on branch `orch/<id>`, PM works in the main checkout. Engineers commit to their own branch; only the PM merges into main.
- **PM coordination surface.** Five new commands: `orchestra send <id> "<msg>"`, `orchestra answer <esc_id> "<answer>"`, `orchestra poll [--timeout 30] [--caller pm]`, `orchestra merge <id>`, `orchestra reap <id>`. `poll` returns a bounded state snapshot — per-engineer status, new-event count since last poll, pending escalations, last status message — so the PM's context stays compact across long mega-turns.
- **Single mega-turn PM execution.** The PM stays in one Claude response for the whole orchestration, chaining `poll → act → poll → ...` until the verifier passes. `Stop`-as-heartbeat doesn't work in Claude Code (fires on response end, not per tool call), so single-turn is the natural model.
- **Three watchdogs** in the e2e driver (`scripts/e2e-build-urlshortener.sh`): wall-clock (90 min ceiling), activity (10 min no events = abort), and a cost watchdog (placeholder — see Known Issues).
- **Role-aware prompts** (`orchestra/role_prompts.py`) — PM template enforces mega-turn discipline + phase-status writes + `/compact` advisory; Engineer template enforces worktree-only writes + escalate-on-uncertainty.
- **`orchestra worker done --summary "..."`** — cooperative termination signal; sets `workers.status='done'` + records a `worker_done` event. The e2e watchdog detects PM `status=done` and exits 0.
- **Soft-recoverable spawn timeouts.** A timed-out `_wait_idle_via_event` now records `spawn_stale_idle` + sets `status=stale_spawn` (instead of `error`) and continues the spawn flow, so a worker whose hooks are slow can still recover and complete.

### Test coverage

106 tests passing across 9 test files. The full suite excludes `tests/test_web.py` due to a pre-existing v0 `sse_starlette` import — see Known Issues.

### Acceptance contract

`./scripts/e2e-build-urlshortener.sh` against an authenticated `claude` CLI exits 0 with `[e2e] PASS`. The script wipes `/tmp/orch-urlshortener`, kills any leftover tmux session, runs `orchestra init` + `orchestra spawn pm opus --role pm --brief ...`, and waits for the watchdog to confirm PM-done + verifier-pass.

### Known issues / v1.2 follow-ups

1. **Token tracking shows $0.00.** Claude Code's Stop hook payload doesn't carry token usage — it carries a `transcript_path` to a JSONL file where the per-message usage lives. `orchestra/hooks._extract_token_usage` needs to read the transcript file to extract real tokens. Cost watchdog is currently non-functional; sized by wall-clock instead.
2. **Frontend `status=working` after `worker_done`.** Edge case: an engineer calls `orchestra worker done` (sets `status=done`) but the SessionStart-flips-status path or a later event resets it. Cosmetic — `worker_done` event is still recorded.
3. **`tests/test_web.py` can't be collected.** `orchestra/web.py` imports `sse_starlette` which is missing from `pyproject.toml`. Pre-existing v0 packaging bug, unrelated to v1.
4. **No PM crash recovery.** v1 accepts the loss; the e2e activity watchdog catches stuck-PM cases. v2 work if data demands it.
5. **No reviewer role.** The PM's verifier output is the final word. v2 work.

### Design docs

- Spec: `docs/superpowers/specs/2026-05-17-claude-orchestra-v1-design.md`
- Plan: `docs/superpowers/plans/2026-05-17-claude-orchestra-v1.md`
- Mission used by the e2e: `examples/urlshortener-mission.md`
- Verifier: `examples/urlshortener-verifier.sh`

---

## v0.1 — 2026-05-16

Initial scaffolding: SQLite state, tmux driver primitives, single-worker spawn, FastAPI dashboard, worker-cooperative CLI (`orchestra worker status`, `orchestra worker escalate`). See `docs/superpowers/specs/2026-05-16-claude-orchestra-design.md`.
