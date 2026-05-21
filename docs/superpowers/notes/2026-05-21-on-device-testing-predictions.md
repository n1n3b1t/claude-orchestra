# Predictions: orchestra for on-device testing (pre-run)

**Date:** 2026-05-21
**Context:** about to run a PM → QA → Tester orchestra against the Samsung S22 Ultra to verify the ws-scrcpy v4.0 migration (phases 0-3). Writing down predicted pain points first so the post-run review is honest.

## The architecture being tested

- **PM** (opus, no worktree) — reads the migration arc + survey docs, asks QA to design tests, dispatches Tester, reads results.
- **QA** (sonnet, worktree) — reads code + docs, produces `docs/on-device-tests/scenarios.md` with concrete commands + pass criteria. One-shot: design, commit, exit.
- **Tester** (haiku, no worktree, read-only via composition role) — executes each scenario via Bash, captures evidence to a results log, escalates on unexpected outputs. Permissions deny Write/Edit/destructive Bash; allow Read/Grep/Glob + general Bash.

This is the first time we'll exercise:
- Three distinct roles in one run (not just engineer/pm)
- The haiku model on a worker (cheaper than sonnet)
- A read-only-with-Bash worker that LOGS via shell append (not the Write tool)

## Predicted pain points

1. **PATH for adb.** `adb` lives at `/home/n1n3b1t/platform-tools/adb`, not on the default PATH the tmux pane inherits. Tester will need to either (a) prepend explicitly in every command, (b) get the path injected via the brief, or (c) source a shell rc. Predicted friction: mission text bloat.

2. **No "shared resource" primitive.** One adb device. If Tester needs to be re-dispatched (e.g. after QA adds more checks), the second Tester must wait for the first to fully tear down (scrcpy server killed, port forward removed). Today orchestra has no lock primitive — if the PM accidentally spawns parallel Testers via spawn-batch, they'd race. Predicted finding: "device lock" / "exclusive resource" primitive worth adding to v2.4.

3. **Tester needs to APPEND to a log file but role denies Write.** The cleanest test trace is a results.md file with per-scenario output. But the Tester role's `permissions.deny: [Write, Edit]` means the Write tool is blocked. The Tester will fall back to `echo X >> results.md` via Bash, which is uglier and harder for a Haiku model to do consistently. Predicted finding: Claude Code's permission model is "tool gate" not "filesystem gate"; deny-Write doesn't actually prevent file writes via Bash. We may want a clearer mental model in the docs.

4. **Haiku might not handle escalations gracefully.** Haiku is fast and cheap but less robust at structured reporting. If Tester hits something unexpected (e.g. the v4.0 jar fails to load on the device), will Haiku know to escalate vs. flail? Predicted finding: cheap model is fine for command-running but the brief needs explicit "if X, escalate" instructions.

5. **Sequential dispatch wastes orchestra's mega-turn model.** PM → QA → merge → Tester is strictly serial. spawn-batch is irrelevant. merge --batch is irrelevant. Orchestra's parallelism story doesn't add value here; only the cooperative-event-bus + hooks do. Predicted finding: orchestra IS overkill for purely-serial pipelines but the observability is genuinely useful (hook-debug.log + state.db is a free audit trail).

6. **Worktree teardown deletes the QA's scenarios.md before Tester reads it.** v2.2's reap-by-default means as soon as PM merges QA's branch, the QA worktree is removed. The merged file is in `main`, so Tester (no worktree) reads it from main — fine. But if Tester's brief tries to reference QA's worktree by path, it'll 404. Predicted finding: minor doc hazard; PM brief must point Tester at main-relative paths only.

7. **Tester results need to PERSIST after worker_done.** Engineer worktree gets reaped on merge. If Tester writes results to its own worktree (it has none) or runs `cat > /tmp/foo` (ephemeral), the evidence is gone after the run. Predicted finding: results should be in the main checkout, committed by Tester via `git add + git commit` before signaling done. Requires Tester role permissions allow `Bash(git add:*)` + `Bash(git commit:*)`.

8. **First-time-using-roles overhead.** ws-scrcpy is initialized but has no `.orchestra/roles/`. We need to create that dir + write `qa.md` and `tester.md` before spawning. Predicted finding: scaffolding overhead for a new project. Worth a `orchestra new-role <name> --read-only` scaffolder for v2.4.

## What I'll consider "success"

- Test produces a results.md committed to main listing each scenario + pass/fail with command evidence
- All real device interactions (push, run, forward, connect, cleanup) are exercised
- At LEAST one escalation cycle happens (Tester reports something unexpected → PM intervenes) — proves the cooperative loop works at scale

## What I'll write down post-run (to compare to predictions)

- Which predicted pain points were real
- Which predictions were wrong / overblown
- Any pain points I didn't anticipate
- Concrete v2.3 / v2.4 issues to file
