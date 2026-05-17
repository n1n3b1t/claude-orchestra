"""Tests for orchestra.hooks (spike layer: log-only)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra import hooks


class TestSpikeLogging:
    def test_logs_raw_stdin_to_debug_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / ".orchestra" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
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
        db.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ORCHESTRA_WORKER_ID", "w1")
        monkeypatch.setenv("ORCHESTRA_STATE_DB", str(db))
        rc = hooks.run_spike("Stop", stdin_text="not-json{")
        assert rc == 0
        log = db.parent / "hook-debug.log"
        assert log.exists()
        line = json.loads(log.read_text().strip())
        assert line["parse_error"] is True
        assert line["raw"] == "not-json{"
