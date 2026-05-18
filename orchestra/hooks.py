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

import contextlib
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
    with (d / name).open("a") as fh:
        fh.write(line.rstrip("\n") + "\n")


# --- Spike fallback (Task 1) ---

def run_spike(event: str, *, stdin_text: str) -> int:
    """Log-only hook implementation. Returns the exit code (always 0)."""
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

def _zero_usage() -> dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0}


def _usage_from_dict(usage: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
    }


def _usage_from_transcript(transcript_path: str) -> dict[str, int] | None:
    """Stream a Claude Code transcript JSONL and return the LAST assistant
    turn's usage. Returns None if the file is missing/unreadable or has no
    assistant-with-usage lines, so callers can fall back to zeros.

    Claude Code writes cumulative usage on the final assistant message of
    each turn, so the last `message.usage` line is the right one to bill.
    """
    p = Path(transcript_path)
    if not p.is_file():
        return None
    last_usage: dict[str, Any] | None = None
    try:
        with p.open() as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue
                # Only assistant messages carry usage worth billing.
                if entry.get("type") != "assistant" and msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
    except OSError:
        return None
    if last_usage is None:
        return None
    return _usage_from_dict(last_usage)


def _extract_token_usage(payload: dict[str, Any]) -> dict[str, int]:
    """Pull token counts out of a Stop-hook payload.

    Claude Code's Stop payload does not embed usage directly; it carries
    `transcript_path`, an absolute path to a JSONL transcript whose last
    assistant line holds the cumulative usage for the turn. We try that
    first, then fall back to the legacy direct-`usage` shape used by older
    fixtures, then to zeros. Never raises — hook handlers MUST exit 0.
    """
    if not isinstance(payload, dict):
        return _zero_usage()
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        usage = _usage_from_transcript(transcript_path)
        if usage is not None:
            return usage
    usage_field = payload.get("usage")
    if isinstance(usage_field, dict):
        return _usage_from_dict(usage_field)
    # Legacy shape: token fields top-level on the payload itself.
    if any(k in payload for k in (
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    )):
        return _usage_from_dict(payload)
    return _zero_usage()


def _handle(event: str, payload: dict[str, Any], conn: Any, wid: str) -> None:
    if event == "SessionStart":
        w = state.get_worker(conn, wid)
        if w is not None and w.status == "done":
            # Worker already signalled cooperative completion (orchestra worker done).
            # A re-entry here would corrupt the final status row — see issue #2.
            state.record_event(conn, "done_to_working_blocked", worker_id=wid,
                               session_id=payload.get("session_id"))
        else:
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
        if w is not None and w.status not in ("error", "stopped", "stop_send_failed", "done"):
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
        # Temporary diagnostic: append raw payload so we can verify the Stop-hook
        # payload schema from real runs. Remove once _extract_token_usage is confirmed.
        _append_log(
            "hook-debug.log",
            json.dumps({"ts": _now(), "event": event, "worker_id": wid, "payload": payload}),
        )
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
            with contextlib.suppress(Exception):
                conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: reads event name from argv, payload from stdin."""
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        # Defensive: a malformed invocation must not break the worker turn —
        # the hook contract requires exit 0.
        return 0
    event = argv[0]
    try:
        stdin_text = sys.stdin.read()
    except Exception:  # noqa: BLE001 — any read error must not break the turn
        stdin_text = ""
    return dispatch(event, stdin_text=stdin_text)
