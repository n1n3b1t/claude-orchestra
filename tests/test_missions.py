"""Tests for the orchestra/missions.py scaffolding module."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestra import missions


class TestSlugValidation:
    @pytest.mark.parametrize("slug", [
        "urlshortener", "kanban-v2", "abc", "a1", "a-b-c", "0name",
    ])
    def test_valid(self, slug: str) -> None:
        missions.validate_slug(slug)  # no raise

    @pytest.mark.parametrize("slug", [
        "", "-leading", "UPPER", "has_underscore", "has.dot", "has space",
    ])
    def test_invalid(self, slug: str) -> None:
        with pytest.raises(missions.InvalidSlugError):
            missions.validate_slug(slug)


class TestScaffold:
    def test_creates_files(self, tmp_path: Path) -> None:
        missions.scaffold_mission_dir(tmp_path, slug="m1")
        assert (tmp_path / "missions" / "m1" / "mission.md").is_file()
        assert (tmp_path / "missions" / "m1" / "verifier.sh").is_file()
        # Verifier executable bit set.
        assert (tmp_path / "missions" / "m1" / "verifier.sh").stat().st_mode & 0o111

    def test_mission_template_mentions_worker_done(self, tmp_path: Path) -> None:
        missions.scaffold_mission_dir(tmp_path, slug="m1")
        body = (tmp_path / "missions" / "m1" / "mission.md").read_text()
        assert "worker_done" in body

    def test_refuses_if_dir_exists(self, tmp_path: Path) -> None:
        (tmp_path / "missions" / "m1").mkdir(parents=True)
        with pytest.raises(missions.SlugCollisionError):
            missions.scaffold_mission_dir(tmp_path, slug="m1")
