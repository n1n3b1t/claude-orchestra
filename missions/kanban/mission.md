# Mission: minimal Trello-lite kanban app

Build a small kanban app with three clients sharing one HTTP API:

- **Backend** — FastAPI HTTP server on port 8765 with SQLite storage. Endpoints below.
- **Web** — static HTML + JS single-page client served at `/`, drag-and-drop columns.
- **CLI** — a `kanban-cli` Python script (entrypoint at `cli/kanban_cli.py`) that
  lists/creates/moves cards via the same API.

## API contract (the architect produces this first; see below)

```yaml
openapi: 3.1.0
info: { title: kanban, version: 0.1.0 }
paths:
  /api/health: { get: { responses: { '200': { description: ok } } } }
  /api/boards:
    get: { responses: { '200': { description: list boards } } }
    post:
      requestBody:
        required: true
        content:
          application/json:
            schema: { type: object, properties: { name: { type: string } } }
      responses: { '200': { description: created } }
  /api/boards/{board_id}/cards:
    get: { responses: { '200': { description: list cards } } }
    post:
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                title: { type: string }
                column: { type: string, enum: [todo, doing, done] }
      responses: { '200': { description: created } }
  /api/cards/{card_id}:
    patch:
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties: { column: { type: string, enum: [todo, doing, done] } }
      responses: { '200': { description: moved } }
```

The architect MUST commit this YAML to `docs/api.yaml` on main BEFORE any
engineer is spawned. Engineers' worktrees are created from main and will
inherit the file.

## Acceptance

- `missions/kanban/verifier.sh` exits 0 against a running server.
- The verifier checks: `/api/health` returns 200, a POST to `/api/boards`
  returns a JSON `{ "id": ..., "name": ... }`, a POST to that board's
  `/cards` returns a card with the requested `title`, a PATCH to
  `/api/cards/<id>` moves the card to `done`, the web client at `/`
  returns HTML containing the strings `kanban` and `todo`, and the CLI
  `python cli/kanban_cli.py list` against the running server prints the
  created card's title to stdout.

## TEAM

(The PM template's "YOUR TEAM" block is empty when the runner is invoked
without inline engineer_specs; the team is enumerated here.)

Spawn in this order:

1. **architect** (sonnet) — writes `docs/api.yaml` (verbatim from the
   contract above, or a refined version) and commits it on main; calls
   `orchestra worker done` when committed.
   - Brief at `.orchestra/briefs/architect.md`, role at
     `missions/kanban/.orchestra/roles/architect.md`.

After architect's branch is merged into main, spawn three engineers in
parallel:

2. **backend** (sonnet) — implements the FastAPI server in `backend/app.py`
   with SQLite at `backend/kanban.db`. Reads `docs/api.yaml` to confirm
   the contract.
3. **web** (sonnet) — implements `web/index.html` + `web/app.js` served
   by the backend at `/`. Reads `docs/api.yaml` for endpoints.
4. **cli** (sonnet) — implements `cli/kanban_cli.py` (a Python script,
   not a package) with subcommands `list`, `add`, `move`. Reads
   `docs/api.yaml`.

After all three are merged, spawn the reviewer:

5. **reviewer** (sonnet) — NO worktree. Runs in main checkout with
   restrictive permissions. Reads the merged code, runs the verifier in
   read-only mode (no Write/Edit allowed), and either:
   - calls `orchestra worker done --summary "approved"` if everything
     passes, OR
   - calls `orchestra worker escalate --blocking --question "..." --context "..."`
     with concrete findings, then exits and waits for PM resolution.

## VERIFIER

```bash
bash missions/kanban/verifier.sh
```

## PM PROTOCOL

- Write per-engineer briefs to `.orchestra/briefs/<id>.md`.
- Use `orchestra spawn <id> sonnet "" --role <role-name> --brief <brief-path> --worktree <id>`
  for engineers and the architect. For the reviewer, omit `--worktree`.
- Poll for `worker_done` events. Merge with `orchestra merge <id>`.
- After all merges + reviewer approval, run the verifier. If it passes,
  `orchestra worker done --summary "kanban verified"` and `/exit`.
- If the reviewer escalates, decide: spawn a follow-up engineer to fix,
  or `orchestra answer <esc_id> "override: <reason>"` if the finding is
  spurious.
