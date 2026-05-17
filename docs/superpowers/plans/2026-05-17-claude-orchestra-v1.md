# claude-orchestra v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v1 hook-based, multi-worker orchestration layer on top of the v0 codebase, culminating in a PM + two engineers autonomously building a URL-shortener web app end-to-end.

**Architecture:** Claude Code's hook system replaces v0 polling-based detection — workers write events into `state.db` via `orchestra worker hook <event>` invoked from `.claude/settings.local.json`. A PM worker (role=pm) spawns engineers (role=engineer) into git worktrees, mediates via a blocking `orchestra poll` tool inside a single Claude Code mega-turn, and merges results. An e2e bash script with three watchdogs (wall-clock, activity, cost) enforces the v1 acceptance contract.

**Tech Stack:** Python 3.10+, Typer, SQLite (stdlib), tmux, Claude Code hooks, FastAPI (already in v0), git worktrees, FastAPI/pytest for the *target* app the agents build.

**Source spec:** `docs/superpowers/specs/2026-05-17-claude-orchestra-v1-design.md`

---

## File structure

**Created:**
- `orchestra/hooks.py` — hook stdin parser + typed handlers per event kind
- `orchestra/settings_merge.py` — `.claude/settings.local.json` deep-merge logic
- `orchestra/poll.py` — bounded state-snapshot rendering for `orchestra poll`
- `orchestra/worktree.py` — `git worktree` add/remove helpers
- `orchestra/role_prompts.py` — PM + Engineer prompt renderers (kept separate from `prompts.py` for clarity; the existing single-role renderer stays untouched)
- `scripts/e2e-build-urlshortener.sh` — the v1 acceptance test driver
- `examples/urlshortener-mission.md` — human-authored PM mission file
- `examples/urlshortener-verifier.sh` — bash verifier the PM runs at the end
- `tests/test_hooks.py`, `tests/test_settings_merge.py`, `tests/test_poll.py`, `tests/test_worktree.py`, `tests/test_role_prompts.py`

**Modified:**
- `orchestra/state.py` — `role` + `worktree` columns on `workers`; migration helper; doc-block updated for new event kinds
- `orchestra/cli.py` — new subcommands (`send`, `answer`, `poll`, `merge`, `reap`, `worker hook`); new spawn flags (`--role`, `--brief`, `--worktree`); init extended to merge settings.local.json
- `orchestra/spawn.py` — `_wait_idle` and `_wait_first_status` rewritten to watch DB events; worktree creation step added; role-flag wiring
- `orchestra/__main__.py` — no change expected; verify Typer subapps still register
- `tests/test_state.py`, `tests/test_cli.py`, `tests/test_spawn.py`, `tests/test_prompts.py`, `tests/conftest.py` — extended

---

## Task 1: Hook stdin spike — capture real Claude Code hook payloads

**Goal:** A no-op `orchestra worker hook <event>` that logs raw stdin to `.orchestra/hook-debug.log`, run against a real worker to learn the JSON shape Claude Code sends. Document the captured shapes (especially Stop's token fields) in code as the source of truth.

**Files:**
- Create: `orchestra/hooks.py` (log-only, plus a `LOG_PATH` constant)
- Modify: `orchestra/cli.py:18-20` (register a `worker hook` subcommand)
- Create: `tests/test_hooks.py`
- Create: `docs/hook-schemas.md` (or inline as module docstring in `hooks.py`) — captured shapes

**Acceptance Criteria:**
- [ ] `echo '{"foo":"bar"}' | ORCHESTRA_WORKER_ID=w1 ORCHESTRA_STATE_DB=/tmp/x orchestra worker hook SessionStart` exits 0 and appends a single JSONL line to `.orchestra/hook-debug.log` next to the state db
- [ ] The settings.local.json snippet from the spec, copied into a real project, fires hooks for SessionStart, Stop, PreToolUse, PostToolUse, SessionEnd, Notification
- [ ] At least one captured payload per event kind is pasted into `docs/hook-schemas.md`
- [ ] Hook never returns non-zero even on internal error (a failing hook would break the worker turn)

**Verify:** `pytest tests/test_hooks.py -v` then run the manual capture script (documented in `docs/hook-schemas.md`).

### Steps

- [ ] **Step 1: Create `orchestra/hooks.py` skeleton + failing test**

`tests/test_hooks.py`:

```python
"""Tests for orchestra.hooks (spike layer: log-only)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestra import hooks


class TestSpikeLogging:
    def test_logs_raw_stdin_to_debug_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        payload = {"event": "SessionStart", "session_id": "abc"}
        rc = hooks.run_spike("SessionStart", stdin_text=json.dumps(payload))
        assert rc == 0
        log = db.parent / "hook-debug.log"
        assert log.exists()
        line = json.loads(log.read_text().strip())
        assert line["event"] == "SessionStart"
        assert line["payload"] == payload
        assert "ts" in line
        assert line["worker_id"] == "w1"

    def test_returns_zero_even_on_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        rc = hooks.run_spike("Stop", stdin_text="not-json{")
        assert rc == 0
        log = db.parent / "hook-debug.log"
        assert log.exists()
        line = json.loads(log.read_text().strip())
        assert line["parse_error"] is True
        assert line["raw"] == "not-json{"
```

Run: `pytest tests/test_hooks.py -v` → Expected: ImportError / module not found.

- [ ] **Step 2: Implement `orchestra/hooks.py` (spike)**

```python
"""Claude Code hook entrypoint for orchestra workers.

This module starts as a log-only spike: every hook invocation appends a
JSONL line to `.orchestra/hook-debug.log` (next to state.db) so we can
inspect the actual payload shape Claude Code sends. Typed dispatch is
added in Task 2 once the schemas are pinned down.

Invocation contract (from Claude Code):
    The configured `command` is run; the event payload arrives on stdin
    as JSON. The hook MUST exit 0 — a non-zero exit breaks the worker turn.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _log_path() -> Path | None:
    db = os.environ.get("ORCHESTRA_STATE_DB")
    if not db:
        return None
    return Path(db).parent / "hook-debug.log"


def run_spike(event: str, *, stdin_text: str) -> int:
    """Log-only hook implementation. Returns the exit code (always 0)."""
    log = _log_path()
    if log is None:
        return 0  # No state db env — silently no-op.
    log.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "ts": _now(),
        "event": event,
        "worker_id": os.environ.get("ORCHESTRA_WORKER_ID"),
    }
    try:
        record["payload"] = json.loads(stdin_text) if stdin_text else None
    except json.JSONDecodeError:
        record["parse_error"] = True
        record["raw"] = stdin_text
    with log.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: reads event name from argv, payload from stdin."""
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        return 0  # Defensive: no event name → silent no-op.
    event = argv[0]
    try:
        stdin_text = sys.stdin.read()
    except Exception:  # noqa: BLE001 — any read error must not break the turn
        stdin_text = ""
    return run_spike(event, stdin_text=stdin_text)
```

- [ ] **Step 3: Wire the subcommand into `orchestra/cli.py`**

Add a worker hook command. Edit `orchestra/cli.py` immediately after the existing `worker_escalate` definition (around line 242):

```python
@worker_app.command("hook")
def worker_hook(event: str = typer.Argument(..., metavar="EVENT")) -> None:
    """Hook entrypoint invoked by Claude Code; reads payload JSON on stdin."""
    from orchestra import hooks  # local import to keep CLI import cheap

    rc = hooks.main([event])
    raise typer.Exit(rc)
```

(Local import keeps `orchestra status` etc. from paying the hook-module import cost.)

- [ ] **Step 4: Run unit tests**

Run: `pytest tests/test_hooks.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Capture real hook payloads (manual)**

Create `docs/hook-schemas.md` with this scaffolding:

````markdown
# Claude Code hook payload schemas — captured 2026-05-17

These are the actual JSON shapes Claude Code sends on stdin for each hook
event. Captured by running a real `claude` session against the spike-mode
`orchestra worker hook` handler. Source of truth for `orchestra/hooks.py`
typed dispatch.

## How to reproduce

```bash
mkdir -p /tmp/hook-capture && cd /tmp/hook-capture
orchestra init
mkdir -p .claude
cat > .claude/settings.local.json <<'EOF'
{
  "hooks": {
    "SessionStart":  [{"hooks": [{"type": "command", "command": "orchestra worker hook SessionStart"}]}],
    "Stop":          [{"hooks": [{"type": "command", "command": "orchestra worker hook Stop"}]}],
    "PreToolUse":    [{"matcher": ".*", "hooks": [{"type": "command", "command": "orchestra worker hook PreToolUse"}]}],
    "PostToolUse":   [{"matcher": ".*", "hooks": [{"type": "command", "command": "orchestra worker hook PostToolUse"}]}],
    "SessionEnd":    [{"hooks": [{"type": "command", "command": "orchestra worker hook SessionEnd"}]}],
    "Notification":  [{"hooks": [{"type": "command", "command": "orchestra worker hook Notification"}]}]
  }
}
EOF
ORCHESTRA_WORKER_ID=spike ORCHESTRA_STATE_DB=$PWD/.orchestra/state.db \
  claude --dangerously-skip-permissions
# inside claude: ask it to write a file, run a bash command, then exit
tail -n 200 .orchestra/hook-debug.log
```

## Captured payloads

(paste one JSON example per event kind here)

### SessionStart
```json
<paste>
```

### Stop
```json
<paste — note especially the token-count fields>
```

### PreToolUse / PostToolUse / SessionEnd / Notification
```json
<paste>
```
````

Run the capture script manually. Paste real payloads into the doc. (This step does not have a unit-test gate — its output is documentation that informs Task 2.)

- [ ] **Step 6: Commit**

```bash
git add orchestra/hooks.py orchestra/cli.py tests/test_hooks.py docs/hook-schemas.md
git commit -m "feat(hooks): add spike-mode worker hook handler

Log-only entrypoint that appends every Claude Code hook payload to
.orchestra/hook-debug.log next to state.db. Captures the actual JSON
shapes so Task 2's typed handlers can dispatch on real fields, not
guesses.

Includes the settings.local.json snippet to wire it up; captured
shapes pasted into docs/hook-schemas.md."
```

---

## Task 2: Typed hook handlers + init-time settings.local.json deep-merge

**Goal:** Replace the spike's log-only handler with typed dispatch that writes the event-kind table from the spec into state.db. `orchestra init` deep-merges the hook block into `<project>/.claude/settings.local.json` once per project, preserving any pre-existing user hooks.

**Files:**
- Modify: `orchestra/hooks.py` (typed dispatch replaces `run_spike` callers; spike fallback remains for diagnostics)
- Create: `orchestra/settings_merge.py`
- Modify: `orchestra/cli.py` (init merges settings.local.json)
- Modify: `tests/test_hooks.py`
- Create: `tests/test_settings_merge.py`

**Acceptance Criteria:**
- [ ] Six events map: `SessionStart→session_ready` (status=working), `Stop→turn_complete` (turns+=1, payload carries token counts), `PreToolUse→tool_started`, `PostToolUse→tool_finished`, `SessionEnd→session_ended` (status=done if not already error), `Notification→notification`
- [ ] Hook always exits 0; on internal error it writes a row to `events` with `kind="hook_error"` and a `traceback` payload, plus a line to `.orchestra/hook-errors.log`
- [ ] `orchestra init` writes or merges `.claude/settings.local.json`: empty file gets the canonical block; file with unrelated hooks keeps them; file with overlapping event keys gets our command appended to the existing array (no replacement)
- [ ] Second `orchestra init` invocation is idempotent: no duplicate `orchestra worker hook` entries

**Verify:** `pytest tests/test_hooks.py tests/test_settings_merge.py tests/test_cli.py -v`

### Steps

- [ ] **Step 1: Write failing tests for typed dispatch**

Append to `tests/test_hooks.py`:

```python
import sqlite3

from orchestra import state


def _seed_worker(db: Path) -> sqlite3.Connection:
    conn = state.connect(db)
    state.init_schema(conn)
    state.create_worker(
        conn, id="w1", task="t", model="sonnet",
        branch="orch/w1", pane_target="s:1",
    )
    return conn


class TestTypedDispatch:
    def test_session_start_sets_working_and_records_session_ready(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        rc = hooks.dispatch("SessionStart", stdin_text='{"session_id":"abc"}')
        assert rc == 0
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "working"
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "session_ready" in kinds

    def test_stop_increments_turns_and_records_turn_complete(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}}
        rc = hooks.dispatch("Stop", stdin_text=json.dumps(payload))
        assert rc == 0
        w = state.get_worker(conn, "w1")
        assert w is not None and w.turns == 1
        evts = [e for e in state.list_events(conn, worker_id="w1")
                if e.kind == "turn_complete"]
        assert len(evts) == 1
        # token fields pulled from payload
        assert evts[0].payload.get("input_tokens") == 100
        assert evts[0].payload.get("output_tokens") == 50

    def test_session_end_sets_done_when_no_prior_error(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        state.update_worker(conn, "w1", status="working")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        rc = hooks.dispatch("SessionEnd", stdin_text="{}")
        assert rc == 0
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "done"

    def test_session_end_keeps_error_status(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        state.update_worker(conn, "w1", status="error")
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        hooks.dispatch("SessionEnd", stdin_text="{}")
        w = state.get_worker(conn, "w1")
        assert w is not None and w.status == "error"

    def test_pre_post_tool_use_record_events_only(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _seed_worker(tmp_db)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        hooks.dispatch("PreToolUse",
                       stdin_text='{"tool_name":"Bash","tool_input":{"command":"ls"}}')
        hooks.dispatch("PostToolUse",
                       stdin_text='{"tool_name":"Bash","tool_output":"a\\nb"}')
        kinds = [e.kind for e in state.list_events(conn, worker_id="w1")]
        assert "tool_started" in kinds and "tool_finished" in kinds
        w = state.get_worker(conn, "w1")
        assert w is not None and w.turns == 0

    def test_internal_error_records_hook_error_and_returns_zero(
        self, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No worker exists for the env-set id; status update will raise.
        state.connect(tmp_db).close()
        # ^ ensures DB file exists; schema absent → typed dispatch will error
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "nope")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(tmp_db))
        rc = hooks.dispatch("SessionStart", stdin_text="{}")
        assert rc == 0  # NEVER non-zero
        err_log = tmp_db.parent / "hook-errors.log"
        assert err_log.exists()
```

Run: `pytest tests/test_hooks.py::TestTypedDispatch -v` → Expected: ALL FAIL (`hooks.dispatch` not yet defined).

- [ ] **Step 2: Implement typed dispatch in `orchestra/hooks.py`**

Replace the file content with:

```python
"""Claude Code hook entrypoint for orchestra workers.

Maps the six Claude Code hook events to rows in state.db:

    SessionStart -> session_ready    (worker.status=working)
    Stop         -> turn_complete    (worker.turns += 1; payload carries tokens)
    PreToolUse   -> tool_started     (event only)
    PostToolUse  -> tool_finished    (event only)
    SessionEnd   -> session_ended    (worker.status=done if not already error)
    Notification -> notification     (event only)

Captured payload shapes are documented in docs/hook-schemas.md.

This module MUST never raise out to the caller: a non-zero exit aborts
the worker turn. Errors are caught, logged to hook-errors.log, and
recorded as a `hook_error` event (when the DB is reachable).
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestra import state


# --- Helpers ---

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _orch_dir() -> Path | None:
    db = os.environ.get("ORCHESTRA_STATE_DB")
    return Path(db).parent if db else None


def _state_db() -> Path | None:
    db = os.environ.get("ORCHESTRA_STATE_DB")
    return Path(db) if db else None


def _worker_id() -> str | None:
    return os.environ.get("ORCHESTRA_WORKER_ID")


def _append_log(name: str, line: str) -> None:
    d = _orch_dir()
    if d is None:
        return
    d.mkdir(parents=True, exist_ok=True)
    (d / name).open("a").write(line.rstrip("\n") + "\n")


# --- Spike fallback (Task 1) ---

def run_spike(event: str, *, stdin_text: str) -> int:
    record: dict[str, Any] = {
        "ts": _now(),
        "event": event,
        "worker_id": _worker_id(),
    }
    try:
        record["payload"] = json.loads(stdin_text) if stdin_text else None
    except json.JSONDecodeError:
        record["parse_error"] = True
        record["raw"] = stdin_text
    _append_log("hook-debug.log", json.dumps(record))
    return 0


# --- Typed dispatch (Task 2) ---

def _extract_token_usage(payload: dict[str, Any]) -> dict[str, int]:
    """Pull token counts out of a Stop-hook payload.

    Claude Code's exact field names live in docs/hook-schemas.md; this
    function tolerates the two known shapes (top-level vs nested under
    `usage`). Missing fields default to 0 — never raises.
    """
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        usage = payload if isinstance(payload, dict) else {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
    }


def _handle(event: str, payload: dict[str, Any], conn, wid: str) -> None:
    if event == "SessionStart":
        state.update_worker(conn, wid, status="working")
        state.record_event(conn, "session_ready", worker_id=wid,
                           session_id=payload.get("session_id"))
        return
    if event == "Stop":
        w = state.get_worker(conn, wid)
        next_turns = (w.turns if w else 0) + 1
        state.update_worker(conn, wid, turns=next_turns)
        tokens = _extract_token_usage(payload)
        state.record_event(conn, "turn_complete", worker_id=wid, **tokens)
        return
    if event == "PreToolUse":
        state.record_event(conn, "tool_started", worker_id=wid,
                           tool=payload.get("tool_name"),
                           input_summary=str(payload.get("tool_input", ""))[:200])
        return
    if event == "PostToolUse":
        state.record_event(conn, "tool_finished", worker_id=wid,
                           tool=payload.get("tool_name"),
                           output_summary=str(payload.get("tool_output", ""))[:200])
        return
    if event == "SessionEnd":
        w = state.get_worker(conn, wid)
        # SessionEnd marks done unless the worker already failed.
        if w is not None and w.status not in ("error", "stopped", "stop_send_failed"):
            state.update_worker(conn, wid, status="done")
        state.record_event(conn, "session_ended", worker_id=wid,
                           reason=payload.get("reason"))
        return
    if event == "Notification":
        state.record_event(conn, "notification", worker_id=wid,
                           message=payload.get("message"))
        return
    # Unknown event: log to debug log but don't error.
    _append_log("hook-debug.log",
                json.dumps({"ts": _now(), "event": event,
                            "worker_id": wid, "unknown": True, "payload": payload}))


def dispatch(event: str, *, stdin_text: str) -> int:
    """Typed dispatch — always returns 0."""
    db = _state_db()
    wid = _worker_id()
    if db is None or wid is None:
        # Hook fired outside an orchestra worker context: no-op.
        return 0
    try:
        payload = json.loads(stdin_text) if stdin_text else {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
    except json.JSONDecodeError:
        payload = {"_parse_error": True, "_raw": stdin_text}

    conn = None
    try:
        conn = state.connect(db)
        _handle(event, payload, conn, wid)
    except Exception:  # noqa: BLE001 — never break the turn
        tb = traceback.format_exc()
        _append_log("hook-errors.log",
                    json.dumps({"ts": _now(), "event": event,
                                "worker_id": wid, "traceback": tb}))
        # Best-effort: record a hook_error event too if DB is reachable.
        try:
            if conn is None:
                conn = state.connect(db)
            state.record_event(conn, "hook_error", worker_id=wid,
                               event=event, traceback=tb[-2000:])
        except Exception:  # noqa: BLE001
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        return 0
    event = argv[0]
    try:
        stdin_text = sys.stdin.read()
    except Exception:  # noqa: BLE001
        stdin_text = ""
    return dispatch(event, stdin_text=stdin_text)
```

- [ ] **Step 3: Run typed-dispatch tests**

Run: `pytest tests/test_hooks.py::TestTypedDispatch -v`
Expected: all six pass. The earlier spike tests should still pass because `run_spike` is preserved.

- [ ] **Step 4: Write failing tests for settings.local.json merge**

`tests/test_settings_merge.py`:

```python
"""Tests for the settings.local.json deep-merge."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra import settings_merge


HOOK_CMD = "orchestra worker hook"


class TestMerge:
    def test_empty_target_gets_canonical_block(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        # Target file absent.
        settings_merge.ensure_hooks(settings)
        got = json.loads(settings.read_text())
        for event in ("SessionStart", "Stop", "PreToolUse", "PostToolUse",
                      "SessionEnd", "Notification"):
            entries = got["hooks"][event]
            assert any(
                HOOK_CMD in inner["command"]
                for outer in entries for inner in outer["hooks"]
            ), f"missing hook for {event}"

    def test_existing_unrelated_hook_preserved(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "hooks": {
                "SessionStart": [{"hooks": [
                    {"type": "command", "command": "user's own hook"}
                ]}]
            }
        }))
        settings_merge.ensure_hooks(settings)
        got = json.loads(settings.read_text())
        cmds = [
            inner["command"]
            for outer in got["hooks"]["SessionStart"]
            for inner in outer["hooks"]
        ]
        assert "user's own hook" in cmds
        assert any(HOOK_CMD in c for c in cmds)

    def test_idempotent(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings_merge.ensure_hooks(settings)
        first = settings.read_text()
        settings_merge.ensure_hooks(settings)
        second = settings.read_text()
        # Second invocation must not duplicate our entries.
        got = json.loads(second)
        for event in got["hooks"]:
            cmds = [
                inner["command"]
                for outer in got["hooks"][event]
                for inner in outer["hooks"]
                if HOOK_CMD in inner["command"]
            ]
            assert len(cmds) == 1, f"duplicate for {event}: {cmds}"
        # Structurally stable across runs.
        assert json.loads(first) == json.loads(second)

    def test_non_dict_target_overwritten_with_canonical(self, tmp_path: Path) -> None:
        # If the file is corrupt (not a JSON object), we replace with canonical.
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("[]")
        settings_merge.ensure_hooks(settings)
        got = json.loads(settings.read_text())
        assert "hooks" in got
```

Run: `pytest tests/test_settings_merge.py -v` → Expected: FAIL (module missing).

- [ ] **Step 5: Implement `orchestra/settings_merge.py`**

```python
"""Deep-merge orchestra's Claude Code hooks into .claude/settings.local.json.

We own only the entries whose `command` contains `orchestra worker hook`.
Any other hooks the user added are preserved untouched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOOK_MARKER = "orchestra worker hook"

EVENTS_NO_MATCHER = ("SessionStart", "Stop", "SessionEnd", "Notification")
EVENTS_WITH_MATCHER = ("PreToolUse", "PostToolUse")


def _our_entry(event: str) -> dict[str, Any]:
    inner = {"type": "command", "command": f"{HOOK_MARKER} {event}"}
    if event in EVENTS_WITH_MATCHER:
        return {"matcher": ".*", "hooks": [inner]}
    return {"hooks": [inner]}


def _entry_is_ours(entry: dict[str, Any]) -> bool:
    for h in entry.get("hooks", []):
        if HOOK_MARKER in (h.get("command") or ""):
            return True
    return False


def _merge_event(existing: list[Any], event: str) -> list[Any]:
    ours = _our_entry(event)
    keep: list[Any] = []
    for e in existing or []:
        if isinstance(e, dict) and _entry_is_ours(e):
            continue  # drop any stale orchestra entry; we'll re-add a fresh one
        keep.append(e)
    keep.append(ours)
    return keep


def ensure_hooks(path: Path) -> None:
    """Merge canonical orchestra hooks into `path`. Creates the file if missing."""
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
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    for event in EVENTS_NO_MATCHER + EVENTS_WITH_MATCHER:
        hooks[event] = _merge_event(hooks.get(event) or [], event)
    path.write_text(json.dumps(data, indent=2) + "\n")
```

- [ ] **Step 6: Run merge tests**

Run: `pytest tests/test_settings_merge.py -v`
Expected: all four pass.

- [ ] **Step 7: Wire merge into `orchestra init`**

Edit `orchestra/cli.py`, replacing the `init` function:

```python
@app.command()
def init() -> None:
    """Initialize .orchestra/ in the current directory and install hooks."""
    cwd = Path.cwd()
    d = _orch_dir()
    d.mkdir(exist_ok=True)
    db = d / "state.db"
    conn = state.connect(db)
    state.init_schema(conn)
    conn.close()
    cfg = d / "config.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG)
    # Install Claude Code hooks (idempotent).
    from orchestra import settings_merge  # local import to keep init cheap
    settings_merge.ensure_hooks(cwd / ".claude" / "settings.local.json")
    typer.echo(f"initialized {d}")
```

- [ ] **Step 8: Extend `tests/test_cli.py`**

Add to `TestInit`:

```python
    def test_installs_hooks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        settings = tmp_path / ".claude" / "settings.local.json"
        assert settings.exists()
        got = __import__("json").loads(settings.read_text())
        assert "SessionStart" in got["hooks"]
```

- [ ] **Step 9: Run full test suite**

Run: `pytest -v`
Expected: all previously-green tests still pass; new tests pass.

- [ ] **Step 10: Commit**

```bash
git add orchestra/hooks.py orchestra/settings_merge.py orchestra/cli.py \
        tests/test_hooks.py tests/test_settings_merge.py tests/test_cli.py
git commit -m "feat(hooks): typed dispatch + init-time settings.local.json merge

Replace the spike with typed handlers for the six Claude Code hook
events (SessionStart/Stop/Pre|PostToolUse/SessionEnd/Notification),
writing typed rows into events and mutating workers.status/turns.

Stop's payload carries Claude's token usage which we extract into the
turn_complete event payload — feeds the e2e cost watchdog later.

orchestra init now deep-merges our hook block into .claude/settings.
local.json, preserving any user hooks. Repeating init does not
duplicate entries."
```

---

## Task 3: State schema additions — role/worktree columns + new event kinds

**Goal:** Extend `workers` with `role TEXT NOT NULL DEFAULT 'engineer'` and `worktree TEXT NULL`. Issue ALTER TABLE on init when columns are missing (no migration framework). Document the new event kinds in `state.py`'s module docstring.

**Files:**
- Modify: `orchestra/state.py` (schema, dataclass, `create_worker`, migration helper)
- Modify: `tests/test_state.py`

**Acceptance Criteria:**
- [ ] `Worker` dataclass has `role: str = "engineer"` and `worktree: str | None = None`
- [ ] `init_schema` is idempotent and upgrades a v0 DB (missing columns) in place
- [ ] `create_worker(..., role=..., worktree=...)` accepts both new kwargs; v0 callers (positional/keyword without them) keep working
- [ ] `worker.status == "done"` is a valid value (no enum constraint; just documented)

**Verify:** `pytest tests/test_state.py -v`

### Steps

- [ ] **Step 1: Write failing tests**

Append to `tests/test_state.py`:

```python
class TestSchemaUpgrade:
    def test_v0_db_gets_new_columns_on_init(self, tmp_db: Path) -> None:
        # Simulate a v0 DB: original schema only.
        v0_sql = """
        CREATE TABLE workers (
            id TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL,
            branch TEXT, pane_target TEXT NOT NULL, status TEXT NOT NULL,
            progress TEXT, turns INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT,
            ts TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT NOT NULL,
            ts TEXT NOT NULL, question TEXT NOT NULL, context TEXT,
            blocking INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
            answer TEXT
        );
        """
        conn = state.connect(tmp_db)
        conn.executescript(v0_sql)
        # Now run init_schema — should add role/worktree columns.
        state.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        assert "role" in cols
        assert "worktree" in cols


class TestRoleAndWorktree:
    def test_create_with_role_and_worktree(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        w = state.create_worker(
            conn, id="backend", task="api", model="sonnet",
            branch="orch/backend", pane_target="s:backend",
            role="engineer", worktree="backend",
        )
        assert w.role == "engineer"
        assert w.worktree == "backend"

    def test_default_role_is_engineer(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        w = state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        assert w.role == "engineer"
        assert w.worktree is None

    def test_pm_role(self, tmp_db: Path) -> None:
        conn = _open(tmp_db)
        w = state.create_worker(
            conn, id="pm", task="lead", model="opus",
            branch=None, pane_target="s:pm", role="pm",
        )
        assert w.role == "pm"
```

Run: `pytest tests/test_state.py::TestSchemaUpgrade tests/test_state.py::TestRoleAndWorktree -v`
Expected: FAIL (no `role`/`worktree` support yet).

- [ ] **Step 2: Update `Worker` dataclass and schema**

Edit `orchestra/state.py`:

Replace the `Worker` dataclass (lines 21-32):

```python
@dataclass(frozen=True)
class Worker:
    id: str
    task: str
    model: str
    branch: str | None
    pane_target: str
    status: str
    progress: str | None
    turns: int
    started_at: str
    updated_at: str
    role: str = "engineer"
    worktree: str | None = None
```

Replace the `SCHEMA` block and `init_schema` (lines 73-108):

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id          TEXT PRIMARY KEY,
    task        TEXT NOT NULL,
    model       TEXT NOT NULL,
    branch      TEXT,
    pane_target TEXT NOT NULL,
    status      TEXT NOT NULL,
    progress    TEXT,
    turns       INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'engineer',
    worktree    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_worker_ts ON events(worker_id, ts);
CREATE TABLE IF NOT EXISTS escalations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    ts        TEXT NOT NULL,
    question  TEXT NOT NULL,
    context   TEXT,
    blocking  INTEGER NOT NULL,
    resolved  INTEGER NOT NULL DEFAULT 0,
    answer    TEXT
);
"""


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Forward-compat: if a v0 DB pre-dates role/worktree, ALTER TABLE in.
    cols = _existing_columns(conn, "workers")
    if "role" not in cols:
        conn.execute(
            "ALTER TABLE workers ADD COLUMN role TEXT NOT NULL DEFAULT 'engineer'"
        )
    if "worktree" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN worktree TEXT")
```

- [ ] **Step 3: Update `_row_to_worker` and `create_worker`**

Replace `_row_to_worker` (lines 113-125):

```python
def _row_to_worker(row: sqlite3.Row) -> Worker:
    return Worker(
        id=row["id"],
        task=row["task"],
        model=row["model"],
        branch=row["branch"],
        pane_target=row["pane_target"],
        status=row["status"],
        progress=row["progress"],
        turns=row["turns"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        role=row["role"],
        worktree=row["worktree"],
    )
```

Replace `create_worker` (lines 128-149):

```python
def create_worker(
    conn: sqlite3.Connection,
    *,
    id: str,
    task: str,
    model: str,
    branch: str | None,
    pane_target: str,
    status: str = "spawning",
    role: str = "engineer",
    worktree: str | None = None,
) -> Worker:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO workers (id, task, model, branch, pane_target,
                             status, progress, turns, started_at, updated_at,
                             role, worktree)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?)
        """,
        (id, task, model, branch, pane_target, status, ts, ts, role, worktree),
    )
    got = get_worker(conn, id)
    assert got is not None  # just inserted
    return got
```

- [ ] **Step 4: Update module docstring with new event kinds**

Replace the docstring at `orchestra/state.py:1-9` with:

```python
"""SQLite-backed state for claude-orchestra.

Tables:
- workers: one row per spawned worker, mutated as the worker progresses.
  v1 columns: role ('engineer'|'pm'), worktree (None unless --worktree was set).
- events: append-only audit trail; payload is JSON.
- escalations: blocking/non-blocking questions from workers to user / PM.

Event kinds (v0 → v1):
- v0: spawn_start, spawn_window, spawn_idle, spawn_timeout, spawn_trust_accepted,
      model_switched, prompt_injected, prompt_inject_retry, prompt_inject_failed,
      spawn_ok, spawn_first_status_timeout, status, escalation,
      escalation_resolved, stopped, stop_send_failed, hook_error
- v1 (hook-driven): session_ready (SessionStart), turn_complete (Stop, payload
      includes input_tokens/output_tokens/cache_read_tokens/cache_creation_tokens),
      tool_started (PreToolUse), tool_finished (PostToolUse), session_ended
      (SessionEnd), notification (Notification)
- v1 (coordination): message_sent (orchestra send), worktree_created,
      worktree_reaped, merge_attempted, merge_conflict, merge_ok

Connection settings: WAL journal mode + 5s busy timeout.
"""
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_state.py -v`
Expected: all pass (existing v0 tests + new schema tests).

- [ ] **Step 6: Commit**

```bash
git add orchestra/state.py tests/test_state.py
git commit -m "feat(state): add role + worktree columns; in-place v0 upgrade

workers gains role (default 'engineer') and nullable worktree. init_schema
ALTER TABLEs them onto an existing v0 DB so existing projects upgrade on
first v1 init. Module docstring enumerates the new v1 event kinds."
```

---

## Task 4: New CLI commands — send, answer, poll, merge, reap + spawn flags

**Goal:** Five new commands plus three new flags on `spawn`. `poll` returns a bounded state snapshot. `answer` resolves an escalation and types the answer into the asker's pane. `merge`/`reap` handle git worktree lifecycle.

**Files:**
- Modify: `orchestra/cli.py` (add subcommands; extend spawn signature)
- Create: `orchestra/poll.py`
- Create: `orchestra/worktree.py`
- Modify: `tests/test_cli.py`
- Create: `tests/test_poll.py`
- Create: `tests/test_worktree.py`

**Acceptance Criteria:**
- [ ] `orchestra spawn ID MODEL [TASK] [--role pm|engineer] [--brief PATH] [--worktree NAME]` — `--role` defaults `engineer`, `--brief` reads file contents at startup-prompt render time (forwarded to spawn_worker), `--worktree` triggers `git worktree add -b orch/<id> worktrees/<name> HEAD` before window creation
- [ ] `orchestra send <worker_id> "<msg>"` records `message_sent` event + calls `tmux.send_multiline` on the worker's pane_target
- [ ] `orchestra answer <escalation_id> "<answer>"` resolves the row, records `escalation_resolved`, sends the answer to the asker's pane
- [ ] `orchestra poll [--timeout 30] [--include-tools]` blocks up to N seconds for new events (polling DB id every 500ms), returns immediately if changes since caller's last poll (tracked via a per-caller file `.orchestra/poll-cursor.<worker_id>`), prints a markdown snapshot
- [ ] `orchestra merge <worker_id>` runs `git merge orch/<worker_id>` from cwd, records `merge_attempted` + (`merge_ok` | `merge_conflict` with diff snippet)
- [ ] `orchestra reap <worker_id>` runs `git worktree remove --force` + `git branch -D`, records `worktree_reaped`

**Verify:** `pytest tests/test_cli.py tests/test_poll.py tests/test_worktree.py -v`

### Steps

- [ ] **Step 1: Write failing tests for `orchestra/worktree.py`**

`tests/test_worktree.py`:

```python
"""Tests for orchestra.worktree (git worktree helpers)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestra import worktree


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)


class TestAddRemove:
    def test_add_creates_worktree_and_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = worktree.add(tmp_path, name="backend", worker_id="backend")
        assert wt.exists()
        assert (wt / "README.md").exists()
        # Branch should exist:
        out = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", "orch/backend"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/backend" in out

    def test_add_idempotent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt1 = worktree.add(tmp_path, name="backend", worker_id="backend")
        wt2 = worktree.add(tmp_path, name="backend", worker_id="backend")
        assert wt1 == wt2

    def test_remove(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = worktree.add(tmp_path, name="backend", worker_id="backend")
        worktree.remove(tmp_path, name="backend", worker_id="backend")
        assert not wt.exists()
        out = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", "orch/backend"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "orch/backend" not in out
```

Run: `pytest tests/test_worktree.py -v` → Expected: FAIL.

- [ ] **Step 2: Implement `orchestra/worktree.py`**

```python
"""git worktree add/remove helpers for orchestra engineers."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def add(project_root: Path, *, name: str, worker_id: str) -> Path:
    """Ensure a worktree exists at <root>/worktrees/<name> on branch orch/<worker_id>.

    Idempotent: if the directory exists, returns it without reinitialising.
    """
    wt_path = project_root / "worktrees" / name
    if wt_path.exists():
        return wt_path
    branch = f"orch/{worker_id}"
    # If the branch already exists (e.g. from a prior partial run), reuse it.
    existing = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--list", branch],
        capture_output=True, text=True, check=True,
    ).stdout
    args = ["worktree", "add"]
    if branch in existing:
        args += [str(wt_path), branch]
    else:
        args += ["-b", branch, str(wt_path), "HEAD"]
    _git(project_root, *args)
    return wt_path


def remove(project_root: Path, *, name: str, worker_id: str) -> None:
    """Remove the worktree and delete its branch. Tolerates already-missing."""
    wt_path = project_root / "worktrees" / name
    if wt_path.exists():
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(wt_path)],
            check=False,
        )
    subprocess.run(
        ["git", "-C", str(project_root), "branch", "-D", f"orch/{worker_id}"],
        check=False, capture_output=True,
    )
```

Run: `pytest tests/test_worktree.py -v` → Expected: PASS.

- [ ] **Step 3: Write failing tests for `orchestra/poll.py`**

`tests/test_poll.py`:

```python
"""Tests for orchestra.poll (state snapshot rendering)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from orchestra import poll, state


def _setup(tmp_db: Path) -> None:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    state.create_worker(
        conn, id="backend", task="api", model="sonnet",
        branch="orch/backend", pane_target="s:backend",
        role="engineer", worktree="backend",
    )
    state.create_worker(
        conn, id="frontend", task="ui", model="sonnet",
        branch="orch/frontend", pane_target="s:frontend",
        role="engineer", worktree="frontend",
    )
    conn.close()


class TestSnapshot:
    def test_renders_per_engineer_rows(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        out = poll.render_snapshot(tmp_db, since_id=0)
        assert "backend" in out and "frontend" in out

    def test_tool_events_filtered_out_of_count(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.record_event(conn, "tool_started", worker_id="backend", tool="Read")
        state.record_event(conn, "tool_finished", worker_id="backend", tool="Read")
        state.record_event(conn, "turn_complete", worker_id="backend", input_tokens=10)
        conn.close()
        out = poll.render_snapshot(tmp_db, since_id=0)
        # The new-event count for backend should be 1 (turn_complete), not 3.
        line = next(l for l in out.splitlines() if "backend" in l)
        assert "1" in line  # naive: at least one number that's 1
        # tool_started/tool_finished should not appear in the human-readable summary.
        assert "tool_started" not in out
        assert "tool_finished" not in out

    def test_pending_escalations_listed(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.create_escalation(
            conn, worker_id="backend",
            question="What is the API contract?",
            context=None, blocking=True,
        )
        conn.close()
        out = poll.render_snapshot(tmp_db, since_id=0)
        assert "API contract" in out


class TestBlocking:
    def test_returns_immediately_when_changes_since_cursor(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        state.record_event(conn, "turn_complete", worker_id="backend")
        max_id_before = max(e.id for e in state.list_events(conn))
        conn.close()
        start = time.monotonic()
        new_cursor, _ = poll.poll(tmp_db, since_id=0, timeout=5)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert new_cursor >= max_id_before

    def test_blocks_until_event_arrives(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        conn = state.connect(tmp_db)
        max_id_before = max((e.id for e in state.list_events(conn)), default=0)
        conn.close()

        import threading
        def write_after_delay() -> None:
            time.sleep(0.3)
            c = state.connect(tmp_db)
            state.record_event(c, "turn_complete", worker_id="backend")
            c.close()
        t = threading.Thread(target=write_after_delay)
        t.start()
        try:
            new_cursor, snapshot = poll.poll(
                tmp_db, since_id=max_id_before, timeout=3,
                poll_interval_s=0.05,
            )
        finally:
            t.join()
        assert new_cursor > max_id_before
        assert "backend" in snapshot

    def test_returns_after_timeout_even_with_no_events(self, tmp_db: Path) -> None:
        _setup(tmp_db)
        start = time.monotonic()
        cursor, _ = poll.poll(tmp_db, since_id=10_000, timeout=0.3, poll_interval_s=0.05)
        elapsed = time.monotonic() - start
        assert 0.3 <= elapsed < 1.5
```

Run: `pytest tests/test_poll.py -v` → Expected: FAIL.

- [ ] **Step 4: Implement `orchestra/poll.py`**

```python
"""orchestra poll — bounded state snapshot + blocking wait.

`poll(db, since_id, timeout)` busy-polls events.id every poll_interval_s seconds.
Returns as soon as a row with id > since_id appears, OR after `timeout` seconds.
The snapshot it returns is bounded: one row per engineer worker, plus a small
section listing pending escalations. Tool-use events are excluded from the
new-event count by default.

Caller persistence:
- The CLI stores the last seen events.id in `.orchestra/poll-cursor.<caller_id>`.
- Each call passes the previous cursor in via `since_id`.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from orchestra import state

# Events that count as "interesting" for new-event counts and chatter.
INTERESTING_KINDS = (
    "session_ready",
    "turn_complete",
    "session_ended",
    "notification",
    "status",
    "escalation",
    "escalation_resolved",
    "message_sent",
    "worktree_created",
    "worktree_reaped",
    "merge_attempted",
    "merge_ok",
    "merge_conflict",
    "hook_error",
)


def _max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0])


def _engineers(conn: sqlite3.Connection) -> list[state.Worker]:
    return [w for w in state.list_workers(conn) if w.role == "engineer"]


def _new_event_count(
    conn: sqlite3.Connection,
    worker_id: str,
    since_id: int,
    include_tools: bool,
) -> int:
    if include_tools:
        kinds_clause = ""
        args: list[Any] = [worker_id, since_id]
    else:
        placeholders = ",".join("?" * len(INTERESTING_KINDS))
        kinds_clause = f" AND kind IN ({placeholders})"
        args = [worker_id, since_id, *INTERESTING_KINDS]
    sql = (
        "SELECT COUNT(*) FROM events "
        f"WHERE worker_id = ? AND id > ?{kinds_clause}"
    )
    return int(conn.execute(sql, args).fetchone()[0])


def _last_status_for(conn: sqlite3.Connection, worker_id: str) -> str | None:
    row = conn.execute(
        "SELECT payload FROM events WHERE worker_id = ? AND kind = 'status' "
        "ORDER BY id DESC LIMIT 1",
        (worker_id,),
    ).fetchone()
    if row is None:
        return None
    import json
    try:
        return json.loads(row[0]).get("progress")
    except Exception:  # noqa: BLE001
        return None


def render_snapshot(db: Path, *, since_id: int, include_tools: bool = False) -> str:
    """Render a markdown state snapshot for engineers."""
    conn = state.connect(db)
    try:
        engineers = _engineers(conn)
        lines: list[str] = []
        lines.append("| worker | status | new events | last status |")
        lines.append("|---|---|---|---|")
        for w in engineers:
            n = _new_event_count(conn, w.id, since_id, include_tools)
            last = _last_status_for(conn, w.id) or "(none)"
            lines.append(f"| {w.id} | {w.status} | {n} | {last} |")
        escs = state.list_open_escalations(conn)
        if escs:
            lines.append("")
            lines.append("**Pending escalations:**")
            for e in escs:
                lines.append(
                    f"- #{e.id} from {e.worker_id}: {e.question}"
                    + (f" (context: {e.context})" if e.context else "")
                )
        return "\n".join(lines)
    finally:
        conn.close()


def poll(
    db: Path,
    *,
    since_id: int,
    timeout: float,
    poll_interval_s: float = 0.5,
    include_tools: bool = False,
) -> tuple[int, str]:
    """Block up to `timeout`s waiting for a new event, then return (new_max_id, snapshot)."""
    deadline = time.monotonic() + timeout
    while True:
        conn = state.connect(db)
        try:
            current_max = _max_event_id(conn)
        finally:
            conn.close()
        if current_max > since_id:
            return current_max, render_snapshot(db, since_id=since_id,
                                                include_tools=include_tools)
        if time.monotonic() >= deadline:
            return current_max, render_snapshot(db, since_id=since_id,
                                                include_tools=include_tools)
        time.sleep(min(poll_interval_s, max(0.0, deadline - time.monotonic())))
```

Run: `pytest tests/test_poll.py -v` → Expected: PASS.

- [ ] **Step 5: Write failing tests for new CLI commands**

Append to `tests/test_cli.py`:

```python
class TestSpawnFlags:
    def test_role_brief_worktree_flags_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        # Make tmp_path a git repo so worktree creation works.
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"],
                       check=True)
        (tmp_path / "README.md").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"],
                       check=True)
        (tmp_path / "brief.md").write_text("do backend stuff")
        called: dict = {}
        def fake_spawn(conn, **kw):
            called.update(kw)
        monkeypatch.setattr(cli.spawn, "spawn_worker", fake_spawn)
        result = runner.invoke(app, [
            "spawn", "backend", "sonnet", "implement",
            "--role", "engineer", "--brief", "brief.md", "--worktree", "backend",
        ])
        assert result.exit_code == 0, result.output
        assert called["role"] == "engineer"
        assert called["brief"] == "do backend stuff"
        assert called["worktree_name"] == "backend"


class TestSend:
    def test_send_records_event_and_calls_tmux(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        # Seed a worker row.
        with cli._open_db() as conn:  # type: ignore[attr-defined]
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer",
            )
        sent: list = []
        def fake_send(target, msg, **kw):
            sent.append((target, msg))
        monkeypatch.setattr(cli.tmux, "send_multiline", fake_send)
        result = runner.invoke(app, ["send", "backend", "merge conflict in app.py"])
        assert result.exit_code == 0, result.output
        assert sent == [("s:backend", "merge conflict in app.py")]
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "message_sent" in kinds


class TestAnswer:
    def test_answer_resolves_and_sends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer",
            )
            esc = state.create_escalation(
                conn, worker_id="backend",
                question="contract?", context=None, blocking=True,
            )
        sent: list = []
        monkeypatch.setattr(cli.tmux, "send_multiline",
                            lambda t, m, **k: sent.append((t, m)))
        result = runner.invoke(app, ["answer", str(esc.id), "use {code:str}"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            open_now = state.list_open_escalations(conn)
        assert open_now == []
        assert sent and sent[0][0] == "s:backend"
        assert "use {code:str}" in sent[0][1]


class TestPoll:
    def test_poll_prints_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer", worktree="backend",
            )
        result = runner.invoke(app, ["poll", "--timeout", "0.1", "--caller", "pm"])
        assert result.exit_code == 0, result.output
        assert "backend" in result.output


class TestMergeReap:
    def test_merge_records_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _init_in(tmp_path, monkeypatch)
        # Stub the git call so the test stays hermetic.
        import subprocess as _sub
        calls: list = []
        def fake_run(argv, **kw):  # noqa: ANN001
            calls.append(argv)
            return _sub.CompletedProcess(argv, 0, stdout="", stderr="")
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        with cli._open_db() as conn:
            state.create_worker(
                conn, id="backend", task="t", model="sonnet",
                branch="orch/backend", pane_target="s:backend",
                role="engineer", worktree="backend",
            )
        result = runner.invoke(app, ["merge", "backend"])
        assert result.exit_code == 0, result.output
        with cli._open_db() as conn:
            kinds = [e.kind for e in state.list_events(conn, worker_id="backend")]
        assert "merge_attempted" in kinds
        assert "merge_ok" in kinds
```

Run: `pytest tests/test_cli.py -v -k "TestSpawnFlags or TestSend or TestAnswer or TestPoll or TestMergeReap"` → Expected: FAIL.

- [ ] **Step 6: Extend `orchestra/cli.py`**

Make these changes:

a) Replace `spawn_command` (lines 84-108):

```python
@app.command("spawn")
def spawn_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
    model: str = typer.Argument(..., metavar="MODEL"),
    task: str = typer.Argument("", metavar="TASK"),
    context: list[str] = typer.Option(  # noqa: B008
        [], "--context", help="Context files."
    ),
    role: str = typer.Option("engineer", "--role"),
    brief: Path | None = typer.Option(None, "--brief"),
    worktree_name: str | None = typer.Option(None, "--worktree"),
) -> None:
    """Spawn a worker into a new tmux window."""
    project_root = str(Path.cwd())
    session_name = _session_name_for(Path.cwd())
    brief_text = brief.read_text() if brief else None
    with _open_db() as conn:
        db = _state_db()
        spawn.spawn_worker(
            conn,
            worker_id=worker_id,
            model=model,
            task=task,
            project_root=project_root,
            state_db=db,
            ctx_files=list(context),
            session_name=session_name,
            role=role,
            brief=brief_text,
            worktree_name=worktree_name,
        )
        typer.echo(f"spawn {worker_id} → {session_name}:{worker_id}")
```

b) Add new commands after `worker_escalate` (after the existing file's end):

```python
@app.command("send")
def send_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
    message: str = typer.Argument(..., metavar="MSG"),
) -> None:
    """Type MSG into the worker's pane (PM → engineer nudges)."""
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None:
            typer.echo(f"no such worker: {worker_id}", err=True)
            raise typer.Exit(2)
        tmux.send_multiline(w.pane_target, message)
        state.record_event(conn, "message_sent", worker_id=worker_id, message=message)


@app.command("answer")
def answer_command(
    escalation_id: int = typer.Argument(..., metavar="ESC_ID"),
    answer: str = typer.Argument(..., metavar="ANSWER"),
) -> None:
    """Resolve an escalation and send the answer to the asker's pane."""
    with _open_db() as conn:
        esc = state.resolve_escalation(conn, escalation_id, answer=answer)
        w = state.get_worker(conn, esc.worker_id)
        if w is not None:
            tmux.send_multiline(w.pane_target, f"[answer to #{esc.id}] {answer}")
        state.record_event(
            conn, "escalation_resolved", worker_id=esc.worker_id,
            escalation_id=esc.id, answer=answer,
        )


def _cursor_file(caller: str) -> Path:
    return _orch_dir() / f"poll-cursor.{caller}"


@app.command("poll")
def poll_command(
    timeout: float = typer.Option(30.0, "--timeout"),
    include_tools: bool = typer.Option(False, "--include-tools"),
    caller: str = typer.Option("pm", "--caller",
                                help="Caller id for cursor persistence."),
) -> None:
    """Block up to TIMEOUT seconds for new engineer events; print state snapshot."""
    from orchestra import poll as poll_mod  # local import keeps CLI cheap

    db = _require_initialized()
    cur_path = _cursor_file(caller)
    since_id = 0
    if cur_path.exists():
        try:
            since_id = int(cur_path.read_text().strip() or "0")
        except ValueError:
            since_id = 0
    new_cursor, snapshot = poll_mod.poll(
        db, since_id=since_id, timeout=timeout, include_tools=include_tools,
    )
    cur_path.write_text(str(new_cursor))
    typer.echo(snapshot)


@app.command("merge")
def merge_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
) -> None:
    """git merge orch/<worker_id> from the project root (caller's cwd)."""
    project_root = Path.cwd()
    branch = f"orch/{worker_id}"
    with _open_db() as conn:
        state.record_event(conn, "merge_attempted", worker_id=worker_id, branch=branch)
        proc = subprocess.run(
            ["git", "-C", str(project_root), "merge", "--no-edit", branch],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            state.record_event(conn, "merge_ok", worker_id=worker_id,
                               stdout=proc.stdout[-2000:])
            typer.echo(f"merged {branch}")
        else:
            state.record_event(
                conn, "merge_conflict", worker_id=worker_id,
                stdout=proc.stdout[-2000:], stderr=proc.stderr[-2000:],
            )
            typer.echo(f"merge conflict on {branch}", err=True)
            raise typer.Exit(1)


@app.command("reap")
def reap_command(
    worker_id: str = typer.Argument(..., metavar="ID"),
) -> None:
    """Remove the worker's worktree and delete its branch."""
    from orchestra import worktree as wt_mod
    with _open_db() as conn:
        w = state.get_worker(conn, worker_id)
        if w is None or w.worktree is None:
            typer.echo(f"worker {worker_id} has no worktree", err=True)
            raise typer.Exit(2)
        wt_mod.remove(Path.cwd(), name=w.worktree, worker_id=worker_id)
        state.record_event(conn, "worktree_reaped", worker_id=worker_id,
                           name=w.worktree)
```

- [ ] **Step 7: Run CLI tests**

Run: `pytest tests/test_cli.py -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add orchestra/poll.py orchestra/worktree.py orchestra/cli.py \
        tests/test_poll.py tests/test_worktree.py tests/test_cli.py
git commit -m "feat(cli): add send/answer/poll/merge/reap + spawn flags

- spawn gains --role, --brief PATH, --worktree NAME (forwarded to
  spawn_worker; consumed in Task 6).
- send: types into worker pane, records message_sent.
- answer: resolves escalation, sends answer to asker, records resolved.
- poll: bounded state snapshot, blocks up to --timeout seconds, cursor
  per --caller stored under .orchestra/poll-cursor.<caller>.
- merge/reap: thin wrappers over git merge + git worktree remove with
  event recording.
- New modules: orchestra.poll, orchestra.worktree."
```

---

## Task 5: Role-aware prompt templates — PM + Engineer

**Goal:** New `orchestra/role_prompts.py` with `render_pm_prompt` and `render_engineer_prompt`. The PM prompt enforces mega-turn discipline + phase status writes + `/compact` advisory. Engineer prompt enforces worktree-only writes + escalate-on-uncertainty. The existing `orchestra/prompts.py` stays untouched (used when `--role` is absent — preserves v0 behaviour).

**Files:**
- Create: `orchestra/role_prompts.py`
- Create: `tests/test_role_prompts.py`

**Acceptance Criteria:**
- [ ] `render_pm_prompt(*, mission, worker_id, project_name, engineer_specs, verifier_block)` returns a string containing: mission text verbatim, the seven RULES bullets from the spec, the verifier block, "GO" trailer
- [ ] `render_engineer_prompt(*, worker_id, cwd, branch, brief_path, brief_content=None)` returns a string with: worker id, cwd, branch, brief reference, COORDINATION block, three RULES bullets
- [ ] Tests assert each required directive (substring match) is present

**Verify:** `pytest tests/test_role_prompts.py -v`

### Steps

- [ ] **Step 1: Write failing tests**

`tests/test_role_prompts.py`:

```python
"""Tests for orchestra.role_prompts (PM + Engineer templates)."""
from __future__ import annotations

from orchestra import role_prompts


PM_REQUIRED_SUBSTRINGS = [
    "ROLE: Project Manager",
    "MISSION",
    "YOUR TEAM",
    "orchestra spawn",
    "orchestra poll",
    "orchestra answer",
    "orchestra merge",
    "Stay in one turn",  # mega-turn rule
    "phase:",            # phase status-write requirement
    "/compact",
    "VERIFIER",
    "GO",
]

ENGINEER_REQUIRED_SUBSTRINGS = [
    "ROLE: Engineer",
    "Worker ID",
    "Workspace",
    "your own git worktree",
    "orchestra worker escalate",
    "Stay in",  # cwd-only rule
    "Do not spawn workers",
    "Tests live",
]


class TestPMPrompt:
    def test_required_directives_present(self) -> None:
        out = role_prompts.render_pm_prompt(
            mission="Build a URL shortener.",
            worker_id="pm",
            project_name="urlshortener",
            engineer_specs=[
                ("backend", "sonnet", "implements the FastAPI app, SQLite, tests"),
                ("frontend", "sonnet", "implements templates/index.html + static/style.css"),
            ],
            verifier_block="pytest -q && curl ...",
        )
        for s in PM_REQUIRED_SUBSTRINGS:
            assert s in out, f"missing: {s!r}\n---\n{out}\n---"
        # Engineer names appear:
        assert "backend" in out
        assert "frontend" in out


class TestEngineerPrompt:
    def test_required_directives_present(self) -> None:
        out = role_prompts.render_engineer_prompt(
            worker_id="backend",
            cwd="/tmp/proj/worktrees/backend",
            branch="orch/backend",
            brief_path=".orchestra/briefs/backend.md",
            brief_content=None,
        )
        for s in ENGINEER_REQUIRED_SUBSTRINGS:
            assert s in out, f"missing: {s!r}\n---\n{out}\n---"

    def test_inlined_brief_when_no_path(self) -> None:
        out = role_prompts.render_engineer_prompt(
            worker_id="backend",
            cwd="/tmp/proj",
            branch="orch/backend",
            brief_path=None,
            brief_content="Implement /shorten endpoint.",
        )
        assert "Implement /shorten" in out
```

Run: `pytest tests/test_role_prompts.py -v` → Expected: FAIL (module missing).

- [ ] **Step 2: Implement `orchestra/role_prompts.py`**

```python
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

from typing import Sequence


def render_pm_prompt(
    *,
    mission: str,
    worker_id: str,
    project_name: str,
    engineer_specs: Sequence[tuple[str, str, str]],  # (id, model, brief)
    verifier_block: str,
) -> str:
    team = "\n".join(
        f"- `{eid}` ({model}) — {brief}" for eid, model, brief in engineer_specs
    )
    return f"""## ROLE: Project Manager
Project: {project_name}
Worker ID: {worker_id}

### MISSION
{mission}

### YOUR TEAM
You will spawn and coordinate these engineers:
{team}

### TOOLS YOU CAN USE
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
- When you finish, leave a final status message:
    orchestra worker status --progress "DONE: <summary>" --turns <N>
  Then end your session (let Claude finish naturally — your SessionEnd
  hook will mark you done in the DB).

### RULES
- Stay in {cwd}. Do not touch files outside your worktree.
- Do not spawn workers.
- Tests live in your worktree. Run them before declaring DONE.
"""
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_role_prompts.py -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add orchestra/role_prompts.py tests/test_role_prompts.py
git commit -m "feat(prompts): role-aware PM + Engineer templates

PM template enforces the mega-turn discipline (don't end your turn
until done) and the phase-status-write requirement that feeds the
e2e activity watchdog. Engineer template enforces worktree-only
writes and escalate-on-uncertainty.

The v0 single-role template (orchestra.prompts.render_startup_prompt)
is preserved for non-role spawns."
```

---

## Task 6: Refactor spawn to wait on hook events instead of polling

**Goal:** Replace `_wait_idle`'s pane-polling loop with "block until a `session_ready` event for this worker_id appears in state.db, max 60s" and `_wait_first_status` with the same shape against `turn_complete`. Trust-prompt dismissal stays (must fire before SessionStart). Spawn signature gains `role`, `brief`, `worktree_name`; worktree creation runs before window creation when `--worktree` is set. Role flag selects PM vs Engineer prompt renderer.

**Files:**
- Modify: `orchestra/spawn.py`
- Modify: `tests/test_spawn.py`

**Acceptance Criteria:**
- [ ] `_wait_idle_via_event(conn, worker_id, timeout=BOOT_TIMEOUT_S)` returns True when a `session_ready` event for that worker appears; trust-prompt dismissal still runs in parallel (capture-pane based)
- [ ] `_wait_first_status_via_event(conn, worker_id, timeout=FIRST_STATUS_TIMEOUT_S)` waits for `turn_complete` rather than the v0 `status` event
- [ ] `spawn_worker(..., role='pm'|'engineer', brief=str|None, worktree_name=str|None)` is the new signature; v0 callers without those kwargs still work because all three have defaults
- [ ] When `worktree_name` is set, `orchestra.worktree.add` runs before window creation; cwd passed to `new_window` is the worktree path; the worker's `worktree` column is set
- [ ] When `role='pm'`, `role_prompts.render_pm_prompt` is used; when `role='engineer'`, `role_prompts.render_engineer_prompt` is used; default (no `role` passed) keeps `prompts.render_startup_prompt` (v0 path)
- [ ] Unit tests inject events into the DB mid-wait to assert spawn proceeds

**Verify:** `pytest tests/test_spawn.py -v`

### Steps

- [ ] **Step 1: Write failing tests**

Append to `tests/test_spawn.py` (skim the existing file first to understand the mock patterns used). Add:

```python
class TestEventDrivenWaits:
    def test_wait_idle_returns_true_when_session_ready_event_arrives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        # No event yet — first call times out fast.
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 0.2)
        monkeypatch.setattr(spawn, "BOOT_POLL_S", 0.05)
        assert spawn._wait_idle_via_event(conn, "w1") is False

        # Now insert the event and try again — must succeed.
        state.record_event(conn, "session_ready", worker_id="w1")
        monkeypatch.setattr(spawn, "BOOT_TIMEOUT_S", 1.0)
        assert spawn._wait_idle_via_event(conn, "w1") is True

    def test_wait_first_status_uses_turn_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        state.create_worker(
            conn, id="w1", task="t", model="sonnet",
            branch="orch/w1", pane_target="s:1",
        )
        monkeypatch.setattr(spawn, "FIRST_STATUS_TIMEOUT_S", 0.2)
        monkeypatch.setattr(spawn, "FIRST_STATUS_POLL_S", 0.05)
        # Cooperative `status` from worker_status command must NOT count.
        state.record_event(conn, "status", worker_id="w1")
        assert spawn._wait_first_status_via_event(conn, "w1") is False
        # turn_complete must count.
        state.record_event(conn, "turn_complete", worker_id="w1")
        monkeypatch.setattr(spawn, "FIRST_STATUS_TIMEOUT_S", 1.0)
        assert spawn._wait_first_status_via_event(conn, "w1") is True


class TestSpawnRoleSwitching:
    def test_pm_role_uses_pm_renderer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mock everything except the renderer-selection branch.
        from orchestra import role_prompts
        called = {"which": None}
        monkeypatch.setattr(
            role_prompts, "render_pm_prompt",
            lambda **kw: (called.__setitem__("which", "pm") or "PM PROMPT"),
        )
        monkeypatch.setattr(
            role_prompts, "render_engineer_prompt",
            lambda **kw: (called.__setitem__("which", "eng") or "ENG PROMPT"),
        )
        # Stub out everything else.
        for fn, name in [
            ("ensure_session", None), ("new_window", "s:pm"),
            ("send_literal", None), ("send_enter", None),
            ("send_multiline", None), ("capture", "❯ "),
            ("is_idle", True),
        ]:
            monkeypatch.setattr(tmux, fn, lambda *a, _r=name, **kw: _r)
        monkeypatch.setattr(spawn, "_wait_idle_via_event", lambda *a, **k: True)
        monkeypatch.setattr(spawn, "_wait_first_status_via_event",
                            lambda *a, **k: True)
        db = tmp_path / "state.db"
        conn = state.connect(db)
        state.init_schema(conn)
        spawn.spawn_worker(
            conn, worker_id="pm", model="opus", task="lead",
            project_root=str(tmp_path), state_db=db, ctx_files=[],
            session_name="orch-x", role="pm",
            brief="MISSION BODY", worktree_name=None,
        )
        assert called["which"] == "pm"
```

(Adapt imports — `from orchestra import spawn, state, tmux` and pathlib `Path` likely already there.)

Run: `pytest tests/test_spawn.py::TestEventDrivenWaits tests/test_spawn.py::TestSpawnRoleSwitching -v`
Expected: FAIL (functions / kwargs missing).

- [ ] **Step 2: Refactor `orchestra/spawn.py`**

Replace the file's `_wait_idle` and `_wait_first_status` helpers and the `spawn_worker` body. Keep the trust-prompt dismissal capture-pane logic.

```python
"""Worker spawn choreography (v1).

v1 changes vs v0:
- The two polling waits (_wait_idle, _wait_first_status) are replaced by
  hook-event waits: spawn watches state.db for session_ready and
  turn_complete events for the worker_id, max BOOT_TIMEOUT_S /
  FIRST_STATUS_TIMEOUT_S seconds.
- The trust-prompt dismissal still runs in parallel (capture-pane based)
  because it fires BEFORE Claude reaches the point of emitting SessionStart.
- New kwargs: role ('engineer'|'pm'|None), brief (markdown body for
  engineers; mission body for PMs — caller chooses what to inject),
  worktree_name (when set, a git worktree is created under
  <project_root>/worktrees/<name> on branch orch/<worker_id>, and the
  spawn uses that path as cwd).
- When role is set, role_prompts.render_pm_prompt /
  render_engineer_prompt is used instead of the v0 single-role template.
"""
from __future__ import annotations

import shlex
import sqlite3
import time
from pathlib import Path
from time import monotonic

from orchestra import prompts, role_prompts, state, tmux, worktree as worktree_mod

BOOT_TIMEOUT_S = 60
BOOT_POLL_S = 1.0
FIRST_STATUS_TIMEOUT_S = 90
FIRST_STATUS_POLL_S = 1.0


_TRUST_PROMPT_MARKERS = (
    "trust this folder",
    "Is this a project you created",
    "Yes, I trust",
)


def _boot_command(worker_id: str, state_db: Path) -> str:
    return (
        f"ORCHESTRA_WORKER_ID={shlex.quote(worker_id)} "
        f"ORCHESTRA_STATE_DB={shlex.quote(str(state_db))} "
        f"claude --dangerously-skip-permissions"
    )


def _has_event(
    conn: sqlite3.Connection, *, worker_id: str, kind: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE worker_id = ? AND kind = ? LIMIT 1",
        (worker_id, kind),
    ).fetchone()
    return row is not None


def _wait_idle_via_event(
    conn: sqlite3.Connection, worker_id: str,
    *, target: str | None = None,
) -> bool:
    """Block until a session_ready event appears, OR the trust prompt needs
    dismissing, OR BOOT_TIMEOUT_S elapses.

    If `target` is provided, we capture the pane in parallel to dismiss the
    trust prompt (it fires before SessionStart, so we can't rely on hooks).
    """
    trust_handled = False
    deadline = monotonic() + BOOT_TIMEOUT_S
    while monotonic() < deadline:
        if _has_event(conn, worker_id=worker_id, kind="session_ready"):
            return True
        if target is not None and not trust_handled:
            cap = tmux.capture(target, lines=30)
            if any(marker in cap for marker in _TRUST_PROMPT_MARKERS):
                tmux.send_enter(target)
                state.record_event(conn, "spawn_trust_accepted", worker_id=worker_id)
                trust_handled = True
                time.sleep(2.0)
                continue
        time.sleep(BOOT_POLL_S)
    return False


def _wait_first_status_via_event(
    conn: sqlite3.Connection, worker_id: str
) -> bool:
    """Block until the first turn_complete event for worker_id, max FIRST_STATUS_TIMEOUT_S."""
    deadline = monotonic() + FIRST_STATUS_TIMEOUT_S
    while monotonic() < deadline:
        if _has_event(conn, worker_id=worker_id, kind="turn_complete"):
            return True
        time.sleep(FIRST_STATUS_POLL_S)
    return False


def _render_startup_prompt(
    *,
    role: str | None,
    worker_id: str,
    model: str,
    task: str,
    ctx_files: list[str],
    brief: str | None,
    cwd: str,
    branch: str,
) -> str:
    if role == "pm":
        return role_prompts.render_pm_prompt(
            mission=brief or task,
            worker_id=worker_id,
            project_name=Path(cwd).name,
            engineer_specs=[],  # PM authors briefs itself; the mission lists the team
            verifier_block="(see mission for verifier)",
        )
    if role == "engineer":
        return role_prompts.render_engineer_prompt(
            worker_id=worker_id,
            cwd=cwd,
            branch=branch,
            brief_path=None,
            brief_content=brief,
        )
    # v0 fallback
    return prompts.render_startup_prompt(
        worker_id=worker_id, task=task, model=model, ctx_files=ctx_files,
    )


def spawn_worker(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    model: str,
    task: str,
    project_root: str,
    state_db: Path,
    ctx_files: list[str],
    session_name: str,
    role: str | None = None,
    brief: str | None = None,
    worktree_name: str | None = None,
) -> None:
    branch = f"orch/{worker_id}"
    pane_target = f"{session_name}:{worker_id}"

    # Pre-step: worktree (engineers only — PMs work in the main checkout)
    cwd = project_root
    if worktree_name is not None:
        wt = worktree_mod.add(Path(project_root), name=worktree_name, worker_id=worker_id)
        cwd = str(wt)
        state.record_event(conn, "worktree_created", worker_id=worker_id,
                           name=worktree_name, path=str(wt))

    # Step 1: worker row
    state.create_worker(
        conn, id=worker_id, task=task, model=model,
        branch=branch, pane_target=pane_target,
        role=role or "engineer",
        worktree=worktree_name,
    )
    state.record_event(conn, "spawn_start", worker_id=worker_id, task=task, model=model,
                       role=role, worktree=worktree_name)

    # Step 2: tmux session + window
    tmux.ensure_session(session_name, cwd=cwd)
    target = tmux.new_window(session=session_name, name=worker_id, cwd=cwd)
    state.record_event(conn, "spawn_window", worker_id=worker_id, target=target)

    # Step 3: boot claude
    tmux.send_literal(target, _boot_command(worker_id, state_db))
    tmux.send_enter(target)

    # Step 4: wait for SessionStart hook (session_ready event). Trust-prompt
    # dismissal runs in parallel because it fires BEFORE SessionStart.
    if not _wait_idle_via_event(conn, worker_id, target=target):
        last_screen = tmux.capture(target, lines=20)
        state.record_event(
            conn, "spawn_timeout", worker_id=worker_id, last_screen=last_screen,
        )
        state.update_worker(conn, worker_id, status="error")
        return
    state.record_event(conn, "spawn_idle", worker_id=worker_id)

    # Step 5: switch model
    tmux.send_literal(target, f"/{model}")
    tmux.send_enter(target)
    time.sleep(3.0)
    state.record_event(conn, "model_switched", worker_id=worker_id, model=model)

    # Step 6: inject startup prompt
    startup = _render_startup_prompt(
        role=role, worker_id=worker_id, model=model, task=task,
        ctx_files=ctx_files, brief=brief, cwd=cwd, branch=branch,
    )
    inject_ok = False
    for attempt in (1, 2):
        try:
            tmux.send_multiline(target, startup, buffer_name=f"orch-{worker_id}")
            inject_ok = True
            break
        except Exception as e:  # noqa: BLE001
            state.record_event(
                conn, "prompt_inject_retry",
                worker_id=worker_id, attempt=attempt, error=repr(e),
            )
            time.sleep(1.0)
    if not inject_ok:
        state.record_event(conn, "prompt_inject_failed", worker_id=worker_id)
        state.update_worker(conn, worker_id, status="error")
        return
    state.record_event(conn, "prompt_injected", worker_id=worker_id)

    # Step 7: wait for first turn_complete event (proof of life).
    if _wait_first_status_via_event(conn, worker_id):
        state.record_event(conn, "spawn_ok", worker_id=worker_id)
        state.update_worker(conn, worker_id, status="working")
    else:
        state.update_worker(conn, worker_id, status="stale_spawn")
        state.record_event(conn, "spawn_first_status_timeout", worker_id=worker_id)
```

- [ ] **Step 3: Run all existing spawn tests + new tests**

Run: `pytest tests/test_spawn.py -v`
Expected: all green. Older tests that exercised the polling-based `_wait_idle`/`_wait_first_status` may need to be updated — the orchestra layer's public behaviour (final status, event sequence) is what matters, not the internal helper names. If tests assert exact helper names, rewrite them to assert the externally-observable event sequence (spawn_start → spawn_window → spawn_idle → model_switched → prompt_injected → spawn_ok) and status transitions.

- [ ] **Step 4: Run the full suite**

Run: `pytest -v`
Expected: green across the board.

- [ ] **Step 5: Commit**

```bash
git add orchestra/spawn.py tests/test_spawn.py
git commit -m "feat(spawn): wait on hook events; add role/brief/worktree

_wait_idle becomes _wait_idle_via_event watching for session_ready;
_wait_first_status becomes _wait_first_status_via_event watching for
turn_complete (NOT the cooperative 'status' kind — that has a separate
purpose).

spawn_worker gains role, brief, worktree_name kwargs. When worktree_name
is set, orchestra.worktree.add runs before window creation and the
worker's cwd is the worktree path. When role is set, the matching
PM/Engineer template renders the startup prompt; role=None preserves
v0 behaviour."
```

---

## Task 7: E2e fixture — URL shortener mission + driver + 3 watchdogs

**Goal:** The v1 acceptance test. `scripts/e2e-build-urlshortener.sh` cleans worktrees, runs `orchestra init`, spawns the PM with the URL-shortener mission, runs three watchdogs (wall-clock 90 min, activity 10 min, cost `MAX_BUDGET_USD` default $10), and exits 0 iff the verifier passes.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `scripts/e2e-build-urlshortener.sh`
- Create: `examples/urlshortener-mission.md`
- Create: `examples/urlshortener-verifier.sh`

**Acceptance Criteria:**
- [ ] `scripts/e2e-build-urlshortener.sh` is idempotent: drops leftover worktrees, deletes orphan `orch/*` branches, recreates the project dir
- [ ] Wall-clock watchdog kills the run after 90 min, exits non-zero
- [ ] Activity watchdog tails `events.id`; if no new row for 10 min, aborts with a distinct code
- [ ] Cost watchdog sums token counts from `turn_complete` payloads × pricing; aborts if total > `MAX_BUDGET_USD`
- [ ] On verifier success (and PM `worker.status == 'done'`), script prints a final state.db summary and exits 0
- [ ] Manual / opt-in (consumes API credits + needs authenticated claude); documented as "not in CI"

**Verify:** Manual run on a real authenticated `claude`. A passing run is the v1 contract; no green CI gate here.

### Steps

- [ ] **Step 1: Write the mission file**

`examples/urlshortener-mission.md`:

```markdown
# Mission: URL shortener web app

Build a small FastAPI web app that shortens URLs.

## Acceptance
- `pytest` passes from the project root.
- `uvicorn app:app --port 8765` starts the server.
- `curl -X POST localhost:8765/shorten -H 'content-type: application/json' -d '{"url":"https://example.com"}'`
  returns HTTP 200 with a JSON body `{"code":"<short>"}`.
- `curl -I localhost:8765/<short>` returns HTTP 302 with `Location: https://example.com`.
- `curl localhost:8765/` returns an HTML page with a form posting to `/shorten`.

## Team
Spawn two engineers in their own worktrees:
- `backend` (sonnet) — implements the FastAPI app, SQLite storage, and tests.
- `frontend` (sonnet) — implements the HTML form page and any static assets.

You mediate the API contract. You merge their work into main. You run the
acceptance checks. You only mark yourself done when all four acceptance
checks pass.

The verifier script is at `examples/urlshortener-verifier.sh`.
```

- [ ] **Step 2: Write the verifier script**

`examples/urlshortener-verifier.sh`:

```bash
#!/usr/bin/env bash
# Verifier for the URL-shortener mission. Run from project root.
set -e

PROJECT_ROOT="${PROJECT_ROOT:-$PWD}"
PORT="${PORT:-8765}"

( cd "$PROJECT_ROOT" && pytest -q ) || { echo "VERIFIER: pytest failed"; exit 1; }

( cd "$PROJECT_ROOT" && uvicorn app:app --port "$PORT" ) &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null || true" EXIT
sleep 2

CODE=$(curl -fs -X POST "localhost:$PORT/shorten" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])') || {
    echo "VERIFIER: POST /shorten failed"; exit 2; }
test -n "$CODE" || { echo "VERIFIER: empty code"; exit 2; }

curl -fsI "localhost:$PORT/$CODE" | grep -q '^location: https://example.com' \
  || { echo "VERIFIER: GET /<code> did not 302 to example.com"; exit 3; }

curl -fs "localhost:$PORT/" | grep -q '<form' \
  || { echo "VERIFIER: GET / had no <form>"; exit 4; }

echo "VERIFIER OK code=$CODE"
```

Make it executable: `chmod +x examples/urlshortener-verifier.sh`.

- [ ] **Step 3: Write the e2e driver script**

`scripts/e2e-build-urlshortener.sh`:

```bash
#!/usr/bin/env bash
# scripts/e2e-build-urlshortener.sh — claude-orchestra v1 acceptance test.
#
# Spawns a PM + two engineers and waits for them to autonomously build a
# URL shortener web app. Three watchdogs (wall-clock, activity, cost)
# bound the run. Exits 0 only if the PM marks itself done AND the verifier
# passes.
#
# Requires: claude CLI authenticated, tmux, orchestra installed.
# Consumes API credits. NOT in CI.

set -euo pipefail

WALL_CLOCK_SECS="${WALL_CLOCK_SECS:-5400}"        # 90 min
ACTIVITY_TIMEOUT_SECS="${ACTIVITY_TIMEOUT_SECS:-600}"  # 10 min
MAX_BUDGET_USD="${MAX_BUDGET_USD:-10.00}"
PROJECT_DIR="${PROJECT_DIR:-/tmp/orch-urlshortener}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

for bin in claude orchestra tmux jq python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "FAIL: $bin not in PATH" >&2; exit 2; }
done

# --- Cleanup any leftovers from a prior run -----------------------------
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

# --- Initialise the target repo ----------------------------------------
( cd "$PROJECT_DIR" \
  && git init -q -b main \
  && git config user.email "orch@local" \
  && git config user.name "orch" \
  && cp "$REPO_ROOT/examples/urlshortener-verifier.sh" verifier.sh \
  && chmod +x verifier.sh \
  && git add verifier.sh \
  && git commit -q -m "seed: verifier" )

( cd "$PROJECT_DIR" && orchestra init )

# --- Watchdogs --------------------------------------------------------
DB="$PROJECT_DIR/.orchestra/state.db"
WATCHDOG_LOG="$PROJECT_DIR/.orchestra/watchdog.log"
RESULT_FILE="$PROJECT_DIR/.orchestra/e2e-result"

_query() { sqlite3 "$DB" "$@"; }

_token_cost_usd() {
  # Crude per-million-token pricing; update as needed. Reads input/output
  # tokens from turn_complete payloads.
  python3 - "$DB" <<'PY'
import json, sqlite3, sys
db = sys.argv[1]
rates = {
    # rough Anthropic public prices ($ per million tokens) — accuracy
    # matters less than HAVING a bound; tune if it bites.
    "opus":   {"in": 15.00, "out": 75.00},
    "sonnet": {"in":  3.00, "out": 15.00},
    "haiku":  {"in":  1.00, "out":  5.00},
}
conn = sqlite3.connect(db)
total = 0.0
for (worker_id, kind, payload) in conn.execute(
    "SELECT worker_id, kind, payload FROM events WHERE kind = 'turn_complete'"
):
    try:
        p = json.loads(payload)
    except Exception:
        continue
    inp = int(p.get("input_tokens") or 0)
    out = int(p.get("output_tokens") or 0)
    # Find model from workers table.
    row = conn.execute(
        "SELECT model FROM workers WHERE id = ?", (worker_id,)
    ).fetchone()
    if row is None: continue
    model = row[0].lower()
    rate = rates.get(model) or rates["sonnet"]
    total += inp / 1_000_000 * rate["in"]
    total += out / 1_000_000 * rate["out"]
print(f"{total:.4f}")
PY
}

run_watchdog() {
  local start=$(date +%s)
  local last_max_id=0
  local last_event_seen=$(date +%s)
  while sleep 30; do
    local now=$(date +%s)
    local elapsed=$(( now - start ))
    # Wall-clock
    if (( elapsed > WALL_CLOCK_SECS )); then
      echo "WATCHDOG: wall-clock $WALL_CLOCK_SECS elapsed" | tee -a "$WATCHDOG_LOG"
      echo "wall_clock" > "$RESULT_FILE"; return 124
    fi
    # Activity
    local current_max
    current_max=$(_query "SELECT COALESCE(MAX(id),0) FROM events" 2>/dev/null || echo 0)
    if (( current_max > last_max_id )); then
      last_max_id=$current_max; last_event_seen=$now
    elif (( now - last_event_seen > ACTIVITY_TIMEOUT_SECS )); then
      echo "WATCHDOG: no events for ${ACTIVITY_TIMEOUT_SECS}s" | tee -a "$WATCHDOG_LOG"
      echo "activity" > "$RESULT_FILE"; return 125
    fi
    # Cost
    local cost
    cost=$(_token_cost_usd)
    if (( $(echo "$cost > $MAX_BUDGET_USD" | bc -l) )); then
      echo "WATCHDOG: cost \$$cost > \$$MAX_BUDGET_USD" | tee -a "$WATCHDOG_LOG"
      echo "cost" > "$RESULT_FILE"; return 126
    fi
    # PM done?
    local pm_status
    pm_status=$(_query "SELECT status FROM workers WHERE id='pm'" 2>/dev/null || echo "")
    if [[ "$pm_status" == "done" ]]; then
      echo "WATCHDOG: PM reports done" | tee -a "$WATCHDOG_LOG"
      echo "pm_done" > "$RESULT_FILE"; return 0
    fi
  done
}

run_watchdog &
WATCHDOG_PID=$!

# --- Kick off the PM --------------------------------------------------
cd "$PROJECT_DIR"
orchestra spawn pm opus "$(cat "$REPO_ROOT/examples/urlshortener-mission.md")" \
  --role pm \
  --brief "$REPO_ROOT/examples/urlshortener-mission.md"

# --- Wait for watchdog to decide --------------------------------------
wait "$WATCHDOG_PID" || true
RESULT=$(cat "$RESULT_FILE" 2>/dev/null || echo "unknown")

# --- Final summary ----------------------------------------------------
echo
echo "==================== e2e summary ===================="
echo "Project dir: $PROJECT_DIR"
echo "Watchdog result: $RESULT"
echo "Final cost: \$$(_token_cost_usd)"
echo "Worker final states:"
_query "SELECT id, role, status, turns FROM workers" \
  | column -ts'|' -N 'id,role,status,turns' || true
echo "Recent events (last 20):"
_query ".headers on" "SELECT id, worker_id, ts, kind FROM events ORDER BY id DESC LIMIT 20" \
  | column -ts'|' || true

# --- Final acceptance gate -------------------------------------------
if [[ "$RESULT" == "pm_done" ]]; then
  if ( cd "$PROJECT_DIR" && bash verifier.sh ); then
    echo "[e2e] PASS"
    exit 0
  else
    echo "[e2e] FAIL: PM reported done but verifier failed"
    exit 10
  fi
else
  echo "[e2e] FAIL: watchdog tripped ($RESULT)"
  case "$RESULT" in
    wall_clock) exit 124 ;;
    activity)   exit 125 ;;
    cost)       exit 126 ;;
    *)          exit 1 ;;
  esac
fi
```

Make it executable: `chmod +x scripts/e2e-build-urlshortener.sh`.

- [ ] **Step 4: Sanity-check the driver with `bash -n`**

Run: `bash -n scripts/e2e-build-urlshortener.sh`
Expected: no output (syntax OK).

Same for the verifier: `bash -n examples/urlshortener-verifier.sh`.

- [ ] **Step 5: Manual e2e run**

This step is opt-in and consumes API credits. Do not run from CI.

```bash
./scripts/e2e-build-urlshortener.sh
```

Expected: within 45-90 min, exit 0 with `[e2e] PASS`. The final summary block shows two engineers in `status=done`, the PM in `status=done`, and a verifier "VERIFIER OK code=..." line.

If it fails:
- `cost` exit means the budget guard tripped — investigate by reading `.orchestra/state.db` events.
- `activity` exit means the PM or an engineer wedged — read `.orchestra/hook-debug.log` if present, plus the tmux panes.
- `wall_clock` exit means the run went over time. Bump `WALL_CLOCK_SECS` for a longer attempt or treat as a real failure if the PM is making no progress.

- [ ] **Step 6: Commit**

```bash
git add scripts/e2e-build-urlshortener.sh examples/urlshortener-mission.md \
        examples/urlshortener-verifier.sh
chmod +x scripts/e2e-build-urlshortener.sh examples/urlshortener-verifier.sh
git commit -m "feat(e2e): URL-shortener acceptance test + 3 watchdogs

scripts/e2e-build-urlshortener.sh is the v1 acceptance contract:
- Cleans any prior worktrees + orch/* branches.
- Initialises a fresh project, runs orchestra init, copies the verifier in.
- Spawns the PM with --role pm + the URL-shortener mission.
- Background watchdog enforces wall-clock (90m), activity (10m no events),
  and cost (\$MAX_BUDGET_USD, default \$10) bounds.
- On PM-done, runs the verifier; exits 0 only if it passes.

Mission file (examples/urlshortener-mission.md) is the PM's brief.
Verifier (examples/urlshortener-verifier.sh) is what the PM runs at the
end — pytest + uvicorn + curl-shaped acceptance probes."
```

---

## Spec coverage check

| Spec section / requirement | Implemented by |
|---|---|
| Hook-based detection (6 events) | Task 1 (spike) + Task 2 (typed dispatch) |
| `settings.local.json` deep-merge at init | Task 2 |
| `_wait_idle` / `_wait_first_status` rewritten as event waits | Task 6 |
| Multi-worker spawn (PM spawns engineers) | Task 4 (CLI) + Task 6 (spawn behaviour) |
| Worktree per worker | Task 4 (`orchestra.worktree`) + Task 6 (creation step) |
| `orchestra send`, `answer`, `poll`, `merge`, `reap` | Task 4 |
| Mega-turn PM + state-snapshot `poll` | Task 4 (`orchestra.poll`) + Task 5 (PM prompt rules) |
| `role`, `worktree` columns; new event kinds | Task 3 |
| `turn_complete` carries token usage | Task 2 (`_extract_token_usage`) |
| Activity + cost + wall-clock watchdogs | Task 7 |
| URL-shortener mission + verifier | Task 7 |
| PM never ends its turn; emits phase status writes | Task 5 (PM prompt rules) |
