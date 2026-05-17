"""Tests for the settings.local.json deep-merge."""
from __future__ import annotations

import json
from pathlib import Path

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

    def test_null_event_value_handled(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"SessionStart": None}}))
        settings_merge.ensure_hooks(settings)
        got = json.loads(settings.read_text())
        entries = got["hooks"]["SessionStart"]
        assert isinstance(entries, list)
        assert any(HOOK_CMD in inner["command"]
                   for outer in entries for inner in outer["hooks"])
