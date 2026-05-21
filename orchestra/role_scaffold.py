"""Pure-logic scaffolder for orchestra role files."""
from __future__ import annotations

from pathlib import Path

_REVIEWER_ALLOW = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(cat:*)",
    "Bash(ls:*)",
    "Bash(grep:*)",
    "Bash(find:*)",
]

_REVIEWER_DENY = [
    "Write",
    "Edit",
    "NotebookEdit",
    "Bash(rm:*)",
    "Bash(git push:*)",
    "Bash(git commit:*)",
    "Bash(git checkout:*)",
]

_RUNNER_EXTRA_ALLOW = [
    "Bash(adb:*)",
    "Bash(node:*)",
    "Bash(npm:*)",
    "Bash(curl:*)",
    "Bash(timeout:*)",
    "Bash(sleep:*)",
    "Bash(echo:*)",
    "Bash(printf:*)",
    "Bash(mkdir:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
]

# Runner deny: reviewer deny minus Bash(git commit:*)
_RUNNER_DENY = [
    "Write",
    "Edit",
    "NotebookEdit",
    "Bash(rm:*)",
    "Bash(git push:*)",
    "Bash(git checkout:*)",
]

# Use __ARCH__ as sentinel replaced at scaffold time; {worker_id} etc. remain
# as literal str.format_map placeholders for orchestra to fill at spawn time.
_BODY_TEMPLATE = (
    "## ROLE: __ARCH__\n"
    "Worker ID: {worker_id}\n"
    "Workspace: {cwd}  (branch: {branch})\n"
    "\n"
    "{brief_section}\n"
    "### COORDINATION\n"
    '- The PM is at worker id \'pm\'. To ask a question, use:\n'
    '    orchestra worker escalate --blocking --question "..." --context "..."\n'
    "- When you finish, mark yourself done with EXACTLY:\n"
    '    orchestra worker done --summary "<one-sentence summary>"\n'
    "  Then end your session.\n"
    "\n"
    "### RULES\n"
    "- Stay in {cwd}. Do not touch files outside your workspace.\n"
    "- Do not spawn workers.\n"
)


def _frontmatter_block(allow: list[str], deny: list[str]) -> str:
    lines = ["---", "permissions:", "  allow:"]
    for item in allow:
        lines.append(f'    - "{item}"')
    lines.append("  deny:")
    for item in deny:
        lines.append(f'    - "{item}"')
    lines.append("---")
    return "\n".join(lines) + "\n"


def _render_body(archetype: str) -> str:
    return _BODY_TEMPLATE.replace("__ARCH__", archetype)


def scaffold(
    name: str,
    *,
    dest_dir: Path,
    engineer: bool = False,
    reviewer: bool = False,
    runner: bool = False,
    force: bool = False,
) -> Path:
    """Generate a role markdown file in dest_dir and return its path.

    Raises ValueError if more than one archetype flag is set.
    Raises FileExistsError if the file already exists and force is False.
    """
    if sum([engineer, reviewer, runner]) > 1:
        raise ValueError("at most one of engineer/reviewer/runner may be set")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.md"

    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists (use --force to overwrite)")

    if runner:
        allow = _REVIEWER_ALLOW + _RUNNER_EXTRA_ALLOW
        content = _frontmatter_block(allow, _RUNNER_DENY) + _render_body("Runner")
    elif reviewer:
        content = _frontmatter_block(_REVIEWER_ALLOW, _REVIEWER_DENY) + _render_body("Reviewer")
    else:
        content = _render_body("Engineer")

    dest.write_text(content)
    return dest
