"""Mission scaffolding + slug helpers.

Pure-Python helpers (no DB I/O). The CLI calls these from cli.py, then
writes the resulting Mission row via orchestra.state.
"""
from __future__ import annotations

import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

MISSION_TEMPLATE = """\
# Mission: <one-line goal>

Replace this paragraph with a few sentences describing what you want built.

## Acceptance
- <criterion 1>
- <criterion 2>

## Team
- engineer (sonnet) — implements the work.

You only emit `worker_done` when every acceptance check passes.
"""

VERIFIER_TEMPLATE = """\
#!/usr/bin/env bash
set -euo pipefail
# Replace these checks with your real acceptance commands.
# Exit 0 = pass, non-zero = fail.
echo "verifier skeleton — replace with real checks"
"""


class InvalidSlugError(ValueError):
    """Slug failed the regex check."""


class SlugCollisionError(FileExistsError):
    """missions/<slug>/ already exists on disk or in the missions table."""


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"slug {slug!r} must match {SLUG_RE.pattern} "
            "(lowercase alphanumerics + dashes, must not start with a dash)"
        )


def scaffold_mission_dir(project_root: Path, *, slug: str) -> Path:
    """Create missions/<slug>/{mission.md,verifier.sh}.

    Raises:
        InvalidSlugError: slug fails the regex.
        SlugCollisionError: missions/<slug>/ already exists.
    """
    validate_slug(slug)
    target = project_root / "missions" / slug
    if target.exists():
        raise SlugCollisionError(f"{target} already exists")
    target.mkdir(parents=True)
    (target / "mission.md").write_text(MISSION_TEMPLATE)
    verifier = target / "verifier.sh"
    verifier.write_text(VERIFIER_TEMPLATE)
    verifier.chmod(0o755)
    return target
