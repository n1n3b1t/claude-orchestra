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


class TestEnsurePerms:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_perms(p, {"allow": ["Read"], "deny": ["Write"]})
        assert p.exists()
        data = json.loads(p.read_text())
        assert data == {"permissions": {"allow": ["Read"], "deny": ["Write"]}}

    def test_preserves_existing_hooks(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_hooks(p)
        before = json.loads(p.read_text())
        settings_merge.ensure_perms(p, {"allow": ["Read"], "deny": []})
        after = json.loads(p.read_text())
        assert after["hooks"] == before["hooks"]
        assert after["permissions"]["allow"] == ["Read"]

    def test_dedupes_on_second_call(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        perms = {"allow": ["Read", "Grep"], "deny": ["Write"]}
        settings_merge.ensure_perms(p, perms)
        settings_merge.ensure_perms(p, perms)
        data = json.loads(p.read_text())
        assert data["permissions"]["allow"] == ["Read", "Grep"]
        assert data["permissions"]["deny"] == ["Write"]

    def test_merges_with_existing_perms_preserving_user_entries(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "permissions": {"allow": ["WebFetch"], "deny": []}
        }))
        settings_merge.ensure_perms(p, {"allow": ["Read"], "deny": ["Edit"]})
        data = json.loads(p.read_text())
        assert "WebFetch" in data["permissions"]["allow"]
        assert "Read" in data["permissions"]["allow"]
        assert data["permissions"]["deny"] == ["Edit"]

    def test_empty_input_is_noop(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_perms(p, {})
        assert not p.exists()

    def test_drops_non_string_entries(self, tmp_path: Path) -> None:
        p = tmp_path / ".claude" / "settings.local.json"
        settings_merge.ensure_perms(p, {"allow": ["Read", 42, None], "deny": []})
        data = json.loads(p.read_text())
        assert data["permissions"]["allow"] == ["Read"]
