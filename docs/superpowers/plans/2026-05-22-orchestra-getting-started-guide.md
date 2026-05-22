# GETTING_STARTED.md Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `GETTING_STARTED.md` at the repo root — a linear, copy-pasteable runbook that lets an external user (or a Claude Code agent acting on their behalf) bring orchestra to a fresh project from zero to a running PM.

**Architecture:** Single Markdown file. Top of file is a paste-prompt block addressed to a Claude Code agent. Body is 10 numbered steps, each with **What / Run / Verify** inline labels. `README.md` gets one new line linking to the guide. No code changes outside docs.

**Tech Stack:** Markdown only. The guide references existing `orchestra` CLI commands (`init`, `mission lint`, `run`, `status`, `dash`, `tail`); no new CLI surface.

**Source spec:** `docs/superpowers/specs/2026-05-22-orchestra-getting-started-guide-design.md`

---

## File Structure

- **Create:** `GETTING_STARTED.md` (repo root)
- **Modify:** `README.md` — add one link line near the top, after the project description paragraph, before the "Findings so far" section.

No other files change. No code, no tests beyond an end-to-end dry-run of the guide itself.

---

## Task 1: Write GETTING_STARTED.md and link from README

**Goal:** Produce the complete guide at the repo root and a discoverability link from `README.md`.

**Files:**
- Create: `GETTING_STARTED.md`
- Modify: `README.md` — insert one line near the top.

**Acceptance Criteria:**
- [ ] `GETTING_STARTED.md` exists at the repo root.
- [ ] First content block (after the H1 and one-paragraph intro) is a paste-prompt fenced code block. The paste-prompt is plain English addressed to Claude, instructs it to execute steps 1–5 in order, run the `Verify` command after each step, stop if any verify fails, and stop at the boundary between step 5 and step 6 to ask the user before spending API credits.
- [ ] An API-credit-cost callout (a blockquote or admonition) appears either inside the intro paragraph or immediately above step 6.
- [ ] Sections present in order: `## 1. Prerequisites`, `## 2. Install orchestra`, `## 3. Initialize orchestra in your project`, `## 4. Write a mission file`, `## 5. (Optional) Add a verifier script`, `## 6. Run the mission`, `## 7. Watch progress`, `## 8. When it finishes`, `## 9. Troubleshooting`, `## 10. Next steps`.
- [ ] Each of sections 1–6 contains the three bold inline labels `**What:**`, `**Run:**`, `**Verify:**` exactly once each. Sections 7, 8, 9, 10 do not require `Verify` (read-only or pointer content).
- [ ] The mission-file template inside section 4 mentions `worker_done` so `orchestra mission lint` produces no warning lines.
- [ ] Section 10 contains three link bullets pointing at: (a) `README.md#defining-custom-roles` or the README section header for custom roles, (b) `CLAUDE.md` section "Pre-run hook", (c) `CLAUDE.md` section "Architecture — the big picture".
- [ ] Total line count is between 150 and 300 inclusive.
- [ ] No occurrences of the literal strings `TBD`, `TODO`, `FILL ME IN`, `XXX`, or `<insert ...>` in the final guide.
- [ ] `README.md` gains exactly one new line near the top (after the project description, before "Findings so far") that links to `GETTING_STARTED.md`.

**Verify:**
```bash
test -f GETTING_STARTED.md \
  && [ "$(wc -l < GETTING_STARTED.md)" -ge 150 ] \
  && [ "$(wc -l < GETTING_STARTED.md)" -le 300 ] \
  && ! grep -E 'TBD|TODO|FILL ME IN|XXX|<insert' GETTING_STARTED.md \
  && grep -q 'GETTING_STARTED.md' README.md \
  && grep -q '^## 1\. Prerequisites' GETTING_STARTED.md \
  && grep -q '^## 6\. Run the mission' GETTING_STARTED.md \
  && grep -q '^## 10\. Next steps' GETTING_STARTED.md \
  && grep -q '\*\*Verify:\*\*' GETTING_STARTED.md \
  && echo OK
```

Expected output: `OK`.

**Steps:**

- [ ] **Step 1: Draft `GETTING_STARTED.md` with the full structure.**

Write the file as one piece. The shape is:

```markdown
# Getting started with orchestra in a fresh project

<one-paragraph intro: what this guide produces, who it is for, that step 6 spends API credits>

> **Heads-up:** Step 6 (`orchestra run`) spends Claude API credits. Stop before
> step 6 if you don't want to spend any yet — everything up to step 5 is free.

## Want Claude to set this up for you?

Paste the block below into a fresh Claude Code session **opened inside your
project directory**. Claude will read the rest of this file and execute the
setup for you, stopping before the credit-spending step.

```
You are helping me set up `orchestra` in this project. Read GETTING_STARTED.md
in the orchestra repo (path I will give you, or fetch via the URL I will
provide), then execute steps 1 through 5 in order. After each step, run the
listed `Verify` command and only proceed if it passes. If any verification
fails, STOP and ask me before doing anything else. Do NOT run step 6
(`orchestra run`) — it spends API credits. When you reach the boundary between
step 5 and step 6, summarize what you've done so far and wait for my explicit
confirmation before continuing.

If any command would require `sudo`, STOP and ask me first — I will run it
myself.
```

## 1. Prerequisites

**What:** Confirm the four tools the guide depends on are installed.

**Run:**
```bash
python3 --version   # need 3.11+
tmux -V
git --version
claude --version    # the Claude Code CLI; you must already be logged in
```

**Verify:** all four commands exit 0 and the Python version is `>= 3.11`.

## 2. Install orchestra

**What:** Clone the orchestra repo to a location **outside** your project, install it into a virtualenv, and make the `orchestra` CLI available on `PATH`. orchestra is not on PyPI yet — source install is the only path.

**Run:**
```bash
cd ~/dev   # or wherever you keep tools
git clone https://github.com/n1n3b1t/claude-orchestra.git
cd claude-orchestra
python3 -m venv .venv
.venv/bin/pip install -e .
# Either add .venv/bin to PATH, or use the absolute path everywhere:
export PATH="$PWD/.venv/bin:$PATH"
```

**Verify:**
```bash
orchestra --version
```

Expected: a version string prints, exit 0.

## 3. Initialize orchestra in your project

**What:** Switch into your project, ensure it is a git repo, and run `orchestra init`. This creates `.orchestra/` (state directory) and merges Claude Code hook entries into `.claude/settings.local.json` so the orchestrator can observe worker events.

**Run:**
```bash
cd /path/to/your/project
git init           # skip if already a repo
orchestra init
```

**Verify:**
```bash
test -d .orchestra && grep -q orchestra .claude/settings.local.json && echo OK
```

Expected: `OK`.

## 4. Write a mission file

**What:** Drop a mission file at `.orchestra/mission.md`. It tells the PM what to build, what "done" looks like, and which engineer roles to spawn. The template below is a generic starting point — replace the bracketed placeholders.

**Run:**
```bash
cat > .orchestra/mission.md <<'EOF'
# Mission: <one-line goal>

Replace this paragraph with a few sentences describing what you want built and any constraints (language, framework, existing files to respect).

## Acceptance
- <criterion 1>
- <criterion 2>
- <criterion 3>

## Team
Spawn the following engineers in their own worktrees:
- engineer (sonnet) — implements the work.

You mediate the API contract (if applicable), merge work into main, run the
acceptance checks, and only emit `worker_done` when every acceptance check
passes.
EOF
```

**Verify:**
```bash
orchestra mission lint .orchestra/mission.md
```

Expected: exit 0 and no `warning:` lines. (The `worker_done` reference in the
template silences the lint warning.)

## 5. (Optional) Add a verifier script

**What:** A shell script the PM can run to confirm acceptance. Optional — the mission body alone is enough to start. Skip this step if your acceptance checks are short enough to inline in the mission.

**Run:**
```bash
cat > .orchestra/verifier.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# Replace these checks with your real acceptance commands.
# Exit 0 = pass, non-zero = fail.
echo "verifier skeleton — replace with real checks"
EOF
chmod +x .orchestra/verifier.sh
```

**Verify:**
```bash
bash .orchestra/verifier.sh; echo "exit=$?"
```

Expected: `exit=0`.

## 6. Run the mission

**What:** Spawn the PM and block until it either signals done or a watchdog
fires. **This spends API credits.** Default watchdogs: 90 min wall-clock,
10 min activity. See `CLAUDE.md` for tuning.

**Run:**
```bash
orchestra run .orchestra/mission.md
```

**Verify:** in a second terminal:
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

- **Trust prompt didn't dismiss on first spawn.** See `CLAUDE.md` →
  "event-driven spawn waits" for why this can happen and how the dismissal
  step works.
- **`orchestra run` aborts with exit code 2.** Your `.orchestra/pre-run.sh`
  (if present) exited non-zero. Check its output; common cause is a flaky
  `adb connect` for on-device missions.
- **Cost watchdog never fires.** Known issue — the `Stop` hook payload
  doesn't carry usage. See `CHANGELOG.md` / the v1.2 milestone.
- **PM spawned but engineer hooks aren't firing.** Worktrees get their own
  `.claude/settings.local.json` merged automatically by `orchestra spawn`.
  If you created a worktree manually outside the orchestra flow, hooks
  won't be registered there.

## 10. Next steps

- Custom roles (multi-engineer topology, read-only reviewers): `README.md`
  section "Defining custom roles" and `examples/kanban/`.
- Pre-run setup steps (e.g. `adb connect`): `CLAUDE.md` section "Pre-run hook".
- How orchestra actually works under the hood: `CLAUDE.md` section
  "Architecture — the big picture".
```

- [ ] **Step 2: Insert the README link.**

Open `README.md`. The current top reads:

```
# claude-orchestra

Experiments in driving Claude Code instances from outside the process — starting
with tmux send-keys, since every Claude Code session is just a TTY-attached
program running in a pane.

## Findings so far
```

Insert a single line between the intro paragraph and the `## Findings so far` heading, so the result is:

```
# claude-orchestra

Experiments in driving Claude Code instances from outside the process — starting
with tmux send-keys, since every Claude Code session is just a TTY-attached
program running in a pane.

New to orchestra? Start with [GETTING_STARTED.md](GETTING_STARTED.md).

## Findings so far
```

- [ ] **Step 3: Run the acceptance verify command.**

Run:
```bash
test -f GETTING_STARTED.md \
  && [ "$(wc -l < GETTING_STARTED.md)" -ge 150 ] \
  && [ "$(wc -l < GETTING_STARTED.md)" -le 300 ] \
  && ! grep -E 'TBD|TODO|FILL ME IN|XXX|<insert' GETTING_STARTED.md \
  && grep -q 'GETTING_STARTED.md' README.md \
  && grep -q '^## 1\. Prerequisites' GETTING_STARTED.md \
  && grep -q '^## 6\. Run the mission' GETTING_STARTED.md \
  && grep -q '^## 10\. Next steps' GETTING_STARTED.md \
  && grep -q '\*\*Verify:\*\*' GETTING_STARTED.md \
  && echo OK
```

Expected output: `OK`. If the line-count bound fails, tighten or expand prose until it passes — do not pad with whitespace.

- [ ] **Step 4: Commit.**

```bash
git add GETTING_STARTED.md README.md
git commit -m "docs: add GETTING_STARTED.md quickstart + README link"
```

---

## Task 2: Dry-run the guide in a clean directory

**Goal:** Execute every `Verify` command in `GETTING_STARTED.md` against a fresh, empty project to catch wrong commands, missing flags, or broken expected outputs. Fix any issues found in the guide. Step 6 (the credit-spending run) is checked by `--help` only.

**Files:**
- Modify (only if dry-run finds issues): `GETTING_STARTED.md`

**Acceptance Criteria:**
- [ ] Every `Verify:` block in sections 1–5 executes successfully against a freshly created scratch project at `/tmp/orchestra-dryrun-<timestamp>` (timestamp picked at run time to avoid stale state).
- [ ] Section 6's `orchestra run` is **not** executed — only `orchestra run --help` is invoked to confirm the command exists.
- [ ] If any verify fails because the guide is wrong, the guide is fixed and the dry-run is re-executed against a fresh scratch directory until it passes cleanly.
- [ ] The scratch directory is removed at the end of the dry-run so no stray test data lingers.

**Verify:** The dry-run script in Step 1 below exits 0 on its final invocation.

**Steps:**

- [ ] **Step 1: Create the dry-run script.**

Save the following to `/tmp/orchestra-guide-dryrun.sh` (do not commit; this is throwaway scaffolding):

```bash
#!/usr/bin/env bash
set -euo pipefail

GUIDE="$(pwd)/GETTING_STARTED.md"
test -f "$GUIDE" || { echo "GUIDE not found at $GUIDE"; exit 1; }

SCRATCH="/tmp/orchestra-dryrun-$(date +%s)"
mkdir -p "$SCRATCH"
trap 'rm -rf "$SCRATCH"' EXIT

cd "$SCRATCH"

echo "=== Step 1: prerequisites ==="
python3 --version
tmux -V
git --version
claude --version

echo "=== Step 2: orchestra --version ==="
orchestra --version

echo "=== Step 3: orchestra init in a fresh repo ==="
git init -q
orchestra init
test -d .orchestra
grep -q orchestra .claude/settings.local.json

echo "=== Step 4: mission template + lint (no warnings) ==="
cat > .orchestra/mission.md <<'EOF'
# Mission: <one-line goal>

Replace this paragraph with a few sentences describing what you want built.

## Acceptance
- <criterion 1>

## Team
- engineer (sonnet) — implements the work.

You only emit `worker_done` when every acceptance check passes.
EOF
LINT_OUT="$(orchestra mission lint .orchestra/mission.md 2>&1)"
echo "$LINT_OUT"
if echo "$LINT_OUT" | grep -q '^warning:'; then
  echo "FAIL: mission lint emitted warnings"; exit 1
fi

echo "=== Step 5: verifier skeleton ==="
cat > .orchestra/verifier.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "verifier skeleton"
EOF
chmod +x .orchestra/verifier.sh
bash .orchestra/verifier.sh

echo "=== Step 6: orchestra run --help (does NOT spend credits) ==="
orchestra run --help >/dev/null

echo "=== Dry-run passed ==="
```

- [ ] **Step 2: Run the dry-run.**

```bash
bash /tmp/orchestra-guide-dryrun.sh
```

Expected output: ends with `=== Dry-run passed ===` and exits 0.

- [ ] **Step 3: Fix any issues the dry-run surfaced.**

If the dry-run failed at any step, the guide is wrong (or the dry-run script's assumption about the guide is wrong — read carefully which side is at fault):

- If the guide listed a command that no longer exists or has a different flag, update the guide.
- If the guide's `Verify` expected output is wrong, update either the verify command or the expected output to match reality.
- If the dry-run script and the guide drifted apart, update whichever is wrong so both reflect the actual CLI.

Re-run Step 2 in a fresh scratch directory until it exits 0 cleanly.

- [ ] **Step 4: Commit any fixes.**

If the dry-run forced changes to `GETTING_STARTED.md`:

```bash
git add GETTING_STARTED.md
git commit -m "docs(getting-started): fix issues found in dry-run"
```

If the dry-run passed first try with no edits, skip this step — there is nothing to commit.

- [ ] **Step 5: Remove the dry-run script.**

```bash
rm -f /tmp/orchestra-guide-dryrun.sh
```

(The script lives in `/tmp` and is auto-cleaned anyway, but be explicit.)
