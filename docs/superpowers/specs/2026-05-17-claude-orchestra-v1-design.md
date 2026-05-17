# claude-orchestra v1 — hook-based state detection

**Status:** draft
**Date:** 2026-05-17
**Supersedes parts of:** `2026-05-16-claude-orchestra-design.md` (the polling-based state detection layer)
**Headline change:** stop screen-scraping; subscribe to Claude Code's native hook events instead.

## Background

v0 detects worker state by polling `tmux capture-pane` output and regex-matching for prompt characters and spinner words. It works but is:

- **Probabilistic.** Idle is detected up to `BOOT_POLL_S` seconds after it actually happens.
- **Brittle.** Any change to Claude Code's UI (different spinner text, different prompt glyph, a new welcome screen) breaks `is_idle`. We already hit this with the trust-folder prompt.
- **Coupled to terminal rendering.** When claude refreshes a screen, our capture sees stale state for one tick.
- **Workers do double bookkeeping.** Workers are instructed to call `orchestra worker status --progress ... --turns N` periodically. If the model forgets, the dashboard goes stale.

Claude Code exposes a [hook system](https://docs.claude.com/en/docs/claude-code/hooks) that fires shell commands on real lifecycle events. Those events are the authoritative source of truth for "what is claude doing." We should use them.

## Goals

- Replace `_wait_idle`'s polling with subscription to a `SessionStart` hook event.
- Replace `_wait_first_status`'s polling with subscription to the first `Stop` hook event.
- Auto-record every turn via the `Stop` hook — workers no longer need to call `orchestra worker status` manually.
- Surface tool-call activity (`PreToolUse` / `PostToolUse`) to the dashboard so the user sees "currently running Bash..." in real time.
- Make state detection event-driven, not poll-driven. Latency goes from "up to N seconds" to "within tens of milliseconds."

## Non-goals for v1

- Multi-worker concurrency (separate v1.1 spec).
- Adaptive heartbeat (separate v1.1 spec).
- Rate-limit watchdog (v1.2).
- Worktree-per-worker (v1.2).

The point of v1 is the detection layer. Holding the rest of the roadmap stable lets us land hooks without churn elsewhere.

## Architecture changes

### 1. Per-worker `.claude/settings.local.json`

`orchestra spawn` writes (or merges into) `<project_root>/.claude/settings.local.json` with hooks that invoke a new subcommand:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{"type": "command", "command": "orchestra worker hook SessionStart"}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "orchestra worker hook Stop"}]
    }],
    "PreToolUse": [{
      "matcher": ".*",
      "hooks": [{"type": "command", "command": "orchestra worker hook PreToolUse"}]
    }],
    "PostToolUse": [{
      "matcher": ".*",
      "hooks": [{"type": "command", "command": "orchestra worker hook PostToolUse"}]
    }],
    "SessionEnd": [{
      "hooks": [{"type": "command", "command": "orchestra worker hook SessionEnd"}]
    }],
    "Notification": [{
      "hooks": [{"type": "command", "command": "orchestra worker hook Notification"}]
    }]
  }
}
```

If `settings.local.json` already exists, we deep-merge our `hooks` block into the existing object. We do NOT touch any other top-level key.

### 2. New CLI subcommand: `orchestra worker hook <event_name>`

Reads JSON from stdin (Claude Code's standard hook protocol), writes a row to `state.db`, and exits 0. The shape per event:

| Event | Recorded `kind` | Side effect on worker row |
|---|---|---|
| `SessionStart` | `session_ready` | `status = working` |
| `Stop` | `status` | `turns += 1`; payload stores any usage info |
| `PreToolUse` | `tool_started` | none (informational; payload has tool name + input summary) |
| `PostToolUse` | `tool_finished` | none |
| `SessionEnd` | `session_ended` | `status = done` if no error; preserve status if user already stopped |
| `Notification` | `notification` | none in v1 (future: surface to dashboard alerts) |

The subcommand requires `ORCHESTRA_WORKER_ID` and `ORCHESTRA_STATE_DB` env, same as `worker status`. Without them, exit 2 — same convention.

### 3. Simplified spawn flow

Old:
```
boot → poll is_idle → double-Enter → /<model> → paste prompt → poll for first status
```

New:
```
boot → wait for session_ready event (DB) → /<model> → paste prompt → wait for first Stop event (DB)
```

`_wait_idle` and `_wait_first_status` both become "block on a specific event kind appearing in state.db." Same polling primitive, but the condition is precise: an event of the named kind exists for this worker_id with `id > some_baseline`.

The trust-prompt special-case stays for now — it fires before `SessionStart` (claude hasn't even started its session yet), so hooks don't help us there. We keep the existing capture-pane based dismissal.

### 4. Worker prompt simplification

`orchestra/prompts.py`'s startup template currently tells the worker to call `orchestra worker status --progress "..." --turns N` periodically. In v1 we drop that directive — Stop hooks do it automatically. We keep:
- The escalation instruction (`orchestra worker escalate`) — still manual.
- The "commit yes, push no" and other coordination rules.

The `worker status` CLI stays available for callers who want to override `progress`-strings explicitly. The auto-recorded Stop event uses a default progress string (e.g. `"turn complete"`).

### 5. is_idle becomes a debug helper

After v1, no production code path calls `tmux.is_idle`. It stays in the module as a diagnostic (and the `orchestra tail` command could grow an `--idle` flag) but doesn't gate spawn or status. We can simplify or remove the spinner / prompt regexes if nothing else needs them.

### 6. Capture-pane stays

The dashboard's live pane peek (`GET /api/workers/{id}/pane`) still uses `tmux capture-pane`. Hooks tell us *what* claude is doing, but the rendered terminal view is still useful for humans, especially for debugging escalations.

## State schema additions

No schema migration. `events.kind` is already a free-form text column. New kinds in v1:
- `session_ready`
- `tool_started`, `tool_finished`
- `session_ended`
- `notification`
- existing `status` kind keeps its meaning, just gets auto-recorded on Stop

`workers.status` enum gets one new value:
- `done` — set by `SessionEnd` when no prior error / manual stop.

## Data flow walkthrough

**Spawn timeline (v1):**

```
0.0s   orchestra spawn w1 sonnet "task"
0.0s   state.create_worker(status=spawning)
0.0s   merge .claude/settings.local.json
0.0s   tmux new-window
0.1s   send "claude --dangerously-skip-permissions" + Enter
3-8s   claude starts, prints trust prompt (if first run in project)
4-9s   our trust-prompt regex catches it, sends Enter
6-12s  claude finishes startup, SessionStart hook fires
6-12s  hook runs `orchestra worker hook SessionStart`, writes session_ready event
6-12s  spawn observes session_ready in DB → done waiting; status = working
6-12s  send /<model>; sleep brief; paste-buffer task prompt
15-30s claude does the task; Stop hook fires when first response completes
15-30s hook writes status event, turns = 1
15-30s spawn observes first Stop event → spawn_ok; returns
```

Latency from "claude is actually idle" to "spawn knows it" drops from 0-3s (poll interval) to <100ms (subprocess overhead of the hook).

**Per-turn timeline:**

```
worker thinking ...
worker writes a file via Edit tool
  PreToolUse hook → tool_started event in DB
  PostToolUse hook → tool_finished event in DB
worker emits response
  Stop hook → status event in DB; turns counter bumped
```

Dashboard sees all of this within ~100ms of each event via SSE.

## Risks

- **Hook config format evolution.** Claude Code's hook schema is documented but young. Bumps to claude CLI may rename or reshape events. Mitigation: pin tested version range in `pyproject.toml` (already at `claude` system dep); add a one-line check in `orchestra spawn` that verifies `claude --version` is in the supported range, emits a warning otherwise.
- **Hook failures.** If `orchestra worker hook` crashes, claude shows a banner and the worker turn may stall. `orchestra worker hook` must be bulletproof: wrap the whole subcommand in a try/except, exit 0 even on internal errors (log to stderr — claude shows it but doesn't block). Better an unrecorded event than a broken hook.
- **Settings merge collisions.** If the user has existing hooks (e.g. their own Stop hook for desktop notifications), we must not clobber. Deep-merge: append our `{"type": "command", "command": "orchestra worker hook ..."}` to the existing hooks array rather than replacing it.
- **Hook ordering.** When several hooks fire on the same event, Claude Code runs them in order. Our hook should not assume it's first or last.
- **DB contention.** Stop hooks fire often. SQLite WAL + busy_timeout=5000 handle low contention well, but if dashboard polls are also writing, we should make sure all hook writes are short transactions.

## Migration path from v0

v0 and v1 can co-exist. Sequence:

1. **v0.x maintenance release:** ship `orchestra worker hook` CLI and settings.local.json merge logic alongside the existing polling code. No behavior change yet.
2. **v1.0 release:** spawn switches from polling to hook-event subscription. Worker prompt template drops the manual-status directive. `is_idle` callers go away.
3. **v1.0.x:** clean up dead code in `spawn.py`. Mark `worker status` CLI as "still supported, but not required for status tracking."

Backwards compat: existing `worker status` and `worker escalate` keep working. Old workers that don't have hooks installed will still function — they just won't auto-update turns; the dashboard will show whatever they last wrote manually.

## Out of scope (still v2 territory)

- Multiple concurrent workers + adaptive heartbeat (own spec).
- Rate-limit watchdog (own spec).
- Worktree-per-worker isolation (own spec).
- Per-tool permission allowlists via hook gating (`PreToolUse` could enforce, but that's a v2 design decision).

## Testing

**Unit:**
- `orchestra worker hook <event>` for each event kind: feed canned JSON to stdin, assert the right row appears in a tmpdir state.db.
- Settings merge logic: empty file, existing settings with no hooks, existing settings with overlapping hooks. Each case verifies our hook is appended without dropping user content.

**Integration:**
- `spawn_worker` with `tmux` mocked, simulate a hook firing by inserting a `session_ready` event into the DB while spawn is mid-wait. Assert spawn proceeds.

**End-to-end (manual):**
- Update `scripts/e2e-spawn.sh` to assert that `session_ready` and at least one `Stop` event appear in state.db within 60s.
- Verify settings.local.json is created in the spawn dir and contains the expected hooks.

## Open questions

- Should `orchestra init` install hooks once per project (in `.claude/settings.local.json`) or should `orchestra spawn` do it per spawn? Init-time is cleaner; spawn-time guarantees it's always present. Decide during implementation.
- What does the hook's stdin JSON actually look like for each event kind? Need to verify against Claude Code's current hook docs and the running version before locking the schema. First implementation task should be a small script that just logs raw hook stdin to a file, run against a real worker, and adjust the parser to match.
- How do we cleanly remove hooks when a worker is stopped? Maybe `orchestra stop` shouldn't touch settings (other workers may still need it); the hooks are harmless for non-orchestra workflows since `ORCHESTRA_WORKER_ID` won't be set and the hook subcommand will exit 2 silently.
