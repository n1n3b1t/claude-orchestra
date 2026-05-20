"""Tests for `orchestra merge` — single-arg legacy + batch form (#26)."""
from __future__ import annotations

import json
import subprocess as _sub
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestra import cli, state
from orchestra.__main__ import app

runner = CliRunner()


def _init_in(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output


def _seed_worker(wid: str) -> None:
    with cli._open_db() as conn:
        state.create_worker(
            conn, id=wid, task="t", model="sonnet",
            branch=f"orch/{wid}", pane_target=f"s:{wid}",
            role="engineer", worktree=wid,
        )


class TestMergeBatchHappyPath:
    def test_three_clean_merges_record_events_and_emit_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _init_in(tmp_path, monkeypatch)
        for wid in ("backend", "web", "cli"):
            _seed_worker(wid)

        calls: list[list[str]] = []

        def fake_run(argv, **kw):  # noqa: ANN001
            calls.append(list(argv))
            return _sub.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)

        result = runner.invoke(
            app, ["merge", "--batch", "backend", "web", "cli"],
        )
        assert result.exit_code == 0, result.output

        payload = json.loads(result.stdout)
        assert payload == [
            {"id": "backend", "status": "ok"},
            {"id": "web", "status": "ok"},
            {"id": "cli", "status": "ok"},
        ]

        # Three merges, no --abort.
        merge_argvs = [c for c in calls if "merge" in c and "--abort" not in c]
        assert len(merge_argvs) == 3
        assert not any("--abort" in c for c in calls)

        with cli._open_db() as conn:
            for wid in ("backend", "web", "cli"):
                kinds = [
                    e.kind for e in state.list_events(conn, worker_id=wid)
                ]
                assert "merge_attempted" in kinds
                assert "merge_ok" in kinds
                assert "merge_conflict" not in kinds


class TestMergeBatchConflictMidBatch:
    def test_conflict_aborts_remaining_and_records_only_attempted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _init_in(tmp_path, monkeypatch)
        for wid in ("backend", "web", "cli"):
            _seed_worker(wid)

        calls: list[list[str]] = []

        def fake_run(argv, **kw):  # noqa: ANN001
            calls.append(list(argv))
            # Second merge (orch/web) conflicts.
            if "merge" in argv and "orch/web" in argv:
                return _sub.CompletedProcess(
                    argv, 1, stdout="CONFLICT (content): ...", stderr="",
                )
            # merge --abort + other merges succeed.
            return _sub.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)

        result = runner.invoke(
            app, ["merge", "--batch", "backend", "web", "cli"],
        )
        # Any non-ok in batch → exit 2.
        assert result.exit_code == 2, result.output

        payload = json.loads(result.stdout)
        assert [r["id"] for r in payload] == ["backend", "web", "cli"]
        assert payload[0]["status"] == "ok"
        assert payload[1]["status"] == "conflict"
        assert "CONFLICT" in payload[1]["summary"]
        assert payload[2]["status"] == "skipped"

        # merge --abort was invoked once after the conflict.
        abort_calls = [c for c in calls if "--abort" in c]
        assert len(abort_calls) == 1

        # No third 'git merge orch/cli' call.
        cli_merge_calls = [c for c in calls if "orch/cli" in c]
        assert cli_merge_calls == []

        with cli._open_db() as conn:
            backend_kinds = [
                e.kind for e in state.list_events(conn, worker_id="backend")
            ]
            web_kinds = [
                e.kind for e in state.list_events(conn, worker_id="web")
            ]
            cli_kinds = [
                e.kind for e in state.list_events(conn, worker_id="cli")
            ]
        assert "merge_ok" in backend_kinds
        assert "merge_attempted" in web_kinds
        assert "merge_conflict" in web_kinds
        # The skipped worker has NO merge_attempted recorded.
        assert "merge_attempted" not in cli_kinds


class TestMergeSingleArgRegression:
    def test_single_arg_still_records_event_and_echoes_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _init_in(tmp_path, monkeypatch)
        _seed_worker("backend")

        def fake_run(argv, **kw):  # noqa: ANN001
            return _sub.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)

        result = runner.invoke(app, ["merge", "backend"])
        assert result.exit_code == 0, result.output
        # Legacy plain-text output is preserved.
        assert "merged orch/backend" in result.stdout
        # And NOT JSON.
        assert "{" not in result.stdout

        with cli._open_db() as conn:
            kinds = [
                e.kind for e in state.list_events(conn, worker_id="backend")
            ]
        assert "merge_attempted" in kinds
        assert "merge_ok" in kinds

    def test_single_arg_conflict_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _init_in(tmp_path, monkeypatch)
        _seed_worker("backend")

        def fake_run(argv, **kw):  # noqa: ANN001
            return _sub.CompletedProcess(argv, 1, stdout="CONFLICT", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)

        result = runner.invoke(app, ["merge", "backend"])
        # Legacy contract: single-arg conflict → exit 1, not 2.
        assert result.exit_code == 1

    def test_no_arg_and_no_batch_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _init_in(tmp_path, monkeypatch)
        result = runner.invoke(app, ["merge"])
        assert result.exit_code == 2
