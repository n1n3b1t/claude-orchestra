"""Tests for orchestra.role_prompts (PM + Engineer templates)."""
from __future__ import annotations

from orchestra import role_prompts

PM_REQUIRED_SUBSTRINGS = [
    "ROLE: Project Manager",
    "MISSION",
    "YOUR TEAM",
    "orchestra spawn",
    "orchestra poll",
    "orchestra answer",
    "orchestra merge",
    "Stay in one turn",  # mega-turn rule
    "phase:",            # phase status-write requirement
    "/compact",
    "VERIFIER",
    "GO",
    # Patch 4: PM must know to call worker done + /exit after verifier passes.
    "orchestra worker done --summary",
    "/exit",
]

ENGINEER_REQUIRED_SUBSTRINGS = [
    "ROLE: Engineer",
    "Worker ID",
    "Workspace",
    "your own git worktree",
    "orchestra worker escalate",
    "Stay in",  # cwd-only rule
    "Do not spawn workers",
    "Tests live",
    # Patch 4: engineer must use worker done, not worker status.
    "orchestra worker done --summary",
]


class TestPMPrompt:
    def test_required_directives_present(self) -> None:
        out = role_prompts.render_pm_prompt(
            mission="Build a URL shortener.",
            worker_id="pm",
            project_name="urlshortener",
            engineer_specs=[
                ("backend", "sonnet", "implements the FastAPI app, SQLite, tests"),
                ("frontend", "sonnet", "implements templates/index.html + static/style.css"),
            ],
            verifier_block="pytest -q && curl ...",
        )
        for s in PM_REQUIRED_SUBSTRINGS:
            assert s in out, f"missing: {s!r}\n---\n{out}\n---"
        # Engineer names appear:
        assert "backend" in out
        assert "frontend" in out


class TestEngineerPrompt:
    def test_required_directives_present(self) -> None:
        out = role_prompts.render_engineer_prompt(
            worker_id="backend",
            cwd="/tmp/proj/worktrees/backend",
            branch="orch/backend",
            brief_path=".orchestra/briefs/backend.md",
            brief_content=None,
        )
        for s in ENGINEER_REQUIRED_SUBSTRINGS:
            assert s in out, f"missing: {s!r}\n---\n{out}\n---"

    def test_inlined_brief_when_no_path(self) -> None:
        out = role_prompts.render_engineer_prompt(
            worker_id="backend",
            cwd="/tmp/proj",
            branch="orch/backend",
            brief_path=None,
            brief_content="Implement /shorten endpoint.",
        )
        assert "Implement /shorten" in out
