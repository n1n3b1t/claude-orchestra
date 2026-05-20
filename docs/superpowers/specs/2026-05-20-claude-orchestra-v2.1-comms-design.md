# claude-orchestra v2.1 — agent-communication optimizations

**Status:** design, ready for plan
**Date:** 2026-05-20
**Predecessor:** [v2.0 design](2026-05-18-claude-orchestra-v2-design.md)

## Goal

Cut wall-clock and per-event overhead on multi-engineer runs by attacking three measured bottlenecks from the v2.0 kanban dogfood, without changing any user-visible semantics.

## Measured baseline (v2.0 kanban e2e, 9:05 wall-clock, 460 events)

| Bucket | Time | Why this PR cares |
|---|---|---|
| PM-only serial work (briefs, post-spawn, merges, wrap-up) | ~5:00 (55%) | Three sub-buckets we can shrink: sequential `orchestra spawn` waves, sequential `orchestra merge` between-Claude-thinks, hook subprocess CPU. |
| Engineer parallel work | ~3:30 (39%) | Not addressed here — that's actual building. |
| Hook subprocess overhead | ~28s (5% of total) | Spread across 6 workers; ~65 ms per event × 432 tool events. Not on critical path, but unsalvageable at higher event volumes (web dashboard, bigger projects). |

## What v2.1 adds — three communication primitives

### 1. `orchestra spawn-batch <spec.jsonl>` (closes #24)

One CLI invocation that spawns N workers concurrently via `ThreadPoolExecutor`. Each spawn is independent (its own short-lived sqlite3 connection, its own tmux window). PM template gains a one-line example so any wave of ≥2 engineers uses it.

**Expected win:** ~18s shaved on every 3-engineer wave (3 × 9s sequential → 1 × ~9s concurrent).

**Files:**
- `orchestra/spawn_batch.py` (new) — parse JSONL, dispatch `spawn.spawn_worker` in threads, collect results
- `orchestra/cli.py` — register the new command
- `orchestra/roles/pm.md` — one-line guidance
- Tests in `tests/test_spawn_batch.py`

### 2. Hook daemon — Unix domain socket (closes #25)

Replace the subprocess-per-hook model with a lazily-spawned per-project daemon listening on `<project>/.orchestra/hook.sock`. The hook command becomes a thin client; the daemon owns the SQLite write loop.

**Architecture (stdlib only):**

```
worker pane                                              daemon
─────────────                                           ──────────
Claude Code fires hook
  → spawns `orchestra worker hook EVENT`
       │
       │  fast path (~5-10 ms)
       ▼
  connect to <project>/.orchestra/hook.sock           dispatch _handle(event, payload, conn, wid)
       │  send JSON line                                 │  write to state.db (same as today)
       │  close                                          │
       │
       │  fallback (socket missing / connect fails)
       ▼
  lazy-spawn daemon (double-fork) + retry once
  if retry fails: do today's heavy in-process path
  (full imports + direct SQLite write)
```

**Daemon lifecycle (tmux-server style):**
- PID file at `<project>/.orchestra/hookd.pid`
- First hook event with no socket → client double-forks the daemon → retries connect
- Daemon listens via `asyncio.start_unix_server` (stdlib)
- Idle shutdown after 5 minutes of no events (configurable via env var)
- SIGTERM → graceful close + cleanup of PID + socket
- `orchestra worker shutdown-hookd` for explicit teardown (used by e2e scripts)

**Backward compatibility:** if the daemon is unreachable for any reason, the client falls back to the v2.0 codepath (full `_handle` in-process). Existing tests pass unmodified. v1.x/v2.0 setups without the daemon keep working.

**Expected win:**
- Per-hook: 65 ms → ~8 ms (8× reduction)
- Aggregate: ~28s → ~3s on a kanban-sized run
- Bigger win on tool-event-heavy projects (the original C option was to drop tool events entirely — we keep them instead, which preserves the future web-dashboard use case)

**Files:**
- `orchestra/hookd.py` (new) — daemon process
- `orchestra/_hook_client.py` (new) — thin client
- `orchestra/hooks.py` — fast-path through client, fallback unchanged
- `orchestra/cli.py` — `worker shutdown-hookd` cleanup command
- Tests in `tests/test_hookd.py` (round-trip + respawn + fallback)
- Tests in `tests/test_hooks.py` (existing tests still pass — the fallback path is what they exercise)

### 3. `orchestra merge --batch <id1> <id2> ...` (closes #26)

Single CLI invocation iterates merges sequentially in-process (same working tree, no parallel-git), reporting `(id, status, conflict_summary)` per merge. Aborts on first conflict. Saves the Claude-API round-trips the PM today eats between three back-to-back merges.

**Expected win:** ~20-40s on a 3-engineer wave (3 × ~10-15s of PM-thinking-between-merges → 1 × decision turn).

**Files:**
- `orchestra/cli.py` — extend the `merge` command with `--batch`
- `orchestra/roles/pm.md` — one-line guidance
- Tests in `tests/test_cli.py`

## Non-goals (deferred)

- Cross-project daemon (one global socket). Per-project keeps the existing isolation model.
- Networked transport (TCP, gRPC). UDS is sufficient for same-host orchestration.
- Message persistence beyond SQLite. The daemon is a write-through layer; SQLite remains source of truth.
- Replacing the polling loop in `orchestra poll` with SQLite triggers / inotify. Measurements showed the 0.5s polling interval contributes 0% of PM-blocking time — the PM is always Claude-bound.
- True parallel `git merge` (different working trees + tmp index). Not worth the complexity; the in-process batching captures the PM-time win.

## Backward compatibility

- v2.0 missions using single-`orchestra spawn` and single-`orchestra merge` work unchanged. The new flags are purely additive.
- Hook daemon's fallback path IS the v2.0 hooks.dispatch — no v2.0 test changes.
- Bundled `pm.md` template adds an advisory note about `spawn-batch` and `merge --batch`. PMs that don't use them still work; they just pay the v2.0 cost.

## Test strategy

Two layers:

1. **Unit tests** for each primitive in isolation (parser, threading correctness, daemon socket protocol, batched merge bookkeeping, fallback path).
2. **v2.0 kanban e2e as cross-regression** — re-run `scripts/e2e-build-kanban.sh` after all three primitives land. It MUST still pass; same five-role pipeline, just faster. We don't write a new e2e — we re-use the existing v2.0 one as the acceptance gate.

## Acceptance for v2.1

1. All v2.0 tests pass unchanged.
2. New unit tests cover each primitive's happy path, fallback path, and the two-failure modes (`spawn-batch` partial failure; `merge --batch` mid-batch conflict).
3. `./scripts/e2e-build-kanban.sh` exits 0 and produces `OK` from the verifier (cross-regression).
4. Measured improvement: re-running the kanban e2e shows wall-clock reduction of ≥ 30s vs the v2.0 baseline of 9:05. (We won't gate-fail on this if the measurement is noisy — but it's the expected outcome.)
5. CHANGELOG has a v2.1 section listing all three primitives, the measured baseline, and the expected wins.

## Phasing

Single PR — the three primitives are independent and small enough to bundle. Three commits (one per issue), bundled into one v2.1 PR + orchestrated run, matching the v1.2 / v1.3 / v2.0 pattern.

## Risks and open questions

1. **Hook daemon lazy-spawn race.** If two hooks fire simultaneously and both see no socket, both might try to spawn a daemon. Mitigation: file-lock on `<project>/.orchestra/hookd.lock` during the double-fork. The losing process retries connect, the winning daemon serves both.

2. **Daemon survives across orchestra runs.** If the daemon idle-shutdowns mid-quiet, fine. If it crashes, fine (fallback). If it lingers from a previous run with stale state — the SQLite path it writes to is encoded in env at connect time, so a stale daemon writes to its old DB. Mitigation: each connection sends the worker_id AND state_db path in the JSON line; daemon validates and rejects mismatched DB paths (relinking to the new DB if appropriate). Probably overkill for v2.1 — `orchestra init` can SIGTERM any stale `hookd.pid` first.

3. **Asyncio server startup latency.** Spinning up an asyncio loop adds maybe 50-100ms to the FIRST hook event (which lazy-spawns the daemon). All subsequent events are fast. Acceptable; the first event of a run is `spawn_start` which is already in a 9s spawn window.

4. **spawn-batch failure modes.** If one worker in the batch fails to spawn (tmux error, role load error), the others should still complete. Each future is independent; the CLI returns the aggregate but exit code reflects any failure.

5. **`merge --batch` conflict reporting.** A conflict in worker N aborts merges N+1…M. The CLI prints the conflict diff for N and a clear "skipped" message for N+1…M. PM can then merge them one-by-one in single-arg mode.
