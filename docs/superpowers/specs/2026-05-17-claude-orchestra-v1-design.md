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
6. PM runs the verifier: `pytest && uvicorn app:app &` then `curl -X POST localhost:8000/shorten -d '{"url":"https://example.com"}'` (expects 200 + a code), then `curl -I localhost:8000/<code>` (expects 302).
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
- Inter-worker coordination via the existing escalation channel + a new "PM brief" message channel.
- Adaptive heartbeat for the PM (it sleeps between polls, wakes on engineer events).
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

Same plan as the previous v1 draft, kept here for completeness.

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

| Event | DB event kind | Worker row mutation |
|---|---|---|
| SessionStart | `session_ready` | `status=working` |
| Stop | `status` | `turns += 1` |
| PreToolUse | `tool_started` | none (payload: tool name + input summary) |
| PostToolUse | `tool_finished` | none |
| SessionEnd | `session_ended` | `status=done` if no prior error/stop |
| Notification | `notification` | none |

### Spawn loses two polling loops

- `_wait_idle` becomes "block until a `session_ready` event for this worker_id appears in state.db, max 60s".
- `_wait_first_status` becomes "block until first `Stop` event for this worker_id, max 90s".
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

### Heartbeat-style PM tick

v0 had no heartbeat — workers ran independently and the dashboard polled. v1 adds a PM-side heartbeat: while waiting for engineers to finish, the PM runs a loop:

1. `orchestra poll` — block up to 30s on a new event for any tracked worker, or return immediately if one has fired since last poll. Returns a structured summary (workers that completed turns, new escalations, sessions that ended).
2. Decide what to do (answer an escalation, send a follow-up message, merge a completed branch, declare done).
3. Repeat.

This replaces the v0.1-roadmap heartbeat process. The PM-as-coordinator owns its own loop; there's no separate daemon.

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
- `tool_started`, `tool_finished` (Pre/PostToolUse)
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
| `orchestra poll [--timeout 30]` | Block up to N seconds for a new event in any worker the PM tracks. Print a structured summary. PM-flavoured wrapper around state.db queries. |
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
- `uvicorn app:app --port 8000` starts the server.
- `curl -X POST localhost:8000/shorten -H 'content-type: application/json' -d '{"url":"https://example.com"}'`
  returns HTTP 200 with a JSON body `{"code":"<short>"}`.
- `curl -I localhost:8000/<short>` returns HTTP 302 with `Location: https://example.com`.
- `curl localhost:8000/` returns an HTML page with a form posting to `/shorten`.

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

- **Claude API rate limits.** Three concurrent workers chewing through tokens can hit limits. v1 does NOT include the watchdog. Mitigation: if it bites, surface the error and stop; document it as known v1 limitation.
- **PM blocks on `orchestra poll`.** If `poll` is implemented as a sleep loop inside the PM's claude turn, claude's own response-streaming may not look idle. Need to verify the Stop hook fires when `poll` blocks (it should — Stop fires on response end, not on tool end). Test this empirically in the first integration spike.
- **Worktree state across runs.** Worktrees persist between runs unless `reap`ed. The e2e script should `git worktree remove` everything in `worktrees/` at the start.
- **Hook stdin format drift.** Claude Code's hook JSON schema may change. First integration spike: write a no-op hook that logs raw stdin to `.orchestra/hook-debug.log`, run against a real worker, document the actual shape.
- **Brief content drift.** If a brief is too vague, engineers wander. If too prescriptive, no orchestration is exercised. The acceptance test is more useful if PM has to do real coordination work; if engineers can complete in 1 turn each, we proved nothing.
- **Merge logic.** v1 keeps merges trivial (engineers own disjoint files). If two engineers somehow touch the same file, `git merge` will conflict and the test fails honestly. That's fine for v1; multi-file ownership negotiation is v2.

## Migration path from v0

v0 codebase is the starting point. v1 work adds:

1. `orchestra worker hook` CLI subcommand + settings.local.json merge logic. Spawn doesn't use it yet — purely additive.
2. New v1 CLI commands (`send`, `answer`, `poll`, `merge`, `reap`, plus `spawn` flags).
3. `Role` + `worktree` columns on `workers`.
4. Role-aware prompts in `prompts.py`.
5. Spawn refactored to wait on hook events (when available) instead of polling.
6. v1 e2e fixture: the mission file, the verifier, the bash driver.

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

`scripts/e2e-build-urlshortener.sh`. Manual / opt-in (consumes API credits + needs authenticated claude). Sets up fresh project dir, runs the PM mission, waits up to 90 min, exits 0 if verifier passed.

This is the contract for v1 being "done."

## Open questions

- **Where do hooks live: init-time or spawn-time?** `orchestra init` is cleaner (one writeup per project); per-spawn risks racing if two workers spawn close together. Decide during implementation; init-time is the favored path.
- **`orchestra poll` semantics.** Block until any event OR timeout? Or block until events from a specific worker subset? PM probably wants the latter. Default to "all workers I (the caller) tracked" — implementation detail.
- **PM authoring its own brief.** The mission file is human-written. Engineer briefs are PM-authored. Should the PM have a template helper (`orchestra brief <engineer> "..."`) or just `Write` to `.orchestra/briefs/<id>.md`? `Write` is simpler.
- **What if the PM is wrong?** No reviewer in v1. PM's verifier output is the final word. If the PM declares success but the URL shortener has a subtle bug, we won't catch it without a Reviewer role. Documented v2 work; v1 trusts the PM.
- **Cost ceiling.** A naive run could spend $10-50 in API credits depending on how many turns each engineer needs. e2e script should record cumulative token usage (from Stop hook payload) and abort if a threshold is exceeded. Add a `--max-tokens` flag.
