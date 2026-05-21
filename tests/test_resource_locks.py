"""Focused tests for resource_locks blocking / contention semantics."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from orchestra import state


def _open(tmp_db: Path) -> sqlite3.Connection:
    conn = state.connect(tmp_db)
    state.init_schema(conn)
    return conn


class TestBlockingSemantics:
    def test_acquire_blocking_unblocks_when_released(self, tmp_db: Path) -> None:
        """Blocking acquire returns True once the holder releases the lock."""
        _open(tmp_db)  # init schema

        conn_a = state.connect(tmp_db)
        assert state.acquire_resource(conn_a, "device", "A", blocking=False) is True

        def release_after_delay() -> None:
            time.sleep(0.3)
            conn_rel = state.connect(tmp_db)
            state.release_resource(conn_rel, "device", "A")
            conn_rel.close()

        t = threading.Thread(target=release_after_delay, daemon=True)
        t.start()

        start = time.monotonic()
        conn_b = state.connect(tmp_db)
        result = state.acquire_resource(
            conn_b, "device", "B", blocking=True, timeout_s=5.0, poll_s=0.05
        )
        elapsed = time.monotonic() - start

        t.join(timeout=2.0)

        assert result is True
        assert elapsed >= 0.25, f"should have waited for release; elapsed={elapsed:.3f}s"
        assert elapsed < 2.0, f"should not have taken long after release; elapsed={elapsed:.3f}s"

    def test_acquire_blocking_times_out(self, tmp_db: Path) -> None:
        """Blocking acquire returns False after timeout_s with no release."""
        conn = _open(tmp_db)
        assert state.acquire_resource(conn, "device", "A", blocking=False) is True

        conn_b = state.connect(tmp_db)
        start = time.monotonic()
        result = state.acquire_resource(
            conn_b, "device", "B", blocking=True, timeout_s=0.5, poll_s=0.1
        )
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed >= 0.45, f"should have waited ~0.5s; elapsed={elapsed:.3f}s"
        assert elapsed < 2.0, f"should not block much past timeout; elapsed={elapsed:.3f}s"
