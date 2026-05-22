# Design: GETTING_STARTED.md — running orchestra in a fresh project

**Date:** 2026-05-22
**Status:** design / pre-implementation
**Output artifact:** `GETTING_STARTED.md` at the repo root, linked from `README.md`.

## Context

Today the README mixes a research log ("Findings so far"), caveats, a brief
"One-shot runner" snippet, and a custom-roles section. `CLAUDE.md` is rich but
addressed to contributors working in this repo, not to an external user trying
to bring orchestra to their own project. There is no single document that
walks a newcomer from "I heard about orchestra" to "the PM is running against
my mission file."

This spec defines that document.

## Goals

1. A newcomer with a fresh project directory can reach a running PM by
   following the guide top-to-bottom — no other docs required.
2. The same document, when pasted into a fresh Claude Code session inside the
   user's project, lets Claude execute the setup itself. Each step has an
   explicit verification command, so the agent can confirm-before-proceeding.
3. Total length stays in quickstart range (~150–300 lines). Anything heavier
   (architecture, role internals, dashboards beyond "open the URL") is linked,
   not inlined.

## Non-goals

- Not a reference manual. Roles, escalations, watchdog tuning, mission-file
  grammar specifics, dashboard internals — all stay in README/CLAUDE.md/CHANGELOG.
- Not a tutorial with a baked-in worked example. The mission file in the guide
  is a generic template the reader fills in.
- Not an installation path other than "clone + pip install from source"
  (orchestra is not on PyPI yet; pretending otherwise would mislead).
- Not a substitute for `CLAUDE.md`. Contributors to this repo continue to read
  CLAUDE.md; GETTING_STARTED.md is purely outward-facing.

## Audience

External user with a fresh project directory who wants to try orchestra. They
have basic shell competence; they may or may not have used Claude Code before
but they have the `claude` CLI installed and logged in. They are either
following the guide themselves or delegating execution to a Claude Code agent
they have started inside their project.

## Structure (linear runbook)

The document is a numbered runbook. Each numbered step contains:

- **What:** a one-line description of the goal of the step.
- **Run:** the exact shell commands, in a fenced code block.
- **Verify:** a one-line command whose successful exit (or expected output)
  confirms the step worked. The agent path depends on this; the human path
  benefits from it too.

### Section list

0. **Paste-prompt block** — at the very top. Fenced code block under a heading
   like *"Want Claude to set this up for you?"*. The block is plain English
   addressed to Claude, telling it: read this file, execute steps 1–5 in
   order, after each step run the listed `Verify` command, stop and ask the
   user if any verification fails or any command requires sudo. **Step 6
   spends API credits and must not be run without explicit user
   confirmation** — the paste-prompt instructs the agent to stop at the
   boundary between step 5 and step 6, summarize what was done, and wait for
   the user to greenlight the actual run.

   The paste-prompt must work as a single copy-paste into a fresh Claude Code
   session. It must not assume Claude has prior context about orchestra.

1. **Prerequisites** — Python 3.11+, tmux, git, `claude` CLI logged in. One
   `Verify` command per requirement (`python3 --version`, `tmux -V`,
   `git --version`, `claude --version`).

2. **Install orchestra** — clone the repo to a location *outside* the user's
   project (recommended: `~/dev/claude-orchestra`), create a venv, `pip
   install -e .`, and ensure the venv's `bin/` is on `PATH` (or use absolute
   paths). Verify: `orchestra --version` prints a version.

3. **Initialize orchestra in your project** — `cd` into the user's project
   (`git init` if needed; `orchestra` requires a git repo), then
   `orchestra init`. Explain in one sentence what this creates
   (`.orchestra/`) and what it merges into `.claude/settings.local.json` (the
   hook entries). Verify: `ls .orchestra` and a `grep` showing the hook
   command landed in `.claude/settings.local.json`.

4. **Write a mission file** — provide a generic template the reader copies to
   `.orchestra/mission.md`. The template has placeholder sections for goal,
   acceptance criteria, and team (which roles to spawn), and must reference
   `worker_done` in the body so `orchestra mission lint` produces no warnings.
   Verify: `orchestra mission lint .orchestra/mission.md` exits 0 with no
   warning lines.

5. **(Optional) Add a verifier script** — short skeleton at
   `.orchestra/verifier.sh` that exits 0 when the user's acceptance checks
   pass. Mark this step optional — the mission file alone is enough to start.
   Verify: `bash .orchestra/verifier.sh; echo $?` (the reader inspects the
   output; an empty skeleton will exit 0 trivially).

6. **Run the mission** — `orchestra run .orchestra/mission.md`. Call out that
   this spends API credits. Mention the default watchdogs (90 min wall,
   10 min activity) in one line; link to CLAUDE.md for tuning. Verify: in a
   second terminal, `orchestra status` lists the PM as `running`.

7. **Watch progress** — three options: `orchestra dash` (browser on :8765),
   `orchestra tail <id>` for a single pane, `orchestra status` for a snapshot.
   No verify command needed — these are read-only.

8. **When it finishes** — describe the terminal state: PM emits `worker_done`,
   `orchestra status` shows `done`, the tmux session can be killed. Suggest
   `git log --oneline` on `main` to see merged work.

9. **Troubleshooting (short)** — bullet list of the most likely failures and
   their one-line fix:
   - Trust prompt didn't dismiss → see CLAUDE.md "event-driven spawn waits".
   - `orchestra run` aborted with exit 2 → `.orchestra/pre-run.sh` failed,
     check its output.
   - Cost watchdog never fires → known issue, see CHANGELOG / v1.2 milestone.
   - PM spawned but engineers can't see hooks firing → worktrees get their
     own `.claude/settings.local.json` merged by `orchestra spawn`; if you
     created a worktree manually outside the orchestra flow, hooks won't be
     registered there.

10. **Next steps** — three bullets linking out:
    - Custom roles → README "Defining custom roles" + `examples/kanban/`.
    - Pre-run hook → CLAUDE.md "Pre-run hook".
    - Architecture / why it works → CLAUDE.md "Architecture — the big picture".

## Format conventions

- Step headings are `## 1. Install orchestra` (numbered, sentence case).
- Inside a step, the `What / Run / Verify` triplet is **bold inline labels**,
  not subheadings, to keep TOC depth shallow.
- Code blocks are language-tagged (` ```bash `) where it matters.
- The doc opens with a one-paragraph framing sentence above section 0, plus a
  callout box reminding the reader that running step 6 spends API credits.

## Cross-linking

- `README.md` gains a single new line near the top: a link to
  `GETTING_STARTED.md` labeled something like *"New here? Start with the
  getting-started guide."*
- The guide itself links to: `README.md` (roles section), `CLAUDE.md`
  (architecture, pre-run hook, watchdog tuning), `CHANGELOG.md` (cost
  watchdog known issue), and `examples/kanban/` (multi-role example).

## Verification of the guide itself

Before merge, the author runs the guide top-to-bottom in a clean directory
(empty folder, fresh `git init`, no `.orchestra/`). Each `Verify` command in
the guide is executed as part of this dry run. The dry run goes up to step 5
(does not need to actually launch a PM and spend credits) — step 6 is checked
by command-existence only (`orchestra run --help`).

## Out of scope / explicit non-decisions

- Whether to publish orchestra to PyPI — out of scope. Install path is
  source-only.
- Whether to add `pipx` instructions — out of scope for v1; can be added later
  without restructuring the doc.
- Localization, screenshots, animated GIFs — none.

## Open follow-ups (after v1 ships)

- Add a `pipx` install path once it has been validated.
- Consider a second guide (or appendix) for the "I have an existing repo with
  history" case, distinct from the fresh-project case this guide targets.
- Revisit the troubleshooting section after the first few external users hit
  failures we didn't anticipate.
