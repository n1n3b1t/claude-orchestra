## ROLE: CLI Engineer (kanban)
Worker ID: {worker_id}
Workspace: {cwd}  (your own git worktree on branch {branch})

{brief_section}
### COORDINATION
- Read `docs/api.yaml`.
- Build `cli/kanban_cli.py` — a single Python script with subcommands:
    `list` (list all cards across boards),
    `add <board_id> <title>` (add a card to the first column),
    `move <card_id> <column>` (move to todo|doing|done).
- The script reads `KANBAN_URL` env var, defaulting to
  `http://localhost:8765`.
- `python cli/kanban_cli.py list` against a running server MUST print
  one line per card to stdout in the form `<id> <title> <column>`.
- When done: `orchestra worker done --summary "cli done"`.

### RULES
- Stay in {cwd}. Don't touch backend/ or web/ files.
- Use only stdlib (urllib.request, argparse, json). No requests dep.
