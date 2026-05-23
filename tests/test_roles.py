"""Tests for the v2.0 filesystem role loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.role_prompts import (
    RoleNotFoundError,
    _load_role,
    render_engineer_prompt,
    render_pm_prompt,
    render_role,
)


class TestPrecedence:
    def test_bundled_role_loads(self, tmp_path: Path) -> None:
        body, perms = _load_role("pm", project_root=tmp_path)
        assert "## ROLE: Project Manager" in body
        assert perms == {}

    def test_project_override_wins(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / ".orchestra" / "roles"
        custom_dir.mkdir(parents=True)
        (custom_dir / "pm.md").write_text("## CUSTOM PM\n{worker_id}\n")
        body, perms = _load_role("pm", project_root=tmp_path)
        assert body == "## CUSTOM PM\n{worker_id}\n"
        assert perms == {}


class TestMissingRole:
    def test_unknown_role_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RoleNotFoundError, match="no role template: ghost"):
            _load_role("ghost", project_root=tmp_path)


class TestPermissions:
    def test_role_without_frontmatter_has_empty_perms(self, tmp_path: Path) -> None:
        _, perms = _load_role("engineer", project_root=tmp_path)
        assert perms == {}

    def test_role_with_permissions(self, tmp_path: Path) -> None:
        roles = tmp_path / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "reviewer.md").write_text(
            "---\n"
            "permissions:\n"
            "  allow:\n"
            "    - Read\n"
            "    - Grep\n"
            "  deny:\n"
            "    - Write\n"
            "---\n"
            "## ROLE: Reviewer\n"
            "Worker ID: {worker_id}\n"
        )
        body, perms = _load_role("reviewer", project_root=tmp_path)
        assert "## ROLE: Reviewer" in body
        assert perms == {"allow": ["Read", "Grep"], "deny": ["Write"]}


class TestRendering:
    def test_pm_prompt_byte_identical_to_v1(self) -> None:
        out = render_pm_prompt(
            mission="x", worker_id="pm", project_name="proj",
            engineer_specs=[("a", "sonnet", "do a")], verifier_block="true",
        )
        assert "## ROLE: Project Manager" in out
        assert "Project: proj" in out
        assert "Worker ID: pm" in out
        assert "### YOUR TEAM" in out
        assert "`a` (sonnet) — do a" in out
        assert "true" in out

    def test_engineer_prompt_byte_identical_to_v1(self) -> None:
        out = render_engineer_prompt(
            worker_id="w1", cwd="/some/cwd", branch="orch/w1",
            brief_path=None, brief_content="do the thing",
        )
        assert "## ROLE: Engineer" in out
        assert "Worker ID: w1" in out
        assert "Workspace: /some/cwd" in out
        assert "orch/w1" in out
        assert "do the thing" in out

    def test_unknown_placeholder_raises(self, tmp_path: Path) -> None:
        roles = tmp_path / ".orchestra" / "roles"
        roles.mkdir(parents=True)
        (roles / "broken.md").write_text("Hello {nope}\n")
        with pytest.raises(KeyError):
            render_role("broken", project_root=tmp_path)


class TestKanbanExampleAssets:
    """Make sure the shipped kanban example role files actually load via the
    v2.0 loader. Without this, the example role files are only smoke-tested
    by the (opt-in, expensive) e2e script.
    """

    @pytest.mark.parametrize(
        "name", ["architect", "backend", "web", "cli", "reviewer"]
    )
    def test_kanban_role_loads(self, name: str) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        body, perms = _load_role(name, project_root=repo_root / "missions" / "kanban")
        assert body, f"{name}.md body is empty"
        # All kanban roles use engineer-shape vars
        assert "{worker_id}" in body
        assert "{cwd}" in body
        assert "{brief_section}" in body
        assert isinstance(perms, dict)

    def test_reviewer_denies_write_and_push(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        _, perms = _load_role(
            "reviewer", project_root=repo_root / "missions" / "kanban"
        )
        deny = perms.get("deny", [])
        assert "Write" in deny
        assert "Edit" in deny
        assert "Bash(git push:*)" in deny

    def test_architect_allows_write_denies_destructive(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        _, perms = _load_role(
            "architect", project_root=repo_root / "missions" / "kanban"
        )
        assert "Write" in perms.get("allow", [])
        deny = perms.get("deny", [])
        assert "Bash(rm:*)" in deny
        assert "Bash(git push:*)" in deny
