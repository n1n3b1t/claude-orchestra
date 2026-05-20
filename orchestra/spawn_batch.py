"""orchestra spawn-batch — parallel worker spawn.

Reads a JSONL file where each line is a worker spec dict (id, model, role?,
brief?, worktree?) and dispatches them through ``spawn.spawn_worker`` in a
ThreadPoolExecutor. Each worker gets its own short-lived sqlite3 connection
(post-v1.2 #6 the spawn flow no longer pins a connection across its blocking
waits), so true concurrency is safe.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from orchestra import spawn, state


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a worker-spec JSONL file. Raises ValueError on empty input."""
    specs: list[dict[str, Any]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        specs.append(json.loads(line))
    if not specs:
        raise ValueError(f"no specs in {path}")
    return specs


def _spawn_one(
    spec: dict[str, Any],
    *,
    project_root: str,
    state_db: Path,
    session_name: str,
) -> dict[str, Any]:
    """Spawn a single worker. Owns its own conn for the duration."""
    conn = state.connect(state_db)
    try:
        spawn.spawn_worker(
            conn,
            worker_id=spec["id"],
            model=spec["model"],
            task=spec.get("task", ""),
            project_root=project_root,
            state_db=state_db,
            ctx_files=spec.get("ctx_files", []),
            session_name=session_name,
            role=spec.get("role"),
            brief=spec.get("brief"),
            worktree_name=spec.get("worktree"),
        )
        return {"id": spec["id"], "status": "ok"}
    except Exception as e:  # noqa: BLE001 — one failure shouldn't kill the batch
        return {"id": spec["id"], "status": "error", "error": repr(e)}
    finally:
        conn.close()


def run(
    *,
    specs: list[dict[str, Any]],
    project_root: str,
    state_db: Path,
    session_name: str,
) -> list[dict[str, Any]]:
    """Spawn all specs concurrently, return per-worker status dicts."""
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as ex:
        futures = [
            ex.submit(
                _spawn_one,
                s,
                project_root=project_root,
                state_db=state_db,
                session_name=session_name,
            )
            for s in specs
        ]
        for f in as_completed(futures):
            results.append(f.result())
    return results
