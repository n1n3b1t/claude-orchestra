# claude-orchestra v1 — multi-agent orchestration with hook-based detection

**Status:** draft
**Date:** 2026-05-17
**Supersedes parts of:** `2026-05-16-claude-orchestra-design.md` (v0 detection + spawn layers)
**Headline goal:** stand up a PM + two specialised engineers, have them collaboratively build a small web app end-to-end, with no human intervention beyond kicking it off.

## North star — the v1 acceptance test

`scripts/e2e-build-urlshortener.sh`:

1. Initialises a fresh project directory.
2. Runs `orchestra spawn pm opus "..."` with a PM brief that names two engineers.
3. The PM spawns `backend` (Sonnet) and `frontend` (Sonnet) into their own worktrees.
4. Engineers build their parts. PM polls, mediates the API contract, answers escalations.
5. PM merges both worktrees into `main`.
6. PM runs the verifier (see Verifier section below — uses port 8765 to avoid clashing with common dev defaults): `pytest`, then `uvicorn app:app --port 8765 &`, then `curl -X POST localhost:8765/shorten -d '{"url":"https://example.com"}'` (expects 200 + a code), then `curl -I localhost:8765/<code>` (expects 302).
7. PM marks itself `done` once the verifier passes.

The shell script exits 0 if the PM reaches `done` within a wall-clock budget (target: 45 min, ceiling: 90 min).

If we can make this work reliably, v1 is shipped.

## Why this is the goal

v0 proved that the orchestration substrate (tmux + SQLite + dashboard) works. It proved nothing about *orchestrating useful work*. The hook system, multi-worker spawn, worktrees, and role-based prompts are interesting but only justify their complexity if they cooperate to actually build software. The acceptance test is the contract.

## Scope

In:

- Hook-based state detection (Claude Code hook events into `state.db`).
- Multi-worker spawn (PM spawns engineers; engineers don't spawn each other).
- Worktree-per-worker git isolation.
- Role-based startup prompts (PM, Engineer).
- Inter-worker coordination via the existing escalation channel + a new "PM brief" message channel + a new "send" channel for PM-to-engineer nudges.
- PM single-mega-turn execution: PM stays in one Claude response, calls `orchestra poll` to wait for engineer events, drives the whole run to completion or give-up.
- E2e script with wall-clock, activity, and cost watchdogs.
- URL shortener as the canonical e2e target.

Out (still v2+):

- Rate-limit watchdog. Single workers are unlikely to hit limits in the e2e window; if they do, we surface and stop. The watchdog is a v2 hardening pass.
- Per-tool permission allowlists. v1 keeps `--dangerously-skip-permissions`.
- More than 3 workers concurrently. The e2e is the budget ceiling, not a generic N-worker design.
- Multi-host or multi-user.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Project dir (the URL shortener repo we're building)                    │
│  /tmp/orch-urlshortener/                                                │
│  ├── .git/    main branch (final merged result)                         │
│  ├── .claude/settings.local.json   (hooks pointing at `orchestra worker │
│  │                                  hook ...` — same for every worker)  │
│  ├── .orchestra/                                                        │
│  │   ├── state.db                                                       │
│  │   ├── config.toml                                                    │
│  │   └── briefs/                ← PM-authored task briefs               │
│  │       ├── backend.md                                                 │
│  │       └── frontend.md                                                │
│  └── worktrees/                                                         │
│      ├── backend/   (git worktree, branch orch/backend)                 │
│      └── frontend/  (git worktree, branch orch/frontend)                │
│                                                                         │
│  tmux session 'orch-orch-urlshortener':                                 │
│    window 0  shell (the human watches here)                             │
│    window 1  pm        (Claude Code, Opus)                              │
│    window 2  backend   (Claude Code, Sonnet, cwd=worktrees/backend)     │
│    window 3  frontend  (Claude Code, Sonnet, cwd=worktrees/frontend)    │
│                                                                         │
│  Hooks (claude → orchestra):                                            │
│    SessionStart, Stop, Pre/PostToolUse, SessionEnd, Notification        │
│    → `orchestra worker hook <event>` → state.db                         │
│                                                                         │
│  Coordination (PM → engineers, engineers → PM):                         │
│    PM writes briefs/<engineer>.md and runs                              │
│       `orchestra send <engineer> "go read your brief"`                  │
│    Engineers escalate via                                               │
│       `orchestra worker escalate --question ...` (writes to state.db,   │
│       PM sees it on next poll and answers via                           │
│       `orchestra answer <esc-id> "..."`)                                │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why a star with PM at the center

Three reasons:

1. **Bounded API surface.** Engineers need exactly one peer — the PM. They don't have to know about each other's existence, lifecycle, or branch names.
2. **Single coordinator for the API contract.** The whole reason this isn't a single-engineer task is to test that the PM can mediate between two engineers' assumptions ("backend returns `{code}`, frontend posts `{url}`"). A mesh would let the engineers negotiate directly, which is interesting but a different design.
3. **PM owns merge.** Engineers commit to their own branch. Only the PM checks out main and merges. Fewer concurrent writers to main = fewer race possibilities.

## Hook-based state detection

### settings.local.json injection

`orchestra init` writes (or deep-merges into) `<project_root>/.claude/settings.local.json`:

```json
{
  "hooks": {
    "SessionStart":  [{"hooks": [{"type": "command", "command": "orchestra worker hook SessionStart"}]}],
    "Stop":          [{"hooks": [{"type": "command", "command": "orchestra worker hook Stop"}]}],
    "PreToolUse":    [{"matcher": ".*", "hooks": [{"type": "command", "command": "orchestra worker hook PreToolUse"}]}],
    "PostToolUse":   [{"matcher": ".*", "hooks": [{"type": "command", "command": "orchestra worker hook PostToolUse"}]}],
    "SessionEnd":    [{"hooks": [{"type": "command", "command": "orchestra worker hook SessionEnd"}]}],
    "Notification":  [{"hooks": [{"type": "command", "command": "orchestra worker hook Notification"}]}]
  }
}
```

If the file already exists, we deep-merge our hooks into the existing array per event (don't replace user hooks).

### `orchestra worker hook <event>`

Reads JSON from stdin (Claude Code's hook protocol), writes to state.db, exits 0 even on internal errors (a failing hook breaks the worker turn). Mapping:

| Event | DB event kind | Worker row mutation | Payload notes |
|---|---|---|---|
| SessionStart | `session_ready` | `status=working` | session id |
| Stop | `turn_complete` | `turns += 1` | input/output token counts + cache hits (e2e cost watchdog reads these) |
| PreToolUse | `tool_started` | none | tool name + input summary |
| PostToolUse | `tool_finished` | none | tool name + output summary |
| SessionEnd | `session_ended` | `status=done` if no prior error | reason |
| Notification | `notification` | none | message text |

Naming note: the Stop hook's DB kind is `turn_complete` (not `status`) to avoid collision with the v0 `status` event kind already emitted by `orchestra worker status --progress …` cooperative writes. Both kinds coexist; they mean different things (one is hook-driven turn-end, the other is worker-cooperative phase declaration).

### Spawn loses two polling loops

- `_wait_idle` becomes "block until a `session_ready` event for this worker_id appears in state.db, max 60s".
- `_wait_first_status` becomes "block until first `turn_complete` event for this worker_id, max 90s".
- The capture-pane trust-prompt dismissal stays (fires before SessionStart).

Detection latency drops from "up to N seconds (poll interval)" to "within hook subprocess RTT" (~100ms).

## Multi-worker support

### Workers run concurrently; PM is the lifecycle owner

Engineers never spawn other engineers. Only the PM can call `orchestra spawn`. Within the PM's tmux pane, the PM runs:

```
orchestra spawn backend  sonnet  --brief .orchestra/briefs/backend.md  --worktree backend
orchestra spawn frontend sonnet  --brief .orchestra/briefs/frontend.md --worktree frontend
```

New spawn options for v1:

- `--brief PATH` — path (relative to project root) of a markdown brief. The startup prompt for the engineer includes "your brief is at `<path>`; read it before doing anything." This decouples the brief content from the CLI arg string.
- `--worktree NAME` — create or reuse a git worktree at `<project>/worktrees/<name>` on branch `orch/<worker_id>`, and pass it as `--cwd` to the spawned `claude` process. Without this flag, spawn behaves as v0 did (shared cwd, no worktree).

### Worktree management

`orchestra spawn ... --worktree backend` does:

1. If `worktrees/backend/` doesn't exist:
   - `git worktree add -b orch/backend worktrees/backend HEAD`
2. Otherwise reuse it.
3. Spawn claude with `cwd=worktrees/backend`.

`orchestra reap <worker_id>` (new): removes the worktree + deletes the branch. PM calls this after merging.

The PM has a corresponding merge helper:

```
orchestra merge <worker_id>     # PM runs from project root (main checkout)
```

Internally: `git fetch worktrees/<worker_id>` is unnecessary (same repo); the PM does `git merge orch/<worker_id>` and reports conflicts back to the engineer via the escalation channel if it fails.

### PM execution model: single mega-turn

The PM is one Claude Code session that orchestrates the whole run inside a single model response. Its startup prompt instructs it: "do not end your turn until the verifier passes or you give up." Within that one response, the PM chains tool calls:

1. `orchestra poll [--timeout 30]` — blocks up to 30s until a new event for any tracked worker arrives, or returns immediately if one has fired since the last poll. Returns a **state snapshot**: per-worker current status, count of new interesting events since the caller's last poll, pending escalations (with question text), last status message. Bounded size — does not grow with cumulative event count, so the PM's context stays manageable across many polls. Filters out `tool_started` / `tool_finished` noise.
2. PM decides what to do (answer an escalation, send a nudge, merge a completed branch, declare done).
3. PM calls poll again. Loop until the verifier passes.

Why mega-turn:

- The `Stop` hook fires when a Claude response *finishes*, not between tool calls. A blocking `orchestra poll` holds the same turn open. There is no "turn boundary" for the PM to use as a heartbeat — single mega-turn is the model Claude Code naturally supports.
- No external wake mechanism. Simpler architecture; one less moving part than a turn-per-cycle design.
- Engineer state (state.db + worktrees) is durable. Only the PM's reasoning lives in its context.

Trade-offs:

- Context grows monotonically across many polls. State snapshots keep growth shallow per call. If the PM nears its window, it can `/compact` itself.
- A PM crash mid-run loses its reasoning. See "PM recovery model" below.

### PM recovery model: accept loss + watchdog

v1 does not attempt to recover the PM's reasoning if it crashes. Two guards instead:

1. **Wall-clock budget.** The e2e script enforces 45 min target, 90 min ceiling. If the PM hasn't reached `done` by the ceiling, kill the session and exit non-zero.
2. **Activity watchdog.** The e2e script polls `state.db` for new events. If no event of any kind appears for 10 min, abort — something is wedged (stuck PM, hung engineer, lost hook).

The PM's startup prompt requires `orchestra worker status --progress "phase: <name>" --turns <n>` at known phases (briefs written, engineers spawned, contract decided, merges queued, verifier running). These status writes feed the watchdog so PM silence is detectable.

If we discover the mega-turn approach is too crash-prone in practice, the v2 path is a `pm-notes.md` + `orchestra resume <project>` protocol (deferred).

## Role-based prompts

`orchestra spawn` learns a `--role <pm|engineer>` flag (default: `engineer`, matches v0 behavior). `orchestra/prompts.py` gains role-specific templates.

### PM prompt skeleton

```
## ROLE: Project Manager
Project: {project_name}
Worker ID: {worker_id}

### MISSION
{mission}

### YOUR TEAM
You will spawn and coordinate these engineers:
{engineer_briefs}

### TOOLS YOU CAN USE
- orchestra spawn <id> <model> --brief <path> --worktree <name>
- orchestra send <worker_id> "<message>"
- orchestra poll                            # wait for engineer events
- orchestra answer <escalation_id> "<answer>"
- orchestra merge <worker_id>               # after engineer reports done
- orchestra reap <worker_id>                # cleanup
- All normal tools (Read, Write, Bash, Edit) for your own files

### RULES
- Write per-engineer briefs to .orchestra/briefs/<id>.md before spawning.
- Each engineer is responsible for their own worktree only. Don't touch their files.
- Mediate the API contract: when the engineers' assumptions diverge, decide
  and propagate the decision to both.
- Verify the final result with the verifier script before marking done.
- Stay in one turn. Keep calling tools (`orchestra poll`, `orchestra answer`,
  `orchestra send`, `orchestra merge`, etc.) until the verifier passes or you
  give up. Do NOT emit a final answer until you have succeeded or given up.
  Each `orchestra poll` may block up to 30s — that is normal.
- Emit `orchestra worker status --progress "phase: <name>" --turns <n>` at
  each major phase: briefs-written, engineers-spawned, contract-decided,
  merges-queued, verifier-running, done. This feeds the activity watchdog.
- If your context grows large, run `/compact` between phases.

### VERIFIER (you must pass this before marking yourself done)
{verifier_command_block}

### GO
Read the mission, plan the engineer split, write briefs, spawn engineers,
coordinate, merge, verify.
```

### Engineer prompt skeleton

```
## ROLE: Engineer
Worker ID: {worker_id}
Workspace: {cwd}  (your own git worktree on branch {branch})

### YOUR BRIEF
{brief_content_inlined_OR_path_to_read}

### COORDINATION
- Commit to {branch}. Don't push. Don't merge.
- The PM is at worker id 'pm'. To ask a question, use:
    orchestra worker escalate --question "..." --context "..."
- When you finish, leave a final status message:
    orchestra worker status --progress "DONE: <summary>" --turns <N>
  Then end your session (let Claude finish naturally — your SessionEnd
  hook will mark you done in the DB).

### RULES
- Stay in {cwd}. Do not touch files outside your worktree.
- Do not spawn workers.
- Tests live in your worktree. Run them before declaring DONE.
```

## Coordination protocol

### Channels

Three message channels, all backed by state.db / filesystem:

1. **Brief channel** (`.orchestra/briefs/<engineer>.md`) — PM writes once at spawn, engineer reads once at startup.
2. **Escalation channel** — engineer writes via `orchestra worker escalate`, row in `escalations` table, PM reads via `orchestra poll` and responds via `orchestra answer`.
3. **Send channel** (new) — `orchestra send <worker_id> "message"` types into the engineer's tmux pane via `send_multiline`. Used for nudges, clarifications, "merge conflict, please rebase X." Recorded as a `message_sent` event.

### Typical flow

```
PM:        write briefs, spawn engineers
PM:        orchestra poll  →  waits up to 30s for an event
backend:   (working) ... SessionStart ... Stop (turn 1) ...
PM:        sees turn 1 done. backend not yet escalated; keep polling.
PM:        orchestra poll
frontend:  Stop (turn 3); escalation: "what's the response shape of /shorten?"
PM:        sees escalation. Decides API contract. Answers:
              orchestra answer 1 "Response is {\"code\":\"abc123\"}; status 200."
              orchestra send backend "Frontend expects POST /shorten → {\"code\":\"...\"}"
PM:        orchestra poll
backend:   Stop (turn 5); escalation: "tests pass; DONE"
frontend:  Stop (turn 7); "DONE"
PM:        orchestra merge backend; orchestra merge frontend
PM:        runs verifier; exits 0
PM:        orchestra worker status --progress "DONE: verified" --turns N
            (SessionEnd hook fires → status=done)
```

### Failure modes

- Merge conflict → PM sends the engineer back with `orchestra send <id> "merge conflict in app.py lines X-Y; please rebase against main and update"`. Engineer rebases, commits, signals done again.
- Engineer stuck (long silence) → PM sends a poke via `orchestra send`. If still no movement after K minutes, PM escalates by stopping the worker and re-spawning with a clarified brief.
- Verifier fails → PM dispatches a fix back to whichever engineer's domain owns the failure.

## State schema additions

No migration — `events.kind` is free-form text. New kinds and statuses:

**Event kinds:**
- `session_ready` (SessionStart hook)
- `turn_complete` (Stop hook). Payload includes `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` per turn. The e2e cost watchdog sums these.
- `tool_started`, `tool_finished` (Pre/PostToolUse) — `orchestra poll` filters these out by default
- `session_ended` (SessionEnd)
- `notification` (Notification)
- `message_sent` (PM-to-engineer message via `orchestra send`)
- `worktree_created`, `worktree_reaped`
- `merge_attempted`, `merge_conflict`, `merge_ok`

**Worker statuses:** add `done`.

**Workers row:** add `role TEXT NOT NULL DEFAULT 'engineer'` and `worktree TEXT` (nullable; only set when spawn was called with `--worktree`).

## New CLI surface for v1

| Command | Purpose |
|---|---|
| `orchestra spawn ID MODEL [--role pm\|engineer] [--brief PATH] [--worktree NAME] [TASK]` | Spawn a worker. `--brief` reads the brief from a file; `--worktree` creates an isolated worktree. PM-only: `--role pm` and `--brief` for the PM itself; engineers can be spawned by either the user (manual mode) or the PM. |
| `orchestra send <worker_id> "<msg>"` | Send a message to a worker's pane via `send_multiline`. Records `message_sent` event. |
| `orchestra answer <escalation_id> "<answer>"` | Resolve an escalation (DB write) AND send the answer to the asking worker's pane. |
| `orchestra poll [--timeout 30]` | Block up to N seconds for any new interesting event in tracked workers, or return immediately if one has fired since the caller's last poll. Returns a **state snapshot** (markdown table): per-worker status, new-event count since last call, pending escalations with question text, last status message. Filters out `tool_started` / `tool_finished` noise. Tracked = all rows with `role='engineer'` in state.db. |
| `orchestra merge <worker_id>` | `git merge orch/<worker_id>` from main. Records merge_attempted/ok/conflict event. |
| `orchestra reap <worker_id>` | Remove the worktree and delete the branch. |
| `orchestra worker hook <event>` | Hook entry point (called by claude). |

Existing v0 commands stay: `init`, `status`, `stop`, `tail`, `dash`, `worker status`, `worker escalate`.

## Spawn flow changes

```
orchestra spawn pm opus --role pm --brief mission.md
  ↓
state.create_worker(role=pm, worktree=NULL)
merge .claude/settings.local.json (if not already present)
ensure_session, new_window
boot: ORCHESTRA_WORKER_ID=pm ORCHESTRA_STATE_DB=... claude --dangerously-skip-permissions
trust-prompt dismiss (unchanged)
wait for session_ready event (NEW — was _wait_idle polling)
send /opus
paste-buffer the PM startup prompt (rendered from prompts.render_pm_prompt(...))
wait for first Stop event (NEW — was _wait_first_status polling)
return; PM is now alive and reading its brief
```

Engineer spawn is similar but with `--role engineer --worktree backend` adding a `git worktree add` step before window creation, and changing `cwd` to the worktree path.

## URL shortener spec (the e2e target)

This is the actual project the agents build. Lives in the e2e test as a fixture / mission brief.

### Mission (passed to the PM)

```markdown
# Mission: URL shortener web app

Build a small FastAPI web app that shortens URLs.

## Acceptance
- `pytest` passes from the project root.
- `uvicorn app:app --port 8765` starts the server.
- `curl -X POST localhost:8765/shorten -H 'content-type: application/json' -d '{"url":"https://example.com"}'`
  returns HTTP 200 with a JSON body `{"code":"<short>"}`.
- `curl -I localhost:8765/<short>` returns HTTP 302 with `Location: https://example.com`.
- `curl localhost:8765/` returns an HTML page with a form posting to `/shorten`.

## Team
Spawn two engineers in their own worktrees:
- `backend` (sonnet) — implements the FastAPI app, SQLite storage, and tests.
- `frontend` (sonnet) — implements the HTML form page and any static assets.

You mediate the API contract. You merge their work into main. You run the
acceptance checks. You only mark yourself done when all four acceptance
checks pass.
```

### Engineer briefs (PM-authored at runtime)

Approximate content; PM writes the real text.

**backend brief:**
```markdown
You are building the backend for a URL shortener.

Tech: Python 3.10+, FastAPI, SQLite (stdlib sqlite3), pytest.

Endpoints:
- POST /shorten — body {"url": "..."} → 200 {"code": "..."}
- GET /{code} — 302 redirect to the stored URL; 404 if unknown

Files you own:
- app.py
- db.py (table: links(code TEXT PRIMARY KEY, url TEXT NOT NULL, created_at TEXT))
- tests/test_app.py (use FastAPI TestClient)

Do not touch templates/ or static/ — frontend owns those.

When you finish, run `pytest -v` and confirm green before signaling DONE.
```

**frontend brief:**
```markdown
You are building the frontend for a URL shortener.

You own templates/index.html and static/style.css.

The page must:
- Render a form with a URL input + submit button.
- On submit, POST to /shorten and display the returned short code as a clickable link.
- Render with reasonable styling (no framework; ~50 lines of vanilla CSS).

Backend contract (confirmed by PM):
- POST /shorten body {"url":"..."} → 200 {"code":"..."}

You do not write tests for the HTML — manual verification by PM only.
```

### Verifier

A small bash snippet the PM runs at the end:

```bash
set -e
( cd "$PROJECT_ROOT" && pytest -q ) || exit 1
( cd "$PROJECT_ROOT" && uvicorn app:app --port 8765 ) &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null" EXIT
sleep 2
CODE=$(curl -s -X POST localhost:8765/shorten -H 'content-type: application/json' -d '{"url":"https://example.com"}' | jq -r .code)
test -n "$CODE" || exit 2
curl -sI "localhost:8765/$CODE" | grep -q '302' || exit 3
curl -s localhost:8765/ | grep -q '<form' || exit 4
echo "VERIFIER OK code=$CODE"
```

## Risks

- **Claude API rate limits.** Three concurrent workers chewing through tokens can hit limits. v1 does NOT include a rate-limit watchdog. Mitigation: if it bites, surface the error and stop; document it as a known v1 limitation. The e2e cost ceiling (below) provides a cruder backstop.
- **Single mega-turn context growth.** The PM's one response can run for an hour-plus. Per-call output (`orchestra poll`) is bounded by the state-snapshot format, but cumulative tool-call history grows linearly with activity. Mitigation: state snapshots stay compact; the PM prompt instructs it to `/compact` between phases if needed. If 90-min runs reliably exceed the PM's context in practice, the v2 path is turn-per-cycle with a wake mechanism.
- **No PM crash recovery.** v1 accepts that a PM crash kills the run. The activity watchdog catches "PM stuck silently"; nothing catches "PM crashed and lost its decisions" — the e2e simply exits non-zero. v2 work if data demands it.
- **Worktree state across runs.** Worktrees persist between runs unless `reap`ed. The e2e script `git worktree remove --force`s everything in `worktrees/` at the start of each run.
- **Hook stdin format drift.** Claude Code's hook JSON schema may change. **Task 0 of implementation** is the integration spike: a no-op `orchestra worker hook` that logs raw stdin to `.orchestra/hook-debug.log`, run against a real worker, captured shapes documented in code before typed handlers are written.
- **Brief content drift.** If a brief is too vague, engineers wander. If too prescriptive, no orchestration is exercised. The acceptance test is most useful when the PM has to do real coordination work; if engineers can complete in 1 turn each, we proved nothing. Tune iteratively as the e2e runs.
- **Merge logic.** v1 keeps merges trivial (engineers own disjoint files). If two engineers somehow touch the same file, `git merge` will conflict and the test fails honestly. That's fine for v1; multi-file ownership negotiation is v2.

## Migration path from v0

v0 codebase is the starting point. v1 work adds:

0. **Hook stdin spike.** A no-op `orchestra worker hook <event>` that logs raw stdin to `.orchestra/hook-debug.log`. Run against a real worker to capture the actual JSON shape Claude Code sends for each event kind. Document the schema in code before typed handlers exist. Everything downstream depends on this.
1. `orchestra worker hook` typed implementation + `.claude/settings.local.json` deep-merge logic in `orchestra init`. Hook merge happens once at init time, not per spawn — avoids merge races when engineers spawn close together. Spawn doesn't consume the hooks yet — purely additive.
2. New v1 CLI commands (`send`, `answer`, `poll`, `merge`, `reap`, plus `spawn` flags). `poll` returns the bounded state-snapshot format described in the PM execution model section.
3. `role` + `worktree` columns on `workers`. New event kinds (`turn_complete` carries Stop-payload token counts; `tool_started`/`tool_finished`; `session_ready`/`session_ended`/`notification`; `message_sent`; `worktree_created`/`worktree_reaped`; `merge_attempted`/`merge_conflict`/`merge_ok`).
4. Role-aware prompts in `prompts.py`. The PM prompt enforces mega-turn discipline ("don't end your turn until done") and the phase-status-write requirement that feeds the watchdog.
5. Spawn refactored to wait on hook events (when available) instead of polling — `_wait_idle` listens for `session_ready`, `_wait_first_status` listens for the first `Stop`.
6. v1 e2e fixture: the mission file, the verifier, the bash driver. The driver also runs the activity watchdog (no events for 10 min = abort) and reads cumulative tokens from state.db for the cost-ceiling backstop.

Each step is its own task in the implementation plan. v0 tests stay green throughout.

## Testing

### Unit

- `orchestra worker hook X` for each event kind: feed canned JSON to stdin, assert correct row in tmpdir state.db.
- Settings.local.json merge: empty file, existing settings with no hooks, existing settings with overlapping hooks.
- Role-aware prompt rendering: PM template contains team/brief/verifier sections; engineer template contains brief reference + worktree.
- `orchestra merge` happy path + conflict-path event recording.
- `orchestra reap` removes worktree and deletes branch.

### Integration

- Spawn waiting on hook events: mock tmux but inject a `session_ready` event into the DB mid-wait; assert spawn proceeds.
- PM-to-engineer message: `orchestra send <id> "..."` records the right event and calls `tmux.send_multiline`.
- Escalation round-trip: engineer escalate → PM polls → PM answers → engineer receives in pane (tmux mocked).

### End-to-end (the v1 acceptance test)

`scripts/e2e-build-urlshortener.sh`. Manual / opt-in (consumes API credits + needs authenticated claude).

Responsibilities:

- Clean `worktrees/` (`git worktree remove --force` on any leftovers from prior runs; rm any orphaned branches `orch/*`).
- Set up fresh project dir, run `orchestra init`, kick off the PM mission.
- Run three background watchdogs:
  - **Wall-clock.** Abort if not done in 90 min (target 45 min).
  - **Activity.** Abort if no new event in `state.db` for 10 min.
  - **Cost.** Sum token counts from `turn_complete` event payloads × model pricing; abort if total > `MAX_BUDGET_USD` (default $10).
- Exit 0 if the verifier passed, non-zero otherwise. Print the final state.db summary on exit.

This is the contract for v1 being "done."

## Decisions made during refinement (2026-05-17 round 2)

- **PM execution model:** single mega-turn (one Claude response orchestrates the whole run, `orchestra poll` blocks within it). `Stop`-as-heartbeat doesn't work because `Stop` fires on response end, not between tool calls.
- **`orchestra poll` output:** bounded state snapshot (per-worker status + new-event count + pending escalations + last status message), not a raw event feed. Size stays flat across many polls; protects PM context.
- **PM crash recovery:** none in v1. Accept the loss; let the e2e script's wall-clock + activity watchdogs catch failures. Resume protocol deferred to v2.
- **Hook installation:** init-time (`orchestra init` deep-merges `.claude/settings.local.json` once). Spawn-time merge would race when engineers spawn close together.
- **PM brief authoring:** `Write` tool directly to `.orchestra/briefs/<id>.md`. No `orchestra brief` helper.
- **Cost ceiling:** enforced by e2e script using `token_usage` events; `orchestra` core stays stateless about cost. No `--max-tokens` flag on spawn.

## Open questions deferred to v2+

- **Reviewer role.** No reviewer in v1. PM's verifier output is the final word. If the PM declares success but the URL shortener has a subtle bug, we won't catch it. v2 work.
- **PM resume protocol.** If single mega-turn proves too crash-prone in practice, build `.orchestra/pm-notes.md` + `orchestra resume <project>`. Add only if data demands it.
- **Per-worker token caps.** v1 has only an e2e-level total-token ceiling. Per-worker caps (`--max-tokens` on spawn) are v2 ergonomics.
- **Rate-limit watchdog.** Single-source-of-truth retry-on-429 logic. v2 hardening.
