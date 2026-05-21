# Post-run: orchestra for on-device testing — predictions vs reality

**Date:** 2026-05-21
**Run:** ws-scrcpy v4.0 acceptance against Samsung S22 Ultra. PM (opus) → QA (sonnet) → Tester (haiku). 8 scenarios designed + executed in ~14 min wall-clock. Costs: PM $0.01, QA + Tester showed $0.00 (cost-zero issue #33 — real).

**Outcome:** **v4.0 migration is proven on real hardware.** The v4.0 server reports `'4.0'`, accepts our key=value cmdline, opens `@scrcpy_<scid>` abstract socket, writes no PID file, has no `nohup` in argv, emits the 0x00 dummy byte over the forwarded socket, enforces version mismatch, tears down cleanly when host closes the adb connection. All eight survey-doc-derived expectations confirmed.

## Predictions vs reality

| # | Prediction | Verdict |
|---|---|---|
| 1 | `adb` PATH won't propagate to worker pane | **Half-right.** PATH did propagate (PM successfully ran adb commands). The real issue was that the adb-over-network connection state is volatile — Tester saw an empty `adb devices` list because the network connection had dropped. New finding below. |
| 2 | No "shared resource" primitive — parallel testers would race | **Avoided this run** (single sequential Tester), but the gap stands. Still a real v2.4+ improvement. |
| 3 | Tester's deny=Write means it uses Bash append for results.md | **Confirmed.** Tester used `printf >>` and `echo >>` instead of the Write tool. Worked, slightly awkward. The mental model "deny=Write is a tool gate, not a filesystem gate" wasn't surprising for me but worth a docs note. |
| 4 | Haiku might not handle escalations gracefully | **Wrong — Haiku was great.** Tester escalated cleanly when device was missing, then later diagnosed two false-positives in scenarios 2 & 8 (pgrep self-match + sh-wrapper inflating the process count) as artifacts, not real failures, AND verified the underlying semantics. Cheap model held up. Cost-per-token math worked out: full run was ~$0.01 PM + ~$0 for the rest. |
| 5 | Sequential dispatch wastes orchestra's parallelism | **Confirmed but tolerable.** No `spawn-batch`, no `merge --batch`. 14 min wall-clock for a setup that could be a 60-second bash script — but the observability (state.db, hook-debug.log, escalation audit trail) is worth the orchestra wrapper for genuinely flaky environments like adb-over-network. |
| 6 | v2.2's reap-by-default would delete QA's scenarios.md prematurely | **Wrong.** Reap happens AFTER merge, so QA's branch is merged into main before the worktree is removed. Tester (no worktree) reads from main. The pattern works as designed. |
| 7 | Tester results need to persist past worker_done | **Confirmed and handled in the role design.** Tester's brief had `git add + git commit` in its protocol. Worked. The role's `permissions.allow` for `Bash(git add:*)` / `Bash(git commit:*)` was the enabler. |
| 8 | First-time-using-roles scaffolding overhead | **Confirmed.** Two .md role files written from scratch (`qa.md`, `tester.md`). Permissions block for `tester.md` had to enumerate 20+ allow patterns. Definitely worth a `orchestra new-role <name> [--read-only]` scaffolder. |

## Unanticipated wins

9. **PM autonomously fixed the environmental issue.** When Tester escalated "no device attached", PM (without prompt) ran `adb connect 192.168.8.231:40301` and told the Tester "device reconnected, proceed". This emergent behavior from the cooperative escalation pattern is the strongest argument for using orchestra in flaky-environment tests where a bash script would just fail.

10. **Haiku diagnosing artifacts is real reasoning.** Scenarios 2 and 8 "failed" by literal pgrep `wc -l` count but the Tester (Haiku!) figured out that the `sh -c` wrapper and pgrep's self-match were inflating the count, verified the actual server lifecycle via `pgrep -af`, and reported the diagnosis. That's not "dumb command running" — that's a useful cheap model in a well-scoped role.

11. **The mission lint false-positive (#32) didn't bite** because this mission's spawn calls are inline bash commands inside the PM PROTOCOL, not JSONL blocks. Lint passed `OK`. Good case design accidentally dodged the bug.

## Unanticipated pain

12. **adb-over-network connection state is shared across orchestra workers and unpredictable.** The PM had to run `adb connect` once. If Tester gets re-dispatched mid-run, it might find the connection dropped again. There's no per-mission "device pre-flight" primitive. Mission docs ended up encoding this implicitly.

13. **Cost column shows `$0.00` for QA and Tester** (confirming v2.3 #33). PM showed $0.01 — its tokens get billed correctly. The two non-PM workers should each have shown a few cents. Issue #33 is still valid and worth fixing.

14. **adb device list got cluttered.** PM connected via IP, so during the run `adb devices` showed both the original mDNS entry AND the IP entry. After tmux teardown the IP entry vanished. This is adb-over-network's normal behavior, not orchestra's bug — but missions that touch host-side state should include a "consolidate state" step.

15. **Permissions block on `tester.md` is verbose.** 20 lines to enumerate "the Tester can run adb, node, basic shell utilities, and limited git". A future v2.4+ might offer named permission profiles like `--profile read-only-bash-runner` that bundle the common pattern.

## Concrete v2.4 issues to file from this run

A. **`orchestra new-role <name> [--read-only|--engineer|--reviewer]`** — scaffold a `.orchestra/roles/<name>.md` with a sensible permissions skeleton for the chosen archetype. Saves the 20-line enumeration most users will do.

B. **Mission "pre-flight" hook for environmental setup** — a `pre_run` script block in the mission (or a separate `.orchestra/pre-run.sh`) that the PM template tells the PM to execute before spawning any worker. Use case: `adb connect <addr>` before any test that touches the device. Would have shortcut the PM-diagnoses-and-reconnects cycle.

C. **Named permission profiles** — `permissions: profile: read-only-runner` in front matter, with bundled profiles for `engineer-default`, `reviewer-readonly`, `runner-bash-only`, `architect-docs-only`. Reduces role file boilerplate.

D. **`orchestra status --device` panel** — if the mission mentions adb (or generally touches external state), add a host-state column showing `adb devices` and any active `adb forward` entries. Pure visibility win.

E. **Shared-resource lock / `--exclusive` flag on spawn** — for missions that touch a single piece of hardware. `orchestra spawn ... --exclusive-resource device` would refuse to start the worker if another worker with `device` is alive. Belated semantics for "this mission only has one X".

## Verdict on the architecture you proposed

> PM reviews features → QA designs tests → Dumb agent (Haiku) runs commands → QA back-reports to PM, can add more tests

**Works.** Strongly enough that I'd recommend it as a pattern for any orchestra-driven test campaign against external hardware. The cost-tier separation (Opus PM, Sonnet QA, Haiku Tester) is genuinely useful — the bulk of the work is in the cheapest model, design judgment in the middle, and architectural decisions at the top. The cooperative escalation pattern is what makes this pattern survive flaky environments.

The only real friction was the role-file scaffolding (item 8 / future-issue A) and the engineer-cost-zero reporting (#33). Neither blocks the pattern's usefulness.
