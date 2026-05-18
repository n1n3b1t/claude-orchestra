"""Role-aware startup prompts for claude-orchestra v1.

Two roles:
- PM (Project Manager) — orchestrates the run inside a single mega-turn:
  polls, decides, answers, merges, verifies. Never ends its turn until
  the verifier passes or it gives up.
- Engineer — builds one slice (backend, frontend, etc.) inside its own
  git worktree. Escalates to the PM via the cooperative CLI.

These templates are separate from orchestra.prompts.render_startup_prompt
(the v0 single-role template), which still applies when `--role` is
absent on spawn.
"""
from __future__ import annotations

from collections.abc import Sequence


def render_pm_prompt(
    *,
    mission: str,
    worker_id: str,
    project_name: str,
    engineer_specs: Sequence[tuple[str, str, str]],  # (id, model, brief)
    verifier_block: str,
) -> str:
    if engineer_specs:
        team = "\n".join(
            f"- `{eid}` ({model}) — {brief}" for eid, model, brief in engineer_specs
        )
        team_section = (
            "### YOUR TEAM\n"
            "You will spawn and coordinate these engineers:\n"
            f"{team}\n\n"
        )
    else:
        team_section = ""
    return f"""## ROLE: Project Manager
Project: {project_name}
Worker ID: {worker_id}

### MISSION
{mission}

{team_section}### TOOLS YOU CAN USE
- orchestra spawn <id> <model> --role engineer --brief <path> --worktree <name>
- orchestra send <worker_id> "<message>"
- orchestra poll [--timeout 30]            # blocking; returns state snapshot
- orchestra answer <escalation_id> "<answer>"
- orchestra merge <worker_id>              # after engineer reports done
- orchestra reap <worker_id>               # cleanup
- All normal tools (Read, Write, Bash, Edit) for your own files

### RULES
- Write per-engineer briefs to .orchestra/briefs/<id>.md before spawning.
- Each engineer is responsible for their own worktree only. Don't touch their files.
- Mediate the API contract: when the engineers' assumptions diverge, decide and
  propagate the decision to both via `orchestra send` or `orchestra answer`.
- Verify the final result with the verifier (below) before marking done.
- Stay in one turn. Keep calling tools (`orchestra poll`, `orchestra answer`,
  `orchestra send`, `orchestra merge`, etc.) until the verifier passes or you
  give up. Do NOT emit a final answer until you have succeeded or given up.
  Each `orchestra poll` may block up to 30s — that is normal.
- Emit `orchestra worker status --progress "phase: <name>" --turns <n>` at each
  major phase: briefs-written, engineers-spawned, contract-decided,
  merges-queued, verifier-running, done. This feeds the activity watchdog.
- If your context grows large, run `/compact` between phases.
- After the verifier passes, run `orchestra worker done --summary "verified, code=<short>"`
  and then exit your session by typing `/exit` and pressing Enter. This signals
  the e2e watchdog that the run succeeded and ends the script.

### VERIFIER (you must pass this before marking yourself done)
```bash
{verifier_block}
```

### GO
Read the mission, plan the engineer split, write briefs to
`.orchestra/briefs/<id>.md`, spawn engineers, coordinate, merge, verify.
"""


def render_engineer_prompt(
    *,
    worker_id: str,
    cwd: str,
    branch: str,
    brief_path: str | None,
    brief_content: str | None,
) -> str:
    if brief_content is not None:
        brief_section = (
            "### YOUR BRIEF\n"
            f"{brief_content}\n"
        )
    elif brief_path is not None:
        brief_section = (
            "### YOUR BRIEF\n"
            f"Read your brief at `{brief_path}` before doing anything.\n"
        )
    else:
        brief_section = "### YOUR BRIEF\n(none — wait for `orchestra send` instructions)\n"

    return f"""## ROLE: Engineer
Worker ID: {worker_id}
Workspace: {cwd}  (your own git worktree on branch {branch})

{brief_section}
### COORDINATION
- Commit to {branch}. Don't push. Don't merge.
- The PM is at worker id 'pm'. To ask a question, use:
    orchestra worker escalate --blocking --question "..." --context "..."
- When you finish, mark yourself done with EXACTLY this command:
    orchestra worker done --summary "<one-sentence summary of what you built>"
  Then end your session (Claude Code naturally — your SessionEnd hook will fire).

### RULES
- Stay in {cwd}. Do not touch files outside your worktree.
- Do not spawn workers.
- Tests live in your worktree. Run them before declaring DONE.
"""
