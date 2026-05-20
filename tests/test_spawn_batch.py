"""Tests for orchestra.spawn_batch — parallel worker spawn."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orchestra import spawn_batch
from orchestra.__main__ import app

runner = CliRunner()


class TestParse:
    def test_parses_jsonl(self, tmp_path: Path) -> None:
        p = tmp_path / "specs.jsonl"
        p.write_text(
            '{"id":"a","model":"sonnet"}\n'
            '{"id":"b","model":"sonnet","worktree":"b"}\n'
        )
        specs = spawn_batch.parse_jsonl(p)
        assert len(specs) == 2
        assert specs[0]["id"] == "a"
        assert specs[1]["worktree"] == "b"

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        with pytest.raises(ValueError, match="no specs"):
            spawn_batch.parse_jsonl(p)

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "specs.jsonl"
        p.write_text('{"id":"a","model":"sonnet"}\n\n\n')
        specs = spawn_batch.parse_jsonl(p)
        assert len(specs) == 1


class TestConcurrency:
    def test_runs_in_parallel(self, tmp_path: Path) -> None:
        """Two specs whose spawn takes 2s each should complete in <3.5s
        when run via spawn_batch (truly concurrent), not >4s (serial)."""
        calls = []

        def fake_spawn_worker(conn, **kw):
            calls.append(kw["worker_id"])
            time.sleep(2.0)

        specs = [
            {"id": "a", "model": "sonnet"},
            {"id": "b", "model": "sonnet"},
        ]
        with patch("orchestra.spawn_batch.spawn.spawn_worker", side_effect=fake_spawn_worker):
            start = time.monotonic()
            results = spawn_batch.run(
                specs=specs,
                project_root=str(tmp_path),
                state_db=tmp_path / "state.db",
                session_name="orch-test",
            )
        elapsed = time.monotonic() - start
        assert elapsed < 3.5, f"serial pattern detected: {elapsed:.2f}s"
        assert sorted(calls) == ["a", "b"]
        assert all(r["status"] == "ok" for r in results)


class TestFailureModes:
    def test_one_failure_others_complete(self, tmp_path: Path) -> None:
        def fake_spawn(conn, **kw):
            if kw["worker_id"] == "bad":
                raise RuntimeError("boom")

        specs = [
            {"id": "good", "model": "sonnet"},
            {"id": "bad", "model": "sonnet"},
            {"id": "alsogood", "model": "sonnet"},
        ]
        with patch("orchestra.spawn_batch.spawn.spawn_worker", side_effect=fake_spawn):
            results = spawn_batch.run(
                specs=specs,
                project_root=str(tmp_path),
                state_db=tmp_path / "state.db",
                session_name="orch-test",
            )
        by_id = {r["id"]: r for r in results}
        assert by_id["good"]["status"] == "ok"
        assert by_id["bad"]["status"] == "error"
        assert "boom" in by_id["bad"]["error"]
        assert by_id["alsogood"]["status"] == "ok"


class TestCli:
    def test_spawn_batch_command_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        init = runner.invoke(app, ["init"])
        assert init.exit_code == 0, init.output

        spec = tmp_path / "specs.jsonl"
        spec.write_text(
            '{"id":"a","model":"sonnet"}\n'
            '{"id":"b","model":"sonnet"}\n'
        )

        def fake_spawn_worker(conn, **kw):
            return None

        with patch("orchestra.spawn_batch.spawn.spawn_worker", side_effect=fake_spawn_worker):
            result = runner.invoke(app, ["spawn-batch", str(spec)])
        assert result.exit_code == 0, result.output
        assert "a: ok" in result.output
        assert "b: ok" in result.output

    def test_spawn_batch_command_partial_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        init = runner.invoke(app, ["init"])
        assert init.exit_code == 0, init.output

        spec = tmp_path / "specs.jsonl"
        spec.write_text(
            '{"id":"good","model":"sonnet"}\n'
            '{"id":"bad","model":"sonnet"}\n'
        )

        def fake_spawn(conn, **kw):
            if kw["worker_id"] == "bad":
                raise RuntimeError("boom")

        with patch("orchestra.spawn_batch.spawn.spawn_worker", side_effect=fake_spawn):
            result = runner.invoke(app, ["spawn-batch", str(spec)])
        assert result.exit_code == 2, result.output
        assert "good: ok" in result.output
        assert "bad: error" in result.output

    def test_spawn_batch_command_empty_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        init = runner.invoke(app, ["init"])
        assert init.exit_code == 0, init.output

        spec = tmp_path / "empty.jsonl"
        spec.write_text("")
        result = runner.invoke(app, ["spawn-batch", str(spec)])
        assert result.exit_code == 2
        assert "no specs" in result.output

    def test_spawn_batch_command_requires_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        spec = tmp_path / "specs.jsonl"
        spec.write_text('{"id":"a","model":"sonnet"}\n')
        result = runner.invoke(app, ["spawn-batch", str(spec)])
        assert result.exit_code == 2
        assert "orchestra init" in result.output
