# Getting started with orchestra in a fresh project

This guide walks you from zero to a running PM-coordinated mission. It is
aimed at external users who have never used orchestra before. By the end of
step 5 your project will be fully configured and ready to run; step 6 is where
the PM spawns and API credits are spent.

> **Heads-up:** Step 6 (`orchestra run`) spends Claude API credits. Stop before
> step 6 if you do not want to spend any yet — everything up to step 5 is free.

## Want Claude to set this up for you?

Paste the block below into a fresh Claude Code session **opened inside your
project directory**. The prompt is fully self-contained — Claude will fetch the
guide itself and execute the setup without needing any further input from you
until step 6.

```
You are helping me set up `orchestra` in this project.

Fetch and read this guide:
https://raw.githubusercontent.com/n1n3b1t/claude-orchestra/main/GETTING_STARTED.md

Then execute steps 1 through 5 in order. After each step, run the
listed `Verify` command and only proceed if it passes. If any verification
fails, STOP and ask me before doing anything else. Do NOT run step 6
(`orchestra run`) — it spends API credits. When you reach the boundary between
step 5 and step 6, summarize what you have done so far and wait for my explicit
confirmation before continuing.

If any command would require `sudo`, STOP and ask me first — I will run it
myself.
```

## 1. Prerequisites

**What:** Confirm the four tools the guide depends on are installed.

**Run:**
```bash
python3 --version   # need 3.10+
tmux -V
git --version
claude --version    # the Claude Code CLI; you must already be logged in
```

**Verify:** All four commands exit 0 and the Python version is `>= 3.10`.

## 2. Install orchestra

**What:** Clone the orchestra repo to a location **outside** your project,
install it into a virtualenv, and make the `orchestra` CLI available on
`PATH`. orchestra is not on PyPI yet — source install is the only path.

**Run:**
```bash
cd ~/dev   # or wherever you keep tools
git clone https://github.com/n1n3b1t/claude-orchestra.git
cd claude-orchestra
python3 -m venv .venv
.venv/bin/pip install -e .
# Either add .venv/bin to PATH, or use the absolute path everywhere:
export PATH="$PWD/.venv/bin:$PATH"   # session-scoped; add to ~/.bashrc or ~/.zshrc to persist
```

**Verify:**
```bash
orchestra --version
```

Expected: a version string prints, exit 0.

## 3. Initialize orchestra in your project

**What:** Switch into your project, ensure it is a git repo, and run
`orchestra init`. This creates `.orchestra/` (the state directory) and merges
Claude Code hook entries into `.claude/settings.local.json` so the
orchestrator can observe worker events.

**Run:**
```bash
cd /path/to/your/project   # agents already inside the project can skip this
git init           # skip if already a repo
orchestra init
```

**Verify:**
```bash
test -d .orchestra && grep -q orchestra .claude/settings.local.json && echo OK
```

Expected: `OK`.

## 4. Write a mission file

**What:** Scaffold a new mission under `missions/<slug>/`. Replace `<slug>`
with a short, lowercase name (e.g. `urlshortener`, `kanban-v2`). The slug
becomes the mission identity in `state.db`, the worktree namespace
(`worktrees/<slug>/<engineer>/`), and the branch prefix
(`orch/<slug>/<engineer>`).

**Run:**
```bash
orchestra mission new my-first-mission
$EDITOR missions/my-first-mission/mission.md
```

Fill in the placeholder goal, acceptance criteria, and team sections. Keep the
`worker_done` reference — that is what tells the PM how to terminate.

**Verify:**
```bash
orchestra mission lint missions/my-first-mission/mission.md
```

Expected: exit 0, no `warning:` lines.

## 5. (Optional) Customize the verifier script

**What:** `orchestra mission new` already scaffolds
`missions/my-first-mission/verifier.sh` alongside the mission file. Open it
and replace the placeholder checks with your real acceptance commands.

**Run:**
```bash
$EDITOR missions/my-first-mission/verifier.sh
# make sure it's executable (the scaffold sets this, but double-check):
chmod +x missions/my-first-mission/verifier.sh
```

**Verify:**
```bash
bash missions/my-first-mission/verifier.sh; echo "exit=$?"
```

Expected: `exit=0` once you have filled in real checks.

## 6. Run the mission

> **API credits:** This step spawns real Claude Code workers. Each PM turn and
> each engineer turn costs tokens. A typical small mission (one engineer,
> ~30 turns) costs a few dollars. Confirm your Anthropic account has quota
> before proceeding.

**What:** Spawn the PM and block until it either signals done or a watchdog
fires. Default watchdogs: 90 min wall-clock, 10 min activity silence.

**Run:**
```bash
orchestra mission run my-first-mission
```

**Verify:** In a second terminal:
```bash
orchestra status
```

Expected: the PM appears in the table with status `running`.

## 7. Watch progress

While the mission is running, three read-only ways to observe it:

- `orchestra dash` — open http://localhost:8765 in a browser for the live
  dashboard.
- `orchestra tail <id>` — follow one worker's pane output in your terminal.
- `orchestra status` — one-shot snapshot of every worker.

## 8. When it finishes

The PM emits a `worker_done` event when its acceptance checks pass. At that
point:

- `orchestra status` shows the PM as `done`.
- The tmux session (`orch-<projectname>`) can be killed safely.
- `git log --oneline main` shows the merged engineer commits.

## 9. Troubleshooting

- **Trust prompt did not dismiss on first spawn.** See `CLAUDE.md` section
  "event-driven spawn waits" for why this can happen and how the dismissal
  step works.
- **`orchestra run` aborts with exit code 2.** Your `.orchestra/pre-run.sh`
  (if present) exited non-zero. Check its output; common cause is a flaky
  `adb connect` for on-device missions.
- **Cost watchdog never fires.** Known issue — the `Stop` hook payload does
  not carry usage data. See `CHANGELOG.md` and the v1.2 milestone.
- **PM spawned but engineer hooks are not firing.** Worktrees get their own
  `.claude/settings.local.json` merged automatically by `orchestra spawn`.
  If you created a worktree manually outside the orchestra flow, hooks will
  not be registered there.

## 10. Next steps

- Custom roles (multi-engineer topology, read-only reviewers): see
  [`README.md` — "Defining custom roles"](README.md#defining-custom-roles)
  and `missions/kanban/`.
- Pre-run setup steps (e.g. `adb connect`): [`CLAUDE.md` — "Pre-run
  hook"](CLAUDE.md#pre-run-hook).
- How orchestra works under the hood: [`CLAUDE.md` — "Architecture — the big
  picture"](CLAUDE.md#architecture--the-big-picture).
