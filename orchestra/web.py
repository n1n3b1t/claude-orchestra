"""FastAPI app — REST + SSE dashboard for orchestra."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from orchestra import state, tmux

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="claude-orchestra dashboard")


def _db_path() -> Path:
    p = os.environ.get("ORCHESTRA_STATE_DB")
    if p:
        return Path(p)
    # default: .orchestra/state.db relative to cwd
    return Path.cwd() / ".orchestra" / "state.db"


def _conn() -> sqlite3.Connection:
    return state.connect(_db_path())


# ---- HTML ----

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


# ---- REST ----

@app.get("/api/missions")
def list_missions_api() -> list[dict[str, Any]]:
    conn = _conn()
    try:
        return [dataclasses.asdict(m) for m in state.list_missions(conn)]
    finally:
        conn.close()


@app.get("/api/workers")
def list_workers_api(mission: str | None = None) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        if mission is None:
            return [dataclasses.asdict(w) for w in state.list_workers(conn)]
        # Filter by mission slug.
        m = state.get_mission_by_slug(conn, mission)
        if m is None:
            return []
        rows = conn.execute(
            "SELECT * FROM workers WHERE mission_id = ? ORDER BY started_at ASC",
            (m.id,),
        ).fetchall()
        return [dataclasses.asdict(state._row_to_worker(r)) for r in rows]
    finally:
        conn.close()


@app.get("/api/workers/{worker_id}")
def worker_detail(worker_id: str) -> dict[str, Any]:
    conn = _conn()
    try:
        w = state.get_worker(conn, worker_id)
        if w is None:
            raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
        events = state.list_events(conn, worker_id=worker_id, limit=50)
        return {"worker": dataclasses.asdict(w), "events": [dataclasses.asdict(e) for e in events]}
    finally:
        conn.close()


@app.get("/api/workers/{worker_id}/pane")
def worker_pane(worker_id: str, lines: int = 80) -> dict[str, str]:
    conn = _conn()
    try:
        w = state.get_worker(conn, worker_id)
        if w is None:
            raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
        return {"text": tmux.capture(w.pane_target, lines=lines)}
    finally:
        conn.close()


@app.get("/api/workers/{worker_id}/escalations")
def list_escalations_api(worker_id: str, open: bool = True) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        if open:
            escs = state.list_open_escalations(conn, worker_id=worker_id)
            return [dataclasses.asdict(e) for e in escs]
        raise HTTPException(status_code=400, detail="only open=true is supported in v0")
    finally:
        conn.close()


class AnswerIn(BaseModel):
    escalation_id: int
    answer: str


@app.post("/api/workers/{worker_id}/answer")
def answer_escalation(worker_id: str, body: AnswerIn) -> dict[str, Any]:
    conn = _conn()
    try:
        w = state.get_worker(conn, worker_id)
        if w is None:
            raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
        try:
            resolved = state.resolve_escalation(conn, body.escalation_id, answer=body.answer)
        except KeyError as exc:
            raise HTTPException(
                status_code=409, detail="escalation already resolved or unknown"
            ) from exc
        tmux.send_multiline(
            w.pane_target,
            f"Answer to escalation #{resolved.id}: {body.answer}",
            buffer_name=f"orch-ans-{resolved.id}",
        )
        state.update_worker(conn, worker_id, status="working")
        state.record_event(
            conn, "escalation_resolved", worker_id=worker_id,
            escalation_id=resolved.id, answer=body.answer,
        )
        return {"ok": True, "escalation_id": resolved.id}
    finally:
        conn.close()


# ---- SSE ----

@app.get("/api/stream")
async def stream(request: Request) -> EventSourceResponse:
    async def gen() -> AsyncGenerator[dict[str, Any], None]:
        last_id = 0
        # initial: send recent events to bootstrap the client
        conn = _conn()
        try:
            for e in state.list_events(conn, limit=50):
                last_id = max(last_id, e.id)
                yield {
                    "data": json.dumps({
                        "id": e.id,
                        "worker_id": e.worker_id,
                        "kind": e.kind,
                        "payload": e.payload,
                    })
                }
        finally:
            conn.close()
        while True:
            if await request.is_disconnected():
                break
            conn = _conn()
            try:
                new = state.list_events(conn, since_id=last_id, limit=200)
                for e in new:
                    last_id = max(last_id, e.id)
                    yield {
                        "data": json.dumps({
                            "id": e.id,
                            "worker_id": e.worker_id,
                            "kind": e.kind,
                            "payload": e.payload,
                        })
                    }
            finally:
                conn.close()
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen())
