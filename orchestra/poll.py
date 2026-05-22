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

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from orchestra import cost as cost_mod
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
    "worker_done",
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


def _token_summary_for(conn: sqlite3.Connection, worker_id: str) -> tuple[int, int, int]:
    """Return (input_tokens, output_tokens, cache_read_tokens) summed across turn_complete."""
    total_in = total_out = total_cache = 0
    rows = conn.execute(
        "SELECT payload FROM events WHERE worker_id = ? AND kind = 'turn_complete'",
        (worker_id,),
    ).fetchall()
    for (payload_raw,) in rows:
        try:
            p = json.loads(payload_raw) if payload_raw else {}
        except Exception:  # noqa: BLE001
            continue
        total_in += int(p.get("input_tokens") or 0)
        total_out += int(p.get("output_tokens") or 0)
        total_cache += int(p.get("cache_read_tokens") or 0)
    return total_in, total_out, total_cache


def _cost_usd_for(conn: sqlite3.Connection, worker_id: str, fallback_model: str) -> float:
    """Sum turn_complete token costs for a worker.

    Per-turn payload model wins over the worker's spawn-time model so that
    mid-run /model switches are billed at the right rate.
    """
    total = 0.0
    rows = conn.execute(
        "SELECT payload FROM events WHERE worker_id = ? AND kind = 'turn_complete'",
        (worker_id,),
    ).fetchall()
    for (payload_raw,) in rows:
        try:
            p = json.loads(payload_raw) if payload_raw else {}
        except Exception:  # noqa: BLE001
            continue
        inp = int(p.get("input_tokens") or 0)
        out = int(p.get("output_tokens") or 0)
        model_id = p.get("model") or fallback_model
        total += cost_mod.cost_for(model_id, inp, out)
    return total


def _last_status_for(conn: sqlite3.Connection, worker_id: str) -> str | None:
    row = conn.execute(
        "SELECT payload FROM events WHERE worker_id = ? AND kind = 'status' "
        "ORDER BY id DESC LIMIT 1",
        (worker_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        val = json.loads(row[0]).get("progress")
        return val if isinstance(val, str) else None
    except Exception:  # noqa: BLE001
        return None


def render_snapshot(
    db: Path,
    *,
    since_id: int,
    include_tools: bool = False,
    cost_mode: str = "tokens",
) -> str:
    """Render a markdown state snapshot for engineers."""
    conn = state.connect(db)
    try:
        engineers = _engineers(conn)
        lines: list[str] = []
        lines.append("| worker | status | new events | cost | last status |")
        lines.append("|---|---|---|---|---|")
        for w in engineers:
            n = _new_event_count(conn, w.id, since_id, include_tools)
            last = _last_status_for(conn, w.id) or "(none)"
            if cost_mode == "dollars":
                usd = _cost_usd_for(conn, w.id, w.model)
                cost_str = f"${usd:>7.2f}"
            else:
                inp, out, cache = _token_summary_for(conn, w.id)
                cost_str = cost_mod.format_tokens(inp, out, cache)
            lines.append(f"| {w.id} | {w.status} | {n} | {cost_str} | {last} |")
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
    cost_mode: str = "tokens",
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
            return current_max, render_snapshot(
                db, since_id=since_id, include_tools=include_tools, cost_mode=cost_mode,
            )
        if time.monotonic() >= deadline:
            return current_max, render_snapshot(
                db, since_id=since_id, include_tools=include_tools, cost_mode=cost_mode,
            )
        time.sleep(min(poll_interval_s, max(0.0, deadline - time.monotonic())))
