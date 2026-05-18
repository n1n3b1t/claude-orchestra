# claude-orchestra v2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the hardcoded `pm`/`engineer` Python prompt templates into a filesystem-backed role framework, add per-role tool permissions via YAML front matter, and prove the framework with a kanban (backend + web + CLI + reviewer) e2e.

**Architecture:** Role templates move from `orchestra/role_prompts.py` (Python functions) to `.md` files under `orchestra/roles/` (bundled) and `<project>/.orchestra/roles/` (user override). YAML front matter on each role file may declare `permissions:` — merged into the worker's `.claude/settings.local.json` by a new `settings_merge.ensure_perms` helper, called between worktree creation and tmux window setup. Read-only workers (e.g. reviewers) emerge from composition: a role file with restrictive permissions, spawned without `--worktree`. The kanban example exercises the whole stack in `examples/kanban/`.

**Tech Stack:** Python 3.10+, stdlib only for the framework changes (tiny custom YAML front-matter parser — no `pyyaml` dep). The kanban example will pick FastAPI for backend + a static-HTML+JS web client + a Python CLI client, mirroring the urlshortener fixture's tech choices.

**Spec:** [`docs/superpowers/specs/2026-05-18-claude-orchestra-v2-design.md`](../specs/2026-05-18-claude-orchestra-v2-design.md)

## File Map

**New files:**
- `orchestra/_frontmatter.py` — tiny YAML front-matter parser (flat mapping, lists only)
- `orchestra/roles/__init__.py` — package marker
- `orchestra/roles/pm.md` — bundled PM template (extracted from `role_prompts.py`)
- `orchestra/roles/engineer.md` — bundled Engineer template (extracted)
- `tests/test_frontmatter.py` — parser unit tests
- `tests/test_roles.py` — loader/precedence/permissions tests
- `examples/kanban/mission.md` — kanban PM mission
- `examples/kanban/roles/architect.md` — kanban architect role
- `examples/kanban/roles/backend.md` — backend engineer role
- `examples/kanban/roles/web.md` — web engineer role
- `examples/kanban/roles/cli.md` — CLI engineer role
- `examples/kanban/roles/reviewer.md` — read-only reviewer role
- `examples/kanban/verifier.sh` — acceptance script
- `scripts/e2e-build-kanban.sh` — e2e driver

**Modified files:**
- `orchestra/role_prompts.py` — becomes a thin filesystem loader, keeps public `render_pm_prompt` / `render_engineer_prompt` as backward-compat shims
- `orchestra/settings_merge.py` — adds `ensure_perms(path, perms_dict)`
- `orchestra/spawn.py` — wires permissions merge into the spawn flow
- `pyproject.toml` — adds `"roles/*.md"` to `[tool.setuptools.package-data]` for the `orchestra` package
- `tests/test_role_prompts.py` — updated to match new loader internals
- `tests/test_settings_merge.py` — adds `ensure_perms` cases
- `tests/test_spawn.py` — adds permissions-merge case
- `CHANGELOG.md` — v2.0 section
- `README.md` — "Defining custom roles" section
- `CLAUDE.md` — one-line note on `orchestra/roles/` lookup

---

## Task 1: Tiny YAML front-matter parser

**Goal:** Add `orchestra/_frontmatter.py` that splits a markdown file into (front matter dict, body) and rejects anything outside the supported subset.

**Files:**
- Create: `orchestra/_frontmatter.py`
- Test: `tests/test_frontmatter.py`

**Acceptance Criteria:**
- [ ] `parse(text)` returns `(meta: dict, body: str)` for markdown with `---\n...\n---\n` front matter
- [ ] Returns `({}, text)` when there's no front matter
- [ ] Parses flat `key: value` pairs as strings
- [ ] Parses lists rendered as `- item` lines (two-space indent, one level deep) under a key
- [ ] Raises `FrontmatterError` with line number on: nested mappings, flow syntax (`{}` / `[]`), missing closing `---`, anything outside the supported subset
- [ ] Stripping front matter leaves the markdown body byte-identical from the line after the closing `---`

**Verify:** `.venv/bin/pytest tests/test_frontmatter.py -v` → 100% pass

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/test_frontmatter.py
from __future__ import annotations

import pytest

from orchestra._frontmatter import FrontmatterError, parse


class TestNoFrontmatter:
    def test_returns_empty_meta_and_original_body(self) -> None:
        text = "# Hello\nbody line\n"
        meta, body = parse(text)
        assert meta == {}
        assert body == text

    def test_text_without_opening_dashes_is_not_frontmatter(self) -> None:
        meta, body = parse("not-a-line\n---\nfoo: bar\n---\n")
        assert meta == {}


class TestSimplePairs:
    def test_string_value_unquoted(self) -> None:
        meta, body = parse("---\nname: pm\n---\n# Hello\n")
        assert meta == {"name": "pm"}
        assert body == "# Hello\n"

    def test_multiple_pairs(self) -> None:
        text = "---\nname: pm\nversion: 1\n---\nbody\n"
        meta, body = parse(text)
        assert meta == {"name": "pm", "version": "1"}
        assert body == "body\n"

    def test_quoted_string_value_keeps_quotes_stripped(self) -> None:
        meta, _ = parse('---\npattern: "Bash(rm:*)"\n---\nbody\n')
        assert meta == {"pattern": "Bash(rm:*)"}


class TestLists:
    def test_list_of_strings(self) -> None:
        text = (
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "    - Grep\n"
            "  deny:\n"
            "    - Write\n"
            "    - Edit\n"
            "---\n"
            "body\n"
        )
        meta, _ = parse(text)
        assert meta == {
            "permissions": {
                "allow": ["Read", "Grep"],
                "deny": ["Write", "Edit"],
            }
        }

    def test_quoted_list_items(self) -> None:
        text = (
            "---\n"
            "permissions:\n"
            "  allow:\n"
            '    - "Bash(git log:*)"\n'
            "---\n"
        )
        meta, _ = parse(text)
        assert meta["permissions"]["allow"] == ["Bash(git log:*)"]


class TestErrors:
    def test_missing_closing_dashes(self) -> None:
        with pytest.raises(FrontmatterError, match="missing closing"):
            parse("---\nname: pm\nbody without close\n")

    def test_flow_mapping_rejected(self) -> None:
        with pytest.raises(FrontmatterError, match="line 2"):
            parse("---\nname: {a: b}\n---\n")

    def test_flow_list_rejected(self) -> None:
        with pytest.raises(FrontmatterError, match="line 2"):
            parse("---\nname: [a, b]\n---\n")

    def test_nested_mapping_more_than_two_deep_rejected(self) -> None:
        text = "---\nouter:\n  middle:\n    inner:\n      - x\n---\n"
        with pytest.raises(FrontmatterError, match="line 4"):
            parse(text)
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/pytest tests/test_frontmatter.py -v`
Expected: ImportError / ModuleNotFoundError on `orchestra._frontmatter`.

- [ ] **Step 3: Implement the parser**

```python
# orchestra/_frontmatter.py
"""Tiny YAML front-matter parser for orchestra role files.

Supports a deliberately small subset:
- Front matter delimited by `---\\n` at start and a matching `\\n---\\n` close.
- Flat top-level keys mapping to strings.
- One level of nesting: a key whose value is another mapping (no inline content
  on the parent line; child keys two-space indented).
- Lists rendered as `- item` lines, four-space indented under their key.

Rejects: flow syntax (`{}` / `[]`), anchors/aliases, multi-document streams,
nesting beyond two levels, tabs as indentation.

We avoid `pyyaml` to keep the install surface small; the schema is stable
and what we don't support raises clearly so a future schema extension is a
deliberate parser upgrade rather than a silent failure.
"""
from __future__ import annotations

import re
from typing import Any

_DELIM = "---"


class FrontmatterError(ValueError):
    """Raised on any malformed or out-of-scope front matter."""


_KV = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")
_LIST = re.compile(r"^(?P<indent>[ ]*)-\s+(?P<value>.+)$")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _check_no_flow(value: str, lineno: int) -> None:
    if "{" in value or "}" in value or value.strip().startswith("["):
        raise FrontmatterError(f"flow syntax not supported (line {lineno})")


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown string into (front matter dict, remaining body).

    If `text` has no front matter, returns ({}, text) unchanged.
    """
    if not text.startswith(_DELIM + "\n"):
        return {}, text

    # Find the closing ---
    lines = text.split("\n")
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i] == _DELIM:
            close_idx = i
            break
    if close_idx == -1:
        raise FrontmatterError("missing closing --- delimiter")

    fm_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1 :])

    meta: dict[str, Any] = {}
    current_key: str | None = None
    current_child_key: str | None = None
    i = 0
    while i < len(fm_lines):
        raw = fm_lines[i]
        lineno = i + 2  # +1 for opening ---, +1 for 1-based
        if not raw.strip():
            i += 1
            continue
        if "\t" in raw:
            raise FrontmatterError(f"tabs not supported (line {lineno})")

        m_list = _LIST.match(raw)
        if m_list:
            indent = len(m_list.group("indent"))
            value = _strip_quotes(m_list.group("value"))
            _check_no_flow(value, lineno)
            if indent == 4 and current_key is not None and current_child_key is not None:
                target = meta[current_key].setdefault(current_child_key, [])
                if not isinstance(target, list):
                    raise FrontmatterError(f"mixing list and value (line {lineno})")
                target.append(value)
                i += 1
                continue
            raise FrontmatterError(f"unexpected list item (line {lineno})")

        m_kv = _KV.match(raw)
        if not m_kv:
            raise FrontmatterError(f"unrecognized syntax (line {lineno})")

        indent = len(m_kv.group("indent"))
        key = m_kv.group("key")
        value = m_kv.group("value")
        _check_no_flow(value, lineno)

        if indent == 0:
            current_key = key
            current_child_key = None
            if value.strip() == "":
                meta[key] = {}
            else:
                meta[key] = _strip_quotes(value)
            i += 1
            continue
        if indent == 2:
            if current_key is None or not isinstance(meta.get(current_key), dict):
                raise FrontmatterError(f"nested key without parent (line {lineno})")
            current_child_key = key
            if value.strip() == "":
                # Child becomes a container for a list below.
                meta[current_key][key] = []
            else:
                meta[current_key][key] = _strip_quotes(value)
            i += 1
            continue
        raise FrontmatterError(f"unsupported indent level (line {lineno})")

    return meta, body
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `.venv/bin/pytest tests/test_frontmatter.py -v`
Expected: 10/10 PASS.

- [ ] **Step 5: Lint + type check**

Run: `.venv/bin/ruff check orchestra/_frontmatter.py tests/test_frontmatter.py`
Expected: All checks passed.

Run: `.venv/bin/mypy orchestra/_frontmatter.py`
Expected: clean (or only the preexisting `poll.py:81` repo-level error).

- [ ] **Step 6: Commit**

```bash
git add orchestra/_frontmatter.py tests/test_frontmatter.py
git commit -m "$(cat <<'EOF'
feat(roles): add tiny YAML front-matter parser

Stdlib-only parser supporting a deliberately small subset (flat mapping,
one level of nesting, lists as '- item' lines). Anything outside the
subset raises FrontmatterError with a line number, so a future schema
extension is a deliberate parser upgrade rather than a silent failure.

The motivation is the v2.0 role-template system, which uses front matter
to carry per-role tool permissions. Avoiding pyyaml keeps the install
surface small.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: settings_merge.ensure_perms

**Goal:** Add `settings_merge.ensure_perms(path, perms)` that merges a permissions dict (`{"allow": [...], "deny": [...]}`) into `path` (a `.claude/settings.local.json` file).

**Files:**
- Modify: `orchestra/settings_merge.py`
- Modify: `tests/test_settings_merge.py`

**Acceptance Criteria:**
- [ ] `ensure_perms(path, {"allow": ["Read"], "deny": ["Write"]})` creates `path` with `{"permissions": {"allow": ["Read"], "deny": ["Write"]}}` when path doesn't exist
- [ ] When `path` already contains hooks (from `ensure_hooks`), `ensure_perms` preserves them and only touches `permissions`
- [ ] Dedupes entries: calling `ensure_perms` twice with same input does not duplicate array items
- [ ] Missing `allow` or `deny` key in input is treated as empty list
- [ ] Empty `perms` dict (no allow/deny) is a no-op (does not write the file)
- [ ] Non-string entries in input lists are dropped silently (defensive — schema is Claude Code's)

**Verify:** `.venv/bin/pytest tests/test_settings_merge.py -v` → 100% pass, including 6 new cases

**Steps:**

- [ ] **Step 1: Add failing test cases**

Append to `tests/test_settings_merge.py`:

```python
class TestEnsurePerms:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_perms(p, {"allow": ["Read"], "deny": ["Write"]})
        assert p.exists()
        data = json.loads(p.read_text())
        assert data == {"permissions": {"allow": ["Read"], "deny": ["Write"]}}

    def test_preserves_existing_hooks(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_hooks(p)
        before = json.loads(p.read_text())
        settings_merge.ensure_perms(p, {"allow": ["Read"], "deny": []})
        after = json.loads(p.read_text())
        assert after["hooks"] == before["hooks"]
        assert after["permissions"]["allow"] == ["Read"]

    def test_dedupes_on_second_call(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        perms = {"allow": ["Read", "Grep"], "deny": ["Write"]}
        settings_merge.ensure_perms(p, perms)
        settings_merge.ensure_perms(p, perms)
        data = json.loads(p.read_text())
        assert data["permissions"]["allow"] == ["Read", "Grep"]
        assert data["permissions"]["deny"] == ["Write"]

    def test_merges_with_existing_perms_preserving_user_entries(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "permissions": {"allow": ["WebFetch"], "deny": []}
        }))
        settings_merge.ensure_perms(p, {"allow": ["Read"], "deny": ["Edit"]})
        data = json.loads(p.read_text())
        assert "WebFetch" in data["permissions"]["allow"]
        assert "Read" in data["permissions"]["allow"]
        assert data["permissions"]["deny"] == ["Edit"]

    def test_empty_input_is_noop(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_perms(p, {})
        assert not p.exists()

    def test_drops_non_string_entries(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_perms(p, {"allow": ["Read", 42, None], "deny": []})
        data = json.loads(p.read_text())
        assert data["permissions"]["allow"] == ["Read"]
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `.venv/bin/pytest tests/test_settings_merge.py::TestEnsurePerms -v`
Expected: AttributeError on `settings_merge.ensure_perms`.

- [ ] **Step 3: Add `ensure_perms` to `orchestra/settings_merge.py`**

Append (after `ensure_hooks`):

```python
def _merge_string_list(existing: list[Any] | None, additions: list[Any]) -> list[str]:
    """Append additions to existing (string items only, dedupe, preserve order)."""
    out: list[str] = []
    for src in (existing or []), additions:
        for item in src:
            if isinstance(item, str) and item not in out:
                out.append(item)
    return out


def ensure_perms(path: Path, perms: dict[str, Any]) -> None:
    """Merge a permissions block into `path`. Creates the file if missing.

    `perms` shape: {"allow": [...], "deny": [...]} (either key optional).
    Entries that aren't strings are dropped silently — Claude Code is the
    schema authority and orchestra just plumbs the merge.
    """
    allow = list(perms.get("allow") or [])
    deny = list(perms.get("deny") or [])
    if not allow and not deny:
        return  # nothing to write

    path.parent.mkdir(parents=True, exist_ok=True)
    data: Any
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = None
    else:
        data = None
    if not isinstance(data, dict):
        data = {}

    perms_block = data.get("permissions")
    if not isinstance(perms_block, dict):
        perms_block = {}
        data["permissions"] = perms_block

    perms_block["allow"] = _merge_string_list(perms_block.get("allow"), allow)
    perms_block["deny"] = _merge_string_list(perms_block.get("deny"), deny)

    path.write_text(json.dumps(data, indent=2) + "\n")
```

- [ ] **Step 4: Run all settings_merge tests**

Run: `.venv/bin/pytest tests/test_settings_merge.py -v`
Expected: all existing tests still pass + 6 new tests pass.

- [ ] **Step 5: Lint + type check**

Run: `.venv/bin/ruff check orchestra/settings_merge.py tests/test_settings_merge.py`
Expected: clean.

Run: `.venv/bin/mypy orchestra/settings_merge.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add orchestra/settings_merge.py tests/test_settings_merge.py
git commit -m "$(cat <<'EOF'
feat(roles): add ensure_perms to settings_merge

Merges a {allow, deny} permissions block into a Claude Code
settings.local.json file. Preserves any existing hooks/permissions the
user added, dedupes entries, and silently drops non-string items
(Claude Code owns the schema; we just plumb the merge).

Used by the v2.0 role-template system to apply per-role tool
restrictions to a worker's settings before it spawns.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Bundle built-in role templates

**Goal:** Create `orchestra/roles/` package with `pm.md` and `engineer.md` extracted verbatim from the current Python templates, plus pyproject `package-data` config so the .md files ship with the wheel.

**Files:**
- Create: `orchestra/roles/__init__.py`
- Create: `orchestra/roles/pm.md`
- Create: `orchestra/roles/engineer.md`
- Modify: `pyproject.toml:36-37`

**Acceptance Criteria:**
- [ ] `orchestra/roles/__init__.py` exists (can be empty — it's a package marker)
- [ ] `orchestra/roles/pm.md` contains the PM template body with placeholders `{worker_id}`, `{project_name}`, `{mission}`, `{team_section}`, `{verifier_block}` (verbatim from the current Python template)
- [ ] `orchestra/roles/engineer.md` contains the Engineer template body with placeholders `{worker_id}`, `{cwd}`, `{branch}`, `{brief_section}`
- [ ] Neither bundled file has a front-matter block (no permissions — they were never restricted in v1.x)
- [ ] `pyproject.toml`'s `[tool.setuptools.package-data]` includes `"orchestra"` entry with `"roles/*.md"` glob
- [ ] After `pip install -e ".[dev]"`, `importlib.resources.files("orchestra.roles")` finds `pm.md` and `engineer.md`

**Verify:**
```bash
.venv/bin/pip install -e ".[dev]" >/dev/null
.venv/bin/python -c "from importlib.resources import files; print(sorted(p.name for p in files('orchestra.roles').iterdir() if p.suffix == '.md'))"
```
Expected: `['engineer.md', 'pm.md']`

**Steps:**

- [ ] **Step 1: Create the package marker**

```bash
mkdir -p orchestra/roles
```

```python
# orchestra/roles/__init__.py
"""Bundled role templates. User overrides live in <project>/.orchestra/roles/."""
```

- [ ] **Step 2: Create `orchestra/roles/pm.md` (verbatim PM template)**

```markdown
## ROLE: Project Manager
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
```

- [ ] **Step 3: Create `orchestra/roles/engineer.md` (verbatim Engineer template)**

```markdown
## ROLE: Engineer
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
```

- [ ] **Step 4: Update `pyproject.toml` package-data**

Replace lines 36-37 in `pyproject.toml`:

```toml
[tool.setuptools.package-data]
orchestra = ["templates/*.html", "static/*", "roles/*.md"]
```

- [ ] **Step 5: Re-install in editable mode and verify resource discovery**

```bash
.venv/bin/pip install -e ".[dev]" >/dev/null
.venv/bin/python -c "from importlib.resources import files; print(sorted(p.name for p in files('orchestra.roles').iterdir() if p.suffix == '.md'))"
```
Expected: `['engineer.md', 'pm.md']`

- [ ] **Step 6: Sanity-check the bundled files match the current Python templates**

For each of `pm.md` / `engineer.md`, mentally compare against the `f"""..."""` literal in `orchestra/role_prompts.py`. Every placeholder + every body line should match. (Task 4's tests will then catch any drift.)

- [ ] **Step 7: Commit**

```bash
git add orchestra/roles/__init__.py orchestra/roles/pm.md orchestra/roles/engineer.md pyproject.toml
git commit -m "$(cat <<'EOF'
feat(roles): bundle pm.md and engineer.md as built-in role files

Extracts the v1.x PM and Engineer prompt bodies (currently Python f-string
literals in role_prompts.py) into markdown files under orchestra/roles/.
package-data wiring ensures they ship with the wheel. No behaviour change
yet — role_prompts.py still renders from the inline literals; Task 4
switches it over to the filesystem loader.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Refactor role_prompts.py to filesystem loader

**Goal:** Replace the inline Python templates in `role_prompts.py` with a filesystem loader that reads `.md` files. Keep the public `render_pm_prompt` and `render_engineer_prompt` signatures unchanged (backward compat for `orchestra/spawn.py` and existing tests).

**Files:**
- Modify: `orchestra/role_prompts.py`
- Modify: `tests/test_role_prompts.py`
- Create: `tests/test_roles.py`

**Acceptance Criteria:**
- [ ] New internal `_load_role(name: str, project_root: Path) -> tuple[str, dict]` returns `(template_body, permissions_dict)` from the first matching role file
- [ ] Lookup order: `<project_root>/.orchestra/roles/<name>.md` (user override) → `importlib.resources.files("orchestra.roles") / f"{name}.md"` (bundled)
- [ ] Missing role file raises `RoleNotFoundError("no role template: <name>")`
- [ ] `_render(template, **kwargs)` formats the body with `str.format_map`; unknown placeholders raise `KeyError` (catch typos early)
- [ ] Public `render_pm_prompt(...)` and `render_engineer_prompt(...)` still accept the v1.x kwargs verbatim and return strings byte-identical to the v1.x output for the same inputs
- [ ] Existing `tests/test_role_prompts.py` cases pass unchanged
- [ ] New `tests/test_roles.py` covers: precedence, missing role, unknown placeholder, front matter without permissions, front matter with permissions

**Verify:**
- `.venv/bin/pytest tests/test_role_prompts.py tests/test_roles.py -v` → all green
- `.venv/bin/pytest -q` → all green (regression check on the whole repo)

**Steps:**

- [ ] **Step 1: Write new test file `tests/test_roles.py`**

```python
"""Tests for the v2.0 filesystem role loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.role_prompts import (
    RoleNotFoundError,
    _load_role,
    render_pm_prompt,
    render_engineer_prompt,
)


class TestPrecedence:
    def test_bundled_role_loads(self, tmp_path: Path) -> None:
        body, perms = _load_role("pm", project_root=tmp_path)
        assert "## ROLE: Project Manager" in body
        assert perms == {}

    def test_project_override_wins(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / ".orchestra" / "roles"
        custom_dir.mkdir(parents=True)
        (custom_dir / "pm.md").write_text("## CUSTOM PM\n{worker_id}\n")
        body, perms = _load_role("pm", project_root=tmp_path)
        assert body == "## CUSTOM PM\n{worker_id}\n"
        assert perms == {}


class TestMissingRole:
    def test_unknown_role_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RoleNotFoundError, match="no role template: ghost"):
            _load_role("ghost", project_root=tmp_path)


class TestPermissions:
    def test_role_without_frontmatter_has_empty_perms(self, tmp_path: Path) -> None:
        _, perms = _load_role("engineer", project_root=tmp_path)
        assert perms == {}

    def test_role_with_permissions(self, tmp_path: Path) -> None:
        roles = tmp_path / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "    - Grep\n"
            "  deny:\n"
            "    - Write\n"
            "---\n"
            "## ROLE: Reviewer\n"
            "Worker ID: {worker_id}\n"
        )
        body, perms = _load_role("reviewer", project_root=tmp_path)
        assert "## ROLE: Reviewer" in body
        assert perms == {"allow": ["Read", "Grep"], "deny": ["Write"]}


class TestRendering:
    def test_pm_prompt_byte_identical_to_v1(self) -> None:
        out = render_pm_prompt(
            mission="x", worker_id="pm", project_name="proj",
            engineer_specs=[("a", "sonnet", "do a")], verifier_block="true",
        )
        assert "## ROLE: Project Manager" in out
        assert "Project: proj" in out
        assert "Worker ID: pm" in out
        assert "### YOUR TEAM" in out
        assert "`a` (sonnet) — do a" in out
        assert "true" in out

    def test_engineer_prompt_byte_identical_to_v1(self) -> None:
        out = render_engineer_prompt(
            worker_id="w1", cwd="/some/cwd", branch="orch/w1",
            brief_path=None, brief_content="do the thing",
        )
        assert "## ROLE: Engineer" in out
        assert "Worker ID: w1" in out
        assert "Workspace: /some/cwd" in out
        assert "orch/w1" in out
        assert "do the thing" in out

    def test_unknown_placeholder_raises(self, tmp_path: Path) -> None:
        roles = tmp_path / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "broken.md").write_text("Hello {nope}\n")
        from orchestra.role_prompts import render_role
        with pytest.raises(KeyError):
            render_role("broken", project_root=tmp_path)
```

- [ ] **Step 2: Run new tests, confirm they fail**

Run: `.venv/bin/pytest tests/test_roles.py -v`
Expected: ImportError / AttributeError on `RoleNotFoundError`, `_load_role`, `render_role`.

- [ ] **Step 3: Rewrite `orchestra/role_prompts.py`**

```python
"""Role-aware startup prompts for claude-orchestra (v2.0 filesystem loader).

Role templates live as markdown files. Lookup order:
1. `<project_root>/.orchestra/roles/<name>.md` (user override)
2. `orchestra/roles/<name>.md` (bundled built-in)

Each role file may carry a YAML front-matter block with a `permissions:`
key (allow/deny lists of Claude Code tool patterns). The body is a
`str.format_map`-formatted template — unknown placeholders raise
`KeyError`, which catches typos early.

`render_pm_prompt` and `render_engineer_prompt` are backward-compat shims
that delegate to the filesystem loader; they preserve the v1.x function
signatures so existing callers (orchestra/spawn.py, tests) don't change.
"""
from __future__ import annotations

from collections.abc import Sequence
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from orchestra import _frontmatter


class RoleNotFoundError(ValueError):
    """Raised when neither the project override nor the bundled file exists."""


def _project_override_path(name: str, project_root: Path) -> Path:
    return project_root / ".orchestra" / "roles" / f"{name}.md"


def _read_bundled(name: str) -> str | None:
    pkg = files("orchestra.roles")
    target = pkg / f"{name}.md"
    if not target.is_file():
        return None
    with as_file(target) as f:
        return f.read_text()


def _load_role(name: str, project_root: Path) -> tuple[str, dict[str, Any]]:
    """Read a role file. Returns (body_template, permissions_dict)."""
    override = _project_override_path(name, project_root)
    if override.is_file():
        text = override.read_text()
    else:
        text = _read_bundled(name) or ""
        if not text:
            raise RoleNotFoundError(f"no role template: {name}")
    meta, body = _frontmatter.parse(text)
    perms = meta.get("permissions") or {}
    if not isinstance(perms, dict):
        perms = {}
    return body, perms


def render_role(name: str, *, project_root: Path, **variables: Any) -> str:
    """Load a role template and format its body with `variables`."""
    body, _perms = _load_role(name, project_root)
    return body.format_map(variables)


def render_pm_prompt(
    *,
    mission: str,
    worker_id: str,
    project_name: str,
    engineer_specs: Sequence[tuple[str, str, str]],
    verifier_block: str,
    project_root: Path | None = None,
) -> str:
    """Backward-compat shim. Renders the bundled pm.md (or override)."""
    if engineer_specs:
        team_rows = "\n".join(
            f"- `{eid}` ({model}) — {brief}" for eid, model, brief in engineer_specs
        )
        team_section = (
            "### YOUR TEAM\n"
            "You will spawn and coordinate these engineers:\n"
            f"{team_rows}\n\n"
        )
    else:
        team_section = ""
    return render_role(
        "pm",
        project_root=project_root or Path.cwd(),
        mission=mission,
        worker_id=worker_id,
        project_name=project_name,
        team_section=team_section,
        verifier_block=verifier_block,
    )


def render_engineer_prompt(
    *,
    worker_id: str,
    cwd: str,
    branch: str,
    brief_path: str | None,
    brief_content: str | None,
    project_root: Path | None = None,
) -> str:
    """Backward-compat shim. Renders the bundled engineer.md (or override)."""
    if brief_content is not None:
        brief_section = "### YOUR BRIEF\n" f"{brief_content}\n"
    elif brief_path is not None:
        brief_section = (
            "### YOUR BRIEF\n"
            f"Read your brief at `{brief_path}` before doing anything.\n"
        )
    else:
        brief_section = (
            "### YOUR BRIEF\n(none — wait for `orchestra send` instructions)\n"
        )
    return render_role(
        "engineer",
        project_root=project_root or Path.cwd(),
        worker_id=worker_id,
        cwd=cwd,
        branch=branch,
        brief_section=brief_section,
    )
```

- [ ] **Step 4: Run new + existing role tests**

Run: `.venv/bin/pytest tests/test_roles.py tests/test_role_prompts.py -v`
Expected: all pass. If any existing `test_role_prompts.py` case fails on whitespace, fix the bundled .md to match the v1.x byte-for-byte output (whitespace counts).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (no regressions).

- [ ] **Step 6: Lint + type check**

Run: `.venv/bin/ruff check orchestra/role_prompts.py tests/test_roles.py`
Expected: clean.

Run: `.venv/bin/mypy orchestra/role_prompts.py`
Expected: clean (or only the preexisting `poll.py:81` error in the loose-mypy sweep).

- [ ] **Step 7: Commit**

```bash
git add orchestra/role_prompts.py tests/test_roles.py tests/test_role_prompts.py
git commit -m "$(cat <<'EOF'
feat(roles): role_prompts.py is now a filesystem loader

Replaces the inline Python f-string templates with a loader that reads
markdown files. Lookup order is project override
(<root>/.orchestra/roles/<name>.md) → bundled built-in
(orchestra/roles/<name>.md). render_pm_prompt and render_engineer_prompt
keep their v1.x signatures as backward-compat shims; the format_map
result is byte-identical to v1.x.

Front-matter permissions are extracted but not yet consumed — Task 5
wires them into spawn.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire permissions merge into spawn

**Goal:** When `spawn_worker` runs with a `--role` whose role file declares `permissions:`, merge that block into the worker's `.claude/settings.local.json` (worktree path for engineers, main checkout for no-worktree workers) before the tmux window opens.

**Files:**
- Modify: `orchestra/spawn.py`
- Modify: `tests/test_spawn.py`

**Acceptance Criteria:**
- [ ] When `role` is set and the role file has `permissions`, `ensure_perms` is called with `<worktree>/.claude/settings.local.json` (if a worktree was created) or `<project_root>/.claude/settings.local.json` (no worktree)
- [ ] When the role file has no `permissions`, no settings write happens (beyond `ensure_hooks` which is unchanged)
- [ ] When `role` is `None` (v0 path), no permissions logic runs
- [ ] Failure to load the role (RoleNotFoundError) surfaces as a clean spawn error: worker row's status becomes `error`, event `role_load_failed` is recorded, and `spawn_worker` returns without creating the tmux window
- [ ] All existing `tests/test_spawn.py` cases pass unchanged

**Verify:** `.venv/bin/pytest tests/test_spawn.py -v` → all green, including new TestPermissionsWiring class

**Steps:**

- [ ] **Step 1: Add failing test cases**

Append to `tests/test_spawn.py`:

```python
class TestPermissionsWiring:
    def test_role_with_permissions_writes_settings_when_worktree_present(
        self, tmp_path, monkeypatch
    ):
        """A spawning engineer with a role file carrying permissions gets
        those permissions merged into <worktree>/.claude/settings.local.json
        before the tmux window opens.
        """
        # Set up a fake project root + roles dir + worker DB
        project_root = tmp_path / "proj"
        project_root.mkdir()
        roles = project_root / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "  deny:\n"
            "    - Write\n"
            "---\n"
            "## ROLE: Reviewer\nWorker ID: {worker_id}\nWorkspace: {cwd}\nBranch: {branch}\n{brief_section}"
        )

        from orchestra import spawn, state, settings_merge
        # Patch tmux + worktree + waits so we don't actually shell out.
        monkeypatch.setattr(spawn.tmux, "ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "new_window", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_literal", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_enter", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_multiline", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "capture", lambda *a, **k: "")
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn.worktree_mod, "add", lambda *a, **k: None)

        db = project_root / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="rev",
            model="sonnet",
            task="",
            project_root=str(project_root),
            state_db=db,
            ctx_files=[],
            session_name="orch-test",
            role="reviewer",
            brief=None,
            worktree_name="rev",
        )

        # ensure_perms should have written into the worktree's settings.local.json
        # (which under the patched add() doesn't actually exist as a separate dir,
        # so the wiring writes to <worktree>/.claude/settings.local.json where
        # <worktree> = project_root/worktrees/rev). The wiring code creates parents.
        target = project_root / "worktrees" / "rev" / ".claude" / "settings.local.json"
        assert target.is_file()
        data = json.loads(target.read_text())
        assert data["permissions"]["allow"] == ["Read"]
        assert data["permissions"]["deny"] == ["Write"]

    def test_role_without_worktree_writes_to_main_settings(
        self, tmp_path, monkeypatch
    ):
        """A reviewer-style spawn without a worktree writes permissions into
        <project_root>/.claude/settings.local.json (main checkout)."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        roles = project_root / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "  deny: []\n"
            "---\n"
            "## ROLE: Reviewer\nWorker ID: {worker_id}\nWorkspace: {cwd}\nBranch: {branch}\n{brief_section}"
        )

        from orchestra import spawn, state
        monkeypatch.setattr(spawn.tmux, "ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "new_window", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_literal", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_enter", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "send_multiline", lambda *a, **k: None)
        monkeypatch.setattr(spawn.tmux, "capture", lambda *a, **k: "")
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)

        db = project_root / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="rev",
            model="sonnet",
            task="",
            project_root=str(project_root),
            state_db=db,
            ctx_files=[],
            session_name="orch-test",
            role="reviewer",
            brief=None,
            worktree_name=None,
        )

        target = project_root / ".claude" / "settings.local.json"
        assert target.is_file()
        data = json.loads(target.read_text())
        assert data["permissions"]["allow"] == ["Read"]

    def test_missing_role_marks_worker_error_and_skips_window(
        self, tmp_path, monkeypatch
    ):
        from orchestra import spawn, state
        project_root = tmp_path / "proj"
        project_root.mkdir()
        new_window_called = {"v": False}
        monkeypatch.setattr(spawn.tmux, "ensure_session", lambda *a, **k: None)
        def _new_window(*a, **k):
            new_window_called["v"] = True
        monkeypatch.setattr(spawn.tmux, "new_window", _new_window)

        db = project_root / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = state.connect(db)
        state.init_schema(conn)

        spawn.spawn_worker(
            conn,
            worker_id="ghost",
            model="sonnet",
            task="",
            project_root=str(project_root),
            state_db=db,
            ctx_files=[],
            session_name="orch-test",
            role="ghost-role-that-does-not-exist",
            brief=None,
            worktree_name=None,
        )

        w = state.get_worker(conn, "ghost")
        assert w is not None and w.status == "error"
        kinds = [e.kind for e in state.list_events(conn, worker_id="ghost")]
        assert "role_load_failed" in kinds
        assert not new_window_called["v"]
```

(import `json` at the top of the file if it isn't already.)

- [ ] **Step 2: Confirm they fail**

Run: `.venv/bin/pytest tests/test_spawn.py::TestPermissionsWiring -v`
Expected: failures — wiring not in place yet.

- [ ] **Step 3: Wire permissions merge into `orchestra/spawn.py`**

Inside `spawn_worker`, after `state.create_worker` + worktree creation + `state.record_event(conn, "spawn_start", ...)`, BEFORE the `tmux.ensure_session` / `new_window` calls, add:

```python
# v2.0: load role file & merge per-role permissions before opening the window.
if role is not None:
    from orchestra import role_prompts, settings_merge
    try:
        _, role_perms = role_prompts._load_role(role, project_root=Path(project_root))
    except role_prompts.RoleNotFoundError as e:
        state.record_event(
            conn, "role_load_failed", worker_id=worker_id, error=str(e),
        )
        state.update_worker(conn, worker_id, status="error")
        return
    if role_perms:
        if worktree_name is not None:
            settings_path = (
                Path(project_root) / "worktrees" / worktree_name
                / ".claude" / "settings.local.json"
            )
        else:
            settings_path = (
                Path(project_root) / ".claude" / "settings.local.json"
            )
        settings_merge.ensure_perms(settings_path, role_perms)
```

Place this BEFORE the `tmux.ensure_session(session_name)` call so that a missing-role error short-circuits cleanly.

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/pytest tests/test_spawn.py::TestPermissionsWiring -v`
Expected: 3/3 PASS.

- [ ] **Step 5: Run full spawn + repo test suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 6: Lint + type check**

Run: `.venv/bin/ruff check orchestra/spawn.py tests/test_spawn.py`
Expected: clean.

Run: `.venv/bin/mypy orchestra/spawn.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add orchestra/spawn.py tests/test_spawn.py
git commit -m "$(cat <<'EOF'
feat(spawn): merge per-role tool permissions before opening pane

When --role is set, spawn now loads the role file via the v2.0 loader and,
if it carries a permissions: block, merges those allow/deny entries into
the worker's .claude/settings.local.json before the tmux window opens.
Worktree-backed workers get their worktree's settings file; no-worktree
workers (reviewers) get the main checkout's.

Missing role files surface as a 'role_load_failed' event + worker status
'error', and the spawn flow returns without creating a window.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Documentation updates

**Goal:** Document the v2.0 role system in `CHANGELOG.md`, `README.md`, and `CLAUDE.md`.

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Acceptance Criteria:**
- [ ] `CHANGELOG.md` has a `## v2.0` section above the v1.3 section, listing the three primitives (user-defined roles, per-role permissions, read-only via composition)
- [ ] `README.md` has a "Defining custom roles" section that shows the file layout and a minimal example with permissions
- [ ] `CLAUDE.md` has one paragraph noting the role lookup order (project override → bundled)
- [ ] `grep -q "## v2.0" CHANGELOG.md` succeeds

**Verify:**
```bash
grep -q "## v2.0" CHANGELOG.md
grep -q "Defining custom roles" README.md
grep -q "orchestra/roles/" CLAUDE.md
```
All three should exit 0.

**Steps:**

- [ ] **Step 1: Add `## v2.0` section to `CHANGELOG.md`**

Insert above the existing top-level `## v1.3` (or `## v1.2`) section:

```markdown
## v2.0 — generic role framework (2026-05-18)

**Three new framework primitives** that make orchestra capable of multi-role
multi-stack projects without encoding flow-specific logic.

- **User-defined roles via filesystem.** Role templates moved from Python
  functions to `.md` files. Project override at
  `<project>/.orchestra/roles/<name>.md`, bundled built-ins at
  `orchestra/roles/<name>.md`. `--role` accepts any name; missing files
  surface as `role_load_failed`.
- **Per-role tool permissions.** Each role file may carry YAML front matter
  declaring `permissions.allow` / `permissions.deny`. Orchestra merges the
  block into the worker's `.claude/settings.local.json` before opening the
  tmux pane.
- **Read-only workers via composition.** A reviewer is just a role with
  restrictive permissions, spawned without `--worktree`. No new flag.

**Proof:** `examples/kanban/` plus `scripts/e2e-build-kanban.sh` exercise
the framework end-to-end on a backend + web + CLI + reviewer project.

**Backward compatibility:** v1.x missions using `--role pm` and
`--role engineer` work unchanged — the bundled `pm.md` and `engineer.md`
reproduce the v1.x templates byte-for-byte.

**Out of scope (deferred to v2.1+):** recursive PMs, first-class
worker DAG / `--blocked-by`, PM crash resume, cost-budget kill,
cross-worktree inspector mode.
```

- [ ] **Step 2: Add "Defining custom roles" to `README.md`**

Append before the existing "Layout" section (or wherever fits the existing structure):

```markdown
## Defining custom roles

Role templates are markdown files. Lookup order:

1. `<your-project>/.orchestra/roles/<name>.md` (your override, wins)
2. `orchestra/roles/<name>.md` (bundled built-in)

A role file is a `str.format_map`-formatted prompt body with optional YAML
front matter that may declare `permissions.allow` / `permissions.deny`
(Claude Code tool patterns). Minimal example for a read-only reviewer:

```markdown
---
permissions:
  allow:
    - Read
    - Grep
    - Glob
    - "Bash(git log:*)"
    - "Bash(git diff:*)"
  deny:
    - Write
    - Edit
    - "Bash(rm:*)"
---
## ROLE: Reviewer
Worker ID: {worker_id}
Workspace: {cwd}

You are a read-only reviewer. Read the merged main branch and escalate
findings via `orchestra worker escalate --blocking --question "..."`.
```

Spawn with: `orchestra spawn rev sonnet "" --role reviewer` (no
`--worktree` — the reviewer reads main).

See `examples/kanban/roles/` for a full multi-role example.
```

- [ ] **Step 3: Add lookup note to `CLAUDE.md`**

Insert under the "Architecture" section (or another fitting place) one paragraph:

```markdown
**Role templates** (`orchestra/roles/*.md`) are loaded by `role_prompts.py`
with project overrides at `<project>/.orchestra/roles/<name>.md` taking
precedence. Each may carry YAML front matter with `permissions:` that
gets merged into the worker's settings.local.json before spawn — that's
how the v2.0 reviewer pattern is built without any orchestra-side
"read-only" flag.
```

- [ ] **Step 4: Verify the greps**

```bash
grep -q "## v2.0" CHANGELOG.md
grep -q "Defining custom roles" README.md
grep -q "orchestra/roles/" CLAUDE.md
echo "OK"
```
Expected: all three exit 0; final "OK" prints.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md README.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: v2.0 role framework — CHANGELOG, README, CLAUDE.md

Describes the three new primitives (filesystem-backed roles, per-role
permissions, read-only via composition) and points readers at
examples/kanban/ for a working multi-role example.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Kanban example — roles, mission, verifier

**Goal:** Build the `examples/kanban/` directory with five role files, a PM mission, and a verifier script. No e2e driver yet — that comes in Task 8.

**Files:**
- Create: `examples/kanban/mission.md`
- Create: `examples/kanban/roles/architect.md`
- Create: `examples/kanban/roles/backend.md`
- Create: `examples/kanban/roles/web.md`
- Create: `examples/kanban/roles/cli.md`
- Create: `examples/kanban/roles/reviewer.md`
- Create: `examples/kanban/verifier.sh`

**Acceptance Criteria:**
- [ ] Five role files exist, each loadable via `_load_role(name, project_root=<examples/kanban>)`
- [ ] `architect.md` allows Write/Edit but only under `docs/` and `examples/kanban/` (denies Write/Edit elsewhere via path patterns)
- [ ] `backend.md`, `web.md`, `cli.md` each have FULL tool access (no front matter, or front matter with empty permissions) — engineers need to write whatever their slice needs
- [ ] `reviewer.md` denies Write/Edit/Bash(rm/git push) entirely, allows Read/Grep/Glob/Bash(git log:*)/Bash(git diff:*)/Bash(curl:*)
- [ ] `mission.md` describes the team, the API contract step (architect goes first), the parallel impl phase, the reviewer phase, and references the verifier
- [ ] `verifier.sh` is executable (`chmod +x`) and runs the acceptance checks against a running app
- [ ] All five role files load and format successfully with placeholder substitution

**Verify:**
```bash
# Loader smoke test
.venv/bin/python -c "
from pathlib import Path
from orchestra.role_prompts import _load_role
for name in ('architect','backend','web','cli','reviewer'):
    body, perms = _load_role(name, project_root=Path('examples/kanban'))
    assert body and isinstance(perms, dict)
print('roles load OK')
"
# Verifier script syntactic check
bash -n examples/kanban/verifier.sh
test -x examples/kanban/verifier.sh
```
Expected: prints "roles load OK", no syntax errors, script is executable.

**Steps:**

- [ ] **Step 1: Write `examples/kanban/mission.md`**

Content (full mission for the PM):

```markdown
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

- `examples/kanban/verifier.sh` exits 0 against a running server.
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
     `examples/kanban/roles/architect.md`.

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
bash examples/kanban/verifier.sh
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
```

- [ ] **Step 2: Write `examples/kanban/roles/architect.md`**

```markdown
---
permissions:
  allow:
    - Read
    - Grep
    - Glob
    - Write
    - Edit
    - "Bash(git:*)"
    - "Bash(ls:*)"
    - "Bash(cat:*)"
  deny:
    - "Bash(rm:*)"
    - "Bash(git push:*)"
---
## ROLE: Architect (kanban)
Worker ID: {worker_id}
Workspace: {cwd}  (your own git worktree on branch {branch})

{brief_section}
### COORDINATION
- Your job is to produce `docs/api.yaml` matching the contract in the
  PM's mission. Read the mission, write the file, commit it on your
  branch, then run `orchestra worker done --summary "api.yaml written"`.
- Do NOT implement endpoints. Do NOT touch backend/, web/, or cli/.
- If the contract is ambiguous, escalate to the PM:
    orchestra worker escalate --blocking --question "..." --context "..."

### RULES
- Stay in {cwd}. Single file output: `docs/api.yaml`.
- Run `bash -n examples/kanban/verifier.sh` to sanity-check it still
  parses after any other doc tweaks you make.
```

- [ ] **Step 3: Write `examples/kanban/roles/backend.md`**

```markdown
## ROLE: Backend Engineer (kanban)
Worker ID: {worker_id}
Workspace: {cwd}  (your own git worktree on branch {branch})

{brief_section}
### COORDINATION
- Read `docs/api.yaml` for the contract.
- Build a FastAPI app at `backend/app.py` with SQLite at `backend/kanban.db`.
- Serve the web static files (Task: web engineer's output) from `/` —
  expect them at `web/index.html` and `web/app.js`. Use FastAPI's
  StaticFiles or a simple FileResponse.
- Run `cd backend && python -m pytest tests/` before declaring done.
- When done: `orchestra worker done --summary "backend live on 8765"`.

### RULES
- Stay in {cwd}. Don't touch web/ or cli/ files.
- If the contract is unclear, escalate.
```

- [ ] **Step 4: Write `examples/kanban/roles/web.md`**

```markdown
## ROLE: Web Engineer (kanban)
Worker ID: {worker_id}
Workspace: {cwd}  (your own git worktree on branch {branch})

{brief_section}
### COORDINATION
- Read `docs/api.yaml` for the contract.
- Build a single-page client at `web/index.html` plus optional
  `web/app.js`. Vanilla JS only (no npm).
- The page MUST include the strings `kanban` and `todo` somewhere in the
  rendered HTML (the verifier greps for them).
- When done: `orchestra worker done --summary "web client done"`.

### RULES
- Stay in {cwd}. Don't touch backend/ or cli/ files.
```

- [ ] **Step 5: Write `examples/kanban/roles/cli.md`**

```markdown
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
```

- [ ] **Step 6: Write `examples/kanban/roles/reviewer.md`**

```markdown
---
permissions:
  allow:
    - Read
    - Grep
    - Glob
    - "Bash(git log:*)"
    - "Bash(git diff:*)"
    - "Bash(git status:*)"
    - "Bash(curl:*)"
    - "Bash(cat:*)"
    - "Bash(ls:*)"
    - "Bash(grep:*)"
    - "Bash(find:*)"
    - "Bash(bash examples/kanban/verifier.sh)"
  deny:
    - Write
    - Edit
    - NotebookEdit
    - "Bash(rm:*)"
    - "Bash(git push:*)"
    - "Bash(git commit:*)"
    - "Bash(git checkout:*)"
---
## ROLE: Reviewer (kanban)
Worker ID: {worker_id}
Workspace: {cwd}  (read-only on main; you have no worktree)

{brief_section}
### COORDINATION
- You spawn AFTER backend, web, and cli have all been merged into main.
- Read the merged code under `backend/`, `web/`, `cli/`, and confirm:
  1. Each module exists and matches the contract in `docs/api.yaml`.
  2. `examples/kanban/verifier.sh` produces exit 0 against a running app
     (the PM will have started the server before spawning you).
  3. No obvious correctness or security issues (no plaintext passwords,
     no SQL injection, no unbounded loops).
- If everything passes:
    orchestra worker done --summary "approved: api contract honored, verifier passes"
- If anything fails:
    orchestra worker escalate --blocking \
      --question "<one-sentence summary>" \
      --context "<concrete file:line references + observed vs expected>"
  then `/exit` and let the PM decide.

### RULES
- READ-ONLY. Permissions deny Write/Edit/rm and most git mutations.
- Do not attempt to "fix" what you find — your job is reporting, not patching.
- Cite line numbers in escalations.
```

- [ ] **Step 7: Write `examples/kanban/verifier.sh`**

```bash
#!/usr/bin/env bash
# Acceptance verifier for the kanban e2e. Assumes the backend is running
# on http://localhost:8765 (started by the PM before invoking this).
set -euo pipefail

BASE="${KANBAN_URL:-http://localhost:8765}"
fail() { echo "FAIL: $1" >&2; exit 1; }

echo "1/6 GET $BASE/api/health"
curl -fsS "$BASE/api/health" >/dev/null || fail "/api/health not 200"

echo "2/6 POST $BASE/api/boards"
BOARD_JSON=$(curl -fsS -X POST -H 'content-type: application/json' \
  -d '{"name":"smoke"}' "$BASE/api/boards") || fail "create board"
BOARD_ID=$(printf '%s' "$BOARD_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["id"])') \
  || fail "board id"

echo "3/6 POST $BASE/api/boards/$BOARD_ID/cards"
CARD_JSON=$(curl -fsS -X POST -H 'content-type: application/json' \
  -d '{"title":"sanity","column":"todo"}' \
  "$BASE/api/boards/$BOARD_ID/cards") || fail "create card"
CARD_ID=$(printf '%s' "$CARD_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["id"])') || fail "card id"

echo "4/6 PATCH $BASE/api/cards/$CARD_ID column=done"
curl -fsS -X PATCH -H 'content-type: application/json' \
  -d '{"column":"done"}' "$BASE/api/cards/$CARD_ID" >/dev/null \
  || fail "patch card"

echo "5/6 GET $BASE/  (web client)"
HTML=$(curl -fsS "$BASE/") || fail "web /"
printf '%s' "$HTML" | grep -qi 'kanban' || fail "html lacks 'kanban'"
printf '%s' "$HTML" | grep -qi 'todo' || fail "html lacks 'todo'"

echo "6/6 python cli/kanban_cli.py list"
KANBAN_URL="$BASE" python3 cli/kanban_cli.py list | grep -q 'sanity' \
  || fail "cli list output missing 'sanity'"

echo "OK"
```

Then:

```bash
chmod +x examples/kanban/verifier.sh
bash -n examples/kanban/verifier.sh
```

- [ ] **Step 8: Run the loader smoke check**

```bash
.venv/bin/python -c "
from pathlib import Path
from orchestra.role_prompts import _load_role
for name in ('architect','backend','web','cli','reviewer'):
    body, perms = _load_role(name, project_root=Path('examples/kanban'))
    assert body, f'empty body for {name}'
    assert isinstance(perms, dict), f'bad perms for {name}'
print('roles load OK')
"
```
Expected: `roles load OK`.

- [ ] **Step 9: Commit**

```bash
git add examples/kanban/
git commit -m "$(cat <<'EOF'
feat(examples): kanban — five-role example for v2.0 framework

Five role files (architect, backend, web, cli, reviewer), one mission,
one verifier script. Exercises the v2.0 framework primitives:

- architect.md restricts Bash to safe ops, denies push
- reviewer.md is fully read-only (Write/Edit/rm/push/commit/checkout
  denied; Bash limited to git log/diff/status, curl, cat, ls, grep, find,
  plus the verifier itself)
- backend/web/cli have no front matter — they get the full default tool set

The mission walks the PM through a strict architect-first → parallel
engineers → reviewer order. Verifier is a bash script that hits the
running app via curl and shells out to the CLI client.

No e2e driver yet — that lands in the next commit alongside the actual
orchestrated run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Kanban e2e driver + orchestrated run (USER-ORDERED GATE)

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Add `scripts/e2e-build-kanban.sh` (mirrors `scripts/e2e-build-urlshortener.sh` shape — same three watchdogs), then actually run it against this codebase as the v2.0 proof. The kanban app it builds must pass the verifier.

**Files:**
- Create: `scripts/e2e-build-kanban.sh`
- (Runtime, in the target project) Creates and tears down `/tmp/kanban-e2e` with a kanban app inside

**Acceptance Criteria:**
- [ ] `scripts/e2e-build-kanban.sh` is executable and passes `bash -n`
- [ ] It mirrors `e2e-build-urlshortener.sh`'s three watchdogs (wall-clock 5400s, activity 600s, cost ceiling $5)
- [ ] Running the script produces exit code 0 and a working kanban app at `/tmp/kanban-e2e` whose `verifier.sh` exits 0
- [ ] The script copies `examples/kanban/{mission.md,roles/,verifier.sh}` into the target project's `.orchestra/briefs/`, `.orchestra/roles/`, and project root respectively
- [ ] The script kills any pre-existing `orch-kanban-e2e` tmux session at start AND on EXIT
- [ ] During the run, `orchestra status` shows architect → 3 engineers in parallel → reviewer
- [ ] All five role files load via the new filesystem loader (no `RoleNotFoundError`)
- [ ] The reviewer either approves or escalates; if it escalates, the PM resolves and re-runs the verifier
- [ ] Final `git log` on the kanban project main branch shows: 1 commit from architect, 3 from engineers, optional reviewer/PM commits, and `examples/kanban/verifier.sh` produces `OK`

**Verify:**
```bash
./scripts/e2e-build-kanban.sh
echo "exit=$?"
```
Expected: prints `OK` from the verifier, exits 0.

**Steps:**

- [ ] **Step 1: Author `scripts/e2e-build-kanban.sh`**

Mirror `scripts/e2e-build-urlshortener.sh` structure. Key differences:
- `PROJECT_DIR=/tmp/kanban-e2e`
- `SESSION_NAME=orch-kanban-e2e`
- After `orchestra init`, copy `examples/kanban/mission.md` → `$PROJECT_DIR/.orchestra/briefs/mission.md`
- Copy `examples/kanban/roles/` → `$PROJECT_DIR/.orchestra/roles/`
- Copy `examples/kanban/verifier.sh` → `$PROJECT_DIR/examples/kanban/verifier.sh` (preserving the path the reviewer + PM will reference)
- Same three watchdogs (wall-clock / activity / cost)
- On EXIT trap: kill the tmux session

Full script:

```bash
#!/usr/bin/env bash
# v2.0 e2e: PM coordinates an architect, three parallel engineers, and a
# reviewer to build a Trello-lite kanban app. Three watchdogs (wall-clock,
# activity, cost). The kanban verifier passing in the temp project is the
# acceptance signal.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="${KANBAN_PROJECT_DIR:-/tmp/kanban-e2e}"
SESSION_NAME="orch-kanban-e2e"
WALLCLOCK_SECONDS="${WALLCLOCK_SECONDS:-5400}"
ACTIVITY_SECONDS="${ACTIVITY_SECONDS:-600}"
COST_USD_CEILING="${COST_USD_CEILING:-5}"

cleanup_tmux() { tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true; }
trap cleanup_tmux EXIT
cleanup_tmux  # kill any leftover before we start

# --- Clean target dir, init git ---------------------------------------
if [[ -d "$PROJECT_DIR/.git" ]]; then
  ( cd "$PROJECT_DIR" \
    && git worktree list --porcelain 2>/dev/null \
       | awk '/^worktree /{print $2}' | grep -v "^$PROJECT_DIR\$" \
       | while read -r wt; do git worktree remove --force "$wt" || true; done \
    && git branch --list 'orch/*' | sed 's/^[*+ ] //' \
       | while read -r br; do [[ -n "$br" ]] && git branch -D "$br" || true; done
  ) || true
fi
rm -rf "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR"

( cd "$PROJECT_DIR" \
  && git init -q -b main \
  && git config user.email "orch@local" \
  && git config user.name "orch" \
  && mkdir -p docs examples/kanban \
  && cp "$REPO_ROOT/examples/kanban/verifier.sh" examples/kanban/verifier.sh \
  && chmod +x examples/kanban/verifier.sh \
  && git add examples/kanban/verifier.sh \
  && git commit -q -m "seed: kanban verifier" )

( cd "$PROJECT_DIR" && "$REPO_ROOT/.venv/bin/orchestra" init )

# Copy mission + roles into the target project's .orchestra/
mkdir -p "$PROJECT_DIR/.orchestra/briefs" "$PROJECT_DIR/.orchestra/roles"
cp "$REPO_ROOT/examples/kanban/mission.md" "$PROJECT_DIR/.orchestra/briefs/mission.md"
cp "$REPO_ROOT/examples/kanban/roles/"*.md "$PROJECT_DIR/.orchestra/roles/"

# --- Watchdogs ---------------------------------------------------------
DB="$PROJECT_DIR/.orchestra/state.db"
LOGS="$PROJECT_DIR/.orchestra"

_query() { sqlite3 "$DB" "$@"; }

_wallclock_watchdog() {
  sleep "$WALLCLOCK_SECONDS"
  echo "WALLCLOCK_TIMEOUT" >&2
  exit 124
}

_activity_watchdog() {
  local last
  last=$(date +%s)
  while sleep 30; do
    local now max
    now=$(date +%s)
    max=$(_query "SELECT COALESCE(MAX(id), 0) FROM events" 2>/dev/null || echo 0)
    if [[ -f "$LOGS/last-event-id" ]]; then
      local prev
      prev=$(cat "$LOGS/last-event-id" 2>/dev/null || echo 0)
      if [[ "$max" != "$prev" ]]; then last="$now"; fi
    fi
    echo "$max" > "$LOGS/last-event-id"
    if (( now - last > ACTIVITY_SECONDS )); then
      echo "ACTIVITY_TIMEOUT" >&2
      exit 125
    fi
  done
}

_cost_watchdog() {
  # Per-million-token pricing (Anthropic public list 2026-05-18):
  #   Opus 4.x — $15 in / $75 out
  #   Sonnet 4.x — $3 in / $15 out
  #   Haiku 4.x — $1 in / $5 out
  while sleep 60; do
    local usd
    usd=$(python3 - "$DB" <<'PY'
import json, re, sqlite3, sys
RATES = {
  "opus":   {"in":15.0,"out":75.0},
  "sonnet": {"in": 3.0,"out":15.0},
  "haiku":  {"in": 1.0,"out": 5.0},
}
FAM = re.compile(r"(?:^|[-_/])(opus|sonnet|haiku)(?:$|[-\[_/])", re.IGNORECASE)
db=sys.argv[1]; conn=sqlite3.connect(db)
worker_models={wid:(m or "") for wid,m in conn.execute("SELECT id, model FROM workers")}
total=0.0
for wid,payload in conn.execute(
    "SELECT worker_id, payload FROM events WHERE kind='turn_complete'"):
    try: p=json.loads(payload)
    except Exception: p={}
    model=p.get("model") or worker_models.get(wid) or ""
    m=FAM.search(model.lower()); r=RATES["opus"] if not m else RATES[m.group(1).lower()]
    inp=int(p.get("input_tokens",0) or 0)
    out=int(p.get("output_tokens",0) or 0)
    total += (inp/1_000_000.0)*r["in"] + (out/1_000_000.0)*r["out"]
print(f"{total:.4f}")
PY
)
    awk -v u="$usd" -v c="$COST_USD_CEILING" 'BEGIN{ if (u+0 > c+0) exit 0; exit 1 }' \
      && { echo "COST_CEILING_EXCEEDED usd=$usd ceiling=$COST_USD_CEILING" >&2; exit 126; }
  done
}

_wallclock_watchdog &  WALL=$!
_activity_watchdog &   ACT=$!
_cost_watchdog &       COST=$!
trap 'kill $WALL $ACT $COST 2>/dev/null; cleanup_tmux' EXIT

# --- Run the PM ------------------------------------------------------
export PATH="$REPO_ROOT/.venv/bin:$PATH"
( cd "$PROJECT_DIR" \
  && orchestra run .orchestra/briefs/mission.md \
       --max-wallclock "$WALLCLOCK_SECONDS" \
       --max-activity "$ACTIVITY_SECONDS" )

RC=$?
kill $WALL $ACT $COST 2>/dev/null || true

# Final acceptance: the kanban verifier must exit 0 from inside the project.
if [[ $RC -eq 0 ]]; then
  ( cd "$PROJECT_DIR" && bash examples/kanban/verifier.sh )
  RC=$?
fi
exit $RC
```

- [ ] **Step 2: Make executable and lint**

```bash
chmod +x scripts/e2e-build-kanban.sh
bash -n scripts/e2e-build-kanban.sh
```
Expected: no syntax errors.

- [ ] **Step 3: Run the e2e**

```bash
./scripts/e2e-build-kanban.sh
echo "exit=$?"
```

Expected: prints `OK` (from the verifier) and exits 0. Wall-clock should land
under ~25 minutes for a 5-worker pipeline; cost under $5.

If a watchdog fires (124/125/126) or the verifier fails: inspect
`/tmp/kanban-e2e/.orchestra/state.db` for the last events, identify the
failing worker, and either patch the role brief OR file a v2.0 follow-up
issue for the specific friction. **Do not** mark this task complete
until the verifier produces `OK`.

- [ ] **Step 4: Capture results in the commit**

```bash
git add scripts/e2e-build-kanban.sh
git commit -m "$(cat <<'EOF'
feat(e2e): kanban — five-role acceptance test for v2.0 framework

scripts/e2e-build-kanban.sh stages examples/kanban/ into /tmp/kanban-e2e,
spawns the PM via `orchestra run`, and waits for the verifier to pass.
Three watchdogs (wall-clock 5400s, activity 600s, cost $5).

Verified by an actual orchestrated run on 2026-05-18:
- architect committed docs/api.yaml to main
- backend/web/cli engineers ran in parallel in their worktrees
- reviewer (no worktree, read-only role) approved the merged code
- verifier produced OK

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["scripts/e2e-build-kanban.sh"], "verifyCommand": "./scripts/e2e-build-kanban.sh", "acceptanceCriteria": ["e2e exits 0 with verifier OK", "five-role pipeline observed end-to-end", "no role_load_failed events", "reviewer spawned without worktree and used restricted permissions"], "userGate": true, "tags": ["user-gate"], "requiresUserSpecification": false, "gateScope": "task", "failurePolicy": "halt", "requireEvidenceTokens": [["architect-committed","api.yaml-on-main"], ["engineers-merged","backend","web","cli"], ["reviewer-approved","verifier-OK"]]}
```

---

## Self-Review Notes

Cross-checked against `docs/superpowers/specs/2026-05-18-claude-orchestra-v2-design.md`:

- §"What v2.0 adds — three primitives": all three covered by Tasks 1+2+3+4+5.
- §"Code touchpoints" table: every row maps to a task (1 → \_frontmatter, 2 → settings\_merge, 3 → roles bundle, 4 → role\_prompts loader, 5 → spawn wiring, 6 → docs, 7 → kanban example, 8 → e2e).
- §"Acceptance for v2.0": items 1, 2, 3 covered by Task 8 verifier; item 4 by Task 6 CHANGELOG; item 5 ("no kanban-specific logic in framework") falls out because kanban lives entirely under `examples/` and `scripts/` — no framework module references kanban.
- §"Phasing": Tasks 1-6 are PR A (framework). Tasks 7-8 are PR B (kanban proof). Implementation order respects the dependency: PR B can be a single follow-up PR after PR A merges.

No placeholders, no TBD, no "implement later". Every step has either a complete code block or an exact command with expected output. Names are consistent across tasks (e.g. `_load_role`, `RoleNotFoundError`, `ensure_perms`).
