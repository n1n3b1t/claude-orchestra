# Changelog

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
