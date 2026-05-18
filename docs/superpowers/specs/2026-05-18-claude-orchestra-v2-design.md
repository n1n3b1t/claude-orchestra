# claude-orchestra v2.0 — generic role framework

**Status:** design, awaiting plan
**Date:** 2026-05-18
**Predecessors:** [v1 design](2026-05-17-claude-orchestra-v1-design.md), v1.0–v1.3 release notes in `CHANGELOG.md`

## Goal

Make claude-orchestra capable of orchestrating a complete multi-stack project
(several engineers in parallel, plus a reviewer, plus an architect) **without
the orchestra codebase encoding the project-specific flow**. The framework
should stay a thin coordinator on top of tmux + sqlite + Claude Code hooks;
the spec → architect → impl → review → QA flow that the multi-agent literature
converges on belongs in *mission files*, not in orchestra.

The proof: v2.0 builds a Trello-lite kanban app (backend + web + CLI client
+ reviewer) end-to-end via a user-authored mission file, with no
project-flow logic added to the framework.

## Non-goals (deferred)

- Recursive PMs (a worker that itself calls `orchestra spawn`).
- First-class worker DAG / `--blocked-by` dependencies. The PM coordinates
  the dependency graph by polling and `orchestra send`, as today.
- PM crash resume.
- Per-worker cost/token budget enforcement (we have the data after v1.2 #8;
  budget-driven kill is v2.1+).
- Cross-worktree inspector mode (read all engineers' worktrees in flight).
  Reviewer in v2.0 reviews post-merge in main.
- Per-role telemetry / dashboards.

These are all real future work, just not in v2.0.

## What v2.0 adds — three primitives

### 1. User-defined roles (filesystem-backed)

Today the PM and Engineer prompt templates live as Python functions in
`orchestra/role_prompts.py` (`render_pm_prompt`, `render_engineer_prompt`).
A user who wants an `architect` role has to fork the codebase. v2.0 turns
role templates into markdown files with a deterministic lookup order:

```
Highest precedence:  <project_root>/.orchestra/roles/<name>.md   (user override)
Lowest precedence:   orchestra/roles/<name>.md                    (bundled built-in)
```

`pm.md` and `engineer.md` ship as bundled built-ins, extracted verbatim
from the current Python templates, so existing missions and the v1.0
urlshortener e2e keep working unchanged.

The role file is a markdown body with `{worker_id}`, `{cwd}`, `{branch}`,
`{brief}` placeholders. `role_prompts.py` becomes a thin loader: read
file → parse front matter (see §2) → `str.format_map` the body with
known placeholders. Unknown placeholders raise (catch typos early).

`--role <name>` accepts any string. If neither the user-override nor the
bundled file exists for `<name>`, spawn fails fast with `no role
template: <name>` and exit code 2.

### 2. Per-role tool permissions (YAML front matter)

Each role file may carry a YAML front-matter block. The `permissions:`
key — if present — is merged into the worker's `.claude/settings.local.json`
at spawn time, before the tmux window is created. Claude Code respects
`permissions.allow` / `permissions.deny` from settings; orchestra just
plumbs the merge.

Example role file:

```markdown
---
permissions:
  allow:
    - Read
    - Grep
    - Glob
    - "Bash(git log:*)"
    - "Bash(git diff:*)"
    - "Bash(grep:*)"
  deny:
    - Write
    - Edit
    - "Bash(rm:*)"
---
## ROLE: Reviewer
Worker ID: {worker_id}
Workspace: {cwd}

You are a read-only reviewer. Read the merged main branch...
```

Permissions schema: orchestra does **not** validate the contents — it
trusts Claude Code as the schema authority. We just merge two arrays
(allow, deny) into existing settings via `settings_merge.ensure_perms`
(new helper extending the existing `settings_merge.ensure_hooks`).

Order of writes inside a worktree:
1. `worktree.add` creates the worktree.
2. `settings_merge.ensure_hooks` writes hooks into `<wt>/.claude/settings.local.json`
   (existing behavior).
3. `settings_merge.ensure_perms` merges the role's `permissions:` block.
4. `tmux.new_window` creates the pane.

For a no-worktree worker (e.g. a reviewer spawned in main): step 1 is
skipped; the permissions merge goes into `<cwd>/.claude/settings.local.json`.

### 3. Read-only mode = composition, not a new primitive

A "reviewer" is just:
- A role file with restrictive permissions (Read/Grep/Glob, no Write/Edit).
- Spawned without `--worktree`.

`orchestra spawn` already accepts an absent `--worktree` (the PM uses
this). v2.0 does not add a `--read-only` flag. The composition is
sufficient and keeps the framework primitive-set small.

## Backward compatibility

- v1.x missions that spawn `--role pm` or `--role engineer` work
  unchanged — the bundled `orchestra/roles/{pm,engineer}.md` reproduce
  the current Python templates verbatim.
- v1.x role-aware tests in `tests/test_role_prompts.py` keep passing
  against the new loader (the renderer signature stays the same;
  internals change).
- v1.x `.orchestra/` projects without a `roles/` directory work fine —
  the loader falls through to bundled built-ins.

## Code touchpoints

| Module / path | Change |
|---|---|
| `orchestra/roles/__init__.py` (new) | Package marker so the bundled .md files ship via setuptools |
| `orchestra/roles/pm.md` (new) | Extracted from `render_pm_prompt` |
| `orchestra/roles/engineer.md` (new) | Extracted from `render_engineer_prompt` |
| `orchestra/role_prompts.py` | Becomes a loader: `load_role(name, cwd) -> (template_body, permissions_dict)`, then format the body. Keeps the public `render_pm_prompt` / `render_engineer_prompt` shims for backward compat. |
| `orchestra/settings_merge.py` | Add `ensure_perms(settings_path, perms_dict)` that merges `allow`/`deny` arrays (dedupe, preserve existing entries). |
| `orchestra/spawn.py` | Between steps 1 (worktree) and 3 (tmux window): if role file carries permissions, call `settings_merge.ensure_perms` on the target settings file. Pass cwd (main checkout) for no-worktree workers, `worktrees/<name>` for engineers. |
| `orchestra/cli.py` | Remove the `--role` validation against `{pm, engineer}` (it is currently free-form, but no longer mention those two names in error messages). |
| `examples/kanban/mission.md` (new) | Mission text for the kanban e2e test. |
| `examples/kanban/roles/architect.md` (new) | Architect role file with write permissions on `docs/`. |
| `examples/kanban/roles/backend.md` (new) | Backend engineer role. |
| `examples/kanban/roles/web.md` (new) | Web engineer role. |
| `examples/kanban/roles/cli.md` (new) | CLI engineer role. |
| `examples/kanban/roles/reviewer.md` (new) | Reviewer role (read-only). |
| `examples/kanban/verifier.sh` (new) | Acceptance verifier for kanban. |
| `scripts/e2e-build-kanban.sh` (new) | E2E driver, mirrors `e2e-build-urlshortener.sh` shape (wall-clock + activity + cost watchdogs). |
| `tests/test_roles.py` (new) | Loader unit tests: file precedence, front-matter parsing, missing-role error, unknown-placeholder error, permissions extraction. |
| `tests/test_settings_merge.py` | Add cases for `ensure_perms`. |
| `tests/test_spawn.py` | Add a case: spawning with a role that has permissions writes the merged allow/deny arrays into settings.local.json. |
| `CHANGELOG.md` | New `## v2.0` section. |
| `README.md` | One-paragraph "Defining custom roles" section pointing at the kanban example. |
| `CLAUDE.md` | One-line note that `orchestra/roles/` exists and how lookup works. |

## How Kanban exercises the framework

The mission file (user-authored) defines a five-role team:

```
architect  → writes docs/api.yaml on main, commits, signals done
            └─ PM merges, then spawns parallel:
                 backend  ─┐
                 web      ─┼─ each rebases to pick up docs/api.yaml
                 cli      ─┘
            └─ PM merges in order
            └─ reviewer  (no worktree, read-only) reviews merged main,
                          either signs off or escalates findings
            └─ PM runs verifier; if green, worker_done
```

The shared-state problem ("how does the backend engineer see the API
contract") solves itself via convention: architect commits to main
*before* engineers spawn; engineers' worktrees inherit from main at
creation. No orchestra-level "shared workspace" primitive needed.

The reviewer pattern works because:
- Reviewer spawns with no `--worktree`, so it runs in the PM's main
  checkout (which has been fully merged by the time the reviewer starts).
- Reviewer's permissions block denies Write/Edit, so even an over-eager
  reviewer can't corrupt the main branch.
- Reviewer escalates findings via `orchestra worker escalate`; PM
  decides whether to spawn a follow-up engineer to fix or to override.

## Acceptance for v2.0

The release is "done" when **all** of:

1. All v1.x tests pass unchanged (`.venv/bin/pytest -q` shows 0 failures
   after the refactor).
2. The new `tests/test_roles.py` covers: file precedence, front-matter
   parsing, missing role, unknown placeholder, permissions extraction.
3. `examples/kanban/` plus `scripts/e2e-build-kanban.sh` produce a
   working kanban app end-to-end in one orchestrated run, with the
   verifier passing.
4. `CHANGELOG.md` has a `v2.0` section listing the three primitives and
   the kanban example.
5. The framework codebase has **no kanban-specific logic** — kanban
   lives entirely in `examples/` and `scripts/`.

## Phasing

Two PRs, both orchestrated by claude-orchestra against itself (the
dogfood pattern established in v1.2/v1.3 holds).

**PR A — v2.0-roles** (the framework changes):
- All code touchpoints under `orchestra/` and `tests/`.
- Bundled built-in roles for `pm` and `engineer`.
- No new examples.
- Verifier: `.venv/bin/pytest -q && .venv/bin/ruff check && .venv/bin/mypy ...`.

**PR B — v2.0-kanban** (the proof):
- `examples/kanban/` (mission + 5 role files + verifier).
- `scripts/e2e-build-kanban.sh`.
- The kanban e2e driven by an actual orchestra run.
- Verifier: kanban verifier produces a working app.

GitHub issues for both PRs will be filed during the planning step so each
discrete piece of work has a tracked ticket.

PR A lands first (mostly refactor + tests, fast). PR B is the public
proof that the framework holds water on a complex project.

## Risks and open questions

1. **YAML front-matter parser dependency.** Python's stdlib has no YAML
   parser. Options: add `pyyaml` (heavy, common), use `tomllib` with TOML
   front matter instead (stdlib, less idiomatic for `.md` files), or
   write a tiny custom parser. Recommend: tiny custom parser. The exact
   subset we'll support:
   - Front matter delimited by `---\n` … `\n---\n` at the very top of file.
   - Inside, a flat mapping of `key: value` lines (one level deep).
   - Values may be strings (quoted or bare), or lists rendered as
     `- item` lines under the key (each on its own line, two-space
     indent).
   - No anchors, no multi-document, no flow syntax, no nested mappings.

   That's enough to express `permissions.allow` and `permissions.deny`
   as lists of strings. Anything outside this subset raises with a
   clear "unsupported front-matter syntax at line N" error so future
   schema extensions force a deliberate parser upgrade rather than a
   silent failure.

2. **Permissions schema drift.** Claude Code may change its
   `permissions.allow`/`deny` syntax. Orchestra doesn't validate the
   contents — we just merge — so drift surfaces at runtime when the
   worker spawns and Claude Code rejects a setting. Accepted risk for
   v2.0: catch real breakage in the e2e and patch as needed. A
   validation layer would couple orchestra to a Claude Code schema we
   don't control; better to stay loose. (Future v2.1+: an
   `orchestra doctor` CLI could pre-flight known patterns, but it's
   not in scope here.)

3. **What if reviewer wants to suggest a change?** Reviewer is
   read-only. To request an edit, it escalates to PM, who can either
   spawn a follow-up engineer or override the review. This is by
   design — keeps reviewer truly read-only — but worth documenting as
   the expected workflow.

4. **Bundling `.md` files in the Python package.** Setuptools
   `package-data` needs the right config. Add `include "*.md"` under
   `[tool.setuptools.package-data]` for `orchestra.roles`.

5. **Kanban test cost.** A 5-worker run (architect + 3 engineers +
   reviewer) on opus PM + sonnet workers will be more expensive than
   the v1.2/v1.3 runs. Budget a soft ceiling in `e2e-build-kanban.sh`'s
   cost watchdog (target ≤ $5).
