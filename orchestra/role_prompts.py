"""Role-aware startup prompts for claude-orchestra (v2.0 filesystem loader).

Role templates live as markdown files. Lookup order:
1. `<project_root>/.orchestra/roles/<name>.md` (user override)
2. `orchestra/roles/<name>.md` (bundled built-in)

Each role file may carry a YAML front-matter block with a `permissions:`
key (allow/deny lists of Claude Code tool patterns). The body is a
`str.format_map`-formatted template — unknown placeholders raise
`KeyError`, which catches typos in user-defined role files early.

`render_pm_prompt` and `render_engineer_prompt` are backward-compat shims
that delegate to the filesystem loader; they preserve the v1.x function
signatures so existing callers (orchestra/spawn.py, tests) don't change.
"""
from __future__ import annotations

from collections.abc import Sequence
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from orchestra import _frontmatter


class RoleNotFoundError(ValueError):
    """Raised when neither the project override nor the bundled file exists."""


def _project_override_path(name: str, project_root: Path) -> Path:
    return project_root / ".orchestra" / "roles" / f"{name}.md"


def _read_bundled(name: str) -> str | None:
    pkg = files("orchestra.roles")
    target = pkg / f"{name}.md"
    if not target.is_file():
        return None
    with as_file(target) as f:
        return f.read_text()


def _load_role(name: str, project_root: Path) -> tuple[str, dict[str, Any]]:
    """Read a role file. Returns (body_template, permissions_dict).

    Project override at <project_root>/.orchestra/roles/<name>.md wins over
    the bundled orchestra/roles/<name>.md. Raises RoleNotFoundError if
    neither exists.
    """
    override = _project_override_path(name, project_root)
    if override.is_file():
        text = override.read_text()
    else:
        bundled = _read_bundled(name)
        if bundled is None:
            raise RoleNotFoundError(f"no role template: {name}")
        text = bundled
    meta, body = _frontmatter.parse(text)
    perms = meta.get("permissions") or {}
    if not isinstance(perms, dict):
        perms = {}
    return body, perms


def render_role(name: str, *, project_root: Path, **variables: Any) -> str:
    """Load a role template and format its body with `variables`.

    Unknown placeholders in the template raise KeyError via format_map —
    catches typos in user-defined role files early.
    """
    body, _perms = _load_role(name, project_root)
    return body.format_map(variables)


def render_pm_prompt(
    *,
    mission: str,
    worker_id: str,
    project_name: str,
    engineer_specs: Sequence[tuple[str, str, str]],
    verifier_block: str,
    project_root: Path | None = None,
) -> str:
    """Backward-compat shim. Renders the bundled pm.md (or override)."""
    if engineer_specs:
        team_rows = "\n".join(
            f"- `{eid}` ({model}) — {brief}" for eid, model, brief in engineer_specs
        )
        team_section = (
            "### YOUR TEAM\n"
            "You will spawn and coordinate these engineers:\n"
            f"{team_rows}\n\n"
        )
    else:
        team_section = ""
    return render_role(
        "pm",
        project_root=project_root or Path.cwd(),
        mission=mission,
        worker_id=worker_id,
        project_name=project_name,
        team_section=team_section,
        verifier_block=verifier_block,
    )


def render_engineer_prompt(
    *,
    worker_id: str,
    cwd: str,
    branch: str,
    brief_path: str | None,
    brief_content: str | None,
    project_root: Path | None = None,
) -> str:
    """Backward-compat shim. Renders the bundled engineer.md (or override)."""
    if brief_content is not None:
        brief_section = "### YOUR BRIEF\n" f"{brief_content}\n"
    elif brief_path is not None:
        brief_section = (
            "### YOUR BRIEF\n"
            f"Read your brief at `{brief_path}` before doing anything.\n"
        )
    else:
        brief_section = (
            "### YOUR BRIEF\n(none — wait for `orchestra send` instructions)\n"
        )
    return render_role(
        "engineer",
        project_root=project_root or Path.cwd(),
        worker_id=worker_id,
        cwd=cwd,
        branch=branch,
        brief_section=brief_section,
    )
