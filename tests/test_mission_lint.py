"""Tests for orchestra.mission_lint."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestra import mission_lint


def _project(tmp_path: Path) -> Path:
    """Create a project root with a .orchestra/briefs/ dir.

    Returns the project root path. Callers add briefs/roles as needed.
    """
    (tmp_path / ".orchestra" / "briefs").mkdir(parents=True)
    return tmp_path


def _write_mission(project_root: Path, body: str) -> Path:
    p = project_root / ".orchestra" / "briefs" / "mission.md"
    p.write_text(body)
    return p


_BACKEND_SPEC = (
    '{"id": "backend", "model": "sonnet",'
    ' "brief": ".orchestra/briefs/backend.md",'
    ' "role": "engineer", "worktree": "backend"}'
)
_FRONTEND_SPEC = (
    '{"id": "frontend", "model": "sonnet",'
    ' "brief": ".orchestra/briefs/frontend.md",'
    ' "role": "engineer", "worktree": "frontend"}'
)

WELL_FORMED = f"""# Mission: do stuff

Build the thing.

```jsonl
{_BACKEND_SPEC}
{_FRONTEND_SPEC}
```

## TEAM

The team is above.

## ACCEPTANCE

The PM calls `orchestra worker done --summary "..."` once verifier passes.
Look for `worker_done` events in the state DB.

## VERIFIER

```bash
true
```
"""


class TestHappyPath:
    def test_well_formed_mission_no_findings(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("backend brief")
        (root / ".orchestra" / "briefs" / "frontend.md").write_text("frontend brief")
        mission = _write_mission(root, WELL_FORMED)

        findings = mission_lint.lint(mission, project_root=root)
        assert findings == [], mission_lint.render(findings)
        assert not mission_lint.has_errors(findings)


class TestBriefChecks:
    def _root_with_missing_frontend(self, tmp_path: Path) -> tuple[Path, Path]:
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("backend brief")
        mission = _write_mission(root, WELL_FORMED)
        return root, mission

    def test_missing_brief_default_is_warning(self, tmp_path: Path) -> None:
        root, mission = self._root_with_missing_frontend(tmp_path)

        findings = mission_lint.lint(mission, project_root=root)
        brief_findings = [
            f for f in findings if "brief not found" in f.message and "frontend" in f.message
        ]
        assert len(brief_findings) == 1, mission_lint.render(findings)
        assert brief_findings[0].severity == "warning"
        assert not mission_lint.has_errors(findings), mission_lint.render(findings)

    def test_missing_brief_strict_is_error(self, tmp_path: Path) -> None:
        root, mission = self._root_with_missing_frontend(tmp_path)

        findings = mission_lint.lint(mission, project_root=root, strict=True)
        brief_findings = [
            f for f in findings if "brief not found" in f.message and "frontend" in f.message
        ]
        assert len(brief_findings) == 1, mission_lint.render(findings)
        assert brief_findings[0].severity == "error"
        assert mission_lint.has_errors(findings), mission_lint.render(findings)


class TestRoleChecks:
    def test_unknown_role_is_error(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("b")
        (root / ".orchestra" / "briefs" / "frontend.md").write_text("f")
        body = WELL_FORMED.replace('"role": "engineer"', '"role": "nonexistent-role"', 1)
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        errors = [f for f in findings if f.severity == "error"]
        assert any("nonexistent-role" in f.message for f in errors), mission_lint.render(
            findings
        )

    def test_project_override_role_resolves(self, tmp_path: Path) -> None:
        """A user-defined role in <root>/.orchestra/roles/ should resolve cleanly."""
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("b")
        (root / ".orchestra" / "briefs" / "frontend.md").write_text("f")
        roles_dir = root / ".orchestra" / "roles"
        roles_dir.mkdir()
        (roles_dir / "custom.md").write_text("# custom role\nbody.")
        body = WELL_FORMED.replace('"role": "engineer"', '"role": "custom"')
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        assert not mission_lint.has_errors(findings), mission_lint.render(findings)


class TestWorktreeChecks:
    def test_duplicate_worktree_is_error(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("b")
        (root / ".orchestra" / "briefs" / "frontend.md").write_text("f")
        body = WELL_FORMED.replace('"worktree": "frontend"', '"worktree": "backend"')
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        errors = [f for f in findings if f.severity == "error"]
        assert any(
            "duplicate worktree" in f.message and "backend" in f.message for f in errors
        ), mission_lint.render(findings)


class TestSectionChecks:
    def test_no_acceptance_or_verifier_is_error(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: skeleton\n\n"
            "## TEAM\n\nteam stuff\n\n"
            "Mentions worker_done so no warning.\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        errors = [f for f in findings if f.severity == "error"]
        assert any("ACCEPTANCE" in f.message or "VERIFIER" in f.message for f in errors)

    def test_acceptance_section_satisfies_check(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: skeleton\n\n"
            "## TEAM\n\nteam stuff\n\n"
            "## Acceptance\n\nworker_done is mentioned.\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        # case-insensitive `## Acceptance` should pass
        assert not any("ACCEPTANCE" in f.message for f in findings), mission_lint.render(
            findings
        )

    def test_verifier_section_satisfies_check(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: skeleton\n\n"
            "## TEAM\n\nteam stuff\n\n"
            "## VERIFIER\n\nworker_done is mentioned.\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        assert not any(
            "no ## ACCEPTANCE or ## VERIFIER" in f.message for f in findings
        ), mission_lint.render(findings)

    def test_no_team_is_warning(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: no team\n\n"
            "## ACCEPTANCE\n\nworker_done check.\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        warnings = [f for f in findings if f.severity == "warning"]
        assert any("TEAM" in f.message for f in warnings)
        # Must NOT escalate to error
        assert not any("TEAM" in f.message and f.severity == "error" for f in findings)

    def test_no_worker_done_is_warning(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: no terminator\n\n"
            "## TEAM\n\nteam\n\n"
            "## ACCEPTANCE\n\nDeclare done somehow.\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        warnings = [f for f in findings if f.severity == "warning"]
        assert any("worker_done" in f.message for f in warnings)
        assert not any(
            "worker_done" in f.message and f.severity == "error" for f in findings
        )


class TestRender:
    def test_render_ok_when_no_findings(self) -> None:
        assert mission_lint.render([]) == "OK"

    def test_render_includes_line_number(self) -> None:
        out = mission_lint.render([mission_lint.Finding("error", "boom", 42)])
        assert "line 42" in out and "error" in out and "boom" in out

    def test_render_line_optional(self) -> None:
        out = mission_lint.render([mission_lint.Finding("warning", "soft")])
        assert "line" not in out
        assert "warning" in out


class TestBundledKanban:
    def test_kanban_mission_lints_clean(self) -> None:
        """missions/kanban/mission.md should produce no errors (warnings allowed)."""
        repo_root = Path(__file__).resolve().parent.parent
        mission = repo_root / "missions" / "kanban" / "mission.md"
        assert mission.is_file(), f"missing fixture: {mission}"
        findings = mission_lint.lint(mission, project_root=repo_root)
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], mission_lint.render(findings)


class TestInvalidJson:
    def test_invalid_json_line_is_error(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: bad jsonl\n\n"
            "```jsonl\n"
            "{not valid json\n"
            "```\n\n"
            "## ACCEPTANCE\n\nworker_done.\n\n## TEAM\n\nx\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root)
        assert any(
            f.severity == "error" and "invalid JSON" in f.message for f in findings
        ), mission_lint.render(findings)


@pytest.mark.parametrize("strict", [False, True])
class TestOtherChecksAlwaysError:
    """unknown-role, missing-acceptance, duplicate-worktree are errors regardless of strict."""

    def test_unknown_role_always_error(self, tmp_path: Path, strict: bool) -> None:
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("b")
        (root / ".orchestra" / "briefs" / "frontend.md").write_text("f")
        body = WELL_FORMED.replace('"role": "engineer"', '"role": "nonexistent-role"', 1)
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root, strict=strict)
        assert any(
            f.severity == "error" and "nonexistent-role" in f.message for f in findings
        ), mission_lint.render(findings)

    def test_missing_acceptance_always_error(self, tmp_path: Path, strict: bool) -> None:
        root = _project(tmp_path)
        body = (
            "# Mission: skeleton\n\n## TEAM\n\nteam stuff\n\nMentions worker_done so no warning.\n"
        )
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root, strict=strict)
        assert any(
            f.severity == "error" and ("ACCEPTANCE" in f.message or "VERIFIER" in f.message)
            for f in findings
        ), mission_lint.render(findings)

    def test_duplicate_worktree_always_error(self, tmp_path: Path, strict: bool) -> None:
        root = _project(tmp_path)
        (root / ".orchestra" / "briefs" / "backend.md").write_text("b")
        (root / ".orchestra" / "briefs" / "frontend.md").write_text("f")
        body = WELL_FORMED.replace('"worktree": "frontend"', '"worktree": "backend"')
        mission = _write_mission(root, body)

        findings = mission_lint.lint(mission, project_root=root, strict=strict)
        assert any(
            f.severity == "error" and "duplicate worktree" in f.message for f in findings
        ), mission_lint.render(findings)


@pytest.mark.parametrize("section_header", ["## ACCEPTANCE", "## acceptance", "##  VERIFIER"])
def test_section_regex_case_and_spacing(tmp_path: Path, section_header: str) -> None:
    root = _project(tmp_path)
    body = f"# Mission\n\n{section_header}\n\nworker_done.\n\n## TEAM\n\nx\n"
    mission = _write_mission(root, body)
    findings = mission_lint.lint(mission, project_root=root)
    assert not any(
        "no ## ACCEPTANCE or ## VERIFIER" in f.message for f in findings
    ), mission_lint.render(findings)
