# claude-orchestra v2.2 — DX quick wins

**Status:** design, ready for orchestrated implementation
**Date:** 2026-05-20
**Predecessors:** [v2.1 spec](2026-05-20-claude-orchestra-v2.1-comms-design.md), [orchestra patterns notes](../notes/2026-05-20-orchestra-patterns-after-real-world-use.md)

## Goal

Three independent, small DX wins identified by observing eight orchestrated runs (v1.2 dogfood → ws-scrcpy phase 1). Each is < 100 lines of code, each is independently shippable, all three together close a class of "I had to do that by hand again" friction.

## What v2.2 adds

### 1. `orchestra merge` reaps by default (closes #28)

Today: PM has to remember to call `orchestra reap <id>` after every successful merge. 4 out of 8 dogfood runs the PM forgot. Change merge to reap-on-success by default; add `--keep` for inspection.

- **Single-arg form:** `orchestra merge backend` → merges; on `merge_ok` ALSO reaps (worktree removed, branch deleted). On `merge_conflict` → no reap (keep worktree for inspection).
- **Batch form:** `orchestra merge --batch a b c` → reaps each on its own success. On conflict in c, reaps a + b (which succeeded), does NOT reap c.
- **Escape hatch:** `--keep` flag preserves legacy behavior. Useful for manual inspection.

Files: `orchestra/cli.py` (merge command body), `orchestra/roles/pm.md` (drop the explicit-reap guidance line), `tests/test_cli.py` (update existing merge tests, add conflict-no-reap test).

### 2. `orchestra status` shows live cost (closes #29)

Today: token tracking works (v1.2 #8 reads `transcript_path` JSONL) but the data sits in `turn_complete` events; nothing surfaces it. Add a per-worker cost column to the snapshot.

- New helper `orchestra/poll.py::_compute_cost(conn, worker_id) -> float` that sums input/output tokens × per-family rate.
- Rate table + family regex copied verbatim from `scripts/e2e-build-urlshortener.sh` (the watchdog block) — keep one source of truth in `orchestra/cost.py` and have both `poll.py` and the watchdog import it.
- New column inserted between `turns=N` and the progress message: `$X.XX` formatted with 2-decimal precision.

Files: new `orchestra/cost.py` (~25 lines: RATES + FAMILY_RE + `cost_for(model, in_tok, out_tok)`), `orchestra/poll.py` (render_snapshot uses it), `tests/test_cost.py` (new), `tests/test_poll.py` (extend existing snapshot tests for the new column).

### 3. `orchestra mission lint <mission.md>` pre-flight check (closes #30)

Today: typos in mission files surface at minute 3 when a spawn fails. Add a static check.

- Parse any JSONL blocks inside the mission markdown (looking for the `spawn-batch` pattern: a triple-backtick `jsonl` fence followed by JSON-per-line).
- For each spec: verify brief path exists (relative to the mission file's directory), verify role file resolves (override → bundled fallback), verify worktree names are unique.
- Body checks: there's an `## ACCEPTANCE` (or `## VERIFIER`) header with at least one bash code fence under it.
- Warnings (non-fatal): no `## TEAM` heading; mission body doesn't mention `worker_done` (suggests the PM may not know how to terminate).
- Output: `error:` / `warning:` prefixed lines. Exit 0 if no errors, 2 otherwise.

Files: new `orchestra/mission_lint.py`, `orchestra/cli.py` (new command), `tests/test_mission_lint.py` (covers each hard-fail rule + at least one warning rule + a happy-path check against `examples/kanban/mission.md`).

## Out of scope (deferred)

- **Mission templating** (YAML/TOML front-matter → markdown). Bigger swing, v2.3.
- **Auto-reap of any stale `orch/*` branch.** Out of scope; v2.2 only changes reap-on-merge.
- **PM crash resume.** Still v2.1+ deferred work.
- **Per-worker wall-clock budgets.** Mentioned in notes; not pulled.
- **Mission-level forbidden:-rules engine.** Mentioned in notes; defer until we see a PM do something actually destructive.

## Backward compatibility

- `orchestra merge` reap-default is a behavior change. The `--keep` flag covers the use case of "I want to look at the worktree before reaping". Existing tests that assume worktree persistence after merge need updating (covered by the tests-update work item under #28).
- `orchestra status` adds a new column. The output is human-readable text; scripts that parse it by-column-index will need to adjust (unlikely; nothing in the repo does this).
- `orchestra mission lint` is purely new — no compat impact.

## Acceptance for v2.2

1. All v2.1 tests pass unchanged.
2. New unit tests for each of the three pieces.
3. `examples/kanban/mission.md` passes `orchestra mission lint` (sanity check).
4. CHANGELOG has a v2.2 section.

## Phasing

Single PR, three independent commits (one per issue). Three engineers in parallel via spawn-batch; no shared files (the only overlap is `orchestra/cli.py`, which all three touch — but each adds a NEW command body or extends an existing one in a separate function, so well-placed splits avoid conflicts).

**File-touch partition:**

- `reap-default`: `orchestra/cli.py` (extends existing `merge` command), `orchestra/roles/pm.md`, `tests/test_cli.py`
- `cost-column`: NEW `orchestra/cost.py`, `orchestra/poll.py`, `tests/test_cost.py`, `tests/test_poll.py`
- `mission-lint`: NEW `orchestra/mission_lint.py`, `orchestra/cli.py` (new top-level command), `tests/test_mission_lint.py`

The cli.py overlap is real. Mitigation: `reap-default` and `mission-lint` engineers must each work inside their own command function and not reorder unrelated code. The merge order at PM time should put one of them first and the other second — git's textual merge will likely succeed since the changes are non-adjacent.
