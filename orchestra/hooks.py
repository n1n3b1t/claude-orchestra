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
