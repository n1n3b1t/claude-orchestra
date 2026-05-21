"""Tests for orchestra.role_scaffold."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.role_prompts import _load_role
from orchestra.role_scaffold import scaffold


class TestRoleScaffold:
    def test_engineer_archetype_omits_front_matter(self, tmp_path: Path) -> None:
        dest = scaffold("myeng", dest_dir=tmp_path)
        content = dest.read_text()
        assert not content.startswith("---")
        assert "## ROLE: Engineer" in content

    def test_reviewer_archetype_has_deny_write(self, tmp_path: Path) -> None:
        dest = scaffold("myrev", dest_dir=tmp_path, reviewer=True)
        content = dest.read_text()
        assert "deny:" in content
        # Write appears in the deny block
        deny_section = content.split("deny:")[1]
        assert "Write" in deny_section

    def test_runner_archetype_allows_adb(self, tmp_path: Path) -> None:
        dest = scaffold("myrun", dest_dir=tmp_path, runner=True)
        content = dest.read_text()
        allow_section = content.split("allow:")[1].split("deny:")[0]
        assert "Bash(adb:*)" in allow_section
        deny_section = content.split("deny:")[1]
        assert "Bash(git commit:*)" not in deny_section

    def test_runner_archetype_keeps_other_denies(self, tmp_path: Path) -> None:
        dest = scaffold("myrun2", dest_dir=tmp_path, runner=True)
        content = dest.read_text()
        deny_section = content.split("deny:")[1]
        for expected in ["Write", "Edit", "Bash(rm:*)", "Bash(git push:*)", "Bash(git checkout:*)"]:
            assert expected in deny_section

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        scaffold("dup", dest_dir=tmp_path)
        with pytest.raises(FileExistsError):
            scaffold("dup", dest_dir=tmp_path)

    def test_force_overwrites(self, tmp_path: Path) -> None:
        scaffold("dup2", dest_dir=tmp_path, engineer=True)
        dest = scaffold("dup2", dest_dir=tmp_path, reviewer=True, force=True)
        content = dest.read_text()
        assert "## ROLE: Reviewer" in content

    def test_role_loadable_via_role_prompts(self, tmp_path: Path) -> None:
        roles_dir = tmp_path / ".orchestra" / "roles"

        scaffold("myeng", dest_dir=roles_dir)
        _body, perms = _load_role("myeng", project_root=tmp_path)
        assert perms == {}

        scaffold("myrev", dest_dir=roles_dir, reviewer=True)
        _body, perms = _load_role("myrev", project_root=tmp_path)
        assert isinstance(perms.get("allow"), list)
        assert isinstance(perms.get("deny"), list)
        assert "Write" in perms["deny"]

        scaffold("myrun", dest_dir=roles_dir, runner=True)
        _body, perms = _load_role("myrun", project_root=tmp_path)
        assert isinstance(perms.get("allow"), list)
        assert "Bash(adb:*)" in perms["allow"]
        assert "Bash(git commit:*)" not in perms.get("deny", [])
